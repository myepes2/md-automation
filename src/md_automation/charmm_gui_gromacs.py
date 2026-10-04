"""Generate a lab-adaptable GROMACS pipeline from CHARMM-GUI output."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tarfile
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .cluster_config import ClusterConfig, resolve_cluster
from .discovery import (
    DiscoveredLayout,
    WorkflowConfig,
    discover_workflow,
    find_engine_dir,
    read_layout,
)
from .readme_parse import ReadmeInfo, parse_readme
from .schedulers import JobSpec, get_scheduler, render_submitter

__version__ = "0.2.0"


def _write_text(path: Path, content: str) -> None:
    """Write generated scripts with Unix line endings on every platform."""
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)


def _grompp(cluster: ClusterConfig, args: str) -> str:
    """One grompp command line, honoring the configured maxwarn."""
    maxwarn = f" -maxwarn {cluster.grompp_maxwarn}" if cluster.grompp_maxwarn else ""
    return f"{cluster.gmx} grompp{maxwarn} {args}"


def _mdrun(cluster: ClusterConfig, deffnm: str, double: bool = False) -> str:
    gmx = '"$GMXDIR/gmx_d"' if (double and cluster.gmx_dir) else ("gmx_d" if double else cluster.gmx)
    return f"{gmx} mdrun -v -deffnm {deffnm}"


def _build_jobs(
    config: WorkflowConfig,
    sim_name: str,
    cluster: ClusterConfig,
    prod_chunks: int,
) -> list[JobSpec]:
    """Turn a resolved workflow into ordered job specs (min -> eq -> prod)."""
    jobs: list[JobSpec] = []
    top = "-p ../topol.top -n ../index.ndx"

    minimum = config.min_mdp
    jobs.append(JobSpec(
        job_name=f"{sim_name}_min",
        workdir="minimization",
        script=f"gmx_{minimum}.sh",
        deffnm=minimum,
        commands=(
            _grompp(cluster, f"-f {minimum}.mdp -o {minimum}.tpr -c ../{config.init}.gro -r ../{config.rest_prefix}.gro {top}"),
            _mdrun(cluster, minimum, double=cluster.min_double),
        ),
        profile="cpu",
        walltime=cluster.time_min,
    ))

    last = config.equilibration_steps
    for i, stem in enumerate(config.equi_mdps, start=1):
        prev = (
            f"../../minimization/{minimum}.gro"
            if i == 1
            else f"../6_{i - 1}/{config.equi_mdps[i - 2]}.gro"
        )
        jobs.append(JobSpec(
            job_name=f"{sim_name}_eq{i}",
            workdir=f"equilibration/6_{i}",
            script=f"gmx_{stem}.sh",
            deffnm=stem,
            commands=(
                _grompp(cluster, f"-f {stem}.mdp -o {stem}.tpr -c {prev} -r {prev} -p ../../topol.top -n ../../index.ndx"),
                _mdrun(cluster, stem),
            ),
            profile="cpu",
            walltime=cluster.time_eq,
        ))

    for chunk in range(1, prod_chunks + 1):
        name = config.production_name(chunk, prod_chunks)
        if chunk == 1:
            inputs = f"-c ../equilibration/6_{last}/{config.equi_mdps[-1]}.gro"
        else:
            prev_name = config.production_name(chunk - 1, prod_chunks)
            inputs = f"-c {prev_name}.gro -t {prev_name}.cpt"
        jobs.append(JobSpec(
            job_name=f"{sim_name}_prod" if prod_chunks == 1 else f"{sim_name}_prod{chunk}",
            workdir="production",
            script=f"gmx_{name}.sh",
            deffnm=name,
            commands=(
                _grompp(cluster, f"-f {config.prod_mdp}.mdp -o {name}.tpr {inputs} -p ../topol.top -n ../index.ndx"),
                _mdrun(cluster, name),
            ),
            profile="gpu",
            walltime=cluster.time_prod,
        ))

    return jobs


def _prepare_output(engine_dir: Path, output: Path, config: WorkflowConfig, force: bool) -> None:
    """Copy the GROMACS input tree and create the lab workflow directories."""
    if output.exists():
        if not force:
            raise FileExistsError(f"Output already exists: {output}; use --force to replace it")
        shutil.rmtree(output)
    output.mkdir(parents=True)
    shutil.copytree(engine_dir, output, dirs_exist_ok=True)

    for name in ("minimization", "equilibration", "production", "structure", "analysis", "troubleshooting"):
        (output / name).mkdir(exist_ok=True)
    for i in range(1, config.equilibration_steps + 1):
        (output / "equilibration" / f"6_{i}").mkdir(parents=True, exist_ok=True)


def _move_inputs(output: Path, config: WorkflowConfig) -> None:
    """Place MDP files beside the stage scripts that consume them."""
    moves = [(config.min_mdp, "minimization")]
    moves.extend(
        (stem, f"equilibration/6_{i}") for i, stem in enumerate(config.equi_mdps, start=1)
    )
    moves.append((config.prod_mdp, "production"))

    for stem, directory in moves:
        source = output / f"{stem}.mdp"
        if not source.is_file():
            raise FileNotFoundError(f"Required MDP file is missing: {source}")
        shutil.move(str(source), output / directory / source.name)


def _write_preflight(output: Path, config: WorkflowConfig, cluster: ClusterConfig, prod_chunks: int) -> None:
    """Emit preflight.sh: grompp every stage without submitting anything."""
    ref = f"{config.init}.gro"
    lines = [
        "#!/usr/bin/env bash",
        "# Runs `gmx grompp` for every stage without submitting jobs; catches",
        "# topology/index/mdp problems before anything reaches the queue.",
        "# Stages whose real input .gro does not exist yet are checked against",
        f"# {ref} instead — still validates the mdp, topology, and index.",
        "set -uo pipefail",
        'cd "$(dirname "$0")"',
    ]
    if cluster.modules:
        lines.append(f"module load {cluster.modules}")
    if cluster.gmx_dir:
        lines.append(f"export GMXDIR={cluster.gmx_dir}")
    lines += [
        "mkdir -p .preflight",
        "status=0",
        "check() {",
        '    name="$1"; shift',
        f'    if {cluster.gmx} grompp "$@" -o ".preflight/${{name}}.tpr" > ".preflight/${{name}}.log" 2>&1; then',
        '        echo "[PASS] $name"',
        "    else",
        '        echo "[FAIL] $name -- see .preflight/${name}.log"',
        "        status=1",
        "    fi",
        "}",
        "",
    ]
    maxwarn = f" -maxwarn {cluster.grompp_maxwarn}" if cluster.grompp_maxwarn else ""
    lines.append(
        f"check {config.min_mdp} -f minimization/{config.min_mdp}.mdp "
        f"-c {config.init}.gro -r {config.rest_prefix}.gro -p topol.top -n index.ndx{maxwarn}"
    )
    for i, stem in enumerate(config.equi_mdps, start=1):
        lines.append(
            f"check {stem} -f equilibration/6_{i}/{stem}.mdp "
            f"-c {ref} -r {config.rest_prefix}.gro -p topol.top -n index.ndx{maxwarn}"
        )
    for chunk in range(1, prod_chunks + 1):
        name = config.production_name(chunk, prod_chunks)
        lines.append(
            f"check {name} -f production/{config.prod_mdp}.mdp "
            f"-c {ref} -p topol.top -n index.ndx{maxwarn}"
        )
    lines += ["", 'echo "[*] Preflight done (exit $status)."', "exit $status", ""]
    _write_text(output / "preflight.sh", "\n".join(lines))


def _write_manifest(
    output: Path,
    source: Path,
    layout: DiscoveredLayout,
    config: WorkflowConfig,
    jobs: list[JobSpec],
    cluster: ClusterConfig,
    prod_chunks: int,
) -> None:
    """Emit pipeline_manifest.json describing the generated pipeline."""
    digest = None
    if source.is_file():
        hasher = hashlib.sha256()
        with source.open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                hasher.update(block)
        digest = hasher.hexdigest()
    manifest = {
        "generator": f"md-automation {__version__}",
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": {"path": str(source), "sha256": digest, "layout": layout.kind},
        "name": output.name.removesuffix("_gmx_pipeline"),
        "scheduler": cluster.scheduler,
        "cluster": asdict(cluster),
        "workflow": {
            "init": config.init,
            "rest_prefix": config.rest_prefix,
            "min_mdp": config.min_mdp,
            "equi_mdps": list(config.equi_mdps),
            "prod_mdp": config.prod_mdp,
            "prod_step": config.prod_step,
            "prod_chunks": prod_chunks,
        },
        "stages": [
            {
                "name": job.deffnm,
                "workdir": job.workdir,
                "script": job.script,
                "profile": job.profile,
                "depends_on": jobs[i - 1].deffnm if i else None,
            }
            for i, job in enumerate(jobs)
        ],
    }
    _write_text(output / "pipeline_manifest.json", json.dumps(manifest, indent=2) + "\n")


def describe_plan(
    layout: DiscoveredLayout,
    info: ReadmeInfo | None,
    config: WorkflowConfig,
    cluster: ClusterConfig,
    prod_chunks: int,
    output: Path,
) -> str:
    """Human-readable summary of what setup_pipeline would generate."""
    lines = [
        f"layout:        {layout.kind}",
        f"engine dir:    {layout.engine_dir}",
        f"readme:        {layout.readme or 'none found'}",
        f"output dir:    {output}",
        f"scheduler:     {cluster.scheduler}",
        f"init/rest:     {config.init} / {config.rest_prefix}",
        f"minimization:  {config.min_mdp}.mdp",
        f"equilibration: {len(config.equi_mdps)} steps ({', '.join(config.equi_mdps)})",
        f"production:    {config.prod_mdp}.mdp x{prod_chunks}"
        + (f" (chunks named {config.prod_step or config.prod_mdp}_1..{prod_chunks})" if prod_chunks > 1 else ""),
    ]
    if info is not None and info.prod_cntmax and info.prod_cntmax != prod_chunks:
        lines.append(
            f"note: README suggests {info.prod_cntmax} production chunks "
            f"(prod_step={info.prod_step}); pass --prod-chunks {info.prod_cntmax} to match"
        )
    return "\n".join(lines)


def setup_pipeline(
    source_path: str | Path,
    sim_name: str = "XX",
    *,
    force: bool = False,
    cluster: ClusterConfig | None = None,
    prod_chunks: int = 1,
    dry_run: bool = False,
    output_dir: str | Path | None = None,
) -> Path | None:
    """Convert a CHARMM-GUI export into a pipeline; returns its path.

    ``source_path`` may be a ``.tgz``/``.tar`` archive or an already
    extracted directory. With ``dry_run=True`` nothing is written and the
    detection/planning result is printed instead (returns ``None``).
    """
    cluster = cluster or ClusterConfig()
    source = Path(source_path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"Input does not exist: {source}")
    if not sim_name or Path(sim_name).name != sim_name:
        raise ValueError("Simulation name must be a simple directory name")

    output = Path(output_dir).expanduser() if output_dir else Path.cwd() / f"{sim_name}_gmx_pipeline"

    if source.is_dir():
        layout = find_engine_dir(source)
        info = parse_readme(layout.readme) if layout.readme else None
        config = discover_workflow(layout, info)
        if dry_run:
            print(describe_plan(layout, info, config, cluster, prod_chunks, output))
            return None
        jobs = _build_jobs(config, sim_name, cluster, prod_chunks)
        _prepare_output(layout.engine_dir, output, config, force)
    else:
        with tempfile.TemporaryDirectory(prefix="charmm_gui_") as temporary:
            layout, info, config = read_layout(source, Path(temporary))
            if dry_run:
                print(describe_plan(layout, info, config, cluster, prod_chunks, output))
                return None
            jobs = _build_jobs(config, sim_name, cluster, prod_chunks)
            _prepare_output(layout.engine_dir, output, config, force)

    _move_inputs(output, config)

    scheduler = get_scheduler(cluster.scheduler)
    for job in jobs:
        _write_text(output / job.workdir / job.script, scheduler.render_job(job, cluster))
    submitter = "run_all.sh" if scheduler.name == "bash" else "submit_all.sh"
    _write_text(output / submitter, render_submitter(jobs, scheduler, submitter))
    _write_preflight(output, config, cluster, prod_chunks)
    _write_manifest(output, source, layout, config, jobs, cluster, prod_chunks)
    return output


def continue_pipeline(
    pipeline_dir: str | Path,
    chunks: int,
    *,
    cluster: ClusterConfig | None = None,
    prod_prefix: str | None = None,
    after_job: str | None = None,
) -> Path:
    """Emit additional production chunks into an existing pipeline.

    Reads ``pipeline_manifest.json`` for the scheduler, cluster values,
    and existing chunk numbering; falls back to ``prod_prefix`` /
    ``step7_production`` conventions for pipelines generated without one.
    """
    pipeline = Path(pipeline_dir).expanduser().resolve()
    manifest_path = pipeline / "pipeline_manifest.json"
    prod_dir = pipeline / "production"

    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if cluster is None:
            stored = {
                k: tuple(v) if k.endswith("_directives") else v
                for k, v in manifest["cluster"].items()
                if k in ClusterConfig.__dataclass_fields__
            }
            cluster = ClusterConfig(**stored)
        workflow = manifest["workflow"]
        prod_mdp = workflow["prod_mdp"]
        prod_step = workflow.get("prod_step") or prod_mdp.removesuffix("_production")
        sim_name = manifest.get("name", pipeline.name.removesuffix("_gmx_pipeline"))
        existing = [
            s["name"] for s in manifest["stages"]
            if s["workdir"] == "production"
        ]
    else:
        cluster = cluster or ClusterConfig()
        prod_mdp = prod_prefix or "step7_production"
        prod_step = prod_mdp.removesuffix("_production")
        sim_name = pipeline.name.removesuffix("_gmx_pipeline")
        if not (prod_dir / f"{prod_mdp}.mdp").is_file():
            raise FileNotFoundError(
                f"No pipeline_manifest.json and no production/{prod_mdp}.mdp under {pipeline}; "
                "pass --prod-prefix to name the production mdp"
            )
        existing = sorted(p.stem[4:] for p in prod_dir.glob("gmx_*.sh"))

    if not prod_dir.is_dir():
        raise FileNotFoundError(f"No production/ directory under {pipeline}")

    # Highest existing chunk index; 0 means only the bare prod_mdp ran.
    indices = [0]
    for name in existing:
        if name.startswith(f"{prod_step}_"):
            try:
                indices.append(int(name.rsplit("_", 1)[1]))
            except ValueError:
                pass
        elif name == prod_mdp:
            indices.append(0)
    last = max(indices)
    prev_name = f"{prod_step}_{last}" if last else prod_mdp

    scheduler = get_scheduler(cluster.scheduler)
    new_jobs: list[JobSpec] = []
    for i in range(1, chunks + 1):
        name = f"{prod_step}_{last + i}"
        new_jobs.append(JobSpec(
            job_name=f"{sim_name}_prod{last + i}",
            workdir="production",
            script=f"gmx_{name}.sh",
            deffnm=name,
            commands=(
                _grompp(cluster, f"-f {prod_mdp}.mdp -o {name}.tpr -c {prev_name}.gro -t {prev_name}.cpt -p ../topol.top -n ../index.ndx"),
                _mdrun(cluster, name),
            ),
            profile="gpu",
            walltime=cluster.time_prod,
        ))
        prev_name = name

    for job in new_jobs:
        _write_text(prod_dir / job.script, scheduler.render_job(job, cluster))

    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        'cd "$(dirname "$0")/production"',
        'echo "[*] Submitting production continuation..."',
    ]
    for i, job in enumerate(new_jobs):
        if scheduler.name == "bash":
            lines += [f'echo "[*] {job.job_name}"', f"bash {job.script}"]
            continue
        dep = "prev" if i else None
        if i == 0 and after_job:
            invocation = scheduler.submit_invocation(job, "AFTER").replace("${AFTER}", after_job)
        else:
            invocation = scheduler.submit_invocation(job, dep)
        lines += [f"jid=$({invocation})", f'echo "{job.job_name} -> $jid"', "prev=$jid"]
    lines.append('echo "[*] Done."')
    submitter_name = "run_continue.sh" if scheduler.name == "bash" else "submit_continue.sh"
    _write_text(pipeline / submitter_name, "\n".join(lines) + "\n")
    return pipeline


def _cluster_overrides(args: argparse.Namespace) -> dict:
    keys = (
        "scheduler", "gmx_dir", "mail_user", "cpu_account", "gpu_account",
        "cpu_partition", "gpu_partition", "grompp_maxwarn",
    )
    return {key: getattr(args, key) for key in keys if getattr(args, key, None) is not None}


def _add_cluster_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--scheduler", choices=["slurm", "pbs", "openpbs", "bash"],
                        help="Job scheduler to render for (default: slurm or config file)")
    parser.add_argument("--cluster", help="Profile name inside the cluster config file")
    parser.add_argument("--config", help="Path to a cluster.{yaml,json} config file")
    parser.add_argument("--gmx-dir", help="Directory containing the GROMACS executable")
    parser.add_argument("--mail-user", help="Scheduler notification address(es)")
    parser.add_argument("--cpu-account", help="Account for CPU jobs")
    parser.add_argument("--gpu-account", help="Account for the GPU job")
    parser.add_argument("--cpu-partition", help="Partition/queue for CPU jobs")
    parser.add_argument("--gpu-partition", help="Partition/queue for the GPU job")
    parser.add_argument("--grompp-maxwarn", type=int, help="Pass -maxwarn N to every grompp call")


def _resolve(args: argparse.Namespace) -> ClusterConfig:
    return resolve_cluster(args.cluster, args.config, _cluster_overrides(args))


def _main_build(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path, help="CHARMM-GUI export (.tgz/.tar archive or extracted directory)")
    parser.add_argument("--name", default="XX", help="Output simulation name")
    parser.add_argument("--force", action="store_true", help="Replace an existing output directory")
    parser.add_argument("--prod-chunks", type=int, default=1,
                        help="Emit N chained production jobs (prod_step_N) instead of one")
    parser.add_argument("--dry-run", action="store_true",
                        help="Detect layout and print the plan without writing anything")
    parser.add_argument("--output-dir", type=Path, help="Override the output directory")
    _add_cluster_args(parser)
    args = parser.parse_args(argv)

    try:
        cluster = _resolve(args)
        output = setup_pipeline(
            args.file, args.name, force=args.force, cluster=cluster,
            prod_chunks=args.prod_chunks, dry_run=args.dry_run, output_dir=args.output_dir,
        )
    except (FileNotFoundError, FileExistsError, OSError, tarfile.TarError, ValueError) as error:
        parser.error(str(error))
    if output is not None:
        print(f"Pipeline created in: {output}")
    return 0


def _main_continue(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="md-automation continue",
        description="Append production chunks to an existing pipeline.",
    )
    parser.add_argument("pipeline", type=Path, help="Existing *_gmx_pipeline directory")
    parser.add_argument("--chunks", type=int, required=True, help="Number of chunks to append")
    parser.add_argument("--after", help="Job id the first new chunk should wait on")
    parser.add_argument("--prod-prefix", help="Production mdp stem (only needed without a manifest)")
    _add_cluster_args(parser)
    args = parser.parse_args(argv)

    try:
        cluster = _resolve(args)
        continue_pipeline(
            args.pipeline, args.chunks, cluster=cluster,
            prod_prefix=args.prod_prefix, after_job=args.after,
        )
    except (FileNotFoundError, OSError, ValueError) as error:
        parser.error(str(error))
    print(f"Continuation scripts added under: {args.pipeline}")
    return 0


def main() -> int:
    """Dispatch between the build front door and the continue helper."""
    argv = sys.argv[1:]
    if argv[:1] == ["continue"]:
        return _main_continue(argv[1:])
    return _main_build(argv)


if __name__ == "__main__":
    raise SystemExit(main())

"""Generate a lab-adaptable GROMACS/SLURM workflow from CHARMM-GUI output."""

from __future__ import annotations

import argparse
import re
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ClusterConfig:
    """Cluster values used in generated SLURM files."""

    gmx_dir: str = "/path/to/gromacs/bin"
    mail_user: str = "you@example.org"
    cpu_account: str = "cpu_account"
    gpu_account: str = "gpu_account"
    modules: str = "gcc/11.4.0 cuda/11.8.0 openmpi/4.1.6"


@dataclass(frozen=True)
class WorkflowConfig:
    """Names extracted from the CHARMM-GUI ``README`` file."""

    init: str
    rest_prefix: str
    min_prefix: str
    equi_prefix: str
    prod_prefix: str
    equilibration_steps: int

    def equilibration_name(self, step: int) -> str:
        """Return the concrete filename prefix for one equilibration step."""
        return self.equi_prefix.replace("%d", str(step))


def parse_charmm_gui_config(readme: Path) -> WorkflowConfig:
    """Parse the C-shell ``set key = value`` section of a CHARMM-GUI README."""
    values: dict[str, str] = {}
    pattern = re.compile(r"^\s*set\s+([A-Za-z_]\w*)\s*=\s*(.*?)\s*$")

    with readme.open(encoding="utf-8") as handle:
        for line in handle:
            if "# Production" in line:
                break
            match = pattern.match(line)
            if match:
                key, value = match.groups()
                values[key] = value.strip("'\" `")

    try:
        steps = int(values.get("cntmax", "0"))
    except ValueError as error:
        raise ValueError("README contains a non-integer cntmax value") from error
    if steps < 1:
        raise ValueError("README must define at least one equilibration step")

    return WorkflowConfig(
        init=values.get("init", "step5_input"),
        rest_prefix=values.get("rest_prefix", "step5_input"),
        min_prefix=values.get("mini_prefix", "step6.0_minimization"),
        equi_prefix=values.get("equi_prefix", "step6.%d_equilibration"),
        prod_prefix=values.get("prod_prefix", "step7_production"),
        equilibration_steps=steps,
    )


def _safe_extract(archive: Path, destination: Path) -> None:
    """Extract an archive while rejecting path traversal and links."""
    destination = destination.resolve()
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        for member in members:
            target = (destination / member.name).resolve()
            if destination not in target.parents and target != destination:
                raise ValueError(f"Archive member escapes extraction directory: {member.name}")
            if member.issym() or member.islnk():
                raise ValueError(f"Archive links are not supported: {member.name}")
        tar.extractall(destination, members=members)


def _find_project_directory(extracted: Path) -> Path:
    """Find the directory to copy from a CHARMM-GUI archive."""
    candidates: list[Path] = []
    for readme in extracted.rglob("README"):
        if not readme.is_file():
            continue
        parent = readme.parent
        if (parent / "gromacs").is_dir():
            candidates.append(parent)
        elif parent.name.lower() == "gromacs":
            candidates.append(parent)

    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise ValueError("Could not identify a CHARMM-GUI directory containing README and GROMACS files")
    locations = ", ".join(str(path) for path in candidates)
    raise ValueError(f"Found multiple possible CHARMM-GUI project directories: {locations}")


def _prepare_output(source: Path, output: Path, config: WorkflowConfig, force: bool) -> None:
    """Copy the GROMACS input tree and create the lab workflow directories."""
    if output.exists():
        if not force:
            raise FileExistsError(f"Output already exists: {output}; use --force to replace it")
        shutil.rmtree(output)
    output.mkdir(parents=True)

    gromacs_source = source / "gromacs"
    copy_source = gromacs_source if gromacs_source.is_dir() else source
    shutil.copytree(copy_source, output, dirs_exist_ok=True)

    for name in ("minimization", "equilibration", "production", "structure", "analysis", "troubleshooting"):
        (output / name).mkdir(exist_ok=True)
    for step in range(1, config.equilibration_steps + 1):
        (output / "equilibration" / f"6_{step}").mkdir(parents=True, exist_ok=True)
    (output / "equilibration" / "7").mkdir(exist_ok=True)


def _move_inputs(output: Path, config: WorkflowConfig) -> None:
    """Place MDP files beside the stage scripts that consume them."""
    moves = [(config.min_prefix, "minimization")]
    moves.extend((config.equilibration_name(i), f"equilibration/6_{i}") for i in range(1, config.equilibration_steps + 1))
    moves.append((config.prod_prefix, "production"))

    for prefix, directory in moves:
        source = output / f"{prefix}.mdp"
        if not source.is_file():
            raise FileNotFoundError(f"Required MDP file is missing: {source}")
        shutil.move(str(source), output / directory / source.name)


def _slurm_script(job_name: str, time: str, partition: str, account: str, ntasks: int,
                  output_file: str, grompp: str, mdrun: str, cluster: ClusterConfig,
                  extra: str = "") -> str:
    """Render one self-contained SLURM job script."""
    return f"""#!/bin/bash
#SBATCH --job-name={job_name}
#SBATCH --time={time}
#SBATCH --partition={partition}
#SBATCH -A {account}
#SBATCH --nodes=1
#SBATCH --ntasks-per-node={ntasks}
{extra}
#SBATCH --output=\"{output_file}\"
#SBATCH --mail-type=BEGIN,END,FAIL
#SBATCH --mail-user={cluster.mail_user}

module load {cluster.modules}
export GMXDIR={cluster.gmx_dir}

{grompp}
{mdrun}
"""


def _write_text(path: Path, content: str) -> None:
    """Write generated scripts with Unix line endings on every platform."""
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)


def _write_scripts(output: Path, simulation_name: str, config: WorkflowConfig, cluster: ClusterConfig) -> None:
    """Generate stage scripts and a dependency-aware submission script."""
    minimum = config.min_prefix
    min_script = _slurm_script(
        f"{simulation_name}_min", "00:10:00", "shared", cluster.cpu_account, 32,
        f"{minimum}.out",
        f'"$GMXDIR/gmx" grompp -f {minimum}.mdp -o {minimum}.tpr -c ../{config.init}.gro -r ../{config.rest_prefix}.gro -p ../topol.top -n ../index.ndx',
        f'"$GMXDIR/gmx" mdrun -v -deffnm {minimum}', cluster,
    )
    _write_text(output / "minimization" / f"gmx_{minimum}.sh", min_script)

    for step in range(1, config.equilibration_steps + 1):
        current = config.equilibration_name(step)
        previous = f"../../minimization/{minimum}.gro" if step == 1 else f"../6_{step - 1}/{config.equilibration_name(step - 1)}.gro"
        script = _slurm_script(
            f"{simulation_name}_eq{step}", "2:00:00", "shared", cluster.cpu_account, 32,
            f"{current}.out",
            f'"$GMXDIR/gmx" grompp -f {current}.mdp -c {previous} -r {previous} -p ../../topol.top -o {current}.tpr -n ../../index.ndx',
            f'"$GMXDIR/gmx" mdrun -v -deffnm {current}', cluster,
        )
        _write_text(output / "equilibration" / f"6_{step}" / f"gmx_{current}.sh", script)

    final_eq = config.equilibration_name(config.equilibration_steps)
    production = _slurm_script(
        f"{simulation_name}_prod", "12:00:00", "ica100", cluster.gpu_account, 16,
        f"{config.prod_prefix}.out",
        f'"$GMXDIR/gmx" grompp -f {config.prod_prefix}.mdp -o {config.prod_prefix}.tpr -c ../equilibration/6_{config.equilibration_steps}/{final_eq}.gro -p ../topol.top -n ../index.ndx',
        f'"$GMXDIR/gmx" mdrun -v -deffnm {config.prod_prefix}', cluster,
        "#SBATCH --gres=gpu:1\n#SBATCH --mem-per-cpu=3GB\n#SBATCH --no-requeue",
    )
    _write_text(output / "production" / f"gmx_{config.prod_prefix}.sh", production)

    lines = ["#!/usr/bin/env bash", "set -euo pipefail", 'echo "[*] Starting Pipeline Submission..."', "cd minimization", f"jid_min=$(sbatch gmx_{minimum}.sh | awk '{{print $4}}')", 'echo "MINIMIZATION -> $jid_min"', "cd ..", 'prev="$jid_min"']
    for step in range(1, config.equilibration_steps + 1):
        current = config.equilibration_name(step)
        lines.extend([f"cd equilibration/6_{step}", f"jid=$(sbatch --dependency=afterok:${{prev}} gmx_{current}.sh | awk '{{print $4}}')", f'echo "EQUILIBRATION {step} -> $jid (after $prev)"', 'prev="$jid"', "cd ../.."]) 
    lines.extend(["cd production", f"jid_prod=$(sbatch --dependency=afterok:${{prev}} gmx_{config.prod_prefix}.sh | awk '{{print $4}}')", 'echo "PRODUCTION -> $jid_prod (after $prev)"', "cd ..", 'echo "[*] All jobs submitted successfully."'])
    _write_text(output / "submit_all.sh", "\n".join(lines) + "\n")


def setup_pipeline(tgz_path: str | Path, sim_name: str = "XX", *, force: bool = False, cluster: ClusterConfig | None = None) -> Path:
    """Convert ``tgz_path`` into a generated pipeline and return its path."""
    archive = Path(tgz_path).expanduser().resolve()
    if not archive.is_file():
        raise FileNotFoundError(f"Input archive does not exist: {archive}")
    if not sim_name or Path(sim_name).name != sim_name:
        raise ValueError("Simulation name must be a simple directory name")

    output = Path.cwd() / f"{sim_name}_gmx_pipeline"
    with tempfile.TemporaryDirectory(prefix="charmm_gui_") as temporary:
        extracted = Path(temporary)
        _safe_extract(archive, extracted)
        source = _find_project_directory(extracted)
        config = parse_charmm_gui_config(source / "README")
        _prepare_output(source, output, config, force)

    _move_inputs(output, config)
    _write_scripts(output, sim_name, config, cluster or ClusterConfig())
    return output


def main() -> int:
    """Parse command-line arguments and provide user-facing errors."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path, help="CHARMM-GUI GROMACS .tgz archive")
    parser.add_argument("--name", default="XX", help="Output simulation name")
    parser.add_argument("--force", action="store_true", help="Replace an existing output directory")
    parser.add_argument("--gmx-dir", default=ClusterConfig.gmx_dir, help="Directory containing the GROMACS executable")
    parser.add_argument("--mail-user", default=ClusterConfig.mail_user, help="SLURM notification address(es)")
    parser.add_argument("--cpu-account", default=ClusterConfig.cpu_account, help="SLURM account for CPU jobs")
    parser.add_argument("--gpu-account", default=ClusterConfig.gpu_account, help="SLURM account for the GPU job")
    args = parser.parse_args()

    cluster = ClusterConfig(args.gmx_dir, args.mail_user, args.cpu_account, args.gpu_account)
    try:
        output = setup_pipeline(args.file, args.name, force=args.force, cluster=cluster)
    except (FileNotFoundError, FileExistsError, OSError, tarfile.TarError, ValueError) as error:
        parser.error(str(error))
    print(f"Pipeline created in: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

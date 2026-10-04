"""Scheduler backends: render job scripts and a dependency-aware submitter.

Three backends share one job model: every generated script is resumable —
it exits immediately when its ``.done_<deffnm>`` marker exists and writes
the marker only after ``mdrun`` succeeds. Re-running the submitter after a
hand edit therefore skips finished stages and resumes at the first
incomplete one, under any scheduler (a skipped job still satisfies
``afterok`` dependencies) or with the plain-bash runner.
"""

from __future__ import annotations

from dataclasses import dataclass

from .cluster_config import ClusterConfig


@dataclass(frozen=True)
class JobSpec:
    """One pipeline stage to render."""

    job_name: str  # scheduler-facing name, e.g. "mysim_eq3"
    workdir: str  # relative to pipeline root, e.g. "equilibration/6_3"
    script: str  # filename inside workdir
    deffnm: str  # mdrun -deffnm value; basis of the .done marker
    commands: tuple[str, ...]
    profile: str  # "cpu" or "gpu" -> selects resources from ClusterConfig
    walltime: str


def _job_body(spec: JobSpec, cluster: ClusterConfig, guard_modules: bool = False) -> str:
    """Shared resumable body: skip marker, strict mode, env, commands."""
    done = f".done_{spec.deffnm}"
    lines = [
        f'if [ -f "{done}" ]; then',
        f'    echo "SKIP: {spec.deffnm} already finished (delete {done} to redo)"',
        "    exit 0",
        "fi",
        "set -euo pipefail",
        "",
    ]
    if cluster.modules:
        if guard_modules:
            lines += [
                "if command -v module >/dev/null 2>&1; then",
                f"    module load {cluster.modules}",
                "fi",
            ]
        else:
            lines.append(f"module load {cluster.modules}")
    if cluster.gmx_dir:
        lines.append(f"export GMXDIR={cluster.gmx_dir}")
    lines += ["", *spec.commands, "", f'touch "{done}"', ""]
    return "\n".join(lines)


def _mail_lines(cluster: ClusterConfig, directive: str, slurm: bool) -> list[str]:
    if not cluster.mail_user:
        return []
    if slurm:
        return [f"{directive} --mail-type=BEGIN,END,FAIL", f"{directive} --mail-user={cluster.mail_user}"]
    return [f"{directive} -m abe", f"{directive} -M {cluster.mail_user}"]


class SlurmScheduler:
    name = "slurm"

    def render_job(self, spec: JobSpec, cluster: ClusterConfig) -> str:
        res = cluster.resources(spec.profile)
        lines = [
            "#!/bin/bash",
            f"#SBATCH --job-name={spec.job_name}",
            f"#SBATCH --time={spec.walltime}",
        ]
        if res["partition"]:
            lines.append(f"#SBATCH --partition={res['partition']}")
        if res["account"]:
            lines.append(f"#SBATCH -A {res['account']}")
        lines += ["#SBATCH --nodes=1", f"#SBATCH --ntasks-per-node={res['ntasks']}"]
        lines += [f"#SBATCH {d}" for d in res["directives"]]
        lines.append(f'#SBATCH --output="{spec.deffnm}.out"')
        lines += _mail_lines(cluster, "#SBATCH", slurm=True)
        return "\n".join(lines) + "\n\n" + _job_body(spec, cluster)

    def submit_invocation(self, spec: JobSpec, dependency_var: str | None) -> str:
        dep = f" --dependency=afterok:${{{dependency_var}}}" if dependency_var else ""
        return f"sbatch{dep} {spec.script} | awk '{{print $4}}'"


class PbsScheduler:
    name = "pbs"

    def render_job(self, spec: JobSpec, cluster: ClusterConfig) -> str:
        res = cluster.resources(spec.profile)
        lines = [
            "#!/bin/bash",
            f"#PBS -N {spec.job_name}",
            f"#PBS -l walltime={spec.walltime}",
        ]
        if res["account"]:
            lines.append(f"#PBS -A {res['account']}")
        if res["partition"]:
            lines.append(f"#PBS -q {res['partition']}")
        lines.append(f"#PBS -l select=1:ncpus={res['ntasks']}")
        lines += [f"#PBS {d}" for d in res["directives"]]
        lines += ["#PBS -j oe", f"#PBS -o {spec.deffnm}.out"]
        lines += _mail_lines(cluster, "#PBS", slurm=False)
        # PBS starts in $HOME, not the submit directory.
        lines.append('cd "$PBS_O_WORKDIR"')
        return "\n".join(lines) + "\n\n" + _job_body(spec, cluster)

    def submit_invocation(self, spec: JobSpec, dependency_var: str | None) -> str:
        dep = f" -W depend=afterok:${{{dependency_var}}}" if dependency_var else ""
        return f"qsub{dep} {spec.script}"


class BashScheduler:
    """No scheduler: scripts run sequentially via run_all.sh."""

    name = "bash"

    def render_job(self, spec: JobSpec, cluster: ClusterConfig) -> str:
        return "#!/bin/bash\n\n" + _job_body(spec, cluster, guard_modules=True)

    def submit_invocation(self, spec: JobSpec, dependency_var: str | None) -> str:
        return f"bash {spec.script}"


SCHEDULERS: dict[str, type] = {
    "slurm": SlurmScheduler,
    "pbs": PbsScheduler,
    "openpbs": PbsScheduler,
    "bash": BashScheduler,
}


def get_scheduler(name: str):
    """Return a scheduler instance by name."""
    try:
        return SCHEDULERS[name.lower()]()
    except KeyError:
        raise ValueError(
            f"Unknown scheduler '{name}'; expected one of: {', '.join(sorted(SCHEDULERS))}"
        ) from None


def render_submitter(specs: list[JobSpec], scheduler, filename: str) -> str:
    """Render submit_all.sh (slurm/pbs) or run_all.sh (bash).

    The dependency chain is linear; each stage runs in its own workdir.
    """
    if scheduler.name == "bash":
        lines = [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            'cd "$(dirname "$0")"',
        ]
        for spec in specs:
            lines += [f'echo "[*] {spec.job_name}"', f'(cd "{spec.workdir}" && bash "{spec.script}")']
        lines.append('echo "[*] Pipeline finished."')
        return "\n".join(lines) + "\n"

    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        'cd "$(dirname "$0")"',
        'echo "[*] Starting pipeline submission..."',
    ]
    for i, spec in enumerate(specs):
        dep = "prev" if i else None
        var = f"jid{i}"
        lines += [
            f'cd "{spec.workdir}"',
            f"{var}=$({scheduler.submit_invocation(spec, dep)})",
            f'echo "{spec.job_name} -> ${var}"' + (f' (after $prev)' if dep else ""),
            "cd - >/dev/null",
            f"prev=${var}",
        ]
    lines.append('echo "[*] All jobs submitted successfully."')
    return "\n".join(lines) + "\n"

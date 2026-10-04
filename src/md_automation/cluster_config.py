"""Cluster configuration: user config file plus CLI overrides.

The generator never ships real cluster values. A config file supplies
them, searched in order:

1. ``--config PATH``
2. ``$MD_AUTOMATION_CONFIG``
3. ``./cluster.{yaml,yml,json}`` in the working directory
4. ``~/.config/md-automation/cluster.{yaml,yml,json}``

``.json`` files are parsed with the stdlib; ``.yaml``/``.yml`` require
``pyyaml`` (install the ``[config]`` extra). A file may hold one profile
or a ``profiles:`` mapping plus a ``default:`` profile name; ``--cluster``
selects. CLI flags override whatever the file provided.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path


@dataclass(frozen=True)
class ClusterConfig:
    """Every scheduler-visible value; empty strings render as omitted."""

    scheduler: str = "slurm"
    gmx_dir: str = ""  # empty -> call plain `gmx` from PATH
    mail_user: str = ""
    modules: str = ""
    cpu_account: str = ""
    cpu_partition: str = ""
    cpu_ntasks: int = 4
    gpu_account: str = ""
    gpu_partition: str = ""
    gpu_ntasks: int = 4
    time_min: str = "00:10:00"
    time_eq: str = "2:00:00"
    time_prod: str = "12:00:00"
    cpu_directives: tuple[str, ...] = ()
    gpu_directives: tuple[str, ...] = ("--gres=gpu:1",)
    grompp_maxwarn: int = 0  # 0 -> no -maxwarn flag
    min_double: bool = False  # use gmx_d for minimization mdrun

    @property
    def gmx(self) -> str:
        """The mdrun/grompp command as it appears in generated scripts."""
        return '"$GMXDIR/gmx"' if self.gmx_dir else "gmx"

    def resources(self, profile: str) -> dict:
        """Resource fields for a job profile ("cpu" or "gpu")."""
        prefix = f"{profile}_"
        return {
            "account": getattr(self, f"{prefix}account"),
            "partition": getattr(self, f"{prefix}partition"),
            "ntasks": getattr(self, f"{prefix}ntasks"),
            "directives": getattr(self, f"{prefix}directives"),
        }


_SEARCH_BASENAMES = ("cluster.yaml", "cluster.yml", "cluster.json")


def _candidate_paths(config_arg: str | None) -> list[Path]:
    if config_arg:
        return [Path(config_arg).expanduser()]
    env = os.environ.get("MD_AUTOMATION_CONFIG")
    if env:
        return [Path(env).expanduser()]
    cwd = Path.cwd()
    home = Path.home() / ".config" / "md-automation"
    return [cwd / name for name in _SEARCH_BASENAMES] + [home / name for name in _SEARCH_BASENAMES]


def _load_file(path: Path) -> dict:
    if path.suffix == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    try:
        import yaml
    except ImportError:
        raise ValueError(
            f"{path} is YAML but pyyaml is not installed; "
            "install with 'pip install md-automation[config]' or use a .json config file"
        ) from None
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def find_config_file(config_arg: str | None = None) -> Path | None:
    """Return the first existing config file in search order, if any."""
    for candidate in _candidate_paths(config_arg):
        if candidate.is_file():
            return candidate
    if config_arg:
        raise FileNotFoundError(f"Cluster config file not found: {config_arg}")
    return None


def _flatten_profile(profile: dict) -> dict:
    """Map a nested profile dict onto flat ClusterConfig field names."""
    flat = {k: v for k, v in profile.items() if not isinstance(v, dict)}
    for section in ("cpu", "gpu"):
        for key, value in (profile.get(section) or {}).items():
            flat[f"{section}_{key}"] = value
    for key in ("cpu_directives", "gpu_directives"):
        if isinstance(flat.get(key), list):
            flat[key] = tuple(flat[key])
    return flat


def resolve_cluster(
    profile_name: str | None = None,
    config_path: str | None = None,
    overrides: dict | None = None,
) -> ClusterConfig:
    """Merge built-in defaults, config-file profile, and CLI overrides."""
    values: dict = {}
    path = find_config_file(config_path)
    if path is not None:
        data = _load_file(path)
        if "profiles" in data:
            name = profile_name or data.get("default")
            profiles = data["profiles"]
            if name is None:
                if len(profiles) == 1:
                    name = next(iter(profiles))
                else:
                    raise ValueError(
                        f"{path} defines multiple profiles; pass --cluster "
                        f"{'|'.join(profiles)}"
                    )
            if name not in profiles:
                raise ValueError(f"Profile '{name}' not in {path} (have: {', '.join(profiles)})")
            values = _flatten_profile(profiles[name])
        else:
            values = _flatten_profile(data)

    for key, value in (overrides or {}).items():
        if value is not None:
            values[key] = value

    valid = {f for f in ClusterConfig.__dataclass_fields__}
    unknown = set(values) - valid
    if unknown:
        raise ValueError(f"Unknown cluster config keys: {', '.join(sorted(unknown))}")
    return replace(ClusterConfig(), **values)

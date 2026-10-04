"""Locate and describe the GROMACS input tree inside a CHARMM-GUI export.

Real exports come in more than one layout: standard builders put a README
beside ``gromacs/``, while the multicomponent assembler puts the README
*inside* ``gromacs/``, adds ``input.config.dat``/``sysinfo.dat`` at the
root, and may ship sibling engine dirs (``namd/``). Some downloads contain
no runnable engine output at all. Detection is therefore score-based —
the authoritative signal is ``topol.top`` + ``*.mdp`` files — and unknown
layouts raise errors that name what was actually found.
"""

from __future__ import annotations

import json
import re
import tarfile
from dataclasses import dataclass
from pathlib import Path

from .readme_parse import ReadmeInfo, parse_readme


class DiscoveryError(ValueError):
    """Raised when an export layout cannot be identified or is unsupported."""


@dataclass(frozen=True)
class DiscoveredLayout:
    """Where the runnable GROMACS inputs live inside an extracted export."""

    kind: str  # "standard" | "assembler"
    engine_dir: Path
    readme: Path | None
    config_dat: dict | None


@dataclass(frozen=True)
class WorkflowConfig:
    """Concrete step names for one export, derived from files on disk."""

    init: str
    rest_prefix: str
    min_mdp: str  # filename stem, e.g. "step6.0_minimization"
    equi_mdps: tuple[str, ...]  # stems in run order
    prod_mdp: str  # e.g. "step7_production"
    prod_step: str | None  # e.g. "step7" -> chunk names step7_1, step7_2, ...

    def equilibration_name(self, step: int) -> str:
        """Return the mdp stem for 1-based equilibration step ``step``."""
        return self.equi_mdps[step - 1]

    @property
    def equilibration_steps(self) -> int:
        return len(self.equi_mdps)

    def production_name(self, chunk: int, total: int) -> str:
        """Return the run name for 1-based production chunk ``chunk``."""
        if total <= 1:
            return self.prod_mdp
        base = self.prod_step or self.prod_mdp
        return f"{base}_{chunk}"


def _safe_extract(archive: Path, destination: Path) -> None:
    """Extract a tar archive while rejecting path traversal and links."""
    destination = destination.resolve()
    with tarfile.open(archive, "r:*") as tar:
        members = tar.getmembers()
        for member in members:
            target = (destination / member.name).resolve()
            if destination not in target.parents and target != destination:
                raise ValueError(f"Archive member escapes extraction directory: {member.name}")
            if member.issym() or member.islnk():
                raise ValueError(f"Archive links are not supported: {member.name}")
        tar.extractall(destination, members=members)


def _load_config_dat(root: Path) -> dict | None:
    """Read the assembler's ``input.config.dat`` JSON if present."""
    for candidate in root.rglob("input.config.dat"):
        if not candidate.is_file():
            continue
        try:
            return json.loads(candidate.read_text(encoding="utf-8", errors="replace"))
        except json.JSONDecodeError:
            return None
    return None


def _classify_failure(root: Path) -> str:
    """Describe what an unparsable export appears to be, for error messages."""
    names = {p.name.lower() for p in root.rglob("*") if p.is_file()}
    dirs = {p.name.lower() for p in root.rglob("*") if p.is_dir()}
    if "namd" in dirs:
        return "a NAMD-only export (a namd/ directory exists but no gromacs inputs)"
    if any(n.startswith("step1_pdbreader") for n in names):
        return "an intermediate PDB-reader/build download (no engine input directory)"
    return "an unrecognized layout (expected a directory containing topol.top and *.mdp files)"


def find_engine_dir(root: Path) -> DiscoveredLayout:
    """Find the directory holding ``topol.top`` + ``*.mdp`` under ``root``.

    Accepts both layouts: README beside ``gromacs/`` (standard) and README
    inside ``gromacs/`` (multicomponent assembler). Raises
    :class:`DiscoveryError` with a layout-specific message otherwise.
    """
    candidates = [
        topol.parent
        for topol in root.rglob("topol.top")
        if topol.is_file() and any(topol.parent.glob("*.mdp"))
    ]
    unique = list(dict.fromkeys(candidates))

    if not unique:
        raise DiscoveryError(
            f"Could not identify a CHARMM-GUI GROMACS export under {root}: "
            f"this looks like {_classify_failure(root)}"
        )
    if len(unique) > 1:
        preferred = [c for c in unique if c.name.lower() == "gromacs"]
        if len(preferred) == 1:
            unique = preferred
        else:
            locations = ", ".join(str(p) for p in unique)
            raise DiscoveryError(f"Found multiple candidate GROMACS input directories: {locations}")

    engine_dir = unique[0]
    readme = engine_dir / "README"
    if not readme.is_file():
        readme = engine_dir.parent / "README"
    if not readme.is_file():
        readme = None

    config_dat = _load_config_dat(root)
    kind = "assembler" if config_dat is not None else "standard"
    return DiscoveredLayout(kind=kind, engine_dir=engine_dir, readme=readme, config_dat=config_dat)


def _prefix_index(pattern: str, stem: str) -> int | None:
    """Extract the integer substituted into a printf-style prefix."""
    regex = re.escape(pattern)
    regex = re.sub(r"%0?\d*d", r"(\\d+)", regex)
    match = re.fullmatch(regex, stem)
    return int(match.group(1)) if match else None


def discover_workflow(layout: DiscoveredLayout, info: ReadmeInfo | None) -> WorkflowConfig:
    """Resolve concrete mdp stems from the engine dir, using the README.

    The README supplies the naming prefixes; the filesystem supplies the
    counts. If the README is missing or a declared prefix matches nothing,
    falls back to classifying every ``*.mdp`` by name.
    """
    mdps = {p.stem for p in layout.engine_dir.glob("*.mdp")}
    if not mdps:
        raise DiscoveryError(f"No .mdp files found in {layout.engine_dir}")

    if info is not None:
        equi = sorted(
            ((idx, stem) for stem in mdps if (idx := _prefix_index(info.equi_prefix, stem)) is not None),
        )
        mini = info.mini_prefix if info.mini_prefix in mdps else None
        prod = info.prod_prefix if info.prod_prefix in mdps else None
    else:
        equi, mini, prod = [], None, None

    if not equi or mini is None or prod is None:
        # Fallback: classify by filename. Covers missing READMEs and
        # differently-named step prefixes without guessing at prefixes.
        guessed_equi = sorted(
            (
                (int(m.group(1)) if (m := re.search(r"\d+", s)) else 0, s)
                for s in mdps
                if "equil" in s.lower()
            )
        )
        if not equi:
            equi = guessed_equi
        if mini is None:
            mini = next((s for s in sorted(mdps) if "minim" in s.lower()), None)
        if prod is None:
            prod = next((s for s in sorted(mdps) if "prod" in s.lower()), None)

    missing = [label for label, stem in (("equilibration", equi), ("minimization", mini), ("production", prod)) if not stem]
    if missing:
        raise DiscoveryError(
            f"Could not identify {', '.join(missing)} inputs in {layout.engine_dir}; "
            f"found .mdp files: {', '.join(sorted(mdps)) or 'none'}"
        )

    if info is not None and info.equi_cntmax is not None and info.equi_cntmax != len(equi):
        # Files on disk win; the README count is advisory (assembler READMEs
        # reuse cntmax for the production loop, so mismatches are expected).
        pass

    return WorkflowConfig(
        init=info.init if info else "step5_input",
        rest_prefix=info.rest_prefix if info else "step5_input",
        min_mdp=mini,
        equi_mdps=tuple(stem for _, stem in equi),
        prod_mdp=prod,
        prod_step=info.prod_step if info else None,
    )


def read_layout(source: Path, workdir: Path) -> tuple[DiscoveredLayout, ReadmeInfo | None, WorkflowConfig]:
    """Extract (if needed) and fully describe a CHARMM-GUI export."""
    root = source if source.is_dir() else _extract_to(source, workdir)
    layout = find_engine_dir(root)
    info = parse_readme(layout.readme) if layout.readme else None
    return layout, info, discover_workflow(layout, info)


def _extract_to(archive: Path, workdir: Path) -> Path:
    """Extract an archive into ``workdir`` and return its single root."""
    _safe_extract(archive, workdir)
    entries = [p for p in workdir.iterdir()]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return workdir

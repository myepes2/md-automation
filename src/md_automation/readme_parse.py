"""Section-aware parsing of CHARMM-GUI c-shell README files.

CHARMM-GUI READMEs are runnable c-shell scripts built from
``set key = value`` assignments. Standard exports have one ``cntmax``
(equilibration); multicomponent-assembler exports reuse the variable in a
second ``# Production`` section that chunks production into
``${prod_step}_N`` runs. Parsing tracks which section each assignment
belongs to so the two counts never collide.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_SET_RE = re.compile(r"^\s*set\s+([A-Za-z_]\w*)\s*=\s*(.*?)\s*$")
_SECTION_RE = re.compile(r"^\s*#\s*(minimi|equilibr|production)", re.IGNORECASE)

_SECTION_NAMES = {"minimi": "minimization", "equilibr": "equilibration", "production": "production"}


@dataclass(frozen=True)
class ReadmeInfo:
    """Variables and section-scoped counters parsed from a README."""

    values: dict[str, str] = field(default_factory=dict)
    cntmax: dict[str, int] = field(default_factory=dict)  # section -> count

    def _get(self, key: str, default: str) -> str:
        return self.values.get(key, default)

    @property
    def init(self) -> str:
        return self._get("init", "step5_input")

    @property
    def rest_prefix(self) -> str:
        return self._get("rest_prefix", self.init)

    @property
    def mini_prefix(self) -> str:
        return self._get("mini_prefix", "step6.0_minimization")

    @property
    def equi_prefix(self) -> str:
        return self._get("equi_prefix", "step6.%d_equilibration")

    @property
    def prod_prefix(self) -> str:
        return self._get("prod_prefix", "step7_production")

    @property
    def prod_step(self) -> str | None:
        return self.values.get("prod_step")

    @property
    def equi_cntmax(self) -> int | None:
        """Equilibration step count, if the README declares one."""
        return self.cntmax.get("equilibration") or self.cntmax.get("preamble")

    @property
    def prod_cntmax(self) -> int | None:
        """Production chunk count (assembler READMEs), if declared."""
        return self.cntmax.get("production")


def parse_readme(readme: Path) -> ReadmeInfo:
    """Parse ``set`` assignments, tracking which README section owns each."""
    values: dict[str, str] = {}
    cntmax: dict[str, int] = {}
    section = "preamble"

    with readme.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            section_match = _SECTION_RE.match(line)
            if section_match:
                section = _SECTION_NAMES[section_match.group(1).lower()]
                continue
            set_match = _SET_RE.match(line)
            if not set_match:
                continue
            key, value = set_match.groups()
            value = value.strip("'\" `")
            if key == "cntmax":
                try:
                    cntmax[section] = int(value)
                except ValueError:
                    raise ValueError(f"{readme.name}: non-integer cntmax value {value!r}") from None
            else:
                values[key] = value

    return ReadmeInfo(values=values, cntmax=cntmax)

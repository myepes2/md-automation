"""Extract key-frame PDBs from a trajectory, optionally after alignment.

Companion to post-processing: when an aligned trajectory lands in the
FTSW data tree (``sims/<id>/proc/<variant>/``), this writes the key frames
to ``sims/<id>/frames/`` using the ``<name>_<frameindex>.pdb`` convention.

Requires MDAnalysis (``pip install md-automation[frames]``).

Example::

    md-frames --top noWIM.psf --traj run_s10_unwrap_aligned.dcd \
              --name 62x_s10 --frames first,last,every:50 \
              --outdir frames --select "protein or resname UNDP ANAM BNAG BNAM ADGG MDAP DALA"
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def parse_frames(spec: str, nframes: int) -> list[int]:
    """Expand a frame spec into a sorted list of trajectory indices.

    Grammar (comma-separated, deduplicated, clamped to [0, nframes)):

    - ``first`` / ``last`` — endpoints
    - ``all`` — every frame
    - ``every:N`` — every Nth frame starting at 0
    - ``quarters`` — 0, ~1/3, ~2/3, last (4 evenly spaced points)
    - ``range:A-B`` — inclusive index range
    - integers — explicit frame indices
    """
    if nframes <= 0:
        raise ValueError("trajectory has no frames")

    out: set[int] = set()
    for token in spec.split(","):
        token = token.strip().lower()
        if not token:
            continue
        if token == "first":
            out.add(0)
        elif token == "last":
            out.add(nframes - 1)
        elif token == "all":
            out.update(range(nframes))
        elif token == "quarters":
            out.update(round(i * (nframes - 1) / 3) for i in range(4))
        elif token.startswith("every:"):
            step = int(token.split(":", 1)[1])
            if step < 1:
                raise ValueError(f"bad stride in {token!r}")
            out.update(range(0, nframes, step))
        elif token.startswith("range:"):
            m = re.fullmatch(r"range:(\d+)-(\d+)", token)
            if not m:
                raise ValueError(f"bad range spec {token!r}")
            a, b = int(m.group(1)), int(m.group(2))
            out.update(range(a, min(b, nframes - 1) + 1))
        elif re.fullmatch(r"\d+", token):
            idx = int(token)
            if idx >= nframes:
                raise ValueError(f"frame {idx} out of range (nframes={nframes})")
            out.add(idx)
        else:
            raise ValueError(f"unrecognised frame spec token {token!r}")

    if not out:
        raise ValueError(f"frame spec {spec!r} selected no frames")
    return sorted(out)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="md-frames",
        description="Extract key-frame PDBs from a trajectory "
                    "(optionally aligned to a reference or frame 0).",
    )
    ap.add_argument("--top", required=True, type=Path,
                    help="topology (psf/dms/pdb/gro)")
    ap.add_argument("--traj", required=True, nargs="+", type=Path,
                    help="trajectory file(s), concatenated in order")
    ap.add_argument("--name", required=True,
                    help="output prefix, e.g. 62x_s10 -> 62x_s10_333.pdb")
    ap.add_argument("--outdir", type=Path, default=Path("frames"),
                    help="output directory (default: ./frames)")
    ap.add_argument("--frames", default="first,last",
                    help="frame spec: first,last,all,every:N,quarters,"
                         "range:A-B, or comma-separated indices "
                         "(default: first,last)")
    ap.add_argument("--select", default="all",
                    help="MDAnalysis selection written to each PDB "
                         "(default: all atoms)")
    ap.add_argument("--align", metavar="SEL",
                    help="MDAnalysis selection used for least-squares "
                         "alignment before writing (e.g. 'protein and name CA')")
    ap.add_argument("--align-ref", type=Path, metavar="PDB",
                    help="reference structure for --align "
                         "(default: trajectory frame 0)")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        import MDAnalysis as mda
        from MDAnalysis.analysis import align as mda_align
    except ImportError:
        print("md-frames requires MDAnalysis: "
              "pip install 'md-automation[frames]'", file=sys.stderr)
        return 2

    for p in [args.top, *args.traj]:
        if not p.is_file():
            print(f"error: not found: {p}", file=sys.stderr)
            return 1
    if args.align_ref is not None and not args.align_ref.is_file():
        print(f"error: not found: {args.align_ref}", file=sys.stderr)
        return 1

    u = mda.Universe(str(args.top), *[str(t) for t in args.traj])
    n = len(u.trajectory)
    try:
        indices = parse_frames(args.frames, n)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    ref = None
    if args.align:
        if args.align_ref:
            ref = mda.Universe(str(args.align_ref))
        else:
            u.trajectory[0]
            ref = mda.Merge(u.select_atoms("all"))

    args.outdir.mkdir(parents=True, exist_ok=True)
    written = []
    for i in indices:
        u.trajectory[i]
        if args.align:
            mda_align.alignto(u, ref, select=args.align)
        out = args.outdir / f"{args.name}_{i}.pdb"
        u.select_atoms(args.select).write(str(out))
        written.append(out)
        print(f"wrote {out}")

    # Print manifest-ready frame paths when the outdir sits under a sim folder.
    try:
        parts = args.outdir.resolve().parts
        if "sims" in parts:
            rel = Path(*parts[parts.index("sims"):])
            print("\nmanifest frames: entries:")
            for w in written:
                print(f"  - {(rel / w.name).as_posix()}")
    except (ValueError, IndexError):
        pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

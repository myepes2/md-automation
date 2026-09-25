# md-automation agent notes

Reusable CHARMM-GUI → GROMACS/SLURM pipeline generator (see README.md).
Deliberately lab-generic: no FTSW-specific analysis, no credentials, no
cluster-specific defaults. Keep it that way.

## Environment

- Working repo: `C:\Users\12404\Documents\GitHub\md-automation`
  (GitHub `myepes2/md-automation`, branch `master` — NOT main). The copy
  under `MDfolder\Code_Resources\` is a stale snapshot; never edit it.
- `git pull --rebase` before starting; push when done.
- Python ≥3.10; tests: `python -m pytest tests/` (use the
  `md-distance-analysis` conda env, not base anaconda — base is broken).

## Interface with the FtsW data tree

- Input: a CHARMM-GUI GROMACS export (`README` + `gromacs/` dir). For FtsW
  sims the authoritative per-build force-field set is
  `${FTSW_DATA}\sims\<id>_<name>\params\` — toppar differs between builds,
  never assume two sims share parameters.
- Generated pipelines are *inputs for new runs*, not data — do not write
  into `${FTSW_DATA}` directly. If a generated run later produces
  trajectories, the data-tree coordinator registers them in the manifest.
- Invoke the workspace `ftsw-data` skill before referencing any sim path;
  all paths are `${FTSW_DATA}`-relative, no drive letters.

## Boundaries

Owns: everything in this repo.
Does not own: `manifest.*`, the `FtsW_MD` tree, `FtsW-dynamics`,
`ftsw-figures`, `Anton3_Postprocessing`. Need a change there? Note it and
stop rather than editing across the boundary.

# Design note: multicomponent-assembler inputs + scheduler layer

Status: proposal, v0.2 target. Written after surveying real CHARMM-GUI
exports; see "Survey" for evidence.

## 1. What the real exports look like

### 1a. Multicomponent assembler (MCA) archive — the common new case

```
charmm-gui-<jobid>/
  input.config.dat        # JSON: {"systype","dimensions","input":[engines],
                        #        "forcefield":{"type","files","custom"},"hmr"}
  sysinfo.dat             # JSON component inventory, e.g.
                        # {"PROTEIN":{"PROA":1,...},"MEMBRANE":{"POPE":1028}}
  step1_*.inp/.str ...    # CHARMM-format build intermediates (unused)
  gromacs/
    README                # c-shell run script (NO README at archive root)
    index.ndx  topol.top  toppar/
    step5_input.{gro,pdb,psf}
    step6.0_minimization.mdp
    step6.{1..6}_equilibration.mdp
    step7_production.mdp
  namd/                   # sibling engine dir, own README + .inp files
```

Observed: job ids 6057485103, 6057458156, 5823759832, 8131689954. The
current discovery works here only by accident (the `parent.name ==
"gromacs"` fallback); see failure modes below.

### 1b. NAMD-only MCA export

`input.config.dat` says `"input":["namd"]`; there is no `gromacs/` dir at
all. Today this dies with the generic "Could not identify" error — fine,
but the message should say what *was* found (e.g. "namd-only export").

### 1c. PDB-reader / intermediate downloads

`step1_pdbreader.*` + `toppar/`, no engine dir. Must fail loudly.

### 1d. Standard Membrane/Solution Builder — the original target

Root `README` + `gromacs/` dir. Keep supporting it; don't regress.

### 1e. Unpacked directories

Users often have the already-extracted `charmm-gui-<id>/` folder, not the
.tgz. Accepting a directory input is a near-free win.

## 2. README grammar differences that matter

MCA `gromacs/README` vs the standard one:

- Two `set cntmax` blocks: equilibration (`=6`), then production (`=10`)
  driving **chunked** `step7_1 .. step7_10` runs chained via
  `-t ${pstep}.cpt`. Today's parser survives only because it stops at the
  literal comment `"# Production"` — brittle.
- Extra variable `set prod_step = step7` (chunk name prefix).
- Minimization runs `gmx_d mdrun` (double precision) in the README; our
  single-precision `gmx` is still fine.
- Equilibration grompp restrains with `-r ${rest_prefix}.gro` in every
  step (we already do this).

Robustness plan:

- Parse the c-shell `set` block into a dict, but derive the equilibration
  count from the **filesystem** (`equi_prefix`-patterned `*.mdp` files
  present) — the README says what files are *named*, the tree says what
  *exists*. Cross-check against the equilibration-section `cntmax` when
  parseable; warn on mismatch.
- Read the production-section `cntmax` + `prod_step` optionally to
  pre-fill `--prod-chunks` (below).
- Keep prefix fallbacks, but if the resolved prefix's `.mdp` is absent,
  error naming the expected file and the README keys seen.
- Unknown `set` keys and extra sections are ignored.
- Fail messages must identify the detected layout class
  (1a–1d) and the missing piece.

## 3. Discovery rewrite

`_find_project_directory` becomes score-based:

1. Candidates = every directory containing `topol.top` plus at least one
   `*.mdp` (handles both `gromacs/` subdir and bare engine dirs).
2. If exactly one → use it. If several → prefer a dir named `gromacs`
   or whose README parses; else error listing all candidates.
3. Zero candidates → classify the archive (namd-only? pdb-reader?
   empty?) and raise a targeted error.

The "project dir" becomes the engine dir itself in all cases; README
location is `engine_dir/README`, falling back to `parent/README`
(standard layout keeps the run README at root).

## 4. Scheduler abstraction

`md_automation/schedulers.py`:

```python
@dataclass(frozen=True)
class JobSpec:
    name: str            # e.g. "{sim}_eq3"
    workdir: str         # relative to pipeline root
    script_body: str     # grompp + mdrun lines
    profile: str         # "cpu" | "gpu" — selects resources from config

class Scheduler(Protocol):
    name: str
    def render_job(self, spec: JobSpec, cfg: ClusterConfig) -> str: ...
    def render_submit(self, jobs: list[JobSpec]) -> str: ...
    # dependency is a linear chain for this pipeline shape
```

Implementations:

- `slurm` — current output, but every value (partition, time, ntasks,
  gres, mem, account, mail, module loads, `GMXDIR`) comes from config.
- `pbs` / `openpbs` — `#PBS -N/-l walltime/-l select/-A/-q`, submit via
  `qsub -W depend=afterok:<jid>`; `-l` resource lines emitted from the
  same config keys.
- `bash` — no scheduler: jobs are plain `#!/bin/bash` scripts and
  `submit_all.sh` becomes `run_all.sh` that executes stages sequentially
  with `set -euo pipefail`. Useful for testing on a workstation and for
  clusters without a scheduler.

`--scheduler slurm|pbs|bash` selects; default `slurm` today, overridden
by config file. All scripts keep `\n` endings and aim to be
shellcheck-clean (`-x` clean at minimum).

## 5. Cluster config file

`~/.config/md-automation/cluster.yaml` (also honored:
`./cluster.yaml`/`--config PATH` for per-project overrides; CLI flags
override file values; file overrides built-in placeholders).

```yaml
default: rockfish            # which profile --cluster omitted means
profiles:
  rockfish:
    scheduler: slurm
    gmx_dir: /path/to/gromacs/bin
    mail_user: someone@example.org
    modules: "gcc/11.4.0 cuda/11.8.0 openmpi/4.1.6"
    cpu: {account: a1, partition: shared, ntasks: 32, time_min: "00:10:00", time_eq: "2:00:00"}
    gpu: {account: a2, partition: ica100, ntasks: 16, time: "12:00:00",
          extra_directives: ["--gres=gpu:1", "--mem-per-cpu=3GB", "--no-requeue"]}
```

Parsing: `.json` via stdlib `json`; `.yaml` via `pyyaml` **if importable**,
otherwise a clear error ("pip install pyyaml or use JSON"). This keeps the
core stdlib-only. Config keys map onto `ClusterConfig`, which gains the
currently-hard-coded fields (partitions, times, ntasks, gpu directives).

## 6. Production chunks + continuation

- `--prod-chunks N` (default 1, preserving current behavior): emits
  `production/step7_<i>` jobs — first grompps from the last equilibration
  `.gro`, subsequent ones add `-t step7_<i-1>.cpt`; chained afterok.
  Matches the CHARMM-GUI `prod_step`/`cntmax` convention, and matches how
  these pipelines were actually extended by hand (production2..20 dirs).
- `md-automation continue <pipeline> --chunks N` (or a generated
  `continue.sh`) appends N more chunks after the last existing
  `step7_<k>` — the "continuation helper" nice-to-have.

## 6b. Iteration resilience (user-requested)

Real usage: the pipeline usually fails once at minimization (grompp
warnings, `-maxwarn`, clashing atoms in the built PDB), gets hand-fixed,
resubmitted, and occasionally needs equilibration tweaks too. Two
mechanisms make that loop cheap:

- **Resumable stages.** Every generated job script begins by checking
  for its `.done_<deffnm>` marker and exits 0 immediately if present;
  the marker is `touch`ed only after `mdrun` succeeds (scripts run under
  `set -euo pipefail`). `submit_all.sh` can therefore be re-run verbatim
  after an edit — finished stages skip in seconds, `afterok` deps stay
  satisfied, and the chain resumes at the first unfinished stage. To
  redo a stage: delete its `.done_*` file. This works identically under
  slurm/pbs (skip-job satisfies `afterok`) and under the bash runner.
- **Preflight.** A generated `preflight.sh` runs `gmx grompp` for every
  stage without submitting — for stages whose input `.gro` doesn't exist
  yet it substitutes `step5_input.gro`, since grompp's topology/index/
  mdp validation (the class of errors that actually kills day one) is
  mostly coordinate-independent. `--dry-run` does the same inspection
  without writing anything.
- **Knobs in config, not edits.** `grompp_maxwarn`, double-precision
  minimization (`gmx_d`, which the CHARMM-GUI README itself uses for
  minimization), and resource values all come from the cluster config —
  regenerating a pipeline doesn't stomp hand-tuned values.

What stays manual, deliberately: physics problems (bad clashes,
under-equilibrated membranes) and judgment calls (restraint schedules,
how long to equilibrate). The tool's job is fast detection and a tight
edit→rerun loop, not auto-fixing.

## 7. Nice-to-haves wired in now

- `--dry-run`: parse + plan, print the stage table and detected layout,
  write nothing.
- `pipeline_manifest.json` at output root: generator version, source
  archive hash, detected layout class, stage list with files, scheduler,
  config profile used — the "generated manifest" nice-to-have.
- Directory input: if the positional arg is a dir, skip extraction.

## 8. Module layout after the change

```
src/md_automation/
  charmm_gui_gromacs.py   # CLI + orchestration only
  discovery.py            # extract/locate/classify archive layouts
  readme_parse.py         # c-shell set-block + section-aware parsing
  schedulers.py           # JobSpec + slurm/pbs/bash renderers
  cluster_config.py       # config-file loading + CLI override merge
  frames.py               # unchanged (stays generic; optional [frames])
```

## 9. Explicit non-goals / failure contract

- No NAMD/AMBER/OpenMM pipeline generation (namd dir is detected only to
  classify layouts).
- No per-lab defaults: built-in `ClusterConfig` values remain obvious
  placeholders; a real setup requires a config file or flags.
- Anything we can't classify → error out naming the layout we saw; never
  emit a half-valid pipeline.
- tests/ keep using synthetic fixtures; no real trajectories, no cluster.

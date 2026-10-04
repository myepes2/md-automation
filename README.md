# MD Automation

Reusable automation for molecular-dynamics workflows. The first tool converts a CHARMM-GUI GROMACS export into a lab-adaptable, scheduler-ready pipeline.

## Scope

Supports both CHARMM-GUI export layouts:

- **Standard** Solution/Membrane Builder archives — `README` beside `gromacs/`;
- **Multicomponent assembler** archives — `README` inside `gromacs/`, `input.config.dat`/`sysinfo.dat` at the root, sibling `namd/` directories.

Exports with no GROMACS inputs (NAMD-only, PDB-reader downloads) fail with a message naming the layout found. The generator emits:

- energy-minimization, sequential equilibration, and production job scripts — each **resumable** (a `.done_<step>` marker lets you fix inputs and re-run the submitter without repeating finished stages);
- `submit_all.sh` with `afterok` dependencies (SLURM or PBS) or `run_all.sh` for plain sequential bash;
- `preflight.sh`, which `grompp`s every stage before anything hits the queue;
- `pipeline_manifest.json` describing the generated pipeline.

It does not contain FTSW-specific analysis code, simulation data, credentials, or cluster-specific defaults.

## Install

Python 3.10 or newer is recommended because the package uses modern type annotations.

```text
python -m pip install -e .
python -m pip install -e '.[config]'   # adds pyyaml for YAML cluster configs
```

## Use

```text
md-automation charmm-gui.tgz --name my_simulation
```

The input may also be an already-extracted `charmm-gui-*` directory. `--dry-run` prints the detected layout and stage plan without writing anything. The generated directory is protected by default; `--force` replaces it.

```text
md-automation charmm-gui.tgz --name my_simulation --dry-run
md-automation ./charmm-gui-1234567890 --name my_simulation --prod-chunks 10
```

`--prod-chunks N` splits production into N chained `prod_step_N` jobs connected through `.cpt` checkpoints, matching the convention assembler READMEs use (`prod_step`, production `cntmax`). To extend a running pipeline later:

```text
md-automation continue my_simulation_gmx_pipeline --chunks 5 --after 12345
```

### Schedulers

`--scheduler slurm|pbs|openpbs|bash` selects the backend (default `slurm`). PBS jobs use `#PBS` directives and `qsub -W depend=afterok:`; `bash` writes directive-free scripts plus `run_all.sh`, useful for workstation testing.

### Cluster configuration

Cluster values come from a config file — never from code defaults. Searched in order: `--config PATH`, `$MD_AUTOMATION_CONFIG`, `./cluster.{yaml,yml,json}`, `~/.config/md-automation/cluster.{yaml,yml,json}`.

```yaml
default: mycluster
profiles:
  mycluster:
    scheduler: slurm
    gmx_dir: /opt/gromacs/bin        # empty -> call `gmx` from PATH
    mail_user: you@example.org
    modules: "gcc/11.4.0 cuda/11.8.0 openmpi/4.1.6"
    grompp_maxwarn: 0                # >0 adds -maxwarn N to every grompp
    cpu: {account: a1, partition: shared, ntasks: 32}
    gpu: {account: a2, partition: gpuq, ntasks: 16,
          directives: ["--gres=gpu:1", "--no-requeue"]}
```

`--cluster NAME` selects a profile. CLI flags (`--gmx-dir`, `--mail-user`, `--cpu-account`, `--gpu-account`, `--cpu-partition`, `--gpu-partition`, `--scheduler`, `--grompp-maxwarn`) override file values.

### Editing and re-running

Every stage script skips itself when `.done_<step>` exists, so after fixing an mdp you re-run `submit_all.sh` and the chain resumes at the first unfinished stage. Delete a stage's `.done_*` marker to force it to re-run.

## md-frames — key-frame extraction

`md-frames` pulls key-frame PDBs out of a trajectory, optionally after
least-squares alignment — the step that produces `frames/` in the FTSW
data tree when a trajectory is aligned during post-processing.

```text
pip install -e '.[frames]'   # pulls in MDAnalysis

md-frames --top top.psf --traj traj.dcd --name 62x_s10 \
          --frames first,last,quarters \
          --align "protein and name CA" \
          --select "protein or resname UNDP ANAM BNAG BNAM ADGG MDAP DALA" \
          --outdir frames
```

Frame spec: `first`, `last`, `all`, `every:N`, `quarters`, `range:A-B`,
or comma-separated indices (mixable). Output files are
`<name>_<frameindex>.pdb`; when `--outdir` sits under a `sims/` tree the
tool prints manifest-ready `frames:` entries. Alignment targets
`--align-ref` (a reference PDB) or trajectory frame 0.

## Development

Run the tests without needing GROMACS or a cluster:

```text
python -m pytest tests/
```

The tests cover export-layout detection (standard, assembler, NAMD-only,
PDB-reader), README parsing, archive path safety, scheduler rendering,
cluster config resolution, chunked production, and pipeline continuation.

## Design principles

- Keep the workflow narrow and explicit rather than pretending to support every export.
- Separate archive discovery, configuration parsing, filesystem operations, and script generation.
- Make destructive behavior opt-in.
- Keep cluster values configurable and out of source control.
- Test boundaries locally; reserve cluster execution for integration validation.

See `docs/design-v0.2.md` for the layout survey and scheduler design.

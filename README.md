# MD Automation

Reusable automation for molecular-dynamics workflows. The first tool converts a CHARMM-GUI GROMACS archive into a lab-adaptable GROMACS/SLURM pipeline.

## Scope

This project intentionally starts small. It supports CHARMM-GUI exports with a standard project `README` and `gromacs/` directory, then generates:

- energy-minimization inputs and a SLURM job;
- sequential equilibration jobs;
- a GPU production job; and
- `submit_all.sh` with `afterok` dependencies.

It does not contain FTSW-specific analysis code, simulation data, credentials, or cluster-specific defaults.

## Install

Python 3.10 or newer is recommended because the package uses modern type annotations.

```text
python -m pip install -e .
```

## Use

```text
md-automation charmm-gui.tgz --name my_simulation \
  --gmx-dir /path/to/gromacs/bin \
  --mail-user you@example.org \
  --cpu-account my_cpu_account \
  --gpu-account my_gpu_account
```

The generated directory is protected by default. Use `--force` only when replacing an existing output is intentional.

```text
md-automation charmm-gui.tgz --name my_simulation --force
```

## Development

Run the tests without needing GROMACS or a SLURM cluster:

```text
python -m unittest discover -s tests -v
```

The tests cover configuration parsing, archive path safety, and discovery of the CHARMM-GUI project directory when unrelated README files are present.

## Design principles

- Keep the workflow narrow and explicit rather than pretending to support every export.
- Separate archive discovery, configuration parsing, filesystem operations, and script generation.
- Make destructive behavior opt-in.
- Keep cluster values configurable and out of source control.
- Test boundaries locally; reserve cluster execution for integration validation.

## Future additions

Potential next tools include dry-run validation, generated manifests, ShellCheck validation, production continuation helpers, and carefully separated Anton preprocessing/postprocessing adapters.

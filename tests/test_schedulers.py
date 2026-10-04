import unittest

from md_automation.cluster_config import ClusterConfig
from md_automation.schedulers import JobSpec, get_scheduler, render_submitter


def _spec() -> JobSpec:
    return JobSpec(
        job_name="sim_eq1",
        workdir="equilibration/6_1",
        script="gmx_step6.1_equilibration.sh",
        deffnm="step6.1_equilibration",
        commands=("gmx grompp -f x.mdp", "gmx mdrun -v -deffnm step6.1_equilibration"),
        profile="cpu",
        walltime="2:00:00",
    )


class SchedulerRenderTests(unittest.TestCase):
    def test_slurm_job(self) -> None:
        cluster = ClusterConfig(cpu_partition="shared", cpu_account="acct", mail_user="a@b.org")
        text = get_scheduler("slurm").render_job(_spec(), cluster)
        self.assertIn("#SBATCH --job-name=sim_eq1", text)
        self.assertIn("#SBATCH --partition=shared", text)
        self.assertIn("#SBATCH -A acct", text)
        self.assertIn("#SBATCH --mail-user=a@b.org", text)
        self.assertIn('.done_step6.1_equilibration', text)
        self.assertIn("exit 0", text)

    def test_slurm_omits_empty_values(self) -> None:
        text = get_scheduler("slurm").render_job(_spec(), ClusterConfig())
        self.assertNotIn("--mail-user", text)
        self.assertNotIn("#SBATCH -A", text)
        self.assertNotIn("#SBATCH --partition", text)

    def test_pbs_job(self) -> None:
        cluster = ClusterConfig(cpu_partition="workq", cpu_account="acct")
        text = get_scheduler("pbs").render_job(_spec(), cluster)
        self.assertIn("#PBS -N sim_eq1", text)
        self.assertIn("#PBS -l walltime=2:00:00", text)
        self.assertIn("#PBS -q workq", text)
        self.assertIn('cd "$PBS_O_WORKDIR"', text)
        self.assertIn(".done_step6.1_equilibration", text)

    def test_pbs_submit_dependency(self) -> None:
        invocation = get_scheduler("pbs").submit_invocation(_spec(), "prev")
        self.assertEqual(invocation, "qsub -W depend=afterok:${prev} gmx_step6.1_equilibration.sh")

    def test_bash_job_has_no_directives(self) -> None:
        text = get_scheduler("bash").render_job(_spec(), ClusterConfig(modules="gcc"))
        self.assertNotIn("#SBATCH", text)
        self.assertNotIn("#PBS", text)
        self.assertIn("command -v module", text)  # guarded on workstations

    def test_slurm_submitter_chains_afterok(self) -> None:
        specs = [_spec(), _spec()]
        text = render_submitter(specs, get_scheduler("slurm"), "submit_all.sh")
        self.assertIn("sbatch gmx_step6.1_equilibration.sh | awk '{print $4}'", text)
        self.assertIn("--dependency=afterok:${prev}", text)

    def test_bash_submitter_runs_sequentially(self) -> None:
        text = render_submitter([_spec()], get_scheduler("bash"), "run_all.sh")
        self.assertIn('(cd "equilibration/6_1" && bash "gmx_step6.1_equilibration.sh")', text)
        self.assertNotIn("sbatch", text)

    def test_unknown_scheduler_errors(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown scheduler"):
            get_scheduler("sge")


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import unittest
from pathlib import Path

from md_automation.cluster_config import ClusterConfig, resolve_cluster


class ClusterConfigTests(unittest.TestCase):
    def test_defaults_are_placeholder_free(self) -> None:
        cfg = ClusterConfig()
        self.assertEqual(cfg.scheduler, "slurm")
        self.assertEqual(cfg.gmx, "gmx")  # PATH lookup when no gmx_dir

    def test_gmx_dir_wraps_executable(self) -> None:
        cfg = ClusterConfig(gmx_dir="/opt/gromacs/bin")
        self.assertEqual(cfg.gmx, '"$GMXDIR/gmx"')

    def test_json_profile_with_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cluster.json"
            path.write_text(json.dumps({
                "default": "lab",
                "profiles": {
                    "lab": {
                        "scheduler": "pbs",
                        "gmx_dir": "/opt/gmx/bin",
                        "mail_user": "a@b.org",
                        "cpu": {"account": "cpuacct", "partition": "shared", "ntasks": 32},
                        "gpu": {"account": "gpuacct", "partition": "gpuq",
                                "directives": ["-l ngpus=1"]},
                    }
                },
            }), encoding="utf-8")
            cfg = resolve_cluster(config_path=str(path))
        self.assertEqual(cfg.scheduler, "pbs")
        self.assertEqual(cfg.cpu_account, "cpuacct")
        self.assertEqual(cfg.cpu_ntasks, 32)
        self.assertEqual(cfg.gpu_directives, ("-l ngpus=1",))

    def test_cli_overrides_beat_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cluster.json"
            path.write_text(json.dumps({"mail_user": "file@x.org", "scheduler": "pbs"}),
                            encoding="utf-8")
            cfg = resolve_cluster(config_path=str(path),
                                  overrides={"mail_user": "cli@x.org"})
        self.assertEqual(cfg.mail_user, "cli@x.org")
        self.assertEqual(cfg.scheduler, "pbs")

    def test_multiple_profiles_require_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cluster.json"
            path.write_text(json.dumps({"profiles": {"a": {}, "b": {}}}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "--cluster"):
                resolve_cluster(config_path=str(path))
            cfg = resolve_cluster(profile_name="b", config_path=str(path))
            self.assertEqual(cfg.scheduler, "slurm")

    def test_unknown_keys_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cluster.json"
            path.write_text(json.dumps({"bogus_key": 1}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "bogus_key"):
                resolve_cluster(config_path=str(path))

    def test_yaml_config(self) -> None:
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("pyyaml not installed")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cluster.yaml"
            path.write_text(
                "scheduler: pbs\ngmx_dir: /opt/gmx/bin\ncpu:\n  account: acct\n  ntasks: 16\n",
                encoding="utf-8",
            )
            cfg = resolve_cluster(config_path=str(path))
        self.assertEqual(cfg.scheduler, "pbs")
        self.assertEqual(cfg.cpu_account, "acct")


if __name__ == "__main__":
    unittest.main()

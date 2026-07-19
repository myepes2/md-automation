import tarfile
import tempfile
import unittest
from pathlib import Path

from md_automation.charmm_gui_gromacs import (
    _find_project_directory,
    _safe_extract,
    parse_charmm_gui_config,
)


class CharmmGuiGromacsTests(unittest.TestCase):
    def test_parse_charmm_gui_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            readme = Path(directory) / "README"
            readme.write_text(
                "set init = step5_input\n"
                "set rest_prefix = step5_input\n"
                "set mini_prefix = step6.0_minimization\n"
                "set equi_prefix = step6.%d_equilibration\n"
                "set cntmax = 6\n"
                "set prod_prefix = step7_production\n"
                "# Production\n",
                encoding="utf-8",
            )

            config = parse_charmm_gui_config(readme)

        self.assertEqual(config.equilibration_steps, 6)
        self.assertEqual(config.equilibration_name(3), "step6.3_equilibration")

    def test_find_project_directory_ignores_unrelated_readmes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "charmm-gui"
            (project / "gromacs").mkdir(parents=True)
            (project / "README").write_text("Simulation settings\n", encoding="utf-8")
            (root / "notes").mkdir()
            (root / "notes" / "README").write_text("Simulation notes\n", encoding="utf-8")

            self.assertEqual(_find_project_directory(root), project)

    def test_safe_extract_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "unsafe.tgz"
            with tarfile.open(archive, "w:gz") as tar:
                member = tarfile.TarInfo("../../outside.txt")
                member.size = 0
                tar.addfile(member)

            with self.assertRaises(ValueError):
                _safe_extract(archive, root / "extract")


if __name__ == "__main__":
    unittest.main()

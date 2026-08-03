from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.sh"
VERSION = "0.1.0"


class InstallerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.prefix = self.root / "prefix"
        self.codex_home = self.root / "codex"
        self.home.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_installer(
        self,
        *arguments: str,
        prefix: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        command = ["/bin/sh", str(INSTALLER)]
        if prefix:
            command.extend(["--prefix", str(self.prefix)])
        command.extend(arguments)
        return subprocess.run(
            command,
            cwd=self.root,
            env={
                **os.environ,
                "HOME": str(self.home),
                "CODEX_HOME": str(self.codex_home),
            },
            capture_output=True,
            text=True,
            check=False,
        )

    def test_installs_versioned_program_and_skill_links_with_cli_smoke(self) -> None:
        result = self.run_installer()

        self.assertEqual(result.returncode, 0, result.stderr)
        managed = self.prefix / "lib" / "pce"
        version = managed / "versions" / VERSION
        executable = self.prefix / "bin" / "pce"
        skill = self.codex_home / "skills" / "personal-compound"
        self.assertEqual(os.readlink(managed / "current"), f"versions/{VERSION}")
        self.assertEqual(
            os.readlink(executable),
            "../lib/pce/current/scripts/pce.py",
        )
        self.assertEqual(os.readlink(skill), str(managed / "current"))
        self.assertEqual(
            sorted(
                path.relative_to(version).as_posix()
                for path in version.rglob("*")
                if path.is_file()
            ),
            [
                "SKILL.md",
                "references/storage-model.md",
                "scripts/pce.py",
                "scripts/pce_ui.py",
            ],
        )
        smoke = subprocess.run(
            [str(executable), "--version"],
            cwd=self.root,
            env={
                **os.environ,
                "HOME": str(self.home),
                "CODEX_HOME": str(self.codex_home),
            },
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(smoke.returncode, 0, smoke.stderr)
        self.assertEqual(smoke.stdout.strip(), f"pce {VERSION}")
        for private_name in ("projects", "library", "inbox", ".git"):
            self.assertFalse((version / private_name).exists())

    def test_defaults_prefix_beneath_home(self) -> None:
        result = self.run_installer(prefix=False)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.home / ".local" / "bin" / "pce").is_symlink())

    def test_repeat_is_a_noop(self) -> None:
        first = self.run_installer()
        version = self.prefix / "lib" / "pce" / "versions" / VERSION
        first_stat = version.stat()

        second = self.run_installer()

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("already current", second.stdout)
        self.assertEqual(version.stat().st_ino, first_stat.st_ino)
        self.assertEqual(version.stat().st_mtime_ns, first_stat.st_mtime_ns)

    def test_refuses_occupied_executable_without_overwriting(self) -> None:
        occupied = self.prefix / "bin" / "pce"
        occupied.parent.mkdir(parents=True)
        occupied.write_text("unrelated executable\n", encoding="utf-8")

        result = self.run_installer()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to overwrite", result.stderr)
        self.assertEqual(occupied.read_text(encoding="utf-8"), "unrelated executable\n")
        self.assertFalse((self.prefix / "lib" / "pce").exists())

    def test_refuses_occupied_skill_without_overwriting(self) -> None:
        occupied = self.codex_home / "skills" / "personal-compound"
        occupied.parent.mkdir(parents=True)
        occupied.write_text("unrelated skill\n", encoding="utf-8")

        result = self.run_installer()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to overwrite", result.stderr)
        self.assertEqual(occupied.read_text(encoding="utf-8"), "unrelated skill\n")
        self.assertFalse((self.prefix / "lib" / "pce").exists())

    def test_preserves_configuration_store_logs_and_launch_agent(self) -> None:
        markers = {
            self.home
            / "Library/Application Support/Personal Compound/config.json": "config\n",
            self.root / "private-store/projects/private-plan.md": "knowledge\n",
            self.home / "Library/Logs/Personal Compound/autosync.log": "log\n",
            self.home
            / "Library/LaunchAgents/com.personal-compound.sync.plist": "plist\n",
        }
        for path, value in markers.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value, encoding="utf-8")

        result = self.run_installer()

        self.assertEqual(result.returncode, 0, result.stderr)
        for path, value in markers.items():
            self.assertEqual(path.read_text(encoding="utf-8"), value)

    def test_help_does_not_create_installation(self) -> None:
        result = self.run_installer("--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Usage:", result.stdout)
        self.assertFalse(self.prefix.exists())
        self.assertFalse(self.codex_home.exists())


if __name__ == "__main__":
    unittest.main()

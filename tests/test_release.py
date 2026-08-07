from __future__ import annotations

import json
import re
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "scripts" / "build_release.py"
VERSION = re.search(
    r'^PCE_VERSION = "([^"]+)"$',
    (ROOT / "scripts" / "pce.py").read_text(encoding="utf-8"),
    re.MULTILINE,
).group(1)
PAYLOAD = {
    "SKILL.md",
    "references/harvest-workflow.md",
    "references/storage-model.md",
    "scripts/pce.py",
    "scripts/pce_ui.py",
}


class ReleaseTest(unittest.TestCase):
    def run_builder(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(BUILDER), *arguments],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_matching_version_tag_is_valid(self) -> None:
        result = self.run_builder("validate-tag", "--tag", f"v{VERSION}")

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_mismatched_version_tag_is_rejected(self) -> None:
        result = self.run_builder("validate-tag", "--tag", "v9.9.9")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"v{VERSION}", result.stderr)

    def test_builds_verified_platform_independent_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            result = self.run_builder(
                "build",
                "--output",
                str(output),
                "--source-revision",
                "a" * 40,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            manifest_path = output / "release-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["schemaVersion"], 1)
            self.assertEqual(manifest["version"], VERSION)
            self.assertEqual(manifest["sourceRevision"], "a" * 40)
            self.assertEqual(manifest["artifact"]["filename"], f"pce-v{VERSION}.tar.gz")
            archive = output / manifest["artifact"]["filename"]
            with tarfile.open(archive, "r:gz") as package:
                self.assertEqual(
                    {member.name for member in package.getmembers() if member.isfile()},
                    PAYLOAD,
                )
                self.assertFalse(any(member.issym() or member.islnk() for member in package))
            verify = self.run_builder("verify", "--manifest", str(manifest_path))
            self.assertEqual(verify.returncode, 0, verify.stderr)
            self.assertTrue((output / "SHA256SUMS").is_file())

    def test_build_is_byte_for_byte_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_output = root / "first"
            second_output = root / "second"
            source_revision = "b" * 40

            first_result = self.run_builder(
                "build",
                "--output",
                str(first_output),
                "--source-revision",
                source_revision,
            )
            second_result = self.run_builder(
                "build",
                "--output",
                str(second_output),
                "--source-revision",
                source_revision,
            )

            self.assertEqual(first_result.returncode, 0, first_result.stderr)
            self.assertEqual(second_result.returncode, 0, second_result.stderr)
            for filename in (
                f"pce-v{VERSION}.tar.gz",
                "release-manifest.json",
                "SHA256SUMS",
            ):
                self.assertEqual(
                    (first_output / filename).read_bytes(),
                    (second_output / filename).read_bytes(),
                    f"{filename} differs between identical builds",
                )


if __name__ == "__main__":
    unittest.main()

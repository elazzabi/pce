from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import re
import shutil
import socketserver
import subprocess
import tarfile
import tempfile
import threading
import unittest
from functools import partial
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.sh"
VERSION = re.search(
    r'^PCE_VERSION = "([^"]+)"$',
    (ROOT / "scripts" / "pce.py").read_text(encoding="utf-8"),
    re.MULTILINE,
).group(1)


class QuietSimpleHTTPRequestHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *arguments: object) -> None:
        pass


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
        extra_env: dict[str, str] | None = None,
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
                **(extra_env or {}),
            },
            capture_output=True,
            text=True,
            check=False,
        )

    def build_release(self) -> Path:
        output = self.root / "release"
        result = subprocess.run(
            [
                "python3",
                str(ROOT / "scripts" / "build_release.py"),
                "build",
                "--output",
                str(output),
                "--source-revision",
                "a" * 40,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return output

    def serve_release(self, output: Path):
        web_root = self.root / "web"
        versioned = web_root / "releases" / "download" / f"v{VERSION}"
        latest = web_root / "releases" / "latest" / "download"
        shutil.copytree(output, versioned)
        shutil.copytree(output, latest)
        requests: list[tuple[str, str | None]] = []

        class Handler(QuietSimpleHTTPRequestHandler):
            def do_GET(inner_self) -> None:
                requests.append(
                    (inner_self.path, inner_self.headers.get("Authorization"))
                )
                super().do_GET()

        handler = partial(Handler, directory=web_root)
        server = socketserver.TCPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}/releases", requests

    def serve_private_release(self, output: Path):
        manifest = (output / "release-manifest.json").read_bytes()
        archive_path = next(output.glob("pce-v*.tar.gz"))
        archive = archive_path.read_bytes()
        requests: list[tuple[str, str | None, str | None]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(inner_self) -> None:
                requests.append(
                    (
                        inner_self.path,
                        inner_self.headers.get("Authorization"),
                        inner_self.headers.get("Accept"),
                    )
                )
                if inner_self.path in (
                    f"/api/releases/tags/v{VERSION}",
                    "/api/releases/latest",
                ):
                    host = inner_self.headers["Host"]
                    contents = (
                        "{\"assets\":["
                        f"{{\"name\":\"release-manifest.json\",\"url\":\"http://{host}/assets/manifest\"}},"
                        f"{{\"name\":\"{archive_path.name}\",\"url\":\"http://{host}/assets/archive\"}}"
                        "]}"
                    ).encode()
                    content_type = "application/json"
                elif inner_self.path == "/assets/manifest":
                    contents = manifest
                    content_type = "application/octet-stream"
                elif inner_self.path == "/assets/archive":
                    contents = archive
                    content_type = "application/octet-stream"
                else:
                    inner_self.send_error(404)
                    return
                inner_self.send_response(200)
                inner_self.send_header("Content-Type", content_type)
                inner_self.send_header("Content-Length", str(len(contents)))
                inner_self.end_headers()
                inner_self.wfile.write(contents)

            def log_message(self, format: str, *arguments: object) -> None:
                pass

        server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}/api", requests

    def rewrite_release_archive(self, output: Path, mutation: str) -> None:
        archive_path = next(output.glob("pce-v*.tar.gz"))
        with tarfile.open(archive_path, "r:gz") as package:
            entries = []
            for member in package.getmembers():
                source = package.extractfile(member)
                entries.append(
                    (copy.copy(member), source.read() if source is not None else b"")
                )

        if mutation == "extra":
            extra = tarfile.TarInfo("unexpected.md")
            extra.mode = 0o644
            extra.size = len(b"unexpected\n")
            entries.append((extra, b"unexpected\n"))
        elif mutation == "symlink":
            for index, (member, _) in enumerate(entries):
                if member.name == "scripts/pce_ui.py":
                    symlink = tarfile.TarInfo(member.name)
                    symlink.mode = 0o644
                    symlink.type = tarfile.SYMTYPE
                    symlink.linkname = "pce.py"
                    entries[index] = (symlink, b"")
                    break
            else:
                self.fail("release archive is missing scripts/pce_ui.py")
        else:
            self.fail(f"unknown archive mutation: {mutation}")

        replacement = archive_path.with_suffix(".replacement")
        with tarfile.open(replacement, "w:gz") as package:
            for member, contents in entries:
                package.addfile(member, io.BytesIO(contents) if member.isfile() else None)
        replacement.replace(archive_path)

        manifest_path = output / "release-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        artifact = manifest["artifact"]
        artifact["size"] = archive_path.stat().st_size
        artifact["sha256"] = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def install_recording_curl(self) -> tuple[Path, str]:
        real_curl = shutil.which("curl")
        self.assertIsNotNone(real_curl)
        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir()
        log_path = self.root / "curl-arguments.jsonl"
        wrapper = fake_bin / "curl"
        wrapper.write_text(
            """#!/usr/bin/env python3
import json
import os
import stat
import sys

header_files = [argument[1:] for argument in sys.argv[1:] if argument.startswith("@")]
record = {
    "argv": sys.argv[1:],
    "header_modes": [stat.S_IMODE(os.stat(path).st_mode) for path in header_files],
}
with open(os.environ["PCE_TEST_CURL_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps(record) + "\\n")
os.execv(%s, [%s, *sys.argv[1:]])
"""
            % (repr(real_curl), repr(real_curl)),
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        return log_path, f"{fake_bin}{os.pathsep}{os.environ['PATH']}"

    def install_fake_gh(self) -> str:
        fake_bin = self.root / "fake-gh-bin"
        fake_bin.mkdir()
        wrapper = fake_bin / "gh"
        wrapper.write_text(
            """#!/bin/sh
[ "$1" = auth ] && [ "$2" = token ] || exit 2
printf '%s\\n' private-test-token
""",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        return f"{fake_bin}{os.pathsep}{os.environ['PATH']}"

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
                "references/harvest-workflow.md",
                "references/storage-model.md",
                "scripts/pce.py",
                "scripts/pce_ui.py",
            ],
        )
        self.assertTrue((skill / "references" / "harvest-workflow.md").is_file())
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

    def test_local_install_ignores_unrelated_exported_pce_version(self) -> None:
        result = self.run_installer(extra_env={"PCE_VERSION": "9.9.9"})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.prefix / "bin" / "pce").is_symlink())

    def test_installs_exact_remote_release(self) -> None:
        release_url, _ = self.serve_release(self.build_release())

        result = self.run_installer(
            "--version",
            VERSION,
            extra_env={"PCE_RELEASE_BASE_URL": release_url},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"Installed PCE {VERSION}", result.stdout)
        self.assertEqual(
            subprocess.run(
                [str(self.prefix / "bin" / "pce"), "--version"],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip(),
            f"pce {VERSION}",
        )

    def test_installs_latest_remote_release(self) -> None:
        release_url, _ = self.serve_release(self.build_release())

        result = subprocess.run(
            ["/bin/sh"],
            cwd=self.root,
            input=INSTALLER.read_text(encoding="utf-8"),
            env={
                **os.environ,
                "HOME": str(self.home),
                "CODEX_HOME": str(self.codex_home),
                "PCE_RELEASE_BASE_URL": release_url,
            },
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.home / ".local" / "bin" / "pce").is_symlink())

    def test_authenticated_install_uses_private_release_api(self) -> None:
        api_url, requests = self.serve_private_release(self.build_release())
        curl_log, path = self.install_recording_curl()

        result = self.run_installer(
            "--version",
            VERSION,
            extra_env={
                "PCE_GITHUB_API_URL": api_url,
                "PCE_GITHUB_TOKEN": "private-test-token",
                "PCE_TEST_CURL_LOG": str(curl_log),
                "PATH": path,
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(requests), 3)
        self.assertTrue(
            all(
                authorization == "Bearer private-test-token"
                for _, authorization, _ in requests
            )
        )
        self.assertEqual(requests[0][2], "application/vnd.github+json")
        self.assertEqual(requests[1][2], "application/octet-stream")
        self.assertEqual(requests[2][2], "application/octet-stream")
        curl_records = [
            json.loads(line) for line in curl_log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(len(curl_records), 3)
        self.assertNotIn("private-test-token", json.dumps(curl_records))
        self.assertTrue(
            all(record["header_modes"] == [0o600] for record in curl_records)
        )

    def test_authenticated_latest_stdin_install_uses_private_release_api(self) -> None:
        api_url, requests = self.serve_private_release(self.build_release())
        path = self.install_fake_gh()

        result = subprocess.run(
            ["/bin/sh"],
            cwd=self.root,
            input=INSTALLER.read_text(encoding="utf-8"),
            env={
                **os.environ,
                "HOME": str(self.home),
                "CODEX_HOME": str(self.codex_home),
                "PCE_GITHUB_API_URL": api_url,
                "PCE_USE_GH_AUTH": "1",
                "PCE_GITHUB_TOKEN": "",
                "GH_TOKEN": "",
                "GITHUB_TOKEN": "",
                "PATH": path,
            },
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            [path for path, _, _ in requests],
            ["/api/releases/latest", "/assets/manifest", "/assets/archive"],
        )
        self.assertTrue(
            all(
                authorization == "Bearer private-test-token"
                for _, authorization, _ in requests
            )
        )
        self.assertEqual(requests[0][2], "application/vnd.github+json")
        self.assertEqual(requests[1][2], "application/octet-stream")
        self.assertEqual(requests[2][2], "application/octet-stream")

    def test_explicit_release_base_url_wins_over_ambient_tokens(self) -> None:
        release_url, requests = self.serve_release(self.build_release())

        result = self.run_installer(
            "--version",
            VERSION,
            extra_env={
                "PCE_RELEASE_BASE_URL": release_url,
                "PCE_GITHUB_API_URL": "http://127.0.0.1:1/should-not-be-used",
                "PCE_GITHUB_TOKEN": "ambient-pce-token",
                "GH_TOKEN": "ambient-gh-token",
                "GITHUB_TOKEN": "ambient-github-token",
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            [path for path, _ in requests],
            [
                f"/releases/download/v{VERSION}/release-manifest.json",
                f"/releases/download/v{VERSION}/pce-v{VERSION}.tar.gz",
            ],
        )
        self.assertTrue(all(authorization is None for _, authorization in requests))

    def test_refuses_remote_release_with_wrong_checksum(self) -> None:
        output = self.build_release()
        archive = next(output.glob("pce-v*.tar.gz"))
        archive.write_bytes(archive.read_bytes() + b"tampered")
        release_url, _ = self.serve_release(output)

        result = self.run_installer(
            "--version",
            VERSION,
            extra_env={"PCE_RELEASE_BASE_URL": release_url},
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertRegex(result.stderr, "size mismatch|SHA-256 mismatch")
        self.assertFalse((self.prefix / "lib" / "pce").exists())

    def assert_unsafe_archive_rejected(self, mutation: str) -> None:
        output = self.build_release()
        self.rewrite_release_archive(output, mutation)
        release_url, _ = self.serve_release(output)

        result = self.run_installer(
            "--version",
            VERSION,
            extra_env={"PCE_RELEASE_BASE_URL": release_url},
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe payload", result.stderr)
        self.assertFalse((self.prefix / "lib" / "pce").exists())

    def test_refuses_checksum_valid_archive_with_extra_member(self) -> None:
        self.assert_unsafe_archive_rejected("extra")

    def test_refuses_checksum_valid_archive_with_symlink(self) -> None:
        self.assert_unsafe_archive_rejected("symlink")

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

    def test_refuses_same_version_content_drift(self) -> None:
        first = self.run_installer()
        workflow = (
            self.prefix
            / "lib"
            / "pce"
            / "versions"
            / VERSION
            / "references"
            / "harvest-workflow.md"
        )

        self.assertEqual(first.returncode, 0, first.stderr)
        workflow.write_text("locally changed\n", encoding="utf-8")

        second = self.run_installer()

        self.assertNotEqual(second.returncode, 0)
        self.assertIn("differs", second.stderr)
        self.assertEqual(workflow.read_text(encoding="utf-8"), "locally changed\n")

    def test_repeat_tolerates_runtime_bytecode(self) -> None:
        first = self.run_installer()
        executable = self.prefix / "bin" / "pce"

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
        second = self.run_installer()

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(smoke.returncode, 0, smoke.stderr)
        self.assertTrue(
            (self.prefix / "lib" / "pce" / "current" / "scripts" / "__pycache__").is_dir()
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("already current", second.stdout)

    def test_upgrades_a_validated_managed_install(self) -> None:
        previous_version = "0.1.0"
        managed = self.prefix / "lib" / "pce"
        previous_root = managed / "versions" / previous_version
        for relative in (
            "SKILL.md",
            "references/storage-model.md",
            "scripts/pce.py",
            "scripts/pce_ui.py",
        ):
            destination = previous_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, destination)
        previous_program = previous_root / "scripts" / "pce.py"
        previous_program.write_text(
            previous_program.read_text(encoding="utf-8").replace(
                f'PCE_VERSION = "{VERSION}"',
                f'PCE_VERSION = "{previous_version}"',
                1,
            ),
            encoding="utf-8",
        )
        previous_program.chmod(0o755)

        (managed / "current").symlink_to(f"versions/{previous_version}")
        executable = self.prefix / "bin" / "pce"
        executable.parent.mkdir(parents=True)
        executable.symlink_to("../lib/pce/current/scripts/pce.py")
        skill = self.codex_home / "skills" / "personal-compound"
        skill.parent.mkdir(parents=True)
        skill.symlink_to(managed / "current")

        result = self.run_installer()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(os.readlink(managed / "current"), f"versions/{VERSION}")
        self.assertTrue((managed / "versions" / VERSION / "scripts" / "pce.py").is_file())
        self.assertTrue(
            (managed / "versions" / VERSION / "references" / "harvest-workflow.md").is_file()
        )
        self.assertFalse((previous_root / "references" / "harvest-workflow.md").exists())
        self.assertIn(f'PCE_VERSION = "{previous_version}"', previous_program.read_text())

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

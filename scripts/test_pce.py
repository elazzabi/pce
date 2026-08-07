from __future__ import annotations

import fcntl
import importlib.util
import io
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).with_name("pce.py")
SPEC = importlib.util.spec_from_file_location("pce", MODULE_PATH)
assert SPEC and SPEC.loader
pce = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = pce
SPEC.loader.exec_module(pce)


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def git_output(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class PersonalCompoundTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.previous_store = os.environ.get("PERSONAL_COMPOUND_HOME")
        self.previous_config_home = os.environ.get(
            "PERSONAL_COMPOUND_CONFIG_HOME"
        )
        os.environ["PERSONAL_COMPOUND_HOME"] = str(self.root / "store")
        os.environ["PERSONAL_COMPOUND_CONFIG_HOME"] = str(
            self.root / "config-home"
        )
        self.repo_a = self.make_repo("clone-a")
        self.repo_b = self.make_repo("clone-b")

    def tearDown(self) -> None:
        if self.previous_store is None:
            os.environ.pop("PERSONAL_COMPOUND_HOME", None)
        else:
            os.environ["PERSONAL_COMPOUND_HOME"] = self.previous_store
        if self.previous_config_home is None:
            os.environ.pop("PERSONAL_COMPOUND_CONFIG_HOME", None)
        else:
            os.environ["PERSONAL_COMPOUND_CONFIG_HOME"] = self.previous_config_home
        self.temp.cleanup()

    def make_repo(self, name: str) -> Path:
        repo = self.root / name
        repo.mkdir(parents=True)
        git(repo, "init", "-b", "main")
        git(repo, "config", "user.name", "Personal Compound Test")
        git(repo, "config", "user.email", "personal-compound@example.test")
        git(repo, "remote", "add", "origin", "git@github.com:Example/Project.git")
        return repo

    def write_artifact(self, repo: Path, rel: str, value: str) -> None:
        path = repo / pce.LOCAL_ROOT_NAME / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")

    def snapshot_paths(self, *paths: Path) -> dict[str, tuple[str, bytes | None]]:
        snapshot: dict[str, tuple[str, bytes | None]] = {}
        for root in paths:
            if not root.exists():
                snapshot[str(root)] = ("missing", None)
                continue
            for path in [root, *sorted(root.rglob("*"))]:
                kind = "directory" if path.is_dir() else "file"
                value = path.read_bytes() if path.is_file() else None
                snapshot[str(path)] = (kind, value)
        return snapshot

    def test_init_discovery_groups_clones_and_describes_all_read_only_work(self) -> None:
        plans = self.repo_a / "docs" / "plans"
        plans.mkdir(parents=True)
        (plans / "personal.md").write_text("private\n", encoding="utf-8")
        solutions = self.repo_a / ".claude" / "docs" / "solutions"
        solutions.mkdir(parents=True)
        (solutions / "learning.md").write_text("learning\n", encoding="utf-8")
        git(self.repo_a, "remote", "set-url", "origin", "https://user:secret@github.com/Example/Project.git")

        before = self.snapshot_paths(self.repo_a, self.repo_b, pce.store_root())
        candidate = pce.discover_init_candidate(
            [self.repo_a, self.repo_b],
            mode="quickstart",
            central_commit=True,
            service_enabled=False,
            service_interval=pce.SERVICE_INTERVAL,
        )

        self.assertEqual(self.snapshot_paths(self.repo_a, self.repo_b, pce.store_root()), before)
        self.assertFalse(pce.store_root().exists())
        self.assertEqual(len(candidate.origin_groups), 1)
        self.assertEqual(
            candidate.origin_groups[0].checkouts,
            (str(self.repo_a.resolve()), str(self.repo_b.resolve())),
        )
        self.assertNotIn("secret", repr(asdict(candidate)))
        first = candidate.checkouts[0]
        self.assertEqual(first.origin, "github.com/example/project")
        self.assertEqual(first.registration_action, "register")
        self.assertEqual(first.config_action, "update")
        self.assertEqual(first.exclude_action, "update")
        self.assertEqual(first.local_root_action, "create")
        self.assertEqual(
            [(item.path, item.action) for item in first.legacy],
            [
                ("docs/plans/personal.md", "import"),
                (".claude/docs/solutions/learning.md", "import"),
            ],
        )

    def test_init_discovery_recognizes_configured_clone_as_noop(self) -> None:
        pce.setup_repo(self.repo_a)
        before = self.snapshot_paths(self.repo_a, pce.store_root())

        candidate = pce.discover_init_candidate(
            [self.repo_a],
            mode="advanced",
            central_commit=False,
            service_enabled=False,
            service_interval=75,
        )

        self.assertEqual(self.snapshot_paths(self.repo_a, pce.store_root()), before)
        checkout = candidate.checkouts[0]
        self.assertTrue(checkout.registered)
        self.assertEqual(checkout.registration_action, "noop")
        self.assertEqual(checkout.config_action, "noop")
        self.assertEqual(checkout.exclude_action, "noop")
        self.assertEqual(checkout.local_root_action, "noop")

    def test_init_discovery_uses_the_same_exclude_and_import_plan_as_setup(self) -> None:
        exclude = self.repo_a / ".git" / "info" / "exclude"
        exclude.write_text(
            "\n".join(f"  {entry}  " for entry in pce.EXCLUDE_ENTRIES) + "\n",
            encoding="utf-8",
        )
        first = self.repo_a / "docs" / "plans" / "shared.md"
        second = self.repo_a / ".claude" / "docs" / "plans" / "shared.md"
        first.parent.mkdir(parents=True)
        second.parent.mkdir(parents=True)
        first.write_text("first\n", encoding="utf-8")
        second.write_text("second\n", encoding="utf-8")

        candidate = pce.discover_init_candidate(
            [self.repo_a],
            mode="quickstart",
            central_commit=False,
            service_enabled=False,
            service_interval=30,
        )

        checkout = candidate.checkouts[0]
        self.assertEqual(checkout.exclude_action, "noop")
        self.assertEqual(
            [(item.path, item.action) for item in checkout.legacy],
            [
                ("docs/plans/shared.md", "import"),
                (".claude/docs/plans/shared.md", "conflict"),
            ],
        )

        publication = pce.publish_init_candidate(candidate)
        diagnosis = pce.doctor(self.repo_a)
        self.assertTrue(publication["ok"])
        self.assertEqual(diagnosis["missing_excludes"], [])

    def test_init_discovery_plans_duplicate_clone_conflicts_in_review_order(self) -> None:
        for repo, value in ((self.repo_a, "first\n"), (self.repo_b, "second\n")):
            plan = repo / "docs" / "plans" / "same.md"
            plan.parent.mkdir(parents=True)
            plan.write_text(value, encoding="utf-8")
            (repo / "CONCEPTS.md").write_text(value, encoding="utf-8")

        candidate = pce.discover_init_candidate(
            [self.repo_a, self.repo_b],
            mode="quickstart",
            central_commit=False,
            service_enabled=False,
            service_interval=30,
        )

        self.assertEqual(
            [(item.path, item.action) for item in candidate.checkouts[0].legacy],
            [("docs/plans/same.md", "import"), ("CONCEPTS.md", "import")],
        )
        self.assertEqual(
            [(item.path, item.action) for item in candidate.checkouts[1].legacy],
            [("docs/plans/same.md", "conflict"), ("CONCEPTS.md", "conflict")],
        )

        self.assertNotIn("_source_fingerprint", repr(candidate.result()))
        (self.repo_a / "CONCEPTS.md").unlink()
        (self.repo_b / "CONCEPTS.md").unlink()
        artifact_candidate = pce.discover_init_candidate(
            [self.repo_a, self.repo_b],
            mode="quickstart",
            central_commit=False,
            service_enabled=False,
            service_interval=30,
        )

        result = pce.publish_init_candidate(artifact_candidate)

        self.assertTrue(result["ok"])
        namespace = Path(artifact_candidate.checkouts[0].namespace)
        conflicts = list((namespace / "import-conflicts").rglob("same.md"))
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].read_text(), "second\n")

    def test_init_publication_finally_hydrates_every_duplicate_clone(self) -> None:
        first = self.repo_a / "docs" / "plans" / "from-a.md"
        second = self.repo_b / ".claude" / "docs" / "solutions" / "from-b.md"
        first.parent.mkdir(parents=True)
        second.parent.mkdir(parents=True)
        first.write_text("from a\n", encoding="utf-8")
        second.write_text("from b\n", encoding="utf-8")
        candidate = pce.discover_init_candidate(
            [self.repo_a, self.repo_b],
            mode="quickstart",
            central_commit=False,
            service_enabled=False,
            service_interval=30,
        )

        result = pce.publish_init_candidate(candidate)

        self.assertTrue(result["ok"])
        for repo in (self.repo_a, self.repo_b):
            self.assertEqual(
                (repo / pce.LOCAL_ROOT_NAME / "plans" / "from-a.md").read_text(),
                "from a\n",
            )
            self.assertEqual(
                (repo / pce.LOCAL_ROOT_NAME / "solutions" / "from-b.md").read_text(),
                "from b\n",
            )

    def test_init_discovery_resolves_each_checkout_once(self) -> None:
        with patch.object(pce, "resolve_repo", wraps=pce.resolve_repo) as resolve:
            pce.discover_init_candidate(
                [self.repo_a, self.repo_b],
                mode="quickstart",
                central_commit=False,
                service_enabled=False,
                service_interval=30,
            )

        self.assertEqual(resolve.call_count, 2)

    def test_init_service_discovery_compares_complete_desired_state(self) -> None:
        service_root = self.root / "service"
        paths = {
            "plist": service_root / "LaunchAgents" / "service.plist",
            "status": service_root / "Support" / "status.json",
            "stdout": service_root / "Logs" / "stdout.log",
            "stderr": service_root / "Logs" / "stderr.log",
        }
        with (
            patch.object(pce.sys, "platform", "darwin"),
            patch.object(pce, "service_paths", return_value=paths),
            patch.object(pce, "service_is_loaded", return_value=True),
        ):
            desired = pce.desired_service_plist(45)
            paths["plist"].parent.mkdir(parents=True)
            paths["plist"].write_bytes(plistlib.dumps(desired, sort_keys=True))
            exact = pce.discover_service_candidate(enabled=True, interval=45)
            changed = pce.discover_service_candidate(enabled=True, interval=46)
            unchanged_when_not_selected = pce.discover_service_candidate(
                enabled=False,
                interval=45,
            )

        self.assertEqual(exact.action, "noop")
        self.assertTrue(exact.plist_matches)
        self.assertTrue(exact.label_matches)
        self.assertTrue(exact.executable_matches)
        self.assertTrue(exact.store_matches)
        self.assertTrue(exact.interval_matches)
        self.assertTrue(exact.arguments_match)
        self.assertEqual(changed.action, "update")
        self.assertFalse(changed.interval_matches)
        self.assertTrue(changed.arguments_match)
        self.assertEqual(unchanged_when_not_selected.action, "noop")
        self.assertTrue(unchanged_when_not_selected.installed)

    def test_init_discovery_rejects_missing_origin_without_writing(self) -> None:
        git(self.repo_a, "remote", "remove", "origin")
        before = self.snapshot_paths(self.repo_a, pce.store_root())

        with self.assertRaises(pce.PceError):
            pce.discover_init_candidate(
                [self.repo_a],
                mode="quickstart",
                central_commit=True,
                service_enabled=False,
                service_interval=30,
            )

        self.assertEqual(self.snapshot_paths(self.repo_a, pce.store_root()), before)

    def test_store_checkout_and_source_paths_must_not_overlap(self) -> None:
        nested_store = self.repo_a / "private-store"
        with self.assertRaisesRegex(pce.PceError, "separate from product checkout"):
            pce.discover_init_candidate(
                [self.repo_a],
                store=nested_store,
                mode="quickstart",
                central_commit=False,
                service_enabled=False,
                service_interval=30,
            )
        self.assertFalse(nested_store.exists())

        source_nested_store = pce.SCRIPT_DIR.parent / ".pce-test-store-overlap"
        self.assertFalse(source_nested_store.exists())
        with self.assertRaisesRegex(pce.PceError, "separate from PCE source"):
            pce.validate_store_path(source_nested_store)
        self.assertFalse(source_nested_store.exists())

        with pce.selected_store(nested_store):
            with self.assertRaisesRegex(
                pce.PceError,
                "separate from product checkout",
            ):
                pce.setup_repo(self.repo_a)
        self.assertFalse(nested_store.exists())

    def test_store_validation_requires_a_real_git_root_and_accepts_gitfiles(self) -> None:
        invalid = self.root / "invalid-git-store"
        invalid.mkdir()
        (invalid / ".git").write_text("not a gitfile\n", encoding="utf-8")
        sentinel = invalid / "vault.md"
        sentinel.write_text("preserve\n", encoding="utf-8")

        with self.assertRaisesRegex(pce.PceError, "non-empty knowledge store"):
            pce.validate_store_path(invalid)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve\n")

        primary = self.root / "primary-knowledge"
        primary.mkdir()
        git(primary, "init", "-b", "main")
        git(primary, "config", "user.name", "Personal Compound Test")
        git(primary, "config", "user.email", "personal-compound@example.test")
        (primary / "seed.md").write_text("seed\n", encoding="utf-8")
        git(primary, "add", "seed.md")
        git(primary, "commit", "-m", "knowledge: seed")
        worktree = self.root / "knowledge-worktree"
        git(primary, "worktree", "add", "-b", "knowledge-worktree", str(worktree))

        self.assertTrue((worktree / ".git").is_file())
        pce.validate_store_path(worktree)

    def test_store_root_reloads_persisted_config_but_environment_wins(self) -> None:
        configured = self.root / "configured store"
        overridden = self.root / "environment store"
        os.environ.pop("PERSONAL_COMPOUND_HOME")

        pce.persist_store_root(configured)

        self.assertEqual(pce.store_root(), configured.resolve())
        os.environ["PERSONAL_COMPOUND_HOME"] = str(overridden)
        self.assertEqual(pce.store_root(), overridden.resolve())
        self.assertEqual(
            json.loads(pce.user_config_path().read_text(encoding="utf-8")),
            {"store": str(configured.resolve())},
        )

    def test_init_adopts_existing_git_store_without_rewriting_unrelated_state(self) -> None:
        selected = self.root / "existing knowledge"
        selected.mkdir()
        git(selected, "init", "-b", "knowledge")
        git(selected, "config", "user.name", "Knowledge Owner")
        git(selected, "config", "user.email", "knowledge@example.test")
        git(selected, "remote", "add", "origin", "git@example.test:private/knowledge.git")
        sentinel = selected / "unrelated-obsidian.md"
        sentinel.write_text("keep exactly\n", encoding="utf-8")
        existing = selected / "projects" / "existing" / "metadata.json"
        existing.parent.mkdir(parents=True)
        existing.write_text('{"existing": true}\n', encoding="utf-8")
        git(selected, "add", ".")
        git(selected, "commit", "-m", "knowledge: existing history")
        before_head = git_output(selected, "rev-parse", "HEAD")
        before_remote = git_output(selected, "remote", "get-url", "origin")
        before_branch = git_output(selected, "branch", "--show-current")

        candidate = pce.discover_init_candidate(
            [self.repo_a],
            store=selected,
            mode="advanced",
            central_commit=False,
            service_enabled=False,
            service_interval=30,
        )
        result = pce.publish_init_candidate(candidate)

        self.assertTrue(result["ok"])
        self.assertEqual(candidate.store, str(selected.resolve()))
        self.assertEqual(
            [item.repository for item in candidate.checkouts],
            [str(self.repo_a.resolve())],
        )
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep exactly\n")
        self.assertEqual(existing.read_text(encoding="utf-8"), '{"existing": true}\n')
        self.assertEqual(git_output(selected, "rev-parse", "HEAD"), before_head)
        self.assertEqual(git_output(selected, "remote", "get-url", "origin"), before_remote)
        self.assertEqual(git_output(selected, "branch", "--show-current"), before_branch)
        self.assertEqual(
            json.loads(pce.user_config_path().read_text(encoding="utf-8"))["store"],
            str(selected.resolve()),
        )

    def test_init_creates_new_store_only_when_confirmed_publication_runs(self) -> None:
        selected = self.root / "new store"
        candidate = pce.discover_init_candidate(
            [self.repo_a],
            store=selected,
            mode="quickstart",
            central_commit=False,
            service_enabled=False,
            service_interval=30,
        )

        self.assertFalse(selected.exists())
        self.assertFalse(pce.user_config_path().exists())
        result = pce.publish_init_candidate(candidate)

        self.assertTrue(result["ok"])
        self.assertTrue((selected / ".git").is_dir())
        for directory in ("projects", "library", "inbox"):
            self.assertTrue((selected / directory).is_dir())
        self.assertEqual(
            json.loads(pce.user_config_path().read_text(encoding="utf-8"))["store"],
            str(selected.resolve()),
        )

    def test_init_decline_does_not_create_store_or_persist_config(self) -> None:
        selected = self.root / "declined store"
        candidate = pce.discover_init_candidate(
            [self.repo_a],
            store=selected,
            mode="quickstart",
            central_commit=False,
            service_enabled=False,
            service_interval=30,
        )
        bindings = ScriptedPromptBindings([False])

        confirmed = pce.review_init_candidate(
            candidate,
            prompter=pce.Prompter(bindings),
            presenter=pce.create_presenter(output=io.StringIO(), mode="plain"),
        )

        self.assertFalse(confirmed)
        self.assertFalse(selected.exists())
        self.assertFalse(pce.user_config_path().exists())

    def test_init_commit_targets_the_selected_store(self) -> None:
        selected = self.root / "committed store"
        selected.mkdir()
        git(selected, "init", "-b", "main")
        git(selected, "config", "user.name", "Knowledge Owner")
        git(selected, "config", "user.email", "knowledge@example.test")
        sentinel = selected / "sentinel.md"
        sentinel.write_text("unrelated\n", encoding="utf-8")
        git(selected, "add", "sentinel.md")
        git(selected, "commit", "-m", "knowledge: baseline")
        before = git_output(selected, "rev-parse", "HEAD")

        candidate = pce.discover_init_candidate(
            [self.repo_a],
            store=selected,
            mode="quickstart",
            central_commit=True,
            service_enabled=False,
            service_interval=30,
        )
        result = pce.publish_init_candidate(candidate)

        self.assertTrue(result["ok"])
        self.assertNotEqual(git_output(selected, "rev-parse", "HEAD"), before)
        self.assertIn("projects/", git_output(selected, "show", "--name-only", "--format="))
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "unrelated\n")

    def test_init_publication_converges_duplicate_clones_and_legacy_imports(self) -> None:
        legacy = self.repo_a / "docs" / "plans" / "private.md"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("private plan\n", encoding="utf-8")
        candidate = pce.discover_init_candidate(
            [self.repo_a, self.repo_b],
            mode="quickstart",
            central_commit=False,
            service_enabled=False,
            service_interval=30,
        )

        first = pce.publish_init_candidate(candidate)

        self.assertTrue(first["ok"])
        self.assertEqual(
            [(phase["phase"], phase["status"]) for phase in first["phases"]],
            [
                ("setup", "changed"),
                ("setup", "changed"),
                ("doctor", "noop"),
                ("doctor", "noop"),
                ("central_commit", "skipped"),
                ("service", "skipped"),
            ],
        )
        self.assertEqual(len(list((pce.store_root() / "projects").iterdir())), 1)
        imported = (
            pce.store_root()
            / "projects"
            / candidate.checkouts[0].key
            / "artifacts"
            / "plans"
            / "private.md"
        )
        self.assertEqual(
            imported.read_text(encoding="utf-8"),
            "private plan\n",
        )
        for repo in (self.repo_a, self.repo_b):
            self.assertEqual(
                git_output(repo, "status", "--short", "--untracked-files=all"),
                "",
            )

        repeated = pce.publish_init_candidate(
            pce.discover_init_candidate(
                [self.repo_a, self.repo_b],
                mode="quickstart",
                central_commit=False,
                service_enabled=False,
                service_interval=30,
            )
        )
        self.assertTrue(repeated["ok"])
        self.assertEqual(
            [phase["status"] for phase in repeated["phases"]],
            ["noop", "noop", "noop", "noop", "skipped", "skipped"],
        )

    def test_init_publication_aborts_candidate_drift_before_store_creation(self) -> None:
        candidate = pce.discover_init_candidate(
            [self.repo_a],
            mode="quickstart",
            central_commit=True,
            service_enabled=False,
            service_interval=30,
        )
        (self.repo_a / ".git" / "info" / "exclude").write_text(
            "\n".join(pce.EXCLUDE_ENTRIES) + "\n",
            encoding="utf-8",
        )

        result = pce.publish_init_candidate(candidate)

        self.assertFalse(result["ok"])
        self.assertEqual(result["phases"][0]["phase"], "validation")
        self.assertEqual(result["phases"][0]["status"], "failed")
        self.assertEqual(
            [phase["status"] for phase in result["phases"][1:]],
            ["unprocessed", "unprocessed", "skipped", "skipped"],
        )
        self.assertFalse(pce.store_root().exists())
        self.assertEqual(result["retry_commands"], ["pce init"])

    def test_init_publication_detects_same_action_legacy_source_drift(self) -> None:
        source = self.repo_a / "docs" / "plans" / "changing.md"
        source.parent.mkdir(parents=True)
        source.write_text("before\n", encoding="utf-8")
        candidate = pce.discover_init_candidate(
            [self.repo_a],
            mode="quickstart",
            central_commit=False,
            service_enabled=False,
            service_interval=30,
        )
        source.write_text("after\n", encoding="utf-8")

        result = pce.publish_init_candidate(candidate)

        self.assertFalse(result["ok"])
        self.assertEqual(result["phases"][0]["phase"], "validation")
        self.assertFalse(pce.store_root().exists())

    def test_init_publication_detects_between_checkout_drift_after_partial_progress(self) -> None:
        source_a = self.repo_a / "docs" / "plans" / "from-a.md"
        source_b = self.repo_b / "docs" / "plans" / "from-b.md"
        source_a.parent.mkdir(parents=True)
        source_b.parent.mkdir(parents=True)
        source_a.write_text("a\n", encoding="utf-8")
        source_b.write_text("b\n", encoding="utf-8")
        candidate = pce.discover_init_candidate(
            [self.repo_a, self.repo_b],
            mode="advanced",
            central_commit=True,
            service_enabled=False,
            service_interval=30,
        )
        real_setup = pce.setup_repo

        def mutate_second_after_first(repo: Path) -> dict[str, object]:
            result = real_setup(repo)
            if repo == self.repo_a.resolve():
                source_b.write_text("changed after lock\n", encoding="utf-8")
            return result

        with (
            patch.object(pce, "setup_repo", side_effect=mutate_second_after_first),
            patch.object(pce, "commit_store") as commit,
        ):
            result = pce.publish_init_candidate(candidate)

        self.assertFalse(result["ok"])
        self.assertEqual(result["phases"][0]["status"], "changed")
        self.assertEqual(result["phases"][1]["status"], "failed")
        self.assertIn("legacy source changed", result["phases"][1]["error"])
        self.assertTrue(pce.store_root().exists())
        self.assertFalse((self.repo_b / pce.LOCAL_ROOT_NAME).exists())
        commit.assert_not_called()

    def test_doctor_ignores_nested_paths_that_only_resemble_managed_artifacts(self) -> None:
        concepts = self.repo_a / "nested" / "CONCEPTS.md"
        concepts.parent.mkdir(parents=True)
        concepts.write_text("tracked\n", encoding="utf-8")
        git(self.repo_a, "add", "nested/CONCEPTS.md")
        git(self.repo_a, "commit", "-m", "test: add nested concepts")
        concepts.write_text("dirty\n", encoding="utf-8")
        candidate = pce.discover_init_candidate(
            [self.repo_a],
            mode="quickstart",
            central_commit=False,
            service_enabled=False,
            service_interval=30,
        )

        result = pce.publish_init_candidate(candidate)
        diagnosis = pce.doctor(self.repo_a)

        self.assertTrue(result["ok"])
        doctor_phase = next(
            phase for phase in result["phases"] if phase["phase"] == "doctor"
        )
        self.assertEqual(doctor_phase["status"], "noop")
        self.assertNotIn("warnings", doctor_phase)
        self.assertTrue(diagnosis["ok"])
        self.assertEqual(diagnosis["visible_personal_artifacts"], [])

    def test_doctor_ignores_a_modified_tracked_root_concepts_file(self) -> None:
        concepts = self.repo_a / "CONCEPTS.md"
        concepts.write_text("tracked\n", encoding="utf-8")
        git(self.repo_a, "add", "CONCEPTS.md")
        git(self.repo_a, "commit", "-m", "test: add team concepts")
        pce.setup_repo(self.repo_a)
        concepts.write_text("dirty but team-owned\n", encoding="utf-8")

        diagnosis = pce.doctor(self.repo_a)

        self.assertTrue(diagnosis["ok"])
        self.assertEqual(diagnosis["visible_personal_artifacts"], [])

    def test_init_publication_stops_after_checkout_or_doctor_failure(self) -> None:
        candidate = pce.discover_init_candidate(
            [self.repo_a, self.repo_b],
            mode="advanced",
            central_commit=True,
            service_enabled=False,
            service_interval=30,
        )
        enabled_service = replace(
            candidate.service,
            supported=True,
            enabled=True,
            action="install",
        )
        candidate = replace(candidate, service=enabled_service)
        real_setup = pce.setup_repo

        def fail_second(repo: Path) -> dict[str, object]:
            if repo == self.repo_b.resolve():
                raise pce.PceError("unsafe\nsetup failure")
            return real_setup(repo)

        with (
            patch.object(pce, "setup_repo", side_effect=fail_second),
            patch.object(
                pce,
                "discover_service_candidate",
                return_value=enabled_service,
            ),
            patch.object(pce, "commit_store") as commit,
            patch.object(pce, "service_install") as service,
        ):
            result = pce.publish_init_candidate(candidate)

        self.assertFalse(result["ok"])
        self.assertEqual(
            [(phase["phase"], phase["status"]) for phase in result["phases"]],
            [
                ("setup", "changed"),
                ("setup", "failed"),
                ("doctor", "unprocessed"),
                ("doctor", "unprocessed"),
                ("central_commit", "skipped"),
                ("service", "skipped"),
            ],
        )
        self.assertNotIn("\n", result["phases"][1]["error"])
        self.assertIn("--repo", result["phases"][1]["retry_command"])
        commit.assert_not_called()
        service.assert_not_called()

        healthy = pce.discover_init_candidate(
            [self.repo_a, self.repo_b],
            mode="advanced",
            central_commit=True,
            service_enabled=False,
            service_interval=30,
        )
        healthy_service = replace(
            healthy.service,
            supported=True,
            enabled=True,
            action="install",
        )
        healthy = replace(healthy, service=healthy_service)
        with (
            patch.object(
                pce,
                "discover_service_candidate",
                return_value=healthy_service,
            ),
            patch.object(
                pce,
                "doctor",
                side_effect=[
                    {
                        **pce.doctor(self.repo_a),
                        "docs_root_valid": False,
                        "ok": False,
                    }
                ],
            ),
            patch.object(pce, "commit_store") as doctor_commit,
            patch.object(pce, "service_install") as doctor_service,
        ):
            doctor_result = pce.publish_init_candidate(healthy)
        self.assertFalse(doctor_result["ok"])
        self.assertEqual(doctor_result["phases"][2]["status"], "failed")
        self.assertEqual(doctor_result["phases"][3]["status"], "unprocessed")
        doctor_commit.assert_not_called()
        doctor_service.assert_not_called()

        filesystem_candidate = pce.discover_init_candidate(
            [self.repo_a, self.repo_b],
            mode="advanced",
            central_commit=False,
            service_enabled=False,
            service_interval=30,
        )
        with patch.object(
            pce,
            "setup_repo",
            side_effect=OSError("read-only filesystem\nforged output"),
        ):
            filesystem_result = pce.publish_init_candidate(filesystem_candidate)
        self.assertFalse(filesystem_result["ok"])
        self.assertEqual(filesystem_result["phases"][0]["status"], "failed")
        self.assertNotIn("\n", filesystem_result["phases"][0]["error"])

    def test_init_publication_applies_commit_then_service_and_reports_failures(self) -> None:
        service_root = self.root / "ordered-service"
        paths = {
            "plist": service_root / "service.plist",
            "status": service_root / "status.json",
            "stdout": service_root / "stdout.log",
            "stderr": service_root / "stderr.log",
        }
        with (
            patch.object(pce.sys, "platform", "darwin"),
            patch.object(pce, "service_is_loaded", return_value=False),
            patch.object(pce, "service_paths", return_value=paths),
        ):
            candidate = pce.discover_init_candidate(
                [self.repo_a],
                mode="quickstart",
                central_commit=True,
                service_enabled=True,
                service_interval=45,
            )
            self.assertEqual(candidate.service.action, "install")
            calls: list[str] = []
            with (
                patch.object(
                    pce,
                    "commit_store",
                    side_effect=lambda *_args: calls.append("commit")
                    or {"committed": True, "commit": "abc123"},
                ),
                patch.object(
                    pce,
                    "service_install",
                    side_effect=lambda _interval: calls.append("service")
                    or {"installed": True, "loaded": True},
                ),
            ):
                result = pce.publish_init_candidate(candidate)

        self.assertTrue(result["ok"])
        self.assertEqual(calls, ["commit", "service"])
        self.assertEqual(result["phases"][-2]["status"], "changed")
        self.assertEqual(result["phases"][-1]["status"], "changed")

        retry_candidate = pce.discover_init_candidate(
            [self.repo_a],
            mode="quickstart",
            central_commit=True,
            service_enabled=False,
            service_interval=45,
        )
        retry_service = replace(
            retry_candidate.service,
            supported=True,
            enabled=True,
            action="install",
        )
        retry_candidate = replace(retry_candidate, service=retry_service)
        with (
            patch.object(
                pce,
                "discover_service_candidate",
                return_value=retry_service,
            ),
            patch.object(
                pce,
                "commit_store",
                side_effect=pce.PceError("signing failed"),
            ),
            patch.object(pce, "service_install") as retry_install,
        ):
            failed_commit = pce.publish_init_candidate(retry_candidate)
        self.assertFalse(failed_commit["ok"])
        self.assertEqual(failed_commit["phases"][-2]["status"], "failed")
        self.assertEqual(failed_commit["phases"][-1]["status"], "skipped")
        self.assertIn("pce init", failed_commit["retry_commands"])
        retry_install.assert_not_called()

    def test_init_publication_service_noop_disabled_and_failure_are_safe(self) -> None:
        with (
            patch.object(pce.sys, "platform", "darwin"),
            patch.object(pce, "service_is_loaded", return_value=True),
            patch.object(pce, "service_paths") as service_paths,
        ):
            service_root = self.root / "service"
            service_paths.return_value = {
                "plist": service_root / "service.plist",
                "status": service_root / "status.json",
                "stdout": service_root / "stdout.log",
                "stderr": service_root / "stderr.log",
            }
            desired = pce.desired_service_plist(45)
            service_paths.return_value["plist"].parent.mkdir(parents=True)
            service_paths.return_value["plist"].write_bytes(plistlib.dumps(desired))
            noop_candidate = pce.discover_init_candidate(
                [self.repo_a],
                mode="advanced",
                central_commit=False,
                service_enabled=True,
                service_interval=45,
            )
            with patch.object(pce, "service_install") as install:
                noop = pce.publish_init_candidate(noop_candidate)
            install.assert_not_called()
            self.assertEqual(noop["phases"][-1]["status"], "noop")

            disabled_candidate = pce.discover_init_candidate(
                [self.repo_a],
                mode="advanced",
                central_commit=False,
                service_enabled=False,
                service_interval=45,
            )
            with (
                patch.object(pce, "service_install") as install,
                patch.object(pce, "service_uninstall") as uninstall,
            ):
                disabled = pce.publish_init_candidate(disabled_candidate)
            install.assert_not_called()
            uninstall.assert_not_called()
            self.assertEqual(disabled["phases"][-1]["status"], "skipped")

            update_candidate = pce.discover_init_candidate(
                [self.repo_a],
                mode="advanced",
                central_commit=False,
                service_enabled=True,
                service_interval=46,
            )
            self.assertEqual(update_candidate.service.action, "update")
            with patch.object(
                pce, "service_install", side_effect=pce.PceError("launchctl failed")
            ):
                failure = pce.publish_init_candidate(update_candidate)
            self.assertFalse(failure["ok"])
            self.assertEqual(failure["phases"][-1]["status"], "failed")
            self.assertIn("pce service install --interval 46", failure["retry_commands"])

        with (
            patch.object(pce.sys, "platform", "darwin"),
            patch.object(pce, "service_paths", return_value=service_paths.return_value),
            patch.object(pce, "service_is_loaded", return_value=False),
        ):
            reload_candidate = pce.discover_init_candidate(
                [self.repo_a],
                mode="advanced",
                central_commit=False,
                service_enabled=True,
                service_interval=45,
            )
            self.assertEqual(reload_candidate.service.action, "reload")
            with patch.object(
                pce,
                "service_install",
                return_value={"installed": True, "loaded": True},
            ) as install:
                reloaded = pce.publish_init_candidate(reload_candidate)
            install.assert_called_once_with(45)
            self.assertTrue(reloaded["ok"])
            self.assertEqual(reloaded["phases"][-1]["action"], "reload")

    def test_init_service_rediscovery_failure_stays_in_service_phase(self) -> None:
        service_root = self.root / "rediscovery-service"
        paths = {
            "plist": service_root / "service.plist",
            "status": service_root / "status.json",
            "stdout": service_root / "stdout.log",
            "stderr": service_root / "stderr.log",
        }
        with (
            patch.object(pce.sys, "platform", "darwin"),
            patch.object(pce, "service_paths", return_value=paths),
            patch.object(pce, "service_is_loaded", return_value=False),
        ):
            candidate = pce.discover_init_candidate(
                [self.repo_a],
                mode="advanced",
                central_commit=False,
                service_enabled=True,
                service_interval=45,
            )
            with patch.object(
                pce,
                "discover_service_candidate",
                side_effect=[
                    candidate.service,
                    candidate.service,
                    pce.PceError("could not inspect launchd"),
                ],
            ):
                result = pce.publish_init_candidate(candidate)

        self.assertFalse(result["ok"])
        self.assertEqual(
            [(phase["phase"], phase["status"]) for phase in result["phases"]],
            [
                ("setup", "changed"),
                ("doctor", "noop"),
                ("central_commit", "skipped"),
                ("service", "failed"),
            ],
        )
        self.assertNotIn("lock", [phase["phase"] for phase in result["phases"]])
        self.assertEqual(
            result["retry_commands"],
            ["pce service install --interval 45"],
        )

    def test_origins_normalize_to_one_key(self) -> None:
        self.assertEqual(
            pce.normalize_origin("git@github.com:WooCommerce/WooCommerce.git"),
            pce.normalize_origin("https://github.com/woocommerce/woocommerce.git"),
        )
        self.assertNotEqual(
            pce.normalize_origin("https://git.example.test:8443/Owner/Repo.git"),
            pce.normalize_origin("https://git.example.test/Owner/Repo.git"),
        )

        git(
            self.repo_a,
            "remote",
            "set-url",
            "origin",
            "https://user:token@example.com/Owner/Repo.git",
        )
        pce.setup_repo(self.repo_a)
        ns, _ = pce.namespace(self.repo_a)
        metadata = (ns / "metadata.json").read_text(encoding="utf-8")
        self.assertNotIn("token", metadata)
        self.assertNotIn("user:", metadata)

        git(
            self.repo_a,
            "remote",
            "set-url",
            "origin",
            "https://git.example.test/owner/a__b.git",
        )
        git(
            self.repo_b,
            "remote",
            "set-url",
            "origin",
            "https://git.example.test/owner/a/b.git",
        )
        self.assertNotEqual(
            pce.namespace(self.repo_a)[1]["key"],
            pce.namespace(self.repo_b)[1]["key"],
        )

    def test_relative_local_origin_is_stable_across_working_directories(self) -> None:
        git(self.repo_a, "remote", "set-url", "origin", "../upstream.git")
        expected = "local/" + (
            (self.repo_a / "../upstream.git").resolve().as_posix().lstrip("/")
        )
        other_cwd = self.root / "other-cwd"
        other_cwd.mkdir()
        previous_cwd = Path.cwd()
        try:
            os.chdir(other_cwd)
            canonical, key = pce.origin_info(self.repo_a)
            pce.setup_repo(self.repo_a)
            os.chdir(pce.store_root())
            synced = pce.automatic_sync(commit=False)
        finally:
            os.chdir(previous_cwd)

        self.assertEqual(canonical, expected)
        self.assertEqual(key, pce.namespace(self.repo_a)[1]["key"])
        self.assertTrue(synced["ok"])

    def test_origin_errors_never_expose_embedded_credentials(self) -> None:
        for raw in (
            "https://user:top-secret@/missing-host.git",
            "user:top-secret@unsupported-origin",
            "https://user:top-secret@example.test:invalid/repo.git",
            "https://user:top-secret@example.test\uff0fevil.git",
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(pce.PceError) as raised:
                    pce.normalize_origin(raw)
                message = str(raised.exception)
                self.assertNotIn("top-secret", message)
                self.assertNotIn(raw, message)

    def test_tracking_inspection_failure_aborts_legacy_import_planning(self) -> None:
        failure = subprocess.CompletedProcess(
            ["git", "ls-files"],
            128,
            stdout="",
            stderr="index is unreadable",
        )

        def fail_when_checked(
            _args: list[str],
            *,
            cwd: Path | None = None,
            check: bool = True,
        ) -> subprocess.CompletedProcess[str]:
            del cwd
            if check:
                raise pce.PceError("git ls-files failed: index is unreadable")
            return failure

        with (
            patch.object(pce, "run", side_effect=fail_when_checked),
            self.assertRaisesRegex(pce.PceError, "index is unreadable"),
        ):
            pce._tracked_legacy_paths(self.repo_a)

    def test_setup_and_init_refuse_tracked_personal_paths(self) -> None:
        config = self.repo_a / ".compound-engineering" / "config.local.yaml"
        config.parent.mkdir(parents=True)
        config.write_text("docs_root: team-docs\n", encoding="utf-8")
        git(self.repo_a, "add", ".compound-engineering/config.local.yaml")

        with self.assertRaisesRegex(pce.PceError, "tracked personal"):
            pce.discover_init_candidate(
                [self.repo_a],
                mode="quickstart",
                central_commit=False,
                service_enabled=False,
                service_interval=30,
            )
        with self.assertRaisesRegex(pce.PceError, "tracked personal"):
            pce.setup_repo(self.repo_a)
        self.assertFalse(pce.store_root().exists())

        local = self.repo_b / pce.LOCAL_ROOT_NAME / "plans" / "team.md"
        local.parent.mkdir(parents=True)
        local.write_text("team plan\n", encoding="utf-8")
        git(self.repo_b, "add", f"{pce.LOCAL_ROOT_NAME}/plans/team.md")
        with self.assertRaisesRegex(pce.PceError, "tracked personal"):
            pce.discover_init_candidate(
                [self.repo_b],
                mode="quickstart",
                central_commit=False,
                service_enabled=False,
                service_interval=30,
            )

    def test_init_rechecks_tracked_personal_paths_under_lock(self) -> None:
        candidate = pce.discover_init_candidate(
            [self.repo_a],
            mode="quickstart",
            central_commit=False,
            service_enabled=False,
            service_interval=30,
        )
        rediscover = pce._rediscover_init_candidate
        calls = 0

        def track_before_locked_rediscovery(
            reviewed: pce.InitCandidate,
        ) -> pce.InitCandidate:
            nonlocal calls
            calls += 1
            if calls == 2:
                config = self.repo_a / ".compound-engineering" / "config.local.yaml"
                config.parent.mkdir(parents=True)
                config.write_text("docs_root: team-docs\n", encoding="utf-8")
                git(self.repo_a, "add", ".compound-engineering/config.local.yaml")
            return rediscover(reviewed)

        with (
            patch.object(
                pce,
                "_rediscover_init_candidate",
                side_effect=track_before_locked_rediscovery,
            ),
            patch.object(pce, "setup_repo") as setup,
        ):
            result = pce.publish_init_candidate(candidate)

        self.assertFalse(result["ok"])
        setup.assert_not_called()

    def test_setup_is_idempotent(self) -> None:
        config_path = self.repo_a / ".compound-engineering" / "config.local.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            "nested:\n  docs_root: team-docs\ndocs_root: old-docs\n",
            encoding="utf-8",
        )

        with patch.object(pce, "now", return_value="2026-01-01T00:00:00+00:00"):
            pce.setup_repo(self.repo_a)
        ns, _ = pce.namespace(self.repo_a)
        metadata_before = (ns / "metadata.json").read_text(encoding="utf-8")
        with patch.object(pce, "now", return_value="2026-02-01T00:00:00+00:00"):
            pce.setup_repo(self.repo_a)

        config = config_path.read_text(encoding="utf-8")
        self.assertEqual(config.count("docs_root: .ce-personal"), 1)
        self.assertIn("  docs_root: team-docs", config)
        self.assertEqual(
            (ns / "metadata.json").read_text(encoding="utf-8"),
            metadata_before,
        )

        exclude = pce.git_exclude_path(self.repo_a).read_text(encoding="utf-8")
        exclude_lines = exclude.splitlines()
        for entry in pce.EXCLUDE_ENTRIES:
            self.assertEqual(exclude_lines.count(entry), 1)

    def test_setup_imports_only_untracked_legacy_artifacts(self) -> None:
        plans = self.repo_a / "docs" / "plans"
        plans.mkdir(parents=True)
        (plans / "team.md").write_text("tracked\n", encoding="utf-8")
        (plans / "personal.md").write_text("untracked\n", encoding="utf-8")
        git(self.repo_a, "add", "docs/plans/team.md")

        pce.setup_repo(self.repo_a)

        ns, _ = pce.namespace(self.repo_a)
        imported = ns / "artifacts" / "plans"
        self.assertFalse((imported / "team.md").exists())
        self.assertEqual(
            (imported / "personal.md").read_text(encoding="utf-8"),
            "untracked\n",
        )

    def test_same_basename_import_conflicts_are_preserved_separately(self) -> None:
        seed = self.make_repo("seed")
        clone_one = self.make_repo("one/same")
        clone_two = self.make_repo("two/same")
        for repo, value in (
            (seed, "seed\n"),
            (clone_one, "one\n"),
            (clone_two, "two\n"),
        ):
            path = repo / "docs" / "plans" / "shared.md"
            path.parent.mkdir(parents=True)
            path.write_text(value, encoding="utf-8")

        pce.setup_repo(seed)
        pce.setup_repo(clone_one)
        pce.setup_repo(clone_two)

        ns, _ = pce.namespace(seed)
        preserved = sorted(
            path.read_text(encoding="utf-8")
            for path in (ns / "import-conflicts").glob("same-*/docs__plans/shared.md")
        )
        self.assertEqual(preserved, ["one\n", "two\n"])

    def test_duplicate_clones_share_and_merge_disjoint_changes(self) -> None:
        pce.setup_repo(self.repo_a)
        pce.setup_repo(self.repo_b)

        self.write_artifact(self.repo_a, "solutions/shared.md", "from a\n")
        pce.reconcile(self.repo_a, "sync")
        pce.reconcile(self.repo_b, "hydrate")
        self.assertEqual(
            (self.repo_b / pce.LOCAL_ROOT_NAME / "solutions/shared.md").read_text(),
            "from a\n",
        )

        self.write_artifact(self.repo_a, "solutions/shared.md", "from a v2\n")
        self.write_artifact(self.repo_b, "plans/from-b.md", "from b\n")
        pce.reconcile(self.repo_a, "sync")
        pce.reconcile(self.repo_b, "sync")

        ns, _ = pce.namespace(self.repo_a)
        self.assertEqual(
            (ns / "artifacts" / "solutions/shared.md").read_text(),
            "from a v2\n",
        )
        self.assertEqual(
            (ns / "artifacts" / "plans/from-b.md").read_text(),
            "from b\n",
        )

    def test_automatic_sync_merges_and_hydrates_duplicate_clones(self) -> None:
        pce.setup_repo(self.repo_a)
        pce.setup_repo(self.repo_b)
        self.write_artifact(self.repo_a, "solutions/from-a.md", "from a\n")
        self.write_artifact(self.repo_b, "plans/from-b.md", "from b\n")

        result = pce.automatic_sync(commit=False)

        self.assertTrue(result["ok"])
        for repo in (self.repo_a, self.repo_b):
            self.assertEqual(
                (repo / pce.LOCAL_ROOT_NAME / "solutions/from-a.md").read_text(),
                "from a\n",
            )
            self.assertEqual(
                (repo / pce.LOCAL_ROOT_NAME / "plans/from-b.md").read_text(),
                "from b\n",
            )

    def test_automatic_sync_refuses_deletion_and_preserves_central_copy(self) -> None:
        pce.setup_repo(self.repo_a)
        pce.setup_repo(self.repo_b)
        self.write_artifact(self.repo_a, "solutions/shared.md", "base\n")
        pce.reconcile(self.repo_a, "sync")
        pce.reconcile(self.repo_b, "hydrate")
        (self.repo_b / pce.LOCAL_ROOT_NAME / "solutions/shared.md").unlink()

        result = pce.automatic_sync(commit=False)

        self.assertFalse(result["ok"])
        self.assertIn("would delete artifacts", result["failures"][0]["error"])
        ns, _ = pce.namespace(self.repo_a)
        self.assertEqual(
            (ns / "artifacts" / "solutions/shared.md").read_text(),
            "base\n",
        )

    def test_automatic_sync_skips_a_missing_registered_checkout(self) -> None:
        pce.setup_repo(self.repo_a)
        ns, _ = pce.namespace(self.repo_a)
        metadata = pce.read_json(ns / "metadata.json", {})
        metadata["checkouts"].append(str(self.root / "removed-clone"))
        pce.write_json(ns / "metadata.json", metadata)

        result = pce.automatic_sync(commit=False)

        self.assertTrue(result["ok"])
        self.assertEqual(len(result["skipped"]), 1)
        self.assertIn("no longer exists", result["skipped"][0]["reason"])

    def test_automatic_sync_isolates_validation_os_errors_per_checkout(self) -> None:
        pce.setup_repo(self.repo_a)
        pce.setup_repo(self.repo_b)
        real_newest_artifact_mtime = pce.newest_artifact_mtime

        def fail_first_checkout(repo: Path, ns: Path) -> float | None:
            if repo == self.repo_a.resolve():
                raise OSError("validation read failed")
            return real_newest_artifact_mtime(repo, ns)

        with patch.object(
            pce,
            "newest_artifact_mtime",
            side_effect=fail_first_checkout,
        ):
            result = pce.automatic_sync(commit=False)

        self.assertFalse(result["ok"])
        self.assertEqual(result["failures"][0]["phase"], "validate")
        self.assertIn("validation read failed", result["failures"][0]["error"])
        self.assertEqual(len(result["sync"]), 1)
        self.assertEqual(len(result["hydrate"]), 1)
        self.assertEqual(result["sync"][0]["repository"], str(self.repo_b.resolve()))

    def test_automatic_sync_isolates_sync_os_errors_per_checkout(self) -> None:
        pce.setup_repo(self.repo_a)
        pce.setup_repo(self.repo_b)
        real_reconcile = pce.reconcile

        def fail_first_checkout(
            repo: Path,
            mode: str,
            *,
            allow_delete: bool = False,
        ) -> dict[str, object]:
            if repo == self.repo_a.resolve() and mode == "sync":
                raise OSError("sync write failed")
            return real_reconcile(repo, mode, allow_delete=allow_delete)

        with patch.object(pce, "reconcile", side_effect=fail_first_checkout):
            result = pce.automatic_sync(commit=False)

        self.assertFalse(result["ok"])
        self.assertEqual(result["failures"][0]["phase"], "sync")
        self.assertIn("sync write failed", result["failures"][0]["error"])
        self.assertEqual(len(result["sync"]), 1)
        self.assertEqual(len(result["hydrate"]), 1)
        self.assertEqual(result["sync"][0]["repository"], str(self.repo_b.resolve()))

    def test_automatic_sync_isolates_hydrate_os_errors_per_checkout(self) -> None:
        pce.setup_repo(self.repo_a)
        pce.setup_repo(self.repo_b)
        real_reconcile = pce.reconcile

        def fail_first_checkout(
            repo: Path,
            mode: str,
            *,
            allow_delete: bool = False,
        ) -> dict[str, object]:
            if repo == self.repo_a.resolve() and mode == "hydrate":
                raise OSError("hydrate write failed")
            return real_reconcile(repo, mode, allow_delete=allow_delete)

        with patch.object(pce, "reconcile", side_effect=fail_first_checkout):
            result = pce.automatic_sync(commit=False)

        self.assertFalse(result["ok"])
        self.assertEqual(result["failures"][0]["phase"], "hydrate")
        self.assertIn("hydrate write failed", result["failures"][0]["error"])
        self.assertEqual(len(result["sync"]), 2)
        self.assertEqual(len(result["hydrate"]), 1)
        self.assertEqual(
            result["hydrate"][0]["repository"],
            str(self.repo_b.resolve()),
        )

    def test_automatic_sync_retries_a_previously_failed_commit(self) -> None:
        pce.setup_repo(self.repo_a)
        ns, _ = pce.namespace(self.repo_a)
        git(pce.store_root(), "config", "user.name", "Personal Compound Test")
        git(
            pce.store_root(),
            "config",
            "user.email",
            "personal-compound@example.test",
        )
        pce.commit_store("knowledge: baseline", [ns])
        self.write_artifact(self.repo_a, "solutions/retry.md", "retry me\n")
        real_run = pce.run

        def fail_first_commit(
            args: list[str],
            *,
            cwd: Path | None = None,
            check: bool = True,
        ) -> subprocess.CompletedProcess[str]:
            if args[:3] == ["git", "-C", str(pce.store_root())] and "commit" in args:
                return subprocess.CompletedProcess(
                    args,
                    1,
                    stdout="",
                    stderr="temporary signing failure",
                )
            return real_run(args, cwd=cwd, check=check)

        with patch.object(pce, "run", side_effect=fail_first_commit):
            failed = pce.automatic_sync(commit=True)

        self.assertFalse(failed["ok"])
        self.assertEqual(failed["failures"][-1]["phase"], "commit")
        self.assertIn("temporary signing failure", failed["failures"][-1]["error"])
        self.assertTrue(git_output(pce.store_root(), "diff", "--cached"))

        retried = pce.automatic_sync(commit=True)

        self.assertTrue(retried["ok"])
        self.assertEqual(retried["sync"][0]["actions"], [])
        self.assertTrue(retried["central_commit"]["committed"])
        self.assertEqual(git_output(pce.store_root(), "diff", "--cached"), "")

    def test_unchanged_reconciliation_does_not_rewrite_local_state(self) -> None:
        pce.setup_repo(self.repo_a)

        with patch.object(pce, "write_json") as write_mock:
            result = pce.reconcile(self.repo_a, "hydrate")

        self.assertEqual(result["actions"], [])
        write_mock.assert_not_called()

    def test_artifact_names_do_not_collide_with_control_files(self) -> None:
        pce.setup_repo(self.repo_a)
        pce.setup_repo(self.repo_b)
        self.write_artifact(self.repo_a, "__CONCEPTS__.md", "artifact\n")
        self.write_artifact(
            self.repo_a,
            f"nested/{pce.STATE_NAME}",
            "nested artifact\n",
        )

        pce.reconcile(self.repo_a, "sync")
        pce.reconcile(self.repo_b, "hydrate")

        self.assertEqual(
            (self.repo_b / pce.LOCAL_ROOT_NAME / "__CONCEPTS__.md").read_text(
                encoding="utf-8"
            ),
            "artifact\n",
        )
        self.assertEqual(
            (self.repo_b / pce.LOCAL_ROOT_NAME / "nested" / pce.STATE_NAME).read_text(
                encoding="utf-8"
            ),
            "nested artifact\n",
        )

    def test_conflicting_changes_stop_without_overwrite(self) -> None:
        pce.setup_repo(self.repo_a)
        pce.setup_repo(self.repo_b)
        self.write_artifact(self.repo_a, "solutions/shared.md", "base\n")
        pce.reconcile(self.repo_a, "sync")
        pce.reconcile(self.repo_b, "hydrate")

        self.write_artifact(self.repo_a, "solutions/shared.md", "from a\n")
        self.write_artifact(self.repo_b, "solutions/shared.md", "from b\n")
        pce.reconcile(self.repo_a, "sync")
        with self.assertRaises(pce.PceError):
            pce.reconcile(self.repo_b, "sync")

        self.assertEqual(
            (self.repo_b / pce.LOCAL_ROOT_NAME / "solutions/shared.md").read_text(),
            "from b\n",
        )
        ns, _ = pce.namespace(self.repo_a)
        self.assertEqual(
            (ns / "artifacts" / "solutions/shared.md").read_text(),
            "from a\n",
        )

    def test_delete_modify_conflicts_abort_all_actions(self) -> None:
        pce.setup_repo(self.repo_a)
        pce.setup_repo(self.repo_b)
        self.write_artifact(self.repo_a, "solutions/shared.md", "base\n")
        pce.reconcile(self.repo_a, "sync")
        pce.reconcile(self.repo_b, "hydrate")

        (self.repo_a / pce.LOCAL_ROOT_NAME / "solutions/shared.md").unlink()
        self.write_artifact(self.repo_a, "plans/disjoint.md", "pending\n")
        self.write_artifact(self.repo_b, "solutions/shared.md", "central change\n")
        pce.reconcile(self.repo_b, "sync")

        with self.assertRaises(pce.PceError):
            pce.reconcile(self.repo_a, "sync", allow_delete=True)

        ns, _ = pce.namespace(self.repo_a)
        self.assertFalse((ns / "artifacts" / "plans/disjoint.md").exists())
        self.assertEqual(
            (ns / "artifacts" / "solutions/shared.md").read_text(encoding="utf-8"),
            "central change\n",
        )

    def test_deletions_require_authorization_and_are_recoverable(self) -> None:
        pce.setup_repo(self.repo_a)
        pce.setup_repo(self.repo_b)
        self.write_artifact(self.repo_a, "solutions/shared.md", "base\n")
        pce.reconcile(self.repo_a, "sync")
        pce.reconcile(self.repo_b, "hydrate")

        (self.repo_b / pce.LOCAL_ROOT_NAME / "solutions/shared.md").unlink()
        with self.assertRaises(pce.PceError):
            pce.reconcile(self.repo_b, "sync")

        ns, _ = pce.namespace(self.repo_b)
        central = ns / "artifacts" / "solutions/shared.md"
        self.assertTrue(central.exists())
        pce.restore_local(self.repo_b, "solutions/shared.md")
        self.assertEqual(
            (self.repo_b / pce.LOCAL_ROOT_NAME / "solutions/shared.md").read_text(
                encoding="utf-8"
            ),
            "base\n",
        )
        (self.repo_b / pce.LOCAL_ROOT_NAME / "solutions/shared.md").unlink()
        pce.reconcile(self.repo_b, "sync", allow_delete=True)
        self.assertFalse(central.exists())
        central_recoveries = list(
            (pce.store_root() / "recovery" / ns.name).glob(
                "*/central/solutions/shared.md"
            )
        )
        self.assertEqual(len(central_recoveries), 1)

        with self.assertRaises(pce.PceError):
            pce.reconcile(self.repo_a, "hydrate")
        local = self.repo_a / pce.LOCAL_ROOT_NAME / "solutions/shared.md"
        self.assertTrue(local.exists())
        pce.reconcile(self.repo_a, "hydrate", allow_delete=True)
        self.assertFalse(local.exists())
        local_recoveries = list(
            (pce.store_root() / "recovery" / ns.name).glob(
                "*/local/solutions/shared.md"
            )
        )
        self.assertEqual(len(local_recoveries), 1)

    def test_store_lock_rejects_an_invalid_store_without_creating_a_lock(self) -> None:
        invalid_store = self.root / "invalid-store"
        invalid_store.mkdir()
        (invalid_store / "unrelated.txt").write_text(
            "keep me unchanged\n",
            encoding="utf-8",
        )
        before = self.snapshot_paths(invalid_store)

        with (
            patch.dict(
                os.environ,
                {"PERSONAL_COMPOUND_HOME": str(invalid_store)},
            ),
            self.assertRaisesRegex(
                pce.PceError,
                "refusing to initialize Git in a non-empty knowledge store",
            ),
        ):
            with pce.store_lock():
                self.fail("invalid store lock unexpectedly acquired")

        self.assertEqual(self.snapshot_paths(invalid_store), before)
        self.assertFalse((invalid_store / ".pce.lock").exists())

    def test_cli_waits_for_store_lock(self) -> None:
        pce.ensure_store()
        lock_path = pce.store_root() / ".pce.lock"
        with lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(MODULE_PATH),
                    "repo-info",
                    "--repo",
                    str(self.repo_a),
                ],
                env=os.environ.copy(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            time.sleep(0.2)
            self.assertIsNone(process.poll())
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertIn("canonical_origin", stdout)

    def test_cli_lock_timeout_fails_loudly(self) -> None:
        pce.ensure_store()
        lock_path = pce.store_root() / ".pce.lock"
        with lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            env = os.environ.copy()
            env["PERSONAL_COMPOUND_LOCK_TIMEOUT"] = "0.1"
            result = subprocess.run(
                [
                    sys.executable,
                    str(MODULE_PATH),
                    "repo-info",
                    "--repo",
                    str(self.repo_a),
                ],
                env=env,
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("timed out waiting", result.stderr)

    def test_atomic_writes_preserve_existing_target_on_failure(self) -> None:
        text_target = self.root / "config.txt"
        text_target.write_text("original\n", encoding="utf-8")
        with (
            patch.object(pce.os, "replace", side_effect=OSError("replace failed")),
            self.assertRaises(OSError),
        ):
            pce.atomic_write_text(text_target, "replacement\n")
        self.assertEqual(text_target.read_text(encoding="utf-8"), "original\n")
        self.assertEqual(list(self.root.glob(".config.txt.*.tmp")), [])

        source = self.root / "source.txt"
        copy_target = self.root / "target.txt"
        source.write_text("replacement\n", encoding="utf-8")
        copy_target.write_text("original\n", encoding="utf-8")

        def partial_copy(_: Path, temporary: Path) -> None:
            temporary.write_text("partial\n", encoding="utf-8")
            raise OSError("copy failed")

        with (
            patch.object(pce.shutil, "copy2", side_effect=partial_copy),
            self.assertRaises(OSError),
        ):
            pce.atomic_copy(source, copy_target)
        self.assertEqual(copy_target.read_text(encoding="utf-8"), "original\n")
        self.assertEqual(list(self.root.glob(".target.txt.*.tmp")), [])

    def test_atomic_copy_retries_when_source_changes_during_copy(self) -> None:
        source = self.root / "changing-source.txt"
        target = self.root / "stable-target.txt"
        source.write_text("before\n", encoding="utf-8")
        target.write_text("original\n", encoding="utf-8")
        original_copy = pce.shutil.copy2

        def mutate_after_copy(copy_source: Path, temporary: Path) -> None:
            original_copy(copy_source, temporary)
            copy_source.write_text("after\n", encoding="utf-8")

        with (
            patch.object(pce.shutil, "copy2", side_effect=mutate_after_copy),
            self.assertRaisesRegex(pce.PceError, "source changed while copying"),
        ):
            pce.atomic_copy(source, target)

        self.assertEqual(target.read_text(encoding="utf-8"), "original\n")
        self.assertEqual(list(self.root.glob(".stable-target.txt.*.tmp")), [])

    def test_service_install_writes_a_safe_launchd_definition(self) -> None:
        pce.ensure_store()
        real_run = pce.run
        service_root = self.root / "service"
        paths = {
            "plist": service_root / "LaunchAgents" / "service.plist",
            "status": service_root / "Support" / "status.json",
            "stdout": service_root / "Logs" / "stdout.log",
            "stderr": service_root / "Logs" / "stderr.log",
        }

        def run_git_or_succeed(
            args: list[str],
            *,
            cwd: Path | None = None,
            check: bool = True,
        ) -> subprocess.CompletedProcess[str]:
            if args[0] == "git":
                return real_run(args, cwd=cwd, check=check)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with (
            patch.object(pce.sys, "platform", "darwin"),
            patch.object(pce, "service_paths", return_value=paths),
            patch.object(pce, "service_is_loaded", side_effect=[False, True]),
            patch.object(pce, "run", side_effect=run_git_or_succeed) as run_mock,
        ):
            result = pce.service_install(45)

        payload = plistlib.loads(paths["plist"].read_bytes())
        self.assertTrue(result["loaded"])
        self.assertEqual(payload["StartInterval"], 45)
        self.assertTrue(payload["RunAtLoad"])
        self.assertIn("sync-all", payload["ProgramArguments"])
        self.assertIn("--quiet", payload["ProgramArguments"])
        self.assertIn("--settle-seconds", payload["ProgramArguments"])
        self.assertNotIn("--allow-delete", payload["ProgramArguments"])
        self.assertEqual(
            [
                call.args[0]
                for call in run_mock.call_args_list
                if call.args[0][0] == "launchctl"
            ],
            [["launchctl", "bootstrap", pce.service_domain(), str(paths["plist"])]],
        )

    def test_service_install_preserves_old_plist_when_bootout_fails(self) -> None:
        pce.ensure_store()
        real_run = pce.run
        service_root = self.root / "bootout-service"
        paths = {
            "plist": service_root / "LaunchAgents" / "service.plist",
            "status": service_root / "Support" / "status.json",
            "stdout": service_root / "Logs" / "stdout.log",
            "stderr": service_root / "Logs" / "stderr.log",
        }
        paths["plist"].parent.mkdir(parents=True)
        old_payload = pce.desired_service_plist(45)
        old_payload["StartInterval"] = 30
        old_bytes = plistlib.dumps(old_payload, sort_keys=True)
        paths["plist"].write_bytes(old_bytes)

        def run_git_or_fail_bootout(
            args: list[str],
            *,
            cwd: Path | None = None,
            check: bool = True,
        ) -> subprocess.CompletedProcess[str]:
            if args[0] == "git":
                return real_run(args, cwd=cwd, check=check)
            raise pce.PceError("bootout failed")

        with (
            patch.object(pce.sys, "platform", "darwin"),
            patch.object(pce, "service_paths", return_value=paths),
            patch.object(pce, "service_is_loaded", return_value=True),
            patch.object(pce, "run", side_effect=run_git_or_fail_bootout) as run_mock,
            self.assertRaisesRegex(pce.PceError, "bootout failed"),
        ):
            pce.service_install(45)

        self.assertEqual(paths["plist"].read_bytes(), old_bytes)
        self.assertEqual(
            [
                call.args[0]
                for call in run_mock.call_args_list
                if call.args[0][0] == "launchctl"
            ],
            [["launchctl", "bootout", pce.service_domain(), str(paths["plist"])]],
        )
        with (
            patch.object(pce.sys, "platform", "darwin"),
            patch.object(pce, "service_paths", return_value=paths),
            patch.object(pce, "service_is_loaded", return_value=True),
        ):
            retry = pce.discover_service_candidate(enabled=True, interval=45)
        self.assertEqual(retry.action, "update")

        def run_git_or_succeed(
            args: list[str],
            *,
            cwd: Path | None = None,
            check: bool = True,
        ) -> subprocess.CompletedProcess[str]:
            if args[0] == "git":
                return real_run(args, cwd=cwd, check=check)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with (
            patch.object(pce.sys, "platform", "darwin"),
            patch.object(pce, "service_paths", return_value=paths),
            patch.object(pce, "service_is_loaded", side_effect=[True, True]),
            patch.object(pce, "run", side_effect=run_git_or_succeed) as retry_run,
        ):
            retried = pce.service_install(45)
        self.assertTrue(retried["loaded"])
        self.assertEqual(
            plistlib.loads(paths["plist"].read_bytes())["StartInterval"],
            45,
        )
        self.assertEqual(
            [
                call.args[0][1]
                for call in retry_run.call_args_list
                if call.args[0][0] == "launchctl"
            ],
            ["bootout", "bootstrap"],
        )

    def test_service_install_restores_loaded_service_after_write_failure(self) -> None:
        pce.ensure_store()
        real_run = pce.run
        service_root = self.root / "write-rollback-service"
        paths = {
            "plist": service_root / "LaunchAgents" / "service.plist",
            "status": service_root / "Support" / "status.json",
            "stdout": service_root / "Logs" / "stdout.log",
            "stderr": service_root / "Logs" / "stderr.log",
        }
        paths["plist"].parent.mkdir(parents=True)
        old_bytes = plistlib.dumps(pce.desired_service_plist(30), sort_keys=True)
        paths["plist"].write_bytes(old_bytes)

        def run_git_or_succeed(
            args: list[str],
            *,
            cwd: Path | None = None,
            check: bool = True,
        ) -> subprocess.CompletedProcess[str]:
            if args[0] == "git":
                return real_run(args, cwd=cwd, check=check)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with (
            patch.object(pce.sys, "platform", "darwin"),
            patch.object(pce, "service_paths", return_value=paths),
            patch.object(pce, "service_is_loaded", return_value=True),
            patch.object(pce, "atomic_write_text", side_effect=OSError("write failed")),
            patch.object(pce, "run", side_effect=run_git_or_succeed) as run_mock,
            self.assertRaisesRegex(pce.PceError, "write failed"),
        ):
            pce.service_install(45)

        self.assertEqual(paths["plist"].read_bytes(), old_bytes)
        self.assertEqual(
            [
                call.args[0][1]
                for call in run_mock.call_args_list
                if call.args[0][0] == "launchctl"
            ],
            ["bootout", "bootstrap"],
        )

    def test_service_install_restores_loaded_service_after_bootstrap_failure(self) -> None:
        pce.ensure_store()
        real_run = pce.run
        service_root = self.root / "bootstrap-rollback-service"
        paths = {
            "plist": service_root / "LaunchAgents" / "service.plist",
            "status": service_root / "Support" / "status.json",
            "stdout": service_root / "Logs" / "stdout.log",
            "stderr": service_root / "Logs" / "stderr.log",
        }
        paths["plist"].parent.mkdir(parents=True)
        old_bytes = plistlib.dumps(pce.desired_service_plist(30), sort_keys=True)
        paths["plist"].write_bytes(old_bytes)
        bootstrap_attempts = 0

        def fail_replacement_bootstrap(
            args: list[str],
            *,
            cwd: Path | None = None,
            check: bool = True,
        ) -> subprocess.CompletedProcess[str]:
            nonlocal bootstrap_attempts
            if args[0] == "git":
                return real_run(args, cwd=cwd, check=check)
            if args[1] == "bootstrap":
                bootstrap_attempts += 1
                if bootstrap_attempts == 1:
                    raise pce.PceError("replacement bootstrap failed")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with (
            patch.object(pce.sys, "platform", "darwin"),
            patch.object(pce, "service_paths", return_value=paths),
            patch.object(pce, "service_is_loaded", return_value=True),
            patch.object(pce, "run", side_effect=fail_replacement_bootstrap) as run_mock,
            self.assertRaisesRegex(pce.PceError, "replacement bootstrap failed"),
        ):
            pce.service_install(45)

        self.assertEqual(paths["plist"].read_bytes(), old_bytes)
        self.assertEqual(
            [
                call.args[0][1]
                for call in run_mock.call_args_list
                if call.args[0][0] == "launchctl"
            ],
            ["bootout", "bootstrap", "bootstrap"],
        )

    def test_service_install_reports_rollback_bootstrap_failure(self) -> None:
        pce.ensure_store()
        real_run = pce.run
        service_root = self.root / "failed-rollback-service"
        paths = {
            "plist": service_root / "LaunchAgents" / "service.plist",
            "status": service_root / "Support" / "status.json",
            "stdout": service_root / "Logs" / "stdout.log",
            "stderr": service_root / "Logs" / "stderr.log",
        }
        paths["plist"].parent.mkdir(parents=True)
        old_bytes = plistlib.dumps(pce.desired_service_plist(30), sort_keys=True)
        paths["plist"].write_bytes(old_bytes)
        bootstrap_attempts = 0

        def fail_both_bootstraps(
            args: list[str],
            *,
            cwd: Path | None = None,
            check: bool = True,
        ) -> subprocess.CompletedProcess[str]:
            nonlocal bootstrap_attempts
            if args[0] == "git":
                return real_run(args, cwd=cwd, check=check)
            if args[1] == "bootstrap":
                bootstrap_attempts += 1
                label = "replacement" if bootstrap_attempts == 1 else "rollback"
                raise pce.PceError(f"{label} bootstrap failed")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with (
            patch.object(pce.sys, "platform", "darwin"),
            patch.object(pce, "service_paths", return_value=paths),
            patch.object(pce, "service_is_loaded", return_value=True),
            patch.object(pce, "run", side_effect=fail_both_bootstraps),
            self.assertRaisesRegex(
                pce.PceError,
                "replacement bootstrap failed.*service rollback failed.*rollback bootstrap failed",
            ),
        ):
            pce.service_install(45)

        self.assertEqual(paths["plist"].read_bytes(), old_bytes)

    def test_central_commit_does_not_touch_product_git_state(self) -> None:
        tracked = self.repo_a / "tracked.txt"
        tracked.write_text("clean\n", encoding="utf-8")
        git(self.repo_a, "add", "tracked.txt")
        git(self.repo_a, "commit", "-m", "test: add tracked file")
        tracked.write_text("dirty\n", encoding="utf-8")

        pce.ensure_store()
        git(pce.store_root(), "config", "user.name", "Personal Compound Test")
        git(
            pce.store_root(),
            "config",
            "user.email",
            "personal-compound@example.test",
        )
        unrelated = pce.store_root() / "unrelated.txt"
        unrelated.write_text("leave me uncommitted\n", encoding="utf-8")
        before_head = git_output(self.repo_a, "rev-parse", "HEAD")
        before_cached = git_output(self.repo_a, "diff", "--cached")
        before_status = git_output(self.repo_a, "status", "--porcelain=v1")

        result = subprocess.run(
            [
                sys.executable,
                str(MODULE_PATH),
                "setup",
                "--repo",
                str(self.repo_a),
                "--commit",
            ],
            env=os.environ.copy(),
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(git_output(self.repo_a, "rev-parse", "HEAD"), before_head)
        self.assertEqual(git_output(self.repo_a, "diff", "--cached"), before_cached)
        self.assertEqual(
            git_output(self.repo_a, "status", "--porcelain=v1"),
            before_status,
        )
        self.assertTrue(unrelated.exists())
        self.assertIn(
            "?? unrelated.txt", git_output(pce.store_root(), "status", "--short")
        )
        self.assertNotIn(
            "unrelated.txt",
            git_output(
                pce.store_root(),
                "show",
                "--name-only",
                "--format=",
                "HEAD",
            ),
        )

    def test_sync_commit_ignores_missing_recovery_pathspec(self) -> None:
        pce.setup_repo(self.repo_a)
        ns, _ = pce.namespace(self.repo_a)
        git(pce.store_root(), "config", "user.name", "Personal Compound Test")
        git(
            pce.store_root(),
            "config",
            "user.email",
            "personal-compound@example.test",
        )
        pce.commit_store("knowledge: baseline", [ns])
        self.write_artifact(self.repo_a, "solutions/committed.md", "commit me\n")
        key = str(pce.namespace(self.repo_a)[1]["key"])
        recovery = pce.store_root() / "recovery" / key
        self.assertFalse(recovery.exists())

        result = subprocess.run(
            [
                sys.executable,
                str(MODULE_PATH),
                "sync",
                "--repo",
                str(self.repo_a),
                "--commit",
                "--json",
            ],
            env=os.environ.copy(),
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["central_commit"]["committed"])
        self.assertIn(
            "projects/",
            git_output(pce.store_root(), "show", "--name-only", "--format=", "HEAD"),
        )

    def test_commit_store_includes_a_tracked_deleted_path(self) -> None:
        pce.ensure_store()
        recovery = pce.store_root() / "recovery" / "tracked-deletion"
        recovery.mkdir(parents=True)
        artifact = recovery / "artifact.md"
        artifact.write_text("recover me\n", encoding="utf-8")
        git(pce.store_root(), "config", "user.name", "Personal Compound Test")
        git(
            pce.store_root(),
            "config",
            "user.email",
            "personal-compound@example.test",
        )
        pce.commit_store("knowledge: add recovery", [recovery])
        artifact.unlink()
        recovery.rmdir()

        result = pce.commit_store("knowledge: remove recovery", [recovery])

        self.assertTrue(result["committed"])
        self.assertNotIn(
            "recovery/tracked-deletion/artifact.md",
            git_output(pce.store_root(), "ls-tree", "-r", "--name-only", "HEAD"),
        )

    def test_harvest_paginates_oldest_changes_after_watermark(self) -> None:
        revisions: list[str] = []
        history = self.repo_a / "history.txt"
        for number in range(5):
            history.write_text(f"{number}\n", encoding="utf-8")
            git(self.repo_a, "add", "history.txt")
            git(self.repo_a, "commit", "-m", f"test: revision {number}")
            revisions.append(git_output(self.repo_a, "rev-parse", "HEAD"))

        ns, _ = pce.namespace(self.repo_a)
        ns.mkdir(parents=True)
        pce.write_json(
            ns / "state.json",
            {"last_harvested_revision": revisions[0]},
        )
        result = pce.harvest(self.repo_a, 2)

        self.assertEqual(
            [item["revision"] for item in result["commits"]],
            revisions[1:3],
        )
        self.assertTrue(result["truncated"])
        self.assertEqual(result["safe_mark_revision"], revisions[2])

        review = self.root / "review.md"
        review.write_text(
            "# Harvest review\n\nNo knowledge action.\n", encoding="utf-8"
        )
        marked = pce.harvest_mark(
            self.repo_a,
            revisions[2],
            review,
        )
        self.assertIsNotNone(marked["review"])
        self.assertTrue((ns / str(marked["review"])).exists())

        next_page = pce.harvest(self.repo_a, 2)
        self.assertEqual(
            [item["revision"] for item in next_page["commits"]],
            revisions[3:5],
        )
        self.assertFalse(next_page["truncated"])

    def prepare_harvest_transaction(
        self,
    ) -> tuple[list[str], Path, Path]:
        pce.setup_repo(self.repo_a)
        git(pce.store_root(), "config", "user.name", "Personal Compound Test")
        git(
            pce.store_root(),
            "config",
            "user.email",
            "personal-compound@example.test",
        )
        ns, _ = pce.namespace(self.repo_a)
        pce.commit_store("knowledge: baseline", [ns])

        revisions: list[str] = []
        history = self.repo_a / "harvest-history.txt"
        for number in range(3):
            history.write_text(f"{number}\n", encoding="utf-8")
            git(self.repo_a, "add", "harvest-history.txt")
            git(self.repo_a, "commit", "-m", f"test: harvest revision {number}")
            revisions.append(git_output(self.repo_a, "rev-parse", "HEAD"))

        review = self.root / "harvest-review.md"
        review.write_text("# Harvest review\n\nNo knowledge action.\n", encoding="utf-8")
        return revisions, review, ns

    def leave_failed_harvest_transaction(
        self,
    ) -> tuple[list[str], Path, Path]:
        revisions, review, ns = self.prepare_harvest_transaction()
        real_run = pce.run

        def fail_central_commit(
            args: list[str],
            *,
            cwd: Path | None = None,
            check: bool = True,
        ) -> subprocess.CompletedProcess[str]:
            if args[:4] == ["git", "-C", str(pce.store_root()), "commit"]:
                return subprocess.CompletedProcess(
                    args,
                    1,
                    stdout="",
                    stderr="simulated commit failure",
                )
            return real_run(args, cwd=cwd, check=check)

        with patch.object(pce, "run", side_effect=fail_central_commit):
            with self.assertRaisesRegex(pce.PceError, "central commit failed"):
                pce.harvest_mark_and_commit(self.repo_a, revisions[1], review)
        return revisions, review, ns

    def test_harvest_mark_and_commit_commits_a_clean_transaction(self) -> None:
        revisions, review, ns = self.prepare_harvest_transaction()

        completed = subprocess.run(
            [
                sys.executable,
                str(MODULE_PATH),
                "harvest-mark",
                "--repo",
                str(self.repo_a),
                "--revision",
                revisions[1],
                "--review-file",
                str(review),
                "--commit",
                "--json",
            ],
            env=os.environ.copy(),
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["revision"], revisions[1])
        self.assertTrue(result["central_commit"]["committed"])
        self.assertFalse(result["central_commit"]["retried"])
        self.assertEqual(len(list((ns / "harvest-reviews").iterdir())), 1)
        self.assertEqual(
            git_output(pce.store_root(), "diff", "--cached", "--name-only"),
            "",
        )

    def test_failed_harvest_commit_blocks_selection_and_retries_once(self) -> None:
        revisions, review, ns = self.leave_failed_harvest_transaction()
        staged_before = git_output(
            pce.store_root(), "diff", "--cached", "--name-only"
        ).splitlines()
        self.assertIn(
            ns.relative_to(pce.store_root()).joinpath("state.json").as_posix(),
            staged_before,
        )

        with self.assertRaisesRegex(
            pce.PceError, "unresolved staged harvest transaction"
        ):
            pce.harvest(self.repo_a, 2)

        review.unlink()
        result = pce.harvest_mark_and_commit(self.repo_a, revisions[1], review)

        self.assertEqual(result["revision"], revisions[1])
        self.assertTrue(result["central_commit"]["committed"])
        self.assertTrue(result["central_commit"]["retried"])
        self.assertEqual(len(list((ns / "harvest-reviews").iterdir())), 1)

    def test_harvest_retry_rejects_a_different_revision(self) -> None:
        revisions, review, _ns = self.leave_failed_harvest_transaction()

        with self.assertRaisesRegex(pce.PceError, "retry revision does not match"):
            pce.harvest_mark_and_commit(self.repo_a, revisions[2], review)

    def test_harvest_retry_rejects_unrelated_staged_namespace_changes(self) -> None:
        revisions, review, ns = self.leave_failed_harvest_transaction()
        unrelated = ns / "unrelated.md"
        unrelated.write_text("do not absorb me\n", encoding="utf-8")
        git(
            pce.store_root(),
            "add",
            unrelated.relative_to(pce.store_root()).as_posix(),
        )

        with self.assertRaisesRegex(pce.PceError, "unrelated staged namespace"):
            pce.harvest_mark_and_commit(self.repo_a, revisions[1], review)

    def test_harvest_retry_rejects_a_missing_stored_review(self) -> None:
        revisions, review, ns = self.leave_failed_harvest_transaction()
        stored_reviews = list((ns / "harvest-reviews").iterdir())
        self.assertEqual(len(stored_reviews), 1)
        stored_reviews[0].unlink()

        with self.assertRaisesRegex(pce.PceError, "stored harvest review is missing"):
            pce.harvest_mark_and_commit(self.repo_a, revisions[1], review)

    def test_harvest_retry_rejects_unstaged_transaction_edits(self) -> None:
        revisions, review, ns = self.leave_failed_harvest_transaction()
        state_path = ns / "state.json"
        state = pce.read_json(state_path, {})
        self.assertIsInstance(state, dict)
        state["unexpected"] = True
        pce.write_json(state_path, state)

        with self.assertRaisesRegex(pce.PceError, "changed after it was staged"):
            pce.harvest_mark_and_commit(self.repo_a, revisions[1], review)

    def test_harvest_rejects_staged_namespace_state_without_a_transaction(self) -> None:
        _revisions, _review, ns = self.prepare_harvest_transaction()
        unrelated = ns / "unrelated.md"
        unrelated.write_text("not a harvest transaction\n", encoding="utf-8")
        git(
            pce.store_root(),
            "add",
            unrelated.relative_to(pce.store_root()).as_posix(),
        )

        with self.assertRaisesRegex(
            pce.PceError,
            "staged namespace changes do not form a recoverable harvest transaction",
        ):
            pce.harvest(self.repo_a, 2)

    def test_inventory_lists_project_artifacts_and_checkouts(self) -> None:
        pce.setup_repo(self.repo_a)
        pce.setup_repo(self.repo_b)
        self.write_artifact(self.repo_a, "solutions/example.md", "example\n")
        pce.reconcile(self.repo_a, "sync")

        result = pce.inventory(self.repo_a)

        self.assertEqual(
            result["project"]["artifacts"],
            ["solutions/example.md"],
        )
        self.assertEqual(
            result["project"]["checkouts"],
            sorted([str(self.repo_a.resolve()), str(self.repo_b.resolve())]),
        )


class TtyStream(io.StringIO):
    def isatty(self) -> bool:
        return True

    @property
    def encoding(self) -> str:
        return "utf-8"


class ScriptedPromptBindings:
    def __init__(self, answers: list[object]) -> None:
        self.answers = list(answers)
        self.calls: list[str] = []

    def _next(self, kind: str) -> object:
        self.calls.append(kind)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def text(self, _message: str, _default: str | None) -> object:
        return self._next("text")

    def select(self, _message: str, _options: object, _initial: str | None) -> object:
        return self._next("select")

    def multiselect(self, _message: str, _options: object, _initial: list[str]) -> object:
        return self._next("multiselect")

    def confirm(self, _message: str, _initial: bool) -> object:
        return self._next("confirm")


class CliPresentationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = self.root / "store"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "remote", "add", "origin", "git@github.com:Example/Cli.git")
        self.environment = patch.dict(
            os.environ,
            {
                "PERSONAL_COMPOUND_HOME": str(self.store),
                "PERSONAL_COMPOUND_CONFIG_HOME": str(self.root / "config-home"),
            },
            clear=False,
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temp.cleanup()

    def subprocess_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(MODULE_PATH), *arguments],
            check=False,
            capture_output=True,
            text=True,
            env=os.environ.copy(),
        )

    def in_process_cli(
        self,
        arguments: list[str],
        *,
        environment: dict[str, str] | None = None,
    ) -> tuple[int, str, str]:
        stdout = TtyStream()
        stderr = TtyStream()
        stdin = TtyStream()
        command_environment = os.environ.copy()
        command_environment.pop("NO_COLOR", None)
        command_environment.update(environment or {})
        with (
            patch.object(pce.sys, "stdout", stdout),
            patch.object(pce.sys, "stderr", stderr),
            patch.object(pce.sys, "stdin", stdin),
            patch.dict(os.environ, command_environment, clear=True),
        ):
            return pce.main(arguments), stdout.getvalue(), stderr.getvalue()

    def make_prompter(self, answers: list[object]) -> tuple[object, ScriptedPromptBindings]:
        bindings = ScriptedPromptBindings(answers)
        return pce.Prompter(bindings), bindings

    def test_quickstart_and_advanced_build_the_same_complete_candidate_model(self) -> None:
        other = self.root / "clone-two"
        other.mkdir()
        git(other, "init", "-b", "main")
        git(other, "remote", "add", "origin", "https://github.com/example/cli.git")
        service_root = self.root / "service"
        paths = {
            "plist": service_root / "service.plist",
            "status": service_root / "status.json",
            "stdout": service_root / "stdout.log",
            "stderr": service_root / "stderr.log",
        }
        quick, quick_calls = self.make_prompter(
            ["quickstart", str(other), False]
        )
        advanced, advanced_calls = self.make_prompter(
            [
                "advanced",
                str(other),
                [str(self.repo.resolve()), str(other.resolve())],
                False,
                True,
                "75",
            ]
        )
        with (
            patch.object(pce, "resolve_repo", side_effect=lambda raw: self.repo.resolve() if raw is None else Path(raw).resolve()),
            patch.object(pce.sys, "platform", "darwin"),
            patch.object(pce, "service_paths", return_value=paths),
            patch.object(pce, "service_is_loaded", return_value=False),
        ):
            quick_candidate = pce.collect_init_candidate(quick)
            advanced_candidate = pce.collect_init_candidate(advanced)

        self.assertEqual(len(quick_candidate.origin_groups), 1)
        self.assertEqual(len(advanced_candidate.origin_groups), 1)
        self.assertEqual(
            [item.repository for item in quick_candidate.checkouts],
            [item.repository for item in advanced_candidate.checkouts],
        )
        self.assertTrue(quick_candidate.central_commit)
        self.assertFalse(advanced_candidate.central_commit)
        self.assertFalse(quick_candidate.service.enabled)
        self.assertTrue(advanced_candidate.service.enabled)
        self.assertEqual(advanced_candidate.service.interval_seconds, 75)
        self.assertEqual(quick_calls.calls, ["select", "text", "confirm"])
        self.assertEqual(
            advanced_calls.calls,
            ["select", "text", "multiselect", "confirm", "confirm", "text"],
        )

    def test_guided_store_selection_is_separate_from_checkout_selection(self) -> None:
        selected_store = self.root / "existing personal knowledge"
        selected_store.mkdir()
        git(selected_store, "init", "-b", "main")
        prompt, calls = self.make_prompter(
            [str(selected_store), "quickstart", "", False]
        )
        service_root = self.root / "service"
        paths = {
            "plist": service_root / "service.plist",
            "status": service_root / "status.json",
            "stdout": service_root / "stdout.log",
            "stderr": service_root / "stderr.log",
        }

        with (
            patch.dict(os.environ, os.environ.copy(), clear=True),
            patch.object(
                pce,
                "resolve_repo",
                side_effect=lambda raw: self.repo.resolve()
                if raw is None
                else Path(raw).resolve(),
            ),
            patch.object(pce.sys, "platform", "darwin"),
            patch.object(pce, "service_paths", return_value=paths),
            patch.object(pce, "service_is_loaded", return_value=False),
        ):
            os.environ.pop("PERSONAL_COMPOUND_HOME", None)
            candidate = pce.collect_init_candidate(prompt)

        self.assertEqual(candidate.store, str(selected_store.resolve()))
        self.assertEqual(
            [item.repository for item in candidate.checkouts],
            [str(self.repo.resolve())],
        )
        self.assertNotIn(str(selected_store.resolve()), candidate.registered_checkouts)
        self.assertEqual(calls.calls, ["text", "select", "text", "confirm"])

    def test_init_help_exposes_non_destructive_store_selection(self) -> None:
        result = self.subprocess_cli("init", "--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--store", result.stdout)
        self.assertIn("knowledge store", result.stdout.lower())

    def test_init_store_option_decline_leaves_store_and_config_absent(self) -> None:
        selected_store = self.root / "declined option store"
        prompter, _ = self.make_prompter(
            ["quickstart", "", False, False]
        )
        service_root = self.root / "service"
        paths = {
            "plist": service_root / "service.plist",
            "status": service_root / "status.json",
            "stdout": service_root / "stdout.log",
            "stderr": service_root / "stderr.log",
        }

        with (
            patch.dict(os.environ, os.environ.copy(), clear=True),
            patch.object(pce, "create_prompter", return_value=prompter),
            patch.object(
                pce,
                "resolve_repo",
                side_effect=lambda raw: self.repo.resolve()
                if raw is None
                else Path(raw).resolve(),
            ),
            patch.object(pce.sys, "platform", "darwin"),
            patch.object(pce, "service_paths", return_value=paths),
            patch.object(pce, "service_is_loaded", return_value=False),
        ):
            os.environ.pop("PERSONAL_COMPOUND_HOME", None)
            code, output, error = self.in_process_cli(
                ["init", "--store", str(selected_store), "--plain"]
            )

        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.assertIn("Initialization declined. No changes made.", output)
        self.assertFalse(selected_store.exists())
        self.assertFalse(pce.user_config_path().exists())

    def test_init_review_has_rich_plain_semantic_parity_and_decline_writes_nothing(self) -> None:
        candidate = pce.discover_init_candidate(
            [self.repo],
            mode="quickstart",
            central_commit=True,
            service_enabled=False,
            service_interval=30,
        )
        before_repo = git_output(self.repo, "status", "--short", "--untracked-files=all")
        self.assertFalse(self.store.exists())
        outputs: list[str] = []
        for mode in ("plain", "rich"):
            output = TtyStream()
            presenter = pce.create_presenter(output=output, mode=mode)
            prompter, _ = self.make_prompter([False])
            self.assertFalse(
                pce.review_init_candidate(
                    candidate,
                    prompter=prompter,
                    presenter=presenter,
                )
            )
            outputs.append(output.getvalue())

        for heading in (
            "Knowledge store",
            "Shared origin namespaces",
            "Checkouts and local files",
            "Legacy imports",
            "Central commit",
            "Automatic service",
        ):
            self.assertIn(heading, outputs[0])
            self.assertIn(heading, outputs[1])
        self.assertNotIn("\x1b", outputs[0])
        self.assertIn("\x1b", outputs[1])
        self.assertFalse(self.store.exists())
        self.assertEqual(
            git_output(self.repo, "status", "--short", "--untracked-files=all"),
            before_repo,
        )

    def test_init_eof_interrupt_and_cancel_exit_without_creating_store(self) -> None:
        for interruption in (EOFError(), KeyboardInterrupt(), pce.PromptCancelled()):
            with self.subTest(interruption=type(interruption).__name__):
                prompter, _ = self.make_prompter([interruption])
                with patch.object(pce, "create_prompter", return_value=prompter):
                    code, output, error = self.in_process_cli(["init", "--plain"])
                self.assertEqual(code, 0)
                self.assertIn("Initialization cancelled. No changes made.", output)
                self.assertEqual(error, "")
                self.assertFalse(self.store.exists())

    def test_noninteractive_init_refuses_without_creating_store(self) -> None:
        refusal = self.subprocess_cli("init", "--json")
        self.assertEqual(refusal.returncode, 2)
        payload = json.loads(refusal.stdout)
        self.assertEqual(
            set(payload),
            {"ok", "error", "message"},
        )
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "interactive_required")
        self.assertEqual(refusal.stderr, "")
        self.assertFalse(self.store.exists())

    def test_json_mode_returns_a_stable_error_envelope_for_pce_errors(self) -> None:
        for arguments in (
            ["--json", "restore", "--repo", str(self.repo), "--path", "missing.md"],
            ["restore", "--repo", str(self.repo), "--path", "missing.md", "--json"],
        ):
            with self.subTest(arguments=arguments):
                code, output, error = self.in_process_cli(arguments)
                payload = json.loads(output)

                self.assertEqual(code, 2)
                self.assertEqual(error, "")
                self.assertEqual(set(payload), {"ok", "error", "message"})
                self.assertFalse(payload["ok"])
                self.assertEqual(payload["error"], "pce_error")
                self.assertIn("central artifact does not exist", payload["message"])

    def test_sync_all_overwrites_stale_status_after_an_early_failure(self) -> None:
        invalid_store = self.root / "invalid-store"
        invalid_store.mkdir()
        (invalid_store / "unrelated.txt").write_text("not a store\n", encoding="utf-8")
        status_file = self.root / "service" / "status.json"
        pce.write_json(status_file, {"ok": True, "stale": True})

        code, output, error = self.in_process_cli(
            [
                "sync-all",
                "--quiet",
                "--status-file",
                str(status_file),
            ],
            environment={"PERSONAL_COMPOUND_HOME": str(invalid_store)},
        )
        payload = json.loads(status_file.read_text(encoding="utf-8"))

        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertIn("refusing to initialize Git", error)
        self.assertFalse(payload["ok"])
        self.assertNotIn("stale", payload)
        self.assertIn("started_at", payload)
        self.assertIn("finished_at", payload)
        self.assertEqual(payload["failures"][0]["phase"], "sync-all")
        self.assertIn("refusing to initialize Git", payload["failures"][0]["error"])
        self.assertFalse((invalid_store / ".pce.lock").exists())

    def test_confirmed_init_publishes_and_returns_nonzero_with_retry_guidance(self) -> None:
        candidate = pce.discover_init_candidate(
            [self.repo],
            mode="quickstart",
            central_commit=False,
            service_enabled=False,
            service_interval=30,
        )
        publication = {
            "ok": False,
            "checkouts": [str(self.repo)],
            "phases": [
                {
                    "phase": "setup\x1b[2J",
                    "repository": str(self.repo) + "\x1b[2J",
                    "status": "failed",
                    "error": "failed\nforged",
                    "retry_command": "pce setup --repo " + str(self.repo),
                },
                {
                    "phase": "central_commit",
                    "status": "skipped",
                    "reason": "repository\nsetup failed",
                },
                {
                    "phase": "doctor",
                    "repository": str(self.repo),
                    "status": "noop",
                    "warnings": ["nested/CONCEPTS.md"],
                },
                {
                    "phase": "service",
                    "status": "skipped",
                    "reason": "repository setup failed",
                },
            ],
            "retry_commands": ["pce setup --repo " + str(self.repo)],
        }
        with (
            patch.object(pce, "collect_init_candidate", return_value=candidate),
            patch.object(pce, "review_init_candidate", return_value=True),
            patch.object(pce, "publish_init_candidate", return_value=publication) as publish,
        ):
            code, output, error = self.in_process_cli(["init", "--plain"])

        self.assertEqual(code, 1)
        self.assertEqual(error, "")
        self.assertIn("[failed]", output)
        self.assertEqual(output.count("Retry\n"), 1)
        self.assertNotIn("\x1b[2J", output)
        self.assertNotIn("\nforged", output)
        self.assertNotIn("\nsetup failed", output)
        self.assertIn("warning: nested/CONCEPTS.md", output)
        publish.assert_called_once_with(candidate)

    def test_root_nested_help_no_command_and_version_are_complete(self) -> None:
        no_command = self.subprocess_cli()
        self.assertEqual(no_command.returncode, 0)
        self.assertIn("Personal Compound Engineering", no_command.stdout)
        self.assertIn("setup", no_command.stdout)
        self.assertEqual(no_command.stderr, "")

        for arguments, expected in (
            (("sync", "--help"), "Synchronize local and central artifacts"),
            (("service", "--help"), "launchd automatic synchronization"),
            (("service", "install", "--help"), "interval"),
        ):
            with self.subTest(arguments=arguments):
                result = self.subprocess_cli(*arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(expected, result.stdout)
                self.assertEqual(result.stderr, "")

        version = self.subprocess_cli("--version")
        self.assertEqual(version.returncode, 0)
        self.assertRegex(version.stdout, r"^pce \d+\.\d+\.\d+\n$")

    def test_pipe_defaults_to_one_legacy_json_document_for_both_flag_placements(self) -> None:
        baseline = self.subprocess_cli("setup", "--repo", str(self.repo))
        self.assertEqual(baseline.returncode, 0, baseline.stderr)
        expected = json.loads(baseline.stdout)
        self.assertEqual(sorted(expected), ["checkouts"])

        for arguments in (
            ("--json", "setup", "--repo", str(self.repo)),
            ("setup", "--repo", str(self.repo), "--json"),
        ):
            with self.subTest(arguments=arguments):
                result = self.subprocess_cli(*arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), expected)
                self.assertNotIn("\x1b", result.stdout)
                document, end = json.JSONDecoder().raw_decode(result.stdout)
                self.assertEqual(document, expected)
                self.assertEqual(result.stdout[end:].strip(), "")

    def test_tty_uses_human_output_with_plain_and_no_color_fallbacks(self) -> None:
        pce.setup_repo(self.repo)

        returncode, output, error = self.in_process_cli(
            ["status", "--repo", str(self.repo)]
        )
        self.assertEqual(returncode, 0)
        self.assertIn("Repository is synchronized", output)
        self.assertIn("\x1b[", output)
        self.assertEqual(error, "")
        with self.assertRaises(json.JSONDecodeError):
            json.loads(output)

        for arguments, environment in (
            (["status", "--repo", str(self.repo), "--plain"], {}),
            (["status", "--repo", str(self.repo)], {"NO_COLOR": "1"}),
        ):
            with self.subTest(arguments=arguments, environment=environment):
                _, fallback, _ = self.in_process_cli(
                    arguments,
                    environment=environment,
                )
                self.assertIn("Repository is synchronized", fallback)
                self.assertNotIn("\x1b", fallback)

    def test_large_inventory_partial_sync_all_and_unhealthy_doctor_are_human(self) -> None:
        pce.setup_repo(self.repo)
        ns, _ = pce.namespace(self.repo)
        for number in range(125):
            path = ns / "artifacts" / "solutions" / f"item-{number:03}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("knowledge\n", encoding="utf-8")

        code, inventory_output, _ = self.in_process_cli(
            ["inventory", "--repo", str(self.repo)]
        )
        self.assertEqual(code, 0)
        self.assertIn("solutions/item-000.md", inventory_output)
        self.assertIn("solutions/item-124.md", inventory_output)

        partial = {
            "ok": False,
            "registered_checkouts": 2,
            "available_checkouts": 2,
            "sync": [],
            "hydrate": [],
            "skipped": [],
            "failures": [
                {"phase": "sync", "repository": "/tmp/bad\x1b[2J", "error": "conflict\nforged"}
            ],
            "central_commit": None,
        }
        with patch.object(pce, "automatic_sync", return_value=partial):
            code, sync_output, _ = self.in_process_cli(["sync-all"])
        self.assertEqual(code, 1)
        self.assertIn("Synchronization completed with 1 failure", sync_output)
        self.assertNotIn("\x1b[2J", sync_output)
        self.assertNotIn("\nforged", sync_output)

        other = self.root / "unhealthy"
        other.mkdir()
        git(other, "init", "-b", "main")
        git(other, "remote", "add", "origin", "git@github.com:Example/Other.git")
        code, doctor_output, _ = self.in_process_cli(
            ["doctor", "--repo", str(other)]
        )
        self.assertEqual(code, 1)
        self.assertIn("PCE needs attention", doctor_output)

    def test_usage_and_pce_errors_keep_existing_exit_codes(self) -> None:
        usage = self.subprocess_cli("sync", "--unknown")
        self.assertEqual(usage.returncode, 2)
        self.assertIn("usage:", usage.stderr)

        error = self.subprocess_cli(
            "restore", "--repo", str(self.repo), "--path", "missing.md"
        )
        self.assertEqual(error.returncode, 2)
        self.assertEqual(error.stdout, "")
        self.assertIn("error: central artifact does not exist", error.stderr)


if __name__ == "__main__":
    unittest.main()

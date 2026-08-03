from __future__ import annotations

import importlib.util
import io
import os
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).with_name("pce_ui.py")
SPEC = importlib.util.spec_from_file_location("pce_ui", MODULE_PATH)
assert SPEC and SPEC.loader
pce_ui = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pce_ui)


class TestStream(io.StringIO):
    def __init__(self, *, tty: bool, encoding: str = "utf-8") -> None:
        super().__init__()
        self._tty = tty
        self._encoding = encoding

    def isatty(self) -> bool:
        return self._tty

    @property
    def encoding(self) -> str:
        return self._encoding


class RecordingBindings:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def intro(self, message: str) -> None:
        self.calls.append(("intro", message))

    def outro(self, message: str) -> None:
        self.calls.append(("outro", message))

    def section(self, title: str, lines: list[str]) -> None:
        self.calls.append(("section", title, lines))

    def info(self, message: str) -> None:
        self.calls.append(("info", message))

    def success(self, message: str) -> None:
        self.calls.append(("success", message))

    def warning(self, message: str) -> None:
        self.calls.append(("warning", message))

    def error(self, message: str) -> None:
        self.calls.append(("error", message))


class RecordingPromptBindings:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.answers: list[object] = []

    def text(self, message: str, default: str | None) -> object:
        self.calls.append(("text", message, default))
        return self.answers.pop(0)

    def select(
        self,
        message: str,
        options: list[pce_ui.PromptOption],
        initial: str | None,
    ) -> object:
        self.calls.append(("select", message, options, initial))
        return self.answers.pop(0)

    def multiselect(
        self,
        message: str,
        options: list[pce_ui.PromptOption],
        initial: list[str],
    ) -> object:
        self.calls.append(("multiselect", message, options, initial))
        return self.answers.pop(0)

    def confirm(self, message: str, initial: bool) -> object:
        self.calls.append(("confirm", message, initial))
        return self.answers.pop(0)


class PresenterTest(unittest.TestCase):
    def test_routes_every_semantic_status_through_injected_bindings(self) -> None:
        bindings = RecordingBindings()
        presenter = pce_ui.Presenter(bindings)

        presenter.intro("Personal Compound")
        presenter.outro("Ready")
        presenter.section("Checkouts", ["one", "two"])
        presenter.info("Already configured")
        presenter.success("Configured")
        presenter.warning("Review this")
        presenter.error("Setup failed")

        self.assertEqual(
            bindings.calls,
            [
                ("intro", "Personal Compound"),
                ("outro", "Ready"),
                ("section", "Checkouts", ["one", "two"]),
                ("info", "Already configured"),
                ("success", "Configured"),
                ("warning", "Review this"),
                ("error", "Setup failed"),
            ],
        )

    def test_outcome_support_distinguishes_changed_noop_warning_and_error(self) -> None:
        bindings = RecordingBindings()
        presenter = pce_ui.Presenter(bindings)

        presenter.outcome("Updated", status="changed")
        presenter.outcome("Already healthy", status="noop")
        presenter.outcome("Needs review", status="warning")
        presenter.outcome("Failed", status="error")

        self.assertEqual(
            bindings.calls,
            [
                ("success", "Updated"),
                ("info", "Already healthy"),
                ("warning", "Needs review"),
                ("error", "Failed"),
            ],
        )

    def test_hostile_control_characters_are_rendered_inertly(self) -> None:
        bindings = RecordingBindings()
        presenter = pce_ui.Presenter(bindings)

        presenter.section(
            "Repo\x1b[2J\rname",
            ["line\nforged", "token\x00hidden", "direction\u202eevil"],
        )

        rendered = repr(bindings.calls)
        self.assertNotIn("\\x1b", rendered)
        self.assertNotIn("\\x00", rendered)
        self.assertNotIn("\\u202e", rendered)
        self.assertEqual(
            bindings.calls,
            [
                (
                    "section",
                    "Repo [2J name",
                    ["line forged", "token hidden", "direction evil"],
                )
            ],
        )

    def test_section_lines_preserve_copyable_whitespace_and_neutralize_controls(self) -> None:
        bindings = RecordingBindings()
        presenter = pce_ui.Presenter(bindings)
        retry = pce_ui.render_shell_command(
            ["pce", "setup", "--repo", "/tmp/a  b"]
        )

        presenter.section(
            "Retry",
            [retry, "/tmp/literal  path\x1b[2J\nnext"],
        )

        self.assertEqual(
            bindings.calls,
            [
                (
                    "section",
                    "Retry",
                    [
                        "pce setup --repo '/tmp/a  b'",
                        "/tmp/literal  path [2J next",
                    ],
                )
            ],
        )
        rendered = repr(bindings.calls)
        self.assertNotIn("\\x1b", rendered)
        self.assertNotIn("\\nnext", rendered)

    def test_shell_sensitive_arguments_are_copy_safe_posix(self) -> None:
        argv = [
            "pce",
            "setup",
            "--repo",
            "/tmp/a  folder/it's-here; echo unsafe",
            "$(not-run)",
        ]

        rendered = pce_ui.render_shell_command(argv)

        self.assertEqual(shlex.split(rendered, posix=True), argv)
        self.assertNotIn("\n", rendered)


class OutputModeTest(unittest.TestCase):
    def test_rich_requires_a_tty_unicode_and_color_permission(self) -> None:
        output = TestStream(tty=True)
        self.assertEqual(
            pce_ui.select_output_mode(output=output, environment={}),
            "rich",
        )

        cases = [
            {"json_requested": True},
            {"plain_requested": True},
            {"environment": {"NO_COLOR": "1"}},
            {"output": TestStream(tty=False)},
            {"output": TestStream(tty=True, encoding="ascii")},
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                options = {"output": output, "environment": {}}
                options.update(overrides)
                expected = (
                    "json"
                    if overrides.get("json_requested")
                    or (
                        "output" in overrides
                        and not overrides["output"].isatty()
                    )
                    else "plain"
                )
                self.assertEqual(pce_ui.select_output_mode(**options), expected)

    def test_explicit_plain_overrides_non_tty_json_default(self) -> None:
        self.assertEqual(
            pce_ui.select_output_mode(
                output=TestStream(tty=False),
                plain_requested=True,
                environment={},
            ),
            "plain",
        )

    def test_plain_bindings_preserve_line_oriented_hierarchy_without_ansi(self) -> None:
        output = TestStream(tty=False)
        presenter = pce_ui.create_presenter(output=output, mode="plain")

        presenter.intro("Personal Compound")
        presenter.section("Checkouts", ["one", "two"])
        presenter.info("No changes")
        presenter.success("Configured")
        presenter.warning("Review")
        presenter.error("Failed")
        presenter.outro("Run pce doctor")

        self.assertEqual(
            output.getvalue(),
            "Personal Compound\n"
            "\n"
            "Checkouts\n"
            "  one\n"
            "  two\n"
            "[i] No changes\n"
            "[ok] Configured\n"
            "[!] Review\n"
            "[x] Failed\n"
            "\n"
            "Run pce doctor\n",
        )
        self.assertNotIn("\x1b", output.getvalue())

    def test_plain_and_rich_sections_keep_retry_commands_copyable(self) -> None:
        retry = pce_ui.render_shell_command(
            ["pce", "setup", "--repo", "/tmp/a  b\x1b[2J"]
        )
        self.assertEqual(retry, "pce setup --repo '/tmp/a  b [2J'")

        for mode in ("plain", "rich"):
            with self.subTest(mode=mode):
                output = TestStream(tty=True)
                presenter = pce_ui.create_presenter(output=output, mode=mode)
                presenter.section("Retry", [retry])

                self.assertIn("pce setup --repo '/tmp/a  b [2J'", output.getvalue())
                self.assertNotIn("\x1b[2J", output.getvalue())

    def test_ascii_fallback_replaces_unrepresentable_dynamic_text(self) -> None:
        output = TestStream(tty=True, encoding="ascii")
        mode = pce_ui.select_output_mode(output=output, environment={})
        presenter = pce_ui.create_presenter(output=output, mode=mode)

        presenter.section("Répositories", ["東京"])

        self.assertEqual(mode, "plain")
        self.assertTrue(output.getvalue().isascii())
        self.assertNotIn("\x1b", output.getvalue())

    def test_rich_bindings_use_status_glyphs_and_ansi(self) -> None:
        output = TestStream(tty=True)
        presenter = pce_ui.create_presenter(output=output, mode="rich")

        presenter.success("Configured")
        presenter.warning("Review")

        self.assertIn("\x1b[", output.getvalue())
        self.assertIn("✓", output.getvalue())
        self.assertIn("!", output.getvalue())

    def test_json_mode_has_no_human_presenter(self) -> None:
        with self.assertRaisesRegex(ValueError, "JSON output"):
            pce_ui.create_presenter(output=TestStream(tty=True), mode="json")


class CommandPresentationTest(unittest.TestCase):
    def test_malformed_result_collections_fail_loudly(self) -> None:
        with self.assertRaisesRegex(TypeError, "sequence"):
            pce_ui.present_command_result(
                "setup",
                {"checkouts": "not-a-collection"},
                pce_ui.Presenter(RecordingBindings()),
            )

    def test_every_command_has_an_outcome_first_human_presentation(self) -> None:
        samples: dict[str, dict[str, object]] = {
            "setup": {
                "checkouts": [
                    {
                        "repository": "/repo",
                        "origin": "github.com/example/repo",
                        "namespace": "/store/project",
                        "config": "/repo/config.local.yaml",
                        "exclude": "/repo/.git/info/exclude",
                        "import": {"conflict": []},
                    }
                ]
            },
            "hydrate": {"repository": "/repo", "actions": []},
            "sync": {
                "repository": "/repo",
                "actions": [
                    {
                        "path": "plans/one.md",
                        "source": "local",
                        "target": "central",
                        "deleted": False,
                    }
                ],
            },
            "status": {
                "repository": "/repo",
                "local_changes": [],
                "central_changes": [],
                "conflicts": [],
                "in_sync": True,
            },
            "doctor": {
                "ok": True,
                "namespace_exists": True,
                "docs_root_valid": True,
                "missing_excludes": [],
                "visible_personal_artifacts": [],
            },
            "repo-info": {
                "repository": "/repo",
                "canonical_origin": "github.com/example/repo",
                "key": "key",
                "namespace": "/store/project",
            },
            "inventory": {
                "project": {
                    "checkouts": ["/repo"],
                    "artifacts": ["solutions/one.md"],
                },
                "library": [],
                "inbox": [],
            },
            "restore": {
                "path": "plans/one.md",
                "restored_from": "/store/one.md",
                "restored_to": "/repo/one.md",
            },
            "harvest": {
                "commits": [
                    {
                        "revision": "1234567890abcdef",
                        "subject": "Improve output",
                        "author": "Person",
                    }
                ],
                "safe_mark_revision": "1234567890abcdef",
            },
            "harvest-mark": {
                "revision": "1234567890abcdef",
                "review": "harvest/review.md",
                "state_path": "/store/state.json",
            },
            "search": {
                "results": [
                    {
                        "path": "/store/solution.md",
                        "source": "project",
                        "score": 2,
                        "snippet": "Useful knowledge",
                    }
                ]
            },
            "sync-all": {
                "registered_checkouts": 1,
                "available_checkouts": 1,
                "sync": [],
                "hydrate": [],
                "skipped": [],
                "failures": [],
            },
            "service install": {
                "installed": True,
                "loaded": True,
                "interval_seconds": 30,
                "plist": "/service.plist",
            },
            "service status": {
                "installed": True,
                "loaded": True,
                "status": "/status.json",
            },
            "service uninstall": {
                "installed": False,
                "was_loaded": True,
                "plist": "/service.plist",
            },
        }

        for command, result in samples.items():
            with self.subTest(command=command):
                bindings = RecordingBindings()
                pce_ui.present_command_result(
                    command,
                    result,
                    pce_ui.Presenter(bindings),
                )
                self.assertTrue(bindings.calls)
                self.assertIn(
                    bindings.calls[0][0],
                    {"success", "info", "warning", "error"},
                )

    def test_dynamic_result_text_is_sanitized_in_every_section(self) -> None:
        bindings = RecordingBindings()
        pce_ui.present_command_result(
            "sync-all",
            {
                "registered_checkouts": 1,
                "available_checkouts": 1,
                "sync": [],
                "hydrate": [],
                "skipped": [],
                "failures": [
                    {
                        "repository": "/repo\x1b[2J",
                        "phase": "sync\nforged",
                        "error": "conflict\rhidden",
                    }
                ],
            },
            pce_ui.Presenter(bindings),
        )

        rendered = repr(bindings.calls)
        self.assertNotIn("\\x1b", rendered)
        self.assertNotIn("\\nforged", rendered)
        self.assertNotIn("\\rhidden", rendered)


class PrompterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.bindings = RecordingPromptBindings()
        self.prompter = pce_ui.Prompter(self.bindings)
        self.options = [
            pce_ui.PromptOption("quick", "QuickStart", "Recommended"),
            pce_ui.PromptOption("advanced", "Advanced"),
        ]

    def test_prompt_values_and_dynamic_text_are_sanitized(self) -> None:
        self.bindings.answers = ["answer", "quick", ["advanced"], True]

        self.assertEqual(self.prompter.text("Name\x1b[2J", "default\nvalue"), "answer")
        self.assertEqual(
            self.prompter.select("Mode\rnow", self.options, "quick"), "quick"
        )
        self.assertEqual(
            self.prompter.multiselect("Repos", self.options, ["advanced"]),
            ["advanced"],
        )
        self.assertTrue(self.prompter.confirm("Continue?", False))

        self.assertEqual(self.bindings.calls[0], ("text", "Name [2J", "default value"))
        select_options = self.bindings.calls[1][2]
        self.assertEqual(select_options[0].hint, "Recommended")

    def test_cancel_sentinel_cancels_every_prompt_type(self) -> None:
        methods = [
            lambda: self.prompter.text("Name"),
            lambda: self.prompter.select("Mode", self.options),
            lambda: self.prompter.multiselect("Modes", self.options),
            lambda: self.prompter.confirm("Continue?", False),
        ]
        for method in methods:
            with self.subTest(method=method):
                self.bindings.answers = [pce_ui.CANCELLED]
                with self.assertRaises(pce_ui.PromptCancelled):
                    method()

    def test_eof_and_keyboard_interrupt_cancel_every_prompt_type(self) -> None:
        method_names = ("text", "select", "multiselect", "confirm")
        calls = {
            "text": lambda: self.prompter.text("Name"),
            "select": lambda: self.prompter.select("Mode", self.options),
            "multiselect": lambda: self.prompter.multiselect("Modes", self.options),
            "confirm": lambda: self.prompter.confirm("Continue?", False),
        }
        for exception in (EOFError(), KeyboardInterrupt()):
            for method_name in method_names:
                with self.subTest(exception=type(exception), method=method_name):
                    with (
                        patch.object(
                            self.bindings,
                            method_name,
                            side_effect=exception,
                        ),
                        self.assertRaises(pce_ui.PromptCancelled),
                    ):
                        calls[method_name]()

    def test_invalid_injected_answers_fail_without_domain_side_effects(self) -> None:
        self.bindings.answers = ["missing"]
        with self.assertRaisesRegex(ValueError, "unknown prompt selection"):
            self.prompter.select("Mode", self.options)

    def test_line_prompt_cancel_word_cancels_every_prompt_type(self) -> None:
        calls = (
            lambda prompter: prompter.text("Name"),
            lambda prompter: prompter.select("Mode", self.options),
            lambda prompter: prompter.multiselect("Modes", self.options),
            lambda prompter: prompter.confirm("Continue?", False),
        )
        for call in calls:
            with self.subTest(call=call):
                prompter = pce_ui.create_prompter(
                    input_stream=io.StringIO("q\n"),
                    output=io.StringIO(),
                )
                with self.assertRaises(pce_ui.PromptCancelled):
                    call(prompter)

    def test_disabled_initial_selection_is_not_advertised_as_default(self) -> None:
        output = io.StringIO()
        prompter = pce_ui.create_prompter(
            input_stream=io.StringIO("2\n"),
            output=output,
        )
        selected = prompter.select(
            "Mode",
            [
                pce_ui.PromptOption("quick", "QuickStart", disabled=True),
                pce_ui.PromptOption("advanced", "Advanced"),
            ],
            "quick",
        )

        self.assertEqual(selected, "advanced")
        self.assertNotIn("[1]", output.getvalue())


class ImportSafetyTest(unittest.TestCase):
    def test_ui_module_import_does_not_create_the_store_or_other_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = root / "store"
            with patch.dict(os.environ, {"PERSONAL_COMPOUND_HOME": str(store)}):
                spec = importlib.util.spec_from_file_location(
                    "isolated_pce_ui",
                    MODULE_PATH,
                )
                assert spec and spec.loader
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

            self.assertEqual(list(root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()

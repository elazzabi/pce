"""Terminal presentation and guided-prompt primitives for PCE.

This module deliberately has no dependency on PCE's storage or service code.  It
turns already-collected values into terminal output and prompt answers; callers
retain ownership of every filesystem and process mutation.
"""

from __future__ import annotations

import os
import shlex
import sys
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Callable, Literal, Protocol, TextIO

OutputMode = Literal["json", "plain", "rich"]
OutcomeStatus = Literal["changed", "noop", "warning", "error"]


class PresenterBindings(Protocol):
    """Low-level rendering port used by :class:`Presenter`."""

    def intro(self, message: str) -> None: ...

    def outro(self, message: str) -> None: ...

    def section(self, title: str, lines: list[str]) -> None: ...

    def info(self, message: str) -> None: ...

    def success(self, message: str) -> None: ...

    def warning(self, message: str) -> None: ...

    def error(self, message: str) -> None: ...


def sanitize_terminal_text(value: object) -> str:
    """Return dynamic text that cannot move the cursor or forge output lines."""

    cleaned: list[str] = []
    for character in str(value):
        category = unicodedata.category(character)
        if category in {"Cc", "Cf", "Cs", "Zl", "Zp"}:
            cleaned.append(" ")
        else:
            cleaned.append(character)
    return " ".join("".join(cleaned).split())


def sanitize_terminal_literal(value: object) -> str:
    """Neutralize terminal controls while preserving printable whitespace."""

    return "".join(
        " "
        if unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
        else character
        for character in str(value)
    )


def render_shell_command(argv: Sequence[object]) -> str:
    """Render sanitized arguments as a copy-safe POSIX shell command."""

    return shlex.join([sanitize_terminal_literal(argument) for argument in argv])


def supports_unicode(output: TextIO) -> bool:
    """Whether the stream encoding can represent PCE's small rich glyph set."""

    encoding = getattr(output, "encoding", None)
    if not encoding:
        return False
    try:
        "✓─".encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return False
    return True


def _stream_safe_text(value: str, output: TextIO) -> str:
    """Fit text to the stream encoding without allowing a rendering crash."""

    encoding = getattr(output, "encoding", None)
    if not encoding:
        return value
    try:
        value.encode(encoding)
    except (LookupError, UnicodeEncodeError):
        try:
            return value.encode(encoding, errors="replace").decode(encoding)
        except LookupError:
            return value.encode("ascii", errors="replace").decode("ascii")
    return value


def select_output_mode(
    *,
    output: TextIO = sys.stdout,
    json_requested: bool = False,
    plain_requested: bool = False,
    environment: Mapping[str, str] | None = None,
) -> OutputMode:
    """Select JSON, rich human, or line-oriented plain human presentation."""

    if json_requested:
        return "json"
    environment = os.environ if environment is None else environment
    if plain_requested:
        return "plain"
    if not output.isatty():
        return "json"
    if (
        "NO_COLOR" in environment
        or not supports_unicode(output)
    ):
        return "plain"
    return "rich"


class Presenter:
    """Sanitizing semantic presenter independent of terminal styling."""

    def __init__(self, bindings: PresenterBindings) -> None:
        self._bindings = bindings

    def intro(self, message: object) -> None:
        self._bindings.intro(sanitize_terminal_text(message))

    def outro(self, message: object) -> None:
        self._bindings.outro(sanitize_terminal_text(message))

    def section(self, title: object, lines: Sequence[object]) -> None:
        self._bindings.section(
            sanitize_terminal_text(title),
            [sanitize_terminal_literal(line) for line in lines],
        )

    def info(self, message: object) -> None:
        self._bindings.info(sanitize_terminal_text(message))

    def success(self, message: object) -> None:
        self._bindings.success(sanitize_terminal_text(message))

    def warning(self, message: object) -> None:
        self._bindings.warning(sanitize_terminal_text(message))

    def error(self, message: object) -> None:
        self._bindings.error(sanitize_terminal_text(message))

    def outcome(self, message: object, *, status: OutcomeStatus) -> None:
        """Render an outcome before supporting detail with stable semantics."""

        renderers = {
            "changed": self.success,
            "noop": self.info,
            "warning": self.warning,
            "error": self.error,
        }
        try:
            renderer = renderers[status]
        except KeyError as exc:
            raise ValueError(f"unknown outcome status: {status}") from exc
        renderer(message)


class TerminalPresenterBindings:
    """Standard-library rich or plain terminal bindings."""

    _RESET = "\x1b[0m"
    _BOLD = "\x1b[1m"
    _COLORS = {
        "info": "\x1b[36m",
        "success": "\x1b[32m",
        "warning": "\x1b[33m",
        "error": "\x1b[31m",
    }

    def __init__(self, output: TextIO, *, rich: bool) -> None:
        self._output = output
        self._rich = rich

    def intro(self, message: str) -> None:
        if self._rich:
            self._write(f"{self._BOLD}╭─ {message}{self._RESET}\n\n")
        else:
            self._write(f"{message}\n\n")

    def outro(self, message: str) -> None:
        if self._rich:
            self._write(f"\n{self._BOLD}╰─ {message}{self._RESET}\n")
        else:
            self._write(f"\n{message}\n")

    def section(self, title: str, lines: list[str]) -> None:
        if self._rich:
            body = "".join(f"  ├─ {line}\n" for line in lines)
            self._write(f"{self._BOLD}{title}{self._RESET}\n{body}")
        else:
            body = "".join(f"  {line}\n" for line in lines)
            self._write(f"{title}\n{body}")

    def info(self, message: str) -> None:
        self._status("info", "i", message)

    def success(self, message: str) -> None:
        self._status("success", "✓" if self._rich else "ok", message)

    def warning(self, message: str) -> None:
        self._status("warning", "!", message)

    def error(self, message: str) -> None:
        self._status("error", "×" if self._rich else "x", message)

    def _status(self, status: str, marker: str, message: str) -> None:
        if self._rich:
            self._write(f"{self._COLORS[status]}{marker}{self._RESET} {message}\n")
        else:
            self._write(f"[{marker}] {message}\n")

    def _write(self, value: str) -> None:
        self._output.write(_stream_safe_text(value, self._output))


def create_presenter(*, output: TextIO = sys.stdout, mode: OutputMode) -> Presenter:
    """Create a terminal presenter, rejecting machine-output mode explicitly."""

    if mode == "json":
        raise ValueError("JSON output must not create a human presenter")
    if mode not in {"plain", "rich"}:
        raise ValueError(f"unknown output mode: {mode}")
    return Presenter(TerminalPresenterBindings(output, rich=mode == "rich"))


def present_command_result(
    command: str,
    value: Mapping[str, object],
    presenter: Presenter,
) -> None:
    """Present one unchanged domain result using command-specific semantics."""

    handlers: dict[str, Callable[[Mapping[str, object], Presenter], None]] = {
        "init review": _present_init_review,
        "init publication": _present_init_publication,
        "setup": _present_setup,
        "hydrate": lambda result, output: _present_reconcile(
            "hydrate", result, output
        ),
        "sync": lambda result, output: _present_reconcile("sync", result, output),
        "status": _present_status,
        "doctor": _present_doctor,
        "repo-info": _present_repo_info,
        "inventory": _present_inventory,
        "restore": _present_restore,
        "harvest": _present_harvest,
        "harvest-mark": _present_harvest_mark,
        "search": _present_search,
        "sync-all": _present_sync_all,
        "service install": lambda result, output: _present_service(
            "install", result, output
        ),
        "service status": lambda result, output: _present_service(
            "status", result, output
        ),
        "service uninstall": lambda result, output: _present_service(
            "uninstall", result, output
        ),
    }
    try:
        handler = handlers[command]
    except KeyError as exc:
        raise ValueError(f"unknown command presentation: {command}") from exc
    handler(value, presenter)


def _mapping(value: object) -> Mapping[str, object]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError("result item must be a mapping")
    return value


def _items(value: object) -> Sequence[object]:
    if value is None:
        return ()
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return value
    raise TypeError("result collection must be a sequence")


def _text(value: object, fallback: str = "unknown") -> str:
    return fallback if value is None else str(value)


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return singular if count == 1 else (plural or f"{singular}s")


def _section(presenter: Presenter, title: str, lines: Sequence[object]) -> None:
    if lines:
        presenter.section(title, lines)


def _commit_line(value: object) -> list[str]:
    commit = _mapping(value)
    if not commit:
        return []
    if commit.get("committed"):
        return [f"Central commit: {_text(commit.get('commit'))}"]
    return [f"Central commit: {_text(commit.get('reason'), 'not created')}"]


def _present_init_review(
    value: Mapping[str, object], presenter: Presenter
) -> None:
    checkouts = [_mapping(item) for item in _items(value.get("checkouts"))]
    groups = [_mapping(item) for item in _items(value.get("origin_groups"))]
    service = _mapping(value.get("service"))
    presenter.outcome(
        f"Ready to initialize {len(checkouts)} "
        f"{_plural(len(checkouts), 'checkout')} across {len(groups)} "
        f"{_plural(len(groups), 'origin')}.",
        status="noop",
    )
    _section(
        presenter,
        "Knowledge store",
        [
            f"Path: {_text(value.get('store'))}",
            f"State: {'existing' if value.get('store_exists') else 'will be created'}",
            f"User configuration: {_text(value.get('store_config'))} "
            f"[{_text(value.get('store_config_action'))}]",
        ],
    )
    _section(
        presenter,
        "Shared origin namespaces",
        [
            f"{_text(group.get('origin'))} -> {_text(group.get('namespace'))}; "
            f"checkouts: {', '.join(str(item) for item in _items(group.get('checkouts')))}"
            for group in groups
        ],
    )
    _section(
        presenter,
        "Checkouts and local files",
        [
            line
            for checkout in checkouts
            for line in (
                f"{_text(checkout.get('repository'))} "
                f"[registration: {_text(checkout.get('registration_action'))}]",
                f"Configuration: {_text(checkout.get('config_path'))} "
                f"[{_text(checkout.get('config_action'))}]",
                f"Git exclude: {_text(checkout.get('exclude_path'))} "
                f"[{_text(checkout.get('exclude_action'))}]",
                f"Local knowledge: {_text(checkout.get('local_root'))} "
                f"[{_text(checkout.get('local_root_action'))}]",
            )
        ],
    )
    imports = [
        f"{_text(checkout.get('repository'))}: {_text(item.get('path'))} "
        f"[{_text(item.get('action'))}] -> {_text(item.get('target'))}"
        for checkout in checkouts
        for item in [_mapping(raw) for raw in _items(checkout.get("legacy"))]
    ]
    _section(presenter, "Legacy imports", imports or ["None"])
    _section(
        presenter,
        "Central commit",
        ["enabled" if value.get("central_commit") else "disabled"],
    )
    service_checks = (
        "label={label}, executable={executable}, store={store}, interval={interval}, "
        "arguments={arguments}, plist={plist}, loaded={loaded}"
    ).format(
        label="match" if service.get("label_matches") else "different",
        executable="match" if service.get("executable_matches") else "different",
        store="match" if service.get("store_matches") else "different",
        interval="match" if service.get("interval_matches") else "different",
        arguments="match" if service.get("arguments_match") else "different",
        plist="match" if service.get("plist_matches") else "different",
        loaded="yes" if service.get("loaded") else "no",
    )
    _section(
        presenter,
        "Automatic service",
        [
            f"Action: {_text(service.get('action'))}",
            f"Interval: {_text(service.get('interval_seconds'))} seconds",
            f"Definition: {_text(service.get('plist'))}",
            f"Desired-state comparison: {service_checks}",
        ],
    )


def _present_init_publication(
    value: Mapping[str, object], presenter: Presenter
) -> None:
    phases = [_mapping(item) for item in _items(value.get("phases"))]
    failures = [phase for phase in phases if phase.get("status") == "failed"]
    changed = [phase for phase in phases if phase.get("status") == "changed"]
    if failures:
        presenter.outcome(
            f"Initialization stopped after {len(failures)} failed phase.",
            status="error",
        )
    elif changed:
        presenter.outcome("Initialization completed successfully.", status="changed")
    else:
        presenter.outcome("Initialization is already up to date.", status="noop")

    lines: list[str] = []
    for phase in phases:
        repository = phase.get("repository")
        line = f"{phase.get('phase', 'unknown')} [{phase.get('status', 'unknown')}]"
        if repository is not None:
            line += f" {repository}"
        if phase.get("error") is not None:
            line += f": {phase['error']}"
        elif phase.get("reason") is not None:
            line += f": {phase['reason']}"
        warnings = _items(phase.get("warnings"))
        if warnings:
            line += f": warning: {', '.join(_text(item) for item in warnings)}"
        lines.append(line)
    presenter.section("Phases", lines)

    retries = _items(value.get("retry_commands"))
    if retries:
        presenter.section("Retry", retries)


def _present_setup(value: Mapping[str, object], presenter: Presenter) -> None:
    checkouts = [_mapping(item) for item in _items(value.get("checkouts"))]
    count = len(checkouts)
    conflicts = sum(
        len(_items(_mapping(item.get("import")).get("conflict")))
        for item in checkouts
    )
    presenter.outcome(
        f"Configured {count} {_plural(count, 'checkout')}.",
        status="warning" if conflicts else "changed",
    )
    _section(
        presenter,
        "Checkouts",
        [
            f"{_text(item.get('repository'))} ({_text(item.get('origin'))})"
            for item in checkouts
        ],
    )
    _section(
        presenter,
        "Managed paths",
        [
            f"{label}: {_text(item.get(key))}"
            for item in checkouts
            for label, key in (
                ("Namespace", "namespace"),
                ("Configuration", "config"),
                ("Git exclude", "exclude"),
            )
        ],
    )
    if conflicts:
        presenter.warning(
            f"Preserved {conflicts} {_plural(conflicts, 'import conflict')} for review."
        )
    _section(presenter, "Next", ["Run pce doctor --repo <checkout> to verify setup."])


def _present_reconcile(
    command: str,
    value: Mapping[str, object],
    presenter: Presenter,
) -> None:
    actions = [_mapping(item) for item in _items(value.get("actions"))]
    verb = "Hydrated" if command == "hydrate" else "Synchronized"
    if actions:
        presenter.outcome(
            f"{verb} {len(actions)} {_plural(len(actions), 'artifact')}.",
            status="changed",
        )
    else:
        presenter.outcome(
            f"Repository is already {command}d." if command == "hydrate" else "Repository is already synchronized.",
            status="noop",
        )
    _section(presenter, "Repository", [_text(value.get("repository"))])
    _section(
        presenter,
        "Changes",
        [
            f"{_text(item.get('path'))}: {_text(item.get('source'))} -> {_text(item.get('target'))}"
            + (" (deleted)" if item.get("deleted") else "")
            for item in actions
        ],
    )
    _section(presenter, "Commit", _commit_line(value.get("central_commit")))
    _section(presenter, "Next", ["Run pce status to inspect synchronization state."])


def _present_status(value: Mapping[str, object], presenter: Presenter) -> None:
    conflicts = _items(value.get("conflicts"))
    local = _items(value.get("local_changes"))
    central = _items(value.get("central_changes"))
    if conflicts:
        presenter.outcome(
            f"Repository has {len(conflicts)} {_plural(len(conflicts), 'conflict')}.",
            status="warning",
        )
    elif value.get("in_sync"):
        presenter.outcome("Repository is synchronized.", status="noop")
    else:
        presenter.outcome("Repository has unsynchronized changes.", status="warning")
    _section(presenter, "Repository", [_text(value.get("repository"))])
    _section(presenter, "Local changes", local)
    _section(presenter, "Central changes", central)
    _section(presenter, "Conflicts", conflicts)
    if conflicts:
        _section(presenter, "Next", ["Resolve conflicts before running pce sync."])
    elif not value.get("in_sync"):
        _section(presenter, "Next", ["Run pce sync to reconcile these changes."])


def _present_doctor(value: Mapping[str, object], presenter: Presenter) -> None:
    ok = bool(value.get("ok"))
    presenter.outcome(
        "PCE is healthy." if ok else "PCE needs attention.",
        status="noop" if ok else "warning",
    )
    missing = _items(value.get("missing_excludes"))
    visible = _items(value.get("visible_personal_artifacts"))
    checks = [
        f"Namespace: {'present' if value.get('namespace_exists') else 'missing'}",
        f"docs_root: {'valid' if value.get('docs_root_valid') else 'invalid'}",
        f"Git excludes: {'complete' if not missing else f'{len(missing)} missing'}",
        f"Visible personal artifacts: {len(visible)}",
    ]
    _section(presenter, "Checks", checks)
    _section(presenter, "Missing excludes", missing)
    _section(presenter, "Visible artifacts", visible)
    if not ok:
        _section(presenter, "Next", ["Run pce setup --repo <checkout> to repair setup."])


def _present_repo_info(value: Mapping[str, object], presenter: Presenter) -> None:
    presenter.outcome("Repository information loaded.", status="noop")
    _section(
        presenter,
        "Repository",
        [
            f"Checkout: {_text(value.get('repository'))}",
            f"Origin: {_text(value.get('canonical_origin'))}",
            f"Key: {_text(value.get('key'))}",
            f"Namespace: {_text(value.get('namespace'))}",
        ],
    )


def _present_inventory(value: Mapping[str, object], presenter: Presenter) -> None:
    project = _mapping(value.get("project"))
    artifacts = _items(project.get("artifacts"))
    checkouts = _items(project.get("checkouts"))
    library = _items(value.get("library"))
    inbox = _items(value.get("inbox"))
    total = len(artifacts) + len(library) + len(inbox)
    presenter.outcome(
        f"Inventory contains {total} {_plural(total, 'knowledge file')}.",
        status="noop",
    )
    _section(presenter, "Checkouts", checkouts)
    _section(presenter, "Project artifacts", artifacts)
    _section(presenter, "Library", library)
    _section(presenter, "Inbox", inbox)


def _present_restore(value: Mapping[str, object], presenter: Presenter) -> None:
    presenter.outcome(
        f"Restored {_text(value.get('path'), 'artifact')}.", status="changed"
    )
    _section(
        presenter,
        "Paths",
        [
            f"From: {_text(value.get('restored_from'))}",
            f"To: {_text(value.get('restored_to'))}",
        ],
    )
    _section(presenter, "Next", ["Review the restored local artifact before editing."])


def _present_harvest(value: Mapping[str, object], presenter: Presenter) -> None:
    commits = [_mapping(item) for item in _items(value.get("commits"))]
    presenter.outcome(
        f"Found {len(commits)} {_plural(len(commits), 'commit')} to review.",
        status="noop",
    )
    _section(
        presenter,
        "Commits",
        [
            f"{_text(item.get('revision'))[:12]} { _text(item.get('subject'))} ({_text(item.get('author'))})"
            for item in commits
        ],
    )
    _section(
        presenter,
        "Next",
        [
            "Capture useful learnings, then run "
            f"pce harvest-mark --revision {_text(value.get('safe_mark_revision'))} "
            "--review-file <path>."
        ],
    )


def _present_harvest_mark(value: Mapping[str, object], presenter: Presenter) -> None:
    presenter.outcome("Harvest watermark advanced.", status="changed")
    _section(
        presenter,
        "Recorded",
        [
            f"Revision: {_text(value.get('revision'))}",
            f"Review: {_text(value.get('review'), 'none')}",
            f"State: {_text(value.get('state_path'))}",
        ],
    )
    _section(presenter, "Commit", _commit_line(value.get("central_commit")))


def _present_search(value: Mapping[str, object], presenter: Presenter) -> None:
    results = [_mapping(item) for item in _items(value.get("results"))]
    presenter.outcome(
        f"Found {len(results)} {_plural(len(results), 'match', 'matches')}."
        if results
        else "No matching knowledge found.",
        status="noop",
    )
    _section(
        presenter,
        "Results",
        [
            f"{_text(item.get('path'))} [{_text(item.get('source'))}, score {_text(item.get('score'))}] — {_text(item.get('snippet'), '')}"
            for item in results
        ],
    )


def _present_sync_all(value: Mapping[str, object], presenter: Presenter) -> None:
    failures = [_mapping(item) for item in _items(value.get("failures"))]
    skipped = [_mapping(item) for item in _items(value.get("skipped"))]
    changes = sum(
        len(_items(_mapping(item).get("actions")))
        for key in ("sync", "hydrate")
        for item in _items(value.get(key))
    )
    if failures:
        presenter.outcome(
            f"Synchronization completed with {len(failures)} {_plural(len(failures), 'failure')}.",
            status="warning",
        )
    elif changes:
        presenter.outcome(
            f"Synchronized {changes} {_plural(changes, 'artifact action')}.",
            status="changed",
        )
    else:
        presenter.outcome("All registered checkouts are synchronized.", status="noop")
    _section(
        presenter,
        "Checkouts",
        [
            f"Registered: {_text(value.get('registered_checkouts'), '0')}",
            f"Available: {_text(value.get('available_checkouts'), '0')}",
        ],
    )
    _section(
        presenter,
        "Skipped",
        [f"{_text(item.get('repository'))}: {_text(item.get('reason'))}" for item in skipped],
    )
    _section(
        presenter,
        "Failures",
        [
            f"{_text(item.get('repository'))} ({_text(item.get('phase'))}): {_text(item.get('error'))}"
            for item in failures
        ],
    )
    _section(presenter, "Commit", _commit_line(value.get("central_commit")))
    if failures:
        _section(presenter, "Next", ["Resolve the listed failures, then rerun pce sync-all."])


def _present_service(
    action: str,
    value: Mapping[str, object],
    presenter: Presenter,
) -> None:
    if action == "install":
        presenter.outcome(
            "Automatic synchronization service installed.", status="changed"
        )
    elif action == "uninstall":
        presenter.outcome(
            "Automatic synchronization service removed.", status="changed"
        )
    elif value.get("installed") and value.get("loaded"):
        presenter.outcome("Automatic synchronization service is running.", status="noop")
    elif value.get("installed"):
        presenter.outcome(
            "Automatic synchronization service is installed but not running.",
            status="warning",
        )
    else:
        presenter.outcome(
            "Automatic synchronization service is not installed.", status="warning"
        )
    paths = [
        f"{key.replace('_', ' ').title()}: {_text(value.get(key))}"
        for key in ("plist", "status", "stdout", "stderr")
        if value.get(key) is not None
    ]
    if value.get("interval_seconds") is not None:
        paths.insert(0, f"Interval: {value['interval_seconds']} seconds")
    _section(presenter, "Service", paths)
    if action == "install":
        _section(presenter, "Next", ["Run pce service status to inspect its latest run."])


class PromptOption:
    """One stable prompt value with human-facing metadata."""

    def __init__(
        self,
        value: str,
        label: str,
        hint: str | None = None,
        *,
        disabled: bool = False,
    ) -> None:
        self.value = value
        self.label = label
        self.hint = hint
        self.disabled = disabled

    def cleaned(self) -> PromptOption:
        return PromptOption(
            self.value,
            sanitize_terminal_text(self.label),
            None if self.hint is None else sanitize_terminal_text(self.hint),
            disabled=self.disabled,
        )


class PromptBindings(Protocol):
    """Injectable synchronous prompt port."""

    def text(self, message: str, default: str | None) -> object: ...

    def select(
        self,
        message: str,
        options: list[PromptOption],
        initial: str | None,
    ) -> object: ...

    def multiselect(
        self,
        message: str,
        options: list[PromptOption],
        initial: list[str],
    ) -> object: ...

    def confirm(self, message: str, initial: bool) -> object: ...


class PromptCancelled(RuntimeError):
    """A guided prompt was cancelled, interrupted, or reached EOF."""

    def __init__(self) -> None:
        super().__init__("Setup cancelled")


CANCELLED = object()


class Prompter:
    """Sanitizing, validating prompt facade over injectable bindings."""

    def __init__(self, bindings: PromptBindings) -> None:
        self._bindings = bindings

    def text(self, message: object, default: object | None = None) -> str:
        result = self._invoke(
            self._bindings.text,
            sanitize_terminal_text(message),
            None if default is None else sanitize_terminal_text(default),
        )
        if not isinstance(result, str):
            raise ValueError("prompt text answer must be a string")
        return result

    def select(
        self,
        message: object,
        options: Sequence[PromptOption],
        initial: str | None = None,
    ) -> str:
        cleaned = [option.cleaned() for option in options]
        result = self._invoke(
            self._bindings.select,
            sanitize_terminal_text(message),
            cleaned,
            initial,
        )
        selected = self._option_for_value(cleaned, result)
        return selected.value

    def multiselect(
        self,
        message: object,
        options: Sequence[PromptOption],
        initial: Sequence[str] = (),
    ) -> list[str]:
        cleaned = [option.cleaned() for option in options]
        result = self._invoke(
            self._bindings.multiselect,
            sanitize_terminal_text(message),
            cleaned,
            list(initial),
        )
        if not isinstance(result, list):
            raise ValueError("prompt selections must be a list")
        return [self._option_for_value(cleaned, value).value for value in result]

    def confirm(self, message: object, initial: bool) -> bool:
        result = self._invoke(
            self._bindings.confirm,
            sanitize_terminal_text(message),
            initial,
        )
        if not isinstance(result, bool):
            raise ValueError("prompt confirmation must be a boolean")
        return result

    def _invoke(self, function: Callable[..., object], *args: object) -> object:
        try:
            result = function(*args)
        except (EOFError, KeyboardInterrupt) as exc:
            raise PromptCancelled() from exc
        if result is CANCELLED:
            raise PromptCancelled()
        return result

    @staticmethod
    def _option_for_value(
        options: Sequence[PromptOption],
        value: object,
    ) -> PromptOption:
        for option in options:
            if option.value == value and not option.disabled:
                return option
        raise ValueError(f"unknown prompt selection: {value!r}")


class TerminalPromptBindings:
    """Simple line-oriented prompts suitable for plain and rich terminals."""

    def __init__(
        self,
        *,
        input_stream: TextIO = sys.stdin,
        output: TextIO = sys.stdout,
        rich: bool = False,
    ) -> None:
        self._input = input_stream
        self._output = output
        self._rich = rich

    def text(self, message: str, default: str | None) -> object:
        suffix = "" if default is None else f" [{default}]"
        answer = self._ask(f"{message}{suffix}: ")
        return answer if answer else (default or "")

    def select(
        self,
        message: str,
        options: list[PromptOption],
        initial: str | None,
    ) -> object:
        default_index = next(
            (
                index
                for index, option in enumerate(options, 1)
                if option.value == initial and not option.disabled
            ),
            None,
        )
        while True:
            self._render_options(message, options)
            suffix = "" if default_index is None else f" [{default_index}]"
            answer = self._ask(f"Choose one{suffix}: ")
            if answer == "" and default_index is not None:
                answer = str(default_index)
            selected = self._parse_indexes(answer, len(options), multiple=False)
            if selected is None:
                self._write("Choose one of the listed numbers. Try again.\n")
                continue
            option = options[selected[0] - 1]
            if option.disabled:
                self._write("That choice is unavailable. Try again.\n")
                continue
            return option.value

    def multiselect(
        self,
        message: str,
        options: list[PromptOption],
        initial: list[str],
    ) -> object:
        defaults = [
            str(index)
            for index, option in enumerate(options, 1)
            if option.value in initial and not option.disabled
        ]
        while True:
            self._render_options(message, options)
            suffix = "" if not defaults else f" [{','.join(defaults)}]"
            answer = self._ask(
                f"Choose numbers separated by commas{suffix} (or none): "
            )
            if answer.lower() == "none":
                return []
            if answer == "":
                answer = ",".join(defaults)
                if not answer:
                    return []
            selected = self._parse_indexes(answer, len(options), multiple=True)
            if selected is None:
                self._write(
                    "Choose listed numbers separated by commas, or none. Try again.\n"
                )
                continue
            chosen = [options[index - 1] for index in selected]
            if any(option.disabled for option in chosen):
                self._write("That choice is unavailable. Try again.\n")
                continue
            return [option.value for option in chosen]

    def confirm(self, message: str, initial: bool) -> object:
        while True:
            answer = self._ask(f"{message} {'[Y/n]' if initial else '[y/N]'}: ").lower()
            if not answer:
                return initial
            if answer in {"y", "yes"}:
                return True
            if answer in {"n", "no"}:
                return False
            self._write("Please answer yes or no.\n")

    def _render_options(self, message: str, options: Sequence[PromptOption]) -> None:
        self._write(f"{message}\n")
        marker = "›" if self._rich else ")"
        for index, option in enumerate(options, 1):
            hint = "" if option.hint is None else f" - {option.hint}"
            disabled = " (unavailable)" if option.disabled else ""
            self._write(f"  {index}{marker} {option.label}{hint}{disabled}\n")

    def _ask(self, prompt: str) -> str:
        self._write(prompt)
        value = self._input.readline()
        if value == "":
            raise EOFError
        answer = value.strip()
        if answer.lower() in {"cancel", "quit", "q"}:
            raise PromptCancelled()
        return answer

    @staticmethod
    def _parse_indexes(
        raw: str,
        count: int,
        *,
        multiple: bool,
    ) -> list[int] | None:
        parts = [part.strip() for part in raw.split(",")]
        if not multiple and len(parts) != 1:
            return None
        if not parts or any(not part.isdigit() for part in parts):
            return None
        values = list(dict.fromkeys(int(part) for part in parts))
        if any(value < 1 or value > count for value in values):
            return None
        return values

    def _write(self, value: str) -> None:
        self._output.write(_stream_safe_text(value, self._output))
        self._output.flush()


def create_prompter(
    *,
    input_stream: TextIO = sys.stdin,
    output: TextIO = sys.stdout,
    rich: bool = False,
) -> Prompter:
    """Create an interactive prompter without acquiring domain resources."""

    return Prompter(
        TerminalPromptBindings(
            input_stream=input_stream,
            output=output,
            rich=rich,
        )
    )

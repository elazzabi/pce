#!/usr/bin/env python3
"""Private, origin-keyed storage for Compound Engineering artifacts."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from urllib.parse import urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from pce_ui import (  # noqa: E402
    PromptCancelled,
    PromptOption,
    Presenter,
    Prompter,
    create_presenter,
    create_prompter,
    present_command_result,
    render_shell_command,
    sanitize_terminal_text,
    select_output_mode,
)

PCE_VERSION = "0.1.0"

STORE_ENV = "PERSONAL_COMPOUND_HOME"
CONFIG_HOME_ENV = "PERSONAL_COMPOUND_CONFIG_HOME"
LOCAL_ROOT_NAME = ".ce-personal"
STATE_NAME = ".personal-compound-state.json"
EXCLUDE_ENTRIES = (
    "/.ce-personal/",
    "/.compound-engineering/config.local.yaml",
    "/CONCEPTS.md",
    "/docs/plans/",
    "/docs/solutions/",
    "/.claude/docs/plans/",
    "/.claude/docs/solutions/",
)
LEGACY_ARTIFACT_DIRS = (
    ("docs/plans", "plans"),
    ("docs/solutions", "solutions"),
    (".claude/docs/plans", "plans"),
    (".claude/docs/solutions", "solutions"),
)
ARTIFACT_KEY_PREFIX = "artifact:"
CONCEPTS_KEY = "concepts:"
SERVICE_LABEL = "com.personal-compound.sync"
SERVICE_INTERVAL = 30
SERVICE_SETTLE_SECONDS = 10


class PceError(RuntimeError):
    pass


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    timeout = float(os.environ.get("PERSONAL_COMPOUND_COMMAND_TIMEOUT", "120"))
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            check=False,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise PceError(f"{' '.join(args)} timed out after {timeout:g} seconds") from exc
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise PceError(f"{' '.join(args)} failed: {detail}")
    return result


def git(repo: Path, *args: str, check: bool = True) -> str:
    return run(["git", "-C", str(repo), *args], check=check).stdout.strip()


def resolve_repo(raw: str | None) -> Path:
    candidate = Path(raw or os.getcwd()).expanduser().resolve()
    result = run(
        ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
        check=False,
    )
    if result.returncode != 0:
        raise PceError(f"not a Git checkout: {candidate}")
    return Path(result.stdout.strip()).resolve()


def user_config_path() -> Path:
    override = os.environ.get(CONFIG_HOME_ENV)
    if override:
        return Path(override).expanduser().resolve() / "config.json"
    if sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support" / "Personal Compound"
    else:
        root = Path(
            os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
        ) / "pce"
    return root.expanduser().resolve() / "config.json"


def default_store_root() -> Path:
    if sys.platform == "darwin":
        root = (
            Path.home()
            / "Library"
            / "Application Support"
            / "Personal Compound"
            / "Knowledge"
        )
    else:
        root = Path(
            os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
        ) / "pce" / "knowledge"
    return root.expanduser().resolve()


def configured_store_root() -> Path | None:
    path = user_config_path()
    value = read_json(path, {})
    if not isinstance(value, dict):
        raise PceError(f"user configuration is not an object: {path}")
    raw = value.get("store")
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise PceError(f"user configuration has an invalid store path: {path}")
    return Path(raw).expanduser().resolve()


def store_root() -> Path:
    override = os.environ.get(STORE_ENV)
    if override:
        return Path(override).expanduser().resolve()
    return configured_store_root() or default_store_root()


def persist_store_root(root: Path) -> Path:
    selected = root.expanduser().resolve()
    path = user_config_path()
    current = read_json(path, {})
    if not isinstance(current, dict):
        raise PceError(f"user configuration is not an object: {path}")
    if current.get("store") == str(selected):
        return path
    updated = dict(current)
    updated["store"] = str(selected)
    write_json(path, updated)
    return path


@contextmanager
def selected_store(root: Path) -> Iterable[None]:
    """Temporarily bind domain primitives to one reviewed store path."""

    previous = os.environ.get(STORE_ENV)
    os.environ[STORE_ENV] = str(root.expanduser().resolve())
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(STORE_ENV, None)
        else:
            os.environ[STORE_ENV] = previous


def normalize_origin(raw: str) -> str:
    value = raw.strip()
    if not value:
        raise PceError("origin has no URL")

    scp_match = re.match(r"^[^@/\s]+@([^:\s]+):(.+)$", value)
    if scp_match:
        host, path = scp_match.groups()
    elif "://" in value:
        parsed = urlparse(value)
        host = parsed.hostname or ""
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        path = parsed.path
    else:
        path_obj = Path(value).expanduser()
        if path_obj.is_absolute() or value.startswith("."):
            return f"local/{path_obj.resolve().as_posix().lstrip('/')}"
        raise PceError(f"unsupported origin URL: {value}")

    path = path.strip("/").removesuffix(".git").strip("/")
    if not host or not path:
        raise PceError(f"could not normalize origin URL: {value}")
    normalized_host = host.lower()
    normalized_path = (
        path.lower()
        if normalized_host.split(":", 1)[0] in {"github.com", "github.a8c.com"}
        else path
    )
    return f"{normalized_host}/{normalized_path}"


def origin_info(repo: Path) -> tuple[str, str]:
    raw = git(repo, "remote", "get-url", "origin")
    canonical = normalize_origin(raw)
    slug = re.sub(
        r"[^a-zA-Z0-9._-]+",
        "-",
        canonical.replace("/", "__"),
    ).strip("-")
    suffix = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    key = f"{slug}--{suffix}"
    if not key:
        raise PceError(f"origin produced an empty key: {raw}")
    return canonical, key


def namespace(repo: Path) -> tuple[Path, dict[str, object]]:
    canonical, key = origin_info(repo)
    root = store_root() / "projects" / key
    metadata = {
        "key": key,
        "canonical_origin": canonical,
    }
    return root, metadata


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def read_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PceError(f"invalid JSON at {path}: {exc}") from exc


def write_json(path: Path, value: object) -> None:
    atomic_write_text(
        path,
        json.dumps(value, indent=2, sort_keys=True) + "\n",
    )


def atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temp = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temp = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            shutil.copymode(path, temp)
        os.replace(temp, path)
        fsync_parent(path)
    finally:
        if temp.exists():
            temp.unlink()


def atomic_copy(source: Path, target: Path) -> None:
    source_hash = sha256(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temp = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    os.close(descriptor)
    temp = Path(raw_temp)
    try:
        shutil.copy2(source, temp)
        if sha256(temp) != source_hash or sha256(source) != source_hash:
            raise PceError(f"source changed while copying; retry later: {source}")
        with temp.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temp, target)
        fsync_parent(target)
    finally:
        if temp.exists():
            temp.unlink()


def fsync_parent(path: Path) -> None:
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def validate_store_path(root: Path) -> None:
    """Reject store choices that cannot be adopted without destructive setup."""

    root = root.expanduser().resolve()
    if root.exists() and not root.is_dir():
        raise PceError(f"knowledge store path is not a directory: {root}")
    try:
        user_config_path().relative_to(root)
    except ValueError:
        pass
    else:
        raise PceError(
            "knowledge store must not contain Personal Compound user configuration: "
            f"{root}"
        )
    source_root = SCRIPT_DIR.parent
    if source_root == root or source_root.is_relative_to(root):
        raise PceError(f"knowledge store must be separate from PCE source: {root}")
    if not root.exists() or (root / ".git").exists():
        return
    meaningful_entries = [path for path in root.iterdir() if path.name != ".pce.lock"]
    if meaningful_entries:
        raise PceError(
            "refusing to initialize Git in a non-empty knowledge store; "
            f"choose an existing PCE Git store or an empty directory: {root}"
        )


def ensure_store() -> Path:
    root = store_root()
    validate_store_path(root)
    root.mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists():
        result = run(["git", "init", "-b", "main", str(root)], check=False)
        if result.returncode != 0:
            run(["git", "init", str(root)])
    (root / "projects").mkdir(exist_ok=True)
    (root / "library").mkdir(exist_ok=True)
    (root / "inbox").mkdir(exist_ok=True)
    return root


@contextmanager
def store_lock() -> Iterable[None]:
    root = store_root()
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".pce.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        timeout = float(os.environ.get("PERSONAL_COMPOUND_LOCK_TIMEOUT", "30"))
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PceError(
                        f"timed out waiting {timeout:g} seconds for {lock_path}"
                    ) from exc
                time.sleep(min(0.1, remaining))
        try:
            ensure_store()
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def is_tracked(repo: Path, rel: str) -> bool:
    return (
        run(
            ["git", "-C", str(repo), "ls-files", "--error-unmatch", "--", rel],
            check=False,
        ).returncode
        == 0
    )


def update_metadata(repo: Path, ns: Path, base: dict[str, object]) -> None:
    path = ns / "metadata.json"
    current = read_json(path, {})
    if not isinstance(current, dict):
        raise PceError(f"metadata is not an object: {path}")
    checkouts = {
        str(Path(item).expanduser().resolve())
        for item in current.get("checkouts", [])
        if isinstance(item, str)
    }
    checkouts.add(str(repo.resolve()))
    stable = {
        "key": base["key"],
        "canonical_origin": base["canonical_origin"],
        "checkouts": sorted(checkouts),
    }
    changed = any(current.get(key) != value for key, value in stable.items())
    current.pop("observed_origin", None)
    current.pop("observed_origins", None)
    current.update(stable)
    if "created_at" not in current:
        current["created_at"] = now()
        changed = True
    if changed or "updated_at" not in current:
        current["updated_at"] = now()
    write_json(path, current)


def render_local_config(text: str) -> str:
    """Return the additive local configuration setup will publish."""

    lines = text.splitlines()
    active = re.compile(r"^docs_root\s*:")
    output: list[str] = []
    replaced = False
    for line in lines:
        if active.match(line):
            if not replaced:
                output.append(f"docs_root: {LOCAL_ROOT_NAME}")
                replaced = True
            continue
        output.append(line)
    if not replaced:
        prefix = [
            "# Personal Compound Engineering (managed locally)",
            f"docs_root: {LOCAL_ROOT_NAME}",
        ]
        if output and any(item.strip() for item in output):
            prefix.append("")
        output = prefix + output
    return "\n".join(output).rstrip() + "\n"


def update_local_config(repo: Path) -> Path:
    path = repo / ".compound-engineering" / "config.local.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    atomic_write_text(path, render_local_config(text))
    return path


def git_exclude_path(repo: Path) -> Path:
    raw = git(repo, "rev-parse", "--git-path", "info/exclude")
    path = Path(raw)
    return path if path.is_absolute() else repo / path


def missing_exclude_entries(text: str) -> tuple[str, ...]:
    existing = {line.strip() for line in text.splitlines()}
    return tuple(item for item in EXCLUDE_ENTRIES if item not in existing)


def update_git_exclude(repo: Path) -> Path:
    path = git_exclude_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines()
    existing = {line.strip() for line in lines}
    missing = missing_exclude_entries(text)
    if missing:
        if lines and lines[-1].strip():
            lines.append("")
        if "# Personal Compound Engineering" not in existing:
            lines.append("# Personal Compound Engineering")
        lines.extend(missing)
        atomic_write_text(path, "\n".join(lines).rstrip() + "\n")
    return path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iter_files(root: Path) -> Iterable[Path]:
    if not root.exists():
        return
    for path in root.rglob("*"):
        if ".git" in path.parts:
            continue
        if path.is_symlink():
            raise PceError(f"symlinks are not supported in artifact trees: {path}")
        if path.is_file():
            yield path


@dataclass(frozen=True)
class LegacyImportPlan:
    source: Path
    label: str
    outcome: str
    target: Path
    source_fingerprint: str
    destination: Path
    destination_fingerprint: str
    target_fingerprint: str


def _tracked_legacy_paths(repo: Path) -> set[str]:
    candidates = [source for source, _kind in LEGACY_ARTIFACT_DIRS]
    candidates.append("CONCEPTS.md")
    result = run(
        ["git", "-C", str(repo), "ls-files", "-z", "--", *candidates],
        check=False,
    )
    return {item for item in result.stdout.split("\0") if item}


def _path_fingerprint(path: Path) -> str:
    if path.is_symlink():
        return "symlink"
    if not path.exists():
        return "missing"
    if path.is_file():
        return f"file:{sha256(path)}"
    if path.is_dir():
        return "directory"
    return "other"


def plan_legacy_imports(
    repo: Path,
    ns: Path,
    planned_hashes: dict[Path, str] | None = None,
) -> tuple[LegacyImportPlan, ...]:
    """Plan legacy imports in the same order and state setup will apply them."""

    tracked = _tracked_legacy_paths(repo)
    if planned_hashes is None:
        planned_hashes = {}
    plans: list[LegacyImportPlan] = []
    checkout_id = hashlib.sha256(
        str(repo.resolve()).encode("utf-8")
    ).hexdigest()[:12]

    def add(source: Path, label: str, target: Path, conflict_target: Path) -> None:
        source_hash = sha256(source)
        destination = target
        target_hash = planned_hashes.get(target)
        if target_hash is None and target.exists():
            target_hash = sha256(target)
            planned_hashes[target] = target_hash
        destination_fingerprint = (
            f"file:{target_hash}" if target_hash is not None else "missing"
        )
        if target_hash is None:
            outcome = "imported"
            planned_hashes[target] = source_hash
        elif target_hash == source_hash:
            outcome = "duplicate"
        else:
            outcome = "conflict"
            target = conflict_target
        plans.append(
            LegacyImportPlan(
                source,
                label,
                outcome,
                target,
                f"file:{source_hash}",
                destination,
                destination_fingerprint,
                (
                    destination_fingerprint
                    if target == destination
                    else _path_fingerprint(target)
                ),
            )
        )

    artifacts = ns / "artifacts"
    for source_rel, target_kind in LEGACY_ARTIFACT_DIRS:
        source_root = repo / source_rel
        if not source_root.exists():
            continue
        for source in iter_files(source_root):
            relative = source.relative_to(source_root)
            label = f"{source_rel}/{relative.as_posix()}"
            if label in tracked:
                continue
            add(
                source,
                label,
                artifacts / target_kind / relative,
                ns
                / "import-conflicts"
                / f"{repo.name}-{checkout_id}"
                / source_rel.replace("/", "__")
                / relative,
            )

    concepts = repo / "CONCEPTS.md"
    if concepts.exists() and "CONCEPTS.md" not in tracked:
        add(
            concepts,
            "CONCEPTS.md",
            ns / "CONCEPTS.md",
            ns
            / "import-conflicts"
            / f"{repo.name}-{checkout_id}"
            / "CONCEPTS.md",
        )
    return tuple(plans)


def import_legacy(repo: Path, ns: Path) -> dict[str, list[str]]:
    summary: dict[str, list[str]] = {"imported": [], "duplicate": [], "conflict": []}
    for item in plan_legacy_imports(repo, ns):
        if item.outcome != "duplicate":
            item.target.parent.mkdir(parents=True, exist_ok=True)
            atomic_copy(item.source, item.target)
        summary[item.outcome].append(item.label)
    return summary


def artifact_files(
    root: Path,
    *,
    exclude_local_state: bool = False,
) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for path in iter_files(root):
        if exclude_local_state and path == root / STATE_NAME:
            continue
        rel = path.relative_to(root).as_posix()
        result[f"{ARTIFACT_KEY_PREFIX}{rel}"] = path
    return result


def central_files(repo: Path, ns: Path) -> dict[str, Path]:
    result = artifact_files(ns / "artifacts")
    concepts = ns / "CONCEPTS.md"
    if concepts.exists() and not is_tracked(repo, "CONCEPTS.md"):
        result[CONCEPTS_KEY] = concepts
    return result


def local_files(repo: Path) -> dict[str, Path]:
    root = repo / LOCAL_ROOT_NAME
    result = artifact_files(root, exclude_local_state=True)
    concepts = repo / "CONCEPTS.md"
    if concepts.exists() and not is_tracked(repo, "CONCEPTS.md"):
        result[CONCEPTS_KEY] = concepts
    return result


def manifest(files: dict[str, Path]) -> dict[str, str]:
    return {rel: sha256(path) for rel, path in sorted(files.items())}


def state_path(repo: Path) -> Path:
    return repo / LOCAL_ROOT_NAME / STATE_NAME


def load_baseline(repo: Path) -> dict[str, str]:
    data = read_json(state_path(repo), {"baseline": {}})
    if not isinstance(data, dict) or not isinstance(data.get("baseline"), dict):
        raise PceError(f"invalid local baseline: {state_path(repo)}")
    return {str(key): str(value) for key, value in data["baseline"].items()}


def save_baseline(repo: Path, key: str, values: dict[str, str]) -> None:
    path = state_path(repo)
    baseline = dict(sorted(values.items()))
    current = read_json(path, {})
    if (
        isinstance(current, dict)
        and current.get("baseline") == baseline
        and current.get("repository_key") == key
    ):
        return
    write_json(
        path,
        {
            "baseline": baseline,
            "repository_key": key,
            "synced_at": now(),
        },
    )


def recovery_path(ns: Path, rel: str, side: str) -> Path:
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return (
        store_root()
        / "recovery"
        / ns.name
        / timestamp
        / side
        / ("CONCEPTS.md" if rel == CONCEPTS_KEY else artifact_rel(rel))
    )


def remove_file(path: Path, root: Path, ns: Path, rel: str, side: str) -> None:
    if path.exists():
        recovery = recovery_path(ns, rel, side)
        recovery.parent.mkdir(parents=True, exist_ok=True)
        atomic_copy(path, recovery)
        path.unlink()
        fsync_parent(path)
    parent = path.parent
    while parent != root and parent.exists():
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def target_root(repo: Path, ns: Path, rel: str, side: str) -> Path:
    if rel == CONCEPTS_KEY:
        return repo if side == "local" else ns
    return repo / LOCAL_ROOT_NAME if side == "local" else ns / "artifacts"


def artifact_rel(key: str) -> str:
    if not key.startswith(ARTIFACT_KEY_PREFIX):
        raise PceError(f"invalid artifact manifest key: {key}")
    return key.removeprefix(ARTIFACT_KEY_PREFIX)


def display_key(key: str) -> str:
    return "CONCEPTS.md" if key == CONCEPTS_KEY else artifact_rel(key)


def target_path(repo: Path, ns: Path, rel: str, side: str) -> Path:
    root = target_root(repo, ns, rel, side)
    return root / ("CONCEPTS.md" if rel == CONCEPTS_KEY else artifact_rel(rel))


def apply_value(
    repo: Path,
    ns: Path,
    rel: str,
    value: str | None,
    source_side: str,
    target_side: str,
) -> None:
    source = target_path(repo, ns, rel, source_side)
    target = target_path(repo, ns, rel, target_side)
    root = target_root(repo, ns, rel, target_side)
    if value is None:
        remove_file(target, root, ns, rel, target_side)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_copy(source, target)


def plan_reconcile(
    baseline: dict[str, str],
    local: dict[str, str],
    central: dict[str, str],
    mode: str,
) -> tuple[list[tuple[str, str | None, str, str]], list[str]]:
    actions: list[tuple[str, str | None, str, str]] = []
    conflicts: list[str] = []
    for rel in sorted(set(baseline) | set(local) | set(central)):
        old = baseline.get(rel)
        left = local.get(rel)
        right = central.get(rel)
        if left == right:
            continue
        if left == old:
            actions.append((rel, right, "central", "local"))
            continue
        if right == old:
            if mode == "sync":
                actions.append((rel, left, "local", "central"))
            continue
        conflicts.append(rel)
    return actions, conflicts


def reconcile(
    repo: Path,
    mode: str,
    *,
    allow_delete: bool = False,
) -> dict[str, object]:
    ns, meta = namespace(repo)
    ns.mkdir(parents=True, exist_ok=True)
    (ns / "artifacts").mkdir(exist_ok=True)
    baseline = load_baseline(repo)
    local_before = manifest(local_files(repo))
    central_before = manifest(central_files(repo, ns))
    actions, conflicts = plan_reconcile(baseline, local_before, central_before, mode)
    if conflicts:
        raise PceError(
            "conflicting local and central changes; preserved both sides: "
            + ", ".join(display_key(rel) for rel in conflicts)
        )
    deletions = [rel for rel, value, _, _ in actions if value is None]
    if deletions and not allow_delete:
        raise PceError(
            "reconciliation would delete artifacts; rerun only after review with "
            "--allow-delete: " + ", ".join(display_key(rel) for rel in deletions)
        )
    for rel, value, source, target in actions:
        apply_value(repo, ns, rel, value, source, target)
    local_after = manifest(local_files(repo))
    central_after = manifest(central_files(repo, ns))
    if mode == "sync" and local_after != central_after:
        raise PceError("sync did not converge local and central manifests")
    save_baseline(repo, str(meta["key"]), central_after)
    return {
        "mode": mode,
        "repository": str(repo),
        "key": meta["key"],
        "actions": [
            {
                "path": display_key(rel),
                "source": source,
                "target": target,
                "deleted": value is None,
            }
            for rel, value, source, target in actions
        ],
        "local_files": len(local_after),
        "central_files": len(central_after),
    }


def commit_store(message: str, paths: Iterable[Path]) -> dict[str, object]:
    root = ensure_store()
    relative_paths = sorted(
        {path.resolve().relative_to(root).as_posix() for path in paths}
    )
    if not relative_paths:
        return {"committed": False, "reason": "no central paths supplied"}
    status = git(root, "status", "--porcelain", "--", *relative_paths)
    if not status:
        return {"committed": False, "reason": "no central changes"}
    git(root, "add", "--all", "--", *relative_paths)
    result = run(
        [
            "git",
            "-C",
            str(root),
            "commit",
            "-m",
            message,
            "--",
            *relative_paths,
        ],
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise PceError(
            f"central commit failed; changes remain staged in {root}: {detail}"
        )
    return {
        "committed": True,
        "commit": git(root, "rev-parse", "--short", "HEAD"),
        "message": message,
    }


def registered_checkouts() -> list[tuple[Path, str]]:
    projects = ensure_store() / "projects"
    result: list[tuple[Path, str]] = []
    for metadata_path in sorted(projects.glob("*/metadata.json")):
        metadata = read_json(metadata_path, {})
        if not isinstance(metadata, dict):
            raise PceError(f"metadata is not an object: {metadata_path}")
        checkouts = metadata.get("checkouts", [])
        if not isinstance(checkouts, list):
            raise PceError(f"metadata checkouts is not a list: {metadata_path}")
        for raw in checkouts:
            if isinstance(raw, str):
                result.append((Path(raw).expanduser(), metadata_path.parent.name))
    return sorted(set(result), key=lambda item: (str(item[0]), item[1]))


def newest_artifact_mtime(repo: Path, ns: Path) -> float | None:
    paths = [*local_files(repo).values(), *central_files(repo, ns).values()]
    return max((path.stat().st_mtime for path in paths), default=None)


def automatic_sync(
    *,
    commit: bool = True,
    settle_seconds: int = 0,
) -> dict[str, object]:
    if settle_seconds < 0:
        raise PceError("settle time cannot be negative")
    started_at = now()
    registered = registered_checkouts()
    available: list[tuple[Path, str]] = []
    skipped: list[dict[str, str]] = []
    failures: list[dict[str, str]] = []

    for checkout, expected_key in registered:
        if not checkout.exists():
            skipped.append(
                {
                    "repository": str(checkout),
                    "reason": "registered checkout no longer exists",
                }
            )
            continue
        try:
            repo = resolve_repo(str(checkout))
            ns, meta = namespace(repo)
            actual_key = str(meta["key"])
            if actual_key != expected_key:
                raise PceError(
                    "origin no longer matches its registered namespace "
                    f"({expected_key} != {actual_key})"
                )
            newest_mtime = newest_artifact_mtime(repo, ns)
            if (
                settle_seconds
                and newest_mtime is not None
                and time.time() - newest_mtime < settle_seconds
            ):
                skipped.append(
                    {
                        "repository": str(repo),
                        "reason": (
                            f"artifacts changed within the {settle_seconds}-second "
                            "settling window"
                        ),
                    }
                )
                continue
            available.append((repo, expected_key))
        except PceError as exc:
            failures.append(
                {
                    "phase": "validate",
                    "repository": str(checkout),
                    "error": str(exc),
                }
            )

    sync_results: list[dict[str, object]] = []
    successful: list[tuple[Path, str]] = []
    for repo, key in available:
        try:
            result = reconcile(repo, "sync")
            sync_results.append(result)
            successful.append((repo, key))
        except PceError as exc:
            failures.append(
                {
                    "phase": "sync",
                    "repository": str(repo),
                    "error": str(exc),
                }
            )

    hydrate_results: list[dict[str, object]] = []
    for repo, _ in successful:
        try:
            hydrate_results.append(reconcile(repo, "hydrate"))
        except PceError as exc:
            failures.append(
                {
                    "phase": "hydrate",
                    "repository": str(repo),
                    "error": str(exc),
                }
            )

    changed_namespaces = {
        store_root() / "projects" / key
        for result, (_, key) in zip(sync_results, successful, strict=True)
        if result["actions"]
    }
    central_commit: dict[str, object] | None = None
    if commit and changed_namespaces:
        central_commit = commit_store(
            "chore: auto-sync compound knowledge",
            changed_namespaces,
        )

    return {
        "started_at": started_at,
        "finished_at": now(),
        "ok": not failures,
        "registered_checkouts": len(registered),
        "available_checkouts": len(available),
        "sync": sync_results,
        "hydrate": hydrate_results,
        "skipped": skipped,
        "failures": failures,
        "central_commit": central_commit,
    }


def service_paths() -> dict[str, Path]:
    support = Path.home() / "Library" / "Application Support" / "Personal Compound"
    logs = Path.home() / "Library" / "Logs" / "Personal Compound"
    return {
        "plist": Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist",
        "status": support / "autosync-status.json",
        "stdout": logs / "autosync.log",
        "stderr": logs / "autosync.error.log",
    }


def service_domain() -> str:
    return f"gui/{os.getuid()}"


def service_is_loaded() -> bool:
    return (
        run(
            ["launchctl", "print", f"{service_domain()}/{SERVICE_LABEL}"],
            check=False,
        ).returncode
        == 0
    )


def desired_service_plist(interval: int) -> dict[str, object]:
    """Build launchd's desired state without creating the knowledge store."""

    paths = service_paths()
    root = store_root()
    return {
        "Label": SERVICE_LABEL,
        "ProgramArguments": [
            sys.executable,
            str(Path(__file__).resolve()),
            "sync-all",
            "--commit",
            "--quiet",
            "--settle-seconds",
            str(SERVICE_SETTLE_SECONDS),
            "--status-file",
            str(paths["status"]),
        ],
        "RunAtLoad": True,
        "StartInterval": interval,
        "ProcessType": "Background",
        "WorkingDirectory": str(root),
        "StandardOutPath": str(paths["stdout"]),
        "StandardErrorPath": str(paths["stderr"]),
        "EnvironmentVariables": {
            "PERSONAL_COMPOUND_HOME": str(root),
        },
    }


def service_install(interval: int) -> dict[str, object]:
    if sys.platform != "darwin":
        raise PceError("the automatic service currently supports macOS launchd only")
    if interval < 10:
        raise PceError("service interval must be at least 10 seconds")
    paths = service_paths()
    for key in ("plist", "status", "stdout", "stderr"):
        paths[key].parent.mkdir(parents=True, exist_ok=True)
    ensure_store()
    plist = desired_service_plist(interval)
    if service_is_loaded():
        run(["launchctl", "bootout", service_domain(), str(paths["plist"])])
    atomic_write_text(
        paths["plist"],
        plistlib.dumps(plist, sort_keys=True).decode("utf-8"),
    )
    run(["launchctl", "bootstrap", service_domain(), str(paths["plist"])])
    return {
        "installed": True,
        "loaded": service_is_loaded(),
        "interval_seconds": interval,
        **{key: str(value) for key, value in paths.items()},
    }


def service_uninstall() -> dict[str, object]:
    if sys.platform != "darwin":
        raise PceError("the automatic service currently supports macOS launchd only")
    paths = service_paths()
    was_loaded = service_is_loaded()
    if was_loaded:
        run(["launchctl", "bootout", service_domain(), str(paths["plist"])])
    if paths["plist"].exists():
        paths["plist"].unlink()
        fsync_parent(paths["plist"])
    return {
        "installed": False,
        "was_loaded": was_loaded,
        "plist": str(paths["plist"]),
    }


def service_status() -> dict[str, object]:
    paths = service_paths()
    status = read_json(paths["status"], None)
    return {
        "installed": paths["plist"].exists(),
        "loaded": service_is_loaded() if sys.platform == "darwin" else False,
        "last_run": status,
        **{key: str(value) for key, value in paths.items()},
    }


@dataclass(frozen=True)
class InitLegacyCandidate:
    path: str
    action: str
    target: str
    _source_fingerprint: str
    _destination: str
    _destination_fingerprint: str
    _target_fingerprint: str


@dataclass(frozen=True)
class InitCheckoutCandidate:
    repository: str
    origin: str
    key: str
    namespace: str
    registered: bool
    registration_action: str
    config_path: str
    config_action: str
    exclude_path: str
    exclude_action: str
    missing_excludes: tuple[str, ...]
    local_root: str
    local_root_action: str
    legacy: tuple[InitLegacyCandidate, ...]
    _config_fingerprint: str
    _exclude_fingerprint: str
    _local_root_fingerprint: str


@dataclass(frozen=True)
class InitOriginGroup:
    origin: str
    key: str
    namespace: str
    checkouts: tuple[str, ...]


@dataclass(frozen=True)
class InitServiceCandidate:
    supported: bool
    enabled: bool
    action: str
    interval_seconds: int
    plist: str
    installed: bool
    loaded: bool
    plist_valid: bool
    label_matches: bool
    executable_matches: bool
    store_matches: bool
    interval_matches: bool
    arguments_match: bool
    plist_matches: bool


@dataclass(frozen=True)
class InitCandidate:
    mode: str
    store: str
    store_exists: bool
    store_config: str
    store_config_action: str
    _store_config_fingerprint: str
    registered_checkouts: tuple[str, ...]
    checkouts: tuple[InitCheckoutCandidate, ...]
    origin_groups: tuple[InitOriginGroup, ...]
    central_commit: bool
    service: InitServiceCandidate

    def result(self) -> dict[str, object]:
        def without_private(value: object) -> object:
            if isinstance(value, dict):
                return {
                    key: without_private(item)
                    for key, item in value.items()
                    if not str(key).startswith("_")
                }
            if isinstance(value, list):
                return [without_private(item) for item in value]
            if isinstance(value, tuple):
                return tuple(without_private(item) for item in value)
            return value

        result = without_private(asdict(self))
        assert isinstance(result, dict)
        return result


def discover_registered_checkouts() -> tuple[str, ...]:
    """Read checkout registrations from an existing store without creating it."""

    projects = store_root() / "projects"
    if not projects.is_dir():
        return ()
    registered: set[str] = set()
    for metadata_path in sorted(projects.glob("*/metadata.json")):
        metadata = read_json(metadata_path, {})
        if not isinstance(metadata, dict):
            raise PceError(f"metadata is not an object: {metadata_path}")
        for raw in metadata.get("checkouts", []):
            if isinstance(raw, str):
                registered.add(str(Path(raw).expanduser().resolve()))
    return tuple(sorted(registered))


def _registered_for_namespace(namespace_path: Path) -> set[str]:
    metadata_path = namespace_path / "metadata.json"
    metadata = read_json(metadata_path, {})
    if not isinstance(metadata, dict):
        raise PceError(f"metadata is not an object: {metadata_path}")
    return {
        str(Path(raw).expanduser().resolve())
        for raw in metadata.get("checkouts", [])
        if isinstance(raw, str)
    }


def _config_needs_update(repo: Path) -> bool:
    path = repo / ".compound-engineering" / "config.local.yaml"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    return text != render_local_config(text)


def discover_legacy_candidates(
    repo: Path,
    namespace_path: Path,
    planned_hashes: dict[Path, str] | None = None,
) -> tuple[InitLegacyCandidate, ...]:
    """Describe the exact imports setup would attempt, without copying them."""

    return tuple(
        InitLegacyCandidate(
            item.label,
            "import" if item.outcome == "imported" else item.outcome,
            str(item.target),
            item.source_fingerprint,
            str(item.destination),
            item.destination_fingerprint,
            item.target_fingerprint,
        )
        for item in plan_legacy_imports(repo, namespace_path, planned_hashes)
    )


def _discover_checkout_candidate_resolved(
    repo: Path,
    planned_hashes: dict[Path, str] | None = None,
) -> InitCheckoutCandidate:
    namespace_path, metadata = namespace(repo)
    registered = str(repo) in _registered_for_namespace(namespace_path)
    exclude_path = git_exclude_path(repo)
    exclude_text = (
        exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
    )
    missing_excludes = missing_exclude_entries(exclude_text)
    config_path = repo / ".compound-engineering" / "config.local.yaml"
    local_root = repo / LOCAL_ROOT_NAME
    return InitCheckoutCandidate(
        repository=str(repo),
        origin=str(metadata["canonical_origin"]),
        key=str(metadata["key"]),
        namespace=str(namespace_path),
        registered=registered,
        registration_action="noop" if registered else "register",
        config_path=str(config_path),
        config_action="update" if _config_needs_update(repo) else "noop",
        exclude_path=str(exclude_path),
        exclude_action="update" if missing_excludes else "noop",
        missing_excludes=missing_excludes,
        local_root=str(local_root),
        local_root_action="noop" if local_root.is_dir() else "create",
        legacy=discover_legacy_candidates(repo, namespace_path, planned_hashes),
        _config_fingerprint=_path_fingerprint(config_path),
        _exclude_fingerprint=_path_fingerprint(exclude_path),
        _local_root_fingerprint=_path_fingerprint(local_root),
    )


def discover_service_candidate(
    *, enabled: bool, interval: int
) -> InitServiceCandidate:
    if interval < 10:
        raise PceError("service interval must be at least 10 seconds")
    paths = service_paths()
    supported = sys.platform == "darwin"
    installed = paths["plist"].exists()
    loaded = service_is_loaded() if supported else False
    desired = desired_service_plist(interval)
    current: object = None
    valid = False
    if installed:
        try:
            with paths["plist"].open("rb") as handle:
                current = plistlib.load(handle)
            valid = isinstance(current, dict)
        except (OSError, plistlib.InvalidFileException):
            current = None
    current_map = current if isinstance(current, dict) else {}
    desired_args = desired["ProgramArguments"]
    current_args = current_map.get("ProgramArguments")
    desired_environment = desired["EnvironmentVariables"]
    current_environment = current_map.get("EnvironmentVariables")
    label_matches = current_map.get("Label") == desired["Label"]
    executable_matches = (
        isinstance(current_args, list)
        and current_args[:2] == desired_args[:2]  # type: ignore[index]
    )
    store_matches = (
        current_map.get("WorkingDirectory") == desired["WorkingDirectory"]
        and isinstance(current_environment, dict)
        and current_environment.get("PERSONAL_COMPOUND_HOME")
        == desired_environment["PERSONAL_COMPOUND_HOME"]  # type: ignore[index]
    )
    interval_matches = current_map.get("StartInterval") == interval
    arguments_match = current_args == desired_args
    plist_matches = current == desired
    if not supported:
        action = "unavailable" if enabled else "noop"
    elif not enabled:
        action = "noop"
    elif not installed:
        action = "install"
    elif not plist_matches:
        action = "update"
    elif not loaded:
        action = "reload"
    else:
        action = "noop"
    return InitServiceCandidate(
        supported=supported,
        enabled=enabled and supported,
        action=action,
        interval_seconds=interval,
        plist=str(paths["plist"]),
        installed=installed,
        loaded=loaded,
        plist_valid=valid,
        label_matches=label_matches,
        executable_matches=executable_matches,
        store_matches=store_matches,
        interval_matches=interval_matches,
        arguments_match=arguments_match,
        plist_matches=plist_matches,
    )


def discover_init_candidate(
    repositories: Iterable[Path],
    *,
    store: Path | None = None,
    mode: str,
    central_commit: bool,
    service_enabled: bool,
    service_interval: int,
) -> InitCandidate:
    """Construct the complete reviewed init model using read-only operations."""

    if mode not in {"quickstart", "advanced"}:
        raise PceError(f"unknown init mode: {mode}")
    selected = (store or store_root()).expanduser().resolve()
    validate_store_path(selected)
    with selected_store(selected):
        unique: dict[str, Path] = {}
        for raw in repositories:
            repo = resolve_repo(str(raw))
            unique.setdefault(str(repo), repo)
        if not unique:
            raise PceError("select at least one Git checkout")
        if str(selected) in unique:
            raise PceError("knowledge store must not also be a product checkout")
        planned_hashes: dict[Path, str] = {}
        checkouts = tuple(
            _discover_checkout_candidate_resolved(repo, planned_hashes)
            for repo in unique.values()
        )
        grouped: dict[str, list[InitCheckoutCandidate]] = {}
        for checkout in checkouts:
            grouped.setdefault(checkout.key, []).append(checkout)
        origin_groups = tuple(
            InitOriginGroup(
                origin=items[0].origin,
                key=key,
                namespace=items[0].namespace,
                checkouts=tuple(item.repository for item in items),
            )
            for key, items in grouped.items()
        )
        configured = configured_store_root()
        return InitCandidate(
            mode=mode,
            store=str(selected),
            store_exists=selected.is_dir(),
            store_config=str(user_config_path()),
            store_config_action=(
                "noop" if configured == selected else "update"
            ),
            _store_config_fingerprint=_path_fingerprint(user_config_path()),
            registered_checkouts=discover_registered_checkouts(),
            checkouts=checkouts,
            origin_groups=origin_groups,
            central_commit=central_commit,
            service=discover_service_candidate(
                enabled=service_enabled,
                interval=service_interval,
            ),
        )


def _additional_checkout_paths(raw: str) -> list[Path]:
    return [
        Path(value.strip()).expanduser()
        for value in raw.replace("\n", ",").split(",")
        if value.strip()
    ]


def _checkout_prompt_options(paths: Iterable[Path]) -> list[PromptOption]:
    options: list[PromptOption] = []
    seen: set[str] = set()
    for raw in paths:
        path = raw.expanduser().resolve()
        label = str(path)
        if label in seen:
            continue
        seen.add(label)
        valid = run(
            ["git", "-C", label, "rev-parse", "--show-toplevel"],
            check=False,
        ).returncode == 0
        options.append(
            PromptOption(
                label,
                label,
                "Git checkout" if valid else "not currently available",
                disabled=not valid,
            )
        )
    return options


def collect_init_candidate(
    prompter: Prompter,
    requested_store: Path | None = None,
) -> InitCandidate:
    """Collect QuickStart or Advanced choices, then discover one shared model."""

    environment_store = os.environ.get(STORE_ENV)
    if environment_store:
        chosen_store = Path(environment_store).expanduser().resolve()
    elif requested_store is not None:
        chosen_store = requested_store.expanduser().resolve()
    else:
        suggested_store = store_root()
        raw_store = prompter.text(
            "Knowledge store path",
            str(suggested_store),
        )
        chosen_store = Path(raw_store or str(suggested_store)).expanduser().resolve()
    validate_store_path(chosen_store)

    with selected_store(chosen_store):
        current = resolve_repo(None)
        mode = prompter.select(
            "Choose how to initialize Personal Compound Engineering",
            [
                PromptOption(
                    "quickstart",
                    "QuickStart",
                    "current checkout, central commits, and recommended automation",
                ),
                PromptOption(
                    "advanced",
                    "Advanced",
                    "choose checkouts, commits, automation, and interval",
                ),
            ],
            initial="quickstart",
        )
        extra = _additional_checkout_paths(
            prompter.text(
                "Additional checkout paths, separated by commas (optional)",
                "",
            )
        )
        if mode == "quickstart":
            repositories = [current, *extra]
            central_commit = True
            service_enabled = (
                prompter.confirm(
                    "Enable the recommended automatic synchronization service",
                    True,
                )
                if sys.platform == "darwin"
                else False
            )
            service_interval = SERVICE_INTERVAL
        else:
            registered = [Path(raw) for raw in discover_registered_checkouts()]
            options = _checkout_prompt_options([current, *registered, *extra])
            selected = prompter.multiselect(
                "Select checkouts to configure",
                options,
                initial=[str(current)],
            )
            repositories = [Path(raw) for raw in selected]
            central_commit = prompter.confirm(
                "Commit initialized knowledge to the central store",
                True,
            )
            service_enabled = (
                prompter.confirm("Enable automatic synchronization", True)
                if sys.platform == "darwin"
                else False
            )
            service_interval = SERVICE_INTERVAL
            if service_enabled:
                raw_interval = prompter.text(
                    "Automatic synchronization interval in seconds",
                    str(SERVICE_INTERVAL),
                )
                try:
                    service_interval = int(raw_interval)
                except ValueError as exc:
                    raise PceError("service interval must be a whole number") from exc
    return discover_init_candidate(
        repositories,
        store=chosen_store,
        mode=mode,
        central_commit=central_commit,
        service_enabled=service_enabled,
        service_interval=service_interval,
    )


def review_init_candidate(
    candidate: InitCandidate,
    *,
    prompter: Prompter,
    presenter: Presenter,
) -> bool:
    """Render the immutable review and obtain explicit publication consent."""

    present_command_result("init review", candidate.result(), presenter)
    return prompter.confirm("Apply this initialization plan", False)


def _rediscover_init_candidate(candidate: InitCandidate) -> InitCandidate:
    return discover_init_candidate(
        [Path(checkout.repository) for checkout in candidate.checkouts],
        store=Path(candidate.store),
        mode=candidate.mode,
        central_commit=candidate.central_commit,
        service_enabled=candidate.service.enabled,
        service_interval=candidate.service.interval_seconds,
    )


def _candidate_matches(
    reviewed: InitCandidate,
    current: InitCandidate,
    *,
    ignore_store_creation: bool = False,
) -> bool:
    if ignore_store_creation:
        current = replace(current, store_exists=reviewed.store_exists)
    return current == reviewed


def _init_retry_command(phase: str, repository: str | None = None, interval: int = 0) -> str:
    if phase in {"setup", "doctor"} and repository is not None:
        return render_shell_command(["pce", phase, "--repo", repository])
    if phase == "service":
        return render_shell_command(
            ["pce", "service", "install", "--interval", str(interval)]
        )
    return "pce init"


def _setup_changed(
    reviewed: InitCheckoutCandidate,
    result: dict[str, object],
) -> bool:
    planned_changes = (
        reviewed.registration_action != "noop"
        or reviewed.config_action != "noop"
        or reviewed.exclude_action != "noop"
        or reviewed.local_root_action != "noop"
        or any(item.action in {"import", "conflict"} for item in reviewed.legacy)
    )
    imported = result.get("import")
    import_changes = isinstance(imported, dict) and bool(
        imported.get("imported") or imported.get("conflict")
    )
    hydrated = result.get("hydrate")
    hydrate_changes = isinstance(hydrated, dict) and bool(hydrated.get("actions"))
    return planned_changes or import_changes or hydrate_changes


def _verify_checkout_inputs(reviewed: InitCheckoutCandidate) -> None:
    """Reject changes to reviewed local inputs immediately before mutation."""

    checks = (
        (Path(reviewed.config_path), reviewed._config_fingerprint),
        (Path(reviewed.exclude_path), reviewed._exclude_fingerprint),
        (Path(reviewed.local_root), reviewed._local_root_fingerprint),
    )
    for path, expected in checks:
        if _path_fingerprint(path) != expected:
            raise PceError(f"reviewed initialization input changed: {path}")
    repository = Path(reviewed.repository)
    planned_fingerprints: dict[Path, str] = {}
    for legacy in reviewed.legacy:
        source = repository / legacy.path
        if _path_fingerprint(source) != legacy._source_fingerprint:
            raise PceError(f"reviewed legacy source changed: {source}")
        destination = Path(legacy._destination)
        destination_fingerprint = (
            planned_fingerprints[destination]
            if destination in planned_fingerprints
            else _path_fingerprint(destination)
        )
        if destination_fingerprint != legacy._destination_fingerprint:
            raise PceError(f"reviewed legacy destination changed: {destination}")
        target = Path(legacy.target)
        target_fingerprint = (
            planned_fingerprints[target]
            if target in planned_fingerprints
            else _path_fingerprint(target)
        )
        if target_fingerprint != legacy._target_fingerprint:
            raise PceError(f"reviewed legacy target changed: {target}")
        if legacy.action == "import":
            planned_fingerprints[destination] = legacy._source_fingerprint
        elif legacy.action == "conflict":
            planned_fingerprints[target] = legacy._source_fingerprint


def _init_doctor_blockers(diagnosis: dict[str, object]) -> list[str]:
    blockers: list[str] = []
    if not diagnosis.get("namespace_exists"):
        blockers.append("namespace is missing")
    if not diagnosis.get("docs_root_valid"):
        blockers.append("docs_root is not configured")
    missing = diagnosis.get("missing_excludes")
    if isinstance(missing, list) and missing:
        blockers.append("Git exclude entries are missing")
    return blockers


def _unprocessed_checkout_phases(
    checkouts: Iterable[InitCheckoutCandidate],
) -> list[dict[str, object]]:
    phases: list[dict[str, object]] = []
    for checkout in checkouts:
        for phase in ("setup", "doctor"):
            phases.append(
                {
                    "phase": phase,
                    "repository": checkout.repository,
                    "status": "unprocessed",
                    "retry_command": _init_retry_command(
                        phase,
                        checkout.repository,
                    ),
                }
            )
    return phases


def _unprocessed_phases(
    phase: str,
    checkouts: Iterable[InitCheckoutCandidate],
) -> list[dict[str, object]]:
    return [
        {
            "phase": phase,
            "repository": checkout.repository,
            "status": "unprocessed",
            "retry_command": _init_retry_command(phase, checkout.repository),
        }
        for checkout in checkouts
    ]


def _init_result(
    candidate: InitCandidate,
    phases: list[dict[str, object]],
    *,
    ok: bool,
) -> dict[str, object]:
    retry_commands: list[str] = []
    if any(
        phase.get("phase") in {"validation", "lock"}
        and phase.get("status") == "failed"
        for phase in phases
    ):
        retry_commands.append("pce init")
    else:
        for phase in phases:
            retry = phase.get("retry_command")
            if isinstance(retry, str) and retry not in retry_commands:
                retry_commands.append(retry)
    return {
        "ok": ok,
        "checkouts": [checkout.repository for checkout in candidate.checkouts],
        "phases": phases,
        "retry_commands": retry_commands,
    }


def _append_skipped_tail(
    candidate: InitCandidate,
    phases: list[dict[str, object]],
    reason: str,
) -> None:
    phases.extend(
        [
            {
                "phase": "central_commit",
                "status": "skipped",
                "reason": reason,
                "retry_command": "pce init" if candidate.central_commit else None,
            },
            {
                "phase": "service",
                "status": "skipped",
                "reason": reason,
                "retry_command": (
                    _init_retry_command(
                        "service",
                        interval=candidate.service.interval_seconds,
                    )
                    if candidate.service.enabled
                    else None
                ),
            },
        ]
    )


def publish_init_candidate(candidate: InitCandidate) -> dict[str, object]:
    """Publish one reviewed init model through existing mutation primitives."""

    with selected_store(Path(candidate.store)):
        return _publish_init_candidate_selected(candidate)


def _publish_init_candidate_selected(candidate: InitCandidate) -> dict[str, object]:
    phases: list[dict[str, object]] = []
    try:
        current = _rediscover_init_candidate(candidate)
    except (PceError, OSError) as exc:
        current = None
        drift_error = sanitize_terminal_text(exc)
    else:
        drift_error = "reviewed initialization inputs changed; run pce init again"
    if current is None or not _candidate_matches(candidate, current):
        phases.append(
            {
                "phase": "validation",
                "status": "failed",
                "error": drift_error,
                "retry_command": "pce init",
            }
        )
        phases.extend(_unprocessed_checkout_phases(candidate.checkouts))
        _append_skipped_tail(candidate, phases, "candidate validation failed")
        return _init_result(candidate, phases, ok=False)

    try:
        with store_lock():
            locked_candidate = _rediscover_init_candidate(candidate)
            if not _candidate_matches(
                candidate,
                locked_candidate,
                ignore_store_creation=True,
            ):
                phases.append(
                    {
                        "phase": "validation",
                        "status": "failed",
                        "error": (
                            "reviewed initialization inputs changed while acquiring "
                            "the store lock; run pce init again"
                        ),
                        "retry_command": "pce init",
                    }
                )
                phases.extend(_unprocessed_checkout_phases(candidate.checkouts))
                _append_skipped_tail(candidate, phases, "candidate validation failed")
                return _init_result(candidate, phases, ok=False)

            persist_store_root(Path(candidate.store))
            setup_phases: list[dict[str, object]] = []
            for index, checkout in enumerate(candidate.checkouts):
                repository = Path(checkout.repository)
                setup_retry = _init_retry_command("setup", checkout.repository)
                try:
                    _verify_checkout_inputs(checkout)
                    setup = setup_repo(repository)
                except (PceError, OSError) as exc:
                    setup_phases.append(
                        {
                            "phase": "setup",
                            "repository": checkout.repository,
                            "status": "failed",
                            "error": sanitize_terminal_text(exc),
                            "retry_command": setup_retry,
                        }
                    )
                    phases.extend(setup_phases)
                    phases.extend(
                        _unprocessed_phases(
                            "setup",
                            candidate.checkouts[index + 1 :],
                        )
                    )
                    phases.extend(_unprocessed_phases("doctor", candidate.checkouts))
                    _append_skipped_tail(candidate, phases, "repository setup failed")
                    return _init_result(candidate, phases, ok=False)
                setup_phases.append(
                    {
                        "phase": "setup",
                        "repository": checkout.repository,
                        "status": "changed" if _setup_changed(checkout, setup) else "noop",
                        "result": setup,
                    }
                )

            phases.extend(setup_phases)
            for index, (checkout, phase) in enumerate(
                zip(candidate.checkouts, setup_phases, strict=True)
            ):
                repository = Path(checkout.repository)
                try:
                    hydrated = reconcile(repository, "hydrate")
                except (PceError, OSError) as exc:
                    phase.update(
                        {
                            "status": "failed",
                            "error": sanitize_terminal_text(exc),
                            "retry_command": _init_retry_command(
                                "setup",
                                checkout.repository,
                            ),
                        }
                    )
                    phases.extend(_unprocessed_phases("doctor", candidate.checkouts))
                    _append_skipped_tail(candidate, phases, "repository setup failed")
                    return _init_result(candidate, phases, ok=False)
                result = phase.get("result")
                if isinstance(result, dict):
                    result["hydrate"] = hydrated
                if hydrated.get("actions"):
                    phase["status"] = "changed"

            for index, checkout in enumerate(candidate.checkouts):
                repository = Path(checkout.repository)
                doctor_retry = _init_retry_command("doctor", checkout.repository)
                blockers: list[str] = []
                try:
                    diagnosis = doctor(repository)
                except (PceError, OSError) as exc:
                    diagnosis = None
                    doctor_error = sanitize_terminal_text(exc)
                else:
                    blockers = _init_doctor_blockers(diagnosis)
                    doctor_error = "; ".join(blockers)
                if diagnosis is None or blockers:
                    phase: dict[str, object] = {
                        "phase": "doctor",
                        "repository": checkout.repository,
                        "status": "failed",
                        "error": doctor_error,
                        "retry_command": doctor_retry,
                    }
                    if diagnosis is not None:
                        phase["result"] = diagnosis
                    phases.append(phase)
                    phases.extend(
                        _unprocessed_phases(
                            "doctor",
                            candidate.checkouts[index + 1 :],
                        )
                    )
                    _append_skipped_tail(candidate, phases, "repository doctor failed")
                    return _init_result(candidate, phases, ok=False)
                phase = {
                    "phase": "doctor",
                    "repository": checkout.repository,
                    "status": "noop",
                    "result": diagnosis,
                }
                warnings = diagnosis.get("visible_personal_artifacts")
                if isinstance(warnings, list) and warnings:
                    phase["warnings"] = warnings
                phases.append(phase)

            if candidate.central_commit:
                try:
                    commit = commit_store(
                        "chore: initialize personal compound knowledge",
                        [Path(checkout.namespace) for checkout in candidate.checkouts],
                    )
                except (PceError, OSError) as exc:
                    phases.append(
                        {
                            "phase": "central_commit",
                            "status": "failed",
                            "error": sanitize_terminal_text(exc),
                            "retry_command": "pce init",
                        }
                    )
                    phases.append(
                        {
                            "phase": "service",
                            "status": "skipped",
                            "reason": "central commit failed",
                            "retry_command": (
                                _init_retry_command(
                                    "service",
                                    interval=candidate.service.interval_seconds,
                                )
                                if candidate.service.enabled
                                else None
                            ),
                        }
                    )
                    return _init_result(candidate, phases, ok=False)
                phases.append(
                    {
                        "phase": "central_commit",
                        "status": "changed" if commit.get("committed") else "noop",
                        "result": commit,
                    }
                )
            else:
                phases.append(
                    {
                        "phase": "central_commit",
                        "status": "skipped",
                        "reason": "not selected",
                    }
                )

            if not candidate.service.enabled:
                phases.append(
                    {
                        "phase": "service",
                        "status": "skipped",
                        "reason": "not selected",
                    }
                )
            else:
                retry = _init_retry_command(
                    "service",
                    interval=candidate.service.interval_seconds,
                )
                try:
                    effective_service = discover_service_candidate(
                        enabled=True,
                        interval=candidate.service.interval_seconds,
                    )
                    if effective_service.action == "noop":
                        phases.append(
                            {
                                "phase": "service",
                                "status": "noop",
                                "result": asdict(effective_service),
                            }
                        )
                        return _init_result(candidate, phases, ok=True)
                    service = service_install(candidate.service.interval_seconds)
                except (PceError, OSError) as exc:
                    phases.append(
                        {
                            "phase": "service",
                            "status": "failed",
                            "error": sanitize_terminal_text(exc),
                            "retry_command": retry,
                        }
                    )
                    return _init_result(candidate, phases, ok=False)
                phases.append(
                    {
                        "phase": "service",
                        "status": "changed",
                        "action": effective_service.action,
                        "result": service,
                    }
                )
    except (PceError, OSError) as exc:
        phases.append(
            {
                "phase": "lock",
                "status": "failed",
                "error": sanitize_terminal_text(exc),
                "retry_command": "pce init",
            }
        )
        phases.extend(_unprocessed_checkout_phases(candidate.checkouts))
        _append_skipped_tail(candidate, phases, "store lock failed")
        return _init_result(candidate, phases, ok=False)

    return _init_result(candidate, phases, ok=True)


def setup_repo(repo: Path) -> dict[str, object]:
    ensure_store()
    ns, meta = namespace(repo)
    ns.mkdir(parents=True, exist_ok=True)
    (ns / "artifacts").mkdir(exist_ok=True)
    update_metadata(repo, ns, meta)
    imported = import_legacy(repo, ns)
    config = update_local_config(repo)
    exclude = update_git_exclude(repo)
    (repo / LOCAL_ROOT_NAME).mkdir(exist_ok=True)
    hydrated = reconcile(repo, "hydrate")
    return {
        "repository": str(repo),
        "origin": meta["canonical_origin"],
        "key": meta["key"],
        "namespace": str(ns),
        "config": str(config),
        "exclude": str(exclude),
        "import": imported,
        "hydrate": hydrated,
    }


def repository_status(repo: Path) -> dict[str, object]:
    ns, meta = namespace(repo)
    baseline = load_baseline(repo)
    local = manifest(local_files(repo))
    central = manifest(central_files(repo, ns))
    paths = sorted(set(baseline) | set(local) | set(central))
    changed_local: list[str] = []
    changed_central: list[str] = []
    conflicts: list[str] = []
    for rel in paths:
        old = baseline.get(rel)
        local_value = local.get(rel)
        central_value = central.get(rel)
        local_changed = local_value != old
        central_changed = central_value != old
        if local_changed:
            changed_local.append(rel)
        if central_changed:
            changed_central.append(rel)
        if local_changed and central_changed and local_value != central_value:
            conflicts.append(rel)
    return {
        "repository": str(repo),
        "key": meta["key"],
        "namespace": str(ns),
        "local_changes": [display_key(rel) for rel in changed_local],
        "central_changes": [display_key(rel) for rel in changed_central],
        "conflicts": [display_key(rel) for rel in conflicts],
        "in_sync": local == central,
    }


def restore_local(repo: Path, rel: str) -> dict[str, object]:
    ns, meta = namespace(repo)
    key = CONCEPTS_KEY if rel == "CONCEPTS.md" else f"{ARTIFACT_KEY_PREFIX}{rel}"
    central = central_files(repo, ns)
    source = central.get(key)
    if source is None:
        raise PceError(f"central artifact does not exist: {rel}")
    target = target_path(repo, ns, key, "local")
    atomic_copy(source, target)
    return {
        "repository": str(repo),
        "key": meta["key"],
        "path": rel,
        "restored_from": str(source),
        "restored_to": str(target),
    }


def doctor(repo: Path) -> dict[str, object]:
    ns, meta = namespace(repo)
    config_path = repo / ".compound-engineering" / "config.local.yaml"
    config = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    docs_root_lines = [
        line for line in config.splitlines() if re.match(r"^docs_root\s*:", line)
    ]
    exclude_path = git_exclude_path(repo)
    exclude = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
    missing_excludes = list(missing_exclude_entries(exclude))
    visible = git(repo, "status", "--short", "--untracked-files=all")
    visible_ce = [
        line
        for line in visible.splitlines()
        if any(
            marker in line
            for marker in (
                LOCAL_ROOT_NAME,
                ".compound-engineering/config.local.yaml",
                "CONCEPTS.md",
            )
        )
    ]
    namespace_exists = ns.exists()
    docs_root_valid = docs_root_lines == [f"docs_root: {LOCAL_ROOT_NAME}"]
    return {
        "repository": str(repo),
        "key": meta["key"],
        "namespace_exists": namespace_exists,
        "docs_root_lines": docs_root_lines,
        "docs_root_valid": docs_root_valid,
        "missing_excludes": missing_excludes,
        "visible_personal_artifacts": visible_ce,
        "ok": (
            namespace_exists
            and docs_root_valid
            and not missing_excludes
            and not visible_ce
        ),
    }


def default_upstream_ref(repo: Path) -> str:
    symbolic = run(
        ["git", "-C", str(repo), "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"],
        check=False,
    )
    if symbolic.returncode == 0:
        return symbolic.stdout.strip().removeprefix("refs/remotes/")
    for name in ("origin/trunk", "origin/main", "origin/master", "origin/develop"):
        if (
            run(
                ["git", "-C", str(repo), "rev-parse", "--verify", name], check=False
            ).returncode
            == 0
        ):
            return name
    return "HEAD"


def project_state(ns: Path) -> tuple[Path, dict[str, object]]:
    path = ns / "state.json"
    data = read_json(path, {})
    if not isinstance(data, dict):
        raise PceError(f"project state is not an object: {path}")
    return path, data


def harvest(repo: Path, limit: int) -> dict[str, object]:
    if limit < 1:
        raise PceError("harvest limit must be at least 1")
    ns, meta = namespace(repo)
    state_path_value, state = project_state(ns)
    upstream = default_upstream_ref(repo)
    current = git(repo, "rev-parse", upstream)
    previous = state.get("last_harvested_revision")
    format_value = "%H%x1f%cs%x1f%an%x1f%s"
    if isinstance(previous, str) and previous:
        if (
            run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "merge-base",
                    "--is-ancestor",
                    previous,
                    current,
                ],
                check=False,
            ).returncode
            != 0
        ):
            raise PceError(
                f"harvest watermark {previous} is not an ancestor of {upstream}"
            )
        output = git(
            repo,
            "log",
            "--first-parent",
            "--reverse",
            f"--format={format_value}",
            f"{previous}..{current}",
        )
        all_lines = output.splitlines()
        selected_lines = all_lines[:limit]
        truncated = len(all_lines) > limit
    else:
        output = git(
            repo,
            "log",
            "--first-parent",
            f"--max-count={limit}",
            f"--format={format_value}",
            current,
        )
        selected_lines = output.splitlines()
        truncated = False
    commits = []
    for line in selected_lines:
        parts = line.split("\x1f", 3)
        if len(parts) == 4:
            commits.append(
                {
                    "revision": parts[0],
                    "date": parts[1],
                    "author": parts[2],
                    "subject": parts[3],
                }
            )
    safe_mark_revision = (
        str(commits[-1]["revision"]) if truncated and commits else current
    )
    return {
        "repository": str(repo),
        "key": meta["key"],
        "upstream": upstream,
        "current_revision": current,
        "last_harvested_revision": previous,
        "initial_baseline": not (isinstance(previous, str) and previous),
        "truncated": truncated,
        "safe_mark_revision": safe_mark_revision,
        "state_path": str(state_path_value),
        "commits": commits,
    }


def harvest_mark(
    repo: Path,
    revision: str,
    review_file: Path | None = None,
) -> dict[str, object]:
    ns, meta = namespace(repo)
    resolved = git(repo, "rev-parse", "--verify", f"{revision}^{{commit}}")
    upstream = default_upstream_ref(repo)
    current = git(repo, "rev-parse", upstream)
    path, state = project_state(ns)
    previous = state.get("last_harvested_revision")
    if (
        isinstance(previous, str)
        and previous
        and run(
            [
                "git",
                "-C",
                str(repo),
                "merge-base",
                "--is-ancestor",
                previous,
                resolved,
            ],
            check=False,
        ).returncode
        != 0
    ):
        raise PceError(
            f"harvest revision {resolved} does not advance watermark {previous}"
        )
    first_parent = set(git(repo, "rev-list", "--first-parent", current).splitlines())
    if resolved not in first_parent:
        raise PceError(
            f"harvest revision {resolved} is not on {upstream}'s first-parent history"
        )

    stored_review = None
    if review_file is not None:
        source = review_file.expanduser().resolve()
        if not source.is_file():
            raise PceError(f"harvest review file does not exist: {source}")
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        suffix = source.suffix if source.suffix else ".md"
        target = ns / "harvest-reviews" / f"{stamp}-{resolved[:12]}{suffix}"
        atomic_copy(source, target)
        stored_review = target.relative_to(ns).as_posix()

    state["last_harvested_revision"] = resolved
    state["last_harvested_at"] = now()
    state["last_harvested_checkout"] = str(repo)
    if stored_review is not None:
        state["last_harvest_review"] = stored_review
    write_json(path, state)
    return {
        "repository": str(repo),
        "key": meta["key"],
        "revision": resolved,
        "review": stored_review,
        "state_path": str(path),
    }


def inventory(repo: Path | None) -> dict[str, object]:
    root = ensure_store()
    project: dict[str, object] | None = None
    if repo is not None:
        ns, meta = namespace(repo)
        metadata = read_json(ns / "metadata.json", {})
        project_files = [
            path.relative_to(ns / "artifacts").as_posix()
            for path in iter_files(ns / "artifacts")
        ]
        project = {
            "key": meta["key"],
            "namespace": str(ns),
            "checkouts": (
                metadata.get("checkouts", []) if isinstance(metadata, dict) else []
            ),
            "artifacts": sorted(project_files),
        }
    return {
        "project": project,
        "library": sorted(
            path.relative_to(root / "library").as_posix()
            for path in iter_files(root / "library")
        ),
        "inbox": sorted(
            path.relative_to(root / "inbox").as_posix()
            for path in iter_files(root / "inbox")
        ),
    }


def search_store(repo: Path | None, query: list[str], limit: int) -> dict[str, object]:
    root = ensure_store()
    terms = [item.lower() for item in query if item.strip()]
    if not terms:
        raise PceError("search requires at least one term")
    candidates: list[tuple[int, str, str, str]] = []
    roots: list[tuple[str, Path]] = [("library", root / "library")]
    current_key = None
    if repo is not None:
        ns, meta = namespace(repo)
        current_key = str(meta["key"])
        roots.insert(0, ("project", ns / "artifacts" / "solutions"))
    for source, search_root in roots:
        for path in iter_files(search_root):
            if path.suffix.lower() not in {".md", ".html", ".txt"}:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            lowered = text.lower()
            score = sum(lowered.count(term) for term in terms)
            if score:
                snippet = " ".join(text.split())[:240]
                candidates.append((score, source, str(path), snippet))
    candidates.sort(key=lambda item: (-item[0], item[2]))
    return {
        "query": query,
        "repository_key": current_key,
        "results": [
            {"score": score, "source": source, "path": path, "snippet": snippet}
            for score, source, path, snippet in candidates[:limit]
        ],
    }


def print_result(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def present_result(
    command: str,
    value: dict[str, object],
    args: argparse.Namespace,
) -> None:
    """Render an unchanged domain result for this invocation's output contract."""

    mode = select_output_mode(
        output=sys.stdout,
        json_requested=bool(
            getattr(args, "global_json", False)
            or getattr(args, "local_json", False)
        ),
        plain_requested=bool(
            getattr(args, "global_plain", False)
            or getattr(args, "local_plain", False)
        ),
    )
    if mode == "json":
        print_result(value)
        return
    present_command_result(
        command,
        value,
        create_presenter(output=sys.stdout, mode=mode),
    )


def add_output_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json",
        dest="local_json",
        action="store_true",
        help="emit the legacy result as one undecorated JSON document",
    )
    parser.add_argument(
        "--plain",
        dest="local_plain",
        action="store_true",
        help="use line-oriented human output without color or Unicode decoration",
    )


def add_repo_arg(parser: argparse.ArgumentParser, *, multiple: bool = False) -> None:
    parser.add_argument(
        "--repo",
        action="append" if multiple else "store",
        required=multiple,
        help="Git checkout path; defaults to the current checkout when optional",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pce",
        description=(
            "Personal Compound Engineering keeps private plans and learnings "
            "synchronized across repositories that share an origin."
        ),
        epilog=(
            "Run `pce <command> --help` for command details. Piped output stays "
            "compatible with the original JSON interface."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {PCE_VERSION}",
    )
    parser.add_argument(
        "--json",
        dest="global_json",
        action="store_true",
        help="emit the command result as one undecorated JSON document",
    )
    parser.add_argument(
        "--plain",
        dest="global_plain",
        action="store_true",
        help="use line-oriented human output without terminal decoration",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    init_parser = sub.add_parser(
        "init",
        help="interactively review setup for one or more checkouts",
        description=(
            "Discover checkout, origin, local-file, central-store, legacy-import, "
            "and service changes, then review them before any setup writes."
        ),
        epilog=(
            "Agents and scripts should use `pce setup --repo <path>` instead of "
            "driving prompts."
        ),
    )
    init_parser.add_argument(
        "--store",
        type=Path,
        help=(
            "knowledge store to adopt or create after confirmation; "
            "PERSONAL_COMPOUND_HOME takes precedence"
        ),
    )
    add_output_args(init_parser)

    setup_parser = sub.add_parser(
        "setup",
        help="configure and hydrate one or more checkouts",
        description=(
            "Configure one or more checkouts to use an origin-keyed private "
            "knowledge namespace, import untracked legacy artifacts, and hydrate."
        ),
    )
    add_repo_arg(setup_parser, multiple=True)
    setup_parser.add_argument("--commit", action="store_true")
    add_output_args(setup_parser)

    descriptions = {
        "hydrate": "Hydrate local artifacts from the central origin-keyed namespace.",
        "sync": "Synchronize local and central artifacts in both directions.",
        "status": "Show local, central, and conflicting artifact changes.",
        "doctor": "Check repository configuration, excludes, and namespace health.",
    }
    for name in ("hydrate", "sync", "status", "doctor"):
        child = sub.add_parser(
            name,
            help=descriptions[name].rstrip(".").lower(),
            description=descriptions[name],
        )
        add_repo_arg(child)
        if name in {"hydrate", "sync"}:
            child.add_argument(
                "--allow-delete",
                action="store_true",
                help="propagate reviewed deletions and save recoverable copies",
            )
        if name == "sync":
            child.add_argument(
                "--commit",
                action="store_true",
                help="commit changed knowledge in the central store",
            )
        add_output_args(child)

    info_descriptions = {
        "repo-info": "Show the checkout's canonical origin and namespace paths.",
        "inventory": "List project artifacts, registered checkouts, library, and inbox.",
    }
    for name in ("repo-info", "inventory"):
        info_parser = sub.add_parser(
            name,
            help=info_descriptions[name].rstrip(".").lower(),
            description=info_descriptions[name],
        )
        add_repo_arg(info_parser)
        add_output_args(info_parser)

    restore_parser = sub.add_parser(
        "restore",
        help="restore one local artifact from the central namespace",
    )
    add_repo_arg(restore_parser)
    restore_parser.add_argument(
        "--path", required=True, help="artifact path relative to .ce-personal"
    )
    add_output_args(restore_parser)

    harvest_parser = sub.add_parser(
        "harvest",
        help="list upstream commits not yet reviewed for learnings",
        description=(
            "List first-parent upstream commits after the saved harvest watermark "
            "so changes by any contributor can be reviewed for durable learnings."
        ),
    )
    add_repo_arg(harvest_parser)
    harvest_parser.add_argument(
        "--limit", type=int, default=30, help="maximum commits to return (default: 30)"
    )
    add_output_args(harvest_parser)

    mark_parser = sub.add_parser(
        "harvest-mark",
        help="advance the reviewed-commit watermark",
        description=(
            "Record the last reviewed upstream revision and preserve the review "
            "artifact in the project's private namespace."
        ),
    )
    add_repo_arg(mark_parser)
    mark_parser.add_argument("--revision", required=True, help="reviewed Git revision")
    mark_parser.add_argument(
        "--review-file", required=True, help="path to the completed review artifact"
    )
    mark_parser.add_argument(
        "--commit", action="store_true", help="commit the new watermark centrally"
    )
    add_output_args(mark_parser)

    search_parser = sub.add_parser(
        "search",
        help="search project solutions and the shared library",
        description="Search textual personal knowledge by one or more terms.",
    )
    add_repo_arg(search_parser)
    search_parser.add_argument(
        "--limit", type=int, default=10, help="maximum matches (default: 10)"
    )
    search_parser.add_argument("terms", nargs="+", help="case-insensitive search terms")
    add_output_args(search_parser)

    sync_all_parser = sub.add_parser(
        "sync-all",
        help="safely reconcile every registered checkout",
    )
    sync_all_parser.add_argument("--commit", action="store_true")
    sync_all_parser.add_argument(
        "--quiet",
        action="store_true",
        help="suppress JSON output (used by the automatic service)",
    )
    sync_all_parser.add_argument(
        "--settle-seconds",
        type=int,
        default=0,
        help="skip artifact trees with newer writes (default: 0)",
    )
    sync_all_parser.add_argument(
        "--status-file",
        type=Path,
        help="atomically write the result to this JSON file",
    )
    add_output_args(sync_all_parser)

    service_parser = sub.add_parser(
        "service",
        help="manage the macOS automatic synchronization service",
        description=(
            "Install, inspect, or remove the macOS launchd automatic "
            "synchronization service."
        ),
    )
    service_sub = service_parser.add_subparsers(
        dest="service_command",
        metavar="ACTION",
    )
    service_parser.set_defaults(selected_help_parser=service_parser)
    install_parser = service_sub.add_parser(
        "install",
        help="install or replace the launchd service",
        description="Install the launchd service and start automatic synchronization.",
    )
    install_parser.add_argument(
        "--interval",
        type=int,
        default=SERVICE_INTERVAL,
        help=f"seconds between runs (default: {SERVICE_INTERVAL}, minimum: 10)",
    )
    add_output_args(install_parser)
    status_parser = service_sub.add_parser(
        "status",
        help="show installation and last-run state",
        description="Show service installation, load state, paths, and latest result.",
    )
    add_output_args(status_parser)
    uninstall_parser = service_sub.add_parser(
        "uninstall",
        help="stop and remove the launchd service",
        description="Stop the launchd service and remove its property-list file.",
    )
    add_output_args(uninstall_parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "service" and args.service_command is None:
        args.selected_help_parser.print_help()
        return 0
    try:
        if args.command == "init":
            json_requested = bool(args.global_json or args.local_json)
            if json_requested:
                print_result(
                    {
                        "error": "interactive_required",
                        "message": (
                            "pce init is interactive; use pce setup --repo <path> "
                            "for non-interactive setup"
                        ),
                    }
                )
                return 2
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                raise PceError(
                    "pce init requires an interactive terminal; use "
                    "pce setup --repo <path> for non-interactive setup"
                )
            mode = select_output_mode(
                output=sys.stdout,
                plain_requested=bool(args.global_plain or args.local_plain),
            )
            presenter = create_presenter(output=sys.stdout, mode=mode)
            prompter = create_prompter(
                input_stream=sys.stdin,
                output=sys.stdout,
                rich=mode == "rich",
            )
            presenter.intro("Personal Compound Engineering initialization")
            try:
                candidate = collect_init_candidate(prompter, args.store)
                confirmed = review_init_candidate(
                    candidate,
                    prompter=prompter,
                    presenter=presenter,
                )
            except PromptCancelled:
                presenter.outro("Initialization cancelled. No changes made.")
                return 0
            if not confirmed:
                presenter.outro("Initialization declined. No changes made.")
                return 0
            result = publish_init_candidate(candidate)
            present_command_result("init publication", result, presenter)
            if result["ok"]:
                presenter.outro("Personal Compound Engineering is ready.")
                return 0
            presenter.outro("Initialization needs attention; use the retry guidance above.")
            return 1

        if args.command == "service":
            if args.service_command == "install":
                result = service_install(args.interval)
            elif args.service_command == "uninstall":
                result = service_uninstall()
            else:
                result = service_status()
            present_result(f"service {args.service_command}", result, args)
            return 0

        exit_code = 0
        should_present = True
        with store_lock():
            if args.command == "setup":
                values = [setup_repo(resolve_repo(raw)) for raw in args.repo]
                result: dict[str, object] = {"checkouts": values}
                if args.commit:
                    result["central_commit"] = commit_store(
                        "chore: initialize personal compound knowledge",
                        [Path(str(item["namespace"])) for item in values],
                    )
            elif args.command == "sync-all":
                result = automatic_sync(
                    commit=args.commit,
                    settle_seconds=args.settle_seconds,
                )
                if args.status_file is not None:
                    write_json(args.status_file.expanduser().resolve(), result)
                should_present = not args.quiet
                exit_code = 0 if result["ok"] else 1
            else:
                repo = resolve_repo(args.repo)
                if args.command == "repo-info":
                    ns, meta = namespace(repo)
                    result = {**meta, "repository": str(repo), "namespace": str(ns)}
                elif args.command == "inventory":
                    result = inventory(repo)
                elif args.command == "restore":
                    result = restore_local(repo, args.path)
                elif args.command == "hydrate":
                    result = reconcile(repo, "hydrate", allow_delete=args.allow_delete)
                elif args.command == "sync":
                    result = reconcile(
                        repo,
                        "sync",
                        allow_delete=args.allow_delete,
                    )
                    if args.commit:
                        key = str(result["key"])
                        result["central_commit"] = commit_store(
                            f"chore({key}): sync compound knowledge",
                            [
                                Path(str(result["namespace"]))
                                if "namespace" in result
                                else namespace(repo)[0],
                                store_root() / "recovery" / key,
                            ],
                        )
                elif args.command == "status":
                    result = repository_status(repo)
                elif args.command == "doctor":
                    result = doctor(repo)
                    exit_code = 0 if result["ok"] else 1
                elif args.command == "harvest":
                    result = harvest(repo, args.limit)
                elif args.command == "harvest-mark":
                    result = harvest_mark(
                        repo,
                        args.revision,
                        Path(args.review_file),
                    )
                    if args.commit:
                        result["central_commit"] = commit_store(
                            f"chore({result['key']}): advance harvest watermark",
                            [namespace(repo)[0]],
                        )
                elif args.command == "search":
                    result = search_store(repo, args.terms, args.limit)
        if should_present:
            present_result(args.command, result, args)
        return exit_code
    except PceError as exc:
        print(f"error: {sanitize_terminal_text(exc)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

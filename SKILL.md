---
name: personal-compound
description: Manage a private, origin-keyed Compound Engineering knowledge store across duplicate clones and repositories. Use when setting up personal CE storage, hydrating or syncing plans and solutions, capturing a learning privately, refreshing personal learnings against a source repository, inspecting knowledge status, or harvesting merged work by other contributors.
---

# Personal Compound

Keep Compound Engineering artifacts in a private Git repository while giving
each product checkout a repo-local, ignored materialization that existing CE
skills can read and write.

## Deterministic storage tool

Use `scripts/pce.py` for all namespace, config, exclusion, hydration, sync, and
harvest-state operations. It derives identity from the normalized `origin` URL,
so clean duplicate clones share a namespace.

Use the store persisted by the user's confirmed `pce init` review. Do not
assume a personal machine path. `PERSONAL_COMPOUND_HOME` is an explicit
per-command override; use it when the user requests a different store or when
an agent must target a store before user configuration exists.

## Setup

For human onboarding in an interactive terminal, recommend:

```bash
pce init
```

When adopting an existing private store, run this from the product checkout:

```bash
pce init --store <existing-private-store>
```

Never run guided initialization from the PCE source checkout. The current Git
checkout is treated as a product repository candidate. The selected knowledge
store must remain separate from PCE source and from every selected product
checkout.

Treat `init` as a guided composition of the primitives below. Do not run it as
an agent or script, do not answer its prompts for the user, and do not parse its
human output. Initialization is additive: it never resets the store, changes
an origin, deletes knowledge, or uninstalls an existing service.

Adopting an existing Git store does not move, delete, or reinitialize it.
Unrelated files and Git history remain untouched, and hydration targets only
the product checkouts in the confirmed review. PCE refuses a non-empty non-Git
directory instead of guessing that it is safe to convert.

For agent-controlled setup, run the non-interactive primitive and request its
machine contract explicitly:

```bash
pce setup --repo <checkout> --json
```

Setup must:

- preserve existing local CE config and set exactly one active
  `docs_root: .ce-personal`;
- add idempotent entries to the repository's local Git exclude;
- import untracked legacy `docs/{plans,solutions}` and
  `.claude/docs/{plans,solutions}` artifacts;
- hydrate the origin-keyed namespace;
- record every checkout using that origin.

Never edit a tracked `.gitignore` for personal storage. Never change `origin`.

## Capture

1. Run `setup`, then `hydrate`.
2. Invoke `ce-compound` for one solved, verified learning.
3. Do not accept an AGENTS.md/CLAUDE.md discoverability edit unless the user
   explicitly asks to change the team repository.
4. Run:

```bash
pce sync --repo <checkout> --commit --json
```

The sync commits only in the private knowledge repository.

## Refresh

1. Require a repository and prefer the narrowest useful scope.
2. Run `setup`, then `hydrate`.
3. Invoke `ce-compound-refresh <scope>` interactively with these binding
   constraints:
   - personal artifacts only;
   - do not create a product-repository branch;
   - do not stage, commit, push, or open a product-repository PR;
   - stop before its Phase 5 Git workflow;
   - do not edit tracked instruction files.
4. Inspect the refresh report and local artifact changes.
5. Run `sync --commit`.

Never use headless refresh directly in a product repository because upstream
headless defaults may create a branch, commit, and attempt a PR.

## Plan and work

Run `hydrate` before `ce-plan` or `ce-work`. Their normal `<root>/solutions/`
research then reads `.ce-personal/solutions/` automatically.

Use `search` to add advisory cross-project context from the curated library:

```bash
pce search --repo <checkout> --json <terms>
```

Repo-specific results are stronger evidence than library results. Re-ground
portable findings against the current checkout.

Use `inventory --repo <checkout>` when useful terms are not yet known. It lists
the current project's artifacts, registered duplicate clones, library topics,
and pending inbox items.

## Harvest work by others

Before setup or hydration, confirm that the calling environment exposes both
`compound-engineering:ce-compound` and
`compound-engineering:ce-compound-refresh`. Then read
`references/harvest-workflow.md` completely and follow its protocol.

Binding invariants:

- PCE selects commits and records progress deterministically; the calling agent
  performs the semantic review.
- Every's CE skills own learning schema and paths. Never invent a PCE learning
  template or treat a harvest audit as a solution.
- PCE-owned reviews stay in `harvest-reviews/`, separate from CE learnings.
- Fail closed at the first incomplete commit and never advance an initial
  baseline partially.
- A pending harvest transaction permits only the reference's same-revision
  retry; it never permits selection of a new batch.

## Conflict behavior

Hydration and sync use per-file baseline hashes. They merge changes made in
different clones when files do not conflict and stop before overwriting when
both the central and local copy changed the same path differently.

The CLI serializes operations with a private store lock, so parallel agents in
separate clones cannot race while reading, writing, and committing the shared
namespace.

On conflict, report the paths and preserve both sides. Do not use `--force` or
manually overwrite without the user's direction.

Deletion propagation is blocked by default. Show the affected paths and use
`--allow-delete` only after the user explicitly approves those deletions. The
tool copies every deleted artifact into the central `recovery/` tree first.
If the user rejects a local deletion proposed by refresh, restore it with
`restore --repo <checkout> --path <artifact-relative-path>`.

## Verification

After setup or changes, run:

```bash
pce doctor --repo <checkout> --json
pce status --repo <checkout> --json
```

Confirm the product worktree has no new visible CE artifacts with
`git status --short`.

Treat JSON and exit codes as the automation contract. Human TTY output may be
summarized or restyled. For unattended reconciliation, use
`pce sync-all --commit --quiet`; inspect the automatic service's status file or
rerun without `--quiet` and with `--json` when structured output is required.
On a partial failure, preserve completed phases, follow the returned retry
commands, and rerun the idempotent primitive instead of attempting a manual
rollback.

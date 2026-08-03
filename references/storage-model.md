# Storage model

## Program and data boundary

Installed PCE program files live under a managed prefix, normally
`~/.local/lib/pce/`, with `current` selecting one immutable version. The `pce`
executable and Codex skill both link to that active package. The package
contains only the CLI, skill, and reference documentation; it never contains
knowledge namespaces or store metadata.

The private knowledge store is selected independently. A confirmed `pce init`
persists its path in per-user configuration, while `PERSONAL_COMPOUND_HOME`
provides an explicit environment override. Installing a new program version
does not migrate, delete, initialize, or inspect the selected store, and it
does not change logs or LaunchAgent state.

An existing store is adopted only when it is a Git directory. An empty path
may be initialized after confirmation. A non-empty directory without `.git`
is refused so unrelated Obsidian files cannot be reinterpreted as PCE-owned
data. The PCE source/package directory and selected product checkouts cannot
also be used as the store.

The private store is keyed by normalized canonical `origin`:

```text
projects/<readable-origin-slug>--<origin-hash>/
├── artifacts/
│   ├── plans/
│   └── solutions/
├── CONCEPTS.md
├── metadata.json
└── state.json
```

Every checkout materializes the project namespace at `.ce-personal/`.
`.personal-compound-state.json` records the last synchronized per-file hashes
and is never copied to the central store.

The `library/` tree is deliberately separate. Project documents may contain
source-specific paths and behavior. Library documents are curated,
cross-project guidance and are advisory until re-grounded in the current tree.

## Synchronization states

For each path, compare local and central hashes to the last baseline:

| Local | Central | Action |
|---|---|---|
| unchanged | changed | Pull central to local |
| changed | unchanged | Push local during `sync`; preserve during `hydrate` |
| same change | same change | Accept |
| different changes | different changes | Stop and report conflict |

Deletions are changes and follow the same table, but they require an explicit
`--allow-delete` and are copied to `recovery/` before removal.

## Repository identity

Normalize HTTPS and SSH forms to the same identity:

```text
https://github.com/woocommerce/woocommerce.git
git@github.com:woocommerce/woocommerce.git
```

Both become:

```text
github.com/woocommerce/woocommerce
```

The directory name combines a readable slug with a SHA-256-derived suffix.
This prevents ambiguous separators, non-default ports, or punctuation from
making distinct origins collide. Raw remote URLs are never persisted, because
they may contain credentials.

Fork origins remain distinct. Changing a checkout's `origin` intentionally
changes its personal knowledge namespace.

## Initialization model

`pce init` is a human-only, guided composition of existing non-interactive
operations. Before confirmation it reads repository identities, registrations,
legacy artifacts, local configuration, Git excludes, and the complete service
definition into one immutable candidate. It does not create the store or write
to a checkout during discovery or review.

Publication revalidates that candidate before acquiring the store lock and
again after the lock is held. Drift aborts before checkout setup. Confirmed
checkouts run setup and doctor in reviewed order; the central commit runs only
after every checkout is healthy, and service installation or reload runs last.
Disabling service management leaves any installed service untouched.

Setup remains additive rather than reset-oriented. A partial failure can leave
earlier phases complete, so the result records changed, no-op, failed,
unprocessed, and skipped phases with retry commands. Repeating initialization
or the named primitive converges from that state without deleting knowledge or
rolling back successful phases.

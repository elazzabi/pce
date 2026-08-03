# Personal Compound Engineering

PCE keeps private Compound Engineering plans and learnings in one Git-versioned
knowledge store while materializing the relevant namespace into selected product
checkouts. The CLI source and installed program are separate from that private
data.

This repository is currently a private-preview source checkout. Its installer
uses only local files; it does not download a release or publish anything.

## Install from this checkout

From this PCE source checkout, run:

```sh
./install.sh
```

The default prefix is `~/.local`. Ensure `~/.local/bin` is on `PATH`, then
verify the active program:

```sh
pce --version
```

To use another prefix:

```sh
./install.sh --prefix /path/to/prefix
```

The installer creates this program-only layout:

```text
~/.local/
├── bin/pce -> ../lib/pce/current/scripts/pce.py
└── lib/pce/
    ├── current -> versions/0.1.0
    └── versions/0.1.0/
        ├── SKILL.md
        ├── references/storage-model.md
        └── scripts/{pce.py,pce_ui.py}
```

It also links `${CODEX_HOME:-$HOME/.codex}/skills/personal-compound` to the
active package. Repeating the install is a no-op when that version and all
links are already intact. The installer refuses unrelated files, directories,
or links at managed targets instead of replacing them.

The installed payload never contains this repository's user-facing README,
`projects/`, `library/`, `inbox/`, local metadata, or knowledge artifacts.
Installation does not read or change PCE configuration, a knowledge store,
logs, or LaunchAgent state.

## Adopt your existing private store

After installing, leave this source repository and change into a **product
checkout** before initializing. Otherwise, the source checkout could be
mistaken for a product repository to register.

```sh
cd ~/src/example-product
pce init --store "/path/to/existing/private/Compound Engineering"
```

The final review shows the selected store and product checkouts before any
write. When confirmed, `init` records the selected store in the per-user PCE
configuration so later commands can reuse it. `PERSONAL_COMPOUND_HOME` remains
an explicit per-command environment override.

Adopting an existing Git store does not delete, reinitialize, relocate, or
rewrite it. Its Git history and unrelated Obsidian files remain untouched.
Hydration writes only to the product checkouts selected in the review, using
their origin-keyed namespaces and local Git excludes.

An empty store directory may be initialized after confirmation. A non-empty
directory without `.git` is refused because PCE cannot safely infer ownership
or convert it in place. Choose the existing PCE Git store, an empty directory,
or another location instead.

Initialization is additive and rerunnable. It never changes a checkout's Git
`origin`, silently approves deletions, or removes an existing background
service. Same-file conflicts stop before either side is overwritten.

## Everyday commands

Use the guided flow as a person:

```sh
pce init
```

Agents and scripts should use non-interactive primitives with explicit JSON:

```sh
pce setup --repo /path/to/product-checkout --json
pce hydrate --repo /path/to/product-checkout --json
pce sync --repo /path/to/product-checkout --commit --json
pce doctor --repo /path/to/product-checkout --json
```

The optional macOS service runs the same safe reconciliation in the background:

```sh
pce service install
pce service status
```

Service installation is a separate, explicit action. Installing or reinstalling
the CLI does not load, unload, or rewrite the service.

## Development

Run the installer tests:

```sh
python3 tests/test_install.py
```

Run the full PCE suite:

```sh
python3 -m unittest discover -s scripts -p 'test_*.py'
```

There is no curl or GitHub Release installer in this preview. Public release
distribution requires a separately authenticated and verified release design.

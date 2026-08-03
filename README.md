# Personal Compound Engineering

PCE keeps private Compound Engineering plans and learnings in one Git-versioned
knowledge store while materializing the relevant namespace into selected product
checkouts. The CLI source and installed program are separate from that private
data. PCE is distributed through versioned GitHub Releases; the private
knowledge store is never part of a release.

## Install

Install the latest stable release with one command:

```sh
curl -fsSL https://raw.githubusercontent.com/elazzabi/pce/main/install.sh | sh
```

Install an exact version with:

```sh
curl -fsSL https://raw.githubusercontent.com/elazzabi/pce/main/install.sh | sh -s -- --version 0.1.0
```

The default prefix is `~/.local`. Ensure `~/.local/bin` is on `PATH`, then
verify the active program:

```sh
pce --version
```

Pass `--prefix PATH` after `sh -s --` to use another prefix:

```sh
curl -fsSL https://raw.githubusercontent.com/elazzabi/pce/main/install.sh | sh -s -- --prefix /path/to/prefix
```

For development, running the checked-out installer installs directly from local
source:

```sh
./install.sh --prefix /path/to/prefix
```

The release installer downloads a small platform-independent Python archive,
verifies its declared size and SHA-256 digest, rejects unexpected archive
contents, and only then activates the version. Python 3 and `curl` are required.

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

## Development and releases

Run all source, installer, release, and workflow tests:

```sh
sh -n install.sh
python3 -m unittest discover -s scripts -p 'test_*.py'
python3 -m unittest discover -s tests -p 'test_*.py'
```

A tag must exactly match the `PCE_VERSION` declared in `scripts/pce.py`. To
publish `0.1.0`, make sure the release commit is on GitHub, then push its tag:

```sh
git tag v0.1.0
git push origin v0.1.0
```

The tag launches `.github/workflows/release.yml`. The workflow validates the
tag and full commit hash, runs the complete test suite, builds one deterministic
universal archive plus its manifest and checksums, and creates a draft GitHub
Release. A clean job then installs that exact draft through the same installer
path users run. The release is published only after the smoke install succeeds.

Watch the run with:

```sh
gh run watch
```

To validate an existing matching tag without creating or publishing a release:

```sh
gh workflow run release.yml --ref v0.1.0 -f tag=v0.1.0
```

That manual path builds, downloads, and smoke-installs the candidate from the
workflow artifact. When a new stable release is published, rerun the install
command to activate it; existing PCE configuration, services, and knowledge
data are left untouched.

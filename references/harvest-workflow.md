# Harvest learning workflow

Use this protocol when the user asks to learn from recent or pending commits.
PCE remains the deterministic selector and checkpoint store. The calling agent
owns evidence gathering and classification, while Every's Compound Engineering
skills own the learning documents they create or refresh.

Natural-language revision ranges and proposed selectors such as `--from` and
`--to` are unsupported in v1. Say so plainly and do not approximate a range
with a count or a different Git walk. A natural-language count is supported as
the harvest limit; when the user supplies no count, use five.

## 1. Preflight the semantic dependencies

Before any repository mutation, resolve these exact namespaced IDs in the
calling environment's available skill inventory:

1. `compound-engineering:ce-compound`
2. `compound-engineering:ce-compound-refresh`

Both are required even if the selected commits may ultimately need no
knowledge action. If either skill is unavailable, fail closed: report the
missing ID and stop before `setup`, hydration, audit creation, or watermark
mutation. Do not substitute a similarly named skill, copy an upstream schema,
or improvise a learning document.

## 2. Set up and hydrate the checkout

After preflight succeeds, run these deterministic operations in order:

```bash
pce setup --repo <checkout> --json
pce hydrate --repo <checkout> --json
pce status --repo <checkout> --json
```

Require successful JSON results. Before semantic work, `status` must report no
conflict and no unexplained local or central change. Use the returned repository
key and namespace path as authoritative identifiers; never hardcode the user's
private-store path.

## 3. Snapshot the mutation boundaries

Before invoking either CE skill, record a product-Git snapshot containing:

- current branch and `HEAD`;
- staged index diff;
- tracked worktree diff and full porcelain status, including untracked files;
- tracked `AGENTS.md`, `CLAUDE.md`, and `CONCEPTS.md` paths and blob IDs.

Also record a personal-artifact manifest of relative paths and content hashes
under the configured `docs_root` and, when present, an untracked personal root
`CONCEPTS.md`, plus the `pce status --repo <checkout> --json` result. Resolve
`docs_root` from the config written by setup; resolve the central namespace only
from PCE JSON. Keep this snapshot for comparison after every CE action.

Personal artifact changes explicitly reported by the CE action are expected.
A product branch, `HEAD`, index, tracked worktree, tracked instruction-file, or
unrelated personal-artifact change is not expected. On any unexplained delta,
stop before sync and marking, preserve the evidence, and report the exact paths.

## 4. Select and pin one batch

If the user supplied no count, run exactly:

```bash
pce harvest --repo <checkout> --limit 5 --json
```

For an explicit positive count, replace `5` with that count. Preserve the
returned `upstream`, `current_revision`, `last_harvested_revision`,
`safe_mark_revision`, `initial_baseline`, `truncated`, and commit list. The
returned `current_revision` is the pinned current revision for this batch. Do
not include commits that arrive later, rerun selection to enlarge the batch, or
jump from a truncated batch to a newer revision.

If PCE reports that the saved watermark is no longer an ancestor or that a
selected revision is no longer on the upstream first-parent history, stop and
report the history rewrite. Start a fresh selection only after the repository
state is understood; never substitute a nearby revision into the pinned batch.

If PCE reports a pending transaction, do not select a new batch. Return to the
same working review and retry the same revision as described in step 9.

An empty batch is a no-op: report that nothing is pending, do not write an
audit, do not invoke either CE skill, and do not call `pce harvest-mark`.

Order non-empty batches as follows:

- **Incremental:** process commits in the chronological first-parent order
  returned by PCE. When `truncated` is true, the returned
  `safe_mark_revision` is the end of this page, not `current_revision`.
- **Initial baseline:** PCE returns the newest selected commits newest-first.
  Reverse that list and review it oldest-first. Disclose that all history before
  the oldest selected commit is intentionally treated as pre-baseline history.
  The selected baseline is all-or-nothing: every selected commit must become
  terminal before the first watermark can advance.

## 5. Gather evidence and classify the batch

Derive a few problem and component terms from the selected diffs and run
`pce search --repo <checkout> --json <terms>`. Read relevant hydrated
repo-specific solutions before deciding that a learning is new or stale.
Cross-project library matches are advisory only and do not replace evidence
from this checkout.

For every selected revision, inspect the final first-parent diff and every
changed test. For a normal commit, compare its first parent with the revision.
For a root commit, compare the empty tree with the revision, for example:

```bash
empty_tree=$(git hash-object -t tree /dev/null)
git -C <checkout> diff "$empty_tree" <revision>
```

Record changed test paths and what their assertions prove; when no test changed,
record `none changed`. The commit subject or message is navigation context, not
a substitute for the final diff and tests.

Inspect relevant PR, issue, review, and design context when the calling
environment can access it. For each optional source, record exactly one evidence
status: `inspected`, `unavailable`, or `not applicable`. Never label inaccessible
material as inspected and never make remote access a correctness dependency.

Each commit receives exactly one mutually exclusive terminal classification:

- `no knowledge action` — the change is mechanical or local to the patch, adds
  no durable problem-solving method, or is already fully covered by a current
  learning.
- `refresh existing learning` — current evidence materially corrects, narrows,
  replaces, or otherwise updates an existing repo learning.
- `new repo-specific candidate` — the commit contains a verified, reusable
  problem/solution lesson for this repository that no current learning covers.
- `portable-library candidate` — the verified lesson is primarily useful
  across repositories. This is audit-only in v1 because PCE has no library
  writer; record it for later curation and do not create a library artifact.

Do not assign multiple terminal classifications. If a current learning can
fully absorb the lesson, prefer refresh. Otherwise keep a new repo-specific
lesson focused and cross-reference related existing material. Reserve portable
classification for a candidate whose intended future home is the curated
cross-project library.

Build a batch action ledger before invoking CE skills. When one learning is
shared by multiple commits, capture it once at the earliest applicable
checkpoint and reference that one action and artifact from every applicable
commit. Distinct learnings require distinct actions.

## 6. Perform and verify knowledge actions

For `no knowledge action` and `portable-library candidate`, record the rationale
and perform no CE write.

For `new repo-specific candidate`, invoke
`compound-engineering:ce-compound mode:non-interactive depth:full
<bounded-context>` with a bounded context naming the verified problem, solution,
and supporting revisions. Run one distinct learning per invocation and run
multiple invocations sequentially.
Require its successful `Documentation complete` terminal result. Preserve the
schema, filename, and path selected by the CE skill; do not invent or normalize
them in PCE. If it reports `Documentation skipped`, reclassify to
`no knowledge action` only when the evidence independently supports that
decision; otherwise the checkpoint is blocked.

For `refresh existing learning`, use the existing guarded interactive
`compound-engineering:ce-compound-refresh` wrapper from `SKILL.md` with the
narrowest useful scope. Its binding constraints are personal artifacts only;
refresh must not create or switch a product branch, must not stage, commit,
push, or open a PR in the product repository, must stop before Phase 5, and must
not edit tracked instruction files. Never use non-interactive/headless refresh
in a product repository. A cancellation or a refresh that cannot stop before
its product-Git phase blocks the checkpoint.

After each CE invocation, compare the product-Git and personal-artifact state
with the step 3 snapshot. Match every personal delta to the artifact or refresh
report returned by the CE skill. Unexpected product Git state or an unexplained
personal delta blocks the checkpoint; do not sync, mark, clean up, or silently
discard it.

## 7. Sync each successful CE action

After each successful capture or refresh action, and before calling its commit
terminal, run:

```bash
pce sync --repo <checkout> --commit --json
```

Require a successful private-store result and record it in the action ledger.
Then run `pce status --repo <checkout> --json` and require an in-sync,
conflict-free result. Refresh both artifact manifests so the next sequential
action starts from the newly synced state. A failed or indeterminate sync blocks
the checkpoint and watermark; never claim that a CE action is durable merely
because its local file exists.

Only after all required CE actions for a commit have been synced can that
commit be terminal. Stop at the first non-terminal commit and do not process
later commits. The only progress eligible for marking is the longest contiguous
successful prefix from the start of the ordered batch.

## 8. Write the audit review

Create the working Markdown review under `$TMPDIR`, for example with `mktemp`
after confirming `$TMPDIR` exists. This is a PCE checkpoint ledger, not a CE
solution. Never pass it to a CE skill or place it under `solutions/`.

Use this shape (fill every field; use `none`, `unavailable`, or `not applicable`
instead of silently omitting a field):

```markdown
# Harvest review

## Batch

- Repository key: <key>
- Upstream: <upstream>
- Pinned current revision: <current_revision>
- Previous watermark: <revision-or-none>
- Selected count: <count>
- Initial baseline: <true-or-false>
- Pre-baseline disclosure: <older-history-disclosure-or-not-applicable>
- Started at: <ISO-8601-time>
- Final marked revision: <revision-or-none>

## Commit <ordinal>: <short-revision> <subject>

- Revision: <full-revision>
- Subject: <subject>
- Evidence status: final diff=<status>; changed tests=<paths-or-none>;
  PR=<status>; issue=<status>; review=<status>; design=<status>
- Classification: <one-exact-classification>
- Rationale: <evidence-grounded-reason>
- CE actions: <action-ids-or-none>
- Artifact or refresh report: <path/report-reference-or-none>
- Sync result: <result-or-not-applicable>
- Terminal outcome: <terminal|blocked|not-processed>
- Defer or failure reason: <reason-or-none>
```

Include one checkpoint entry for every selected commit. After the first blocked
entry, label later entries `not-processed` with the preceding blocker as the
defer or failure reason. For an incomplete initial baseline, preserve the
working review for diagnosis but do not store it through `harvest-mark`.

## 9. Mark only completed progress

An incomplete initial baseline must not mark any revision. For an incremental
batch, if no commit is terminal, do not mark. Otherwise set the audit's final
marked revision to the last commit in the longest contiguous successful prefix
and run:

```bash
pce harvest-mark \
  --repo <checkout> \
  --revision <last-terminal-revision> \
  --review-file <working-review-under-TMPDIR> \
  --commit \
  --json
```

For a fully completed initial baseline or incremental page, use the returned
`safe_mark_revision` only when it is the same revision as the last terminal
commit. `harvest-mark` copies the audit into `harvest-reviews/`; that stored
review remains separate from CE-owned learnings.

Treat success only as a completed private-store commit. If the command reports
an unresolved pending transaction, leave its staged state intact and report the
signing, authentication, or Git failure. After that problem is resolved, retry
the exact `pce harvest-mark --commit` operation for the same revision. Do not
select a new batch, do not copy or rewrite the review manually, and do not retry
a different revision. The matching retry commits the already staged review and
state without copying a duplicate review.

When a completed incremental result was truncated, begin a new invocation of
this protocol to select the next page. Never reuse the old pinned boundary or
silently continue to a newer `current_revision` within the completed audit.

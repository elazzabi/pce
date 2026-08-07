from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "SKILL.md"
HARVEST_REFERENCE = ROOT / "references" / "harvest-workflow.md"


class HarvestSkillContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.skill = SKILL.read_text(encoding="utf-8")
        cls.workflow = HARVEST_REFERENCE.read_text(encoding="utf-8")
        cls.normalized_workflow = " ".join(cls.workflow.split())

    def assert_in_order(self, *needles: str) -> None:
        cursor = -1
        for needle in needles:
            position = self.workflow.find(needle, cursor + 1)
            self.assertGreater(position, cursor, f"{needle!r} is missing or out of order")
            cursor = position

    def test_skill_routes_harvest_to_the_bundled_reference(self) -> None:
        harvest_section = self.skill.split("## Harvest work by others", 1)[1].split(
            "## Conflict behavior", 1
        )[0]

        self.assertIn("references/harvest-workflow.md", harvest_section)
        self.assertIn("binding invariants", harvest_section.lower())
        self.assertLess(len(harvest_section.splitlines()), 35)

    def test_preflight_names_both_dependencies_before_mutation(self) -> None:
        self.assert_in_order(
            "compound-engineering:ce-compound",
            "compound-engineering:ce-compound-refresh",
            "pce setup --repo <checkout> --commit --json",
            "pce hydrate --repo <checkout> --json",
        )
        self.assertIn("fail closed", self.workflow.lower())
        self.assertIn("before any repository mutation", self.workflow.lower())
        self.assertIn("setup's `central_commit`", self.workflow.lower())
        self.assertRegex(
            self.workflow.lower(), r"first-time batch needs no\s+ce\s+action"
        )

    def test_selection_is_explicit_bounded_and_pinned(self) -> None:
        self.assertIn("pce harvest --repo <checkout> --limit 5 --json", self.workflow)
        self.assertIn("pinned current revision", self.workflow.lower())
        self.assertIn(
            "do not include commits that arrive later",
            self.normalized_workflow.lower(),
        )

    def test_all_terminal_classifications_are_exact(self) -> None:
        self.assertIn("pce search --repo <checkout> --json <terms>", self.workflow)
        for classification in (
            "`no knowledge action`",
            "`refresh existing learning`",
            "`new repo-specific candidate`",
            "`portable-library candidate`",
        ):
            self.assertIn(classification, self.workflow)
        self.assertIn("exactly one", self.workflow.lower())

    def test_required_and_optional_evidence_are_distinguished(self) -> None:
        self.assertIn("final first-parent diff", self.workflow.lower())
        self.assertIn("changed tests", self.workflow.lower())
        self.assertIn("empty tree", self.workflow.lower())
        for evidence in ("PR", "issue", "review", "design"):
            self.assertRegex(self.workflow, rf"(?i)\b{evidence}\b")
        for status in ("inspected", "unavailable", "not applicable"):
            self.assertIn(status, self.workflow.lower())

    def test_incremental_and_initial_ordering_are_distinct(self) -> None:
        self.assertIn("incremental", self.workflow.lower())
        self.assertIn("order returned by PCE", self.normalized_workflow)
        self.assertIn("initial_baseline", self.workflow)
        self.assertIn("reverse", self.workflow.lower())
        self.assertIn("oldest-first", self.workflow.lower())
        self.assertIn("pre-baseline", self.workflow.lower())

    def test_ce_actions_preserve_upstream_schema_and_product_git(self) -> None:
        self.assertIn(
            "`compound-engineering:ce-compound mode:non-interactive depth:full",
            self.workflow,
        )
        self.assertIn("one distinct learning per invocation", self.workflow.lower())
        self.assertIn("sequential", self.workflow.lower())
        self.assertIn("do not invent", self.workflow.lower())
        self.assertIn("personal artifacts only", self.workflow.lower())
        self.assertIn("stop before phase 5", self.workflow.lower())
        for forbidden_action in ("branch", "stage", "commit", "push", "PR"):
            self.assertRegex(
                self.workflow,
                rf"(?is)refresh.*must not.*\b{forbidden_action}\b",
            )

    def test_success_order_is_action_sync_audit_then_mark(self) -> None:
        self.assert_in_order(
            "## 6. Perform and verify knowledge actions",
            "## 7. Sync each successful CE action",
            "## 8. Write the audit review",
            "## 9. Mark only completed progress",
        )

    def test_failures_preserve_only_a_contiguous_prefix(self) -> None:
        self.assertIn("stop at the first non-terminal commit", self.workflow.lower())
        self.assertIn("longest contiguous successful prefix", self.workflow.lower())
        self.assertIn("incomplete initial baseline", self.workflow.lower())
        self.assertIn("must not mark", self.workflow.lower())

    def test_audit_template_contains_batch_and_commit_fields(self) -> None:
        for field in (
            "repository key",
            "upstream",
            "pinned current revision",
            "previous watermark",
            "selected count",
            "initial baseline",
            "pre-baseline disclosure",
            "started at",
            "final marked revision",
            "revision",
            "subject",
            "evidence status",
            "classification",
            "rationale",
            "CE actions",
            "artifact or refresh report",
            "sync result",
            "terminal outcome",
            "defer or failure reason",
        ):
            self.assertRegex(self.workflow, rf"(?i){re.escape(field)}")
        self.assertIn("$TMPDIR", self.workflow)
        self.assertIn("harvest-reviews/", self.workflow)
        self.assertIn("not a CE solution", self.normalized_workflow)

    def test_portable_candidates_are_audit_only(self) -> None:
        portable = self.normalized_workflow.split("`portable-library candidate`", 1)[1]
        self.assertIn("audit-only", portable.lower())
        self.assertIn("no library writer", portable.lower())

    def test_shared_learning_is_captured_once(self) -> None:
        self.assertIn("shared by multiple commits", self.workflow.lower())
        self.assertIn("capture it once", self.workflow.lower())
        self.assertIn("reference", self.workflow.lower())

    def test_ranges_are_explicitly_unsupported(self) -> None:
        self.assertIn(
            "natural-language revision ranges", self.normalized_workflow.lower()
        )
        self.assertIn("`--from`", self.workflow)
        self.assertIn("`--to`", self.workflow)
        self.assertIn("unsupported in v1", self.workflow.lower())
        self.assertRegex(
            self.normalized_workflow.lower(), r"(?:must|do) not approximate"
        )

    def test_pending_transaction_retries_the_same_revision_only(self) -> None:
        self.assertIn("pending transaction", self.workflow.lower())
        self.assertIn("same revision", self.workflow.lower())
        self.assertIn("do not select a new batch", self.workflow.lower())
        self.assertIn("do not copy", self.workflow.lower())

    def test_empty_batch_is_a_no_op(self) -> None:
        self.assertIn("empty batch", self.workflow.lower())
        self.assertIn("no-op", self.workflow.lower())
        self.assertIn("do not write an audit", self.normalized_workflow.lower())
        self.assertIn("do not call `pce harvest-mark`", self.workflow.lower())


if __name__ == "__main__":
    unittest.main()

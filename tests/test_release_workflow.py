from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def job_block(source: str, job_name: str) -> str:
    match = re.search(rf"(?m)^  {re.escape(job_name)}:\n", source)
    if match is None:
        raise AssertionError(f"job {job_name!r} is missing")
    following = re.search(r"(?m)^  [a-z][a-z0-9-]*:\n", source[match.end() :])
    end = len(source) if following is None else match.end() + following.start()
    return source[match.start() : end]


class ReleaseWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = WORKFLOW.read_text(encoding="utf-8")

    def test_validate_resolves_the_triggered_tag_to_one_revision(self) -> None:
        block = job_block(self.source, "validate")

        self.assertIn("tags: ['v*.*.*']", self.source)
        self.assertIn("workflow_dispatch:", self.source)
        self.assertIn("scripts/build_release.py validate-tag", block)
        self.assertIn("ref: ${{ env.RELEASE_TAG }}", block)
        self.assertIn('source_revision="$(git rev-parse HEAD)"', block)
        self.assertIn('source-revision=$source_revision', block)

    def test_build_is_pinned_to_the_validated_revision(self) -> None:
        block = job_block(self.source, "build")

        self.assertIn("    needs: validate\n", block)
        self.assertIn("ref: ${{ needs.validate.outputs.source-revision }}", block)
        self.assertIn("scripts/build_release.py build", block)
        self.assertIn(
            '--source-revision "${{ needs.validate.outputs.source-revision }}"',
            block,
        )

    def test_tag_release_creates_or_reuses_exactly_one_draft(self) -> None:
        block = job_block(self.source, "draft")

        self.assertIn("    if: github.event_name == 'push'\n", block)
        self.assertIn("    needs: [validate, build]\n", block)
        self.assertIn("    timeout-minutes: 10\n", block)
        self.assertIn("release-id: ${{ steps.release.outputs.release-id }}", block)
        self.assertIn("gh api --paginate --slurp", block)
        self.assertIn('releases.json\" || return \"$?\"', block)
        self.assertIn("if len(matches) > 1:", block)
        self.assertIn("if release.get(\"draft\") is not True:", block)
        self.assertIn("release.get(\"target_commitish\") != sys.argv[4]", block)
        self.assertIn('gh api --method POST "/repos/$GH_REPO/releases" \\', block)
        self.assertIn('-f target_commitish="$SOURCE_REVISION" \\', block)
        self.assertIn('-F draft=true \\', block)
        self.assertIn('release_id="$(release_id_from_json', block)
        self.assertIn('gh api --method DELETE "/repos/$GH_REPO/releases/assets/$asset_id"', block)
        self.assertIn('releases/$release_id/assets?name=$asset_name', block)
        self.assertNotIn("wait_for_release", block)
        self.assertNotIn('gh release upload "$RELEASE_TAG"', block)
        self.assertLess(
            block.index('release_id="$(release_id_from_json'),
            block.index('releases/$release_id/assets?name=$asset_name'),
        )
        self.assertIn('echo "release-id=$release_id"', block)

    def test_tag_smoke_downloads_the_pinned_draft_on_both_platforms(self) -> None:
        block = job_block(self.source, "smoke-release")

        self.assertIn("    if: github.event_name == 'push'\n", block)
        self.assertIn("    needs: [validate, draft]\n", block)
        self.assertIn("os: [ubuntu-24.04, macos-15]", block)
        self.assertIn("ref: ${{ needs.validate.outputs.source-revision }}", block)
        self.assertIn("RELEASE_ID: ${{ needs.draft.outputs.release-id }}", block)
        self.assertIn('releases/$RELEASE_ID/assets?per_page=100', block)
        self.assertIn('releases/assets/$asset_id', block)
        self.assertIn("PCE_RELEASE_BASE_URL=http://127.0.0.1:8765", block)
        self.assertIn("--retry-connrefused", block)
        self.assertIn("trap 'kill", block)
        self.assertNotIn("PCE_GITHUB_TOKEN", block)
        self.assertNotIn("/releases/tags/", self.source)

    def test_manual_dispatch_builds_and_smokes_without_a_release(self) -> None:
        block = job_block(self.source, "smoke-dry-run")

        self.assertIn("    if: github.event_name == 'workflow_dispatch'\n", block)
        self.assertIn("    needs: [validate, build]\n", block)
        self.assertIn("os: [ubuntu-24.04, macos-15]", block)
        self.assertIn("ref: ${{ needs.validate.outputs.source-revision }}", block)
        self.assertIn("name: candidate-release", block)
        self.assertIn("PCE_RELEASE_BASE_URL=http://127.0.0.1:8765", block)
        self.assertIn("--retry-connrefused", block)
        self.assertNotIn("gh release create", block)
        self.assertNotIn("gh api --method PATCH", block)

    def test_publish_uses_only_the_validated_release_id(self) -> None:
        block = job_block(self.source, "publish")

        self.assertIn("    if: github.event_name == 'push'\n", block)
        self.assertIn("    needs: [validate, draft, smoke-release]\n", block)
        self.assertIn("RELEASE_ID: ${{ needs.draft.outputs.release-id }}", block)
        self.assertIn('gh api "/repos/$GH_REPO/releases/$RELEASE_ID"', block)
        self.assertIn("the pinned draft changed before publication", block)
        self.assertIn(
            'gh api --method PATCH "/repos/$GH_REPO/releases/$RELEASE_ID" -F draft=false',
            block,
        )
        self.assertNotIn("gh release edit", block)


if __name__ == "__main__":
    unittest.main()

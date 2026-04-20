"""Story templates — classify issues into archetypes + render tailored prompts."""

from __future__ import annotations

import pytest

from maxwell_daemon.templates import IssueKind, classify_issue, render_system_prompt


class TestClassify:
    def test_docs_by_label(self) -> None:
        assert classify_issue(title="any", body="any", labels=["docs"]) is IssueKind.DOCS

    def test_bug_by_label(self) -> None:
        assert classify_issue(title="any", body="any", labels=["bug"]) is IssueKind.BUG

    def test_feature_by_label(self) -> None:
        assert classify_issue(title="any", body="any", labels=["feature"]) is IssueKind.FEATURE

    def test_refactor_by_label(self) -> None:
        assert classify_issue(title="any", body="any", labels=["refactor"]) is IssueKind.REFACTOR

    def test_test_by_label(self) -> None:
        assert classify_issue(title="any", body="any", labels=["tests"]) is IssueKind.TEST

    def test_bug_by_title(self) -> None:
        for title in ("Crash when empty input", "bug: tokenizer fails", "fix segfault"):
            assert classify_issue(title=title, body="", labels=[]) is IssueKind.BUG

    def test_docs_by_title(self) -> None:
        for title in ("typo in README", "docs: clarify install"):
            assert classify_issue(title=title, body="", labels=[]) is IssueKind.DOCS

    def test_feature_by_title(self) -> None:
        assert classify_issue(title="add support for SAML", body="", labels=[]) is IssueKind.FEATURE

    def test_default_is_default(self) -> None:
        assert classify_issue(title="something", body="", labels=[]) is IssueKind.DEFAULT

    def test_complex_label_wins_over_docs_label(self) -> None:
        # p0 + docs → DEFAULT would be wrong; the template classifier only
        # picks archetype, severity is handled elsewhere. docs label should
        # still classify as docs.
        assert classify_issue(title="t", body="", labels=["docs", "p0"]) is IssueKind.DOCS


class TestRenderSystemPrompt:
    def test_known_kind_returns_specialised_prompt(self) -> None:
        prompt = render_system_prompt(IssueKind.BUG)
        assert "bug" in prompt.lower()
        # Every template must declare the JSON schema the executor expects.
        assert '"plan"' in prompt
        assert '"diff"' in prompt

    def test_all_kinds_render(self) -> None:
        for kind in IssueKind:
            prompt = render_system_prompt(kind)
            assert '"plan"' in prompt
            assert '"diff"' in prompt
            assert len(prompt) > 100

    def test_default_is_sensible(self) -> None:
        default = render_system_prompt(IssueKind.DEFAULT)
        # Default prompt should mention senior engineer / pull request framing.
        assert "pull request" in default.lower() or "senior engineer" in default.lower()


class TestContractEnforcement:
    def test_classify_rejects_non_string_title(self) -> None:
        with pytest.raises((TypeError, AttributeError)):
            classify_issue(title=None, body="b", labels=[])  # type: ignore[arg-type]

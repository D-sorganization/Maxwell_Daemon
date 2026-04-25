"""Smart model selection — route by task complexity."""

from __future__ import annotations

import pytest

from maxwell_daemon.core.model_selector import (
    ModelTier,
    score_issue,
    select_model,
)


class TestScoreIssue:
    def test_empty_body_is_simple(self) -> None:
        s = score_issue(title="typo in readme", body="", labels=[])
        assert s.tier is ModelTier.SIMPLE

    def test_short_docs_issue_is_simple(self) -> None:
        s = score_issue(title="fix typo", body="two words", labels=["docs"])
        assert s.tier is ModelTier.SIMPLE

    def test_bug_label_bumps_to_moderate(self) -> None:
        s = score_issue(
            title="tests failing",
            body="three or four lines of test output here",
            labels=["bug"],
        )
        assert s.tier in {ModelTier.MODERATE, ModelTier.COMPLEX}

    def test_p0_or_critical_is_complex(self) -> None:
        for label in ("p0", "critical", "security"):
            s = score_issue(title="bad", body="x", labels=[label])
            assert s.tier is ModelTier.COMPLEX

    def test_long_body_bumps_up(self) -> None:
        long_body = "x " * 1500
        s = score_issue(title="do the thing", body=long_body, labels=[])
        assert s.tier in {ModelTier.MODERATE, ModelTier.COMPLEX}

    def test_code_blocks_bump_up(self) -> None:
        body = "Repro:\n```python\nsegfault\n```\nStack:\n```\nfoo\n```"
        s = score_issue(title="crash", body=body, labels=[])
        assert s.tier is not ModelTier.SIMPLE

    def test_factors_recorded(self) -> None:
        s = score_issue(title="crash", body="x" * 2000, labels=["critical", "security"])
        assert "body_length" in s.factors
        assert "labels" in s.factors


class TestSelectModel:
    def test_picks_from_tier_map(self) -> None:
        tier_map = {
            "simple": "haiku",
            "moderate": "sonnet",
            "complex": "opus",
        }
        assert select_model(ModelTier.SIMPLE, tier_map) == "haiku"
        assert select_model(ModelTier.MODERATE, tier_map) == "sonnet"
        assert select_model(ModelTier.COMPLEX, tier_map) == "opus"

    def test_fallback_when_tier_missing(self) -> None:
        tier_map = {"moderate": "sonnet"}
        # Missing SIMPLE → fall back to MODERATE → sonnet.
        assert select_model(ModelTier.SIMPLE, tier_map, fallback="sonnet") == "sonnet"

    def test_empty_tier_map_returns_fallback(self) -> None:
        assert (
            select_model(ModelTier.SIMPLE, {}, fallback="some-default")
            == "some-default"
        )

    def test_no_fallback_no_entry_raises(self) -> None:
        with pytest.raises(ValueError):
            select_model(ModelTier.SIMPLE, {})


class TestIntegration:
    def test_short_body_no_keywords_is_simple(self) -> None:
        s = score_issue(
            title="need help with login", body="small change needed", labels=[]
        )
        assert s.tier is ModelTier.SIMPLE

    def test_end_to_end_pick(self) -> None:
        from maxwell_daemon.core.model_selector import pick_model_for_issue

        tier_map = {
            "simple": "haiku",
            "moderate": "sonnet",
            "complex": "opus",
        }
        result = pick_model_for_issue(
            title="critical security bug",
            body="crash on auth path",
            labels=["critical"],
            tier_map=tier_map,
            fallback="sonnet",
        )
        assert result.model == "opus"
        assert result.tier is ModelTier.COMPLEX

"""EpisodicStore — searchable history of past issue→PR outcomes."""

from __future__ import annotations

from pathlib import Path

import pytest

from maxwell_daemon.memory import Episode, EpisodicStore


@pytest.fixture
def store(tmp_path: Path) -> EpisodicStore:
    return EpisodicStore(tmp_path / "memory.db")


def _ep(
    *,
    id: str = "e1",
    repo: str = "o/r",
    number: int = 1,
    title: str = "fix the parser",
    body: str = "it breaks on empty input",
    plan: str = "handle empty input explicitly",
    outcome: str = "merged",
) -> Episode:
    return Episode(
        id=id,
        repo=repo,
        issue_number=number,
        issue_title=title,
        issue_body=body,
        plan=plan,
        applied_diff=True,
        pr_url=f"https://github.com/{repo}/pull/{number}",
        outcome=outcome,
    )


class TestRecordAndSearch:
    def test_empty_search(self, store: EpisodicStore) -> None:
        assert store.search("parser", limit=5) == []

    def test_record_and_find_by_keyword(self, store: EpisodicStore) -> None:
        store.record(_ep(title="fix the parser bug"))
        results = store.search("parser", limit=5)
        assert len(results) == 1
        assert results[0].issue_title == "fix the parser bug"

    def test_search_body_text(self, store: EpisodicStore) -> None:
        store.record(_ep(body="segfault on empty input to tokenizer"))
        assert store.search("tokenizer", limit=5)[0].issue_number == 1

    def test_plan_indexed(self, store: EpisodicStore) -> None:
        store.record(_ep(plan="add regression test then fix"))
        assert store.search("regression", limit=5)

    def test_limit_respected(self, store: EpisodicStore) -> None:
        for i in range(10):
            store.record(_ep(id=f"e{i}", number=i, title=f"parser {i}"))
        assert len(store.search("parser", limit=3)) == 3

    def test_repo_scoping(self, store: EpisodicStore) -> None:
        store.record(_ep(id="a", repo="one/x", title="fix bug"))
        store.record(_ep(id="b", repo="two/y", title="fix bug"))
        assert len(store.search("bug", repo="one/x", limit=5)) == 1

    def test_upsert_on_same_id(self, store: EpisodicStore) -> None:
        store.record(_ep(id="x", title="original"))
        store.record(_ep(id="x", title="updated"))
        results = store.search("updated", limit=5)
        assert len(results) == 1

    def test_failed_episodes_excluded_by_default(self, store: EpisodicStore) -> None:
        store.record(_ep(id="ok", outcome="merged", title="same words"))
        store.record(_ep(id="bad", outcome="closed", title="same words"))
        # Default: only successful outcomes returned.
        results = store.search("same words", limit=5)
        assert [r.id for r in results] == ["ok"]

    def test_survives_reopen(self, tmp_path: Path) -> None:
        path = tmp_path / "memory.db"
        s1 = EpisodicStore(path)
        s1.record(_ep(title="fix the parser"))
        s2 = EpisodicStore(path)
        assert s2.search("parser", limit=5)[0].issue_title == "fix the parser"


class TestRender:
    def test_renders_markdown_summary(self, store: EpisodicStore) -> None:
        store.record(_ep(title="fix parser on empty input"))
        rendered = store.render_related("parser empty", repo="o/r", limit=3)
        assert "fix parser on empty input" in rendered
        assert "pull/1" in rendered

    def test_empty_query_returns_empty(self, store: EpisodicStore) -> None:
        assert store.render_related("", repo="o/r", limit=3) == ""

    def test_no_matches_returns_empty(self, store: EpisodicStore) -> None:
        store.record(_ep(title="completely unrelated"))
        assert store.render_related("quantum mechanics", repo="o/r", limit=3) == ""


class TestContractViolation:
    def test_rejects_empty_id(self, store: EpisodicStore) -> None:
        from maxwell_daemon.contracts import PreconditionError

        with pytest.raises(PreconditionError):
            store.record(_ep(id=""))

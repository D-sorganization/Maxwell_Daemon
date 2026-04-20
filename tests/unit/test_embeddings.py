"""Semantic embedding primitives for episodic memory retrieval."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from maxwell_daemon.backends.base import BackendUnavailableError
from maxwell_daemon.contracts import PreconditionError
from maxwell_daemon.memory.embeddings import (
    EmbeddingCache,
    EmbeddingResult,
    OpenAIEmbeddingProvider,
    StubEmbeddingProvider,
    cosine_similarity,
    hash_text,
    rerank,
)


def _run(coro: Any) -> Any:
    """Tiny sync bridge so tests don't each need a pytest-asyncio marker."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# StubEmbeddingProvider
# ---------------------------------------------------------------------------


class TestStubProvider:
    def test_rejects_tiny_dimensions(self) -> None:
        with pytest.raises(PreconditionError):
            StubEmbeddingProvider(dimensions=8)

    def test_accepts_minimum_dimensions(self) -> None:
        provider = StubEmbeddingProvider(dimensions=16)
        assert provider.dimensions == 16

    def test_batch_count_matches_input(self) -> None:
        provider = StubEmbeddingProvider(dimensions=32)
        out = _run(provider.embed_batch(("one", "two", "three")))
        assert len(out) == 3

    def test_each_vector_has_requested_dimensions(self) -> None:
        provider = StubEmbeddingProvider(dimensions=48)
        out = _run(provider.embed_batch(("hello",)))
        assert len(out[0].vector) == 48
        assert out[0].dimensions == 48

    def test_deterministic_same_text_same_vector(self) -> None:
        provider = StubEmbeddingProvider(dimensions=32)
        a = _run(provider.embed_batch(("the parser broke",)))
        b = _run(provider.embed_batch(("the parser broke",)))
        assert a[0].vector == b[0].vector

    def test_different_texts_different_vectors(self) -> None:
        provider = StubEmbeddingProvider(dimensions=32)
        out = _run(provider.embed_batch(("alpha", "beta")))
        assert out[0].vector != out[1].vector

    def test_text_hash_is_sha256_prefix(self) -> None:
        provider = StubEmbeddingProvider(dimensions=32)
        text = "hash me please"
        out = _run(provider.embed_batch((text,)))
        expected = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        assert out[0].text_hash == expected == hash_text(text)

    def test_empty_batch_returns_empty(self) -> None:
        provider = StubEmbeddingProvider(dimensions=32)
        assert _run(provider.embed_batch(())) == ()

    def test_provider_name_stamped(self) -> None:
        provider = StubEmbeddingProvider(dimensions=32)
        out = _run(provider.embed_batch(("x",)))
        assert out[0].provider_name == "stub"


# ---------------------------------------------------------------------------
# OpenAIEmbeddingProvider
# ---------------------------------------------------------------------------


class _FakeEmbeddingItem:
    def __init__(self, embedding: list[float]) -> None:
        self.embedding = embedding


class _FakeEmbeddingResponse:
    def __init__(self, vectors: list[list[float]]) -> None:
        self.data = [_FakeEmbeddingItem(v) for v in vectors]


class _FakeEmbeddings:
    def __init__(self, vectors: list[list[float]]) -> None:
        self._vectors = vectors
        self.create = AsyncMock(return_value=_FakeEmbeddingResponse(vectors))


class _FakeClient:
    def __init__(self, vectors: list[list[float]]) -> None:
        self.embeddings = _FakeEmbeddings(vectors)


class TestOpenAIProvider:
    def test_pulls_api_key_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        # Don't inject — force the constructor to hit the env path. It will
        # build a real openai.AsyncOpenAI with that key but never call it.
        provider = OpenAIEmbeddingProvider()
        assert provider.name == "openai"
        assert provider.model == "text-embedding-3-small"

    def test_raises_when_no_key_and_no_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(BackendUnavailableError):
            OpenAIEmbeddingProvider()

    def test_embed_batch_calls_injected_client(self) -> None:
        client = _FakeClient([[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]])
        provider = OpenAIEmbeddingProvider(http_client=client, model="test-model")
        out = _run(provider.embed_batch(("alpha", "beta")))
        client.embeddings.create.assert_awaited_once_with(
            model="test-model", input=["alpha", "beta"]
        )
        assert len(out) == 2

    def test_response_parsed_into_embedding_results(self) -> None:
        client = _FakeClient([[0.5, 0.5, 0.5]])
        provider = OpenAIEmbeddingProvider(http_client=client)
        (result,) = _run(provider.embed_batch(("only",)))
        assert isinstance(result, EmbeddingResult)
        assert result.vector == (0.5, 0.5, 0.5)
        assert result.dimensions == 3
        assert result.provider_name == "openai"
        assert result.text_hash == hash_text("only")

    def test_dimensions_override_forwarded(self) -> None:
        client = _FakeClient([[0.0] * 256])
        provider = OpenAIEmbeddingProvider(http_client=client, dimensions_override=256)
        _run(provider.embed_batch(("x",)))
        call_kwargs = client.embeddings.create.await_args.kwargs
        assert call_kwargs["dimensions"] == 256
        assert provider.dimensions == 256

    def test_no_dimensions_kwarg_when_not_overridden(self) -> None:
        client = _FakeClient([[0.0] * 3])
        provider = OpenAIEmbeddingProvider(http_client=client)
        _run(provider.embed_batch(("x",)))
        call_kwargs = client.embeddings.create.await_args.kwargs
        assert "dimensions" not in call_kwargs

    def test_empty_batch_short_circuits(self) -> None:
        client = _FakeClient([])
        provider = OpenAIEmbeddingProvider(http_client=client)
        assert _run(provider.embed_batch(())) == ()
        client.embeddings.create.assert_not_awaited()


# ---------------------------------------------------------------------------
# cosine_similarity
# ---------------------------------------------------------------------------


class TestCosineSimilarity:
    def test_identical_vectors(self) -> None:
        v = (1.0, 2.0, 3.0)
        assert cosine_similarity(v, v) == pytest.approx(1.0)

    def test_opposite_vectors(self) -> None:
        a = (1.0, 2.0, 3.0)
        b = (-1.0, -2.0, -3.0)
        assert cosine_similarity(a, b) == pytest.approx(-1.0)

    def test_orthogonal_vectors(self) -> None:
        a = (1.0, 0.0, 0.0)
        b = (0.0, 1.0, 0.0)
        assert cosine_similarity(a, b) == pytest.approx(0.0)

    def test_zero_vector_returns_zero_not_nan(self) -> None:
        zero = (0.0, 0.0, 0.0)
        some = (1.0, 2.0, 3.0)
        assert cosine_similarity(zero, some) == 0.0
        assert cosine_similarity(some, zero) == 0.0

    def test_mismatched_dimensions_raise(self) -> None:
        with pytest.raises(ValueError):
            cosine_similarity((1.0, 2.0), (1.0, 2.0, 3.0))


# ---------------------------------------------------------------------------
# rerank
# ---------------------------------------------------------------------------


class TestRerank:
    def test_stable_index_order_on_ties(self) -> None:
        candidates = (("a", 0.5), ("b", 0.5), ("c", 0.5))
        vec = (1.0, 0.0)
        vecs = ((1.0, 0.0), (1.0, 0.0), (1.0, 0.0))
        assert rerank(candidates, query_vec=vec, candidate_vecs=vecs) == (0, 1, 2)

    def test_pure_embedding_weight_ignores_fts(self) -> None:
        # FTS would rank 0 > 1 > 2; embedding similarity should flip it.
        candidates = (("a", 10.0), ("b", 5.0), ("c", 0.0))
        query = (1.0, 0.0)
        vecs = (
            (-1.0, 0.0),  # cosine = -1
            (0.0, 1.0),  # cosine = 0
            (1.0, 0.0),  # cosine = +1
        )
        assert rerank(
            candidates,
            query_vec=query,
            candidate_vecs=vecs,
            fts_weight=0.0,
            embedding_weight=1.0,
        ) == (2, 1, 0)

    def test_pure_fts_weight_ignores_embedding(self) -> None:
        candidates = (("a", 0.1), ("b", 0.9), ("c", 0.5))
        query = (1.0, 0.0)
        # All embeddings identical — only FTS decides.
        vecs = ((1.0, 0.0), (1.0, 0.0), (1.0, 0.0))
        assert rerank(
            candidates,
            query_vec=query,
            candidate_vecs=vecs,
            fts_weight=1.0,
            embedding_weight=0.0,
        ) == (1, 2, 0)

    def test_zero_weight_sum_rejected(self) -> None:
        with pytest.raises(PreconditionError):
            rerank(
                (("a", 1.0),),
                query_vec=(1.0,),
                candidate_vecs=((1.0,),),
                fts_weight=0.0,
                embedding_weight=0.0,
            )

    def test_negative_weight_rejected(self) -> None:
        with pytest.raises(PreconditionError):
            rerank(
                (("a", 1.0),),
                query_vec=(1.0,),
                candidate_vecs=((1.0,),),
                fts_weight=-0.1,
                embedding_weight=1.0,
            )

    def test_top_result_has_highest_blended_score(self) -> None:
        candidates = (("poor-fts-great-emb", 0.0), ("great-fts-poor-emb", 1.0))
        query = (1.0, 0.0)
        vecs = (
            (1.0, 0.0),  # cosine = 1.0
            (-1.0, 0.0),  # cosine = -1.0
        )
        # Embedding weight dominates → index 0 wins.
        result = rerank(
            candidates,
            query_vec=query,
            candidate_vecs=vecs,
            fts_weight=0.3,
            embedding_weight=0.7,
        )
        assert result[0] == 0

    def test_empty_candidates_returns_empty(self) -> None:
        assert rerank((), query_vec=(1.0,), candidate_vecs=()) == ()


# ---------------------------------------------------------------------------
# EmbeddingCache
# ---------------------------------------------------------------------------


class TestEmbeddingCache:
    @pytest.fixture
    def cache(self, tmp_path: Path) -> EmbeddingCache:
        return EmbeddingCache(tmp_path / "emb.db")

    def _result(
        self,
        *,
        provider: str = "stub",
        text: str = "hello",
        vector: tuple[float, ...] = (0.1, 0.2, 0.3),
    ) -> EmbeddingResult:
        return EmbeddingResult(
            text_hash=hash_text(text),
            vector=vector,
            dimensions=len(vector),
            provider_name=provider,
        )

    def test_miss_returns_none(self, cache: EmbeddingCache) -> None:
        assert cache.get(provider="stub", text_hash="deadbeefdeadbeef") is None

    def test_roundtrip(self, cache: EmbeddingCache) -> None:
        result = self._result(vector=(0.1, 0.2, 0.3, 0.4))
        cache.put(result)
        got = cache.get(provider="stub", text_hash=result.text_hash)
        assert got is not None
        assert len(got) == 4
        for a, b in zip(got, (0.1, 0.2, 0.3, 0.4), strict=True):
            assert a == pytest.approx(b)

    def test_providers_isolated(self, cache: EmbeddingCache) -> None:
        stub = self._result(provider="stub", vector=(1.0, 0.0))
        openai = EmbeddingResult(
            text_hash=stub.text_hash,
            vector=(0.0, 1.0),
            dimensions=2,
            provider_name="openai",
        )
        cache.put(stub)
        cache.put(openai)
        assert cache.get(provider="stub", text_hash=stub.text_hash) == (1.0, 0.0)
        assert cache.get(provider="openai", text_hash=stub.text_hash) == (0.0, 1.0)

    def test_prune_older_than_removes_and_counts(
        self, cache: EmbeddingCache, tmp_path: Path
    ) -> None:
        # Insert a row with a fabricated old timestamp by writing directly.
        result = self._result()
        cache.put(result)

        old_ts = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        import sqlite3

        conn = sqlite3.connect(tmp_path / "emb.db")
        conn.execute(
            "UPDATE embedding_cache SET created_at = ? WHERE text_hash = ?",
            (old_ts, result.text_hash),
        )
        conn.commit()
        conn.close()

        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        removed = cache.prune_older_than(cutoff)
        assert removed == 1
        assert cache.get(provider="stub", text_hash=result.text_hash) is None

    def test_prune_leaves_fresh_rows(self, cache: EmbeddingCache) -> None:
        cache.put(self._result())
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        assert cache.prune_older_than(cutoff) == 0

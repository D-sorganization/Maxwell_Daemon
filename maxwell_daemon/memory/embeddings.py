"""Semantic embedding primitives for episodic memory retrieval.

Today ``EpisodicStore`` ranks past episodes with SQLite FTS5 (keyword BM25).
FTS is fast and zero-dep, but it misses paraphrases — "fix the parser" and
"segfault in the tokenizer" don't share surface tokens even though they're
the same kind of work.

This module delivers the *primitives* needed to bolt semantic retrieval onto
that keyword baseline:

* :class:`EmbeddingResult` — one embedding plus the content hash it encodes.
* :class:`EmbeddingProvider` — structural protocol every backend satisfies.
* :class:`StubEmbeddingProvider` — deterministic, offline provider for tests
  and air-gapped installs. Not semantically meaningful; swap in a real model
  for production.
* :class:`OpenAIEmbeddingProvider` — thin wrapper over OpenAI's
  ``/v1/embeddings`` endpoint. HTTP client is injected so tests never touch
  the network (same injected-runner pattern as ``gh/client.py``).
* :func:`cosine_similarity` / :func:`rerank` — pure-function blending of FTS
  and embedding scores into a single ordered list of candidates.
* :class:`EmbeddingCache` — SQLite-backed ``(provider, text_hash) → vector``
  cache keyed on ``sha256`` so expensive embeddings are paid for exactly once.

The integration into ``EpisodicStore`` / ``MemoryManager`` is deliberately
separate — this module ships only the building blocks.
"""

from __future__ import annotations

import hashlib
import math
import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from maxwell_daemon.backends.base import BackendUnavailableError
from maxwell_daemon.contracts import require

__all__ = [
    "EmbeddingCache",
    "EmbeddingProvider",
    "EmbeddingResult",
    "OpenAIEmbeddingProvider",
    "StubEmbeddingProvider",
    "cosine_similarity",
    "hash_text",
    "rerank",
]


_HASH_PREFIX = 16  # hex chars — 64 bits of collision space is plenty for a cache key.


def hash_text(text: str) -> str:
    """sha256 hex prefix used as the cache key for a piece of text.

    Short enough to keep SQLite indices small, wide enough (64 bits) that
    collisions across a single user's episode corpus are vanishingly rare.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:_HASH_PREFIX]


@dataclass(slots=True, frozen=True)
class EmbeddingResult:
    """One embedding + the text hash it encodes (for cache key reuse)."""

    text_hash: str
    vector: tuple[float, ...]
    dimensions: int
    provider_name: str


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Structural — anything with ``name`` + ``dimensions`` + ``embed_batch`` works."""

    name: str

    @property
    def dimensions(self) -> int: ...

    async def embed_batch(self, texts: tuple[str, ...]) -> tuple[EmbeddingResult, ...]: ...


class StubEmbeddingProvider:
    """Deterministic local provider — tests and air-gapped operation.

    Produces a fixed-dimensional vector whose components are derived from a
    streaming sha256 of ``text``. Fast, zero cost, no network. *Not*
    semantically meaningful — callers should swap in ``sentence-transformers``
    or ``OpenAIEmbeddingProvider`` in prod.

    Determinism makes this provider useful as the default in tests: the same
    text always yields the same vector across processes, so golden assertions
    don't churn.
    """

    name: str = "stub"

    def __init__(self, dimensions: int = 64) -> None:
        require(
            dimensions >= 16,
            f"StubEmbeddingProvider: dimensions must be >= 16, got {dimensions}",
        )
        self._dim = dimensions

    @property
    def dimensions(self) -> int:
        return self._dim

    async def embed_batch(self, texts: tuple[str, ...]) -> tuple[EmbeddingResult, ...]:
        return tuple(self._embed_one(t) for t in texts)

    def _embed_one(self, text: str) -> EmbeddingResult:
        # Stream sha256 blocks until we have enough bytes (4 per float component).
        needed = self._dim * 4
        buf = bytearray()
        seed = text.encode("utf-8")
        counter = 0
        while len(buf) < needed:
            h = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
            buf.extend(h)
            counter += 1

        # Map each 4-byte chunk to [-1, 1) then L2-normalise so cosine == dot.
        components: list[float] = []
        for i in range(self._dim):
            chunk = int.from_bytes(buf[i * 4 : (i + 1) * 4], "big", signed=False)
            components.append((chunk / 0xFFFFFFFF) * 2.0 - 1.0)

        norm = math.sqrt(sum(c * c for c in components))
        if norm > 0:
            components = [c / norm for c in components]

        return EmbeddingResult(
            text_hash=hash_text(text),
            vector=tuple(components),
            dimensions=self._dim,
            provider_name=self.name,
        )


class OpenAIEmbeddingProvider:
    """Wraps OpenAI's ``/v1/embeddings`` endpoint.

    The HTTP client is *injected* (default: ``openai.AsyncOpenAI``) so tests
    can swap in a stub without mocking the network stack. The injected client
    only needs an ``embeddings.create(input=..., model=..., dimensions=...)``
    coroutine returning an object whose ``.data`` is a sequence of objects
    with an ``.embedding`` list — i.e. the shape the OpenAI SDK already
    returns. Anything matching that duck-type works.
    """

    name: str = "openai"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "text-embedding-3-small",
        http_client: Any = None,
        dimensions_override: int | None = None,
    ) -> None:
        if http_client is None:
            key = api_key or os.environ.get("OPENAI_API_KEY")
            if not key:
                raise BackendUnavailableError(
                    "OpenAIEmbeddingProvider: OPENAI_API_KEY not set and no http_client injected"
                )
            # Import lazily so the stub path never pays the openai import cost.
            import openai

            http_client = openai.AsyncOpenAI(api_key=key)

        self._client = http_client
        self._model = model
        self._dimensions_override = dimensions_override
        # Default dimension for text-embedding-3-small; overridable via
        # `dimensions_override` (OpenAI supports truncation on this model).
        self._dim = dimensions_override if dimensions_override is not None else 1536

    @property
    def dimensions(self) -> int:
        return self._dim

    @property
    def model(self) -> str:
        return self._model

    async def embed_batch(self, texts: tuple[str, ...]) -> tuple[EmbeddingResult, ...]:
        if not texts:
            return ()

        kwargs: dict[str, Any] = {"model": self._model, "input": list(texts)}
        if self._dimensions_override is not None:
            kwargs["dimensions"] = self._dimensions_override

        response = await self._client.embeddings.create(**kwargs)

        results: list[EmbeddingResult] = []
        for text, item in zip(texts, response.data, strict=True):
            vector = tuple(float(x) for x in item.embedding)
            results.append(
                EmbeddingResult(
                    text_hash=hash_text(text),
                    vector=vector,
                    dimensions=len(vector),
                    provider_name=self.name,
                )
            )
        return tuple(results)


def cosine_similarity(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """Cosine similarity in ``[-1, 1]``. Zero-length vectors → 0.

    Raises ``ValueError`` if the two vectors differ in dimension — that's
    always a bug, never something the caller wants to silently paper over.
    """
    if len(a) != len(b):
        raise ValueError(f"cosine_similarity: dimension mismatch ({len(a)} vs {len(b)})")
    if not a:
        return 0.0

    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        norm_a += x * x
        norm_b += y * y

    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


def rerank(
    candidates: tuple[tuple[str, float], ...],
    *,
    query_vec: tuple[float, ...],
    candidate_vecs: tuple[tuple[float, ...], ...],
    fts_weight: float = 0.3,
    embedding_weight: float = 0.7,
) -> tuple[int, ...]:
    """Return indices into ``candidates`` sorted by hybrid score (descending).

    Hybrid = ``fts_weight * fts_norm + embedding_weight * cosine``, where
    ``fts_norm`` linearly rescales the candidate FTS scores into ``[0, 1]``
    (so the two components are on the same footing regardless of BM25's raw
    magnitude).

    Design-by-contract:

    * ``fts_weight`` and ``embedding_weight`` must be non-negative.
    * Their sum must be strictly positive — a zero-weight rerank is a no-op
      disguised as a scoring call, almost always a bug.
    * ``candidates`` and ``candidate_vecs`` must be the same length.
    """
    require(fts_weight >= 0.0, "rerank: fts_weight must be non-negative")
    require(embedding_weight >= 0.0, "rerank: embedding_weight must be non-negative")
    require(
        fts_weight + embedding_weight > 0.0,
        "rerank: fts_weight + embedding_weight must be > 0",
    )
    require(
        len(candidates) == len(candidate_vecs),
        "rerank: candidates and candidate_vecs must be the same length",
    )

    if not candidates:
        return ()

    fts_scores = [score for _, score in candidates]
    lo = min(fts_scores)
    hi = max(fts_scores)
    span = hi - lo
    if span > 0.0:  # noqa: SIM108 — keep explanatory comment on the else branch
        fts_norm = [(s - lo) / span for s in fts_scores]
    else:
        # All equal → FTS contributes no ranking signal; zero it so embedding
        # weight alone decides order (stable on ties).
        fts_norm = [0.0 for _ in fts_scores]

    scored: list[tuple[float, int]] = []
    for idx, vec in enumerate(candidate_vecs):
        cos = cosine_similarity(query_vec, vec)
        blended = fts_weight * fts_norm[idx] + embedding_weight * cos
        scored.append((blended, idx))

    # Python's sort is stable — ties preserve the input order so repeated
    # scores map to ascending indices, which is what tests assert.
    scored.sort(key=lambda sv: (-sv[0], sv[1]))
    return tuple(idx for _, idx in scored)


_CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS embedding_cache (
    provider TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    vector BLOB NOT NULL,
    dimensions INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (provider, text_hash)
);
CREATE INDEX IF NOT EXISTS idx_embedding_cache_created_at
    ON embedding_cache(created_at);
"""


class EmbeddingCache:
    """SQLite-backed cache keyed by ``(provider_name, text_hash)``.

    Same connection-per-call pattern as :class:`CostLedger` — callers need
    only one ``EmbeddingCache`` per process, and the lock serialises writes
    so WAL-mode readers never see a half-populated row.
    """

    def __init__(self, db_path: Path) -> None:
        self._path = Path(db_path).expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(_CACHE_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
        finally:
            conn.close()

    def get(self, *, provider: str, text_hash: str) -> tuple[float, ...] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT vector, dimensions FROM embedding_cache
                WHERE provider = ? AND text_hash = ?
                """,
                (provider, text_hash),
            ).fetchone()
        if row is None:
            return None
        return _decode_vector(row["vector"], row["dimensions"])

    def put(self, result: EmbeddingResult) -> None:
        now = datetime.now(timezone.utc).isoformat()
        blob = _encode_vector(result.vector)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO embedding_cache
                    (provider, text_hash, vector, dimensions, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(provider, text_hash) DO UPDATE SET
                    vector = excluded.vector,
                    dimensions = excluded.dimensions,
                    created_at = excluded.created_at
                """,
                (
                    result.provider_name,
                    result.text_hash,
                    blob,
                    result.dimensions,
                    now,
                ),
            )

    def prune_older_than(self, cutoff_ts: datetime) -> int:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM embedding_cache WHERE created_at < ?",
                (cutoff_ts.isoformat(),),
            )
            return cursor.rowcount or 0


def _encode_vector(vector: tuple[float, ...]) -> bytes:
    # Little-endian float64 — portable and avoids numpy as a dependency.
    import struct

    return struct.pack(f"<{len(vector)}d", *vector)


def _decode_vector(blob: bytes, dimensions: int) -> tuple[float, ...]:
    import struct

    return tuple(struct.unpack(f"<{dimensions}d", blob))

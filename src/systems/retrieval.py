"""Embedding + cosine retrieval, shared by every system that retrieves.

Extracted from System A when System B was built. B must retrieve **exactly**
the way A does -- same embedding model, same k, same cosine similarity, same
chronological re-ordering -- or the comparison stops isolating the thing it is
supposed to isolate (what is stored) and starts measuring how it is accessed.
Sharing the code is the only way to guarantee that: a copy would drift.

A "document" here is anything with an `index` (its position in the corpus, used
for the chronological re-ordering) and a `render()` (the text that is embedded
and that reaches the model). System A's documents are windows of turns; System
B's are wiki page fragments. This module does not care which.

No vector database. One conversation is a few hundred documents, so cosine
similarity over a numpy array is instant, and every line of it can be explained
in a viva.
"""
from __future__ import annotations

from typing import Protocol, TypeVar

import numpy as np

import config
from src.llm import LLMClient


class Document(Protocol):
    """What VectorIndex needs from whatever it is indexing."""
    index: int

    def render(self) -> str: ...


D = TypeVar("D", bound=Document)


def unit_rows(matrix: np.ndarray) -> np.ndarray:
    """L2-normalise each row so dot products are cosine similarities."""
    norms = np.linalg.norm(matrix, axis=-1, keepdims=True)
    return matrix / np.clip(norms, 1e-12, None)


class VectorIndex:
    """Embed a corpus once; retrieve the top k per question."""

    def __init__(self, client: LLMClient, model: str = config.EMBEDDING_MODEL):
        self.client = client
        self.model = model
        self.documents: list = []
        self.matrix: np.ndarray | None = None

    def build(self, documents: list) -> dict:
        """Embed every document. Returns the cost/size stats of doing so."""
        self.documents = documents
        texts = [d.render() for d in documents]
        result = self.client.embed(texts, model=self.model)
        self.matrix = unit_rows(np.asarray(result.vectors, dtype=np.float32))
        return {
            "prompt_tokens": result.prompt_tokens,
            "cost_usd": result.cost_usd,
            "latency_s": result.latency_s,
            "n_documents": len(documents),
            "embeddings_from_api": result.api_texts,
            "embeddings_from_cache": result.cached_texts,
        }

    def retrieve(self, query: str, k: int) -> tuple[list, list[float], dict]:
        """Embed the query, score every document, return the top k in
        CHRONOLOGICAL order.

        Relevance order scrambles the timeline, which hurts temporal questions
        for a reason that has nothing to do with retrieval quality, so the
        selected documents are re-sorted by their corpus index before they are
        handed back.
        """
        if self.matrix is None:
            raise RuntimeError("VectorIndex.retrieve() called before build()")

        result = self.client.embed([query], model=self.model)
        vector = unit_rows(np.asarray(result.vectors, dtype=np.float32))[0]

        # Rows and query are unit vectors, so the dot product IS cosine
        # similarity -- no extra API call, no library, one matrix multiply.
        scores = self.matrix @ vector
        top = np.argsort(-scores)[:k]
        ordered = sorted(top, key=lambda i: self.documents[i].index)

        cost = {"embed_cost_usd": result.cost_usd,
                "embed_latency_s": result.latency_s,
                "embed_cached": result.cached_texts > 0}
        return ([self.documents[i] for i in ordered],
                [float(scores[i]) for i in ordered],
                cost)

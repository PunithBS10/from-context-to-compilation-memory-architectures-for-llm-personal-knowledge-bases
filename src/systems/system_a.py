"""System A: the RAG baseline.

Where System L sends the whole conversation on every question, System A chunks
the conversation, embeds the chunks once, and retrieves only the few most
relevant per question. The model sees the retrieved chunks and nothing else --
retrieval happens in our code, never in the model.

Held constant with System L on purpose: the answering prompt (word for word,
including the abstention rule), the answering model, the judge, the
temperature, the dataset and the metrics. The ONLY difference between L and A
is what text reaches the model. Anything else silently invalidates the
comparison.

Two design points that would quietly corrupt the results if missed:

* **Chunks must carry their session date.** L got dates free from the
  transcript headers. Bare turns would strip them, and every temporal question
  would become structurally unanswerable -- A would score near zero on
  temporal for a formatting reason rather than a retrieval failure, unfairly
  flattering the systems that come later.
* **Retrieved chunks are re-ordered chronologically** before they enter the
  prompt. Relevance order scrambles the timeline, which again hurts temporal
  questions for a reason that has nothing to do with retrieval quality.

No vector database. One conversation is ~150 chunks, so cosine similarity over
a numpy array is instant, and every line of it can be explained in a viva.
"""
from __future__ import annotations

import numpy as np

import config
from src.llm import LLMClient, count_tokens
from src.systems.base import Answer, MemorySystem
# The prompt is imported, not copied, so it cannot drift away from System L's.
from src.systems.system_l import SYSTEM_PROMPT, USER_TEMPLATE


class Chunk:
    """A window of consecutive turns, rendered with its session date."""

    def __init__(self, turns: list, index: int):
        self.index = index
        self.turns = turns
        self.dia_ids = [t.dia_id for t in turns if t.dia_id]
        self.session_id = turns[0].session_id
        self.timestamp = turns[0].timestamp

    def render(self) -> str:
        session_number = self.session_id.replace("session_", "")
        header = f"[Session {session_number}"
        if self.timestamp:
            header += f" - {self.timestamp}"
        header += "]"
        return header + "\n" + "\n".join(t.render() for t in self.turns)


def build_chunks(conversation, turns_per_chunk: int = config.CHUNK_TURNS,
                 overlap: int = config.CHUNK_OVERLAP) -> list[Chunk]:
    """Sliding window over turns, never crossing a session boundary.

    Chunks stay within one session so a chunk cannot carry two different dates
    under a single header, which would make its timestamp a lie.
    """
    step = max(1, turns_per_chunk - overlap)
    chunks: list[Chunk] = []
    for session in conversation.sessions:
        turns = session.turns
        if not turns:
            continue
        start = 0
        while start < len(turns):
            window = turns[start:start + turns_per_chunk]
            if window:
                chunks.append(Chunk(window, len(chunks)))
            if start + turns_per_chunk >= len(turns):
                break
            start += step
    return chunks


class SystemA(MemorySystem):
    name = "A"

    def __init__(self, client: LLMClient, model: str = config.ANSWER_MODEL,
                 k: int = config.RAG_K):
        self.client = client
        self.model = model
        self.k = k
        self.chunks: list[Chunk] = []
        self.chunk_texts: list[str] = []
        self.matrix: np.ndarray | None = None
        self.speakers = ""
        self._ingest = {"ingest_prompt_tokens": 0, "ingest_completion_tokens": 0,
                        "ingest_cost_usd": 0.0, "ingest_latency_s": 0.0,
                        "ingest_chunks": 0, "ingest_embeddings_from_api": 0,
                        "ingest_embeddings_from_cache": 0}

    # --- ingest -----------------------------------------------------------
    def ingest(self, conversation) -> None:
        self.speakers = (" and ".join(conversation.speakers)
                         if conversation.speakers else "two people")
        self.chunks = build_chunks(conversation)
        self.chunk_texts = [c.render() for c in self.chunks]

        result = self.client.embed(self.chunk_texts)
        self.matrix = _unit_rows(np.asarray(result.vectors, dtype=np.float32))

        self._ingest.update({
            "ingest_prompt_tokens": result.prompt_tokens,
            "ingest_cost_usd": result.cost_usd,
            "ingest_latency_s": result.latency_s,
            "ingest_chunks": len(self.chunks),
            "ingest_embeddings_from_api": result.api_texts,
            "ingest_embeddings_from_cache": result.cached_texts,
        })

    def ingest_stats(self) -> dict:
        return dict(self._ingest)

    # --- retrieval --------------------------------------------------------
    def retrieve(self, question: str) -> tuple[list[Chunk], list[float], dict]:
        """Embed the question, score every chunk, return the top k in
        chronological order."""
        result = self.client.embed([question])
        query = _unit_rows(np.asarray(result.vectors, dtype=np.float32))[0]

        # Rows and query are unit vectors, so the dot product IS cosine
        # similarity -- no API call, no library, one matrix multiply.
        scores = self.matrix @ query
        top = np.argsort(-scores)[:self.k]
        ordered = sorted(top, key=lambda i: self.chunks[i].index)   # chronological

        cost = {"embed_cost_usd": result.cost_usd, "embed_latency_s": result.latency_s,
                "embed_cached": result.cached_texts > 0}
        return ([self.chunks[i] for i in ordered],
                [float(scores[i]) for i in ordered],
                cost)

    def build_prompt(self, question: str) -> tuple[str, str]:
        """The exact prompt pair answer() would send, for --dry-run and
        --dump-prompts."""
        chunks, _, _ = self.retrieve(question)
        return SYSTEM_PROMPT, USER_TEMPLATE.format(
            speakers=self.speakers,
            transcript="\n\n".join(c.render() for c in chunks),
            question=question,
        )

    # --- answer -----------------------------------------------------------
    def answer(self, question: str) -> Answer:
        chunks, scores, retrieval_cost = self.retrieve(question)
        retrieved_text = "\n\n".join(c.render() for c in chunks)

        user_prompt = USER_TEMPLATE.format(
            speakers=self.speakers, transcript=retrieved_text, question=question
        )
        response = self.client.complete(
            model=self.model,
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            max_tokens=config.MAX_ANSWER_TOKENS,
        )

        # dia_ids of everything retrieved, so the runner can check the retrieved
        # chunks against LoCoMo's `evidence` field. That separates "retrieval
        # missed the evidence" from "retrieval found it and the model still got
        # it wrong" -- two very different failures.
        retrieved_dia_ids = [d for c in chunks for d in c.dia_ids]

        return Answer(
            text=response.text,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            latency_s=response.latency_s + retrieval_cost["embed_latency_s"],
            cost_usd=response.cost_usd + retrieval_cost["embed_cost_usd"],
            cached=response.cached,
            meta={
                "context_overflow": False,   # retrieval bounds the prompt by design
                "k": self.k,
                "n_chunks": len(self.chunks),
                "retrieved_chunk_indices": [c.index for c in chunks],
                "retrieved_dia_ids": retrieved_dia_ids,
                "retrieval_scores": [round(s, 4) for s in scores],
                "retrieval_cost_usd": retrieval_cost["embed_cost_usd"],
                "answer_cost_usd": response.cost_usd,
                "retrieved_tokens": count_tokens(retrieved_text, self.model),
                **response.meta,
            },
        )


def _unit_rows(matrix: np.ndarray) -> np.ndarray:
    """L2-normalise each row so dot products are cosine similarities."""
    norms = np.linalg.norm(matrix, axis=-1, keepdims=True)
    return matrix / np.clip(norms, 1e-12, None)

"""System B: compiled wiki memory.

Systems L and A search raw conversation. System B compiles the conversation
into a written knowledge base of markdown pages (`wiki.py`) and searches
*those*. The hypothesis is that pre-digested facts retrieve better than raw
chat because the noise is gone:

    System A retrieves  "Melanie: Thanks Caroline! The event was really
                         thought-provoking..."
    System B retrieves  "- Melanie ran a charity race raising awareness for
                         mental health - 25 May 2023"

Held constant with System A, deliberately and without exception:

* the **same retrieval** -- same embedding model, same k, same cosine
  similarity, same chronological re-ordering, through the same `VectorIndex`
  object rather than a copy of it;
* the **same answering prompt**, imported from System L word for word,
  including the abstention rule;
* the same answering model, judge, temperature and metrics.

A and B therefore differ in exactly one thing: what is stored. (The prompt's
wording -- "Below is the full conversation" -- is a slightly odd fit for a page
of compiled facts, and that is a deliberate cost: rewording it for B would
introduce a second difference and make any accuracy gap uninterpretable.)

Two things System B has that A does not, both reported separately:

* **Ingestion is expensive.** Compiling costs one LLM call per session, so B's
  total cost of ownership is compile-once plus retrieve-many. `ingest_stats()`
  carries it and the runner reports it apart from answer cost.
* **Ingestion can be wrong.** A hallucinated fact is written into the wiki and
  becomes the memory, so every later question touching it inherits the error.
  That is the failure mode this thesis exists to address, and it is measured by
  `scripts/audit_wiki.py` rather than assumed away.
"""
from __future__ import annotations

import config
from src.llm import LLMClient, count_tokens
from src.systems.base import Answer, MemorySystem
from src.systems.retrieval import VectorIndex
# Prompts imported, never copied, so L, A and B cannot drift apart.
from src.systems.system_l import SYSTEM_PROMPT, USER_TEMPLATE
from src.systems.wiki import (BUILD_FINGERPRINT, Wiki, WikiChunk, WikiCompiler,
                              build_wiki_chunks)


class SystemB(MemorySystem):
    name = "B"

    def __init__(self, client: LLMClient, model: str = config.ANSWER_MODEL,
                 k: int = config.RAG_K,
                 extraction_model: str = config.WIKI_EXTRACTION_MODEL,
                 wiki_dir=None, reuse_wiki: bool | None = None,
                 verbose: bool = False):
        self.client = client
        self.model = model
        self.k = k
        self.extraction_model = extraction_model
        self.wiki_dir = wiki_dir or config.WIKI_DIR
        self.reuse_wiki = config.WIKI_REUSE if reuse_wiki is None else reuse_wiki
        self.verbose = verbose

        self.wiki: Wiki | None = None
        self.chunks: list[WikiChunk] = []
        self.index = VectorIndex(client)
        self.speakers = ""
        self.session_dia_ids: dict[int, list[str]] = {}
        self._ingest: dict = {}

    # --- ingest -----------------------------------------------------------
    def ingest(self, conversation) -> None:
        self.speakers = (" and ".join(conversation.speakers)
                         if conversation.speakers else "two people")
        # Which raw turns each session contains. Needed for evidence recall:
        # compiled facts are not turns, so a retrieved fact is mapped back to
        # the session it was extracted from (see `answer`).
        self.session_dia_ids = {
            session.index: [t.dia_id for t in session.turns if t.dia_id]
            for session in conversation.sessions
        }

        self.wiki = self._load_or_compile(conversation)
        self.wiki.write(self.wiki_dir)      # inspectable evidence, and what the audit reads

        self.chunks = build_wiki_chunks(self.wiki)
        embed = self.index.build(self.chunks)

        stats = dict(self.wiki.stats)
        self._ingest = {
            # The base-class contract: total cost of building this memory,
            # compilation AND embedding, so B's ingestion is comparable with
            # A's (which is embedding only).
            "ingest_prompt_tokens": stats.get("compile_prompt_tokens", 0) + embed["prompt_tokens"],
            "ingest_completion_tokens": stats.get("compile_completion_tokens", 0),
            "ingest_cost_usd": stats.get("compile_cost_usd", 0.0) + embed["cost_usd"],
            "ingest_latency_s": stats.get("compile_latency_s", 0.0) + embed["latency_s"],
            "ingest_chunks": embed["n_documents"],
            "ingest_embeddings_from_api": embed["embeddings_from_api"],
            "ingest_embeddings_from_cache": embed["embeddings_from_cache"],
            # Wiki-specific reporting: size, compression, and the compiler's
            # own cost separated from the embedding cost.
            "wiki_from_disk": self.wiki.from_disk,
            "wiki_dir": str(self.wiki_dir / self.wiki.conv_id),
            "wiki_extraction_model": self.wiki.extraction_model,
            "wiki_build_fingerprint": self.wiki.build_fingerprint,
            "embed_cost_usd": embed["cost_usd"],
            **stats,
        }

    def _load_or_compile(self, conversation) -> Wiki:
        """Reuse the wiki on disk when it was built the same way, else compile.

        Same principle as the response cache: a wiki built by a different
        model, extraction prompt or compiler version is a different experiment,
        so it is rebuilt rather than silently reused. A reused wiki reports the cost of
        the ORIGINAL compilation (recorded in its manifest), flagged with
        `wiki_from_disk`, so nothing is reported as free that was not free.
        """
        if self.reuse_wiki:
            existing = Wiki.load(self.wiki_dir, conversation.conv_id)
            if existing is not None:
                fresh_enough = (
                    existing.extraction_model == self.extraction_model
                    and existing.build_fingerprint == BUILD_FINGERPRINT
                    and existing.stats.get("sessions_compiled") == len(
                        [s for s in conversation.sessions if s.turns])
                )
                if fresh_enough:
                    if self.verbose:
                        print(f"  [wiki] {conversation.conv_id}: reusing "
                              f"{existing.stats.get('n_facts')} facts on "
                              f"{existing.stats.get('n_pages')} pages from disk")
                    return existing
                print(f"  [wiki] {conversation.conv_id}: on-disk wiki was built with a "
                      f"different model or prompt - recompiling")

        compiler = WikiCompiler(self.client, model=self.extraction_model,
                                verbose=self.verbose)
        return compiler.compile(conversation)

    def ingest_stats(self) -> dict:
        return dict(self._ingest)

    # --- retrieval --------------------------------------------------------
    def retrieve(self, question: str) -> tuple[list[WikiChunk], list[float], dict]:
        """Identical to System A's: embed the question, score every chunk,
        return the top k in chronological order."""
        return self.index.retrieve(question, self.k)

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

        # Evidence recall, System B's definition. LoCoMo's `evidence` field
        # lists dia_ids of raw turns, and B never retrieves a turn -- it
        # retrieves facts compiled from a session. Each retrieved fact is
        # therefore mapped back to its source session, and the session's turns
        # count as reached. This is a LOOSER measure than System A's
        # turn-level recall and is NOT directly comparable with it: it is an
        # upper bound, answering "did retrieval reach the right part of the
        # conversation?" rather than "did it reach the right turn?". The
        # granularity travels with the record so no reader can conflate them.
        sessions = sorted({i for c in chunks for i in c.session_indices})
        retrieved_dia_ids = [d for i in sessions for d in self.session_dia_ids.get(i, [])]

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
                "retrieved_pages": [c.page_title for c in chunks],
                "retrieved_fact_ids": [fid for c in chunks for fid in c.fact_ids],
                "retrieved_sessions": sessions,
                "retrieved_dia_ids": retrieved_dia_ids,
                "evidence_recall_granularity": "session",
                "retrieval_scores": [round(s, 4) for s in scores],
                "retrieval_cost_usd": retrieval_cost["embed_cost_usd"],
                "answer_cost_usd": response.cost_usd,
                "retrieved_tokens": count_tokens(retrieved_text, self.model),
                **response.meta,
            },
        )

"""System C: compiled wiki memory that remembers where each fact came from.

System B compiles a conversation into a wiki of dated facts and retrieves over
it. A compiled fact there has no memory of its origin. System C attaches, to
every fact, the raw turn or turns it was extracted from:

    System B   - Caroline loved 'Becoming Nicole' by Amy Ellis Nutt — 12 July 2023
    System C   - Caroline loved 'Becoming Nicole' by Amy Ellis Nutt — 12 July 2023 [D7:11]

That is the whole change to the memory. What is done with it at answer time is
run as **two arms**, because "provenance is stored" and "provenance is used"
are different claims and the thesis needs both measured:

* **cite** — the retrieved fact lines carry their tags and nothing else
  changes. Tests whether a model given cited facts grounds or abstains better
  than one given bare facts. The honest risk is that this is a null result.
* **hydrate** — for each retrieved fact, the source turns it cites are pulled
  in verbatim beneath it. This is the arm that tests whether provenance
  recovers System B's compilation loss.

The arm is chosen by configuration, not by a second class: both arms must share
the wiki, the index and the prompt, and two classes would eventually drift into
two experiments.

Held constant with System B, deliberately:

* the **same retrieval** — same embedding model, same k, same cosine
  similarity, same chronological re-ordering, through the same `VectorIndex`;
* the **same answering prompt**, imported from System L, abstention rule
  included, plus exactly one sentence (see `CITATION_NOTE`);
* the same answering model, judge, temperature and metrics.

**Three differences from System B are unavoidable and are declared rather than
hidden**, because a comparison that hides a variable is not a comparison:

1. **The prompt gains one sentence.** An unexplained `[D7:11]` in the context
   is noise, so the system prompt says what the tags are. Identical in both
   arms, and recorded in the results config snapshot.
2. **C sends more tokens than B at equal k** — the tags in the cite arm, the
   hydrated turns in the hydrate arm. That is a real cost of provenance, not
   an artefact, and it is reported per question.
3. **The compiled page structure differs.** Asking the extractor for citations
   changed how it files facts: 233 pages against B's 287, and 27.3% of facts
   on person pages against B's 15.4%, with the fact count essentially
   unchanged (2,925 vs 2,938). A prompt fix was tried and did not restore B's
   shape reliably, so the drift is reported as a measured difference. What
   retrieval actually works over is barely affected — 505 chunks against B's
   469, mean 189 against 167 tokens — because pages over the chunk budget
   split either way.
"""
from __future__ import annotations

import config
from src.llm import LLMClient, count_tokens
from src.systems.base import Answer, MemorySystem
from src.systems.retrieval import VectorIndex
# Prompts imported, never copied, so L, A, B and C cannot drift apart.
from src.systems.system_l import SYSTEM_PROMPT, USER_TEMPLATE
from src.systems.wiki import C_PROFILE, Wiki, WikiChunk, build_wiki_chunks, load_or_compile

ARMS = ("cite", "hydrate")

# The single sentence System C adds to System L's prompt. One sentence, and the
# SAME one in both arms: if the arms had different prompts, an accuracy gap
# between them would be uninterpretable. It is written to be true in both --
# the cite arm simply never shows a source turn, which makes the second clause
# vacuous rather than false.
CITATION_NOTE = (
    "Each fact is followed in square brackets by the ids of the conversation turns "
    "it was taken from, and where those turns are shown they appear beneath the "
    "fact after 'source:'."
)
SYSTEM_PROMPT_C = SYSTEM_PROMPT + "\n" + CITATION_NOTE


class SystemC(MemorySystem):
    name = "C"

    def __init__(self, client: LLMClient, model: str = config.ANSWER_MODEL,
                 k: int = config.RAG_K, arm: str | None = None,
                 extraction_model: str = config.WIKI_EXTRACTION_MODEL,
                 wiki_dir=None, reuse_wiki: bool | None = None,
                 verbose: bool = False):
        self.arm = (arm or config.C_ARM).strip().lower()
        if self.arm not in ARMS:
            raise ValueError(f"System C arm must be one of {ARMS}, got {self.arm!r}")
        # Labels the results file, so the two arms cannot land on top of each
        # other or be told apart only by reading the config snapshot.
        self.variant = self.arm

        self.client = client
        self.model = model
        self.k = k
        self.extraction_model = extraction_model
        self.wiki_dir = wiki_dir or config.WIKI_C_DIR
        self.reuse_wiki = config.WIKI_REUSE if reuse_wiki is None else reuse_wiki
        self.verbose = verbose

        self.wiki: Wiki | None = None
        self.chunks: list[WikiChunk] = []
        self.index = VectorIndex(client)
        self.speakers = ""
        self.session_dia_ids: dict[int, list[str]] = {}
        self.turns_by_dia_id: dict[str, object] = {}
        self._ingest: dict = {}

    # --- ingest -----------------------------------------------------------
    def ingest(self, conversation) -> None:
        self.speakers = (" and ".join(conversation.speakers)
                         if conversation.speakers else "two people")
        # Which raw turns each session holds, for session-level recall.
        self.session_dia_ids = {
            session.index: [t.dia_id for t in session.turns if t.dia_id]
            for session in conversation.sessions
        }
        # The turns themselves, for the hydrate arm and for nothing else: the
        # cite arm must never reach into this, or the two arms stop differing
        # only in prompt assembly.
        self.turns_by_dia_id = {t.dia_id: t for t in conversation.turns if t.dia_id}

        self.wiki = load_or_compile(self.client, conversation, self.wiki_dir,
                                    profile=C_PROFILE, model=self.extraction_model,
                                    reuse=self.reuse_wiki, verbose=self.verbose)
        if not self.wiki.from_disk:
            self.wiki.write(self.wiki_dir)

        # Both arms share ONE index, built over the fact lines as they are
        # stored -- citation tags included, because the tag is part of the
        # fact. That is what makes the arms comparable: they retrieve
        # identically and differ only in what is assembled from the result.
        self.chunks = build_wiki_chunks(self.wiki)
        embed = self.index.build(self.chunks)

        stats = dict(self.wiki.stats)
        self._ingest = {
            "ingest_prompt_tokens": stats.get("compile_prompt_tokens", 0) + embed["prompt_tokens"],
            "ingest_completion_tokens": stats.get("compile_completion_tokens", 0),
            "ingest_cost_usd": stats.get("compile_cost_usd", 0.0) + embed["cost_usd"],
            "ingest_latency_s": stats.get("compile_latency_s", 0.0) + embed["latency_s"],
            "ingest_chunks": embed["n_documents"],
            "ingest_embeddings_from_api": embed["embeddings_from_api"],
            "ingest_embeddings_from_cache": embed["embeddings_from_cache"],
            "wiki_from_disk": self.wiki.from_disk,
            "wiki_dir": str(self.wiki_dir / self.wiki.conv_id),
            "wiki_extraction_model": self.wiki.extraction_model,
            "wiki_build_fingerprint": self.wiki.build_fingerprint,
            "wiki_compiler_version": self.wiki.compiler_version,
            "embed_cost_usd": embed["cost_usd"],
            "c_arm": self.arm,
            **stats,
        }

    def ingest_stats(self) -> dict:
        return dict(self._ingest)

    # --- retrieval --------------------------------------------------------
    def retrieve(self, question: str) -> tuple[list[WikiChunk], list[float], dict]:
        """Identical to System A's and System B's: embed the question, score
        every chunk, return the top k in chronological order."""
        return self.index.retrieve(question, self.k)

    # --- prompt assembly --------------------------------------------------
    def assemble(self, chunks: list[WikiChunk]) -> tuple[str, dict]:
        """Turn the retrieved chunks into the text that goes in the prompt.

        This is the ONLY place the two arms differ.
        """
        if self.arm == "cite":
            text = "\n\n".join(c.render() for c in chunks)
            return text, {"hydrated_turns": 0, "hydrated_tokens": 0, "hydrated_dia_ids": []}

        blocks: list[str] = []
        shown: set[str] = set()
        order: list[str] = []
        source_lines: list[str] = []
        for chunk in chunks:
            lines = [chunk.header]
            for fact in chunk.facts:
                lines.append(fact.render())
                # Capped per fact so one over-cited fact cannot flood the
                # prompt, and deduped across the whole prompt because several
                # retrieved facts often cite the same turn.
                for dia_id in fact.source_dia_ids[:config.HYDRATE_MAX_TURNS]:
                    turn = self.turns_by_dia_id.get(dia_id)
                    if turn is None or dia_id in shown:
                        continue
                    shown.add(dia_id)
                    order.append(dia_id)
                    line = f"  source: {turn.render()}"
                    lines.append(line)
                    source_lines.append(line)
            blocks.append("\n".join(lines))

        return "\n\n".join(blocks), {
            "hydrated_turns": len(order),
            "hydrated_tokens": count_tokens("\n".join(source_lines), self.model),
            "hydrated_dia_ids": order,
        }

    def build_prompt(self, question: str) -> tuple[str, str]:
        """The exact prompt pair answer() would send, for --dry-run and
        --dump-prompts."""
        chunks, _, _ = self.retrieve(question)
        transcript, _ = self.assemble(chunks)
        return SYSTEM_PROMPT_C, USER_TEMPLATE.format(
            speakers=self.speakers, transcript=transcript, question=question)

    # --- answer -----------------------------------------------------------
    def answer(self, question: str) -> Answer:
        chunks, scores, retrieval_cost = self.retrieve(question)
        retrieved_text, hydration = self.assemble(chunks)

        user_prompt = USER_TEMPLATE.format(
            speakers=self.speakers, transcript=retrieved_text, question=question)
        response = self.client.complete(
            model=self.model,
            system_prompt=SYSTEM_PROMPT_C,
            user_prompt=user_prompt,
            max_tokens=config.MAX_ANSWER_TOKENS,
        )

        # Evidence recall at BOTH granularities -- the measurement provenance
        # buys, and the reason System C can be compared with System A for the
        # first time in the project.
        #
        #   turn     the gold evidence turns named by the citations of the
        #            facts retrieved. Exact, and the same measure as System A's
        #            "did the evidence turn reach the model". This is the
        #            headline figure for C.
        #   session  every turn of every session those facts were compiled
        #            from. This is System B's measure -- an upper bound,
        #            answering "did retrieval reach the right part of the
        #            conversation" -- and it is reported so C also connects
        #            back to B's published numbers.
        #   hydrated the subset actually shown to the model as raw text in the
        #            hydrate arm, after the per-fact cap and deduplication.
        #
        # They are different measurements and must never be tabulated as one,
        # which is why the granularity travels on every record.
        turn_ids = sorted({d for c in chunks for f in c.facts for d in f.source_dia_ids})
        sessions = sorted({i for c in chunks for i in c.session_indices})
        session_ids = [d for i in sessions for d in self.session_dia_ids.get(i, [])]

        recall_sets = {"turn": turn_ids, "session": session_ids}
        if self.arm == "hydrate":
            recall_sets["hydrated"] = hydration["hydrated_dia_ids"]

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
                "c_arm": self.arm,
                "n_chunks": len(self.chunks),
                "retrieved_chunk_indices": [c.index for c in chunks],
                "retrieved_pages": [c.page_title for c in chunks],
                "retrieved_fact_ids": [fid for c in chunks for fid in c.fact_ids],
                "retrieved_sessions": sessions,
                # What `evidence_recall` is computed from. Turn-level for C.
                "retrieved_dia_ids": turn_ids,
                "evidence_recall_granularity": "turn",
                "evidence_recall_sets": recall_sets,
                "retrieval_scores": [round(s, 4) for s in scores],
                "retrieval_cost_usd": retrieval_cost["embed_cost_usd"],
                "answer_cost_usd": response.cost_usd,
                "retrieved_tokens": count_tokens(retrieved_text, self.model),
                **hydration,
                **response.meta,
            },
        )

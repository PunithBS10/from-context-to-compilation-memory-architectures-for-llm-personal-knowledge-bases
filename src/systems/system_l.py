"""System L: the long-context baseline.

The whole conversation goes into the prompt on every single question. No
retrieval, no chunking, no memory management. This is the experimental form of
the supervisor's challenge ("if the knowledge base just sits in the context
window, isn't that better?"), and it is deliberately the most expensive system
in the benchmark: quantifying that cost is part of the result.
"""
from __future__ import annotations

import config
from src.llm import LLMClient, count_tokens
from src.systems.base import Answer, MemorySystem

SYSTEM_PROMPT = (
    "You answer questions about a long-running conversation between two people. "
    "You have the complete transcript, split into dated sessions.\n\n"
    "Rules:\n"
    "1. Answer ONLY from the transcript. Never use outside knowledge and never guess.\n"
    "2. If the transcript does not contain the answer, reply exactly: "
    "No information available.\n"
    "3. Answer with a short phrase, not a sentence. Use the exact words from the "
    "transcript where you can.\n"
    "4. For questions about when something happened, give the date."
)

USER_TEMPLATE = (
    "Below is the full conversation between {speakers}. It takes place over "
    "multiple days; the date of each session is written at the start of that "
    "session.\n\n"
    "{transcript}\n\n"
    "Based only on the conversation above, answer the following question with a "
    "short phrase.\n\n"
    "Question: {question}\n"
    "Short answer:"
)


def format_transcript(conversation) -> str:
    """Render sessions with their dates preserved.

    Temporal reasoning is a scored category, so session boundaries and
    timestamps have to survive into the prompt:

        === Session 1 - 1:56 pm on 8 May, 2023 ===
        Caroline: ...
        Melanie: ...
    """
    blocks = []
    for session in conversation.sessions:
        header = f"=== Session {session.index}"
        if session.timestamp:
            header += f" - {session.timestamp}"
        header += " ==="
        lines = [turn.render() for turn in session.turns]
        blocks.append(header + "\n" + "\n".join(lines))
    return "\n\n".join(blocks)


class SystemL(MemorySystem):
    name = "L"

    def __init__(self, client: LLMClient, model: str = config.ANSWER_MODEL):
        self.client = client
        self.model = model
        self.transcript = ""
        self.speakers = ""
        self.transcript_tokens = 0
        self.overflow = False

    def ingest(self, conversation) -> None:
        self.transcript = format_transcript(conversation)
        self.speakers = " and ".join(conversation.speakers) if conversation.speakers else "two people"
        self.transcript_tokens = count_tokens(self.transcript, self.model)

        # Pre-flight overflow check. The spec is explicit: do NOT silently
        # truncate. An overflow is recorded as an outcome and reported, because
        # how often it happens is itself a finding about long-context memory.
        budget = config.MODEL_CONTEXT_LIMIT - config.MAX_ANSWER_TOKENS - config.CONTEXT_SAFETY_MARGIN
        self.overflow = self.transcript_tokens > budget

    def answer(self, question: str) -> Answer:
        if self.overflow:
            return Answer(
                text="",
                meta={
                    "context_overflow": True,
                    "transcript_tokens": self.transcript_tokens,
                    "context_limit": config.MODEL_CONTEXT_LIMIT,
                },
            )

        user_prompt = USER_TEMPLATE.format(
            speakers=self.speakers, transcript=self.transcript, question=question
        )
        response = self.client.complete(
            model=self.model,
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            max_tokens=config.MAX_ANSWER_TOKENS,
        )
        return Answer(
            text=response.text,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            latency_s=response.latency_s,
            cost_usd=response.cost_usd,
            cached=response.cached,
            meta={
                "context_overflow": False,
                "transcript_tokens": self.transcript_tokens,
                **response.meta,
            },
        )

    def build_prompt(self, question: str) -> tuple[str, str]:
        """The exact prompt pair that answer() would send. Used by --dry-run
        and by --dump-prompt, so formatting bugs are visible by eye and for
        free."""
        return SYSTEM_PROMPT, USER_TEMPLATE.format(
            speakers=self.speakers, transcript=self.transcript, question=question
        )

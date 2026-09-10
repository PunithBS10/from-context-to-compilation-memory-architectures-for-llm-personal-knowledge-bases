"""The interface every benchmarked system implements.

L, A, B, C and D all satisfy this contract, so the runner, the judge and the
metrics are written once. Rule: never put system-specific logic in the runner.
If the runner needs to know which system it is running, the abstraction is
wrong.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class Answer:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_s: float = 0.0
    cost_usd: float = 0.0
    cached: bool = False
    meta: dict = field(default_factory=dict)   # per-system extras


class MemorySystem(ABC):
    """One conversation's worth of memory."""

    name: str = "base"
    # A system run in more than one configuration labels its results with it,
    # so the runner can keep the runs apart without knowing what the
    # configurations are. System C sets it to its arm; everything else leaves
    # it empty.
    variant: str = ""

    @abstractmethod
    def ingest(self, conversation) -> None:
        """Consume the full conversation log and build whatever memory this
        system uses."""

    @abstractmethod
    def answer(self, question: str) -> Answer:
        """Answer a question using only what ingest() stored."""

    def ingest_stats(self) -> dict:
        """Cost/size of building the memory, for systems where ingestion is
        itself expensive (B, C, D compile the wiki with LLM calls). Systems
        with free ingestion, like L, return zeros."""
        return {"ingest_prompt_tokens": 0, "ingest_completion_tokens": 0,
                "ingest_cost_usd": 0.0, "ingest_latency_s": 0.0}

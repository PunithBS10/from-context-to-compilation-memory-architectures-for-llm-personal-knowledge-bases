# Implementation Spec — System L (Long-Context Baseline)

**Read `00_PROJECT_STATUS.md` in the thesis root first for full research context.**

This spec is the handover document for building the first system of the thesis. It is written to be actionable by a coding agent or by hand.

---

## 1. Why System L, and why first

The thesis benchmarks five systems that each add one capability:

| System | What it is |
|---|---|
| **L** | **Long-context baseline — entire knowledge base placed directly in the context window** |
| A | Plain RAG baseline (retrieve chunks, then answer) |
| B | Compiled LLM-Wiki memory (OKF format) |
| C | Wiki + provenance tracking |
| D | Wiki + provenance-driven selective forgetting |

**System L is built first for two reasons:**

1. **It answers the supervisor's challenge directly.** He asked: *"models now have very large context windows — if the knowledge base just sits in context, wouldn't that be better than your compiled-memory idea?"* System L makes that hypothesis an experimental baseline instead of an argument.
2. **It is the simplest system, so it forces the evaluation harness into existence.** L has no retrieval, no chunking, no memory management — just *load conversation → put in prompt → ask question → score answer*. Everything hard about L (dataset loading, prompting, judging, metrics, result logging) is **shared infrastructure that A, B, C and D all reuse**.

Build the harness well here and the remaining four systems are mostly swapping one class.

---

## 2. Architecture — the shared interface

This is the most important design decision. Every system implements the same interface so the runner, evaluator and metrics are written once.

```python
# src/systems/base.py
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

@dataclass
class Answer:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_s: float = 0.0
    meta: dict = field(default_factory=dict)   # per-system extras

class MemorySystem(ABC):
    """One conversation's worth of memory."""

    @abstractmethod
    def ingest(self, conversation) -> None:
        """Consume the full conversation log and build whatever memory this system uses."""

    @abstractmethod
    def answer(self, question: str) -> Answer:
        """Answer a question using only what ingest() stored."""
```

How each system will later implement it:

- **L** — `ingest` stores the raw conversation text; `answer` puts all of it in the prompt.
- **A** — `ingest` chunks + embeds into a vector store; `answer` retrieves top-k then generates.
- **B** — `ingest` compiles the conversation into an OKF markdown wiki; `answer` reads the wiki.
- **C** — as B, plus provenance metadata in YAML frontmatter.
- **D** — as C, plus a forgetting pass driven by source trust.

**Rule: never write system-specific logic into the runner.** If the runner needs to know which system it is running, the abstraction is wrong.

---

## 3. Dataset — LoCoMo

**Source:** `github.com/snap-research/locomo` → `data/locomo10.json`

**Important: inspect the real file before coding against it.** Print the top-level keys and one full sample. The expected shape is roughly:

- A list of conversation records.
- Each record contains a multi-session conversation (`session_1`, `session_2`, … with speaker/text turns, and session timestamps) plus QA annotations.
- Each QA item has a question, an answer, a **category** (single-hop, multi-hop, temporal, open-domain, adversarial), and evidence references.

Write the loader to **normalise** whatever the real schema is into a clean internal form, so no downstream code touches raw JSON:

```python
@dataclass
class Turn:
    speaker: str
    text: str
    session_id: str
    timestamp: str | None

@dataclass
class QAItem:
    question: str
    answer: str
    category: str          # normalise to readable names, not raw ints
    evidence: list | None

@dataclass
class Conversation:
    conv_id: str
    turns: list[Turn]
    qa: list[QAItem]
```

**Adversarial category:** LoCoMo includes questions that are *not* answerable from the conversation. The correct behaviour is abstention ("I don't know"), not a guess. Keep this category separate in results — it is directly relevant to the thesis, since a system that hallucinates confidently is exactly the failure provenance is meant to catch.

---

## 4. System L implementation

```python
class SystemL(MemorySystem):
    def __init__(self, client, model: str):
        ...

    def ingest(self, conversation):
        # Format ALL turns into one transcript string. Keep session boundaries
        # and timestamps — temporal questions depend on them.
        self.transcript = format_transcript(conversation.turns)

    def answer(self, question):
        # Single API call: full transcript + question.
        # Measure tokens and wall-clock latency.
```

**Transcript formatting** — preserve structure, since temporal reasoning is a scored category:

```
=== Session 1 — 2023-05-14 ===
Alice: ...
Bob: ...

=== Session 2 — 2023-06-02 ===
...
```

**Answering prompt requirements:**

- Instruct the model to answer **only** from the transcript.
- Instruct it to say it does not know when the transcript does not contain the answer (needed for the adversarial category — do not skip this or L will look artificially bad *and* the abstention finding will be lost).
- Ask for a short, direct answer — long prose is harder to judge.
- Keep temperature at 0 for reproducibility.

**Context overflow:** if a conversation exceeds the model's window, do **not** silently truncate. Record it as a `context_overflow` outcome and report how often it happens — that is itself a finding about the limits of the long-context approach.

---

## 5. Evaluation

### 5.1 Accuracy — LLM-as-judge

Free-text answers cannot be exact-matched. Use an LLM judge (this is what the Mem0/LoCoMo line of work does, so results stay comparable).

- Judge input: question, ground-truth answer, system answer.
- Judge output: **binary correct / incorrect**, plus a one-line reason.
- Use a fixed judge model and temperature 0; record the judge model name in results.
- Judge prompt must allow semantically equivalent phrasing to count as correct.
- Also compute **F1 / token overlap** as a cheap secondary metric — useful as a sanity check that the judge is not drifting.

**Validate the judge.** Hand-label ~30 answers yourself and check agreement with the judge. Report that agreement figure in the thesis — it defends the evaluation method in the viva. Do this once, early.

### 5.2 Cost and latency — do not skip this

Part of the answer to the supervisor's context-window challenge is **efficiency**, not just accuracy. The harness must log, per question:

- `prompt_tokens`, `completion_tokens`
- estimated **cost** (tokens × model price, price in config)
- **latency** in seconds

System L will be the most expensive and slowest by design — every query resends the whole conversation. Quantifying that is a genuine result, so capture it from the very first run.

### 5.3 Reporting

Produce per-run:

- Overall accuracy
- **Accuracy broken down by QA category** (single-hop / multi-hop / temporal / open-domain / adversarial) — the interesting differences between systems will show up per-category, not in the average
- Mean tokens, cost and latency per question
- Count of context overflows

Write results to `results/<system>_<dataset>_<timestamp>.json` (raw, one record per question) plus a small `.csv` summary. Never overwrite previous runs — the thesis needs the history.

---

## 6. Repository structure

```
03_Implementation/
  README.md                     # how to run, in 10 lines
  requirements.txt
  .env.example                  # OPENAI_API_KEY=...
  config.py                     # models, prices, paths, k, temperature
  data/
    locomo/locomo10.json        # git-ignored
  src/
    datasets/locomo.py          # loader → Conversation objects
    systems/base.py             # MemorySystem, Answer
    systems/system_l.py         # long-context baseline
    evaluation/judge.py         # LLM-as-judge
    evaluation/metrics.py       # accuracy, F1, cost, latency aggregation
    runner.py                   # run(system, dataset) → results file
  results/                      # committed — this is thesis evidence
  notebooks/                    # optional, for looking at results
```

**This repo is a required CDS deliverable**, so from the first commit: real README, `requirements.txt` pinned, `.env` git-ignored, no API keys committed, meaningful commit messages.

---

## 7. Configuration

- **Provider:** OpenAI. Put the model name in `config.py`, never hard-coded in system classes — the model will change and every system must be swappable.
- Choose a **large-context** model for L, and record the exact model string and its context limit in results metadata.
- Use the **same** model for all five systems when benchmarking, otherwise the comparison is invalid.
- Keep the **judge** model separate and fixed in config.
- Set `temperature=0` everywhere. Log every config value into the results file so any run can be reproduced.

---

## 8. Practical guardrails

- **Start with 1 conversation, not all 10.** Get the whole loop working end to end on a tiny slice before spending money on a full run. Add a `--limit` flag.
- **Cache API responses** keyed by (model, prompt hash). Reruns during development then cost nothing, and results stay stable.
- **Retry with backoff** on rate limits; fail loudly on everything else. A silent exception that returns an empty answer will quietly corrupt your accuracy numbers.
- Log the **raw prompt** for a few questions and read them. Most early bugs are formatting problems visible instantly by eye.

---

## 9. Definition of done for System L

- [ ] Loader turns `locomo10.json` into `Conversation` objects; sessions and timestamps preserved
- [ ] `SystemL` implements `MemorySystem`
- [ ] Runner executes all QA for all conversations and writes a results file
- [ ] LLM judge scores answers; judge agreement checked against ~30 hand labels
- [ ] Results report overall accuracy, per-category accuracy, tokens, cost, latency, overflow count
- [ ] Full run completed on LoCoMo and committed to `results/`
- [ ] README explains how to reproduce the run

**Then stop.** System A (RAG) is next, reusing everything except `systems/system_l.py`.

---

## 10. What to show at the next supervision meeting

- System L accuracy on LoCoMo, broken down by category
- Cost and latency per question, and total run cost
- Any context overflows
- The point this evidences: *"Here is what pure long-context actually achieves, and what it costs — this is the bar the compiled-memory systems must beat."*

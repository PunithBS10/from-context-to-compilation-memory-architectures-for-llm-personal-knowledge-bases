# From Context to Compilation

Code for the MSc dissertation *From Context to Compilation: A Comparative Study of
Memory Architectures for LLM Personal Knowledge Bases* — Punith Borehalli
Somashekaraiah, GISMA University of Applied Sciences, 2026 (supervisor: Prof. Dr.
Mehran Monavari).

## In one paragraph

A personal AI assistant has to remember past conversations. That memory can be
stored in different ways: keep the whole conversation in the prompt, retrieve raw
chunks of it, or have an LLM **compile** it into a wiki of short facts — optionally
tagging each fact with the turn it came from. This project builds each approach,
runs them on the same benchmark with the same prompts and retrieval code, and
compares them on **accuracy, hallucination, tokens and cost**.

## The systems

| System | Memory representation |
|---|---|
| **L** | Full context — the whole conversation in the prompt |
| **A** | Plain RAG — chunk, embed, retrieve top-k raw turns |
| **B** | Compiled wiki — an LLM turns conversations into short facts; retrieve over those |
| **C** | B + provenance — each fact cites its source turn. *cite* shows the tags; *hydrate* also pulls in the cited turns |
| **D** | C + provenance-driven forgetting — implemented and trialled, not fully evaluated (future work) |

## How it was evaluated

- **Benchmark:** [LoCoMo](https://github.com/snap-research/locomo) — 10 long
  conversations, 1,986 questions. 444 are unanswerable, so answering them counts
  as a hallucination.
- **Answering models:** `gpt-4o-mini`, `gpt-5.6-luna`, and Luna with reasoning on
  (7 configurations × 3 set-ups = 21 full runs).
- **Judge:** `gpt-4.1-mini` (agreement with human labels: Cohen's κ = 0.933).

## Key findings

- **No architecture wins on everything.** The right choice depends on whether you
  care most about accuracy, grounding, cost or answering conservatively.
- **Accuracy:** full context is best on the frontier model (0.675 with reasoning);
  plain RAG at k=10 is best on `gpt-4o-mini` (0.624).
- **Hallucination:** compiled memory roughly halves it versus raw retrieval at the
  same k (e.g. 5.6% for B vs 12.8% for A on `gpt-4o-mini`), at some cost in accuracy.
- **Cost:** full context uses ~20k tokens per question; retrieval uses 1–2k.
- **Provenance:** showing citations alone barely helps; hydrating the cited turns
  recovers accuracy but brings back tokens and hallucination. All 3,035 generated
  citations pointed to real turns.

The full results table is produced by `scripts/compare_runs.py`; all runs are in
`results/`.

## Quick start

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
cp .env.example .env    # add your OpenAI key
curl -L -o data/locomo/locomo10.json https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json

python -m src.runner --system a --k 10 --limit 1 --dry-run   # check prompts, no API calls
python -m src.runner --system a --k 10                       # run one system
python scripts/compare_runs.py results/system_*.json         # compare runs
```

Responses are cached in `.cache/`, so re-runs are free and interrupted runs resume.

## Repository layout

```
config.py        models, prices and fixed settings
src/runner.py    runs a system over the benchmark
src/systems/     one file per system (L, A, B, C, D) + shared retrieval and wiki code
src/evaluation/  LLM judge, F1, aggregation
scripts/         compile/audit wikis, compare runs and compilers
results/         every run and compiled wiki, kept as evidence
```

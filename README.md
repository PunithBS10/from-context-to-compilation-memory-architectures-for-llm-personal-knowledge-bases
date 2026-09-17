# Memory Systems Benchmark

Code for the MSc thesis *Provenance Tracking and Selective Forgetting in
LLM-Compiled Personal Knowledge Bases* (GISMA, 2026).

## The idea

A personal AI assistant needs memory of past conversations. One popular way to
build it is to have an LLM **compile** conversations into a wiki of short facts
(Karpathy's "LLM wiki" pattern; Google's Open Knowledge Format). This has two
weaknesses: a hallucinated fact gets written in as if it were true, and the
memory only ever grows.

This project asks whether two additions help:

1. **Provenance** — every compiled fact records which conversation turn it came
   from.
2. **Forgetting** — facts are removed based on how trustworthy their source is.

To find out, five systems are built, each adding one capability, and all are
benchmarked on the same data so the effect of each layer can be measured on its
own.

| System | What it is | Status |
|---|---|---|
| **L** | Long-context baseline: the whole conversation in the prompt | done |
| **A** | Plain RAG baseline: chunk, embed, retrieve top-k | done |
| **B** | Compiled wiki memory, retrieved over with A's retrieval | done |
| **C** | B + provenance: every fact carries its source turn | done |
| **D** | C + provenance-driven forgetting | in progress |

## Benchmark

**LoCoMo** — 10 long conversations, 1,986 questions with gold answers, five
categories (single-hop, multi-hop, temporal, open-domain, adversarial).
Adversarial questions have no answer in the conversation; the right response is
to decline, so they measure hallucination directly.

All systems use `gpt-4o-mini` to answer and `gpt-4.1-mini` to judge, with the
same prompt and the same retrieval code, so only the memory differs.

## Results so far (LoCoMo, k=10)

| | L | A | B | C-cite | C-hydrate |
|---|---|---|---|---|---|
| Accuracy (judge) | 0.579 | **0.624** | 0.540 | 0.563 | 0.600 |
| Hallucination on unanswerable questions | 28.4% | 18.5% | **7.9%** | 8.8% | 13.7% |
| Tokens per question | 20,849 | **1,748** | 2,248 | 2,369 | 5,240 |
| Cost of a full run | $6.43 | **$0.74** | $1.00 | $1.08 | $1.94 |

In short: plain retrieval beats stuffing everything into the context window.
Compiling to a wiki loses accuracy but roughly halves hallucination. Adding
provenance and following it back to the source turn (C-hydrate) recovers most
of the lost accuracy, at a token cost. Whether forgetting adds anything is what
System D is testing.

### The same seven configurations on a 2026 frontier model

Everything above was re-run on `gpt-5.6-luna` (reasoning off, judge unchanged),
with the wikis recompiled by Luna into `results/wikis_luna/` and
`results/wikis_c_luna/`:

| | L | A k=10 | B k=10 | C-cite | C-hydrate |
|---|---|---|---|---|---|
| Accuracy: gpt-4o-mini / Luna / Luna+reasoning | 0.579 / **0.663** / **0.675** | 0.624 / 0.637 / 0.620 | 0.540 / 0.562 / 0.576 | 0.563 / 0.567 / 0.582 | 0.600 / 0.618 / 0.624 |
| Hallucination: gpt-4o-mini / Luna / Luna+reasoning | 28.4 / 26.6 / **44.6%** | 18.5 / 23.2 / 38.5% | 7.9 / 11.7 / 20.5% | 8.8 / 13.3 / 18.0% | 13.7 / 16.2 / 22.5% |

On the frontier model, full context is the most accurate configuration — at
12× retrieval's tokens and 9× its cost — so the accuracy case for retrieval
over context does not survive the model change. The hallucination ordering
does, on all three answering configurations: more context still means more
hallucination, and compiling to a wiki still roughly halves it. The more
capable the model, the less it declines: with reasoning on, full context is
both the most accurate and the least safe configuration in the project.

Full numbers, interpretation and everything that went wrong are in the
research log kept alongside this repository.

## Run it

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
cp .env.example .env            # put your OpenAI key in it
curl -L -o data/locomo/locomo10.json https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json

python -m src.runner --system a --k 10                 # run one system
python -m src.runner --system c --arm hydrate --k 10   # System C, hydrate arm
python -m src.runner --system l --limit 1 --dry-run    # no API calls, check prompts
python -m src.runner --system b --k 10 --model luna    # same system, frontier model
python scripts/compile_wikis.py --system c --model luna          # compile Luna's wikis first
python scripts/compare_runs.py results/system_*.json   # side-by-side table
python scripts/compare_wikis.py results/wikis results/wikis_luna  # compiler vs compiler
```

Results go to `results/` as JSON plus a summary CSV. Responses are cached in
`.cache/`, so re-runs are free.

## Layout

```
config.py                  models, prices, paths, all fixed settings
src/runner.py              runs a system over the benchmark, writes results
src/llm.py                 OpenAI client with caching and cost accounting
src/datasets/locomo.py     dataset loader
src/systems/               one file per system; base.py is the shared interface
  retrieval.py             embedding + cosine search, shared by A, B, C, D
  wiki.py                  the compiled wiki format and compiler (B, C)
  forgetting.py            the forgetting pass (D)
src/evaluation/            LLM judge, F1, aggregation
scripts/                   compile wikis, audit them, compare runs
results/                   every run and every compiled wiki, committed as evidence
```

`.env` and `data/` are git-ignored. No API key is ever committed.

# Memory Systems Benchmark

Code for the MSc thesis *From Context to Compilation: A Comparative Study of
Memory Architectures for LLM Personal Knowledge Bases* (GISMA, 2026).

## The idea

A personal AI assistant needs memory of past conversations. There are several
ways to hold it: put every conversation in the context window, retrieve raw
text, or have an LLM **compile** the conversations into a wiki of short facts
(Karpathy's "LLM wiki" pattern; Google's Open Knowledge Format), with or
without a record of where each fact came from.

This project builds each of those, benchmarks them on identical data with
identical prompts, and reports what each representation costs and what it
buys — in accuracy, in tokens, and above all in **hallucination**: how often
the system answers a question the conversation cannot answer.

| System | What it is | Status |
|---|---|---|
| **L** | Long-context baseline: the whole conversation in the prompt | done |
| **A** | Plain RAG baseline: chunk, embed, retrieve top-k | done |
| **B** | Compiled wiki memory, retrieved over with A's retrieval | done |
| **C** | B + provenance: every fact carries its source turn (two arms: *cite* shows the tags, *hydrate* also pulls in the cited turns) | done |
| **D** | C + provenance-driven forgetting | built, run on one conversation, moved to future work |

System D is the thesis's original contribution and it did not survive contact
with the data: forgetting by source trust needs sources that differ in
reliability, and a two-person conversation benchmark has none — 96% of facts
are first-hand, and on the first conversation tested the trust score tied on
every contradiction it found. That is reported as a scoping finding, with the
code and the one-conversation run kept as evidence (`src/systems/forgetting.py`,
`results/forgetting/`).

## Benchmark

**LoCoMo** — 10 long conversations, 1,986 questions with gold answers, five
categories (single-hop, multi-hop, temporal, open-domain, adversarial).
Adversarial questions have no answer in the conversation; the right response is
to decline, so they measure hallucination directly.

Every system uses the same answering prompt and the same retrieval code, so
only the memory differs. The judge is `gpt-4.1-mini` throughout. Each
configuration was run on three answering set-ups: `gpt-4o-mini` (the original
runs), `gpt-5.6-luna` with reasoning off (like-for-like on a 2026 frontier
model), and `gpt-5.6-luna` with its default reasoning on (a sensitivity check).
The wikis are compiled by the answering model, so Luna's runs read wikis Luna
compiled.

## Results (LoCoMo, 1,986 questions)

| | L | A k=5 | A k=10 | B k=5 | B k=10 | C-cite | C-hydrate |
|---|---|---|---|---|---|---|---|
| **Accuracy** gpt-4o-mini | 0.579 | 0.609 | **0.624** | 0.529 | 0.540 | 0.563 | 0.600 |
| Luna | **0.663** | 0.611 | 0.637 | 0.534 | 0.562 | 0.567 | 0.618 |
| Luna + reasoning | **0.675** | 0.595 | 0.620 | 0.545 | 0.576 | 0.582 | 0.624 |
| **Hallucination** gpt-4o-mini | 28.4% | 12.8% | 18.5% | **5.6%** | 7.9% | 8.8% | 13.7% |
| Luna | 26.6% | 21.4% | 23.2% | **10.8%** | 11.7% | 13.3% | 16.2% |
| Luna + reasoning | 44.6% | 33.1% | 38.5% | **19.4%** | 20.5% | 18.0% | 22.5% |
| Tokens per question | 20,849 | 958 | 1,748 | 1,232 | 2,248 | 2,369 | 5,240 |
| Cost of a full run (gpt-4o-mini / Luna) | $6.43 / $8.51 | $0.50 / $0.61 | $0.74 / $0.92 | $0.70 / $0.91 | $1.00 / $1.31 | $1.08 / $1.41 | $1.94 / $2.37 |

Hallucination is the share of the 444 unanswerable questions the system
answered anyway. Tokens are for the gpt-4o-mini runs; Luna's are within 1% for
L and A and slightly lower for B and C, whose wikis Luna compiled differently.

What the table says:

- **On the small model, plain retrieval beats full context** (0.624 vs 0.579).
  **On the frontier model it does not**: full context is the most accurate
  configuration, at 12× retrieval's tokens and 9× its cost.
- **More context means more hallucination, on all three answering set-ups.**
  L > A > B every time, and compiling to a wiki roughly halves the
  hallucination of retrieving raw text at the same k. The representation of
  memory decides how much a model hallucinates, whichever model it is.
- **Capability arrives as decisiveness.** Precision when the model chooses to
  answer barely differs between models; what changes is how often it declines.
  Luna declines less than gpt-4o-mini everywhere, and Luna with reasoning
  declines least — so it is at once the most accurate configuration in the
  project and the worst hallucinator in it. The safest configuration on the
  frontier model (19.4%) is still worse than the safest on the cheap one (5.6%).
- **Provenance shown is a null result; provenance followed buys decisiveness.**
  C-cite scores 0.005–0.023 above B. C-hydrate recovers most of B's accuracy loss
  by pulling in the cited turns, and pays for it in hallucination — the same
  trade the model change makes.

The compilers differ too, and `scripts/compare_wikis.py` measures how. Luna
extracts ~55% more facts from the same sessions, files none under a category
name, cites at 1.0000 resolvability with nothing missing, and does not show
the person-page drift that asking `gpt-4o-mini` for citations caused.

Full numbers, interpretation and everything that went wrong are in the
research log kept alongside this repository.

## Run it

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
cp .env.example .env            # put your OpenAI key in it
curl -L -o data/locomo/locomo10.json https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json

python -m src.runner --system a --k 10                    # run one system
python -m src.runner --system c --arm hydrate --k 10      # System C, hydrate arm
python -m src.runner --system l --limit 1 --dry-run       # no API calls, check prompts
python scripts/compile_wikis.py --system c --model luna   # compile a model's wikis before running B or C on it
python -m src.runner --system b --k 10 --model luna       # same system, frontier model (or luna-reasoning)
python scripts/compare_runs.py results/system_*.json      # side-by-side table of runs
python scripts/compare_wikis.py results/wikis results/wikis_luna   # side-by-side table of compilers
python scripts/audit_wiki.py --wiki-dir results/wikis_c citations  # is each fact faithful to the turn it cites?
```

Results go to `results/` as JSON plus a summary CSV; a run on a model other
than the original carries the model in its filename. Responses are cached in
`.cache/`, so re-runs are free and an interrupted run resumes where it stopped.

## Layout

```
config.py                  models and their API quirks, prices, paths, all fixed settings
src/runner.py              runs a system over the benchmark, writes results
src/llm.py                 OpenAI client with caching, retries and cost accounting
src/datasets/locomo.py     dataset loader
src/systems/               one file per system; base.py is the shared interface
  retrieval.py             embedding + cosine search, shared by A, B, C, D
  wiki.py                  the compiled wiki format and the two compilers (B, C)
  forgetting.py            the forgetting pass (D)
src/evaluation/            LLM judge, F1, aggregation
scripts/                   compile wikis, audit them, compare runs and compilers
results/                   every run and every compiled wiki, committed as evidence
  wikis/, wikis_c/         B and C wikis compiled by gpt-4o-mini
  wikis_luna/, wikis_c_luna/, *_luna-reasoning/   the same, compiled by Luna
  forgetting/              System D's one-conversation run
```

`.env` and `data/` are git-ignored. No API key is ever committed.

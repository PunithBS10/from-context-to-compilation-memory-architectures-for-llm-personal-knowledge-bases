# Thesis Implementation — Memory Systems Benchmark

Code for *Provenance Tracking and Selective Forgetting in LLM-Compiled Personal
Knowledge Bases*. Five systems, each adding one capability, benchmarked on the
same conversational-memory data so the effect of each layer is isolated:

| System | What it is | Status |
|---|---|---|
| **L** | Long-context baseline — the whole conversation in the context window | **built** |
| **A** | Plain RAG baseline — chunk, embed, retrieve top-k | **built** |
| B | Compiled LLM-wiki memory (OKF format) | next |
| C | Wiki + provenance tracking | planned |
| D | Wiki + provenance-driven selective forgetting | planned |

Research context: `../00_PROJECT_STATUS.md` and `../00_RESEARCH_LOG.md`.
Design: `IMPLEMENTATION_SPEC_System_L.md`, `IMPLEMENTATION_SPEC_System_A.md`.

## Run it

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt   # Windows
cp .env.example .env                    # then put your OpenAI key in it
curl -L -o data/locomo/locomo10.json https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json
python scripts/inspect_locomo.py                        # confirm the data schema
python -m src.runner --system l --limit 1 --dry-run     # free: prompts + cost estimate
python -m src.runner --system l --limit 1               # one conversation, real calls
python -m src.runner --system l                         # full run (see cost below)
python -m src.runner --system a --k 5                   # RAG baseline
python -m src.runner --system a --k 10                  # retrieval-depth sweep
```

Useful flags: `--limit N` (first N conversations), `--max-questions N`,
`--stratify` (spread a small sample across all QA categories), `--k N`
(retrieval depth), `--dump-prompts N` (print raw prompts before sending),
`--note "..."` (label the run in its results file), `--no-cache`.

## What it does

`runner.py` loads a dataset, hands each conversation to a system's `ingest()`,
asks every question through `answer()`, grades the reply with an LLM judge, and
writes `results/system_<x>_<dataset>_<timestamp>.json` plus a `_summary.csv`.
Runs are never overwritten — the history is thesis evidence.

Every system implements the same `MemorySystem` interface (`src/systems/base.py`),
so the loader, runner, judge and metrics are written once. Adding System A means
registering one factory in `runner.SYSTEMS`; nothing in the runner is
system-specific.

## Results reported

Per run, overall and broken down by QA category:

- **judge accuracy** — LLM-as-judge, binary, the headline metric
- **F1** — LoCoMo's own scorer, reimplemented faithfully (see below), as a
  comparability anchor and a check that the judge is not drifting
- **abstention rate**, **prompt/completion tokens**, **cost in USD**,
  **latency**, and the **context-overflow count**

Cost and latency are not incidental. Part of the answer to the context-window
challenge is efficiency: System L resends the entire conversation on every
question, and quantifying that is a result in itself.

## The data

LoCoMo (`snap-research/locomo`, `data/locomo10.json`): 10 conversations, 19–32
dated sessions each, 5,882 turns, 1,986 QA items. Not committed — download it
with the command above.

`src/datasets/locomo.py` normalises the real file into `Conversation`/`Turn`/
`QAItem` objects. Three things in the real data would silently corrupt results
if missed, and are handled there:

1. `session_N_date_time` keys exist with **no** matching turn list (conv-26
   declares sessions 20–35 that have no content) — skipped.
2. Sessions must be ordered **numerically**; string-sorting the keys puts
   session_10 before session_2 and scrambles the timeline the temporal category
   depends on.
3. Category 5 (adversarial) items normally have `answer: null` plus an
   `adversarial_answer` holding a plausible-but-wrong answer — the correct
   behaviour is to abstain. **Two** items are the exception and carry a real
   answer, where abstention would be wrong.

Run `python scripts/inspect_locomo.py` to see all of this against the file.

Categories are the official codes: 1 multi-hop, 2 temporal, 3 open-domain,
4 single-hop, 5 adversarial. Adversarial is kept separate in every report: a
system that answers confidently when nothing supports the answer is exactly the
failure provenance is meant to catch.

## Scoring

`src/evaluation/metrics.py` reproduces the reference scorer from
`snap-research/locomo` (`task_eval/evaluation.py`): stemmed token F1 with
articles removed, comma-split multi-answer F1 for multi-hop, the first
`;`-separated alternative for open-domain, and an abstention-phrase check for
adversarial.

`src/evaluation/judge.py` is the LLM judge — binary verdict plus a one-line
reason, at temperature 0, with a separate rubric for adversarial items. Validate
it against your own labels:

```bash
python scripts/validate_judge.py sample results/system_l_locomo_<stamp>.json -n 30
python scripts/validate_judge.py score results/judge_validation_<stamp>.csv
```

That reports agreement and Cohen's kappa. Do it once, early, and quote the
figure in the thesis.

## Results so far

Full LoCoMo, 1,986 questions, `gpt-4o-mini`, judge `gpt-4.1-mini`:

| | L | A (k=5) | A (k=10) |
|---|---|---|---|
| Judge accuracy | 0.579 | 0.609 | **0.624** |
| Mean prompt tokens | 20,849 | **958** | 1,748 |
| Mean latency | 5.71 s | 0.95 s | **0.68 s** |
| Total cost | $6.43 | **$0.50** | $0.74 |
| Evidence recall | n/a | 0.755 | 0.837 |

Retrieval beats brute-force context on this dataset, and does it roughly 13x
cheaper. See `../00_RESEARCH_LOG.md` for the per-category breakdown and the
caveats — particularly judge leniency on temporal questions, which is where
System A's advantage is largest.

## System A notes

Chunks are 4 turns with 1 turn of overlap, never crossing a session boundary,
each rendered with its session date so temporal questions stay answerable.
Retrieved chunks are re-ordered chronologically before entering the prompt.
Similarity is a dot product over L2-normalised embeddings held in a numpy
array — no vector database, because one conversation is ~200 chunks and every
line of this has to be defensible in a viva.

**The answering prompt is imported from `system_l.py`, not copied**, so the two
systems cannot drift apart. The only difference between L and A is what text
reaches the model.

**Evidence recall** uses LoCoMo's `evidence` field to report how often the
retrieved chunks actually contained the answer's source turns. It separates
"retrieval missed it" from "retrieval found it and the model was still wrong".

## Cost and caching

Every API response is cached on disk under `.cache/`, keyed by model, messages,
temperature and max_tokens, so development reruns are free and results stay
stable. `--no-cache` forces live calls. Cached records are flagged in the
results file and excluded from latency means, since a cache hit is not a real
measurement.

Prices live in `config.PRICES` — **check them against current OpenAI pricing
before a full run**, since the cost figures in the results are only as good as
that table. `--dry-run` estimates a run's cost without spending anything.

## Layout

```
config.py                      models, prices, paths, temperature, limits
data/locomo/locomo10.json      git-ignored, downloaded
src/llm.py                     OpenAI client: cache, retry/backoff, accounting
src/datasets/locomo.py         loader -> Conversation objects
src/systems/base.py            MemorySystem, Answer — the shared interface
src/systems/system_l.py        System L
src/systems/system_a.py        System A (chunking, embedding, retrieval)
src/evaluation/judge.py        LLM-as-judge
src/evaluation/metrics.py      F1, aggregation, per-category summaries
src/runner.py                  run(system, dataset) -> results file
scripts/inspect_locomo.py      prints the real dataset schema
scripts/validate_judge.py      judge vs. hand labels, agreement + kappa
results/                       committed — thesis evidence
```

`.env` and `data/` are git-ignored. No key is ever committed.

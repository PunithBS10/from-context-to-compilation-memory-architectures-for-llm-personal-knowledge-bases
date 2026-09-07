# Thesis Implementation — Memory Systems Benchmark

Code for *Provenance Tracking and Selective Forgetting in LLM-Compiled Personal
Knowledge Bases*. Five systems, each adding one capability, benchmarked on the
same conversational-memory data so the effect of each layer is isolated:

| System | What it is | Status |
|---|---|---|
| **L** | Long-context baseline — the whole conversation in the context window | **built** |
| **A** | Plain RAG baseline — chunk, embed, retrieve top-k | **built** |
| **B** | Compiled LLM-wiki memory (OKF format) | **built** |
| C | Wiki + provenance tracking | planned |
| D | Wiki + provenance-driven selective forgetting | planned |


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
python -m src.runner --system b --dry-run               # compiles the wikis, no answers
python -m src.runner --system b --k 5                   # compiled-wiki memory
python scripts/audit_wiki.py stats                      # wiki size and compression
python scripts/audit_wiki.py check -n 100               # wiki fidelity, LLM pass
```

Useful flags: `--limit N` (first N conversations), `--max-questions N`,
`--stratify` (spread a small sample across all QA categories), `--k N`
(retrieval depth), `--dump-prompts N` (print raw prompts before sending),
`--note "..."` (label the run in its results file), `--no-cache`,
`--rebuild-wiki` (System B: recompile rather than reuse `results/wikis/`).

## What it does

`runner.py` loads a dataset, hands each conversation to a system's `ingest()`,
asks every question through `answer()`, grades the reply with an LLM judge, and
writes `results/system_<x>_<dataset>_<timestamp>.json` plus a `_summary.csv`.
Runs are never overwritten — the history is thesis evidence.

Every system implements the same `MemorySystem` interface (`src/systems/base.py`)
— `ingest(conversation)`, `answer(question) -> Answer`, `ingest_stats()` — so the
loader, runner, judge and metrics are written once. Registering System B was one
factory line in `runner.SYSTEMS` and nothing else; if the runner ever needs to
know which system it is running, the abstraction is wrong.

## Experimental design

The thesis claim is about the **difference between systems**, so everything that
is not the system under test is held fixed, and every fixed value is written
into each results file. Anything that drifts silently invalidates the
comparison.

| Held constant | Value | Why it matters |
|---|---|---|
| Answering model | `gpt-4o-mini`, all systems | A cheaper model is valid for measuring a *difference*, and it makes full 1,986-question runs affordable rather than sampled |
| Answering prompt | One string, defined in `system_l.py` and **imported** by A and B | A copy would drift. The abstention rule ("reply exactly: No information available") is part of it, and abstention behaviour is a measured outcome |
| Judge | `gpt-4.1-mini`, separate rubric for adversarial items | Deliberately a different and stronger model than the answerer, so nothing grades its own output. Validated against 30 hand labels: 96.7% agreement, kappa 0.933 |
| Temperature | 0.0 everywhere | |
| Retrieval | One `VectorIndex`, shared by A and B | Same embedding model, same k, same cosine similarity, same chronological re-ordering — so A vs B isolates the *representation*, not the access method |
| Dataset, metrics, categories | LoCoMo, judge accuracy + LoCoMo F1, official category codes | |

What differs between systems is only what is stored and what reaches the model:

| | What is stored | What reaches the model per question |
|---|---|---|
| **L** | nothing — the raw transcript | the entire transcript, every time |
| **A** | 4-turn windows of raw dialogue, embedded | the top-k windows by cosine similarity |
| **B** | compiled wiki facts, embedded | the top-k wiki chunks — *same retrieval as A* |
| C | planned: B + a source on every fact | |
| D | planned: C + forgetting by source trust | |

**Reproducibility.** Every results file carries a `config.as_dict()` snapshot —
models, prices, chunking, k, the wiki chunk budget and the wiki build
fingerprint — so any number can be traced back to the settings that produced it.
`--dry-run` exercises loading, ingestion and prompt assembly without sending an
answering call, which catches formatting bugs for free.

**One-variable-at-a-time is a rule, not an aspiration.** System B's answering
prompt still says "Below is the full conversation" even though B sends compiled
facts. Rewording it would have introduced a second difference and made the A-vs-B
gap uninterpretable, so the odd wording stays and is reported as a known cost.

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

| | L | A (k=5) | A (k=10) | B (k=5) | B (k=10) |
|---|---|---|---|---|---|
| Judge accuracy | 0.579 | 0.609 | **0.624** | 0.529 | 0.540 |
| Mean prompt tokens | 20,849 | **958** | 1,748 | 1,232 | 2,248 |
| Mean latency | 5.71 s | 0.95 s | 0.68 s | 0.66 s | **0.63 s** |
| Total cost of ownership | $6.43 | **$0.50** | $0.74 | $0.70 | $1.00 |
| Hallucination on unanswerable | 28.3% | 12.8% | 18.5% | **5.6%** | 7.9% |
| Evidence recall | n/a | 0.755 | 0.837 | 0.900* | 0.970* |

\* session-level, not turn-level — not comparable with A's. See "System B notes".

Retrieval beats brute-force context on this dataset, and does it roughly 13x
cheaper. **Compiling first loses accuracy and buys safety:** System B is the
least accurate retrieval system and by a wide margin the least likely to
fabricate an answer to a question nothing supports. Its losses are compilation
loss, not retrieval failure — session-level recall is 0.90–0.97, and 141 of the
164 questions B declined but A answered had recall 1.0.

See `../00_RESEARCH_LOG.md` for the per-category breakdown and the caveats —
judge leniency on temporal questions, where System A's advantage is largest,
and the cross-session contradictions the per-session fidelity audit cannot see.

Compare any set of runs directly:

```bash
python scripts/compare_runs.py results/system_a_*_2148*.json results/system_b_*.json
```

## System L notes

The whole conversation goes into the prompt on every question — no retrieval, no
chunking, no memory management. This is the experimental form of the supervisor's
challenge ("if the knowledge base just sits in the context window, isn't that
better?"), and it is deliberately the most expensive system in the benchmark:
quantifying that cost is part of the result.

Sessions are rendered with their dates intact (`=== Session 1 - 1:56 pm on 8 May,
2023 ===`) so temporal questions stay answerable. Before any call, the transcript
is token-counted against the model's context window: **an overflow is recorded as
an outcome and reported, never silently truncated.** Zero overflows occurred on
LoCoMo (largest transcript 23.7k against a 128k window), which is a finding to be
shown rather than an assumption to be stated.

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

## System B notes

System B compiles each conversation into a small OKF-shaped wiki — markdown
pages with YAML frontmatter, one dated fact per line — and retrieves over
*that* instead of over raw dialogue. Compilation is incremental: one LLM call
per session, each seeing that session plus the titles of the pages that already
exist, which is both cheap and how a personal knowledge base is really built.

```markdown
---
title: Charity race
type: event
---

- Melanie ran a charity race for mental health last Saturday — 25 May, 2023
```

**How compilation works.** For each session, in order:

1. that session's turns, plus the titles of the pages that already exist, go to
   the extraction model (`config.WIKI_EXTRACTION_MODEL`, default `gpt-4o-mini`
   — the same model that answers, so B's wiki is not quietly built by a stronger
   model than A ever sees);
2. it returns JSON facts, each assigned to a page title and a page type
   (person / event / topic);
3. facts are appended to their page, new pages created as needed;
4. the finished wiki is written to disk, chunked (one chunk per page, split only
   when a page exceeds `WIKI_CHUNK_MAX_TOKENS`, default 250), embedded, indexed.

Not one giant call: sending the whole conversation would be System L again, and
the output would be unmanageable. Each call sees ~800 tokens, and the whole
corpus compiles for about $0.11.

The extraction prompt (`wiki.EXTRACTION_TEMPLATE`) enforces the properties the
format depends on: only facts stated in *this* session, subject named
explicitly, several small facts in preference to one compound sentence, pages
chosen by subject matter rather than by who was speaking, and any time reference
the speaker gave ("last year", "two weeks ago") kept verbatim inside the fact.
**The date on each line is attached by us from the session metadata, not written
by the model** — so a fact line's date cannot be hallucinated, only the fact can.

**What the compiler gets wrong, and what the code does about it.** All three were
found by reading one compiled wiki end to end before the first paid run; none
would have been visible in an accuracy number.

| Failure | Handling |
|---|---|
| Files nearly everything on the two speakers' pages, creating no topic pages (227 of 249 facts on the first attempt) | Page-assignment rule rewritten: pages are chosen by subject matter, and "a person page must not become a diary of everything someone did" |
| Uses a category as the page title — a page literally called `event` or `topic` | A prompt example showing three facts on three different pages; `repair_title()` deterministically re-files anything still generic onto the page of the person the fact names. 49 facts across the corpus needed it, counted as `generic_page_titles` |
| Emits malformed JSON, silently losing an entire session's facts | `parse_facts()` salvages fact objects individually when the reply will not parse; salvaged / unparsable / empty / truncated sessions are all counted into the wiki manifest. **6 sessions across 4 wikis needed salvaging** — without it, those facts would simply have been absent, with nothing in the output to say so |

Every one of those counters lives in `results/wikis/<conv_id>/wiki.json` and in
the run's `ingest` block, so the health of a compile is inspectable after the
fact rather than trusted.

**Retrieval is shared code, not a copy.** `src/systems/retrieval.py` holds the
embedding and cosine search both A and B use, so the two systems differ in
exactly one thing: what is stored. The answering prompt is imported from System
L for the same reason.

**One fact per line, subject always named.** Not styling: System C hangs a
source on each of these lines, and a pronoun in a compiled fact is a future
misattribution — which is precisely what LoCoMo's adversarial questions probe.

**Every wiki is written to `results/wikis/<conv_id>/`** and committed. The
fidelity audit reads them, and "show me what your system actually built" has an
answer. A wiki is reused on the next run unless the extraction model, the
extraction prompt or the compiler version changed (`wiki.BUILD_FINGERPRINT`);
`--rebuild-wiki` forces it.

**Evidence recall means something different for B.** LoCoMo's `evidence` field
lists raw turns, and B never retrieves a turn. Each retrieved fact is mapped
back to the session it was compiled from, so B's recall is session-level: an
upper bound, answering "did retrieval reach the right part of the
conversation?". Every record carries `evidence_recall_granularity`, so B's
number is never silently read as A's.

### Wiki fidelity — the measurement System B exists for

L and A fail transiently; B can fail permanently. A hallucinated fact is
*written into the memory*, and every later question touching it inherits the
error. `scripts/audit_wiki.py` samples compiled facts (stratified by page
type), compares each against the session it came from, and classifies it
**supported / distorted / unsupported**, with **misattribution** counted
separately. The first pass uses `gpt-4.1-mini` — deliberately not the compiler
— and `check` also writes a `_sheet.md` review sheet so a subset can be
hand-verified without a model's verdict anchoring the labels:

```bash
python scripts/audit_wiki.py check -n 100
# fill in human_verdict / human_misattribution for a subset, then
python scripts/audit_wiki.py score results/wiki_fidelity_<stamp>.csv
```

`score` reports the rates plus checker/human agreement and Cohen's kappa, the
same standard the answer judge was held to.

**Result (100 facts, 30 Aug):** 97% supported, 3% distorted, 0% unsupported,
**0/100 misattributed**. All three distortions are the compiler resolving a
relative time reference ("yesterday") into a date the session never stated.
Read with one limitation in mind: **a per-session audit cannot see
cross-session contradiction.** conv-26's wiki holds both `Melanie has a dog
named Oliver` and `Oliver, her cat` — each supported by its own session,
incoherent together, and nothing in the wiki marks either as suspect. That is
the case for provenance (System C) and forgetting (System D), measured rather
than assumed. The hand-verified subset is still outstanding.

**Ingest cost is reported separately** from answer cost, in every results file
and at the end of every run. B's total cost of ownership is compile-once plus
retrieve-many, and folding the two together would hide the trade-off against A.

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
src/systems/system_a.py        System A (chunking; retrieval via retrieval.py)
src/systems/retrieval.py       embedding + cosine search, shared by A and B
src/systems/wiki.py            OKF wiki: format, compiler, chunking, disk I/O
src/systems/system_b.py        System B (compiled wiki memory)
src/evaluation/judge.py        LLM-as-judge
src/evaluation/metrics.py      F1, aggregation, per-category summaries
src/runner.py                  run(system, dataset) -> results file
scripts/inspect_locomo.py      prints the real dataset schema
scripts/validate_judge.py      judge vs. hand labels, agreement + kappa
scripts/audit_wiki.py          wiki size, fidelity audit, hand-verification sheet
scripts/compare_runs.py        side-by-side table of finished runs
results/wikis/<conv_id>/       every compiled wiki, committed as evidence
results/                       committed — thesis evidence
```

`.env`, `data/` and the `IMPLEMENTATION_SPEC_*.md` working notes are
git-ignored. No key is ever committed.

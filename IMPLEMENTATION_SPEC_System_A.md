# Implementation Spec — System A (RAG Baseline)

**Read `00_PROJECT_STATUS.md` and `00_RESEARCH_LOG.md` first** — the log's "Phase 4" section contains the design reasoning and the threat-to-validity this spec is written to address.

System L is complete and validated (judge kappa 0.933). System A is the second baseline.

---

## 1. What System A is

Instead of sending the whole conversation like System L, System A chunks the conversation, embeds the chunks, retrieves the few most relevant per question, and answers from those alone.

**Two phases:**

- **Ingest** (once per conversation): chunk → embed each chunk → hold vectors in memory
- **Answer** (per question): embed the question → cosine similarity against chunk vectors (local maths, no API) → take top *k* → paste those chunks into the prompt → chat API

The LLM sees only the retrieved chunks. It has no access to the store — retrieval happens in our code.

---

## 2. Non-negotiables — what must NOT change

These keep the L-vs-A comparison valid. Changing any of them silently invalidates the benchmark.

- **Same answering prompt as System L**, word for word, including the abstention rule. The only difference between L and A must be *what text reaches the model*.
- **Same answering model** (`gpt-4o-mini`), same judge (`gpt-4.1-mini`), same temperature (0).
- **Same `MemorySystem` interface** (`ingest` / `answer`). Nothing system-specific in the runner.
- **Same dataset, same 1,986 questions, same metrics.**

Registering System A should mean adding one factory entry to `runner.SYSTEMS` and nothing else.

---

## 3. Settings (agreed — do not hand-tune)

| Setting | Value | Why |
|---|---|---|
| Chunk size | **4 turns** | A single turn is often meaningless alone ("Wow, that's cool!"). Four turns keeps an exchange coherent. |
| Overlap | **1 turn** | Prevents a question and its answer being split across a chunk boundary. |
| k (chunks retrieved) | **5** primary, **10** as a sweep | Conventional. The sweep evidences the baseline is not crippled. |
| Embedding model | `text-embedding-3-small` | Standard, cheap (well under $0.01 for the whole dataset). |
| Similarity | cosine | Standard for normalised embeddings. |

**Do not tune these to make System A look worse.** The strongest attack on this thesis is *"you beat a weak RAG."* Settings are conventional and fixed; if anything is changed later, it must be changed for a documented reason and re-run.

---

## 4. Critical constraint — chunks must carry session dates

System L got session dates free from transcript headers (`=== Session 1 - 1:56 pm on 8 May, 2023 ===`). If System A's chunks are bare turns, that date is gone and **every temporal question becomes structurally unanswerable** — System A would score near zero on temporal for a formatting reason rather than a genuine retrieval failure, which would unfairly flatter the later systems.

Each chunk must be rendered with its session date and speaker attribution, e.g.:

```
[Session 3 - 2:14 pm on 12 June, 2023]
Caroline: ...
Melanie: ...
Caroline: ...
Melanie: ...
```

Image turns keep their `[shares an image: caption]` rendering, exactly as in System L.

---

## 5. Storage — no vector database

Each conversation is its own memory: ~588 turns → ~147 chunks. Cosine similarity over 147 vectors in a numpy array is instant.

Chroma / FAISS / Pinecone solve a problem that does not exist at this scale and would add machinery that has to be defended in the viva. Use a plain numpy array held on the system instance.

**Cache embeddings to disk**, keyed by (model, text hash), in the same spirit as the existing LLM cache. Re-runs must not re-embed. Note the existing `.cache` is for chat completions — embeddings need their own cache or a namespaced key.

---

## 6. Implementation

**New file:** `src/systems/system_a.py`
**New helper:** embedding support — either extend `src/llm.py` with an `embed()` method or add `src/embeddings.py`. Must include the same retry/backoff treatment as chat calls.

```python
class SystemA(MemorySystem):
    name = "A"

    def __init__(self, client, model=config.ANSWER_MODEL, k=config.RAG_K):
        ...

    def ingest(self, conversation):
        # 1. build chunks of CHUNK_TURNS turns with CHUNK_OVERLAP overlap,
        #    each rendered WITH its session date + speakers
        # 2. embed all chunks (batch the API calls — do not embed one at a time)
        # 3. store vectors as a numpy array + keep the chunk texts alongside

    def answer(self, question):
        # 1. embed the question
        # 2. cosine similarity vs all chunk vectors  (no API call)
        # 3. take top k chunks, in original chronological order
        # 4. same prompt as System L, with the retrieved chunks in place of
        #    the full transcript
```

**Two details that matter:**

- **Re-order retrieved chunks chronologically** before putting them in the prompt. Relevance order scrambles the timeline and will hurt temporal questions.
- **Batch the embedding calls.** The embeddings endpoint accepts many texts per request; embedding 147 chunks one at a time is needlessly slow.

New config values: `CHUNK_TURNS = 4`, `CHUNK_OVERLAP = 1`, `RAG_K = 5`, `EMBEDDING_MODEL = "text-embedding-3-small"`, plus the embedding price so cost accounting stays correct. All must appear in the results file's config snapshot.

---

## 7. Reporting

Same metrics as System L, plus:

- **Retrieval cost separated from answer cost** — embedding is cheap but must be counted, including the one-off ingest cost per conversation (`ingest_stats()` on the base class already exists for this).
- **Mean prompt tokens per question** — the headline efficiency comparison against L's 20,849.
- **Recall proxy (worth having):** LoCoMo's `evidence` field lists the `dia_id`s containing the answer. Report **how often the retrieved chunks actually contained the evidence turns.** This separates two very different failure modes: retrieval missed the evidence, versus retrieval found it and the model still answered wrong. That distinction is genuinely valuable for the thesis and cheap to compute.

---

## 8. Bundled leftovers from System L

Do these first, they are small:

1. **`git init`** + `.gitignore` check (`.env`, `.venv`, `.cache`, `data/`) + first commit. The repo is a required CDS deliverable.
2. **Top-up judge validation** — ~15 further samples drawn **only from non-declines**, for Punith to hand-label. The first 30 were 40% refusals, which are near-mechanical to label; this gives a second agreement figure on cases where the judge must exercise real judgement. **Do not label these yourself.**
3. **Clean latency measurement** — a small subset re-run with cache disabled, to give a trustworthy seconds-per-question figure. The full-run mean is contaminated by the earlier stall (cached records carry original latencies up to 304s).

---

## 9. Definition of done

- [ ] `git init`, first commit, no secrets committed
- [ ] Top-up validation sample generated (labelling is Punith's, not the agent's)
- [ ] Clean latency figure recorded
- [ ] `SystemA` implements `MemorySystem`; registered in `runner.SYSTEMS`
- [ ] Chunks carry session date + speakers; retrieved chunks re-ordered chronologically
- [ ] Embedding cache working (second run makes no embedding calls)
- [ ] Dry run inspected — read an actual assembled prompt by eye
- [ ] 10-question stratified proof run
- [ ] Full run at k=5, committed to `results/`
- [ ] Sweep run at k=10, committed
- [ ] Evidence-recall figure reported
- [ ] `00_RESEARCH_LOG.md` updated with results, interpretation and anything that went wrong

**Then stop.** System B (OKF compiled wiki) is next and is a bigger design job.

---

## 10. What this run answers

L already showed brute-force context gets 57.9% at 20.8k tokens per question. System A answers: **does a small, well-chosen slice of the conversation do as well — or better — for a fraction of the cost?**

Either outcome is a result. If A matches L cheaply, that is evidence retrieval beats brute force. If A loses badly, that motivates compiled memory. What matters is that the comparison is fair.

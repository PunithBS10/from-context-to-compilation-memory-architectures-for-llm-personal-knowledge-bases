"""Benchmark runner: run(system, dataset) -> results file.

System-agnostic by design. Adding System A, B, C or D means registering a
factory in SYSTEMS below and writing nothing else here.

Usage:
    python -m src.runner --system l --limit 1              # prove the loop
    python -m src.runner --system l --limit 1 --dry-run    # no API calls
    python -m src.runner --system l                        # full run
"""
from __future__ import annotations

import argparse
import csv
import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

from tqdm import tqdm

import config
from src.datasets.locomo import load_locomo
from src.evaluation import metrics
from src.evaluation.judge import LLMJudge
from src.llm import LLMClient
from src.systems.system_a import SystemA
from src.systems.system_b import SystemB
from src.systems.system_l import SystemL

SYSTEMS = {
    "l": lambda client, model: SystemL(client, model),
    "a": lambda client, model: SystemA(client, model, k=config.RAG_K),
    "b": lambda client, model: SystemB(client, model, k=config.RAG_K),
}

DATASETS = {
    "locomo": lambda limit: load_locomo(config.LOCOMO_PATH, limit=limit),
}


def select_questions(qa_items: list, max_questions: int | None, stratify: bool) -> list:
    """Pick which questions to ask.

    Plain truncation takes them in file order, which on LoCoMo is badly skewed:
    the first ten questions of conv-26 contain no single-hop and no adversarial
    items at all. `stratify` instead round-robins across categories, so a small
    sample still reports every category — adversarial above all, since that is
    the one this thesis cares most about. It is deterministic: no RNG, and
    original order is preserved within each category.
    """
    if not max_questions or max_questions >= len(qa_items):
        return qa_items
    if not stratify:
        return qa_items[:max_questions]

    by_category: dict[str, list] = {}
    for item in qa_items:
        by_category.setdefault(item.category, []).append(item)

    chosen, queues = [], [by_category[c] for c in sorted(by_category)]
    while len(chosen) < max_questions and any(queues):
        for queue in queues:
            if queue and len(chosen) < max_questions:
                chosen.append(queue.pop(0))
    return sorted(chosen, key=lambda item: item.qa_index)


def run(system_key: str, dataset_key: str, limit: int | None, max_questions: int | None,
        use_cache: bool, dump_prompts: int, stratify: bool = False,
        note: str = "") -> Path:
    conversations = DATASETS[dataset_key](limit)
    client = LLMClient(use_cache=use_cache)
    judge = LLMJudge(client)

    records: list[dict] = []
    ingest_stats: list[dict] = []
    prompts_dumped = 0
    started = datetime.now(timezone.utc)

    for conversation in conversations:
        system = SYSTEMS[system_key](client, config.ANSWER_MODEL)
        system.ingest(conversation)

        qa_items = select_questions(conversation.qa, max_questions, stratify)
        overflow_note = " [CONTEXT OVERFLOW]" if getattr(system, "overflow", False) else ""
        print(f"\n{conversation.conv_id}: {conversation.n_sessions} sessions, "
              f"{len(conversation.turns)} turns, {len(qa_items)} questions"
              f"{overflow_note}")

        for qa in tqdm(qa_items, desc=conversation.conv_id, unit="q"):
            # Read a few raw prompts by eye: most early bugs are formatting
            # problems that are instantly visible and cost nothing to catch.
            if prompts_dumped < dump_prompts and hasattr(system, "build_prompt"):
                _dump_prompt(system, qa, prompts_dumped)
                prompts_dumped += 1

            answer = system.answer(qa.question)
            verdict = judge.judge(qa, answer.text)

            records.append({
                "conv_id": conversation.conv_id,
                "qa_index": qa.qa_index,
                "question": qa.question,
                "gold_answer": qa.answer,
                "adversarial_answer": qa.adversarial_answer,
                "category": qa.category,
                "category_code": qa.category_code,
                "abstention_expected": qa.abstention_expected,
                "evidence": qa.evidence,
                "prediction": answer.text,
                "judge_correct": bool(verdict["correct"]),
                "judge_reason": verdict["reason"],
                "judge_error": verdict["judge_error"],
                "judge_cost_usd": verdict["judge_cost_usd"],
                "f1": metrics.locomo_f1(answer.text, qa),
                "abstained": metrics.is_abstention(answer.text),
                "prompt_tokens": answer.prompt_tokens,
                "completion_tokens": answer.completion_tokens,
                "latency_s": answer.latency_s,
                "cost_usd": answer.cost_usd,
                "cached": answer.cached,
                "context_overflow": bool(answer.meta.get("context_overflow", False)),
                "transcript_tokens": answer.meta.get("transcript_tokens"),
                # Retrieval diagnostics. None for systems that do not retrieve,
                # so the same record shape serves every system.
                "retrieval_cost_usd": answer.meta.get("retrieval_cost_usd"),
                "retrieved_dia_ids": answer.meta.get("retrieved_dia_ids"),
                "evidence_recall": _evidence_recall(qa, answer.meta.get("retrieved_dia_ids")),
                # "turn" (System A retrieves raw turns) or "session" (System B
                # retrieves compiled facts, which map back only as far as the
                # session they came from). The two are NOT the same measure and
                # the results file has to say which one it is holding.
                "evidence_recall_granularity": answer.meta.get("evidence_recall_granularity"),
                "k": answer.meta.get("k"),
            })

        ingest_stats.append({"conv_id": conversation.conv_id, **system.ingest_stats()})

    finished = datetime.now(timezone.utc)
    summary = metrics.summarise(records)
    payload = {
        "run": {
            "system": system_key.upper(),
            "dataset": dataset_key,
            "started_utc": started.isoformat(),
            "finished_utc": finished.isoformat(),
            "wall_clock_s": (finished - started).total_seconds(),
            "conversations": [c.conv_id for c in conversations],
            "limit": limit,
            "max_questions": max_questions,
            "stratified": stratify,
            "note": note,
            "cache_enabled": use_cache,
            "cache_hits": client.cache_hits,
            "live_api_calls": client.calls,
            "python": platform.python_version(),
        },
        "config": config.as_dict(),
        "ingest": ingest_stats,
        "summary": summary,
        "records": records,
    }
    return _write_results(system_key, dataset_key, payload, started)


def _evidence_recall(qa, retrieved_dia_ids) -> float | None:
    """Fraction of the question's evidence turns that retrieval actually put in
    front of the model.

    LoCoMo's `evidence` field lists the dia_ids containing the answer, so this
    separates two very different failures: retrieval never surfaced the
    evidence, versus retrieval surfaced it and the model still answered wrong.
    None for systems that do not retrieve, and for questions with no evidence
    listed (most adversarial items), where recall is undefined rather than 0.
    """
    if retrieved_dia_ids is None or not qa.evidence:
        return None
    evidence = [e for e in qa.evidence if isinstance(e, str)]
    if not evidence:
        return None
    found = sum(1 for e in evidence if e in set(retrieved_dia_ids))
    return found / len(evidence)


def _dump_prompt(system, qa, index: int) -> None:
    system_prompt, user_prompt = system.build_prompt(qa.question)
    print("\n" + "=" * 70)
    print(f"RAW PROMPT SAMPLE {index + 1} - {qa.category} - {qa.question}")
    print("=" * 70)
    print("[system]\n" + system_prompt)
    print("\n[user] first 1200 chars:\n" + user_prompt[:1200])
    print("\n... [user] last 600 chars:\n" + user_prompt[-600:])
    print("=" * 70 + "\n")


def _write_results(system_key: str, dataset_key: str, payload: dict, started) -> Path:
    config.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = started.strftime("%Y%m%d_%H%M%S")
    stem = f"system_{system_key}_{dataset_key}_{stamp}"   # never overwrites: the thesis needs the history

    json_path = config.RESULTS_DIR / f"{stem}.json"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    csv_path = config.RESULTS_DIR / f"{stem}_summary.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=metrics.SUMMARY_COLUMNS)
        writer.writeheader()
        writer.writerows(metrics.summary_rows(payload["summary"]))

    _print_summary(payload)
    print(f"\nresults : {json_path}")
    print(f"summary : {csv_path}")
    return json_path


def _print_summary(payload: dict) -> None:
    summary, totals = payload["summary"], payload["summary"]["totals"]
    overall = summary["overall"]
    print("\n" + "=" * 78)
    print(f"System {payload['run']['system']} on {payload['run']['dataset']} "
          f"({config.ANSWER_MODEL}, judge {config.JUDGE_MODEL})")
    print("=" * 78)
    rows = metrics.summary_rows(summary)
    shows_recall = any(row.get("evidence_recall") is not None for row in rows)
    header = (f"{'scope':<14}{'n':>6}{'judge acc':>11}{'F1':>8}{'abstain':>9}"
              f"{'p.tok':>10}{'cost $':>10}{'lat s':>8}"
              + (f"{'ev.recall':>11}" if shows_recall else ""))
    print(header)
    print("-" * 78)
    for row in rows:
        line = (f"{row['scope']:<14}{row['n']:>6}{row['judge_accuracy']:>11.3f}"
                f"{row['f1']:>8.3f}{row['abstention_rate']:>9.3f}"
                f"{row['mean_prompt_tokens']:>10.0f}{row['mean_cost_usd']:>10.4f}")
        latency = row.get('mean_latency_s')
        line += f"{latency:>8.2f}" if latency is not None else f"{'-':>8}"
        if shows_recall:
            recall = row.get("evidence_recall")
            line += f"{recall:>11.3f}" if recall is not None else f"{'-':>11}"
        print(line)
    print("-" * 78)
    _print_ingest(payload)
    print(f"questions {totals['questions']}  |  context overflows {totals['context_overflows']}"
          f"  |  live calls {payload['run']['live_api_calls']}"
          f"  |  cache hits {payload['run']['cache_hits']}")
    print(f"cost of this result set: answers ${totals['total_answer_cost_usd']:.4f} + "
          f"judge ${totals['total_judge_cost_usd']:.4f} = "
          f"${totals['total_cost_usd']:.4f}")
    print(f"  of which billed on this run: ${totals['live_total_cost_usd']:.4f} "
          f"({totals['cached_answers']} answers replayed from cache at $0)")
    if overall["context_overflow"]:
        print("NOTE: some conversations exceeded the context window and were NOT truncated.")
    if not config.is_priced(config.ANSWER_MODEL):
        print(f"WARNING: no price for {config.ANSWER_MODEL} in config.PRICES; cost is reported as 0.")


def _print_ingest(payload: dict) -> None:
    """Cost of BUILDING the memory, reported apart from the cost of using it.

    For L and A ingestion is free or nearly so. For B it is one LLM call per
    session, paid once and then amortised over every question -- a real
    trade-off against A that is invisible if ingest cost is folded into the
    answer cost, and dishonest if it is left out altogether.
    """
    stats = payload.get("ingest") or []
    cost = sum(s.get("ingest_cost_usd", 0.0) for s in stats)
    if not cost:
        return
    prompt_tokens = sum(s.get("ingest_prompt_tokens", 0) for s in stats)
    completion_tokens = sum(s.get("ingest_completion_tokens", 0) for s in stats)
    latency = sum(s.get("ingest_latency_s", 0.0) for s in stats)
    reused = sum(1 for s in stats if s.get("wiki_from_disk"))
    print(f"ingest (build the memory): ${cost:.4f} over {len(stats)} conversations, "
          f"{prompt_tokens:,} prompt + {completion_tokens:,} completion tokens, "
          f"{latency:.0f}s"
          + (f"  [{reused}/{len(stats)} wikis reused from disk, cost is the "
             f"original build's]" if reused else ""))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run a memory system over a QA benchmark.")
    parser.add_argument("--system", default="l", choices=sorted(SYSTEMS))
    parser.add_argument("--dataset", default="locomo", choices=sorted(DATASETS))
    parser.add_argument("--limit", type=int, default=None,
                        help="only the first N conversations (start with 1)")
    parser.add_argument("--max-questions", type=int, default=None,
                        help="only the first N questions per conversation")
    parser.add_argument("--stratify", action="store_true",
                        help="with --max-questions, spread the sample evenly across QA "
                             "categories instead of taking them in file order")
    parser.add_argument("--no-cache", action="store_true", help="ignore the response cache")
    parser.add_argument("--rebuild-wiki", action="store_true",
                        help="System B: recompile every wiki instead of reusing the "
                             "one in results/wikis/")
    parser.add_argument("--k", type=int, default=None,
                        help="retrieval depth for RAG systems; overrides config.RAG_K "
                             "for this run and is recorded in the results snapshot")
    parser.add_argument("--note", default="",
                        help="free-text label recorded in the results file, e.g. why this run exists")
    parser.add_argument("--dump-prompts", type=int, default=0,
                        help="print the first N raw prompts before sending them")
    parser.add_argument("--dry-run", action="store_true",
                        help="load, ingest and format prompts, but make no API calls")
    args = parser.parse_args(argv)

    # Mutate config, not the factory, so the results snapshot records the k
    # actually used rather than the default.
    if args.k is not None:
        config.RAG_K = args.k
    if args.rebuild_wiki:
        config.WIKI_REUSE = False

    if args.dry_run:
        return dry_run(args)

    run(args.system, args.dataset, args.limit, args.max_questions,
        use_cache=not args.no_cache, dump_prompts=args.dump_prompts,
        stratify=args.stratify, note=args.note)
    return 0


def dry_run(args) -> int:
    """Everything except the ANSWERING calls: loading, ingestion, prompt
    formatting, token counting and a cost estimate. Catches most bugs.

    Free for System L. For a retrieval system the assembled prompt depends on
    retrieval, so chunk and question embeddings are genuinely computed -- a few
    tenths of a cent per conversation, cached thereafter. For System B
    ingestion also COMPILES the wiki, which is real LLM calls; that spend is
    reported below rather than hidden, and a wiki already on disk is reused.
    No answering completion is ever sent, which is where the real money is.
    """
    conversations = DATASETS[args.dataset](args.limit)
    from src.llm import count_tokens

    client = LLMClient(use_cache=not args.no_cache)

    print(f"DRY RUN - system {args.system.upper()}, dataset {args.dataset}, "
          f"model {config.ANSWER_MODEL} (context {config.MODEL_CONTEXT_LIMIT:,})\n")
    grand_tokens = grand_questions = 0
    ingest_cost = 0.0
    for conversation in conversations:
        system = SYSTEMS[args.system](client, config.ANSWER_MODEL)
        system.ingest(conversation)
        qa_items = select_questions(conversation.qa, args.max_questions, args.stratify)
        _, user_prompt = system.build_prompt(qa_items[0].question)
        prompt_tokens = count_tokens(user_prompt, config.ANSWER_MODEL)
        run_tokens = prompt_tokens * len(qa_items)
        grand_tokens += run_tokens
        grand_questions += len(qa_items)
        stats = system.ingest_stats()
        ingest_cost += stats.get("ingest_cost_usd", 0.0)
        detail = (f"chunks {stats['ingest_chunks']:>4}"
                  if stats.get("ingest_chunks")
                  else f"transcript {getattr(system, 'transcript_tokens', 0):>7,} tok")
        print(f"{conversation.conv_id}: {conversation.n_sessions:>2} sessions "
              f"{len(conversation.turns):>4} turns  {len(qa_items):>4} questions  "
              f"{detail}  prompt {prompt_tokens:>7,} tok"
              f"{'  OVERFLOW' if getattr(system, 'overflow', False) else ''}")

    estimated = config.price_of(config.ANSWER_MODEL, grand_tokens, grand_questions * 40)
    print(f"\n{grand_questions} questions, ~{grand_tokens:,} prompt tokens")
    print(f"estimated answer cost: ${estimated:.2f} (judge adds roughly 5-10% on top)")

    if args.dump_prompts:
        conversation = conversations[0]
        system = SYSTEMS[args.system](client, config.ANSWER_MODEL)
        system.ingest(conversation)
        for index, qa in enumerate(conversation.qa[:args.dump_prompts]):
            _dump_prompt(system, qa, index)
    return 0


if __name__ == "__main__":
    sys.exit(main())

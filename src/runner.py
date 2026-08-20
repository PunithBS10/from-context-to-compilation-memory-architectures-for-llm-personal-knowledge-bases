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
from src.systems.system_l import SystemL

SYSTEMS = {
    "l": lambda client, model: SystemL(client, model),
    # "a": lambda client, model: SystemA(client, model),   # next
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
            })

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
        "summary": summary,
        "records": records,
    }
    return _write_results(system_key, dataset_key, payload, started)


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
    header = f"{'scope':<14}{'n':>6}{'judge acc':>11}{'F1':>8}{'abstain':>9}{'p.tok':>10}{'cost $':>10}{'lat s':>8}"
    print(header)
    print("-" * 78)
    for row in metrics.summary_rows(summary):
        print(f"{row['scope']:<14}{row['n']:>6}{row['judge_accuracy']:>11.3f}"
              f"{row['f1']:>8.3f}{row['abstention_rate']:>9.3f}"
              f"{row['mean_prompt_tokens']:>10.0f}{row['mean_cost_usd']:>10.4f}"
              f"{row['mean_latency_s']:>8.2f}")
    print("-" * 78)
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
    parser.add_argument("--note", default="",
                        help="free-text label recorded in the results file, e.g. why this run exists")
    parser.add_argument("--dump-prompts", type=int, default=0,
                        help="print the first N raw prompts before sending them")
    parser.add_argument("--dry-run", action="store_true",
                        help="load, ingest and format prompts, but make no API calls")
    args = parser.parse_args(argv)

    if args.dry_run:
        return dry_run(args)

    run(args.system, args.dataset, args.limit, args.max_questions,
        use_cache=not args.no_cache, dump_prompts=args.dump_prompts,
        stratify=args.stratify, note=args.note)
    return 0


def dry_run(args) -> int:
    """Everything except the API calls: loading, ingestion, prompt formatting,
    token counting and a cost estimate. Free, and catches most bugs."""
    conversations = DATASETS[args.dataset](args.limit)
    from src.llm import count_tokens

    print(f"DRY RUN - system {args.system.upper()}, dataset {args.dataset}, "
          f"model {config.ANSWER_MODEL} (context {config.MODEL_CONTEXT_LIMIT:,})\n")
    grand_tokens = grand_questions = 0
    for conversation in conversations:
        system = SYSTEMS[args.system](None, config.ANSWER_MODEL)
        system.ingest(conversation)
        qa_items = select_questions(conversation.qa, args.max_questions, args.stratify)
        _, user_prompt = system.build_prompt(qa_items[0].question)
        prompt_tokens = count_tokens(user_prompt, config.ANSWER_MODEL)
        run_tokens = prompt_tokens * len(qa_items)
        grand_tokens += run_tokens
        grand_questions += len(qa_items)
        print(f"{conversation.conv_id}: {conversation.n_sessions:>2} sessions "
              f"{len(conversation.turns):>4} turns  {len(qa_items):>4} questions  "
              f"transcript {system.transcript_tokens:>7,} tok  prompt {prompt_tokens:>7,} tok"
              f"{'  OVERFLOW' if system.overflow else ''}")

    estimated = config.price_of(config.ANSWER_MODEL, grand_tokens, grand_questions * 40)
    print(f"\n{grand_questions} questions, ~{grand_tokens:,} prompt tokens")
    print(f"estimated answer cost: ${estimated:.2f} (judge adds roughly 5-10% on top)")

    if args.dump_prompts:
        conversation = conversations[0]
        system = SYSTEMS[args.system](None, config.ANSWER_MODEL)
        system.ingest(conversation)
        for index, qa in enumerate(conversation.qa[:args.dump_prompts]):
            _dump_prompt(system, qa, index)
    return 0


if __name__ == "__main__":
    sys.exit(main())

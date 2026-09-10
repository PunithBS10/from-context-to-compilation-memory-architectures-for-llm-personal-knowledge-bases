"""Put finished runs side by side: the table the thesis reports.

    python scripts/compare_runs.py results/system_a_*.json results/system_b_*.json

Prints, per run, the headline metrics and the per-category breakdown, plus two
figures the summary block in the results file does not carry:

* **hallucination rate on unanswerable questions** - the share of LoCoMo's
  adversarial items (those with no real answer) that the system answered
  anyway instead of declining. This is the thesis's motivating failure, and it
  is not the same as 1 - adversarial accuracy: two adversarial items do carry a
  real answer, and they are excluded here.
* **ingest cost**, summed over conversations, so compile-once-retrieve-many
  systems are not compared on answer cost alone.
* **F1 both ways** - as the model emitted the answer, and with provenance tags
  stripped. System C's answers cite their sources unprompted, and token-overlap
  F1 counts every "[D7:11]" as wrong tokens. Both figures are RECOMPUTED here
  from each run's stored predictions rather than read from its summary block,
  so runs finished before the scorer changed are scored by the same rule as
  runs finished after it - without rewriting a single results file, which are
  thesis evidence and are never overwritten.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.evaluation import metrics

CATEGORIES = ["single_hop", "multi_hop", "temporal", "open_domain", "adversarial"]


class _QA:
    """The handful of fields `metrics.locomo_f1` needs, rebuilt from a record.

    Rescoring reads the stored prediction and gold answer, so it needs no API
    call and no re-run: F1 is a pure function of text the results file already
    holds.
    """

    def __init__(self, record: dict):
        self.category_code = record["category_code"]
        self.answer = record["gold_answer"]
        self.abstention_expected = record["abstention_expected"]


def _rescore(records: list[dict]) -> tuple[float, float]:
    """(stripped, raw) mean F1, recomputed for every run by one rule."""
    stripped = statistics.fmean(
        metrics.locomo_f1(r["prediction"], _QA(r)) for r in records)
    raw = statistics.fmean(
        metrics.locomo_f1(r["prediction"], _QA(r), strip_citations=False)
        for r in records)
    return stripped, raw


def summarise(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    records, summary = payload["records"], payload["summary"]
    unanswerable = [r for r in records if r["abstention_expected"]]
    answered_anyway = sum(1 for r in unanswerable if not r["abstained"])
    ingest = payload.get("ingest") or []
    overall = summary["overall"]
    f1, f1_raw = _rescore(records)
    variant = payload["run"].get("variant") or ""
    return {
        "label": (f"{payload['run']['system']}{'-' + variant if variant else ''} "
                  f"k={payload['config'].get('rag_k')}"),
        "path": path.name,
        "note": payload["run"].get("note", ""),
        "n": overall["n"],
        "accuracy": overall["judge_accuracy"],
        "f1": f1,
        "f1_raw": f1_raw,
        "tagged": sum(1 for r in records
                      if metrics.CITATION_TAG.search(r["prediction"] or "")),
        "tokens": overall["mean_prompt_tokens"],
        "latency": overall["mean_latency_s"],
        # System L predates the field: it does not retrieve, so recall is
        # undefined rather than zero.
        "recall": overall.get("evidence_recall"),
        "recall_granularity": next(
            (r.get("evidence_recall_granularity") for r in records
             if r.get("evidence_recall_granularity")), None),
        "hallucination": answered_anyway / len(unanswerable) if unanswerable else None,
        "hallucination_n": f"{answered_anyway}/{len(unanswerable)}",
        "answer_cost": summary["totals"]["total_answer_cost_usd"],
        "judge_cost": summary["totals"]["total_judge_cost_usd"],
        "ingest_cost": sum(s.get("ingest_cost_usd", 0.0) for s in ingest),
        "by_category": summary["by_category"],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("results", nargs="+", help="results/*.json files")
    args = parser.parse_args(argv)
    runs = [summarise(Path(p)) for p in args.results]

    width = 14
    def row(label, values):
        print(f"{label:<26}" + "".join(f"{v:>{width}}" for v in values))

    print("=" * (26 + width * len(runs)))
    row("", [r["label"] for r in runs])
    print("-" * (26 + width * len(runs)))
    row("questions", [r["n"] for r in runs])
    row("judge accuracy", [f"{r['accuracy']:.3f}" for r in runs])
    row("F1 (tags stripped)", [f"{r['f1']:.3f}" for r in runs])
    row("F1 (raw, as emitted)", [f"{r['f1_raw']:.3f}" for r in runs])
    row("  answers carrying a tag", [f"{r['tagged']}/{r['n']}" for r in runs])
    row("mean prompt tokens", [f"{r['tokens']:,.0f}" for r in runs])
    row("mean latency s", [f"{r['latency']:.2f}" if r["latency"] else "-" for r in runs])
    row("evidence recall", [f"{r['recall']:.3f}" if r["recall"] is not None else "-"
                            for r in runs])
    row("  granularity", [r["recall_granularity"] or "-" for r in runs])
    row("hallucination rate", [f"{r['hallucination']:.3f}" if r["hallucination"] is not None
                               else "-" for r in runs])
    row("  (answered/unanswerable)", [r["hallucination_n"] for r in runs])
    print("-" * (26 + width * len(runs)))
    row("answer cost $", [f"{r['answer_cost']:.2f}" for r in runs])
    row("judge cost $", [f"{r['judge_cost']:.2f}" for r in runs])
    row("ingest cost $", [f"{r['ingest_cost']:.4f}" for r in runs])
    row("total $", [f"{r['answer_cost'] + r['judge_cost'] + r['ingest_cost']:.2f}"
                    for r in runs])
    print("=" * (26 + width * len(runs)))

    print("\nJudge accuracy by category")
    print("-" * (26 + width * len(runs)))
    for category in CATEGORIES:
        values = []
        for run in runs:
            block = run["by_category"].get(category)
            values.append(f"{block['judge_accuracy']:.3f}" if block else "-")
        n = next((run["by_category"][category]["n"] for run in runs
                  if category in run["by_category"]), 0)
        row(f"{category} (n={n})", values)

    print("\nEvidence recall by category")
    print("-" * (26 + width * len(runs)))
    for category in CATEGORIES:
        values = []
        for run in runs:
            block = run["by_category"].get(category)
            recall = block.get("evidence_recall") if block else None
            values.append(f"{recall:.3f}" if recall is not None else "-")
        row(category, values)

    print("\nfiles:")
    for run in runs:
        print(f"  {run['label']:<8} {run['path']}  {run['note']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

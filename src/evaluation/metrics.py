"""Scoring and aggregation.

Two accuracy signals are kept side by side:

* the LLM judge (judge.py), which is the headline metric and the one the
  Mem0/LoCoMo line of work reports, and
* LoCoMo's own F1, reimplemented here to match the reference scorer in
  snap-research/locomo (task_eval/evaluation.py) so our numbers stay
  comparable with published results and so judge drift is visible.

The reference scorer's behaviour, preserved exactly:
  category 1 (multi-hop)  - split prediction and gold on commas, take the best
                            match per gold sub-answer, average
  categories 2, 3, 4      - plain token F1, with stemming and article removal;
                            for category 3 only the part before the first ';'
                            of the gold answer counts
  category 5 (adversarial)- 1.0 if the answer contains an abstention phrase
"""
from __future__ import annotations

import re
import statistics
import string
from collections import Counter

from nltk.stem import PorterStemmer

_STEMMER = PorterStemmer()

# The exact phrases the reference scorer accepts as abstention.
ABSTENTION_PHRASES = ("no information available", "not mentioned")


def normalize_answer(text: str) -> str:
    """Lowercase, drop punctuation and the articles a/an/the/and."""
    text = text.replace(",", "")
    text = text.lower()
    text = "".join(ch for ch in text if ch not in set(string.punctuation))
    text = re.sub(r"\b(a|an|the|and)\b", " ", text)
    return " ".join(text.split())


def _stems(text: str) -> list[str]:
    return [_STEMMER.stem(word) for word in normalize_answer(text).split()]


def f1_score(prediction: str, ground_truth: str) -> float:
    """Token-level F1 over stemmed, normalised tokens."""
    predicted, gold = _stems(prediction), _stems(ground_truth)
    if not predicted or not gold:
        return 0.0
    overlap = sum((Counter(predicted) & Counter(gold)).values())
    if overlap == 0:
        return 0.0
    precision = overlap / len(predicted)
    recall = overlap / len(gold)
    return 2 * precision * recall / (precision + recall)


def multi_answer_f1(prediction: str, ground_truth: str) -> float:
    """Multi-hop scoring: gold and prediction hold several comma-separated
    sub-answers; each gold sub-answer takes its best-matching predicted one."""
    predictions = [p.strip() for p in prediction.split(",")] or [prediction]
    golds = [g.strip() for g in ground_truth.split(",")] or [ground_truth]
    return statistics.fmean(
        max(f1_score(p, gold) for p in predictions) for gold in golds
    )


def is_abstention(prediction: str) -> bool:
    return any(phrase in prediction.lower() for phrase in ABSTENTION_PHRASES)


def locomo_f1(prediction: str, qa) -> float:
    """Reference-compatible F1 for one QA item."""
    if not prediction:
        return 0.0
    code = qa.category_code
    if code == 5 and qa.abstention_expected:
        return 1.0 if is_abstention(prediction) else 0.0
    gold = qa.answer
    if code == 3:
        gold = gold.split(";")[0].strip()   # first listed alternative is canonical
    if code == 1:
        return multi_answer_f1(prediction, gold)
    return f1_score(prediction, gold)


# --- aggregation ------------------------------------------------------------

def _mean(values) -> float:
    values = list(values)
    return statistics.fmean(values) if values else 0.0


def summarise(records: list[dict]) -> dict:
    """Aggregate per-question records into the report the thesis needs.

    Overflowed questions count as incorrect in the headline accuracy (the
    system genuinely failed to answer them) but are excluded from the token,
    cost and latency means, where they would otherwise drag the averages down
    with zeros.
    """
    answered = [r for r in records if not r["context_overflow"]]
    live = [r for r in answered if not r["cached"]]   # honest latency needs real calls

    def block(subset: list[dict]) -> dict:
        subset_live = [r for r in subset if not r["cached"] and not r["context_overflow"]]
        subset_answered = [r for r in subset if not r["context_overflow"]]
        return {
            "n": len(subset),
            "judge_accuracy": _mean(r["judge_correct"] for r in subset),
            "f1": _mean(r["f1"] for r in subset),
            "abstention_rate": _mean(r["abstained"] for r in subset),
            "context_overflow": sum(r["context_overflow"] for r in subset),
            "mean_prompt_tokens": _mean(r["prompt_tokens"] for r in subset_answered),
            "mean_completion_tokens": _mean(r["completion_tokens"] for r in subset_answered),
            "mean_cost_usd": _mean(r["cost_usd"] for r in subset_answered),
            "mean_latency_s": _mean(r["latency_s"] for r in subset_live),
        }

    categories = sorted({r["category"] for r in records})
    return {
        "overall": block(records),
        "by_category": {c: block([r for r in records if r["category"] == c]) for c in categories},
        "totals": {
            "questions": len(records),
            "answered": len(answered),
            "context_overflows": len(records) - len(answered),
            "cached_answers": sum(r["cached"] for r in records),
            "live_answers": len(live),
            "total_prompt_tokens": sum(r["prompt_tokens"] for r in records),
            "total_completion_tokens": sum(r["completion_tokens"] for r in records),
            # Cost of producing this result set. Cached records carry the cost
            # of the ORIGINAL call, so these totals are what the numbers cost
            # in total, across however many runs it took.
            "total_answer_cost_usd": sum(r["cost_usd"] for r in records),
            "total_judge_cost_usd": sum(r.get("judge_cost_usd", 0.0) for r in records),
            "total_cost_usd": sum(r["cost_usd"] + r.get("judge_cost_usd", 0.0) for r in records),
            # Incremental spend of THIS run only: what a cache hit did not re-bill.
            "live_answer_cost_usd": sum(r["cost_usd"] for r in live),
            "live_judge_cost_usd": sum(r.get("judge_cost_usd", 0.0) for r in live),
            "live_total_cost_usd": sum(r["cost_usd"] + r.get("judge_cost_usd", 0.0) for r in live),
        },
    }


SUMMARY_COLUMNS = [
    "scope", "n", "judge_accuracy", "f1", "abstention_rate", "context_overflow",
    "mean_prompt_tokens", "mean_completion_tokens", "mean_cost_usd", "mean_latency_s",
]


def summary_rows(summary: dict) -> list[dict]:
    """Flatten the summary into CSV rows: overall first, then per category."""
    rows = [{"scope": "overall", **summary["overall"]}]
    rows += [{"scope": name, **block} for name, block in summary["by_category"].items()]
    for row in rows:
        for key, value in row.items():
            if isinstance(value, float):
                row[key] = round(value, 4)
    return rows

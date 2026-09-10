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

# A provenance tag the answering model copied out of its context and into its
# answer, e.g. "A fantasy novel by Patrick Rothfuss [D11:24]".
#
# System C's prompt explains what the [D7:11] tags on its facts are, and the
# model responded by citing its sources unprompted: on 712 of the 713 answers
# the judge marked correct in the C-cite run, and 808 of 809 in C-hydrate. The
# judge reads through it; token-overlap F1 cannot, and counts every tag token
# as a wrong token. Left alone, that depresses C's F1 by ~0.05 for a reason
# that has nothing to do with whether the answer is right, and makes C's F1
# incomparable with L, A, B and with published LoCoMo results.
#
# So the tag is stripped before F1 is computed, for EVERY system. Applying it
# uniformly is the point: for L, A and B it removes nothing at all (they emit
# no tags and their published F1 figures are unchanged to the last decimal),
# so one scorer is applied to every run rather than a special case to one.
# What is scored is the answer, not the citation formatting.
CITATION_TAG = re.compile(r"\s*\[\s*D\d+:\d+(?:\s*,\s*D\d+:\d+)*\s*\]")


def strip_citation_tags(text: str) -> str:
    """Remove trailing provenance tags from an answer before scoring it."""
    return CITATION_TAG.sub(" ", text or "").strip()


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


def locomo_f1(prediction: str, qa, strip_citations: bool = True) -> float:
    """Reference-compatible F1 for one QA item.

    `strip_citations` removes provenance tags the model copied into its answer
    (see CITATION_TAG). It is on by default and applied to every system; pass
    False for the raw figure, which the runner also records so both are
    reported and the size of the correction stays visible.
    """
    if strip_citations:
        prediction = strip_citation_tags(prediction)
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


def _mean_or_none(values) -> float | None:
    """Mean of the non-None values, or None if there are none at all.

    Distinct from `_mean`: a real 0.0 must stay 0.0, while "no applicable
    records" must stay None, so an un-retrieved metric is never reported as a
    score of zero.
    """
    present = [v for v in values if v is not None]
    return statistics.fmean(present) if present else None


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
            # The same answers scored without stripping provenance tags. Equal
            # to `f1` for every system that does not cite; the gap is the
            # measure of how much a citation habit costs on a token-overlap
            # metric.
            "f1_raw": _mean_or_none(r.get("f1_raw") for r in subset),
            "abstention_rate": _mean(r["abstained"] for r in subset),
            "context_overflow": sum(r["context_overflow"] for r in subset),
            "mean_prompt_tokens": _mean(r["prompt_tokens"] for r in subset_answered),
            "mean_completion_tokens": _mean(r["completion_tokens"] for r in subset_answered),
            "mean_cost_usd": _mean(r["cost_usd"] for r in subset_answered),
            # None, not 0.0, when every record was replayed from cache: a
            # cache hit is not a latency measurement, and reporting 0.00s would
            # look like an impossibly fast system.
            "mean_latency_s": _mean_or_none(r["latency_s"] for r in subset_live),
            # Retrieval-only. None for systems that do not retrieve and for
            # questions with no evidence listed, where recall is undefined
            # rather than zero. `_mean_or_none` keeps a genuine 0.0 distinct
            # from "not applicable" -- `or None` would silently conflate them.
            "evidence_recall": _mean_or_none(
                r.get("evidence_recall") for r in subset),
            # System C measures recall at more than one granularity. These are
            # DIFFERENT measurements of the same run and must never be read as
            # one number: turn-level is exact and comparable with System A,
            # session-level is an upper bound and comparable with System B,
            # and hydrated is the subset the hydrate arm actually showed the
            # model as raw text.
            "evidence_recall_turn": _mean_or_none(
                r.get("evidence_recall_turn") for r in subset),
            "evidence_recall_session": _mean_or_none(
                r.get("evidence_recall_session") for r in subset),
            "evidence_recall_hydrated": _mean_or_none(
                r.get("evidence_recall_hydrated") for r in subset),
            "mean_retrieval_cost_usd": _mean_or_none(
                r.get("retrieval_cost_usd") for r in subset_answered),
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
    "scope", "n", "judge_accuracy", "f1", "f1_raw", "abstention_rate", "context_overflow",
    "mean_prompt_tokens", "mean_completion_tokens", "mean_cost_usd", "mean_latency_s",
    "evidence_recall", "evidence_recall_turn", "evidence_recall_session",
    "evidence_recall_hydrated", "mean_retrieval_cost_usd",
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

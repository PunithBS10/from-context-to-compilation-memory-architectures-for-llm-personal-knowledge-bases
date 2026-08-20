"""LLM-as-judge: binary correct/incorrect plus a one-line reason.

Free-text answers cannot be exact-matched, and this is what the Mem0/LoCoMo
line of work does, so results stay comparable. The judge model and temperature
are fixed in config and recorded in every results file.

Adversarial items get a different rubric. There the question is unanswerable
from the conversation, so the correct behaviour is abstention and a confident
factual answer is wrong however plausible it sounds. That distinction matters
to this thesis: confidently answering from nothing is exactly the failure
provenance is meant to catch.
"""
from __future__ import annotations

import json

import config
from src.evaluation.metrics import is_abstention
from src.llm import LLMClient

JUDGE_SYSTEM = (
    "You grade answers to questions about a long conversation. You are strict "
    "about facts and lenient about wording. Reply with JSON only."
)

STANDARD_TEMPLATE = """Question: {question}
Reference answer: {gold}
Candidate answer: {prediction}

Mark the candidate CORRECT if it conveys the same information as the reference
answer. Differences in phrasing, extra detail, or a different but equivalent
date format do not make it wrong. Mark it INCORRECT if it states different
facts, misses the part of the reference answer the question asked for, or
declines to answer.

Reply with JSON only: {{"correct": true|false, "reason": "<one short sentence>"}}"""

ADVERSARIAL_TEMPLATE = """Question: {question}
This question CANNOT be answered from the conversation. The only correct
response is to say that the information is not available.

Candidate answer: {prediction}

Mark the candidate CORRECT if it says the information is not available, not
mentioned, or otherwise declines to answer. Mark it INCORRECT if it asserts an
answer, however plausible or hedged.

Reply with JSON only: {{"correct": true|false, "reason": "<one short sentence>"}}"""


class LLMJudge:
    def __init__(self, client: LLMClient, model: str = config.JUDGE_MODEL):
        self.client = client
        self.model = model

    def judge(self, qa, prediction: str) -> dict:
        """Grade one answer. Returns correct / reason / cost / judge_error."""
        # An empty answer (context overflow, or an empty completion) is wrong by
        # definition; spending a judge call on it just adds noise and cost.
        if not prediction.strip():
            return {"correct": False, "reason": "empty answer", "judge_cost_usd": 0.0,
                    "judge_called": False, "judge_error": None}

        if qa.abstention_expected:
            prompt = ADVERSARIAL_TEMPLATE.format(question=qa.question, prediction=prediction)
        else:
            prompt = STANDARD_TEMPLATE.format(
                question=qa.question, gold=qa.answer, prediction=prediction
            )

        response = self.client.complete(
            model=self.model,
            system_prompt=JUDGE_SYSTEM,
            user_prompt=prompt,
            max_tokens=config.MAX_JUDGE_TOKENS,
        )
        verdict = _parse_verdict(response.text)
        if verdict is None:
            # Fall back to the deterministic rule rather than silently scoring
            # zero, and flag it so unparsable verdicts can be counted.
            fallback = is_abstention(prediction) if qa.abstention_expected else False
            return {
                "correct": fallback,
                "reason": f"unparsable judge output: {response.text[:120]}",
                "judge_cost_usd": response.cost_usd,
                "judge_called": True,
                "judge_error": "unparsable",
            }
        return {
            "correct": bool(verdict.get("correct", False)),
            "reason": str(verdict.get("reason", ""))[:300],
            "judge_cost_usd": response.cost_usd,
            "judge_called": True,
            "judge_error": None,
        }


def _parse_verdict(text: str) -> dict | None:
    """Tolerate fenced code blocks and surrounding prose around the JSON."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None

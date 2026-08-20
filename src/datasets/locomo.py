"""LoCoMo loader: raw locomo10.json -> normalised Conversation objects.

Everything downstream (systems, runner, evaluation) talks to these dataclasses
only. No other module touches the raw JSON.

Real schema of locomo10.json (verified against the file, not assumed):

    [                                    # 10 conversation records
      {
        "sample_id": "conv-26",
        "conversation": {
          "speaker_a": "Caroline",
          "speaker_b": "Melanie",
          "session_1_date_time": "1:56 pm on 8 May, 2023",
          "session_1": [ {"speaker", "dia_id", "text",
                          # image turns additionally carry:
                          "img_url", "blip_caption", "query", "re-download"} ],
          ...
        },
        "qa": [ {"question", "answer", "evidence", "category"} ],
        "event_summary": {...}, "observation": {...}, "session_summary": {...}
      },
      ...
    ]

Three traps the real file contains, all handled below:

1. `session_N_date_time` keys exist with NO matching `session_N` turn list
   (conv-26 declares sessions 20-35 that have no content). Those are skipped.
2. Sessions must be ordered NUMERICALLY. Sorting the keys as strings puts
   session_10 before session_2 and silently scrambles the timeline, which
   would wreck the temporal-reasoning category.
3. Category 5 (adversarial) items usually have `answer: null` plus an
   `adversarial_answer` holding the plausible-but-wrong answer; the correct
   behaviour there is abstention. Two items are the exception: they carry a
   real answer ("No") alongside `adversarial_answer: "Yes"`, and there the
   real answer is the target. See `QAItem.abstention_expected`.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# Official LoCoMo category codes, confirmed against the reference scorer in
# snap-research/locomo (task_eval/evaluation.py), which scores category 1 with
# multi-answer F1, categories 2/3/4 with plain F1, and category 5 by checking
# the answer for an abstention phrase.
CATEGORY_NAMES = {
    1: "multi_hop",
    2: "temporal",
    3: "open_domain",
    4: "single_hop",
    5: "adversarial",
}

# The reference scorer counts an adversarial answer correct when it contains
# "no information available" or "not mentioned". We use the same target phrase
# so our numbers stay comparable with published LoCoMo results.
ABSTENTION_TARGET = "No information available"

_SESSION_RE = re.compile(r"^session_(\d+)$")


@dataclass
class Turn:
    speaker: str
    text: str
    session_id: str
    timestamp: str | None
    dia_id: str | None = None
    image_caption: str | None = None   # blip_caption of a shared image, if any

    def render(self) -> str:
        """One transcript line. Image turns keep their caption: some QA
        evidence points at image turns, so dropping it loses answerable facts.
        """
        if self.image_caption:
            return f"{self.speaker}: {self.text} [shares an image: {self.image_caption}]"
        return f"{self.speaker}: {self.text}"


@dataclass
class QAItem:
    question: str
    answer: str                  # normalised target, never None
    category: str                # readable name, not the raw int
    evidence: list | None
    category_code: int
    abstention_expected: bool = False        # adversarial, no real answer
    adversarial_answer: str | None = None    # the plausible-but-wrong answer
    qa_index: int = 0


@dataclass
class Session:
    session_id: str
    index: int
    timestamp: str | None
    turns: list[Turn]


@dataclass
class Conversation:
    conv_id: str
    turns: list[Turn]
    qa: list[QAItem]
    speakers: list[str] = field(default_factory=list)
    sessions: list[Session] = field(default_factory=list)

    @property
    def n_sessions(self) -> int:
        return len(self.sessions)


def _parse_sessions(conv: dict) -> list[Session]:
    """Pull out the sessions that actually have turns, in numeric order."""
    sessions: list[Session] = []
    for key, value in conv.items():
        match = _SESSION_RE.match(key)
        if not match or not isinstance(value, list):
            continue                      # trap 1: date_time-only keys
        index = int(match.group(1))
        timestamp = conv.get(f"{key}_date_time")
        turns = [
            Turn(
                speaker=str(turn.get("speaker", "unknown")),
                text=str(turn.get("text", "")).strip(),
                session_id=key,
                timestamp=timestamp,
                dia_id=turn.get("dia_id"),
                image_caption=(turn.get("blip_caption") or None),
            )
            for turn in value
        ]
        sessions.append(Session(session_id=key, index=index, timestamp=timestamp, turns=turns))
    sessions.sort(key=lambda s: s.index)   # trap 2: numeric, not lexicographic
    return sessions


def _parse_qa(item: dict, index: int) -> QAItem:
    code = int(item["category"])
    raw_answer = item.get("answer")
    adversarial = item.get("adversarial_answer")

    abstention_expected = code == 5 and raw_answer is None      # trap 3
    if abstention_expected:
        answer = ABSTENTION_TARGET
    else:
        answer = str(raw_answer)           # a few gold answers are ints, e.g. 2022

    return QAItem(
        question=str(item["question"]).strip(),
        answer=answer.strip(),
        category=CATEGORY_NAMES.get(code, f"unknown_{code}"),
        category_code=code,
        evidence=item.get("evidence"),
        abstention_expected=abstention_expected,
        adversarial_answer=str(adversarial) if adversarial is not None else None,
        qa_index=index,
    )


def load_locomo(path: str | Path, limit: int | None = None) -> list[Conversation]:
    """Load LoCoMo. `limit` takes the first N conversations: use it to prove
    the loop end to end before paying for a full run.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"LoCoMo data not found at {path}. Download it with:\n"
            "  curl -L -o data/locomo/locomo10.json "
            "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json"
        )

    with path.open(encoding="utf-8") as handle:
        raw = json.load(handle)

    records = raw[:limit] if limit else raw
    conversations = []
    for record in records:
        sessions = _parse_sessions(record["conversation"])
        meta = record["conversation"]
        speakers = [s for s in (meta.get("speaker_a"), meta.get("speaker_b")) if s]
        conversations.append(
            Conversation(
                conv_id=record["sample_id"],
                turns=[turn for session in sessions for turn in session.turns],
                qa=[_parse_qa(q, i) for i, q in enumerate(record["qa"])],
                speakers=speakers,
                sessions=sessions,
            )
        )
    return conversations

"""Print the real schema of locomo10.json.

Run this before trusting the loader. It is the evidence that
src/datasets/locomo.py codes against the actual file rather than an assumed
shape, and it re-checks the three traps documented there.

    python scripts/inspect_locomo.py
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import config
from src.datasets.locomo import CATEGORY_NAMES, load_locomo


def main() -> int:
    with config.LOCOMO_PATH.open(encoding="utf-8") as fh:
        raw = json.load(fh)

    print(f"top level: {type(raw).__name__}, {len(raw)} records")
    print(f"record keys: {list(raw[0].keys())}\n")

    print("--- one full sample record (record 0, trimmed to what matters) ---")
    record = raw[0]
    conv = record["conversation"]
    sample = {
        "sample_id": record["sample_id"],
        "conversation": {
            "speaker_a": conv["speaker_a"],
            "speaker_b": conv["speaker_b"],
            "session_1_date_time": conv["session_1_date_time"],
            "session_1 (first 3 of %d turns)" % len(conv["session_1"]): conv["session_1"][:3],
            "...": "%d session keys in total" % len(conv),
        },
        "qa (first 2 of %d)" % len(record["qa"]): record["qa"][:2],
        "other keys": [k for k in record if k not in ("sample_id", "conversation", "qa")],
    }
    print(json.dumps(sample, indent=2)[:2600])

    print("\n--- turn key sets across the whole file ---")
    turn_keys = Counter()
    for rec in raw:
        for value in rec["conversation"].values():
            if isinstance(value, list):
                for turn in value:
                    turn_keys[tuple(sorted(turn.keys()))] += 1
    for keys, count in turn_keys.most_common():
        print(f"  {count:>5}  {keys}")

    print("\n--- QA key sets and categories ---")
    qa_keys, categories = Counter(), Counter()
    for rec in raw:
        for qa in rec["qa"]:
            qa_keys[tuple(sorted(qa.keys()))] += 1
            categories[qa["category"]] += 1
    for keys, count in qa_keys.most_common():
        print(f"  {count:>5}  {keys}")
    for code, count in sorted(categories.items()):
        print(f"  category {code} = {CATEGORY_NAMES[code]:<12} {count:>5}")

    print("\n--- traps ---")
    for rec in raw:
        conv = rec["conversation"]
        orphans = [k for k in conv if k.endswith("_date_time") and k[:-10] not in conv]
        if orphans:
            print(f"  {rec['sample_id']}: {len(orphans)} session dates with no turns "
                  f"(e.g. {orphans[0]}) - skipped by the loader")
    dual = [(rec["sample_id"], qa) for rec in raw for qa in rec["qa"]
            if "adversarial_answer" in qa and qa.get("answer") is not None]
    print(f"  {len(dual)} adversarial items carry a real answer, so abstention is NOT the target:")
    for sample_id, qa in dual:
        print(f"    {sample_id}: {qa['question']} -> answer {qa['answer']!r}, "
              f"adversarial_answer {qa['adversarial_answer']!r}")

    print("\n--- as loaded ---")
    conversations = load_locomo(config.LOCOMO_PATH)
    print(f"  {len(conversations)} conversations, "
          f"{sum(len(c.turns) for c in conversations)} turns, "
          f"{sum(len(c.qa) for c in conversations)} QA items")
    for conversation in conversations:
        print(f"    {conversation.conv_id}: sessions {conversation.n_sessions:>2} "
              f"(indices ordered: {[s.index for s in conversation.sessions] == sorted(s.index for s in conversation.sessions)})"
              f"  turns {len(conversation.turns):>4}  qa {len(conversation.qa):>4}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

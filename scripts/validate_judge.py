"""Check the LLM judge against your own hand labels.

The spec asks for this once, early: hand-label ~30 answers and report the
agreement figure in the thesis. It is what defends the evaluation method in
the viva.

Two steps.

1. Draw a stratified sample from a results file. The judge's verdict is NOT in
   the CSV, so your labelling cannot be anchored by it:

       python scripts/validate_judge.py sample results/system_l_locomo_<stamp>.json -n 30

2. Fill in the `human_correct` column with 1 or 0, then score the agreement:

       python scripts/validate_judge.py score results/judge_validation_<stamp>.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import config

SAMPLE_COLUMNS = [
    "results_file", "conv_id", "qa_index", "category",
    "question", "gold_answer", "prediction", "human_correct",
]


def cmd_sample(args) -> int:
    payload = json.loads(Path(args.results).read_text(encoding="utf-8"))
    records = payload["records"]

    # A decline ("No information available") is near-mechanical to label: correct
    # for an adversarial item, incorrect for an answerable one. They made up 40%
    # of the first 30-row sample, so only ~18 rows actually tested the judge's
    # semantic judgement. --no-declines draws a top-up from the cases where the
    # judge has to compare meaning, giving a second, harder agreement figure.
    if args.no_declines:
        records = [r for r in records if not r["abstained"] and r["prediction"].strip()]
        if not records:
            print("No non-decline records in this results file.")
            return 1

    exclude = set()
    if args.exclude:
        for path in args.exclude:
            for row in csv.DictReader(Path(path).open(encoding="utf-8")):
                exclude.add((row["conv_id"], int(row["qa_index"])))
        before = len(records)
        records = [r for r in records if (r["conv_id"], r["qa_index"]) not in exclude]
        print(f"excluded {before - len(records)} rows already labelled in "
              f"{len(args.exclude)} previous sample(s)")

    by_category = defaultdict(list)
    for record in records:
        by_category[record["category"]].append(record)

    # Stratified: every category represented, remainder filled at random, so
    # the adversarial category cannot vanish from a 30-item sample.
    random.seed(args.seed)
    per_category = max(1, args.n // len(by_category))
    chosen = []
    for category, items in sorted(by_category.items()):
        chosen += random.sample(items, min(per_category, len(items)))
    remaining = [r for r in records if r not in chosen]
    if len(chosen) < args.n and remaining:
        chosen += random.sample(remaining, min(args.n - len(chosen), len(remaining)))
    random.shuffle(chosen)

    stamp = Path(args.results).stem.replace("system_", "")
    out_path = Path(args.out) if args.out else config.RESULTS_DIR / f"judge_validation_{stamp}.csv"
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=SAMPLE_COLUMNS)
        writer.writeheader()
        for record in chosen:
            writer.writerow({
                "results_file": Path(args.results).name,
                "conv_id": record["conv_id"],
                "qa_index": record["qa_index"],
                "category": record["category"],
                "question": record["question"],
                # For adversarial items the gold answer IS abstention; say so
                # plainly so the labelling rule is unambiguous.
                "gold_answer": ("[unanswerable - correct response is to decline]"
                                if record["abstention_expected"] else record["gold_answer"]),
                "prediction": record["prediction"],
                "human_correct": "",
            })

    print(f"{len(chosen)} answers written to {out_path}")
    print("Categories:", dict(Counter(r["category"] for r in chosen)))
    print("Fill in human_correct with 1 (correct) or 0 (incorrect), then run:")
    print(f"  python scripts/validate_judge.py score {out_path}")
    return 0


def cmd_score(args) -> int:
    rows = list(csv.DictReader(Path(args.labels).open(encoding="utf-8")))
    labelled = [r for r in rows if r["human_correct"].strip() in ("0", "1")]
    if not labelled:
        print("No rows labelled yet: fill in the human_correct column with 1 or 0.")
        return 1

    results_file = args.results or (config.RESULTS_DIR / labelled[0]["results_file"])
    payload = json.loads(Path(results_file).read_text(encoding="utf-8"))
    verdicts = {(r["conv_id"], r["qa_index"]): r for r in payload["records"]}

    agree = 0
    both_yes = both_no = judge_only = human_only = 0
    disagreements = []
    for row in labelled:
        record = verdicts[(row["conv_id"], int(row["qa_index"]))]
        human = row["human_correct"].strip() == "1"
        judge = bool(record["judge_correct"])
        if human == judge:
            agree += 1
            both_yes += human
            both_no += not human
        else:
            judge_only += judge and not human
            human_only += human and not judge
            disagreements.append((row, record, human, judge))

    n = len(labelled)
    accuracy = agree / n
    # Cohen's kappa: agreement corrected for chance, which raw agreement
    # flatters when one verdict dominates.
    p_judge = (both_yes + judge_only) / n
    p_human = (both_yes + human_only) / n
    expected = p_judge * p_human + (1 - p_judge) * (1 - p_human)
    kappa = (accuracy - expected) / (1 - expected) if expected < 1 else 1.0

    print(f"Hand-labelled answers : {n}")
    print(f"Judge/human agreement : {accuracy:.1%} ({agree}/{n})")
    print(f"Cohen's kappa         : {kappa:.3f}")
    print(f"  both correct {both_yes} | both incorrect {both_no} | "
          f"judge lenient {judge_only} | judge strict {human_only}")
    if disagreements:
        print(f"\n--- {len(disagreements)} disagreements ---")
        for row, record, human, judge in disagreements:
            print(f"[{row['category']}] {row['question']}")
            print(f"  gold      : {row['gold_answer']}")
            print(f"  predicted : {row['prediction']}")
            print(f"  judge {'correct' if judge else 'incorrect'}, "
                  f"you {'correct' if human else 'incorrect'} - {record['judge_reason']}\n")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_sample = sub.add_parser("sample", help="draw a stratified sample to hand-label")
    p_sample.add_argument("results", help="a results/*.json file")
    p_sample.add_argument("-n", type=int, default=30)
    p_sample.add_argument("-o", "--out", default=None)
    p_sample.add_argument("--seed", type=int, default=0)
    p_sample.add_argument("--no-declines", action="store_true",
                          help="sample only answers that attempted a real answer, "
                               "excluding 'No information available' declines")
    p_sample.add_argument("--exclude", nargs="*", default=None,
                          help="previous validation CSV(s) whose rows must not be redrawn")
    p_sample.set_defaults(func=cmd_sample)

    p_score = sub.add_parser("score", help="score agreement on a labelled CSV")
    p_score.add_argument("labels", help="the filled-in judge_validation_*.csv")
    p_score.add_argument("--results", default=None, help="override the results file")
    p_score.set_defaults(func=cmd_score)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

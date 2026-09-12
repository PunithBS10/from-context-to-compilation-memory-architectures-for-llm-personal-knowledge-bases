"""Run the forgetting pass over System C's wikis and report what it did.

Level 1 of System D's evaluation: measuring the MEMORY directly, before any
question is asked of it. What did forgetting find, what did it remove, and did
the provenance policy decide anything differently from plain recency? These
numbers stand regardless of whether any QA benchmark can see them - and the
research log predicts, in advance, that LoCoMo mostly cannot.

    python scripts/forget_wikis.py                 # both policies, all 10
    python scripts/forget_wikis.py --limit 1       # prove it on conv-26 first
    python scripts/forget_wikis.py --rebuild       # ignore pruned wikis on disk
    python scripts/forget_wikis.py --decisions conv-26   # print the log for one

Both policies read the same System C wiki, the same verification verdicts and
the same detected contradictions (results/forgetting/). Only the resolution
rule differs, which is the variable the ablation isolates. C's wikis are read
and never written.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import config
from src.datasets.locomo import load_locomo
from src.llm import LLMClient
from src.systems.forgetting import KINDS, POLICIES, Decision, disagreement, load_or_forget
from src.systems.wiki import Wiki


def cmd_run(args) -> int:
    conversations = load_locomo(config.LOCOMO_PATH, limit=args.limit)
    client = LLMClient(use_cache=not args.no_cache)
    print(f"forgetting pass over {config.WIKI_C_DIR.name}, checker {config.FORGET_MODEL}")
    print(f"policies: {', '.join(POLICIES)}\n")

    per_policy: dict[str, list[tuple[Wiki, list[Decision], dict]]] = {p: [] for p in POLICIES}
    for conversation in conversations:
        for policy in POLICIES:
            result = load_or_forget(
                client, conversation, policy, config.WIKI_C_DIR, config.WIKI_D_DIRS[policy],
                model=config.FORGET_MODEL, reuse=not args.rebuild, verbose=args.verbose)
            per_policy[policy].append(result)
        _print_row(conversation.conv_id, per_policy)

    _print_totals(per_policy)
    return 0


ROW = "{conv:<9}{policy:<12}{before:>7}{after:>7}{removed:>9}{unsup:>7}{found:>7}{res:>6}{trust:>7}{rec:>7}{tok:>10}"


def _print_row(conv_id: str, per_policy: dict, header: bool = False) -> None:
    for policy in POLICIES:
        wiki, decisions, r = per_policy[policy][-1]
        print(ROW.format(
            conv=conv_id if policy == POLICIES[0] else "", policy=policy,
            before=r["facts_before"], after=r["facts_after"],
            removed=f"{r['facts_removed_pct']:.1%}", unsup=r["unsupported_dropped"],
            found=r["contradictions_detected"], res=r["contradictions_resolved"],
            trust=r["decided_by_trust"], rec=r["decided_by_recency"],
            tok=f"{r['wiki_tokens_after']:,}"))


def _print_totals(per_policy: dict) -> None:
    print("-" * 88)
    print(ROW.format(conv="conv", policy="policy", before="before", after="after",
                     removed="removed", unsup="unsup", found="found", res="res",
                     trust="trust", rec="recncy", tok="tokens"))
    print("=" * 88)

    def total(policy: str, key: str):
        return sum(r[key] for _, _, r in per_policy[policy])

    for policy in POLICIES:
        rows = per_policy[policy]
        before, after = total(policy, "facts_before"), total(policy, "facts_after")
        print(ROW.format(
            conv="TOTAL" if policy == POLICIES[0] else "", policy=policy,
            before=before, after=after, removed=f"{(before - after) / before:.1%}",
            unsup=total(policy, "unsupported_dropped"),
            found=total(policy, "contradictions_detected"),
            res=total(policy, "contradictions_resolved"),
            trust=total(policy, "decided_by_trust"), rec=total(policy, "decided_by_recency"),
            tok=f"{total(policy, 'wiki_tokens_after'):,}"))

    # The shared passes: verdict distribution and cost, counted once.
    prov = per_policy["provenance"]
    verdicts = {}
    for _, _, r in prov:
        for k, v in r["verdicts"].items():
            verdicts[k] = verdicts.get(k, 0) + v
    n = sum(verdicts.values()) or 1
    kinds = {k: sum(r["contradictions_by_kind"][k] for _, _, r in prov) for k in KINDS}
    cost = sum(r.get("passes", {}).get("passes_cost_usd", 0.0) for _, _, r in prov)
    vcalls = sum(r.get("passes", {}).get("verify_calls", 0) for _, _, r in prov)
    dcalls = sum(r.get("passes", {}).get("detect_calls", 0) for _, _, r in prov)
    unread = sum(r.get("passes", {}).get("verify_unreadable", 0) for _, _, r in prov)
    tokens_before = sum(r["wiki_tokens_before"] for _, _, r in prov)

    print()
    print("--- verification (shared by both policies) ---")
    for k in ("supported", "partially", "unsupported", "unreadable"):
        print(f"  {k:<12}{verdicts.get(k, 0):>6}  ({verdicts.get(k, 0) / n:.1%})")
    print(f"  {vcalls} calls, {unread} facts with no readable verdict")
    print("--- contradiction detection (shared) ---")
    for k in KINDS:
        print(f"  {k:<14}{kinds[k]:>5}")
    print(f"  {dcalls} pages checked; within-page only (cross-page not detected)")
    print(f"--- passes cost ${cost:.4f}; wiki tokens before {tokens_before:,} ---")

    # THE ABLATION
    print()
    print("=== policy disagreement: provenance vs recency ===")
    agg = {"pairs_compared": 0, "disagreements": 0,
           "by_kind": {k: {"pairs": 0, "disagree": 0} for k in KINDS}}
    for (pw, pd, _), (rw, rd, _) in zip(per_policy["provenance"], per_policy["recency"]):
        d = disagreement(pd, rd)
        agg["pairs_compared"] += d["pairs_compared"]
        agg["disagreements"] += d["disagreements"]
        for k in KINDS:
            agg["by_kind"][k]["pairs"] += d["by_kind"][k]["pairs"]
            agg["by_kind"][k]["disagree"] += d["by_kind"][k]["disagree"]
    pc, dg = agg["pairs_compared"], agg["disagreements"]
    print(f"contradictions resolved by both : {pc}")
    print(f"chose differently               : {dg}  "
          f"({dg / pc:.1%})" if pc else "chose differently               : -")
    for k in KINDS:
        b = agg["by_kind"][k]
        rate = f"{b['disagree'] / b['pairs']:.1%}" if b["pairs"] else "-"
        print(f"  {k:<14} {b['disagree']:>4} of {b['pairs']:>4}  ({rate})")
    if pc and dg == 0:
        print("\nNOTE: zero disagreement - provenance-driven forgetting behaved exactly "
              "like recency on this corpus. That is the null result and must be reported.")


def cmd_decisions(args) -> int:
    """Print one conversation's decision log, both policies side by side."""
    logs = {}
    for policy in POLICIES:
        path = config.WIKI_D_DIRS[policy] / args.conv_id / "decisions.json"
        if not path.exists():
            raise SystemExit(f"no decisions for {args.conv_id} under {policy}; run the pass first")
        logs[policy] = json.loads(path.read_text(encoding="utf-8"))

    prov = [d for d in logs["provenance"]["decisions"]]
    rec_by = {(d["page_slug"], d["fact_a"], d["fact_b"]): d for d in logs["recency"]["decisions"]}
    print(f"# {args.conv_id}: {len(prov)} contradictions "
          f"({sum(1 for d in prov if d['decided_by'] != 'moot')} resolved)\n")
    for n, d in enumerate(prov, 1):
        r = rec_by.get((d["page_slug"], d["fact_a"], d["fact_b"]))
        flag = ""
        if r and d["decided_by"] != "moot" and r["winner"] != d["winner"]:
            flag = "   <-- POLICIES DISAGREE"
        print(f"--- {n}. page {d['page_slug']} · {d['kind']}{flag}")
        print(f"    detector: {d['reason']}")
        for fid, text, sig, trust in ((d["fact_a"], d["text_a"], d["signals_a"], d["trust_a"]),
                                      (d["fact_b"], d["text_b"], d["signals_b"], d["trust_b"])):
            mark = "KEEP" if fid == d["winner"] else "drop"
            print(f"    [{mark}] {fid}: {text}")
            print(f"           session {sig['session_index']} ({sig['date_label']}) · "
                  f"verified={sig['verified']} · first_hand={sig['first_hand']} "
                  f"(subject {sig['subject']}, cited speakers {sig['cited_speakers']}) · "
                  f"cited {sig['n_cited_turns']} turn(s) -> trust {trust}")
        print(f"    provenance: kept {d['winner']} by {d['decided_by']}"
              + (f" | recency: kept {r['winner']}" if r else ""))
        print()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--rebuild", action="store_true",
                        help="re-run the pass even when a pruned wiki is on disk")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--decisions", dest="conv_id", default=None,
                        help="print the decision log for one conversation and exit")
    args = parser.parse_args(argv)
    if args.conv_id:
        return cmd_decisions(args)
    return cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())

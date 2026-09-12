"""Wiki fidelity audit: what percentage of compiled facts are actually true?

This is the measurement System B exists for. Systems L and A fail transiently --
one wrong answer to one question. System B can fail permanently: if the
compiler writes "Caroline ran a charity race" into the wiki, that falsehood
*becomes the memory*, and every later question touching it inherits the error.
That is the hallucination-into-compiled-memory problem this thesis is about, so
it is measured rather than asserted.

Every sampled fact is compared against the turns of the session it was
extracted from, and classified:

    supported    the session says this
    distorted    related to something in the session but altered -- wrong
                 subject, wrong date, overstated, detail changed
    unsupported  nothing in the session says this

Misattribution (a fact pinned on the wrong person) is flagged separately: it is
the failure mode LoCoMo's adversarial questions probe, and averaging it into a
single error rate would hide it.

The first pass is automated with a DIFFERENT and stronger model than the
compiler, for the same reason the judge is not the answerer. A subset is then
hand-verified: a claim about hallucination rates that rests entirely on a model
checking a model will not survive a viva.

System C sharpens the question. Where `check` asks "is this fact faithful to
its session?", `citations` asks "is it faithful to the *turn it names*?" -- a
stricter test that only a wiki carrying per-fact provenance can be given, and
the one that catches a fact which is locally plausible but pinned to the wrong
line. It adds a verdict `check` has no way to express:

    partially   the cited turns state part of it; the rest is in the session
                but on a turn the fact does not cite

    python scripts/audit_wiki.py stats                       # size + compression
    python scripts/audit_wiki.py check -n 100                # the LLM pass
    python scripts/audit_wiki.py citations -n 100            # System C: per-TURN
    python scripts/audit_wiki.py score results/wiki_fidelity_<stamp>.csv

`check` also writes a `_sheet.md` review sheet: each sampled fact printed above
the session it came from, so hand-verification needs nothing but that file.
Fill in `human_verdict` (supported/distorted/unsupported) and
`human_misattribution` (1/0) in the CSV, then run `score` again.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import config
from src.datasets.locomo import load_locomo
from src.llm import LLMClient
from src.systems.forgetting import SUPPORT_RUBRIC
from src.systems.wiki import Wiki, parse_json

VERDICTS = ("supported", "distorted", "unsupported")

# The citation audit's classes. "partially" is the one the per-session audit
# cannot express: the fact is true of the session but the turn it names does
# not carry all of it, which is a provenance defect rather than a fidelity one.
CITATION_VERDICTS = ("supported", "partially", "unsupported")

CITATION_SYSTEM = (
    "You check whether a compiled fact is supported by the specific conversation "
    "turns it cites. You are strict about WHICH turn says what. Reply with JSON only."
)

CITATION_TEMPLATE = """Below is one session of a conversation between {speakers}, which took place on {date}.

{transcript}

A knowledge base compiled from this conversation contains this entry:

    "{fact}"   (filed on the page "{page}", dated {date_label})

The entry cites these turns as its source:

{cited}

Decide whether the CITED TURNS support the entry:

""" + SUPPORT_RUBRIC + """

Also decide whether the entry attributes something to the WRONG PERSON compared
with what the cited turns actually say.

Reply with JSON only:
{{"verdict": "supported|partially|unsupported", "misattribution": true|false, "reason": "<one short sentence>"}}"""

CITATION_COLUMNS = [
    "conv_id", "fact_id", "page_title", "page_type", "session_index", "date_label",
    "cited_dia_ids", "fact", "verdict", "misattribution", "reason",
    "human_verdict", "human_misattribution",
]

CHECK_SYSTEM = (
    "You verify a compiled knowledge base against its source. You are strict: "
    "a fact counts as supported only if the source session actually says it. "
    "Reply with JSON only."
)

CHECK_TEMPLATE = """Below is one session of a conversation between {speakers}, which took place on {date}.

{transcript}

A knowledge base was compiled from this conversation. One of its entries reads:

    "{fact}"   (filed on the page "{page}", dated {date_label})

Classify that entry against the session above:

- "supported"   - the session states this, allowing for rewording and summary
- "distorted"   - the session says something related but this entry alters it:
                  wrong person, wrong date, wrong detail, or an overstatement
- "unsupported" - nothing in the session says this; it is invented, or it is
                  about something the session never mentions

Also decide whether the entry attributes something to the WRONG PERSON --
whether it says one speaker did or felt something that the session attributes
to the other speaker, or to someone else entirely.

Reply with JSON only:
{{"verdict": "supported|distorted|unsupported", "misattribution": true|false, "reason": "<one short sentence>"}}"""

SAMPLE_COLUMNS = [
    "conv_id", "fact_id", "page_title", "page_type", "session_index", "date_label",
    "fact", "verdict", "misattribution", "reason", "human_verdict", "human_misattribution",
]


# --- loading ----------------------------------------------------------------
def load_wikis(wiki_dir: Path) -> list[Wiki]:
    wikis = []
    for directory in sorted(Path(wiki_dir).glob("*/")):
        wiki = Wiki.load(wiki_dir, directory.name)
        if wiki is not None:
            wikis.append(wiki)
    if not wikis:
        raise SystemExit(
            f"No compiled wikis in {wiki_dir}. Build them first:\n"
            "  python -m src.runner --system b --dry-run")
    return wikis


def load_sessions(conv_ids: set[str]) -> dict[tuple[str, str], dict]:
    """(conv_id, session_id) -> rendered turns, date and speakers.

    The audit has to compare each fact against exactly what the compiler saw,
    so the source text is rebuilt from the dataset rather than from the wiki.
    """
    sessions = {}
    for conversation in load_locomo(config.LOCOMO_PATH):
        if conversation.conv_id not in conv_ids:
            continue
        speakers = (" and ".join(conversation.speakers)
                    if conversation.speakers else "two people")
        for session in conversation.sessions:
            sessions[(conversation.conv_id, session.session_id)] = {
                "speakers": speakers,
                "timestamp": session.timestamp or "an unrecorded date",
                "transcript": "\n".join(t.render() for t in session.turns),
                # The citation audit shows the transcript with ids, exactly as
                # the compiler saw it, plus the individual cited turns quoted
                # on their own. Checking a citation against a session with the
                # ids hidden would be checking something else.
                "transcript_ids": "\n".join(
                    f"[{t.dia_id}] {t.render()}" if t.dia_id else t.render()
                    for t in session.turns),
                "turns": {t.dia_id: t.render() for t in session.turns if t.dia_id},
            }
    return sessions


# --- stats ------------------------------------------------------------------
def cmd_stats(args) -> int:
    """Wiki size: pages, facts, and compiled tokens versus raw tokens."""
    wikis = load_wikis(Path(args.wiki_dir))
    rows = []
    for wiki in wikis:
        stats = wiki.stats
        types = Counter(p.type for p in wiki.pages)
        rows.append({
            "conv_id": wiki.conv_id,
            "pages": stats.get("n_pages", len(wiki.pages)),
            "person": types["person"], "event": types["event"], "topic": types["topic"],
            "facts": stats.get("n_facts", len(wiki.facts)),
            "sessions": stats.get("sessions_compiled", len(wiki.sessions)),
            "raw_tokens": stats.get("raw_conversation_tokens", 0),
            "wiki_tokens": stats.get("wiki_tokens", 0),
            "compression": stats.get("compression_ratio"),
            "compile_cost_usd": round(stats.get("compile_cost_usd", 0.0), 4),
            "salvaged": stats.get("salvaged_sessions", 0),
            "unparsable": stats.get("unparsable_sessions", 0),
            "empty": stats.get("empty_sessions", 0),
            "truncated": stats.get("truncated_sessions", 0),
            "generic_titles": stats.get("generic_page_titles", 0),
            # System C only. Zero throughout a System B wiki, whose compiler
            # was never asked for citations.
            "citations": stats.get("citations_total", 0),
            "unresolvable": stats.get("citations_unresolvable", 0),
            "other_session": stats.get("citations_other_session", 0),
            "nonexistent": stats.get("citations_nonexistent", 0),
            "no_citation": stats.get("facts_with_no_citation", 0),
        })

    header = (f"{'conv':<10}{'pages':>6}{'per':>5}{'evt':>5}{'top':>5}{'facts':>7}"
              f"{'sess':>6}{'raw tok':>10}{'wiki tok':>10}{'compr':>8}{'$':>9}"
              f"{'salv':>6}{'unpar':>7}{'empty':>7}{'trunc':>7}")
    print(header)
    print("-" * len(header))
    for row in rows:
        print(f"{row['conv_id']:<10}{row['pages']:>6}{row['person']:>5}{row['event']:>5}"
              f"{row['topic']:>5}{row['facts']:>7}{row['sessions']:>6}"
              f"{row['raw_tokens']:>10,}{row['wiki_tokens']:>10,}"
              f"{(row['compression'] or 0):>8.3f}{row['compile_cost_usd']:>9.4f}"
              f"{row['salvaged']:>6}{row['unparsable']:>7}{row['empty']:>7}"
              f"{row['truncated']:>7}")
    print("-" * len(header))
    totals = {key: sum(r[key] for r in rows)
              for key in ("pages", "facts", "raw_tokens", "wiki_tokens", "salvaged",
                          "unparsable", "empty", "truncated", "generic_titles",
                          "citations", "unresolvable", "other_session",
                          "nonexistent", "no_citation")}
    cost = sum(r["compile_cost_usd"] for r in rows)
    print(f"{len(rows)} wikis | {totals['pages']} pages | {totals['facts']} facts | "
          f"{totals['wiki_tokens']:,} compiled tokens from {totals['raw_tokens']:,} raw "
          f"({totals['wiki_tokens'] / totals['raw_tokens']:.1%}) | compile cost ${cost:.4f}")
    print(f"facts per conversation: mean {statistics.fmean(r['facts'] for r in rows):.0f}, "
          f"per page {totals['facts'] / totals['pages']:.1f}")
    print(f"extraction problems: {totals['salvaged']} sessions salvaged from malformed "
          f"JSON, {totals['unparsable']} unreadable, {totals['empty']} empty, "
          f"{totals['truncated']} truncated, {totals['generic_titles']} facts filed "
          f"under a category name and re-filed")

    if totals["citations"]:
        cites = totals["citations"]
        print(f"citations: {cites} ids on {totals['facts']} facts "
              f"({cites / totals['facts']:.2f} per fact) | "
              f"resolvability {(cites - totals['nonexistent']) / cites:.4f} | "
              f"session consistency {(cites - totals['unresolvable']) / cites:.4f}")
        print(f"  unresolvable {totals['unresolvable']} "
              f"({totals['other_session']} named another session, "
              f"{totals['nonexistent']} named no turn at all) | "
              f"{totals['no_citation']} facts kept with no citation "
              f"({totals['no_citation'] / totals['facts']:.2%})")

    if args.out:
        out = Path(args.out)
        with out.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        print(f"\nwritten: {out}")
    return 0


# --- the LLM pass -----------------------------------------------------------
def sample_facts(wikis: list[Wiki], n: int, seed: int) -> list[tuple[Wiki, object]]:
    """Draw n facts, stratified by page type and spread across conversations.

    Person, event and topic pages fail differently -- a person page invites
    misattribution, an event page invites wrong dates -- so a sample that
    happened to be mostly one type would not describe the wiki. Within a type,
    conversations are taken round-robin so no single conversation dominates.
    Deterministic given the seed.
    """
    rng = random.Random(seed)
    by_type: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for wiki in wikis:
        for fact in wiki.facts:
            by_type[fact.page_type][wiki.conv_id].append((wiki, fact))

    available = {t: sum(len(v) for v in convs.values()) for t, convs in by_type.items()}
    total = sum(available.values())
    if not total:
        raise SystemExit("The compiled wikis contain no facts.")

    # Equal share per type where the wiki can supply it, so the rarer page
    # types are not swamped; anything a type cannot supply is redistributed.
    quota = {t: min(available[t], max(1, n // len(by_type))) for t in by_type}
    while sum(quota.values()) < min(n, total):
        for page_type in sorted(by_type, key=lambda t: -available[t]):
            if quota[page_type] < available[page_type] and sum(quota.values()) < min(n, total):
                quota[page_type] += 1

    chosen: list[tuple[Wiki, object]] = []
    for page_type, convs in sorted(by_type.items()):
        queues = []
        for conv_id in sorted(convs):
            items = list(convs[conv_id])
            rng.shuffle(items)
            queues.append(items)
        taken = 0
        while taken < quota[page_type] and any(queues):
            for queue in queues:
                if queue and taken < quota[page_type]:
                    chosen.append(queue.pop())
                    taken += 1
    rng.shuffle(chosen)
    return chosen


def check_fact(client: LLMClient, model: str, fact, source: dict) -> dict:
    prompt = CHECK_TEMPLATE.format(
        speakers=source["speakers"], date=source["timestamp"],
        transcript=source["transcript"], fact=fact.text,
        page=fact.page_title, date_label=fact.date_label,
    )
    response = client.complete(model=model, system_prompt=CHECK_SYSTEM,
                               user_prompt=prompt, max_tokens=config.MAX_AUDIT_TOKENS)
    parsed = parse_json(response.text)
    if not isinstance(parsed, dict) or parsed.get("verdict") not in VERDICTS:
        # Unreadable verdicts are counted, never silently scored as supported:
        # that would flatter exactly the number this audit exists to report.
        return {"verdict": "unreadable", "misattribution": False,
                "reason": f"unparsable check output: {response.text[:120]}",
                "cost_usd": response.cost_usd}
    return {"verdict": parsed["verdict"],
            "misattribution": bool(parsed.get("misattribution", False)),
            "reason": str(parsed.get("reason", ""))[:300],
            "cost_usd": response.cost_usd}


def cmd_check(args) -> int:
    from tqdm import tqdm

    wikis = load_wikis(Path(args.wiki_dir))
    chosen = sample_facts(wikis, args.n, args.seed)
    sources = load_sessions({w.conv_id for w in wikis})
    client = LLMClient(use_cache=not args.no_cache)

    rows, cost = [], 0.0
    for wiki, fact in tqdm(chosen, desc="checking facts", unit="fact"):
        source = sources.get((wiki.conv_id, fact.session_id))
        if source is None:            # a fact whose session vanished from the data
            continue
        result = check_fact(client, args.model, fact, source)
        cost += result["cost_usd"]
        rows.append({
            "conv_id": wiki.conv_id, "fact_id": fact.fact_id,
            "page_title": fact.page_title, "page_type": fact.page_type,
            "session_index": fact.session_index, "date_label": fact.date_label,
            "fact": fact.text, "verdict": result["verdict"],
            "misattribution": int(result["misattribution"]), "reason": result["reason"],
            "human_verdict": "", "human_misattribution": "",
        })

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    csv_path = Path(args.out) if args.out else config.RESULTS_DIR / f"wiki_fidelity_{stamp}.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=SAMPLE_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    json_path = csv_path.with_suffix(".json")
    json_path.write_text(json.dumps({
        "audit": {"checked_utc": stamp, "checker_model": args.model,
                  "compiler_model": wikis[0].extraction_model,
                  "build_fingerprint": wikis[0].build_fingerprint,
                  "n_sampled": len(rows), "seed": args.seed,
                  "wikis": [w.conv_id for w in wikis], "cost_usd": cost},
        "rows": rows,
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    sheet_path = _write_review_sheet(csv_path, rows, sources)

    print(f"\n{len(rows)} facts checked by {args.model} for ${cost:.4f}")
    _report(rows)
    print(f"\ncsv   : {csv_path}")
    print(f"json  : {json_path}")
    print(f"sheet : {sheet_path}")
    print("\nHand-verify a subset: read the sheet, fill in human_verdict "
          "(supported/distorted/unsupported) and human_misattribution (1/0) in the "
          f"CSV, then run:\n  python scripts/audit_wiki.py score {csv_path}")
    return 0


def check_citation(client: LLMClient, model: str, fact, source: dict) -> dict:
    """Does the turn this fact NAMES actually say it?"""
    cited = "\n".join(f"[{d}] {source['turns'].get(d, '(turn not found)')}"
                      for d in fact.source_dia_ids)
    prompt = CITATION_TEMPLATE.format(
        speakers=source["speakers"], date=source["timestamp"],
        transcript=source["transcript_ids"], fact=fact.text,
        page=fact.page_title, date_label=fact.date_label, cited=cited,
    )
    response = client.complete(model=model, system_prompt=CITATION_SYSTEM,
                               user_prompt=prompt, max_tokens=config.MAX_AUDIT_TOKENS)
    parsed = parse_json(response.text)
    if not isinstance(parsed, dict) or parsed.get("verdict") not in CITATION_VERDICTS:
        return {"verdict": "unreadable", "misattribution": False,
                "reason": f"unparsable check output: {response.text[:120]}",
                "cost_usd": response.cost_usd}
    return {"verdict": parsed["verdict"],
            "misattribution": bool(parsed.get("misattribution", False)),
            "reason": str(parsed.get("reason", ""))[:300],
            "cost_usd": response.cost_usd}


def cmd_citations(args) -> int:
    """System C's measurement: is a fact faithful to the turn it cites?

    Levels 1 and 2 of the citation check -- does the id name a real turn, and
    a turn of the right session -- are deterministic and already reported by
    `stats`. This is level 3, the only one that needs a model, and it is the
    only one that can catch a fact whose citation points at the wrong line.
    """
    from tqdm import tqdm

    wiki_dir = Path(args.wiki_dir if args.wiki_dir != str(config.WIKI_DIR)
                    else config.WIKI_C_DIR)
    wikis = load_wikis(wiki_dir)
    cited_only = [w for w in wikis if any(f.source_dia_ids for f in w.facts)]
    if not cited_only:
        raise SystemExit(
            f"No facts in {wiki_dir} carry citations. This mode audits System C's "
            f"wikis; build them with:\n  python scripts/compile_wikis.py --system c")

    chosen = [(w, f) for w, f in sample_facts(cited_only, args.n, args.seed)
              if f.source_dia_ids]
    sources = load_sessions({w.conv_id for w in cited_only})
    client = LLMClient(use_cache=not args.no_cache)

    rows, cost = [], 0.0
    for wiki, fact in tqdm(chosen, desc="checking citations", unit="fact"):
        source = sources.get((wiki.conv_id, fact.session_id))
        if source is None:
            continue
        result = check_citation(client, args.model, fact, source)
        cost += result["cost_usd"]
        rows.append({
            "conv_id": wiki.conv_id, "fact_id": fact.fact_id,
            "page_title": fact.page_title, "page_type": fact.page_type,
            "session_index": fact.session_index, "date_label": fact.date_label,
            "cited_dia_ids": " ".join(fact.source_dia_ids),
            "fact": fact.text, "verdict": result["verdict"],
            "misattribution": int(result["misattribution"]), "reason": result["reason"],
            "human_verdict": "", "human_misattribution": "",
        })

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    csv_path = Path(args.out) if args.out else config.RESULTS_DIR / f"wiki_citations_{stamp}.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CITATION_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    json_path = csv_path.with_suffix(".json")
    json_path.write_text(json.dumps({
        "audit": {"mode": "citations", "checked_utc": stamp, "checker_model": args.model,
                  "compiler_model": cited_only[0].extraction_model,
                  "compiler_version": cited_only[0].compiler_version,
                  "build_fingerprint": cited_only[0].build_fingerprint,
                  "wiki_dir": str(wiki_dir), "n_sampled": len(rows), "seed": args.seed,
                  "wikis": [w.conv_id for w in cited_only], "cost_usd": cost},
        "rows": rows,
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    sheet_path = _write_review_sheet(csv_path, rows, sources, mode="citations")

    print(f"\n{len(rows)} citations checked by {args.model} for ${cost:.4f}")
    _report(rows, verdicts=CITATION_VERDICTS)
    print(f"\ncsv   : {csv_path}")
    print(f"json  : {json_path}")
    print(f"sheet : {sheet_path}")
    print("\nHand-verify 30 rows: read the sheet, fill in human_verdict "
          "(supported/partially/unsupported) and human_misattribution (1/0) in the "
          f"CSV, then run:\n  python scripts/audit_wiki.py score {csv_path}")
    return 0


def _write_review_sheet(csv_path: Path, rows: list[dict], sources: dict,
                        mode: str = "fidelity") -> Path:
    """Fact plus its source session, in reading order, for hand-verification.

    The model's verdict is deliberately NOT printed: an anchored hand label is
    not independent evidence, and the whole point of the subset is to be
    independent of the model being validated.
    """
    path = csv_path.with_name(csv_path.stem + "_sheet.md")
    if mode == "citations":
        lines = [f"# Citation audit - hand verification sheet ({csv_path.name})", "",
                 "For each entry, decide whether **the turns it cites** support it:",
                 "**supported** (the cited turns say it), **partially** (the cited turns",
                 "say part, and the rest is on another turn of the session that is not",
                 "cited) or **unsupported** (the cited turns do not say it at all).",
                 "Also flag whether it is pinned on the wrong person. Write your labels",
                 "into the `human_verdict` and `human_misattribution` columns of the CSV,",
                 "matching on `fact_id`. The model's verdict is not shown here on",
                 "purpose - an anchored label is not independent evidence.", ""]
    else:
        lines = [f"# Wiki fidelity - hand verification sheet ({csv_path.name})", "",
                 "For each entry, decide from the session below it whether the fact is",
                 "**supported**, **distorted** (related but altered) or **unsupported**,",
                 "and whether it is pinned on the wrong person. Write your labels into the",
                 "`human_verdict` and `human_misattribution` columns of the CSV, matching",
                 "on `fact_id`. The model's verdict is not shown here on purpose.", ""]
    for number, row in enumerate(rows, start=1):
        source = sources.get((row["conv_id"], f"session_{row['session_index']}"), {})
        lines += [
            "---", "",
            f"## {number}. {row['conv_id']} · {row['fact_id']} · page "
            f"*{row['page_title']}* ({row['page_type']})", "",
            f"**Compiled fact:** {row['fact']} — {row['date_label']}", "",
        ]
        if mode == "citations":
            turns = source.get("turns", {})
            cited = (row.get("cited_dia_ids") or "").split()
            lines += [f"**It cites:** {', '.join(cited) or '(none)'}", "", "```"]
            lines += [f"[{d}] {turns.get(d, '(turn not found)')}" for d in cited]
            lines += ["```", "",
                      f"**Whole session {row['session_index']} "
                      f"({source.get('timestamp', '?')}), for context:**", "",
                      "```", source.get("transcript_ids", "(session not found)"), "```", ""]
        else:
            lines += [
                f"**Source, session {row['session_index']} "
                f"({source.get('timestamp', '?')}):**", "",
                "```", source.get("transcript", "(session not found)"), "```", ""]
        lines += ["`human_verdict:` ____________   `human_misattribution:` ____", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# --- reporting --------------------------------------------------------------
def _rates(rows: list[dict], key: str, verdicts=VERDICTS) -> dict:
    counts = Counter(r[key] for r in rows)
    n = len(rows) or 1
    return {v: counts.get(v, 0) / n for v in verdicts} | {
        "unreadable": counts.get("unreadable", 0) / n, "n": len(rows)}


def _report(rows: list[dict], key: str = "verdict", mis_key: str = "misattribution",
            verdicts=VERDICTS) -> None:
    if not rows:
        print("nothing to report")
        return
    overall = _rates(rows, key, verdicts)
    misattributed = sum(1 for r in rows if str(r[mis_key]) in ("1", "True", "true"))

    middle = verdicts[1]        # "distorted" for fidelity, "partially" for citations
    print(f"\n{'scope':<16}{'n':>5}{'supported':>12}{middle:>12}"
          f"{'unsupported':>13}{'misattrib':>11}")
    print("-" * 69)

    def line(scope: str, subset: list[dict]) -> None:
        rates = _rates(subset, key, verdicts)
        mis = sum(1 for r in subset if str(r[mis_key]) in ("1", "True", "true"))
        print(f"{scope:<16}{rates['n']:>5}{rates['supported']:>12.3f}"
              f"{rates[middle]:>12.3f}{rates['unsupported']:>13.3f}"
              f"{mis / (len(subset) or 1):>11.3f}")

    line("overall", rows)
    for page_type in sorted({r["page_type"] for r in rows}):
        line(page_type, [r for r in rows if r["page_type"] == page_type])
    print("-" * 69)

    error_rate = overall[middle] + overall["unsupported"]
    print(f"fact error rate ({middle} + unsupported): {error_rate:.1%}")
    print(f"misattribution: {misattributed}/{len(rows)} facts "
          f"({misattributed / len(rows):.1%}) name the wrong person")
    if overall["unreadable"]:
        print(f"WARNING: {overall['unreadable']:.1%} of checks returned an "
              f"unreadable verdict and are counted in n but in no class")

    worst = [r for r in rows if r[key] in (middle, "unsupported")]
    if worst:
        print(f"\n--- {min(len(worst), 10)} of {len(worst)} failures ---")
        for row in worst[:10]:
            print(f"[{row[key]}{' MISATTRIBUTED' if str(row[mis_key]) in ('1', 'True', 'true') else ''}] "
                  f"{row['conv_id']} {row['fact_id']}")
            print(f"  fact  : {row['fact']}")
            print(f"  reason: {row['reason']}")


def cmd_score(args) -> int:
    rows = list(csv.DictReader(Path(args.audit).open(encoding="utf-8")))
    if not rows:
        print("empty audit file")
        return 1

    # Which audit this is, taken from the file rather than a flag: the citation
    # audit is the one that records which turns each fact cited.
    verdicts = CITATION_VERDICTS if "cited_dia_ids" in rows[0] else VERDICTS
    print(f"=== automated pass: {args.audit} ({'citations' if verdicts is CITATION_VERDICTS else 'fidelity'}) ===")
    _report(rows, verdicts=verdicts)

    labelled = [r for r in rows if r.get("human_verdict", "").strip().lower() in verdicts]
    if not labelled:
        print("\nNo hand labels yet. Fill in human_verdict (and human_misattribution) "
              "for a subset -- the LLM pass alone is not evidence.")
        return 0

    print(f"\n=== hand-verified subset: {len(labelled)} facts ===")
    _report(labelled, key="human_verdict", mis_key="human_misattribution",
            verdicts=verdicts)

    agree = sum(1 for r in labelled
                if r["verdict"] == r["human_verdict"].strip().lower())
    accuracy = agree / len(labelled)
    kappa = _cohens_kappa([r["verdict"] for r in labelled],
                          [r["human_verdict"].strip().lower() for r in labelled])
    print(f"\nchecker/human agreement : {accuracy:.1%} ({agree}/{len(labelled)})")
    print(f"Cohen's kappa           : {kappa:.3f}")
    # With ~97% of facts in one class, kappa is unstable and can read 0.000 at
    # high agreement, because chance agreement is nearly as high as observed
    # agreement. Say so, rather than let a low kappa be quoted as poor
    # reliability of the checker.
    share = max(Counter(r["human_verdict"].strip().lower() for r in labelled).values())
    if share / len(labelled) >= 0.85:
        print(f"  NOTE: {share}/{len(labelled)} hand labels fall in one class. Kappa is "
              f"unstable on a distribution this skewed - quote raw agreement alongside "
              f"it, and over-sample suspected failures if a stronger figure is needed.")

    disagreements = [r for r in labelled if r["verdict"] != r["human_verdict"].strip().lower()]
    if disagreements:
        print(f"\n--- {len(disagreements)} disagreements ---")
        for row in disagreements:
            print(f"{row['conv_id']} {row['fact_id']}: checker said {row['verdict']}, "
                  f"you said {row['human_verdict'].strip().lower()}")
            print(f"  fact  : {row['fact']}")
            print(f"  reason: {row['reason']}")
    return 0


def _cohens_kappa(a: list[str], b: list[str]) -> float:
    """Multi-class Cohen's kappa: agreement corrected for chance."""
    n = len(a)
    if not n:
        return 0.0
    observed = sum(1 for x, y in zip(a, b) if x == y) / n
    count_a, count_b = Counter(a), Counter(b)
    expected = sum((count_a[label] / n) * (count_b[label] / n)
                   for label in set(count_a) | set(count_b))
    return (observed - expected) / (1 - expected) if expected < 1 else 1.0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--wiki-dir", default=str(config.WIKI_DIR))
    sub = parser.add_subparsers(dest="command", required=True)

    p_stats = sub.add_parser("stats", help="wiki size, compression and extraction problems")
    p_stats.add_argument("-o", "--out", default=None, help="also write a CSV here")
    p_stats.set_defaults(func=cmd_stats)

    p_check = sub.add_parser("check", help="LLM fidelity pass over a stratified sample")
    p_check.add_argument("-n", type=int, default=config.WIKI_AUDIT_SAMPLE)
    p_check.add_argument("--seed", type=int, default=0)
    p_check.add_argument("--model", default=config.WIKI_AUDIT_MODEL)
    p_check.add_argument("-o", "--out", default=None)
    p_check.add_argument("--no-cache", action="store_true")
    p_check.set_defaults(func=cmd_check)

    p_cites = sub.add_parser(
        "citations",
        help="System C: is each fact faithful to the TURN it cites? (defaults to "
             "the System C wikis in results/wikis_c)")
    p_cites.add_argument("-n", type=int, default=config.WIKI_AUDIT_SAMPLE)
    p_cites.add_argument("--seed", type=int, default=0)
    p_cites.add_argument("--model", default=config.WIKI_AUDIT_MODEL)
    p_cites.add_argument("-o", "--out", default=None)
    p_cites.add_argument("--no-cache", action="store_true")
    p_cites.set_defaults(func=cmd_citations)

    p_score = sub.add_parser("score", help="report rates, and agreement on hand labels")
    p_score.add_argument("audit", help="a results/wiki_fidelity_*.csv")
    p_score.set_defaults(func=cmd_score)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

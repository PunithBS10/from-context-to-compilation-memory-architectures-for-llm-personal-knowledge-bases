"""Provenance-driven selective forgetting: the pass that turns C's wiki into D's.

This is the thesis contribution. Everything before it was substrate: System B
compiled the conversation into facts, System C attached to each fact the turns
it came from. System D uses those turns to decide what the memory should stop
believing.

Two operations, run before retrieval, producing a pruned wiki:

(a) **Verification pruning.** A fact whose own cited turns do not support it
    is untrustworthy by definition, and is dropped. Facts whose cited turns
    support *part* of them are kept: System C's audit found that shape to be
    benign - a fact merging a detail from an adjacent, uncited turn - and
    dropping correct information is not forgetting, it is damage.

(b) **Contradiction resolution.** When two facts on a page conflict, each is
    given a trust score computed from PROVENANCE SIGNALS ONLY, and the lower
    one is dropped. This is the Oliver case (a dog in session 7, a cat in
    session 13) and the reason System D exists.

The trust score is deterministic and every signal is logged, so the rule can be
stated in the thesis and reproduced by an examiner, and every decision the
pass makes can be read back and argued with:

    verified       the cited turns state the fact       (from the LLM check)
    first-hand     the fact's subject is the speaker    (deterministic)
                   of the turn it cites
    corroborated   the fact cites more than one turn    (deterministic)
    recency        later source wins                    TIEBREAK ONLY

Recency is deliberately the weakest signal and never dominates, because
recency-based forgetting is what the existing literature already does. If
provenance-driven forgetting behaved like recency it would not be a
contribution, so the same pass is run under a second policy - **recency
alone** - and the two are compared. Where they choose differently, that
difference *is* the contribution, measured.

The two LLM passes (verification, detection) are shared between the policies
and written to disk once. Only resolution differs, which is exactly the
variable the ablation must isolate.

Scope limit, declared: contradictions are detected WITHIN a page. Pages group
facts by subject, so genuine conflicts almost always co-occur there, and
pairwise comparison across ~3,000 facts would be unaffordable. Cross-page
contradictions are not found.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from tqdm import tqdm

import config
from src.llm import LLMClient, count_tokens
from src.systems.wiki import Fact, Wiki, parse_json

FORGETTING_VERSION = "d1"
POLICIES = ("provenance", "recency")
KINDS = ("incompatible", "update")

# --- the support rubric, shared with audit_wiki.py --------------------------
# One definition, imported by the audit, so the pass that prunes and the audit
# that measures agree on what "supported" means by construction rather than by
# two people remembering to keep two strings the same.
SUPPORT_RUBRIC = """\
- "supported"   - the cited turns state this, allowing for rewording and summary
- "partially"   - the cited turns state part of it, but some of the entry rests
                  on a different turn of the session that the entry does not cite
- "unsupported" - the cited turns do not state this at all: it is somewhere else
                  in the session entirely, it is altered (wrong person, wrong
                  date, wrong detail), or nothing in the session says it"""

SUPPORT_VERDICTS = ("supported", "partially", "unsupported")

VERIFY_SYSTEM = (
    "You check whether compiled facts are supported by the specific conversation "
    "turns they cite. You are strict about WHICH turn says what. Reply with JSON only."
)

VERIFY_TEMPLATE = """Below is one session of a conversation between {speakers}, which took place on {date}.

{transcript}

A knowledge base compiled from this session contains these entries. Each cites the turns it came from:

{entries}

For EACH entry, decide whether the CITED TURNS support it:

""" + SUPPORT_RUBRIC + """

Reply with JSON only, one verdict per entry, in this form:
{{"verdicts": [{{"id": "<entry id>", "verdict": "supported|partially|unsupported", "reason": "<one short sentence>"}}]}}"""

DETECT_SYSTEM = (
    "You find entries in a knowledge base that cannot both be true. You are "
    "conservative: two entries that describe different events, or that add detail "
    "to each other, are NOT a contradiction. Reply with JSON only."
)

DETECT_TEMPLATE = """These are the entries on the page "{title}" ({page_type}) of a knowledge base compiled from a long conversation between {speakers}. Each entry is dated to the session it was written from.

{entries}

Find pairs of entries that conflict. Two kinds:

- "incompatible" - they make conflicting claims about the same thing, so at least
                   one is wrong: a pet that is a dog in one and a cat in the other,
                   two different ages, two different birthplaces, two different
                   names for the same person's partner.
- "update"       - both could have been true on their dates, but the later one
                   supersedes the earlier: was looking for a job / started a job;
                   lived in one city / moved to another; was engaged / got married.

Do NOT report: two different events of the same kind on different dates (two
parades, two hikes); one entry adding detail to another; entries that are merely
about the same topic. Report nothing if nothing conflicts.

Reply with JSON only:
{{"pairs": [{{"a": "<entry id>", "b": "<entry id>", "kind": "incompatible|update", "reason": "<one short sentence>"}}]}}"""


# --- records ----------------------------------------------------------------
@dataclass
class Verdict:
    fact_id: str
    verdict: str          # supported | partially | unsupported | unreadable
    reason: str


@dataclass
class Contradiction:
    page_slug: str
    fact_a: str
    fact_b: str
    kind: str             # incompatible | update
    reason: str


@dataclass
class Decision:
    """One resolved contradiction, with everything that went into it.

    Thesis evidence: the only way to show the policy is explainable rather
    than a black box is to be able to print, for every fact the pass dropped,
    exactly which signals fired and by how much the other side won.
    """
    policy: str
    page_slug: str
    kind: str
    reason: str
    fact_a: str
    fact_b: str
    text_a: str
    text_b: str
    signals_a: dict
    signals_b: dict
    trust_a: float
    trust_b: float
    winner: str
    loser: str
    decided_by: str       # "trust" | "recency" | "moot"


# --- trust ------------------------------------------------------------------
# Weights. Verification carries the most because it is the only signal that
# says whether the fact is TRUE of its source; first-hand and corroboration say
# how much to believe a source that does support it. Recency is not in the sum
# at all - it breaks ties and does nothing else.
W_VERIFIED = 2.0
W_FIRST_HAND = 1.0
W_CORROBORATED = 0.5      # per extra cited turn, capped at two extras

VERIFIED_VALUE = {"supported": 1.0, "partially": 0.5, "unsupported": 0.0, "unreadable": 0.5}


def subject_of(fact: Fact, speakers: list[str]) -> str | None:
    """Which speaker a fact is about, from its first words.

    The compiler is told to name the subject first ("Melanie ran a charity
    race", never "She ran"), and System B's inspection found zero pronoun-led
    facts, so the opening name is a reliable subject. A fact opening with
    neither speaker ("The necklace Caroline received...") has no first-hand
    signal either way.
    """
    head = fact.text.strip().lower()
    for name in speakers:
        n = name.lower()
        if head.startswith(n + " ") or head.startswith(n + "'"):
            return name
    return None


def trust_signals(fact: Fact, verdict: str, turns_by_id: dict, speakers: list[str]) -> dict:
    """Every provenance signal for one fact, as data, before any weighting."""
    subject = subject_of(fact, speakers)
    cited_speakers = sorted({turns_by_id[d].speaker for d in fact.source_dia_ids
                             if d in turns_by_id})
    first_hand = bool(subject) and bool(cited_speakers) and all(
        s == subject for s in cited_speakers)
    return {
        "verified": verdict,
        "subject": subject,
        "cited_turns": list(fact.source_dia_ids),
        "cited_speakers": cited_speakers,
        "first_hand": first_hand,
        "n_cited_turns": len(fact.source_dia_ids),
        "session_index": fact.session_index,
        "date_label": fact.date_label,
    }


def trust_score(signals: dict) -> float:
    verified = W_VERIFIED * VERIFIED_VALUE.get(signals["verified"], 0.5)
    first_hand = W_FIRST_HAND * (1.0 if signals["first_hand"] else 0.0)
    extra = max(0, min(signals["n_cited_turns"] - 1, 2))
    corroborated = W_CORROBORATED * extra
    return round(verified + first_hand + corroborated, 3)


def _later(a: Fact, b: Fact) -> Fact:
    """The more recent of two facts: later session, then later turn."""
    if a.session_index != b.session_index:
        return a if a.session_index > b.session_index else b
    return a if _turn_order(a) >= _turn_order(b) else b


def _turn_order(fact: Fact) -> int:
    ids = [int(d.split(":")[1]) for d in fact.source_dia_ids if ":" in d]
    return max(ids) if ids else 0


def resolve(a: Fact, b: Fact, sig_a: dict, sig_b: dict, policy: str) -> tuple[Fact, str]:
    """Which fact survives, and what decided it."""
    if policy == "recency":
        return _later(a, b), "recency"
    ta, tb = trust_score(sig_a), trust_score(sig_b)
    if ta != tb:
        return (a if ta > tb else b), "trust"
    return _later(a, b), "recency"


# --- the LLM passes ---------------------------------------------------------
def verify_wiki(client: LLMClient, wiki: Wiki, conversation, model: str,
                verbose: bool = False) -> tuple[list[Verdict], dict]:
    """Check every fact against the turns it cites. One call per session.

    Batched by session so the checker sees exactly the transcript the compiler
    saw when it wrote those facts, and sees every fact it wrote from it at
    once. ~11 facts and ~800 transcript tokens per call.
    """
    facts_by_session: dict[int, list[Fact]] = {}
    for fact in wiki.facts:
        facts_by_session.setdefault(fact.session_index, []).append(fact)
    sessions = {s.index: s for s in conversation.sessions}
    speakers = " and ".join(conversation.speakers) if conversation.speakers else "two people"

    verdicts: list[Verdict] = []
    calls = cached = unreadable = 0
    prompt_tokens = completion_tokens = 0
    cost = latency = 0.0

    for index in tqdm(sorted(facts_by_session), desc=f"{wiki.conv_id} verify",
                      unit="session", leave=False):
        session = sessions.get(index)
        facts = facts_by_session[index]
        if session is None:
            verdicts += [Verdict(f.fact_id, "unreadable", "session not in data") for f in facts]
            continue
        transcript = "\n".join(
            f"[{t.dia_id}] {t.render()}" if t.dia_id else t.render() for t in session.turns)
        entries = "\n".join(
            f'{f.fact_id}: "{f.text}"   (cites {", ".join(f.source_dia_ids) or "nothing"})'
            for f in facts)
        response = client.complete(
            model=model, system_prompt=VERIFY_SYSTEM,
            user_prompt=VERIFY_TEMPLATE.format(
                speakers=speakers, date=session.timestamp or "an unrecorded date",
                transcript=transcript, entries=entries),
            max_tokens=config.MAX_FORGET_TOKENS)
        calls += 1
        cached += bool(response.cached)
        prompt_tokens += response.prompt_tokens
        completion_tokens += response.completion_tokens
        cost += response.cost_usd
        latency += response.latency_s

        parsed = parse_json(response.text)
        got = {}
        if isinstance(parsed, dict) and isinstance(parsed.get("verdicts"), list):
            for item in parsed["verdicts"]:
                if isinstance(item, dict) and item.get("verdict") in SUPPORT_VERDICTS:
                    got[str(item.get("id", "")).strip()] = item
        for fact in facts:
            item = got.get(fact.fact_id)
            if item is None:
                # A missing or unreadable verdict is counted, never scored as
                # supported: that would flatter exactly the thing being measured.
                unreadable += 1
                verdicts.append(Verdict(fact.fact_id, "unreadable", "no verdict returned"))
            else:
                verdicts.append(Verdict(fact.fact_id, item["verdict"],
                                        str(item.get("reason", ""))[:300]))
        if verbose:
            print(f"  [forget] {wiki.conv_id} session {index:>2}: {len(facts)} facts checked")

    stats = {"verify_calls": calls, "verify_cached_calls": cached,
             "verify_prompt_tokens": prompt_tokens, "verify_completion_tokens": completion_tokens,
             "verify_cost_usd": cost, "verify_latency_s": latency,
             "verify_unreadable": unreadable}
    return verdicts, stats


def detect_contradictions(client: LLMClient, wiki: Wiki, conversation, model: str,
                          verbose: bool = False) -> tuple[list[Contradiction], dict]:
    """Find conflicting pairs, one call per page. Pages with one fact are skipped."""
    speakers = " and ".join(conversation.speakers) if conversation.speakers else "two people"
    found: list[Contradiction] = []
    calls = cached = pages_checked = unreadable = 0
    prompt_tokens = completion_tokens = 0
    cost = latency = 0.0

    for page in tqdm(wiki.ordered_pages(), desc=f"{wiki.conv_id} detect",
                     unit="page", leave=False):
        if len(page.facts) < 2:
            continue
        pages_checked += 1
        ids = {f.fact_id for f in page.facts}
        entries = "\n".join(
            f"{f.fact_id}: \"{f.text}\" — {f.date_label} (cites {', '.join(f.source_dia_ids) or 'nothing'})"
            for f in page.facts)
        response = client.complete(
            model=model, system_prompt=DETECT_SYSTEM,
            user_prompt=DETECT_TEMPLATE.format(
                title=page.title, page_type=page.type, speakers=speakers, entries=entries),
            max_tokens=config.MAX_FORGET_TOKENS)
        calls += 1
        cached += bool(response.cached)
        prompt_tokens += response.prompt_tokens
        completion_tokens += response.completion_tokens
        cost += response.cost_usd
        latency += response.latency_s

        parsed = parse_json(response.text)
        if not isinstance(parsed, dict) or not isinstance(parsed.get("pairs"), list):
            unreadable += 1
            continue
        seen: set[tuple[str, str]] = set()
        for item in parsed["pairs"]:
            if not isinstance(item, dict):
                continue
            a, b = str(item.get("a", "")).strip(), str(item.get("b", "")).strip()
            kind = str(item.get("kind", "")).strip().lower()
            # Only pairs of real ids on this page, each pair once, never a
            # fact against itself. Anything else is the model misreading the
            # list, and is dropped rather than resolved.
            if a not in ids or b not in ids or a == b or kind not in KINDS:
                continue
            key = tuple(sorted((a, b)))
            if key in seen:
                continue
            seen.add(key)
            found.append(Contradiction(page.slug, key[0], key[1], kind,
                                       str(item.get("reason", ""))[:300]))
        if verbose and seen:
            print(f"  [forget] {wiki.conv_id} page {page.title!r}: {len(seen)} conflicting pairs")

    stats = {"detect_calls": calls, "detect_cached_calls": cached,
             "detect_pages_checked": pages_checked,
             "detect_prompt_tokens": prompt_tokens, "detect_completion_tokens": completion_tokens,
             "detect_cost_usd": cost, "detect_latency_s": latency,
             "detect_unreadable_pages": unreadable}
    return found, stats


# --- the pass ---------------------------------------------------------------
def forget(wiki: Wiki, conversation, verdicts: list[Verdict],
           contradictions: list[Contradiction], policy: str
           ) -> tuple[Wiki, list[Decision], dict]:
    """Apply one policy: prune unsupported facts, resolve every contradiction.

    Pure and deterministic given its inputs - no model is called here, so the
    two policies can be run over identical verdicts and contradictions and the
    only thing that differs is the rule.
    """
    if policy not in POLICIES:
        raise ValueError(f"policy must be one of {POLICIES}, got {policy!r}")

    verdict_of = {v.fact_id: v.verdict for v in verdicts}
    facts_by_id = {f.fact_id: f for f in wiki.facts}
    turns_by_id = {t.dia_id: t for t in conversation.turns if t.dia_id}
    speakers = list(conversation.speakers)

    # (a) verification pruning
    dropped: dict[str, str] = {}      # fact_id -> why
    for fact in wiki.facts:
        if verdict_of.get(fact.fact_id) == "unsupported":
            dropped[fact.fact_id] = "unsupported"

    # (b) contradiction resolution, in a fixed order so the log is reproducible
    decisions: list[Decision] = []
    for c in sorted(contradictions, key=lambda c: (c.page_slug, c.fact_a, c.fact_b)):
        a, b = facts_by_id.get(c.fact_a), facts_by_id.get(c.fact_b)
        if a is None or b is None:
            continue
        sig_a = trust_signals(a, verdict_of.get(a.fact_id, "unreadable"), turns_by_id, speakers)
        sig_b = trust_signals(b, verdict_of.get(b.fact_id, "unreadable"), turns_by_id, speakers)
        ta, tb = trust_score(sig_a), trust_score(sig_b)
        if a.fact_id in dropped or b.fact_id in dropped:
            # One side already gone (unsupported, or lost an earlier pair):
            # nothing to decide, but it is logged so the count of detected
            # contradictions reconciles with the count of decisions.
            gone = a.fact_id if a.fact_id in dropped else b.fact_id
            kept = b.fact_id if gone == a.fact_id else a.fact_id
            decisions.append(Decision(policy, c.page_slug, c.kind, c.reason,
                                      a.fact_id, b.fact_id, a.text, b.text, sig_a, sig_b,
                                      ta, tb, kept, gone, "moot"))
            continue
        winner, decided_by = resolve(a, b, sig_a, sig_b, policy)
        loser = b if winner is a else a
        dropped[loser.fact_id] = f"lost to {winner.fact_id} ({decided_by})"
        decisions.append(Decision(policy, c.page_slug, c.kind, c.reason,
                                  a.fact_id, b.fact_id, a.text, b.text, sig_a, sig_b,
                                  ta, tb, winner.fact_id, loser.fact_id, decided_by))

    # build the pruned wiki; ids are kept stable so the log can be read against
    # either wiki
    pruned = Wiki(conv_id=wiki.conv_id, speakers=list(wiki.speakers),
                  extraction_model=wiki.extraction_model,
                  build_fingerprint=wiki.build_fingerprint,
                  compiler_version=f"{wiki.compiler_version}+{FORGETTING_VERSION}-{policy}",
                  built_utc=datetime.now(timezone.utc).isoformat(),
                  sessions=list(wiki.sessions))
    for page in wiki.ordered_pages():
        kept = [f for f in page.facts if f.fact_id not in dropped]
        if not kept:
            continue
        for f in kept:
            new = pruned.add_fact(title=page.title, page_type=page.type, text=f.text,
                                  date_label=f.date_label, session_index=f.session_index,
                                  session_id=f.session_id, source_dia_ids=f.source_dia_ids)
            if new is not None:
                new.fact_id = f.fact_id      # stable handle, not renumbered

    n_before, n_after = len(wiki.facts), len(pruned.facts)
    verdict_counts = {v: sum(1 for x in verdicts if x.verdict == v)
                      for v in SUPPORT_VERDICTS + ("unreadable",)}
    resolved = [d for d in decisions if d.decided_by != "moot"]
    report = {
        "policy": policy,
        "forgetting_version": FORGETTING_VERSION,
        "facts_before": n_before,
        "facts_after": n_after,
        "facts_removed": n_before - n_after,
        "facts_removed_pct": round((n_before - n_after) / n_before, 4) if n_before else None,
        "verdicts": verdict_counts,
        "unsupported_dropped": sum(1 for why in dropped.values() if why == "unsupported"),
        "contradictions_detected": len(contradictions),
        "contradictions_by_kind": {k: sum(1 for c in contradictions if c.kind == k) for k in KINDS},
        "contradictions_resolved": len(resolved),
        "contradictions_moot": len(decisions) - len(resolved),
        "decided_by_trust": sum(1 for d in resolved if d.decided_by == "trust"),
        "decided_by_recency": sum(1 for d in resolved if d.decided_by == "recency"),
        "pages_before": len(wiki.pages),
        "pages_after": len(pruned.pages),
        "wiki_tokens_before": wiki.stats.get("wiki_tokens") or count_tokens(wiki.render(), config.ANSWER_MODEL),
        "wiki_tokens_after": count_tokens(pruned.render(), config.ANSWER_MODEL),
    }
    pruned.stats = {**wiki.stats, "forgetting": report,
                    "n_pages": len(pruned.pages), "n_facts": n_after,
                    "wiki_tokens": report["wiki_tokens_after"]}
    return pruned, decisions, report


def disagreement(prov: list[Decision], rec: list[Decision]) -> dict:
    """Where the two policies chose differently on the same contradictions.

    The headline ablation number. If it is zero, provenance-driven forgetting
    behaved exactly like recency and the contribution collapses into prior
    work; if not, the pairs listed here are the contribution, one by one.
    """
    by_pair = lambda ds: {(d.page_slug, d.fact_a, d.fact_b): d
                          for d in ds if d.decided_by != "moot"}
    p, r = by_pair(prov), by_pair(rec)
    shared = sorted(set(p) & set(r))
    differ = [(p[k], r[k]) for k in shared if p[k].winner != r[k].winner]
    by_kind = {}
    for kind in KINDS:
        same_kind = [k for k in shared if p[k].kind == kind]
        d = sum(1 for k in same_kind if p[k].winner != r[k].winner)
        by_kind[kind] = {"pairs": len(same_kind), "disagree": d,
                         "rate": round(d / len(same_kind), 4) if same_kind else None}
    return {
        "pairs_compared": len(shared),
        "disagreements": len(differ),
        "disagreement_rate": round(len(differ) / len(shared), 4) if shared else None,
        "by_kind": by_kind,
        "pairs": [{"page": a.page_slug, "kind": a.kind,
                   "provenance_kept": a.winner, "recency_kept": b.winner,
                   "trust": {a.fact_a: a.trust_a, a.fact_b: a.trust_b},
                   "text": {a.fact_a: a.text_a, a.fact_b: a.text_b},
                   "decided_by": a.decided_by, "reason": a.reason}
                  for a, b in differ],
    }


# --- disk -------------------------------------------------------------------
def passes_path(conv_id: str) -> Path:
    return config.FORGETTING_DIR / f"{conv_id}.json"


def load_passes(conv_id: str, wiki: Wiki, model: str
                ) -> tuple[list[Verdict], list[Contradiction], dict] | None:
    """The shared LLM passes for one wiki, if they were run over THIS wiki."""
    path = passes_path(conv_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if (data.get("wiki_fingerprint") != wiki.build_fingerprint
            or data.get("n_facts") != len(wiki.facts)
            or data.get("model") != model
            or data.get("forgetting_version") != FORGETTING_VERSION):
        return None
    return ([Verdict(**v) for v in data["verdicts"]],
            [Contradiction(**c) for c in data["contradictions"]],
            data.get("stats", {}))


def run_passes(client: LLMClient, wiki: Wiki, conversation, model: str,
               reuse: bool = True, verbose: bool = False
               ) -> tuple[list[Verdict], list[Contradiction], dict]:
    """Verification and detection for one wiki, shared by both policies and
    written to disk once as evidence."""
    if reuse:
        existing = load_passes(wiki.conv_id, wiki, model)
        if existing is not None:
            return existing
    verdicts, vstats = verify_wiki(client, wiki, conversation, model, verbose)
    contradictions, dstats = detect_contradictions(client, wiki, conversation, model, verbose)
    stats = {**vstats, **dstats,
             "passes_cost_usd": vstats["verify_cost_usd"] + dstats["detect_cost_usd"]}
    config.FORGETTING_DIR.mkdir(parents=True, exist_ok=True)
    passes_path(wiki.conv_id).write_text(json.dumps({
        "conv_id": wiki.conv_id, "model": model,
        "forgetting_version": FORGETTING_VERSION,
        "wiki_fingerprint": wiki.build_fingerprint, "n_facts": len(wiki.facts),
        "run_utc": datetime.now(timezone.utc).isoformat(),
        "stats": stats,
        "verdicts": [asdict(v) for v in verdicts],
        "contradictions": [asdict(c) for c in contradictions],
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    return verdicts, contradictions, stats


def write_decisions(directory: Path, decisions: list[Decision], report: dict) -> Path:
    path = Path(directory) / "decisions.json"
    path.write_text(json.dumps({"report": report, "decisions": [asdict(d) for d in decisions]},
                               indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def load_or_forget(client: LLMClient, conversation, policy: str, source_dir: Path,
                   target_dir: Path, model: str, reuse: bool = True,
                   verbose: bool = False) -> tuple[Wiki, list[Decision], dict]:
    """The pruned wiki for one conversation under one policy.

    Reuses a pruned wiki on disk only when it was derived from the SAME source
    wiki by the SAME pass version and policy; anything else is rebuilt. The
    source wiki is read and never written: C's wikis underpin its published
    results and its citation audit.
    """
    source = Wiki.load(source_dir, conversation.conv_id)
    if source is None:
        raise FileNotFoundError(
            f"no System C wiki for {conversation.conv_id} in {source_dir}; build it with\n"
            f"  python scripts/compile_wikis.py --system c")
    expected_version = f"{source.compiler_version}+{FORGETTING_VERSION}-{policy}"

    if reuse:
        existing = Wiki.load(target_dir, conversation.conv_id)
        decisions_file = Path(target_dir) / conversation.conv_id / "decisions.json"
        if (existing is not None and existing.compiler_version == expected_version
                and existing.build_fingerprint == source.build_fingerprint
                and decisions_file.exists()):
            data = json.loads(decisions_file.read_text(encoding="utf-8"))
            return (existing, [Decision(**d) for d in data["decisions"]], data["report"])

    verdicts, contradictions, pass_stats = run_passes(
        client, source, conversation, model, reuse=reuse, verbose=verbose)
    pruned, decisions, report = forget(source, conversation, verdicts, contradictions, policy)
    report["passes"] = pass_stats
    pruned.stats["forgetting"] = report
    directory = pruned.write(target_dir)
    write_decisions(directory, decisions, report)
    return pruned, decisions, report

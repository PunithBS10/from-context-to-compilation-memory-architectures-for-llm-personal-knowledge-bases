"""Compile the wikis, and report what the citations look like — before any run.

Compilation is the expensive, irreversible half of Systems B, C and D: whatever
the compiler writes down *becomes* the memory, and no amount of retrieval at
answer time can recover a fact it never wrote. So it is built and inspected on
its own, apart from any benchmark, for two reasons the System B build proved
the hard way:

1. Three compiler defects in System B — a person-page collapse, category names
   used as page titles, and a malformed reply that silently erased a whole
   session — were all found by reading the compiled output, and none of them
   would have shown up as anything but "this system scores badly".
2. System C's citation validation is **deterministic and free**. Every id the
   extractor cites is checked against the ids the session actually contains,
   so whether the compiler cites well is known before a cent is spent
   answering questions with it.

    python scripts/compile_wikis.py --system c            # all 10, into results/wikis_c
    python scripts/compile_wikis.py --system c --limit 1  # prove it on one first
    python scripts/compile_wikis.py --system c --rebuild  # ignore what is on disk
    python scripts/compile_wikis.py --system c --print conv-26   # read one by eye
    python scripts/compile_wikis.py --system c --model luna     # Luna's own wikis

System B's wikis are published and committed. `--system b` is available so the
script is honest about which compiler it is running, but it reuses what is on
disk and will not recompile over it unless `--rebuild` is passed explicitly.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import config
from src.datasets.locomo import load_locomo
from src.llm import LLMClient
from src.systems.wiki import B_PROFILE, C_PROFILE, Wiki, load_or_compile

PROFILES = {"b": B_PROFILE, "c": C_PROFILE}


def _default_dir(system: str) -> Path:
    """Read at call time, not import time: `--model` moves these."""
    return config.WIKI_DIR if system == "b" else config.WIKI_C_DIR


def cmd_compile(args) -> int:
    profile, default_dir = PROFILES[args.system], _default_dir(args.system)
    wiki_dir = Path(args.out) if args.out else default_dir
    conversations = load_locomo(config.LOCOMO_PATH, limit=args.limit)
    client = LLMClient(use_cache=not args.no_cache)

    effort = f" (reasoning {config.REASONING_EFFORT})" if config.REASONING_EFFORT else ""
    print(f"compiler {profile.name} (version {profile.version}, fingerprint "
          f"{profile.fingerprint}), model {config.WIKI_EXTRACTION_MODEL}{effort}")
    print(f"writing to {wiki_dir}\n")

    wikis: list[Wiki] = []
    _print_header()
    for conversation in conversations:
        wiki = load_or_compile(client, conversation, wiki_dir, profile=profile,
                               model=config.WIKI_EXTRACTION_MODEL,
                               reuse=not args.rebuild, verbose=args.verbose)
        if not wiki.from_disk:
            wiki.write(wiki_dir)
        wikis.append(wiki)
        _print_row(wiki)

    _print_totals(wikis)
    return 0


ROW = ("{conv:<10}{pages:>6}{facts:>7}{sess:>6}{wiki_tok:>10}{compr:>8}{cost:>9}"
       "{cites:>8}{unres:>7}{nocite:>8}{flag:>7}")


def _print_header() -> None:
    print(ROW.format(conv="conv", pages="pages", facts="facts", sess="sess",
                     wiki_tok="wiki tok", compr="compr", cost="$",
                     cites="cites", unres="unres", nocite="no cite", flag=""))
    print("-" * 92)


def _print_row(wiki: Wiki) -> None:
    stats = wiki.stats
    print(ROW.format(
        conv=wiki.conv_id,
        pages=stats.get("n_pages", 0),
        facts=stats.get("n_facts", 0),
        sess=stats.get("sessions_compiled", 0),
        wiki_tok=f"{stats.get('wiki_tokens', 0):,}",
        compr=f"{stats.get('compression_ratio') or 0:.3f}",
        cost=f"{stats.get('compile_cost_usd', 0.0):.4f}",
        cites=stats.get("citations_total", 0),
        unres=stats.get("citations_unresolvable", 0),
        nocite=stats.get("facts_with_no_citation", 0),
        flag=" [disk]" if wiki.from_disk else "",
    ))


def _print_totals(wikis: list[Wiki]) -> None:
    """The corpus figures, and the three citation numbers the spec asks for.

    Reported as counts as well as rates: a rate of 0.998 hides how many facts
    it is, and the failures are what a compiler is judged on.
    """
    def total(key: str) -> int:
        return sum(w.stats.get(key, 0) or 0 for w in wikis)

    pages, facts = total("n_pages"), total("n_facts")
    wiki_tokens, raw_tokens = total("wiki_tokens"), total("raw_conversation_tokens")
    cost = sum(w.stats.get("compile_cost_usd", 0.0) for w in wikis)
    latency = sum(w.stats.get("compile_latency_s", 0.0) for w in wikis)
    reused = sum(1 for w in wikis if w.from_disk)

    print("-" * 92)
    compression = f" ({wiki_tokens / raw_tokens:.1%})" if raw_tokens else ""
    print(f"{len(wikis)} wikis | {pages} pages | {facts} facts | "
          f"{wiki_tokens:,} compiled tokens from {raw_tokens:,} raw{compression}")
    print(f"compile cost ${cost:.4f} over {total('compile_calls')} calls, {latency:.0f}s"
          + (f"  [{reused}/{len(wikis)} reused from disk; cost is the original build's]"
             if reused else ""))
    print(f"extraction problems: {total('salvaged_sessions')} sessions salvaged, "
          f"{total('unparsable_sessions')} unreadable, {total('empty_sessions')} empty, "
          f"{total('truncated_sessions')} truncated, "
          f"{total('generic_page_titles')} generic page titles repaired")

    cites = total("citations_total")
    if not cites:
        print("\nno citations in these wikis (System B's compiler was never asked for them)")
        return

    unresolvable = total("citations_unresolvable")
    other_session = total("citations_other_session")
    nonexistent = total("citations_nonexistent")
    no_citation = total("facts_with_no_citation")

    print("\n--- citation validation (deterministic, free) ---")
    print(f"citations_total         {cites:>7}   ids the extractor emitted, "
          f"{cites / facts:.2f} per fact")
    print(f"citations_unresolvable  {unresolvable:>7}   "
          f"({unresolvable / cites:.2%})  dropped from the fact, fact kept")
    print(f"  - other session       {other_session:>7}   "
          f"({other_session / cites:.2%})  a real turn, but not one it was shown")
    print(f"  - names no turn       {nonexistent:>7}   "
          f"({nonexistent / cites:.2%})  invented outright")
    print(f"facts_with_no_citation  {no_citation:>7}   "
          f"({no_citation / facts:.2%})  of {facts} facts, kept without provenance")
    print(f"\nresolvability        {(cites - nonexistent) / cites:.4f}   "
          f"the id names a turn that exists")
    print(f"session consistency  {(cites - unresolvable) / cites:.4f}   "
          f"the id names a turn in the session the fact came from")


def cmd_print(args) -> int:
    """One wiki, whole, to be read end to end by eye before any run."""
    wiki_dir = Path(args.out) if args.out else _default_dir(args.system)
    wiki = Wiki.load(wiki_dir, args.conv_id)
    if wiki is None:
        raise SystemExit(f"no wiki for {args.conv_id} in {wiki_dir}")

    stats = wiki.stats
    print(f"# {wiki.conv_id} — {stats.get('n_pages')} pages, {stats.get('n_facts')} facts, "
          f"{stats.get('sessions_compiled')} sessions")
    print(f"# compiler {wiki.compiler_version or '?'} ({wiki.build_fingerprint}), "
          f"model {wiki.extraction_model}, built {wiki.built_utc}")
    print(f"# citations {stats.get('citations_total', 0)}, "
          f"unresolvable {stats.get('citations_unresolvable', 0)}, "
          f"facts with no citation {stats.get('facts_with_no_citation', 0)}")
    print(f"# speakers: {', '.join(wiki.speakers)}\n")
    for page in wiki.ordered_pages():
        print(f"===== {page.path} =====")
        print(page.render())
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--system", default="c", choices=sorted(PROFILES),
                        help="which compiler to run (default: c)")
    parser.add_argument("--out", default=None,
                        help="write to this directory instead of the system's default")
    parser.add_argument("--model", default=None, choices=sorted(config.MODEL_VARIANTS),
                        help="compile with this answer-model variant; its wikis go in "
                             "that variant's own directory (results/wikis_<tag>), never "
                             "over the published gpt-4o-mini ones")
    parser.add_argument("--limit", type=int, default=None,
                        help="only the first N conversations")
    parser.add_argument("--rebuild", action="store_true",
                        help="recompile even when a matching wiki is already on disk")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--print", dest="conv_id", default=None,
                        help="print one compiled wiki end to end and exit")
    args = parser.parse_args(argv)
    if args.model is not None:
        config.select_model(args.model)

    if args.conv_id:
        return cmd_print(args)
    return cmd_compile(args)


if __name__ == "__main__":
    sys.exit(main())

"""Put compiled wikis side by side: what did each compiler actually write?

    python scripts/compare_wikis.py results/wikis results/wikis_luna
    python scripts/compare_wikis.py results/wikis_c results/wikis_c_luna -o results/wiki_compare.csv

A benchmark score says how a memory answered; it does not say what the memory
holds. When the compiler changes -- System C's citation prompt, or a new
answering model -- the wiki changes with it, and any accuracy difference is
uninterpretable until the two wikis are described by the same numbers. Every
figure here is read from the manifests the compiler wrote (`wiki.json`), so
this costs nothing and never touches the API.

What is compared, and why each matters:

* **pages, facts, facts per page** -- the shape of the memory. The same
  conversation compiled into 300 facts or 500 is two different memories.
* **person-page share** -- the fraction of FACTS filed on a person page. The
  prompt asks that person pages not become diaries; the share measures how
  far the compiler obeyed. System C's compiler drifted from 15% to 27% and it
  cost a worked example, so it is tracked per compiler.
* **generic titles** -- facts the compiler filed under "Event" or "Misc"
  instead of naming a page: instruction-following, counted.
* **salvaged / unparsable / empty / truncated sessions** -- holes in the
  memory. A salvaged session lost the one object that broke the JSON; an
  unparsable one is gone entirely.
* **citation resolvability and session consistency** -- System C only:
  whether the turn ids the compiler cited exist, and exist in the session it
  was shown. Deterministic, and the first thing that tells a compiler apart.
* **chunks** -- what retrieval indexes. Pages over the chunk budget split, so
  this is the number that actually faces the question embedding.
"""
from __future__ import annotations

import argparse
import csv
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import config
from src.llm import count_tokens
from src.systems.wiki import GENERIC_TITLES, Wiki, build_wiki_chunks, slugify


def load_wikis(wiki_dir: Path) -> list[Wiki]:
    wikis = []
    for manifest in sorted(wiki_dir.glob("*/wiki.json")):
        wiki = Wiki.load(wiki_dir, manifest.parent.name)
        if wiki is not None:
            wikis.append(wiki)
    if not wikis:
        raise SystemExit(f"no wikis under {wiki_dir}")
    return wikis


def describe(wiki_dir: Path) -> dict:
    """One column of the comparison: the corpus figures for one directory."""
    wikis = load_wikis(wiki_dir)

    def total(key: str) -> int:
        return sum(w.stats.get(key, 0) or 0 for w in wikis)

    facts = [f for w in wikis for f in w.facts]
    pages = [p for w in wikis for p in w.pages]
    n_facts, n_pages = len(facts), len(pages)
    person_facts = sum(1 for f in facts if f.page_type == "person")
    # Pages still carrying a category name, after `repair_title` had its go.
    generic_pages = sum(1 for p in pages if slugify(p.title) in GENERIC_TITLES)
    chunks = [c for w in wikis for c in build_wiki_chunks(w)]
    chunk_tokens = [count_tokens(c.render(), config.ANSWER_MODEL) for c in chunks]

    cites = total("citations_total")
    nonexistent = total("citations_nonexistent")
    unresolvable = total("citations_unresolvable")
    raw_tokens, wiki_tokens = total("raw_conversation_tokens"), total("wiki_tokens")

    models = sorted({w.extraction_model for w in wikis})
    versions = sorted({w.compiler_version for w in wikis})
    fingerprints = sorted({w.build_fingerprint for w in wikis})
    return {
        "directory": wiki_dir.name,
        "compiler model": ", ".join(models),
        "compiler version": ", ".join(versions),
        "build fingerprint": ", ".join(fingerprints),
        "conversations": len(wikis),
        "sessions compiled": total("sessions_compiled"),
        "pages": n_pages,
        "  person pages": sum(1 for p in pages if p.type == "person"),
        "  event pages": sum(1 for p in pages if p.type == "event"),
        "  topic pages": sum(1 for p in pages if p.type == "topic"),
        "facts": n_facts,
        "facts per page": n_facts / n_pages if n_pages else 0.0,
        "facts per conversation": n_facts / len(wikis),
        "person-page share of facts": person_facts / n_facts if n_facts else 0.0,
        "generic titles (facts re-filed)": total("generic_page_titles"),
        "generic titles (pages left)": generic_pages,
        "duplicate facts dropped": total("duplicate_facts_dropped"),
        "salvaged sessions": total("salvaged_sessions"),
        "unparsable sessions": total("unparsable_sessions"),
        "empty sessions": total("empty_sessions"),
        "truncated sessions": total("truncated_sessions"),
        "wiki tokens": wiki_tokens,
        "compression (wiki/raw)": wiki_tokens / raw_tokens if raw_tokens else 0.0,
        "chunks": len(chunks),
        "mean chunk tokens": statistics.fmean(chunk_tokens) if chunk_tokens else 0.0,
        "compile calls": total("compile_calls"),
        "compile prompt tokens": total("compile_prompt_tokens"),
        "compile completion tokens": total("compile_completion_tokens"),
        "compile cost $": sum(w.stats.get("compile_cost_usd", 0.0) for w in wikis),
        "compile latency s": sum(w.stats.get("compile_latency_s", 0.0) for w in wikis),
        "citations": cites,
        "citations per fact": cites / n_facts if n_facts and cites else None,
        "citation resolvability": (cites - nonexistent) / cites if cites else None,
        "citation session consistency": (cites - unresolvable) / cites if cites else None,
        "  other-session citations": total("citations_other_session") if cites else None,
        "  nonexistent citations": nonexistent if cites else None,
        "facts with no citation": total("facts_with_no_citation") if cites else None,
    }


PERCENT = {"person-page share of facts", "compression (wiki/raw)"}
RATE = {"citation resolvability", "citation session consistency"}
MONEY = {"compile cost $"}
ONE_DP = {"facts per page", "facts per conversation", "mean chunk tokens",
          "compile latency s"}
TWO_DP = {"citations per fact"}


def fmt(key: str, value) -> str:
    if value is None:
        return "-"
    if key in PERCENT:
        return f"{value:.1%}"
    if key in RATE:
        return f"{value:.4f}"
    if key in MONEY:
        return f"{value:.4f}"
    if key in ONE_DP:
        return f"{value:,.1f}"
    if key in TWO_DP:
        return f"{value:.2f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("wiki_dirs", nargs="+", help="results/wikis*, one column each")
    parser.add_argument("-o", "--out", default=None, help="also write the table as CSV")
    args = parser.parse_args(argv)

    columns = [describe(Path(d)) for d in args.wiki_dirs]
    keys = list(columns[0])
    width = max(18, max(len(fmt(k, c[k])) for c in columns for k in keys) + 2)
    label_width = max(len(k) for k in keys) + 2

    print("=" * (label_width + width * len(columns)))
    for key in keys:
        print(f"{key:<{label_width}}" + "".join(f"{fmt(key, c[key]):>{width}}" for c in columns))
        if key in ("build fingerprint", "  topic pages", "truncated sessions",
                   "mean chunk tokens", "compile latency s"):
            print("-" * (label_width + width * len(columns)))
    print("=" * (label_width + width * len(columns)))

    if args.out:
        out = Path(args.out)
        with out.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["metric"] + [c["directory"] for c in columns])
            for key in keys:
                writer.writerow([key.strip()] + [c[key] if c[key] is not None else ""
                                                 for c in columns])
        print(f"\nwritten: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

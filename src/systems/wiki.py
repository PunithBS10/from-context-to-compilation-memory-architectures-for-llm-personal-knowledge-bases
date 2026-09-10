"""The compiled wiki: the memory substrate for System B (and later C and D).

Systems L and A search raw dialogue. System B first *compiles* the conversation
into a small knowledge base of markdown pages and searches those instead.

Format is OKF-shaped -- a directory of markdown files with YAML frontmatter --
so the substrate is a citable open format rather than something invented here:

    wiki/
      people/caroline.md
      events/charity-race.md
      topics/painting.md

    ---
    title: Charity race
    type: event
    ---

    - Melanie ran a charity race raising awareness for mental health - 25 May 2023
    - The event led Melanie to reflect on the importance of self-care - 25 May 2023

Three properties of that shape are requirements, not styling:

* **One fact per line, each carrying its date.** System C attaches a source to
  each of these lines; flowing prose would leave nowhere to hang it.
* **Every fact names its subject.** "She ran a charity race" is how
  misattribution enters a knowledge base, and LoCoMo's adversarial questions
  work by swapping the two speakers, so a pronoun in the wiki is a future wrong
  answer.
* **Pages cover events and topics, not only people.** LoCoMo has two speakers
  per conversation; per-person pages alone would be two enormous documents.

Compilation is **incremental, session by session**. Sending the whole
conversation in one call would be System L again, and the output would be
unmanageable. Each call sees one session (~800 tokens) plus the titles of the
pages that already exist, which is also how a personal knowledge base is really
built -- as the conversations happen.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from tqdm import tqdm

import config
from src.llm import LLMClient, count_tokens

# --- page types -------------------------------------------------------------
# Directory per type, matching the OKF example layout. Anything the model
# invents outside this map is filed as a topic rather than dropped: a fact in
# the wrong folder is recoverable, a lost fact is not.
TYPE_DIRS = {"person": "people", "event": "events", "topic": "topics"}
_TYPE_ALIASES = {
    "person": "person", "people": "person", "speaker": "person",
    "event": "event", "events": "event", "activity": "event",
    "topic": "topic", "topics": "topic", "subject": "topic",
    "place": "topic", "location": "topic", "thing": "topic", "object": "topic",
}

FACT_SEPARATOR = " — "     # em dash between the fact and its date

# Titles that are a category rather than a subject. The compiler falls back to
# these when it cannot be bothered to name a page, which produces a grab-bag
# page whose header tells retrieval nothing. The prompt forbids them; this
# counts how often that instruction is ignored, so the failure is visible in
# the manifest instead of hiding inside a page called "Event".
GENERIC_TITLES = {"person", "people", "event", "events", "topic", "topics",
                  "misc", "miscellaneous", "general", "other", "notes", "facts",
                  "untitled", "unknown"}


# --- prompts ----------------------------------------------------------------
EXTRACTION_SYSTEM = (
    "You compile a personal knowledge base from a long-running conversation. "
    "You are given one session at a time and you write down the facts it "
    "states, filing each on a page. You never add anything the session does "
    "not say. Reply with JSON only."
)

# --- System B's compiler, frozen --------------------------------------------
# System B is complete, published and committed, and the wikis in
# results/wikis/ are the artefact both its results and its fidelity audit rest
# on. Its prompt and version are therefore pinned here verbatim rather than
# left in git history. System C changes the compiler; if B's own prompt were
# the thing that changed, B's wikis would look stale to B's reuse check and the
# next `--system b` run would silently recompile over the published artefact.
B_COMPILER_VERSION = "b2"

B_EXTRACTION_TEMPLATE = """Conversation between {speakers}.
This is session {session_number}, which took place on {date}.

Transcript of this session:
{transcript}

Pages that already exist in the knowledge base:
{page_list}

Write down the facts this session states.

Rules:
1. Only facts stated in THIS session. Never infer, embellish, or add outside
   knowledge. If the session does not say it, it does not go in.
2. Name the subject explicitly in every fact: write "Melanie ran a charity
   race", never "She ran a charity race". Take care to attribute each fact to
   the right person - the two speakers must never be mixed up.
3. One fact per entry, short and self-contained. Prefer several small facts to
   one long compound sentence.
4. Keep any time reference the speaker gave inside the fact text ("last year",
   "in 2019", "two weeks ago", "when she was a child"). Do NOT write the
   session date into the fact - it is attached automatically.
5. File each fact on a page chosen by SUBJECT MATTER, not by who said it:
   - page_type "person": who someone IS - their job, family, home, health,
     identity, where they live. One page per person, titled with their name.
   - page_type "event": something that happened at a particular time - a race,
     a trip, a performance, a hospital visit, a party.
   - page_type "topic": an interest, project or recurring thread - painting,
     adoption research, marathon training, a job hunt.
   Most facts belong on an event or topic page. A person page must NOT become a
   diary of everything someone did: if a fact is about an activity, it goes on
   that activity's page even though a person is its subject. Reuse an existing
   page title EXACTLY where one fits; otherwise name a new page. Keep titles
   short and general enough to be reused later ("Painting", not "Melanie's
   painting of a sunrise").
   The page value is a TITLE - "Painting", "Charity race", "Melanie" - never
   the words "Person", "Event", "Topic", "Misc" or "General". A fact that fits
   no existing page gets a specific new title of its own.
6. Skip greetings, pleasantries and pure reactions ("that's great!").

Reply with JSON only, in this form:
{{"facts": [
  {{"fact": "Melanie ran a charity race raising awareness for mental health", "page": "Charity race", "page_type": "event"}},
  {{"fact": "Melanie works night shifts at the hospital", "page": "Melanie", "page_type": "person"}},
  {{"fact": "Caroline painted a sunrise over the harbour", "page": "Painting", "page_type": "topic"}}
]}}"""


# --- System C's compiler ----------------------------------------------------
# Identical to B's in shape - one call per session, incremental, page
# assignment by subject matter - with one addition: the transcript is rendered
# with each turn's id, and every fact must name the turn or turns it came from.
# That citation is the whole of System C. A model cannot cite an id it was
# never shown, so the two changes are a pair and neither works alone.
COMPILER_VERSION = "c1"

EXTRACTION_TEMPLATE = """Conversation between {speakers}.
This is session {session_number}, which took place on {date}.

Transcript of this session. Every turn is prefixed with its id in square
brackets, like [D7:11]:
{transcript}

Pages that already exist in the knowledge base:
{page_list}

Write down the facts this session states.

Rules:
1. Only facts stated in THIS session. Never infer, embellish, or add outside
   knowledge. If the session does not say it, it does not go in.
2. Name the subject explicitly in every fact: write "Melanie ran a charity
   race", never "She ran a charity race". Take care to attribute each fact to
   the right person - the two speakers must never be mixed up.
3. One fact per entry, short and self-contained. Prefer several small facts to
   one long compound sentence.
4. Keep any time reference the speaker gave inside the fact text ("last year",
   "in 2019", "two weeks ago", "when she was a child"). Do NOT write the
   session date into the fact - it is attached automatically.
5. File each fact on a page chosen by SUBJECT MATTER, not by who said it:
   - page_type "person": who someone IS - their job, family, home, health,
     identity, where they live. One page per person, titled with their name.
   - page_type "event": something that happened at a particular time - a race,
     a trip, a performance, a hospital visit, a party.
   - page_type "topic": an interest, project or recurring thread - painting,
     adoption research, marathon training, a job hunt.
   Most facts belong on an event or topic page. A person page must NOT become a
   diary of everything someone did: if a fact is about an activity, it goes on
   that activity's page even though a person is its subject. Reuse an existing
   page title EXACTLY where one fits; otherwise name a new page. Keep titles
   short and general enough to be reused later ("Painting", not "Melanie's
   painting of a sunrise").
   The page value is a TITLE - "Painting", "Charity race", "Melanie" - never
   the words "Person", "Event", "Topic", "Misc" or "General". A fact that fits
   no existing page gets a specific new title of its own.
6. Skip greetings, pleasantries and pure reactions ("that's great!").
7. Every fact must list the turn ids it came from, exactly as shown in square
   brackets in the transcript. Most facts come from one turn. List more than
   one only when the fact genuinely needs them (a question in one turn and its
   answer in the next).
8. Cite only ids from THIS session's transcript. Never invent an id, never
   cite a turn you were not shown.

Reply with JSON only, in this form:
{{"facts": [
  {{"fact": "Melanie ran a charity race raising awareness for mental health", "page": "Charity race", "page_type": "event", "sources": ["D7:4"]}},
  {{"fact": "Melanie works night shifts at the hospital", "page": "Melanie", "page_type": "person", "sources": ["D7:6"]}},
  {{"fact": "Caroline painted a sunrise over the harbour", "page": "Painting", "page_type": "topic", "sources": ["D7:9", "D7:10"]}}
]}}"""


@dataclass(frozen=True)
class CompilerProfile:
    """One compiler's behaviour, and the fingerprint that identifies it.

    A wiki on disk is reused only when the profile that built it matches the
    one in force now, so each system carries its own profile rather than
    sharing a single module-level constant that a later system can move. B's
    profile reproduces its published fingerprint exactly (9ad622199c7e); if
    that ever stops being true, B's committed wikis will be treated as stale,
    which is precisely what must not happen.
    """
    name: str
    system_prompt: str
    template: str
    version: str
    show_dia_ids: bool

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(
            (self.system_prompt + self.template + self.version).encode("utf-8")
        ).hexdigest()[:12]


B_PROFILE = CompilerProfile("B", EXTRACTION_SYSTEM, B_EXTRACTION_TEMPLATE,
                            B_COMPILER_VERSION, show_dia_ids=False)
C_PROFILE = CompilerProfile("C", EXTRACTION_SYSTEM, EXTRACTION_TEMPLATE,
                            COMPILER_VERSION, show_dia_ids=True)

# The build in force now. Recorded in every results file and in every wiki
# manifest, so a results file always says which compiler produced the memory
# it was measuring.
BUILD_FINGERPRINT = C_PROFILE.fingerprint


# --- data model -------------------------------------------------------------
@dataclass
class Fact:
    """One line of one page: a single dated statement."""
    text: str
    date_label: str          # date of the session that produced it
    session_index: int
    session_id: str
    page_title: str
    page_type: str
    fact_id: str = ""        # "charity-race#1" - stable handle for the audit
    # System C: the raw turns this fact was extracted from, validated at
    # compile time against the ids that session actually contains. Empty for
    # System B, whose compiler was never asked for them - which is also why
    # render() appends a tag only when there is one, so B's wikis still render
    # exactly as they did when its results were published.
    source_dia_ids: list[str] = field(default_factory=list)

    def render(self) -> str:
        line = f"- {self.text}{FACT_SEPARATOR}{self.date_label}"
        if self.source_dia_ids:
            line += " [" + ", ".join(self.source_dia_ids) + "]"
        return line

    def as_dict(self) -> dict:
        return {"fact_id": self.fact_id, "text": self.text,
                "date_label": self.date_label, "session_index": self.session_index,
                "session_id": self.session_id, "page_title": self.page_title,
                "page_type": self.page_type,
                "source_dia_ids": list(self.source_dia_ids)}


@dataclass
class WikiPage:
    title: str
    type: str
    slug: str
    facts: list[Fact] = field(default_factory=list)

    @property
    def path(self) -> str:
        return f"{TYPE_DIRS.get(self.type, 'topics')}/{self.slug}.md"

    @property
    def first_session(self) -> int:
        return min((f.session_index for f in self.facts), default=0)

    @property
    def sources(self) -> list[str]:
        """The sessions this page draws on, in conversation order.

        Page-level provenance, and the natural OKF home for it: the frontmatter
        is where a reader - or a later system - looks to see where a page came
        from without reading every line of it.
        """
        return [f"session_{i}" for i in sorted({f.session_index for f in self.facts})]

    def render(self) -> str:
        """The markdown file: YAML frontmatter, then one fact per line."""
        front = (f"---\ntitle: {_yaml_scalar(self.title)}\ntype: {self.type}\n"
                 f"sources: [{', '.join(self.sources)}]\n---\n\n")
        return front + "\n".join(f.render() for f in self.facts) + "\n"


@dataclass
class Wiki:
    conv_id: str
    speakers: list[str] = field(default_factory=list)
    pages: list[WikiPage] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    extraction_model: str = config.WIKI_EXTRACTION_MODEL
    build_fingerprint: str = BUILD_FINGERPRINT
    compiler_version: str = COMPILER_VERSION
    built_utc: str = ""
    sessions: list[dict] = field(default_factory=list)   # index/id/date_label
    from_disk: bool = False

    def __post_init__(self):
        self._by_slug = {p.slug: p for p in self.pages}

    # -- building ------------------------------------------------------
    def add_fact(self, title: str, page_type: str, text: str, date_label: str,
                 session_index: int, session_id: str,
                 source_dia_ids: list[str] | None = None) -> Fact | None:
        """File one fact, creating its page if needed.

        Pages are matched on the SLUG of the title alone, not on (title, type):
        if the compiler files "Charity race" as an event in one session and as
        a topic in another, those are the same page and must not split into
        two. The first type seen wins.
        """
        slug = slugify(title)
        page = self._by_slug.get(slug)
        if page is None:
            page = WikiPage(title=title.strip(), type=normalise_type(page_type), slug=slug)
            self._by_slug[slug] = page
            self.pages.append(page)

        # Exact repeats of a line already on the page are noise, not memory.
        # A repeat's citations are dropped with it rather than merged into the
        # fact already on the page: that fact is dated to the session it was
        # first compiled from, and hanging a later session's turn on it would
        # make its citations cross-session - which System C measures as a
        # compiler defect. A duplicate is dropped whole or not at all.
        if any(f.text.strip().lower() == text.strip().lower() for f in page.facts):
            return None

        fact = Fact(text=text.strip(), date_label=date_label, session_index=session_index,
                    session_id=session_id, page_title=page.title, page_type=page.type,
                    fact_id=f"{slug}#{len(page.facts) + 1}",
                    source_dia_ids=list(source_dia_ids or []))
        page.facts.append(fact)
        return fact

    # -- views ---------------------------------------------------------
    @property
    def facts(self) -> list[Fact]:
        return [f for p in self.ordered_pages() for f in p.facts]

    def ordered_pages(self) -> list[WikiPage]:
        """Pages in the order the conversation first raised them.

        This is what gives the wiki a timeline: retrieved chunks are re-ordered
        by corpus index, exactly as in System A, so that index has to mean
        something chronological.
        """
        return sorted(self.pages, key=lambda p: (p.first_session, p.slug))

    def page_list_for_prompt(self) -> str:
        if not self.pages:
            return "(none yet - this is the first session)"
        return "\n".join(f"- {p.title} ({p.type})" for p in self.ordered_pages())

    def render(self) -> str:
        """The whole wiki as one string, for token counting and eyeballing."""
        return "\n\n".join(f"## {p.path}\n{p.render()}" for p in self.ordered_pages())

    # -- disk ----------------------------------------------------------
    def write(self, root: Path) -> Path:
        """Write the markdown pages plus a manifest.

        The markdown is the artefact a supervisor can read; the manifest keeps
        the per-fact metadata (which session each fact came from) that the
        fidelity audit and evidence recall need and that markdown alone would
        lose.
        """
        directory = Path(root) / self.conv_id
        if directory.exists():
            for stale in directory.rglob("*.md"):
                stale.unlink()          # a rebuild must not leave old pages behind
        for page in self.ordered_pages():
            path = directory / page.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(page.render(), encoding="utf-8")

        manifest = {
            "conv_id": self.conv_id,
            "speakers": self.speakers,
            "extraction_model": self.extraction_model,
            "build_fingerprint": self.build_fingerprint,
            "compiler_version": self.compiler_version,
            "built_utc": self.built_utc or datetime.now(timezone.utc).isoformat(),
            "sessions": self.sessions,
            "stats": self.stats,
            "pages": [
                {"title": p.title, "type": p.type, "slug": p.slug, "path": p.path,
                 "facts": [f.as_dict() for f in p.facts]}
                for p in self.ordered_pages()
            ],
        }
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "wiki.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        return directory

    @classmethod
    def load(cls, root: Path, conv_id: str) -> "Wiki | None":
        path = Path(root) / conv_id / "wiki.json"
        if not path.exists():
            return None
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None        # a corrupt manifest is a rebuild, not a crash
        pages = [
            WikiPage(
                title=p["title"], type=p["type"], slug=p["slug"],
                facts=[Fact(text=f["text"], date_label=f["date_label"],
                            session_index=f["session_index"], session_id=f["session_id"],
                            page_title=f["page_title"], page_type=f["page_type"],
                            fact_id=f["fact_id"],
                            # Absent from every System B manifest, which is
                            # exactly right: B's facts have no citations.
                            source_dia_ids=list(f.get("source_dia_ids") or []))
                       for f in p["facts"]],
            )
            for p in manifest["pages"]
        ]
        return cls(
            conv_id=manifest["conv_id"], speakers=manifest.get("speakers", []),
            pages=pages, stats=manifest.get("stats", {}),
            extraction_model=manifest.get("extraction_model", ""),
            build_fingerprint=manifest.get("build_fingerprint", ""),
            compiler_version=manifest.get("compiler_version", ""),
            built_utc=manifest.get("built_utc", ""),
            sessions=manifest.get("sessions", []), from_disk=True,
        )


# --- compiler ---------------------------------------------------------------
class WikiCompiler:
    """Turns a conversation into a Wiki, one session per LLM call."""

    def __init__(self, client: LLMClient, model: str = config.WIKI_EXTRACTION_MODEL,
                 verbose: bool = False, profile: CompilerProfile = C_PROFILE):
        self.client = client
        self.model = model
        self.verbose = verbose
        # Which compiler this is. System B passes B_PROFILE, which reproduces
        # the prompt and fingerprint its published wikis were built with, so a
        # change made for C can never make B's own artefact look stale.
        self.profile = profile

    def compile(self, conversation) -> Wiki:
        wiki = Wiki(conv_id=conversation.conv_id, speakers=list(conversation.speakers),
                    extraction_model=self.model,
                    build_fingerprint=self.profile.fingerprint,
                    compiler_version=self.profile.version,
                    built_utc=datetime.now(timezone.utc).isoformat())
        speakers = (" and ".join(conversation.speakers)
                    if conversation.speakers else "two people")

        prompt_tokens = completion_tokens = calls = cached = truncated = 0
        duplicates = generic_titles = salvaged = unparsable = empty = 0
        cost = latency = 0.0

        # System C's citation validation, all of it deterministic and free.
        citations_total = citations_unresolvable = 0
        citations_other_session = citations_nonexistent = facts_with_no_citation = 0
        # Every turn id anywhere in this conversation. An id the extractor
        # cites that lives here but not in the session it was shown is a
        # different, and milder, defect from one that names no turn at all, and
        # the two are counted apart.
        conversation_dia_ids = {t.dia_id for t in conversation.turns if t.dia_id}

        sessions = [s for s in conversation.sessions if s.turns]
        for session in tqdm(sessions, desc=f"{conversation.conv_id} wiki",
                            unit="session", leave=False):
            date_label = date_of(session.timestamp)
            session_dia_ids = [t.dia_id for t in session.turns if t.dia_id]
            wiki.sessions.append({"index": session.index, "session_id": session.session_id,
                                  "timestamp": session.timestamp, "date_label": date_label})

            user_prompt = self.profile.template.format(
                speakers=speakers,
                session_number=session.index,
                date=session.timestamp or date_label,
                transcript=render_transcript(session.turns, self.profile.show_dia_ids),
                page_list=wiki.page_list_for_prompt(),
            )
            response = self.client.complete(
                model=self.model,
                system_prompt=self.profile.system_prompt,
                user_prompt=user_prompt,
                max_tokens=config.MAX_EXTRACTION_TOKENS,
            )
            calls += 1
            cached += bool(response.cached)
            prompt_tokens += response.prompt_tokens
            completion_tokens += response.completion_tokens
            cost += response.cost_usd
            latency += response.latency_s

            # A truncated reply silently loses the tail of a session's facts.
            # Count it and say so rather than let it disappear into the wiki.
            if response.meta.get("finish_reason") == "length":
                truncated += 1
                print(f"  [wiki] {conversation.conv_id} session {session.index}: "
                      f"extraction hit the {config.MAX_EXTRACTION_TOKENS}-token "
                      f"ceiling; facts may be missing")

            items, status = parse_facts(response.text)
            if status == "salvaged":
                salvaged += 1
                print(f"  [wiki] {conversation.conv_id} session {session.index}: "
                      f"malformed JSON; {len(items)} facts salvaged")
            elif status == "unparsable":
                unparsable += 1
                print(f"  [wiki] {conversation.conv_id} session {session.index}: "
                      f"extraction reply could not be read AT ALL - this session "
                      f"contributed nothing to the wiki")
            elif not items:
                empty += 1
                print(f"  [wiki] {conversation.conv_id} session {session.index}: "
                      f"no facts extracted")

            for item in items:
                generic_titles += slugify(item["page"]) in GENERIC_TITLES
                title = repair_title(item["fact"], item["page"], conversation.speakers)
                # Unresolvable ids are dropped and the FACT IS KEPT. A fact
                # with no usable citation is still a fact; dropping it would
                # silently shrink C's wiki relative to B's and turn a citation
                # problem into an incomparable memory.
                resolvable, foreign, invented = validate_citations(
                    item["sources"], session_dia_ids, conversation_dia_ids)
                added = wiki.add_fact(
                    title=title, page_type=item["page_type"], text=item["fact"],
                    date_label=date_label, session_index=session.index,
                    session_id=session.session_id, source_dia_ids=resolvable,
                )
                if added is None:
                    duplicates += 1
                    continue      # a dropped duplicate contributes no citations
                citations_total += len(resolvable) + len(foreign) + len(invented)
                citations_unresolvable += len(foreign) + len(invented)
                citations_other_session += len(foreign)
                citations_nonexistent += len(invented)
                facts_with_no_citation += not resolvable

            if self.verbose:
                print(f"  [wiki] {conversation.conv_id} session {session.index:>2}: "
                      f"{len(wiki.facts):>4} facts on {len(wiki.pages):>3} pages")

        raw_tokens = count_tokens(
            "\n".join(t.render() for t in conversation.turns), config.ANSWER_MODEL)
        wiki_tokens = count_tokens(wiki.render(), config.ANSWER_MODEL)
        wiki.stats = {
            "compile_prompt_tokens": prompt_tokens,
            "compile_completion_tokens": completion_tokens,
            "compile_cost_usd": cost,
            "compile_latency_s": latency,
            "compile_calls": calls,
            "compile_cached_calls": cached,
            "sessions_compiled": calls,
            "truncated_sessions": truncated,
            "duplicate_facts_dropped": duplicates,
            # Facts the compiler filed under a category name; `repair_title`
            # re-files them, but the count stays as evidence of how often the
            # extraction prompt was ignored.
            "generic_page_titles": generic_titles,
            # Sessions whose extraction reply was malformed (facts recovered
            # object by object), unreadable (session lost), or simply empty.
            # These are holes in the memory and are reported, never hidden.
            "salvaged_sessions": salvaged,
            "unparsable_sessions": unparsable,
            "empty_sessions": empty,
            # --- citation validation (System C) -------------------------
            # The first result System C produces, and it costs nothing. Two
            # levels, both exact:
            #   resolvability       - the id names a turn that exists at all
            #   session consistency - it names a turn in the session the fact
            #                         was compiled from, which is the only
            #                         thing the extractor was shown
            # Anything else is a compiler defect, visible here before a cent is
            # spent on a run.
            "citations_total": citations_total,
            "citations_resolvable": citations_total - citations_unresolvable,
            "citations_unresolvable": citations_unresolvable,
            "citations_other_session": citations_other_session,
            "citations_nonexistent": citations_nonexistent,
            "facts_with_no_citation": facts_with_no_citation,
            "citation_resolvability": (
                round((citations_total - citations_nonexistent) / citations_total, 4)
                if citations_total else None),
            "citation_session_consistency": (
                round((citations_total - citations_unresolvable) / citations_total, 4)
                if citations_total else None),
            "n_pages": len(wiki.pages),
            "n_facts": len(wiki.facts),
            "wiki_tokens": wiki_tokens,
            "raw_conversation_tokens": raw_tokens,
            "compression_ratio": round(wiki_tokens / raw_tokens, 4) if raw_tokens else None,
        }
        return wiki


def load_or_compile(client: LLMClient, conversation, wiki_dir,
                    profile: CompilerProfile = C_PROFILE,
                    model: str = config.WIKI_EXTRACTION_MODEL,
                    reuse: bool = True, verbose: bool = False) -> Wiki:
    """Reuse the wiki on disk when it was built the same way, else compile it.

    Same principle as the response cache: a wiki built by a different model,
    extraction prompt or compiler version is a different experiment, so it is
    rebuilt rather than silently reused. A reused wiki reports the cost of the
    ORIGINAL compilation, recorded in its manifest and flagged `from_disk`, so
    nothing is reported as free that was not free.

    Shared by System B, System C and the build script for the same reason
    retrieval is shared: three copies of this rule would drift, and the one
    that drifted would be the one that quietly recompiled over a published
    wiki.
    """
    if reuse:
        existing = Wiki.load(wiki_dir, conversation.conv_id)
        if existing is not None:
            fresh_enough = (
                existing.extraction_model == model
                and existing.build_fingerprint == profile.fingerprint
                and existing.stats.get("sessions_compiled") == len(
                    [s for s in conversation.sessions if s.turns])
            )
            if fresh_enough:
                if verbose:
                    print(f"  [wiki] {conversation.conv_id}: reusing "
                          f"{existing.stats.get('n_facts')} facts on "
                          f"{existing.stats.get('n_pages')} pages from disk")
                return existing
            print(f"  [wiki] {conversation.conv_id}: on-disk wiki was built with a "
                  f"different model or prompt - recompiling")

    compiler = WikiCompiler(client, model=model, verbose=verbose, profile=profile)
    return compiler.compile(conversation)


def parse_facts(text: str) -> tuple[list[dict], str]:
    """Pull the fact list out of the model's reply.

    Returns the facts and how they were obtained: "json" (the reply parsed),
    "salvaged" (it did not, and the facts were recovered object by object) or
    "unparsable" (nothing could be read).

    The salvage path is not defensive padding. `gpt-4o-mini` closed one
    session's reply with a stray brace, strict parsing returned nothing, and
    that session vanished from the wiki -- including the two turns two QA items
    depend on. A whole session silently disappearing from memory over one
    character is a far worse failure than a slightly ugly parser, and the
    caller counts both outcomes so neither can happen quietly again.
    """
    payload = parse_json(text)
    status = "json"
    if payload is None:
        items, status = _salvage_objects(text), "salvaged"
    else:
        items = payload.get("facts") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            items, status = _salvage_objects(text), "salvaged"

    facts = []
    for item in items:
        if not isinstance(item, dict):
            continue
        fact = str(item.get("fact") or item.get("text") or "").strip()
        page = str(item.get("page") or item.get("page_title") or item.get("title") or "").strip()
        page_type = str(item.get("page_type") or item.get("type") or "topic").strip()
        if not fact:
            continue
        facts.append({"fact": fact, "page": page or "Miscellaneous",
                      "page_type": page_type, "sources": _claimed_sources(item)})

    if not facts and status == "salvaged":
        status = "unparsable"
    return facts, status


def _salvage_objects(text: str) -> list[dict]:
    """Every balanced {...} in the text that parses on its own and looks like a
    fact.

    Brace matching, aware of strings and escapes. The broken object is the only
    thing lost; its siblings survive, because they are read individually rather
    than as part of the reply that failed to parse.
    """
    objects: list[dict] = []
    stack: list[int] = []
    in_string = escape = False
    for position, char in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            stack.append(position)
        elif char == "}" and stack:
            opened = stack.pop()
            try:
                parsed = json.loads(text[opened:position + 1])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and ("fact" in parsed or "text" in parsed):
                objects.append(parsed)
    return objects


def parse_json(text: str):
    """Strict read of the reply, tolerating a fenced code block and prose
    around the JSON."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    return None


def _claimed_sources(item: dict) -> list[str]:
    """The turn ids one extracted fact claims, however the model shaped them.

    Asked for a list, `gpt-4o-mini` mostly returns one, sometimes a bare
    string, occasionally under a different key. Nothing is judged here - this
    only gets the claim out of the reply. Whether the ids are real is decided
    against the session's own ids in `validate_citations`.
    """
    raw = item.get("sources") or item.get("source") or item.get("dia_ids") or []
    if isinstance(raw, str):
        raw = re.split(r"[,;\s]+", raw)
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(x).strip() for x in raw if str(x).strip()]


def render_transcript(turns: list, show_dia_ids: bool) -> str:
    """The session exactly as the extractor sees it.

    System C prefixes every turn with its id, because a model cannot cite an id
    it was never shown - the prefix and the `sources` field are a pair and
    neither works alone. System B's transcript is left as it was.
    """
    if not show_dia_ids:
        return "\n".join(t.render() for t in turns)
    return "\n".join(
        f"[{t.dia_id}] {t.render()}" if t.dia_id else t.render() for t in turns)


def validate_citations(claimed: list[str], session_dia_ids: list[str],
                       conversation_dia_ids: set[str]
                       ) -> tuple[list[str], list[str], list[str]]:
    """Split the ids one fact claims into resolvable, other-session, invented.

    Forgiving about FORM, strict about EXISTENCE: surrounding brackets, stray
    whitespace and case are normalised away, because "d7:11" is a formatting
    variant of a real citation rather than a wrong one, while an id naming no
    turn is never rescued. Resolvable ids come back in the session's own
    spelling, so what lands in the wiki is exactly the form LoCoMo's `evidence`
    field is later compared against.
    """
    canonical = {str(c).strip().lower(): c for c in session_dia_ids}
    elsewhere = {str(c).strip().lower() for c in conversation_dia_ids}

    resolvable: list[str] = []
    foreign: list[str] = []
    invented: list[str] = []
    seen: set[str] = set()
    for raw in claimed:
        key = str(raw).strip().strip("[]()").strip().lower()
        if not key or key in seen:
            continue                    # a repeated id is one citation, not two
        seen.add(key)
        if key in canonical:
            resolvable.append(canonical[key])
        elif key in elsewhere:
            foreign.append(key)
        else:
            invented.append(key)
    return resolvable, foreign, invented


# --- chunking ---------------------------------------------------------------
@dataclass
class WikiChunk:
    """What retrieval actually indexes: a page, or a slice of a long one.

    Every chunk carries its page title and type in the rendered text, so a
    retrieved fragment still says what it is about even when the page it came
    from is not retrieved whole.
    """
    index: int
    page_title: str
    page_type: str
    facts: list[Fact]
    part: int = 1
    n_parts: int = 1

    def render(self) -> str:
        header = f"[Wiki page: {self.page_title} ({self.page_type})"
        if self.n_parts > 1:
            header += f" - part {self.part} of {self.n_parts}"
        header += "]"
        return header + "\n" + "\n".join(f.render() for f in self.facts)

    @property
    def session_indices(self) -> list[int]:
        return sorted({f.session_index for f in self.facts})

    @property
    def fact_ids(self) -> list[str]:
        return [f.fact_id for f in self.facts]


def build_wiki_chunks(wiki: Wiki, max_tokens: int = config.WIKI_CHUNK_MAX_TOKENS,
                      model: str = config.ANSWER_MODEL) -> list[WikiChunk]:
    """One chunk per page where the page fits; longer pages are split.

    Pages are far shorter than the raw conversation, so splitting is the
    exception. Chunks follow `wiki.ordered_pages()`, which puts pages in the
    order the conversation first raised them -- that ordering is what makes
    re-sorting retrieved chunks by index chronological, as in System A.
    """
    chunks: list[WikiChunk] = []
    for page in wiki.ordered_pages():
        if not page.facts:
            continue
        header_tokens = count_tokens(f"[Wiki page: {page.title} ({page.type})]", model)
        groups: list[list[Fact]] = []
        current: list[Fact] = []
        running = header_tokens
        for fact in page.facts:
            size = count_tokens(fact.render(), model)
            if current and running + size > max_tokens:
                groups.append(current)
                current, running = [], header_tokens
            current.append(fact)
            running += size
        if current:
            groups.append(current)

        for part, group in enumerate(groups, start=1):
            chunks.append(WikiChunk(index=len(chunks), page_title=page.title,
                                    page_type=page.type, facts=group,
                                    part=part, n_parts=len(groups)))
    return chunks


# --- small helpers ----------------------------------------------------------
def date_of(timestamp: str | None) -> str:
    """'1:56 pm on 8 May, 2023' -> '8 May, 2023'.

    The clock time is noise on a fact line; the date is what temporal questions
    are asked about. The date is attached by us, from the session metadata,
    rather than copied by the model, so a fact line's date cannot be
    hallucinated -- only the fact itself can.
    """
    if not timestamp:
        return "undated"
    return timestamp.split(" on ")[-1].strip() or timestamp.strip()


def repair_title(fact: str, page: str, speakers: list[str]) -> str:
    """Rescue a fact filed under a category name instead of a subject.

    `gpt-4o-mini` files roughly one fact in seven under a page literally called
    "event" or "topic", whatever the prompt says, which produces a grab-bag
    page whose header tells retrieval nothing. Facts always name their subject
    first, so a fact that opens with a speaker's name goes on that person's
    page; the rest land on "Miscellaneous", which is at least honest about what
    it is. Deterministic, and it costs no extra call.
    """
    if slugify(page) not in GENERIC_TITLES:
        return page
    stripped = fact.strip().lower()
    for name in speakers:
        if name and stripped.startswith(name.lower()):
            return name
    return "Miscellaneous"


def normalise_type(page_type: str) -> str:
    return _TYPE_ALIASES.get((page_type or "").strip().lower(), "topic")


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return slug[:60].rstrip("-") or "untitled"


def _yaml_scalar(value: str) -> str:
    """Quote a frontmatter value only when plain YAML would misread it."""
    if (not value or value != value.strip() or value[0] in "-?:,[]{}#&*!|>%@`\"'"
            or ": " in value or value.endswith(":")):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value

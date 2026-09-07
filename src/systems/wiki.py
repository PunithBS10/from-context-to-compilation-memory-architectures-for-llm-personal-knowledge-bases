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

EXTRACTION_TEMPLATE = """Conversation between {speakers}.
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

# Bump when the compiler's behaviour changes in a way the prompt text does not
# capture -- parsing, page assignment, fact repair. Wikis built by an older
# compiler are then rebuilt instead of being silently reused.
COMPILER_VERSION = "b2"

# A prompt edit or a compiler change makes every wiki built before it a
# different artefact. This fingerprint goes into each wiki's manifest and into
# the results config snapshot, and a wiki on disk is reused only when it
# matches the build in force now.
BUILD_FINGERPRINT = hashlib.sha256(
    (EXTRACTION_SYSTEM + EXTRACTION_TEMPLATE + COMPILER_VERSION).encode("utf-8")
).hexdigest()[:12]


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

    def render(self) -> str:
        return f"- {self.text}{FACT_SEPARATOR}{self.date_label}"

    def as_dict(self) -> dict:
        return {"fact_id": self.fact_id, "text": self.text,
                "date_label": self.date_label, "session_index": self.session_index,
                "session_id": self.session_id, "page_title": self.page_title,
                "page_type": self.page_type}


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

    def render(self) -> str:
        """The markdown file: YAML frontmatter, then one fact per line."""
        front = f"---\ntitle: {_yaml_scalar(self.title)}\ntype: {self.type}\n---\n\n"
        return front + "\n".join(f.render() for f in self.facts) + "\n"


@dataclass
class Wiki:
    conv_id: str
    speakers: list[str] = field(default_factory=list)
    pages: list[WikiPage] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    extraction_model: str = config.WIKI_EXTRACTION_MODEL
    build_fingerprint: str = BUILD_FINGERPRINT
    built_utc: str = ""
    sessions: list[dict] = field(default_factory=list)   # index/id/date_label
    from_disk: bool = False

    def __post_init__(self):
        self._by_slug = {p.slug: p for p in self.pages}

    # -- building ------------------------------------------------------
    def add_fact(self, title: str, page_type: str, text: str, date_label: str,
                 session_index: int, session_id: str) -> Fact | None:
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
        if any(f.text.strip().lower() == text.strip().lower() for f in page.facts):
            return None

        fact = Fact(text=text.strip(), date_label=date_label, session_index=session_index,
                    session_id=session_id, page_title=page.title, page_type=page.type,
                    fact_id=f"{slug}#{len(page.facts) + 1}")
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
                            fact_id=f["fact_id"])
                       for f in p["facts"]],
            )
            for p in manifest["pages"]
        ]
        return cls(
            conv_id=manifest["conv_id"], speakers=manifest.get("speakers", []),
            pages=pages, stats=manifest.get("stats", {}),
            extraction_model=manifest.get("extraction_model", ""),
            build_fingerprint=manifest.get("build_fingerprint", ""),
            built_utc=manifest.get("built_utc", ""),
            sessions=manifest.get("sessions", []), from_disk=True,
        )


# --- compiler ---------------------------------------------------------------
class WikiCompiler:
    """Turns a conversation into a Wiki, one session per LLM call."""

    def __init__(self, client: LLMClient, model: str = config.WIKI_EXTRACTION_MODEL,
                 verbose: bool = False):
        self.client = client
        self.model = model
        self.verbose = verbose

    def compile(self, conversation) -> Wiki:
        wiki = Wiki(conv_id=conversation.conv_id, speakers=list(conversation.speakers),
                    extraction_model=self.model,
                    built_utc=datetime.now(timezone.utc).isoformat())
        speakers = (" and ".join(conversation.speakers)
                    if conversation.speakers else "two people")

        prompt_tokens = completion_tokens = calls = cached = truncated = 0
        duplicates = generic_titles = salvaged = unparsable = empty = 0
        cost = latency = 0.0

        sessions = [s for s in conversation.sessions if s.turns]
        for session in tqdm(sessions, desc=f"{conversation.conv_id} wiki",
                            unit="session", leave=False):
            date_label = date_of(session.timestamp)
            wiki.sessions.append({"index": session.index, "session_id": session.session_id,
                                  "timestamp": session.timestamp, "date_label": date_label})

            user_prompt = EXTRACTION_TEMPLATE.format(
                speakers=speakers,
                session_number=session.index,
                date=session.timestamp or date_label,
                transcript="\n".join(t.render() for t in session.turns),
                page_list=wiki.page_list_for_prompt(),
            )
            response = self.client.complete(
                model=self.model,
                system_prompt=EXTRACTION_SYSTEM,
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
                added = wiki.add_fact(
                    title=title, page_type=item["page_type"], text=item["fact"],
                    date_label=date_label, session_index=session.index,
                    session_id=session.session_id,
                )
                duplicates += added is None

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
            "n_pages": len(wiki.pages),
            "n_facts": len(wiki.facts),
            "wiki_tokens": wiki_tokens,
            "raw_conversation_tokens": raw_tokens,
            "compression_ratio": round(wiki_tokens / raw_tokens, 4) if raw_tokens else None,
        }
        return wiki


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
        facts.append({"fact": fact, "page": page or "Miscellaneous", "page_type": page_type})

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

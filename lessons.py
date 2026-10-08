"""A learned-correction store with severity, escapes, and noise retirement.

Design adapted from the lesson model in **psygns/osENV.io**
(https://github.com/psygns/osENV.io), read at revision 0ef07457d4, version 0.3.0.
The upstream design carries these ideas:

  - a lesson is a written rule plus a precise description of the mistake as it is
    about to happen, not a topic or a category;
  - a lesson has a severity, and severity escalates: it starts as advice and
    becomes a hard stop once the same mistake has happened anyway enough times;
  - a lesson that keeps being judged relevant but never catches anything is
    noise, and noise retires;
  - proven lessons move between projects as a pack.

The upstream wording for the escalation rule, quoted for attribution:

  "the same mistake as an existing lesson? Then that lesson is sharpened, and
  escalates to a kick after 2 escapes, instead of a copy piling up."
      -- psygns, osENV.io README.md

  "Lessons that Jev keeps calling relevant but that never catch anything are
  noise: they retire."
      -- psygns, osENV.io learn.go

No code was copied from that project. There is no license file in the upstream
repository, so its source is all rights reserved; only the design was studied.
This module is an independent implementation in Python. It also differs in one
deliberate way: upstream ranks lessons with vector similarity, while this
implementation uses a deterministic lexical score, because the plugin must stay
dependency free and its ranking must be auditable line by line.

Local only. Nothing here contacts a provider.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

try:
    from .ledger import append as append_ledger
    from .runtime import get_hermes_home
except ImportError:  # Standalone package use.
    from ledger import append as append_ledger
    from runtime import get_hermes_home

# Structural constants, matching the upstream design so escalation behaves the
# same way. They are named rather than inlined because the tests assert them.
REPEAT_MATCH = 0.7          # a new correction at or above this repeats an old lesson
ESCALATE_AT_ESCAPES = 2     # escapes needed to turn advice into a hard stop
RETIRE_AT_SURFACED = 40     # judgements of relevance before noise is retired
CREDIBILITY_SURFACED_DIVISOR = 20

SEVERITIES = ("nudge", "kick")
KINDS = ("lesson", "route")
STATUSES = ("active", "retired")
OWNER_SOURCE = "owner"

# Two floors, because the two jobs have opposite error costs.
#
# SURFACE_FLOOR pre-ranks candidates for a semantic judgement. It is deliberately
# permissive: a false candidate costs one line in a prompt, while a missed
# candidate loses the whole point of the lesson. Lexical overlap cannot see a
# paraphrase, so this is a prefilter, never a verdict. Upstream does the same job
# with vector similarity and still routes the real judgement to Jev.
SURFACE_FLOOR = 0.05
# KICK_FLOOR is the bar for a local hard stop with no provider call. A wrong kick
# blocks the user's work, so the floor is set for precision, and it sits inside a
# measured band rather than at a guessed round number. On the calibration pairs in
# tests/test_lessons.py the paraphrases that must fire score 0.57 to 1.00 while the
# actions that must not fire score at most 0.14, so 0.40 keeps margin on both sides
# while staying biased toward not blocking real work.
#
# Reach is still bounded, and the bound is honest: canonical word matching catches
# the paraphrase families written into _CANON above, so "clear out the temporary
# decision folder with a recursive delete" now matches a lesson written as
# "rm -rf /tmp/jevs-decision-store". It cannot catch an unbounded rephrasing, which
# is why a review path exists and why a miss is recorded rather than hidden.
KICK_FLOOR = 0.40
# A kick also needs this many canonical words in common, independently of the
# score. Without it a one word lesson can reach the floor purely through the
# containment term against a long action, which is the cheapest possible way to
# block something by accident.
KICK_MIN_SHARED = 3

_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "if", "then", "than", "so", "to", "of",
    "in", "on", "at", "by", "for", "with", "from", "as", "is", "are", "was",
    "were", "be", "been", "being", "it", "its", "this", "that", "these", "those",
    "you", "your", "we", "our", "they", "their", "i", "me", "my", "not", "no",
    "do", "does", "did", "done", "have", "has", "had", "will", "would", "can",
    "could", "should", "must", "may", "might", "into", "out", "up", "down",
    "when", "which", "who", "what", "how", "why", "there", "here", "all", "any",
    "one", "two", "only", "also", "just", "more", "most", "less", "least",
})
_TOKEN_RE = re.compile(r"[a-z0-9_./:+-]+")
# Paths, flags, and dotted names are split so that "/tmp/x" can meet "temporary
# directory" and "-rf" can meet "recursive".
_SPLIT_RE = re.compile(r"[/\\.:_-]+")

# Words that belong to the tool envelope rather than to the action. Every action
# text is built as "<tool_name> <json args>", so without this the words "terminal"
# and "command" sit inside every single action, inflating the union and pushing
# every score down.
_ENVELOPE_WORDS = frozenset({
    "terminal", "command", "cmd", "cmdline", "arg", "args", "param", "params",
    "path", "content", "query", "input", "value", "values", "json", "shell",
})

# The deliberate half of paraphrase handling, and the reason it can exist at all.
# A local hard stop is not allowed to call a provider, so the only extra reach it
# can have is the reach written down here: different ways of naming one action
# collapse onto a single token. It is a small hand written table on purpose,
# because every entry is a claim about meaning that a reviewer can read and
# challenge one line at a time. Upstream reached this with vector similarity; a
# dependency free plugin cannot, so this table plus the shape rules below are the
# local substitute, and the honest limit is stated where the floor is defined.
_CANON = {
    "rm": "delete", "remove": "delete", "removing": "delete", "removed": "delete",
    "delete": "delete", "deleting": "delete", "deleted": "delete",
    "erase": "delete", "erasing": "delete", "erased": "delete",
    "wipe": "delete", "wiping": "delete", "wiped": "delete",
    "clear": "delete", "clearing": "delete", "cleared": "delete",
    "purge": "delete", "purging": "delete", "drop": "delete", "dropping": "delete",
    "unlink": "delete", "trash": "delete", "destroy": "delete", "destroying": "delete",
    "clean": "delete", "cleanup": "delete",
    "rf": "recursive", "r": "recursive", "recursive": "recursive", "recursively": "recursive",
    "f": "force", "force": "force", "forced": "force",
    "dir": "directory", "dirs": "directory", "folder": "directory", "folders": "directory",
    "directory": "directory", "directories": "directory",
    "tmp": "temporary", "temp": "temporary", "temporary": "temporary",
    "file": "file", "files": "file", "document": "file", "documents": "file",
    "store": "store", "storage": "store",
    "check": "check", "verify": "check", "verifies": "check", "confirm": "check",
    "inspect": "check", "validate": "check",
    "mv": "move", "move": "move", "moves": "move", "moving": "move",
    "rename": "move", "renaming": "move", "relocate": "move",
    "mkdir": "create", "create": "create", "creating": "create", "make": "create",
    "write": "write", "writing": "write", "wrote": "write",
    "cp": "copy", "copy": "copy", "copying": "copy",
    "prod": "production", "production": "production",
    "db": "database", "database": "database",
}

JACCARD_WEIGHT = 0.6
CONTAINMENT_WEIGHT = 0.4


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stem(word: str) -> str:
    """A crude suffix stripper, enough to fold "stores" and "storing" onto "store".

    Short words are left alone, because stripping four characters off a five
    letter word destroys it. A wrong fold can only cost precision on the local
    hard stop path, and every local stop is reviewable, so crude is acceptable
    here where clever would not be auditable.
    """
    for suffix in ("ing", "ed", "es", "s"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def _canonical(word: str) -> str:
    direct = _CANON.get(word)
    if direct:
        return direct
    stemmed = _stem(word)
    return _CANON.get(stemmed, stemmed)


def _tokens(text: Any) -> set[str]:
    """Canonical content words for matching.

    Deterministic and auditable line by line, which matters more here than
    semantic reach: this decides whether a lesson is worth surfacing, whether a
    correction repeats an existing lesson, and whether a kick may stop an action
    locally with no provider call.
    """
    out: set[str] = set()
    for raw in _TOKEN_RE.findall(str(text or "").lower()):
        for part in _SPLIT_RE.split(raw):
            if len(part) < 2 or part in _STOPWORDS or part in _ENVELOPE_WORDS:
                continue
            out.add(_canonical(part))
    return out


def _shared_count(left: Any, right: Any) -> int:
    """How many canonical words two texts have in common."""
    return len(_tokens(left) & _tokens(right))


def _lexical_overlap(left: Any, right: Any) -> float:
    """Similarity of two texts, 0 to 1, over canonical content words.

    Two shapes are blended because either one alone fails on this path. Jaccard
    alone punishes a lesson for the action text being longer, and an action text
    always carries more words than the rule describing it. Containment of the
    smaller set is what actually answers "does this lesson describe this action",
    but used alone it would score a one word lesson highly against any long
    action, so it is weighted below Jaccard and measured against the smaller set.

    Sharing no canonical word scores exactly zero, so an unrelated lesson cannot
    drift up onto a floor by accident.
    """
    a, b = _tokens(left), _tokens(right)
    if not a or not b:
        return 0.0
    shared = len(a & b)
    if not shared:
        return 0.0
    jaccard = shared / len(a | b)
    containment = shared / min(len(a), len(b))
    return JACCARD_WEIGHT * jaccard + CONTAINMENT_WEIGHT * containment


@dataclass
class Lesson:
    """One written rule, with its precision description and its track record."""

    id: str
    text: str
    detect: str
    kind: str = "lesson"
    scope: str = "global"
    to: str = ""
    severity: str = "nudge"
    source: str = "stated"
    evidence: str = ""
    tags: list[str] = field(default_factory=list)
    made: str = ""
    surfaced: int = 0
    catches: int = 0
    escapes: int = 0
    status: str = "active"
    why: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "Lesson":
        """Rebuild one lesson, normalising whatever was on disk.

        The store file is data that can be hand edited or truncated by a crash, so
        every field is coerced here once rather than checked at each use. A row
        without a usable id is rejected by the caller, not patched up.
        """
        known = {f for f in cls.__dataclass_fields__}
        clean = {k: v for k, v in row.items() if k in known}
        clean["id"] = str(clean.get("id") or "")
        clean["text"] = str(clean.get("text") or "")
        clean["detect"] = str(clean.get("detect") or "")
        clean["why"] = str(clean.get("why") or "")
        clean["kind"] = clean.get("kind") if clean.get("kind") in KINDS else "lesson"
        clean["status"] = clean.get("status") if clean.get("status") in STATUSES else "active"
        clean["severity"] = clean.get("severity") if clean.get("severity") in SEVERITIES else "nudge"
        raw_tags = clean.get("tags")
        clean["tags"] = [str(t) for t in raw_tags] if isinstance(raw_tags, (list, tuple)) else []
        for counter in ("surfaced", "catches", "escapes"):
            try:
                clean[counter] = max(0, int(clean.get(counter) or 0))
            except (TypeError, ValueError):
                clean[counter] = 0
        return cls(**clean)

    @property
    def owner(self) -> bool:
        return self.source == OWNER_SOURCE

    def credibility(self) -> float:
        """How much its track record should count, 0 to 1.

        A lesson judged relevant many times without ever catching anything loses
        ranking weight long before it retires.
        """
        proven = self.catches + self.escapes + 1
        return proven / (proven + self.surfaced / CREDIBILITY_SURFACED_DIVISOR)

    def relevance(self, action_text: str, tags: set[str] | None = None) -> float:
        """Deterministic relevance of this lesson to a described action."""
        overlap = _lexical_overlap(action_text, f"{self.text} {self.detect}")
        if not overlap:
            return 0.0
        own, given = set(self.tags), (tags or set())
        tag_boost = 0.15 * (len(own & given) / max(1, len(own | given)))
        return min(1.0, (overlap + tag_boost) * self.credibility())

    def as_pack_row(self) -> dict[str, Any]:
        """Proven lessons carry their track record into another install."""
        return {
            "text": self.text,
            "detect": self.detect,
            "severity": self.severity,
            "catches": self.catches,
            "escapes": self.escapes,
        }


class LessonStore:
    """Local, file backed lesson store. One JSON document plus a sequence file."""

    def __init__(self, root: str | None = None) -> None:
        self._lock = threading.RLock()
        self._root = root
        self._path = ""
        self._seq_path = ""
        self._items: list[Lesson] = []
        self._seq = 0
        self._loaded = False

    # -- storage ---------------------------------------------------------

    def _paths(self) -> tuple[str, str]:
        base = self._root or os.path.join(str(get_hermes_home()), "jev")
        return os.path.join(base, "lessons.json"), os.path.join(base, "lessons-seq.json")

    def _load(self) -> None:
        if self._loaded:
            return
        self._path, self._seq_path = self._paths()
        with contextlib.suppress(OSError):
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
        for attr, path in (("_items", self._path), ("_seq", self._seq_path)):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    raw = json.load(handle)
            except (OSError, ValueError):
                continue
            if attr == "_items" and isinstance(raw, list):
                rows = []
                for row in raw:
                    if not isinstance(row, dict):
                        continue
                    try:
                        item = Lesson.from_dict(row)
                    except (TypeError, ValueError):
                        continue
                    if item.id:
                        rows.append(item)
                self._items = rows
            elif attr == "_seq" and isinstance(raw, int):
                self._seq = raw
        self._loaded = True

    @contextlib.contextmanager
    def _mutation(self):
        """Serialise one read-modify-write of the whole document across processes.

        Gateway, CLI, and cron run as separate processes, so an unguarded
        load-mutate-save would silently drop whichever writer finished second. The
        lock is advisory and shared with every other Jev process by path. If the
        lock cannot be taken the write still proceeds, because losing a lesson
        update is better than failing the caller.
        """
        base = self._path or self._paths()[0]
        handle = None
        try:
            os.makedirs(os.path.dirname(base), exist_ok=True)
            handle = open(f"{base}.lock", "a+")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except OSError:
            if handle is not None:
                handle.close()
            handle = None
        try:
            if handle is not None:
                # Another process may have written since this one last read.
                self._loaded = False
            self._load()
            yield
        finally:
            if handle is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()

    def _save(self) -> None:
        """Atomic write, so a crash cannot truncate the store."""
        for path, payload in (
            (self._path, json.dumps([item.as_dict() for item in self._items], ensure_ascii=True, sort_keys=True, indent=1)),
            (self._seq_path, json.dumps(self._seq)),
        ):
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)

    def reset(self, root: str | None = None) -> None:
        with self._lock:
            self._root = root
            self._loaded = False
            self._items = []
            self._seq = 0
            self._load()

    # -- reads -----------------------------------------------------------

    def all(self, include_retired: bool = False) -> list[Lesson]:
        with self._lock:
            self._load()
            return [i for i in self._items if include_retired or i.status == "active"]

    def get(self, lesson_id: str) -> Lesson | None:
        with self._lock:
            self._load()
            for item in self._items:
                if item.id == lesson_id:
                    return item
            return None

    def candidates(self, action_text: str, tags: set[str] | None = None, limit: int = 8, floor: float | None = None) -> list[tuple[Lesson, float]]:
        """Active lessons pre-ranked for a semantic judgement, above a floor.

        This is a shortlist, not an answer. The caller decides which of these
        actually match the action and records that with ``record_surfaced`` and
        ``record_caught``.
        """
        floor = SURFACE_FLOOR if floor is None else floor
        scored = []
        for item in self.all():
            score = item.relevance(action_text, tags)
            if score >= floor:
                scored.append((item, score))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[: max(1, limit)]

    def local_kicks(self, action_text: str, tags: set[str] | None = None) -> list[tuple[Lesson, float]]:
        """Kick lessons that match closely enough to stop an action locally.

        Local only and provider free. The match is canonical word overlap, so it
        reaches the paraphrase families named in ``_CANON`` (delete/remove/wipe,
        tmp/temporary, dir/folder) and misses an unbounded rephrasing, which is
        why an explicit review exists. Two independent gates apply: the score
        floor, and a minimum number of words in common, so a short lesson cannot
        stop a long action on the containment term alone.
        """
        hits = []
        for item, score in self.candidates(action_text, tags, limit=16, floor=KICK_FLOOR):
            if item.severity != "kick":
                continue
            if _shared_count(action_text, f"{item.text} {item.detect}") < KICK_MIN_SHARED:
                continue
            hits.append((item, score))
        return hits

    def stats(self) -> dict[str, Any]:
        items = self.all(include_retired=True)
        active = [i for i in items if i.status == "active"]
        return {
            "total": len(items),
            "active": len(active),
            "retired": len(items) - len(active),
            "owner_rules": len([i for i in items if i.owner]),
            "kicks": len([i for i in active if i.severity == "kick"]),
            "nudges": len([i for i in active if i.severity == "nudge"]),
            "catches": sum(i.catches for i in items),
            "escapes": sum(i.escapes for i in items),
            "surfaced": sum(i.surfaced for i in items),
            "by_source": sorted({i.source for i in items}),
        }

    # -- writes ----------------------------------------------------------

    def add(
        self,
        *,
        text: str,
        detect: str = "",
        severity: str = "nudge",
        source: str = "stated",
        scope: str = "global",
        evidence: str = "",
        tags: list[str] | None = None,
        kind: str = "lesson",
    ) -> dict[str, Any]:
        """Record a correction.

        A correction that repeats an active lesson sharpens that lesson instead of
        adding a near copy, and that repeat is what escalates severity.
        """
        text = str(text or "").strip()
        if not text:
            raise ValueError("a lesson needs text")
        detect = str(detect or "").strip()
        with self._lock, self._mutation():
            repeat = self._match_repeat(text, detect)
            if repeat is not None:
                lesson, score = repeat
                # Each correction call is one occurrence of the mistake. A caller
                # holding several findings that map to the same lesson must
                # deduplicate them before calling, or the lesson escalates early.
                lesson.escapes += 1
                lesson.detect = detect or lesson.detect
                lesson.tags = sorted(set(lesson.tags) | _tokens(f"{text} {detect}"))
                if lesson.escapes >= ESCALATE_AT_ESCAPES and lesson.severity != "kick":
                    lesson.severity = "kick"
                if lesson.scope != "global":
                    lesson.scope = "global"
                lesson.why = f"{lesson.why} | merged {_now()[:16]} (same {score:.2f})".strip()
                self._save()
                append_ledger("lesson", {"action": "merged", "lesson_id": lesson.id, "escapes": lesson.escapes, "severity": lesson.severity})
                return {"lesson": lesson.as_dict(), "how": "merged", "score": round(score, 4)}

            self._seq += 1
            severity = severity if severity in SEVERITIES else "nudge"
            if source == OWNER_SOURCE:
                # An owner rule is a hard stop from the moment it is made.
                severity = "kick"
            item = Lesson(
                id=f"{'R' if kind == 'route' else 'L'}{self._seq}",
                text=text,
                detect=detect,
                kind=kind if kind in KINDS else "lesson",
                scope=scope,
                severity=severity,
                source=source,
                evidence=str(evidence or ""),
                tags=sorted(set(tags or []) | _tokens(f"{text} {detect}")),
                made=_now(),
                why="owner rule: a hard stop from day one, never retired" if source == OWNER_SOURCE else "",
            )
            self._items.append(item)
            self._save()
            append_ledger("lesson", {"action": "added", "lesson_id": item.id, "severity": item.severity, "source": item.source})
            return {"lesson": item.as_dict(), "how": "added"}

    def _match_repeat(self, text: str, detect: str) -> tuple[Lesson, float] | None:
        """The active lesson this correction repeats, if any.

        Similarity only, not the credibility weighted relevance: a repeat is
        about how alike the mistake is, not about the old lesson's record.
        """
        best: Lesson | None = None
        best_score = 0.0
        for item in self._items:
            if item.status != "active" or item.kind != "lesson":
                continue
            score = _lexical_overlap(f"{text} {detect}", f"{item.text} {item.detect}")
            if score >= REPEAT_MATCH and score > best_score:
                best, best_score = item, score
        if best is None:
            return None
        return best, best_score

    def record_surfaced(self, lesson_ids: list[str]) -> dict[str, int]:
        """Count that these lessons were judged relevant to an action.

        Only record this from a real judgement, meaning a local kick match or a
        review that returned which lessons applied. Counting prefilter candidates
        here would inflate the tally and retire good lessons as noise.
        """
        with self._lock, self._mutation():
            touched = {}
            for lesson_id in lesson_ids:
                item = self.get(lesson_id)
                if item is not None:
                    item.surfaced += 1
                    touched[item.id] = item.surfaced
            if touched:
                self._save()
            return touched

    def note_would_kick(self, lesson_id: str, *, tool_name: str = "", score: float = 0.0) -> None:
        """Record that a kick lesson matched while enforcement was off.

        Shadow mode must not change execution, so nothing was caught. This keeps
        the evidence of what enforcement would have done, and it deliberately does
        not touch the catch tally.
        """
        append_ledger("lesson_would_kick", {
            "lesson_id": lesson_id,
            "tool_name": str(tool_name or ""),
            "score": round(float(score), 4),
        })

    def record_caught(self, lesson_ids: list[str]) -> dict[str, int]:
        """Count a mistake caught before it happened."""
        with self._lock, self._mutation():
            touched = {}
            for lesson_id in lesson_ids:
                item = self.get(lesson_id)
                if item is not None:
                    item.catches += 1
                    touched[item.id] = item.catches
            if touched:
                self._save()
                append_ledger("lesson", {"action": "caught", "lesson_ids": sorted(touched)})
            return touched

    def edit(
        self,
        lesson_id: str,
        *,
        severity: str | None = None,
        escapes: int | None = None,
        catches: int | None = None,
        detect: str | None = None,
        text: str | None = None,
        scope: str | None = None,
    ) -> dict[str, Any]:
        """Correct a lesson by hand, keeping its record.

        An owner rule is exempt, and exempt wholly rather than field by field.
        `retire` and `sweep` both refuse owner rules, and the promise they keep
        is that an owner rule is a hard stop from the moment it is made. An
        `edit` that could lower `severity`, rewrite `text`, or zero the escape
        count would reopen every door those two guards close, by the one caller
        that holds the tool: the agent being gated. There is deliberately no
        operator exception here either, because this method is the model-facing
        boundary and an authority flag it can simply not pass is the only one
        that cannot be forged.
        """
        with self._lock, self._mutation():
            item = self.get(lesson_id)
            if item is None:
                raise ValueError(f"unknown lesson: {lesson_id}")
            if item.owner:
                raise ValueError(
                    "an owner rule is never weakened; make a new rule instead"
                )
            if severity is not None:
                item.severity = severity if severity in SEVERITIES else item.severity
            if escapes is not None:
                item.escapes = max(0, int(escapes))
            if catches is not None:
                item.catches = max(0, int(catches))
            if detect is not None:
                item.detect = str(detect).strip()
            if text is not None:
                item.text = str(text).strip()
            if scope is not None:
                item.scope = str(scope)
            item.why = f"{item.why} | edited {_now()[:16]}".strip()
            self._save()
            return {"lesson": item.as_dict()}

    def retire(self, lesson_id: str, reason: str = "retired by hand") -> dict[str, Any]:
        with self._lock, self._mutation():
            item = self.get(lesson_id)
            if item is None:
                raise ValueError(f"unknown lesson: {lesson_id}")
            if item.owner:
                raise ValueError("an owner rule is never retired")
            item.status = "retired"
            item.why = f"{item.why} | {reason}".strip()
            self._save()
            append_ledger("lesson", {"action": "retired", "lesson_id": item.id, "reason": reason})
            return {"lesson": item.as_dict()}

    def sweep(self) -> list[str]:
        """Retire noise: lessons judged relevant many times that never caught anything."""
        with self._lock, self._mutation():
            gone = []
            for item in self._items:
                if (item.status == "active" and item.kind == "lesson" and not item.owner
                        and item.surfaced >= RETIRE_AT_SURFACED and item.catches == 0 and item.escapes == 0):
                    item.status = "retired"
                    item.why = f"{item.why} | retired as noise: surfaced {item.surfaced}, never caught".strip()
                    gone.append(item.id)
            if gone:
                self._save()
                append_ledger("lesson", {"action": "swept", "lesson_ids": gone})
            return gone

    # -- packs -----------------------------------------------------------

    def export_pack(self, *, source: str = "", min_catches: int = 1) -> dict[str, Any]:
        """Proven, global, non-owner lessons, ready for another install."""
        rows = [
            item.as_pack_row()
            for item in self.all()
            if item.kind == "lesson" and item.scope == "global" and not item.owner and item.catches >= min_catches
        ]
        return {"from": source or str(get_hermes_home()), "made": _now(), "lessons": rows}

    def import_pack(self, pack: dict[str, Any]) -> dict[str, Any]:
        """Load a pack. Imported lessons arrive with no local track record."""
        if not isinstance(pack, dict):
            raise ValueError("a pack must be an object")
        rows = pack.get("lessons")
        if not isinstance(rows, list):
            raise ValueError("a pack needs a lessons list")
        origin = str(pack.get("from") or "unknown")
        made = str(pack.get("made") or "")[:10]
        added = []
        with self._lock, self._mutation():
            for row in rows:
                if not isinstance(row, dict) or not str(row.get("text") or "").strip():
                    continue
                self._seq += 1
                raw_severity = str(row.get("severity") or "")
                severity = raw_severity if raw_severity in SEVERITIES else "nudge"
                item = Lesson(
                    id=f"L{self._seq}",
                    text=str(row["text"]).strip(),
                    detect=str(row.get("detect") or "").strip(),
                    severity=severity,
                    source=f"imported:{origin}",
                    tags=sorted(_tokens(f"{row.get('text', '')} {row.get('detect', '')}")),
                    made=_now(),
                    why=(f"imported from {origin}'s lesson pack ({made}): "
                         f"{int(row.get('catches') or 0)} catches, {int(row.get('escapes') or 0)} escapes there"),
                )
                self._items.append(item)
                added.append(item.id)
            self._save()
        if added:
            append_ledger("lesson", {"action": "imported", "from": origin, "lesson_ids": added})
        return {"imported": added, "count": len(added), "from": origin}


_DEFAULT: LessonStore | None = None
_DEFAULT_LOCK = threading.Lock()


def default_store() -> LessonStore:
    """Process wide lesson store used by the plugin."""
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = LessonStore()
        return _DEFAULT

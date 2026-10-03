"""The derived layer: the novelized manuscript.

The log is roleplay — turns, headers, two people writing at each other. The
manuscript is the same events as prose. It is a SEPARATE DOCUMENT, not an edit
of the log, because the transformation is lossy in the direction that matters:
you cannot recover "Wren: *she lets the chart roll slip*" from "Wren let one of
her charts slip and stooped beside the nearest cask." Keeping them as one
mutable text would mean every novelization silently destroyed its own source.

So the manuscript reads from the log and never writes toward it:

    log  ──novelize──▶  manuscript

Provenance rides on the block, never in the prose. A block records which source
uids it came from, so an exported chapter is clean text while the editor can
still answer "what did this paragraph used to be" for any sentence on screen.

Drift
-----
A derived scene stores a signature of what it was generated from. When the
layer below changes — a restyle lands on message 691, an interlude is injected
mid-scene — the signature no longer matches and the scene is in **drift**: still
valid prose, but no longer a faithful derivation. Drift is detected, never
auto-resolved; rebasing is the author's call, and a scene may legitimately be
pinned to a source that has since moved on.

The signature covers each message's uid *and* its text, so an insertion inside a
span registers as drift even when no existing word changed. That case is the
whole reason Phase 0 happened, and it would otherwise be invisible.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import config, loader, text as text_mod
from .identity import Anchor

Log = loader.Log

SCHEMA = "story-editor/manuscript@1"

# A derived scene's life: generated → the author accepts or refuses it. Nothing
# reaches an export until it is approved.
STATUSES = ("draft", "approved", "rejected")

# Deliberately few. Structure that a typesetter needs, not a rich text model —
# anything more expressive belongs in the prose itself.
BLOCK_KINDS = ("para", "dialogue", "break", "heading")

MANUSCRIPT_LAYER = "manuscript"


# --------------------------------------------------------------------------- #
# Voice
# --------------------------------------------------------------------------- #
#
# Person and tense are the two decisions a novelization cannot avoid making, and
# they are the author's to make: the same log is a different book in first-person
# present than in close third past. So voice is stored, not inferred — once on
# the document as the book's default, and optionally on a scene that departs from
# it (a chapter that follows a different head is ordinary craft, not an error).

PERSONS: dict[str, str] = {
    "first": "first person",
    "second": "second person",
    "close_third": "close third person",
    "omniscient": "third person omniscient",
}

TENSES: dict[str, str] = {"past": "past tense", "present": "present tense"}

# Only these need to know whose head they are in; omniscient has no focal char.
FOCAL_PERSONS = frozenset({"first", "second", "close_third"})


@dataclass
class Voice:
    person: str = "close_third"
    tense: str = "past"
    focal: str = ""  # whose perception bounds the page; unused by omniscient

    def normalized(self) -> "Voice":
        person = self.person if self.person in PERSONS else "close_third"
        tense = self.tense if self.tense in TENSES else "past"
        focal = self.focal.strip() if person in FOCAL_PERSONS else ""
        return Voice(person=person, tense=tense, focal=focal)

    def needs_focal(self) -> bool:
        return self.person in FOCAL_PERSONS

    def describe(self) -> str:
        parts = [PERSONS.get(self.person, self.person), TENSES.get(self.tense, self.tense)]
        line = ", ".join(parts)
        return f"{line}, following {self.focal}" if self.focal else line

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"person": self.person, "tense": self.tense}
        if self.focal:
            out["focal"] = self.focal
        return out

    @classmethod
    def from_json(cls, d: dict[str, Any] | None) -> "Voice":
        d = d or {}
        return cls(
            person=str(d.get("person") or "close_third"),
            tense=str(d.get("tense") or "past"),
            focal=str(d.get("focal") or ""),
        ).normalized()

    def merged(self, patch: dict[str, Any] | None) -> "Voice":
        """This voice with only the fields the caller actually expressed changed.
        Asking for present tense must not quietly reset the focal character."""
        patch = patch or {}
        return Voice(
            person=str(patch.get("person") or self.person),
            tense=str(patch.get("tense") or self.tense),
            focal=str(patch["focal"]) if "focal" in patch else self.focal,
        ).normalized()


def mint_scene_id() -> str:
    return "sc-" + uuid.uuid4().hex[:12]


def mint_block_id() -> str:
    return "b-" + uuid.uuid4().hex[:8]


def path_for(layer: str = MANUSCRIPT_LAYER) -> Path:
    """Where a layer's document lives. The manuscript is the only derived layer."""
    if layer != MANUSCRIPT_LAYER:
        raise ValueError(f"no document for the {layer!r} layer")
    return Path(config.MANUSCRIPT)


# --------------------------------------------------------------------------- #
# Signatures
# --------------------------------------------------------------------------- #


def signature_of(parts: Iterable[str]) -> str:
    h = hashlib.sha1()
    for part in parts:
        h.update(part.encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


def is_front_matter(scene: "Scene") -> bool:
    """True for manuscript scenes that are not derived from the log.

    Used for the PDF bookends (prologue, later the epilogue). They sort before
    the novel by sitting on a negative ordinal span, so they never cover a
    message and never steal another scene's novelization.
    """
    return scene.anchor.start < 0 and scene.anchor.end < 0


def source_messages(log: Log, anchor: Anchor) -> list[loader.Message]:
    start, end = anchor.resolve(log)
    if not len(log):
        return []
    # Front-matter spans sit below 0 on purpose. Clamping them to 0 would make
    # the prologue appear to derive from the first log turn.
    if start < 0 or end < 0:
        return []
    start = max(0, min(start, len(log) - 1))
    end = max(0, min(end, len(log) - 1))
    return log.messages[start : end + 1]


def source_text(log: Log, anchor: Anchor) -> str:
    """The prose a novelization pass reads: headers and meta blocks stripped,
    turns separated, speakers preserved so the model knows who acts."""
    out = []
    for m in source_messages(log, anchor):
        body = text_mod.clean(m.text)
        if body:
            out.append(f"{m.speaker}: {body}")
    return "\n\n".join(out)


def source_signature(log: Log, anchor: Anchor) -> str:
    """Identity of a log span's content — uids included, so inserting a message
    into the span is a change even if every existing message is untouched."""
    return signature_of(
        part
        for m in source_messages(log, anchor)
        for part in (m.uid or f"@{m.msg_id}", text_mod.clean(m.text))
    )


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #


@dataclass
class Block:
    """One paragraph of derived prose, and where it came from."""

    id: str
    kind: str = "para"
    text: str = ""
    src: list[str] = field(default_factory=list)  # source uids (or parent block ids)
    edited: bool = False  # hand-touched after generation
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "kind": self.kind, "text": self.text}
        if self.src:
            out["src"] = self.src
        if self.edited:
            out["edited"] = True
        if self.note:
            out["note"] = self.note
        return out

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Block":
        return cls(
            id=str(d.get("id") or mint_block_id()),
            kind=str(d.get("kind") or "para"),
            text=str(d.get("text") or ""),
            src=[str(x) for x in (d.get("src") or [])],
            edited=bool(d.get("edited")),
            note=str(d.get("note") or ""),
        )


@dataclass
class Scene:
    """A derived scene: prose blocks plus the provenance to defend them."""

    id: str
    anchor: Anchor = field(default_factory=Anchor)  # log span
    source_sig: str = ""
    status: str = "draft"
    title: str = ""
    blocks: list[Block] = field(default_factory=list)
    generated: str = ""
    model: str = ""
    notes: str = ""
    voice: Voice | None = None  # None → the document's default voice
    pinned: bool = False  # deliberate divergence from source (pin, not rebase)
    log_scene_id: int | None = None  # source segmentation provenance
    narrative_scene_id: str = ""  # stable manuscript-facing unit label
    episode_id: int | None = None  # canonical chapter ownership
    episode_title: str = ""

    def text(self) -> str:
        """The prose, and nothing else — this is what export emits."""
        return "\n\n".join(b.text.strip() for b in self.blocks if b.text.strip())

    def source_uids(self) -> list[str]:
        seen: list[str] = []
        for b in self.blocks:
            for uid in b.src:
                if uid not in seen:
                    seen.append(uid)
        return seen

    def log_span(self) -> tuple[int | None, int | None]:
        """The log ordinals this scene covers, or ``(None, None)`` when it has
        no log anchor (front matter)."""
        a = self.anchor
        if a.start < 0 or not (a.from_uid or a.to_uid or a.end):
            return None, None
        return a.start, a.end

    def block_texts(self) -> list[tuple[str, str]]:
        return [(b.id, b.text) for b in self.blocks]

    def block(self, block_id: str) -> Block | None:
        return next((b for b in self.blocks if b.id == block_id), None)

    def bind(self, log: Log) -> None:
        """Refresh the ordinal cache from the uids. Cheap, and it means a scene
        written before an injection still reports the right message numbers."""
        if self.anchor.from_uid or self.anchor.to_uid:
            self.anchor = self.anchor.refreshed(log)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "status": self.status,
            "source_sig": self.source_sig,
            "blocks": [b.to_json() for b in self.blocks],
        }
        if (
            self.anchor.from_uid
            or self.anchor.to_uid
            or self.anchor.end
            or self.anchor.start < 0
        ):
            out["source"] = self.anchor.to_json()
        if self.voice is not None:
            out["voice"] = self.voice.to_json()
        if self.pinned:
            out["pinned"] = True
        if self.log_scene_id is not None:
            out["log_scene_id"] = self.log_scene_id
        if self.narrative_scene_id:
            out["narrative_scene_id"] = self.narrative_scene_id
        if self.episode_id is not None:
            out["episode_id"] = self.episode_id
        if self.episode_title:
            out["episode_title"] = self.episode_title
        for key in ("title", "generated", "model", "notes"):
            value = getattr(self, key)
            if value:
                out[key] = value
        return out

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Scene":
        return cls(
            id=str(d.get("id") or mint_scene_id()),
            anchor=Anchor.from_json(d.get("source")),
            source_sig=str(d.get("source_sig") or ""),
            status=str(d.get("status") or "draft"),
            title=str(d.get("title") or ""),
            blocks=[Block.from_json(b) for b in (d.get("blocks") or [])],
            generated=str(d.get("generated") or ""),
            model=str(d.get("model") or ""),
            notes=str(d.get("notes") or ""),
            voice=Voice.from_json(d["voice"]) if d.get("voice") else None,
            pinned=bool(d.get("pinned")),
            log_scene_id=(int(d["log_scene_id"]) if d.get("log_scene_id") is not None else None),
            narrative_scene_id=str(d.get("narrative_scene_id") or ""),
            episode_id=(int(d["episode_id"]) if d.get("episode_id") is not None else None),
            episode_title=str(d.get("episode_title") or ""),
        )


@dataclass
class Document:
    """One layer's worth of derived scenes."""

    layer: str = MANUSCRIPT_LAYER
    scenes: list[Scene] = field(default_factory=list)
    log: str = ""
    updated: str = ""
    voice: Voice = field(default_factory=Voice)  # the book's default voice

    def __len__(self) -> int:
        return len(self.scenes)

    def voice_for(self, scene: Scene | None) -> Voice:
        """The voice a scene is written in: its own if it departs from the book,
        the book's otherwise."""
        if scene is not None and scene.voice is not None:
            return scene.voice
        return self.voice

    def by_id(self, scene_id: str) -> Scene | None:
        return next((s for s in self.scenes if s.id == scene_id), None)


    def covering(self, msg_id: int) -> Scene | None:
        """Which derived scene claims this log message.

        Front matter sits on a negative span so it can never cover a real turn.
        Looking up a negative ordinal returns the matching bookend, if any.
        """
        if msg_id < 0:
            return next(
                (
                    s
                    for s in self.scenes
                    if is_front_matter(s)
                    and s.anchor.start <= msg_id <= s.anchor.end
                ),
                None,
            )
        return next(
            (
                s
                for s in self.scenes
                if not is_front_matter(s)
                and s.anchor.start <= msg_id <= s.anchor.end
            ),
            None,
        )

    def upsert(self, scene: Scene) -> None:
        existing = self.by_id(scene.id)
        if existing is None:
            self.scenes.append(scene)
        else:
            self.scenes[self.scenes.index(existing)] = scene
        self.scenes.sort(key=lambda s: (s.anchor.start, s.id))

    def remove(self, scene_id: str) -> bool:
        scene = self.by_id(scene_id)
        if scene is None:
            return False
        self.scenes.remove(scene)
        return True

    def stats(self) -> dict[str, int]:
        counts = {status: 0 for status in STATUSES}
        for s in self.scenes:
            counts[s.status] = counts.get(s.status, 0) + 1
        counts["scenes"] = len(self.scenes)
        counts["blocks"] = sum(len(s.blocks) for s in self.scenes)
        counts["words"] = sum(len(s.text().split()) for s in self.scenes)
        return counts


# --------------------------------------------------------------------------- #
# Construction
# --------------------------------------------------------------------------- #


def new_scene(
    log: Log,
    start: int,
    end: int,
    *,
    blocks: list[Block] | None = None,
    title: str = "",
    model: str = "",
    status: str = "draft",
    voice: Voice | None = None,
) -> Scene:
    """A manuscript scene over a log span, stamped with the source it derives
    from so drift can be detected later.

    The voice is stamped too: a scene written in close third past does not
    silently become first-person present because the book's default changed.
    """
    anchor = Anchor.for_span(log, start, end)
    return Scene(
        id=mint_scene_id(),
        anchor=anchor,
        source_sig=source_signature(log, anchor),
        status=status,
        title=title,
        blocks=list(blocks or []),
        generated=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        model=model,
        voice=voice.normalized() if voice is not None else None,
    )


def new_front_matter_scene(
    *,
    blocks: list[Block],
    title: str,
    notes: str = "",
    status: str = "approved",
    model: str = "",
    voice: Voice | None = None,
    pinned: bool = True,
    start: int = -1,
    end: int = -1,
) -> Scene:
    """A manuscript scene that is not derived from the log.

    Negative ordinals keep it ahead of the novel and out of ``covering`` for
    real turns. ``source_sig`` is the empty-span signature, so the log moving
    underneath Opening cannot mark this file as drifted.
    """
    if start >= 0 or end >= 0:
        raise ValueError("front matter must sit on a negative ordinal span")
    anchor = Anchor(start=start, end=end)
    return Scene(
        id=mint_scene_id(),
        anchor=anchor,
        source_sig=signature_of([]),
        status=status,
        title=title,
        blocks=list(blocks),
        generated=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        model=model,
        notes=notes,
        voice=voice.normalized() if voice is not None else None,
        pinned=pinned,
    )



def blocks_from_prose(prose: str, src: list[str] | None = None) -> list[Block]:
    """Split generated prose on blank lines. The model returns paragraphs; the
    document stores them addressably so annotations can hang off one of them."""
    out: list[Block] = []
    for chunk in prose.replace("\r\n", "\n").split("\n\n"):
        body = chunk.strip()
        if body:
            out.append(Block(id=mint_block_id(), text=body, src=list(src or [])))
    return out


def reconcile_blocks(old: list[Block], prose: str, src: list[str] | None = None) -> list[Block]:
    """Re-split a hand-edited scene without re-minting every block.

    Paragraphs whose text survived keep their block untouched (id, kind, src,
    edited, note), and a paragraph rewritten in place keeps its id. Queued
    prose-review proposals address blocks by id, so a whole-scene save that
    minted fresh ids would strand every one of them.
    """
    import difflib

    fresh = blocks_from_prose(prose, src=src)
    matcher = difflib.SequenceMatcher(
        a=[b.text.strip() for b in old], b=[b.text for b in fresh], autojunk=False,
    )
    out: list[Block] = []
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            out.extend(old[i1:i2])
            continue
        kept = old[i1:i2] if op == "replace" else []
        for k, block in enumerate(fresh[j1:j2]):
            if k < len(kept):
                prev = kept[k]
                prev.text = block.text
                prev.edited = True
                out.append(prev)
            else:
                block.edited = True
                out.append(block)
    return out


# --------------------------------------------------------------------------- #
# Drift
# --------------------------------------------------------------------------- #

CLEAN = ""
SOURCE_CHANGED = "source-changed"
ANCHOR_LOST = "anchor-lost"
PINNED = "pinned"


@dataclass
class Drift:
    scene_id: str
    kind: str = CLEAN
    detail: str = ""

    @property
    def dirty(self) -> bool:
        """Unresolved drift that should block a clean export. Pinned divergence
        is intentional — shown in the UI, not treated as a failure."""
        return self.kind in (SOURCE_CHANGED, ANCHOR_LOST)

    def to_json(self) -> dict[str, Any]:
        return {"scene_id": self.scene_id, "kind": self.kind, "detail": self.detail}


def scene_drift(scene: Scene, source: Log) -> Drift:
    """Compare a manuscript scene against the log span it was generated from."""
    log = source
    for uid in (scene.anchor.from_uid, scene.anchor.to_uid):
        if uid and log.ordinal_of(uid) is None:
            return Drift(scene.id, ANCHOR_LOST, f"source message {uid} is gone")
    current = source_signature(log, scene.anchor)
    if current != scene.source_sig:
        if scene.pinned:
            return Drift(scene.id, PINNED, "log moved; divergence pinned")
        return Drift(scene.id, SOURCE_CHANGED, "log text changed under this scene")
    return Drift(scene.id)


def drift(doc: Document, source: Log) -> list[Drift]:
    """Every scene's standing against the log. Cheap enough to run on
    load, which is what makes a live drift indicator possible in the GUI."""
    return [scene_drift(s, source) for s in doc.scenes]


def rebase(scene: Scene, source: Log) -> Scene:
    """Accept the current source as this scene's basis without touching its
    prose. The author's way of saying "I have read the change and the paragraph
    still stands" — the alternative to regenerating.

    When a boundary uid was deleted (``anchor-lost``), ``Anchor.refreshed``
    alone keeps the dead uids, so drift would stay dirty forever. Remint the
    anchor from the resolved ordinals so rebase actually clears the warning.
    """
    start, end = scene.anchor.resolve(source)
    # Remint uids at the current ordinals (not refreshed(), which preserves
    # gone boundary uids and leaves ANCHOR_LOST uncleared).
    scene.anchor = Anchor.for_span(source, start, end)
    scene.source_sig = source_signature(source, scene.anchor)
    scene.pinned = False
    return scene


def pin(scene: Scene) -> Scene:
    """Mark a source divergence as deliberate. Prose and signature stay put;
    drift reports ``pinned`` instead of ``source-changed``."""
    scene.pinned = True
    return scene


def unpin(scene: Scene) -> Scene:
    scene.pinned = False
    return scene


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #


def load(
    layer: str = MANUSCRIPT_LAYER,
    *,
    path: Path | None = None,
    log: Log | None = None,
    bind: bool = True,
) -> Document:
    """Read a layer's document, re-resolving anchors against the log.

    ``bind=False`` returns the file's own ordinals untouched, for callers with
    no log to resolve against or that want to see exactly what was written.
    """
    p = Path(path or path_for(layer))
    if not p.exists():
        return Document(layer=layer, log=str(config.working_log()))
    data = json.loads(p.read_text(encoding="utf-8")) or {}
    doc = Document(
        layer=str(data.get("layer") or layer),
        scenes=[Scene.from_json(s) for s in (data.get("scenes") or [])],
        log=str(data.get("log") or config.working_log()),
        updated=str(data.get("updated") or ""),
        voice=Voice.from_json(data.get("voice")),
    )
    if not bind:
        return doc
    if log is None:
        try:
            log = loader.load(config.working_log())
        except (FileNotFoundError, OSError):
            log = None
    if log is not None:
        for scene in doc.scenes:
            scene.bind(log)
        doc.scenes.sort(key=lambda s: (s.anchor.start, s.id))
    return doc


def save(doc: Document, path: Path | None = None) -> Path:
    p = Path(path or path_for(doc.layer))
    p.parent.mkdir(parents=True, exist_ok=True)
    doc.updated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    body = {
        "schema": SCHEMA,
        "layer": doc.layer,
        "log": doc.log or str(config.working_log()),
        "updated": doc.updated,
        "voice": doc.voice.to_json(),
        "scenes": [
            s.to_json()
            for s in sorted(doc.scenes, key=lambda s: (s.anchor.start, s.id))
        ],
    }
    p.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return p


def export_text(doc: Document, *, approved_only: bool = True) -> str:
    """Build artifact: prose only, no ids, no anchors, no provenance."""
    parts = []
    for scene in doc.scenes:
        if approved_only and scene.status != "approved":
            continue
        body = scene.text()
        if not body:
            continue
        if scene.title:
            parts.append(f"# {scene.title}\n\n{body}")
        else:
            parts.append(body)
    return "\n\n".join(parts) + ("\n" if parts else "")

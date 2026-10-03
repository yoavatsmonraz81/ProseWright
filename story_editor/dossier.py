"""Character case files — dated dossier entries pinned to story-time.

Phase 5 / Author Studio: a bible is atemporal; a dossier supplies the missing
axis so checks judge a scene against who the character was *at that point*.

Entries: state | relationship | secret | capability.
Open items capture contradictions for a human to close (wrong earlier entry /
arc change / prose bug) — not auto-fixed.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config, identity as identity_mod, loader
from .loader import Log

ENTRY_KINDS = ("state", "relationship", "secret", "capability")
OPEN_RESOLUTIONS = ("wrong_earlier", "arc_change", "prose_bug", "")


@dataclass
class DossierEntry:
    id: str
    kind: str
    text: str
    from_uid: str | None = None
    to_uid: str | None = None
    # Cached ordinals for display; uids are authoritative on disk.
    from_msg: int | None = None
    to_msg: int | None = None
    created: str = ""
    source: str = "hand"  # hand | put_on_file | seed

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> DossierEntry:
        kind = str(d.get("kind") or "state")
        if kind not in ENTRY_KINDS:
            kind = "state"
        return cls(
            id=str(d.get("id") or uuid.uuid4().hex[:12]),
            kind=kind,
            text=str(d.get("text") or "").strip(),
            from_uid=d.get("from_uid"),
            to_uid=d.get("to_uid"),
            from_msg=d.get("from_msg"),
            to_msg=d.get("to_msg"),
            created=str(d.get("created") or ""),
            source=str(d.get("source") or "hand"),
        )


@dataclass
class OpenItem:
    id: str
    entry_a: str
    entry_b: str
    note: str
    status: str = "open"  # open | closed
    resolution: str = ""
    created: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> OpenItem:
        return cls(
            id=str(d.get("id") or uuid.uuid4().hex[:12]),
            entry_a=str(d.get("entry_a") or ""),
            entry_b=str(d.get("entry_b") or ""),
            note=str(d.get("note") or ""),
            status=str(d.get("status") or "open"),
            resolution=str(d.get("resolution") or ""),
            created=str(d.get("created") or ""),
        )


@dataclass
class Dossier:
    character: str
    label: str = ""
    portrait: str | None = None  # filename under canon/portraits or ST
    entries: list[DossierEntry] = field(default_factory=list)
    open_items: list[OpenItem] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": "story-editor/dossier@1",
            "character": self.character,
            "label": self.label,
            "portrait": self.portrait,
            "entries": [e.to_json() for e in self.entries],
            "open_items": [o.to_json() for o in self.open_items],
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Dossier:
        return cls(
            character=str(d.get("character") or ""),
            label=str(d.get("label") or ""),
            portrait=d.get("portrait"),
            entries=[DossierEntry.from_json(e) for e in (d.get("entries") or [])],
            open_items=[OpenItem.from_json(o) for o in (d.get("open_items") or [])],
        )


def dossiers_dir() -> Path:
    return Path(getattr(config, "DOSSIERS_DIR", Path(config.CANON_DIR) / "dossiers"))


def dossier_path(character: str) -> Path:
    key = _slug(character)
    return dossiers_dir() / f"{key}.json"


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    return s or "unknown"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def list_characters() -> list[str]:
    root = dossiers_dir()
    if not root.exists():
        return []
    return sorted(p.stem for p in root.glob("*.json"))


def load(character: str) -> Dossier | None:
    path = dossier_path(character)
    if not path.exists():
        # Also try slug match against listed files
        key = _slug(character)
        for p in dossiers_dir().glob("*.json") if dossiers_dir().exists() else []:
            if p.stem == key:
                path = p
                break
        else:
            return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    d = Dossier.from_json(data)
    if not d.character:
        d.character = character
    return d


def save(dossier: Dossier) -> Path:
    root = dossiers_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = dossier_path(dossier.character)
    path.write_text(
        json.dumps(dossier.to_json(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _entry_ceiling(entry: DossierEntry) -> int | None:
    """Latest msg ordinal this entry is pinned through (inclusive)."""
    if entry.to_msg is not None:
        return int(entry.to_msg)
    if entry.from_msg is not None:
        return int(entry.from_msg)
    return None


def as_of(dossier: Dossier, msg_id: int) -> list[DossierEntry]:
    """Entries whose pin range has started by ``msg_id`` (story-time projection).

    An entry with no pin is treated as always-on background brief.
    An entry pinned from F..T is visible when ``msg_id >= F`` (and if T set,
    still listed — later contradictions open items rather than hiding history).
    """
    out: list[DossierEntry] = []
    for e in dossier.entries:
        if not e.text:
            continue
        start = e.from_msg
        if start is not None and msg_id < int(start):
            continue
        out.append(e)
    return out


def project_brief(dossier: Dossier, msg_id: int, *, max_entries: int = 12) -> str:
    """Compact prompt block: who this character is as of ``msg_id``."""
    entries = as_of(dossier, msg_id)
    if not entries:
        return ""
    # Prefer later pins last so the model sees the current edge.
    entries = sorted(
        entries,
        key=lambda e: (e.to_msg is not None, e.to_msg or e.from_msg or -1),
    )
    lines = [f"DOSSIER {dossier.character} (as of msg {msg_id}):"]
    for e in entries[-max_entries:]:
        lines.append(f"- [{e.kind}] {e.text}")
    return "\n".join(lines)


_NEGATION_PAIRS = (
    (re.compile(r"\bnever\b", re.I), re.compile(r"\balways\b", re.I)),
    (re.compile(r"\bdead\b", re.I), re.compile(r"\balive\b", re.I)),
    (re.compile(r"\bloyal\b", re.I), re.compile(r"\btraitor\b|\bbetray", re.I)),
    (re.compile(r"\bhates?\b", re.I), re.compile(r"\bloves?\b", re.I)),
)


def detect_contradictions(dossier: Dossier) -> list[OpenItem]:
    """Heuristic pair scan among current entries; does not auto-close anything."""
    existing = {
        (min(o.entry_a, o.entry_b), max(o.entry_a, o.entry_b))
        for o in dossier.open_items
        if o.status == "open"
    }
    found: list[OpenItem] = []
    ents = [e for e in dossier.entries if e.text]
    for i, a in enumerate(ents):
        for b in ents[i + 1 :]:
            key = (min(a.id, b.id), max(a.id, b.id))
            if key in existing:
                continue
            hit = False
            for pat_x, pat_y in _NEGATION_PAIRS:
                if (pat_x.search(a.text) and pat_y.search(b.text)) or (
                    pat_y.search(a.text) and pat_x.search(b.text)
                ):
                    hit = True
                    break
            if not hit and a.kind == b.kind:
                # Same-kind near-duplicate with opposing polarity words
                if ("not " in a.text.lower()) != ("not " in b.text.lower()):
                    # shared content words (≥5 chars)
                    wa = {w for w in re.findall(r"[a-z]{5,}", a.text.lower())}
                    wb = {w for w in re.findall(r"[a-z]{5,}", b.text.lower())}
                    if len(wa & wb) >= 2:
                        hit = True
            if hit:
                found.append(
                    OpenItem(
                        id=uuid.uuid4().hex[:12],
                        entry_a=a.id,
                        entry_b=b.id,
                        note=f"Possible contradiction between [{a.kind}] entries",
                        created=_now(),
                    )
                )
    return found


def refresh_open_items(dossier: Dossier) -> list[OpenItem]:
    """Append newly detected contradictions; return the new ones."""
    new = detect_contradictions(dossier)
    dossier.open_items.extend(new)
    return new


def add_entry(
    dossier: Dossier,
    *,
    kind: str,
    text: str,
    from_msg: int | None = None,
    to_msg: int | None = None,
    from_uid: str | None = None,
    to_uid: str | None = None,
    source: str = "hand",
    log: Log | None = None,
) -> DossierEntry:
    if kind not in ENTRY_KINDS:
        raise ValueError(f"kind must be one of {ENTRY_KINDS}")
    text = text.strip()
    if not text:
        raise ValueError("empty dossier entry")
    if log is not None:
        if from_msg is not None and from_uid is None:
            try:
                from_uid = log.get(from_msg).uid
            except Exception:  # noqa: BLE001
                pass
        if to_msg is not None and to_uid is None:
            try:
                to_uid = log.get(to_msg).uid
            except Exception:  # noqa: BLE001
                pass
    entry = DossierEntry(
        id=uuid.uuid4().hex[:12],
        kind=kind,
        text=text,
        from_uid=from_uid,
        to_uid=to_uid,
        from_msg=from_msg,
        to_msg=to_msg,
        created=_now(),
        source=source,
    )
    dossier.entries.append(entry)
    refresh_open_items(dossier)
    return entry


def close_open_item(
    dossier: Dossier, item_id: str, *, resolution: str
) -> OpenItem:
    if resolution not in ("wrong_earlier", "arc_change", "prose_bug"):
        raise ValueError("resolution must be wrong_earlier | arc_change | prose_bug")
    for o in dossier.open_items:
        if o.id == item_id:
            o.status = "closed"
            o.resolution = resolution
            return o
    raise KeyError(f"open item not found: {item_id}")


def remove_entry(dossier: Dossier, entry_id: str) -> DossierEntry:
    """Drop an evidence entry and any open items that cited it."""
    kept: list[DossierEntry] = []
    removed: DossierEntry | None = None
    for e in dossier.entries:
        if e.id == entry_id:
            removed = e
        else:
            kept.append(e)
    if removed is None:
        raise KeyError(f"entry not found: {entry_id}")
    dossier.entries = kept
    dossier.open_items = [
        o
        for o in dossier.open_items
        if o.entry_a != entry_id and o.entry_b != entry_id
    ]
    return removed


@dataclass
class SpanCheck:
    ok: bool
    open_items: list[OpenItem] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "open_items": [o.to_json() for o in self.open_items],
            "notes": list(self.notes),
        }


def check_span(
    log: Log,
    start: int,
    end: int,
    *,
    characters: list[str] | None = None,
) -> SpanCheck:
    """Gate helper for the author loop.

    Fails if any named character has unresolved open items whose entries fall
    inside or before the span ceiling. Also flags hard dossier 'never' facts
    that appear contradicted by span text (substring heuristic).
    """
    if end < start:
        start, end = end, start
    names = characters or list_characters()
    open_hits: list[OpenItem] = []
    notes: list[str] = []
    span_text = " ".join(
        (log.get(i).text or "") for i in range(start, min(end, len(log) - 1) + 1)
        if 0 <= i < len(log)
    ).lower()

    for name in names:
        d = load(name)
        if d is None:
            continue
        for o in d.open_items:
            if o.status != "open":
                continue
            # Open items always block the gate for that character.
            open_hits.append(o)
            notes.append(f"{d.character}: unresolved open item {o.id}: {o.note}")
        for e in as_of(d, end):
            m = re.search(r"\bnever\s+([^.!?]+)", e.text, re.I)
            if not m:
                continue
            fragment = m.group(1).strip().lower()
            # Take a distinctive token from the never-clause
            toks = [t for t in re.findall(r"[a-z]{5,}", fragment) if t not in {
                "never", "about", "their", "would", "should", "could",
            }]
            for tok in toks[:3]:
                # If the dossier says never X and the span asserts X positively
                if tok in span_text and f"not {tok}" not in span_text:
                    # Weak signal — only note when "never" entry also shares another word
                    notes.append(
                        f"{d.character}: span may violate dossier never-clause "
                        f"({e.id}: …{tok}…)"
                    )
                    break

    # Open items fail closed; soft never-clause notes alone do not fail
    # (they feed retry prompts). Hard fail = unresolved open items.
    return SpanCheck(ok=not open_hits, open_items=open_hits, notes=notes)


def summarize_passage_to_entry(
    *,
    character: str,
    passage: str,
    kind: str = "state",
) -> str:
    """LLM helper for 'put on file'. Returns entry text (no persist)."""
    from . import llm

    if kind not in ENTRY_KINDS:
        kind = "state"
    messages = [
        {
            "role": "system",
            "content": (
                "You write police-file dossier bullets for a story character. "
                "One or two concrete sentences. No spoilers beyond the passage. "
                "Ground only in the provided text. Output plain text only."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Character: {character}\nKind: {kind}\n\nPassage:\n{passage}\n\n"
                "Dossier entry:"
            ),
        },
    ]
    raw = llm.chat(messages, temperature=0.2, max_tokens=200)
    return " ".join(raw.strip().split())


# Dossier / display name → SillyTavern card filename stems (read-only), for
# characters whose card file is named differently. Story-specific; none built in.
_ST_PORTRAIT_ALIASES: dict[str, tuple[str, ...]] = {}

_PORTRAIT_EXTS = (".png", ".jpg", ".jpeg", ".webp")


def _st_characters_dir() -> Path | None:
    """The open project's SillyTavern characters folder, or None if it has none."""
    return Path(config.ST_CHARACTERS_DIR) if config.ST_CHARACTERS_DIR else None


def _st_portrait_candidates(character: str) -> list[Path]:
    """ST ``characters/`` card faces — never written, only resolved."""
    root = _st_characters_dir()
    if root is None or not root.is_dir():
        return []
    slug = _slug(character)
    stems: list[str] = []
    stems.extend(_ST_PORTRAIT_ALIASES.get(slug, ()))
    raw = character.strip()
    if raw:
        stems.extend((raw, raw.replace(" ", "_"), raw.replace(" ", "")))
    stems.append(slug)
    # Preserve order, drop empties/dupes.
    seen: set[str] = set()
    ordered: list[str] = []
    for s in stems:
        key = s.casefold()
        if not s or key in seen:
            continue
        seen.add(key)
        ordered.append(s)

    out: list[Path] = []
    for stem in ordered:
        for ext in _PORTRAIT_EXTS:
            out.append(root / f"{stem}{ext}")

    wanted = {_slug(s) for s in ordered}
    wanted.add(slug)
    try:
        for p in root.iterdir():
            if (
                p.is_file()
                and p.suffix.lower() in _PORTRAIT_EXTS
                and _slug(p.stem) in wanted
            ):
                out.append(p)
    except OSError:
        pass
    return out


def _canon_portraits_dir() -> Path:
    return Path(config.CANON_DIR) / "portraits"


def resolve_portrait_ref(ref: str) -> Path | None:
    """Resolve a stored portrait ref (``st:File.png``, ``canon:…``, or bare name)."""
    raw = (ref or "").strip()
    if not raw or raw in ("auto", "none", "-"):
        return None
    source = "auto"
    name = raw
    if ":" in raw:
        source, name = raw.split(":", 1)
        source = source.strip().lower()
        name = name.strip()
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        return None
    if Path(name).name != name:
        return None

    candidates: list[Path] = []
    st_dir = _st_characters_dir()
    if source in ("st", "sillytavern"):
        if st_dir is not None:
            candidates.append(st_dir / name)
    elif source in ("canon", "local"):
        candidates.append(_canon_portraits_dir() / name)
    else:
        # Bare filename: prefer canon override, then ST card.
        candidates.append(_canon_portraits_dir() / name)
        if st_dir is not None:
            candidates.append(st_dir / name)
        if raw != name:
            candidates.append(Path(raw))

    for p in candidates:
        try:
            if p.is_file():
                return p.resolve()
        except OSError:
            continue
    return None


def list_portrait_catalog() -> list[dict[str, str]]:
    """All pickable card faces (canon first, then ST). Read-only paths."""
    items: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(source: str, path: Path) -> None:
        if path.suffix.lower() not in _PORTRAIT_EXTS or not path.is_file():
            return
        ref = f"{source}:{path.name}"
        key = ref.casefold()
        if key in seen:
            return
        seen.add(key)
        items.append({
            "ref": ref,
            "label": path.stem,
            "source": source,
            "filename": path.name,
        })

    canon = _canon_portraits_dir()
    if canon.is_dir():
        for p in sorted(canon.iterdir(), key=lambda x: x.name.casefold()):
            add("canon", p)

    st = _st_characters_dir()
    if st is not None and st.is_dir():
        for p in sorted(st.iterdir(), key=lambda x: x.name.casefold()):
            add("st", p)

    return items


def set_portrait(character: str, ref: str | None) -> Dossier:
    """Pin a portrait ref on the dossier (``None`` / empty = auto alias)."""
    d = load(character)
    if d is None:
        raise FileNotFoundError(f"no dossier for {character}")
    cleaned = (ref or "").strip()
    if cleaned in ("", "auto", "none", "-"):
        d.portrait = None
    else:
        if resolve_portrait_ref(cleaned) is None:
            raise ValueError(f"portrait not found: {cleaned}")
        d.portrait = cleaned
    save(d)
    return d


def import_portrait_file(
    character: str,
    filename: str,
    data: bytes,
) -> Dossier:
    """Copy an uploaded image into ``canon/portraits`` and pin it."""
    if not data:
        raise ValueError("empty image")
    if len(data) > 8 * 1024 * 1024:
        raise ValueError("image too large (max 8 MB)")
    d = load(character)
    if d is None:
        raise FileNotFoundError(f"no dossier for {character}")

    raw_name = Path(filename or "portrait.png").name
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(raw_name).stem).strip("._") or "portrait"
    ext = Path(raw_name).suffix.lower()
    if ext not in _PORTRAIT_EXTS:
        # sniff a few magic headers
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            ext = ".png"
        elif data[:2] == b"\xff\xd8":
            ext = ".jpg"
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            ext = ".webp"
        else:
            raise ValueError("unsupported image type (use png, jpg, or webp)")

    dest_dir = _canon_portraits_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_name = f"{_slug(character)}_{stem}{ext}"
    dest = dest_dir / dest_name
    # Avoid clobbering an unrelated file with the same stem+ext.
    n = 2
    while dest.exists():
        dest_name = f"{_slug(character)}_{stem}_{n}{ext}"
        dest = dest_dir / dest_name
        n += 1
    dest.write_bytes(data)
    d.portrait = f"canon:{dest_name}"
    save(d)
    return d


def resolve_portrait_path(character: str) -> Path | None:
    """Pinned ref, else ``canon/portraits``, else ST card alias; None → monogram."""
    d = load(character)
    if d and d.portrait:
        pinned = resolve_portrait_ref(str(d.portrait))
        if pinned is not None:
            return pinned
    root = _canon_portraits_dir()
    candidates: list[Path] = []
    slug = _slug(character)
    for ext in _PORTRAIT_EXTS:
        candidates.append(root / f"{slug}{ext}")
        candidates.append(root / f"{character}{ext}")
    candidates.extend(_st_portrait_candidates(character))
    for p in candidates:
        try:
            if p.is_file():
                return p.resolve()
        except OSError:
            continue
    return None


def has_portrait(character: str) -> bool:
    return resolve_portrait_path(character) is not None


def portrait_selection(character: str) -> dict[str, Any]:
    """What the Files pane needs to show / cycle the current pick."""
    d = load(character)
    pinned = (d.portrait if d else None) or None
    path = resolve_portrait_path(character)
    mode = "pinned" if pinned else ("auto" if path else "none")
    return {
        "character": character,
        "portrait": pinned,
        "mode": mode,
        "has_portrait": path is not None,
        "resolved": path.name if path else None,
    }


# Keep identity import referenced for future uid backfill helpers.
_ = identity_mod

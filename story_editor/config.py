"""Central configuration. Paths are resolved relative to the project root so the
engine can be run from anywhere.

Prime directive: the WORKING_LOG is a throwaway copy. The engine never touches
the live SillyTavern file. Edits go to WORKING_LOG only, and only after a backup.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# The project home: workspace/, canon/, cards/, worlds/ and project.json (see
# docs/PROJECTS.md). In order: STORY_EDITOR_HOME; the active project in the
# project registry (story_editor/registry.py); a project.json at the repository
# root; the bundled example project.
from . import registry as _registry  # noqa: E402  (stdlib-only; never imports config)

_HOME_ENV = os.environ.get("STORY_EDITOR_HOME")
EXAMPLE_HOME = PROJECT_ROOT / "examples" / "lantern-quay"
_REGISTRY_HOME, REGISTRY_WARNING = (None, "") if _HOME_ENV else _registry.active_home()
if _HOME_ENV:
    HOME_DIR = Path(_HOME_ENV).expanduser().resolve()
    HOME_SOURCE = "env"
elif _REGISTRY_HOME is not None:
    HOME_DIR = _REGISTRY_HOME
    HOME_SOURCE = "registry"
elif (PROJECT_ROOT / "project.json").exists():
    HOME_DIR = PROJECT_ROOT
    HOME_SOURCE = "repo"
else:
    HOME_DIR = EXAMPLE_HOME
    HOME_SOURCE = "example"
IS_ALT_HOME = HOME_DIR != PROJECT_ROOT

WORKSPACE_DIR = HOME_DIR / "workspace"
BACKUP_DIR = WORKSPACE_DIR / "backups"

# The copy we operate on. A project's manifest names its own (see the end of
# this module); this is the default for a home without one.
WORKING_LOG = WORKSPACE_DIR / "story.jsonl"

# OpenAI-compatible chat endpoint. Default is local textgen-webui.
# For OpenRouter (or any hosted OpenAI-compatible API):
#   export STORY_EDITOR_MODEL_PROVIDER=openrouter
#   export STORY_EDITOR_MODEL_API_KEY=sk-or-...   # or OPENROUTER_API_KEY
#   export STORY_EDITOR_MODEL_NAME=anthropic/claude-sonnet-4
# Optional: STORY_EDITOR_MODEL_URL overrides the provider default base.
# Runtime switches (GUI / POST /model) call ``apply_model_settings`` — env is
# only the boot default; the process does not rewrite your shell exports.
_OPENROUTER_BASE = "https://openrouter.ai/api/v1"
_DEFAULT_LOCAL_PORT = 5000
_ENV_LOCAL_PORT = os.environ.get("STORY_EDITOR_MODEL_PORT", "").strip()
try:
    _LOCAL_PORT = int(_ENV_LOCAL_PORT) if _ENV_LOCAL_PORT else _DEFAULT_LOCAL_PORT
except ValueError:
    _LOCAL_PORT = _DEFAULT_LOCAL_PORT
if not 1 <= _LOCAL_PORT <= 65535:
    _LOCAL_PORT = _DEFAULT_LOCAL_PORT
_LOCAL_BASE = f"http://127.0.0.1:{_LOCAL_PORT}/v1"
_LOCAL_MODEL = "gemma-4-31B-it-UD-Q8_K_XL.gguf"
_OPENROUTER_DEFAULT_MODEL = "anthropic/claude-sonnet-4"

_MODEL_PROVIDER = os.environ.get("STORY_EDITOR_MODEL_PROVIDER", "local").strip().lower()
MODEL_PROVIDER = _MODEL_PROVIDER or "local"

_ENV_MODEL_URL = os.environ.get("STORY_EDITOR_MODEL_URL")
_ENV_MODEL_NAME = os.environ.get("STORY_EDITOR_MODEL_NAME")
_ENV_CREATIVE = os.environ.get("STORY_EDITOR_MODEL_CREATIVE")
_ENV_ANALYST = os.environ.get("STORY_EDITOR_MODEL_ANALYST")

if MODEL_PROVIDER == "openrouter":
    MODEL_BASE_URL = _ENV_MODEL_URL or _OPENROUTER_BASE
    MODEL_NAME = _ENV_MODEL_NAME or _OPENROUTER_DEFAULT_MODEL
else:
    MODEL_BASE_URL = _ENV_MODEL_URL or _LOCAL_BASE
    MODEL_NAME = _ENV_MODEL_NAME or _LOCAL_MODEL

# Bearer token for remote providers. Local textgen accepts any/placeholder value.
MODEL_API_KEY = (
    os.environ.get("STORY_EDITOR_MODEL_API_KEY")
    or os.environ.get("OPENROUTER_API_KEY")
    or ""
)

# OpenRouter dashboard attribution (harmless elsewhere; ignored by local servers).
MODEL_HTTP_REFERER = os.environ.get(
    "STORY_EDITOR_MODEL_HTTP_REFERER", "https://github.com/local/story-editor"
)
MODEL_APP_TITLE = os.environ.get(
    "STORY_EDITOR_MODEL_APP_TITLE", "Story Editor"
)

# Optional per-role presets (same endpoint, different loaded model names).
# Set STORY_EDITOR_MODEL_CREATIVE / STORY_EDITOR_MODEL_ANALYST to route tasks.
MODEL_PRESET_CREATIVE = _ENV_CREATIVE or MODEL_NAME
MODEL_PRESET_ANALYST = _ENV_ANALYST or MODEL_NAME

# Remember last model id per provider so the GUI can flip without losing names.
_LAST_MODEL_BY_PROVIDER: dict[str, str] = {
    "local": MODEL_NAME if MODEL_PROVIDER == "local" else _LOCAL_MODEL,
    "openrouter": MODEL_NAME if MODEL_PROVIDER == "openrouter" else _OPENROUTER_DEFAULT_MODEL,
}
_LAST_URL_BY_PROVIDER: dict[str, str] = {
    "local": MODEL_BASE_URL if MODEL_PROVIDER == "local" else _LOCAL_BASE,
    "openrouter": MODEL_BASE_URL if MODEL_PROVIDER == "openrouter" else _OPENROUTER_BASE,
}


def apply_model_settings(
    *,
    provider: str | None = None,
    model_name: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    local_port: int | str | None = None,
) -> dict:
    """Mutate the live model endpoint for this process.

    Returns a public snapshot (never includes the raw API key). Raises
    ``ValueError`` for unknown providers or OpenRouter without a key.
    """
    global MODEL_PROVIDER, MODEL_BASE_URL, MODEL_NAME, MODEL_API_KEY
    global MODEL_PRESET_CREATIVE, MODEL_PRESET_ANALYST

    prev = MODEL_PROVIDER
    _LAST_MODEL_BY_PROVIDER[prev] = MODEL_NAME
    _LAST_URL_BY_PROVIDER[prev] = MODEL_BASE_URL

    next_provider = (provider or MODEL_PROVIDER).strip().lower()
    if next_provider not in ("local", "openrouter"):
        raise ValueError("provider must be 'local' or 'openrouter'")
    parsed_port: int | None = None
    if local_port is not None and str(local_port).strip():
        if next_provider != "local":
            raise ValueError("local_port can only be set for the local provider")
        try:
            parsed_port = int(str(local_port).strip())
        except ValueError as exc:
            raise ValueError("local port must be a number from 1 to 65535") from exc
        if not 1 <= parsed_port <= 65535:
            raise ValueError("local port must be a number from 1 to 65535")

    if api_key is not None:
        MODEL_API_KEY = api_key.strip()

    if next_provider == "openrouter" and not (MODEL_API_KEY or "").strip():
        raise ValueError(
            "OpenRouter needs an API key — set it here or via "
            "STORY_EDITOR_MODEL_API_KEY / OPENROUTER_API_KEY before switching."
        )

    if model_name is not None and model_name.strip():
        next_model = model_name.strip()
    elif next_provider == prev:
        next_model = MODEL_NAME
    else:
        next_model = _LAST_MODEL_BY_PROVIDER.get(
            next_provider,
            _OPENROUTER_DEFAULT_MODEL if next_provider == "openrouter" else _LOCAL_MODEL,
        )

    if base_url is not None and base_url.strip():
        next_url = base_url.strip().rstrip("/")
    elif next_provider == prev:
        next_url = MODEL_BASE_URL
    else:
        next_url = _LAST_URL_BY_PROVIDER.get(
            next_provider,
            _OPENROUTER_BASE if next_provider == "openrouter" else _LOCAL_BASE,
        )
        # If the remembered URL is for the wrong host family, fall back to default.
        if next_provider == "openrouter" and "openrouter.ai" not in next_url:
            next_url = _OPENROUTER_BASE
        if next_provider == "local" and "openrouter.ai" in next_url:
            next_url = _LOCAL_BASE

    if parsed_port is not None:
        parts = urlsplit(next_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("local base URL must be an http(s) URL with a host")
        host = parts.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        next_url = urlunsplit((
            parts.scheme,
            f"{host}:{parsed_port}",
            parts.path or "/v1",
            parts.query,
            parts.fragment,
        )).rstrip("/")

    MODEL_PROVIDER = next_provider
    MODEL_NAME = next_model
    MODEL_BASE_URL = next_url
    _LAST_MODEL_BY_PROVIDER[next_provider] = next_model
    _LAST_URL_BY_PROVIDER[next_provider] = next_url

    if not _ENV_CREATIVE:
        MODEL_PRESET_CREATIVE = MODEL_NAME
    if not _ENV_ANALYST:
        MODEL_PRESET_ANALYST = MODEL_NAME

    return model_settings_public()


def model_settings_public() -> dict:
    """Safe view of the live model config (no secret material)."""
    parts = urlsplit(MODEL_BASE_URL)
    active_local_port = parts.port if MODEL_PROVIDER == "local" else None
    return {
        "provider": MODEL_PROVIDER,
        "model": MODEL_NAME,
        "base_url": MODEL_BASE_URL,
        "local_port": active_local_port,
        "api_key_set": bool((MODEL_API_KEY or "").strip()),
        "presets": {
            "creative": MODEL_PRESET_CREATIVE,
            "analyst": MODEL_PRESET_ANALYST,
        },
        "providers": ["local", "openrouter"],
        "defaults": {
            "local": {"model": _LOCAL_MODEL, "base_url": _LOCAL_BASE},
            "openrouter": {
                "model": _OPENROUTER_DEFAULT_MODEL,
                "base_url": _OPENROUTER_BASE,
            },
        },
    }


def sync_local_model_from_api(
    available: list[str] | None = None,
    *,
    force: bool = False,
) -> dict:
    """Align ``MODEL_NAME`` with what the local OpenAI-compatible server reports.

    Local textgen (and most llama.cpp servers) serve whatever file is loaded;
    the ``model`` field in chat requests is often ignored. Our boot default
    (``_LOCAL_MODEL``) can therefore claim gemma-4 while MeroMero is actually
    loaded. When the configured name is missing from ``GET /models``, adopt the
    first (usually only) id the API advertises.

    Explicit ``STORY_EDITOR_MODEL_NAME`` still wins unless ``force`` or the name
    is not in the live list.
    """
    global MODEL_NAME, MODEL_PRESET_CREATIVE, MODEL_PRESET_ANALYST

    if MODEL_PROVIDER != "local":
        return {
            "synced": False,
            "reason": "not_local",
            "model": MODEL_NAME,
            "available": list(available or []),
        }

    names = list(available or [])
    if not names:
        return {
            "synced": False,
            "reason": "no_models",
            "model": MODEL_NAME,
            "available": [],
        }

    if MODEL_NAME in names and not force:
        return {
            "synced": False,
            "reason": "already_current",
            "model": MODEL_NAME,
            "available": names,
        }

    # Env pin: keep it if the server still lists that id (unless forced).
    if _ENV_MODEL_NAME and _ENV_MODEL_NAME in names and not force:
        if MODEL_NAME != _ENV_MODEL_NAME:
            MODEL_NAME = _ENV_MODEL_NAME
            if not _ENV_CREATIVE:
                MODEL_PRESET_CREATIVE = MODEL_NAME
            if not _ENV_ANALYST:
                MODEL_PRESET_ANALYST = MODEL_NAME
            _LAST_MODEL_BY_PROVIDER["local"] = MODEL_NAME
        return {
            "synced": MODEL_NAME == _ENV_MODEL_NAME,
            "reason": "env_pin",
            "model": MODEL_NAME,
            "available": names,
        }

    chosen = names[0]
    prev = MODEL_NAME
    MODEL_NAME = chosen
    if not _ENV_CREATIVE:
        MODEL_PRESET_CREATIVE = MODEL_NAME
    if not _ENV_ANALYST:
        MODEL_PRESET_ANALYST = MODEL_NAME
    _LAST_MODEL_BY_PROVIDER["local"] = MODEL_NAME
    return {
        "synced": True,
        "reason": "adopted_loaded",
        "model": MODEL_NAME,
        "previous": prev,
        "available": names,
    }

# Task → preset bucket. ``llm.chat(..., task="sweep")`` picks analyst unless overridden.
TASK_MODEL_PRESET: dict[str, str] = {
    "restyle": "creative",
    "retune": "creative",
    "inject": "creative",
    "novelize": "creative",
    "attribution_propose": "analyst",
    "sweep": "analyst",
    "proofread": "analyst",
    "canon_check": "analyst",
    "canon_draft": "analyst",
    "canon_test": "analyst",
    "structure": "analyst",
    "ask": "analyst",
    "weed": "creative",
    "copyedit": "analyst",
    "stamps": "analyst",
}


def model_for_task(task: str | None, *, override: str | None = None) -> str:
    """Resolve OpenAI ``model`` id for a task bucket or explicit override."""
    if override:
        return override
    if not task:
        return MODEL_NAME
    bucket = TASK_MODEL_PRESET.get(task, "creative")
    if bucket == "analyst":
        return MODEL_PRESET_ANALYST
    return MODEL_PRESET_CREATIVE

# Per-project asset roots (relocated by STORY_EDITOR_HOME; default unchanged).
CANON_DIR = HOME_DIR / "canon"
CARDS_DIR = HOME_DIR / "cards"
# Character case files (Phase 5 / Author Studio). Dated entries pinned to uids.
DOSSIERS_DIR = CANON_DIR / "dossiers"
# Author Studio mini-spine seed (experimental generative track).
MINI_SPINE = HOME_DIR / "mini_spine.json"

# Phase 1.5 structure inference: a proposed beat spine (written by
# `structure derive`, reviewed, then committed to DERIVED_SPINE). Same
# propose-then-approve discipline as every operator - nothing becomes the
# working spine until the director signs off.
PENDING_SPINE = WORKSPACE_DIR / "pending_spine.json"
PENDING_SPINE_MD = WORKSPACE_DIR / "pending_spine.md"
PENDING_SPINE_CRITIQUE = WORKSPACE_DIR / "pending_spine_critique.md"
DERIVED_SPINE = WORKSPACE_DIR / "derived_spine.json"
# Director-approved manuscript structure.  When this file is canon-locked it
# supersedes the generated derived spine for the working log, while the latter
# remains on disk as provenance for the earlier structure pass.
CANON_EPISODE_MAP = WORKSPACE_DIR / "director_chapter_map.json"

# Cached scene cards (faithful per-scene synopses). A one-time LLM map pass turns
# each scene into 1-2 grounded sentences so beat derivation reasons over real
# summaries instead of head/tail fragments. Keyed by a signature of the scene
# boundaries, so it auto-invalidates when the log changes.
SCENE_CARDS = WORKSPACE_DIR / "scene_cards.json"

# Phase 2 transform operators (restyle / retune / inject). A proposed edit-set is
# a list of per-message before/after rewrites, held pending until the director
# approves - same propose-then-approve discipline as spines.
PENDING_EDITS = WORKSPACE_DIR / "pending_edits.json"
PENDING_EDITS_MD = WORKSPACE_DIR / "pending_edits.md"

# Inferred chronicle (date / time / location) per message uid. Headers in the
# log remain the source of truth when present; this sidecar covers the gaps.
STAMPS = WORKSPACE_DIR / "stamps.json"

# Phase 3 propagation sweep. After a commit the operator writes a small context
# file recording what changed so `sweep --last` can re-use it without the user
# having to remember msg_ids.
LAST_SWEEP_CONTEXT = WORKSPACE_DIR / "last_sweep_context.json"
LAST_SWEEP_REPORT = WORKSPACE_DIR / "last_sweep_report.md"

# Phase 3 extension — director-labelled consistency judgments. Each entry is one
# (candidate_passage, llm_verdict, director_action) triple written after the
# director acts on a cascade cycle. Synthetic perturbation data can be added later
# with source="synthetic". The file is JSONL, never overwritten — always appended.
CONSISTENCY_LABELS = WORKSPACE_DIR / "consistency_labels.jsonl"

# Phase 3.5 — upstream proof-reader. The report is the markdown output of a
# proofread run. The constraints file holds "do not invent X/Y/Z" items from the
# last proofread that contained CONTRADICTS verdicts, ready for re-roll.
LAST_PROOFREAD_REPORT = WORKSPACE_DIR / "last_proofread_report.md"
REROLL_CONSTRAINTS = WORKSPACE_DIR / "reroll_constraints.json"

# Phase 6 — append-only edit changelog (every commit + undo).
EDIT_HISTORY = WORKSPACE_DIR / "edit_history.jsonl"

# Novel editor — the derived layer. The manuscript is the novelized log, in
# prose form. It is a one-way derivation: it reads from the log and is NEVER
# written back toward it, which is what keeps the ST chat file safe from a
# novelization.
MANUSCRIPT = WORKSPACE_DIR / "manuscript.json"
PROSE_REVIEW_QUEUE = WORKSPACE_DIR / "prose_review_queue.json"

# Built UI bundle, served at /app by `serve`. Committed so running the editor
# never requires node — only rebuilding it does.
# Override with STORY_EDITOR_APP_DIR for Author Studio (or any alt UI bundle).
_APP_ENV = os.environ.get("STORY_EDITOR_APP_DIR")
APP_DIR = (
    Path(_APP_ENV).expanduser().resolve()
    if _APP_ENV
    else PROJECT_ROOT / "app" / "dist"
)

# Raw model replies an operator could not parse as JSON, kept so the error can
# name a file instead of a decoder offset. Written only when every attempt failed.
BAD_REPLIES_DIR = WORKSPACE_DIR / "bad_replies"

# Cached authored-vs-derived spine alignment (the LLM audit behind `[validate]`).
# Cached because the audit is a slow model call and the Spine tab must render
# instantly on open — a stale verdict with its timestamp beats an empty pane.
SPINE_ALIGNMENT = WORKSPACE_DIR / "spine_alignment.json"

# User-imported typefaces. Gitignored: the typewriter faces worth wanting are
# mostly personal-use-only, so they live on the machine that licensed them and
# never in the repo. `fonts.json` describes what is there (family, role, licence).
FONTS_DIR = WORKSPACE_DIR / "fonts"
FONTS_JSON = FONTS_DIR / "fonts.json"

# Phase 5 — canon layer. Per-character bibles for the main cast (channel /
# bias_lens / voice_rules / forbidden_phrasings / seed_queries, plus role,
# canonical_facts, aesthetic_rules and relationship_notes). Stored in canon/
# (not cards/) to keep them separate from SillyTavern card assets.
CHARACTERS_JSON = CANON_DIR / "characters.json"
# Alias for Author Studio seed path (same as MINI_SPINE).
AUTHOR_SEED = MINI_SPINE
AUTHOR_PROGRESS = WORKSPACE_DIR / "author_progress.json"
# Per-speaker include/exclude msg_ids for ``canon draft`` (card name ≠ POV character).
BIBLE_SOURCES = CANON_DIR / "bible_sources.json"
# Per-msg_id voice labels (card slot ≠ character voiced). See ``canon attribute``.
VOICE_ATTRIBUTION = CANON_DIR / "voice_attribution.json"
# Machine-phrase list for the weed pass (load-bearing and kin). Author-editable.
WEEDS = CANON_DIR / "weeds.json"
# Project-specific authorship/source selection for the weed pass.  This is
# separate from narrative voice: a human may write in another character's role.
WEED_SCOPE = CANON_DIR / "weed_scope.json"

# Optional per-project STORY CONTEXT primer, read by canon.story_primer() and
# fed into the transform and structure-derive prompts as comprehension grounding.
STORY_PRIMER_FILE = CANON_DIR / "story_primer.txt"

# Phase 1 index: one SQLite database per log, sat next to the working copy.
# The embedder is local & offline (sentence-transformers / BAAI/bge-small-en-v1.5).
EMBED_MODEL_NAME = os.environ.get(
    "STORY_EDITOR_EMBED_MODEL", "BAAI/bge-small-en-v1.5"
)
EMBED_DIM = 384  # bge-small dimensionality; tightly coupled to EMBED_MODEL_NAME

# World-info ("lorebook") files the editor consults at generation time: the
# manifest's list, or every worlds/*.json in the home.
_worlds = HOME_DIR / "worlds"
LOREBOOK_PATHS: list[Path] = sorted(_worlds.glob("*.json")) if _worlds.exists() else []


def working_log() -> Path:
    return Path(os.environ.get("STORY_EDITOR_LOG", str(WORKING_LOG)))


def log_key(path) -> str:
    """A machine-independent name for a log path.

    The project lives at different absolute paths on different machines
    (``~/work/story-editor`` here, ``~/Desktop/story-editor`` there),
    and several files record which log they belong to. Comparing those records
    by absolute path makes every synced record look foreign. Paths inside the
    project collapse to their project-relative form; a path recorded on another
    machine is recognised by its ``story-editor/`` segment.
    """
    raw = str(path or "")
    if not raw:
        return ""
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = HOME_DIR / p
    try:
        return p.resolve().relative_to(HOME_DIR).as_posix()
    except ValueError:
        pass
    marker = "/story-editor/"
    if marker in p.as_posix():
        return p.as_posix().rsplit(marker, 1)[1]
    return p.resolve().as_posix()


def same_log(recorded, path) -> bool:
    """Whether a recorded log path names the same log as ``path``."""
    return bool(recorded) and log_key(recorded) == log_key(path)


def index_db_for(log_path) -> Path:
    """One index db per log, sat next to the working copy:
    `workspace/story.jsonl` -> `workspace/story.index.sqlite3`.
    """
    p = Path(log_path)
    return p.with_name(p.stem + ".index.sqlite3")


# Phase 6 — local HTTP API for thin clients (ST extension, scripts).
API_HOST = os.environ.get("STORY_EDITOR_API_HOST", "127.0.0.1")
API_PORT = int(os.environ.get("STORY_EDITOR_API_PORT", "8765"))

# Novelize: how many cleaned source characters one model pass may hold. Longer
# scenes are split into contiguous chunks under this ceiling. Raise it if your
# local context/output budget can finish a bigger chunk; lower it if passes
# truncate. Override per call via CLI/API/UI without restarting.
NOVELIZE_MAX_SPAN_CHARS = int(
    os.environ.get("STORY_EDITOR_NOVELIZE_MAX_SPAN_CHARS", "24000")
)



# --------------------------------------------------------------------------- #
# Per-project integrations (story_editor/project.py).
#
# A home with project.json gets exactly the integrations its manifest names;
# a home without one gets none. Nothing falls back to another project's chat,
# spine file, character folder or Drive folder.
# --------------------------------------------------------------------------- #
from . import project as _project  # noqa: E402  (config must be complete first)

PROJECT = _project.load(HOME_DIR)
PROJECT_ID = PROJECT.id if PROJECT is not None else HOME_DIR.name
PROJECT_TITLE = PROJECT.title if PROJECT is not None else HOME_DIR.name
# SillyTavern chat the working log came from (push-back target), if any.
SOURCE_LOG: str | None = str(PROJECT.st_chat) if PROJECT is not None and PROJECT.st_chat else None
ST_CHARACTERS_DIR: Path | None = PROJECT.st_characters_dir if PROJECT is not None else None
# The author's own spine document (.md); Phase 1.5 'audit' compares it with the prose.
SPINE_MD: Path | None = PROJECT.authored_spine_md if PROJECT is not None else None
AUTHORED_SPINE_MD = SPINE_MD
# Drive folder for workspace sync, if any.
# A Player-mode play folder this project plays from (the Play view), if any.
PLAY_HOME: Path | None = PROJECT.play_home if PROJECT is not None else None
DRIVE_REMOTE: str | None = PROJECT.drive_remote if PROJECT is not None else None
if PROJECT is not None:
    WORKING_LOG = PROJECT.log
    if PROJECT.lorebooks is not None:
        LOREBOOK_PATHS = list(PROJECT.lorebooks)

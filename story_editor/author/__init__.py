"""Author Studio experimental track — spark, write_scene, beat_loop.

Toe-dip for agentic forward writing. Not a replacement for the novel editor.
"""

from . import seed
from .beat_loop import (
    BeatLoopResult,
    advance_beat,
    beat_label_from_edit_set,
    build_author_story,
    mark_beat_committed,
    on_author_edits_committed,
    redo_author_commit,
    undo_author_commit,
    walk_beats,
)
from .run import ensure_open_run, load_run, new_run, save_run
from .trajectory import build_trajectory, trajectory_jsonl, write_trajectory
from .seed import (
    MiniSpine,
    load_external_seed,
    load_seed,
    save_seed,
    save_seed_and_sync_progress,
    seed_template,
    seed_view,
)
from .spark import (
    AuthorNote,
    clear_spark_draft,
    compile_spark,
    load_spark_draft,
    manual_spark_template,
    order_speakers,
    save_spark_draft,
    spark_from_text,
)
from .write_scene import WriteResult, write_scene

__all__ = [
    "AuthorNote",
    "BeatLoopResult",
    "MiniSpine",
    "WriteResult",
    "advance_beat",
    "beat_label_from_edit_set",
    "build_author_story",
    "build_trajectory",
    "clear_spark_draft",
    "compile_spark",
    "ensure_open_run",
    "load_external_seed",
    "load_run",
    "load_seed",
    "load_spark_draft",
    "manual_spark_template",
    "mark_beat_committed",
    "new_run",
    "on_author_edits_committed",
    "order_speakers",
    "redo_author_commit",
    "save_run",
    "save_seed",
    "save_seed_and_sync_progress",
    "save_spark_draft",
    "seed",
    "seed_template",
    "seed_view",
    "spark_from_text",
    "trajectory_jsonl",
    "undo_author_commit",
    "walk_beats",
    "write_scene",
    "write_trajectory",
]

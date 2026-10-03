"""The story primer: comprehension context for the project's own story.

Transform and structure-derive prompts open with a short primer (setting, the
central relationships, the cast by role) so the model reads the scenes in the
right frame. It must not contain the plot architecture or anything resembling a
beat list; that would let the beat deriver cheat instead of reading the prose.

The primer is per project: ``canon/story_primer.txt`` in the project home. A
project without one gets no primer; the engine carries no story of its own.
"""

from __future__ import annotations

from . import config


def story_primer() -> str:
    """The project's ``canon/story_primer.txt``, or "" when it has none."""
    primer_file = getattr(config, "STORY_PRIMER_FILE", None)
    if primer_file is not None and primer_file.exists():
        return primer_file.read_text(encoding="utf-8").strip()
    return ""

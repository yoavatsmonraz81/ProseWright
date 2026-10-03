import pytest

from story_editor.structure import _extract_json_array


def test_extract_json_array_from_fence() -> None:
    raw = 'Here you go:\n```json\n[{"first_scene":0,"last_scene":1,"title":"A","why":"x"}]\n```'
    assert len(_extract_json_array(raw)) == 1


def test_extract_json_array_truncated_raises() -> None:
    raw = '[{"first_scene":0,"last_scene":1,"title":"A","why":"x"'
    with pytest.raises(ValueError, match="truncated"):
        _extract_json_array(raw)

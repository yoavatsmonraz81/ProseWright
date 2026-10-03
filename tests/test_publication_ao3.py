from story_editor import manuscript as ms, publication


def _doc() -> ms.Document:
    doc = ms.Document()
    doc.upsert(ms.Scene(
        id="sc-prologue", anchor=ms.Anchor(start=-1, end=-1), title="Prologue",
        blocks=[ms.Block(id="p0", text="Front matter stays out.")],
    ))
    doc.upsert(ms.Scene(
        id="sc-a", anchor=ms.Anchor(start=0, end=0), title="Hall",
        blocks=[
            ms.Block(id="a1", text="She said *never* & meant **it** <quietly>."),
            ms.Block(id="a2", text="First line.\nSecond line."),
            ms.Block(id="a3", text="-------------"),
            ms.Block(id="a4", text="After the break."),
        ],
    ))
    doc.upsert(ms.Scene(
        id="sc-b", anchor=ms.Anchor(start=1, end=1), title="Study",
        blocks=[ms.Block(id="b1", text="Next scene.")],
    ))
    return doc


def test_ao3_html_is_paragraphs_emphasis_and_scene_rules():
    html = publication.ao3_chapter_html(_doc(), {"id": 0, "title": "The Late Ship", "start": 0, "end": 1})

    assert html == (
        "<p>She said <em>never</em> &amp; meant <strong>it</strong> &lt;quietly&gt;.</p>\n"
        "<p>First line.</p>\n"
        "<p>Second line.</p>\n"
        "<hr />\n"
        "<p>After the break.</p>\n"
        "<hr />\n"
        "<p>Next scene.</p>\n"
    )

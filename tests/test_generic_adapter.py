from pathlib import Path

from storyindex.adapters.generic_adapter import GenericAdapter


def test_filename_fallback_title_and_defaults():
    adapter = GenericAdapter(Path("."))
    fields = adapter.extract("Just plain text.\n\nSecond paragraph.", relpath="raw/my_old_story-final.txt")
    assert fields.title == "My Old Story Final"
    assert fields.author == "Unknown"
    assert fields.tags == ()
    assert fields.body_text == "Just plain text.\n\nSecond paragraph."


def test_regex_extraction_and_batch_tags():
    adapter = GenericAdapter(Path("."), config={
        "title_regex": r"<title>(.*?)</title>",
        "author_regex": r"by ([^\n<]+)",
        "tags": ["imported"],
        "tags_regex": r"Tags: ([^\n<]+)",
        "strip_html": True,
    })
    html_text = (
        "<title>The Example</title>\nby Jane Doe\nTags: adventure, mystery\n"
        "<p>Once upon a time...</p>\n\n<p>The end.</p>"
    )
    fields = adapter.extract(html_text, relpath="stories/the-example.html")
    assert fields.title == "The Example"
    assert fields.author == "Jane Doe"
    assert fields.tags == ("imported", "adventure", "mystery")
    assert "<p>" not in fields.body_text
    assert "Jane Doe" not in fields.body_text
    assert "Once upon a time..." in fields.body_text


def test_keep_header_in_body_opts_out_of_trimming():
    adapter = GenericAdapter(Path("."), config={
        "title_regex": r"<title>(.*?)</title>",
        "keep_header_in_body": True,
    })
    fields = adapter.extract("<title>T</title>\nbody here", relpath="x.txt")
    assert "<title>T</title>" in fields.body_text


def test_exclude_globs():
    adapter = GenericAdapter(Path("."), config={"exclude_globs": ["**/index.html"]})
    assert adapter.matches("stories/index.html")
    assert not adapter.is_story_page("stories/index.html")
    assert adapter.is_story_page("stories/chapter.html")


def test_group_key_and_part_index_are_one_file_per_story():
    adapter = GenericAdapter(Path("."))
    assert adapter.group_key("a/b.txt") == "a/b.txt"
    assert adapter.part_index("a/b.txt") == 0

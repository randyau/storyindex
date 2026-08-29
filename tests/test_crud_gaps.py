from storyindex import db, libraries


def test_list_removed_stories(conn, make_sig):
    db.upsert_story(conn, make_sig("s1", title="A"))
    db.upsert_story(conn, make_sig("s2", title="B"))
    db.set_story_status(conn, "s2", "removed")
    conn.commit()

    removed = db.list_removed_stories(conn)
    assert [r["id"] for r in removed] == ["s2"]
    assert removed[0]["removed_at"] is not None


def test_create_manual_story(conn):
    db.create_manual_story(conn, "manual-1", "My Story", "Me", "Body text.", "2026-01-01T00:00:00Z")
    conn.commit()
    story = db.get_story(conn, "manual-1")
    assert story["title"] == "My Story"
    assert story["author"] == "Me"
    assert story["body_text"] == "Body text."
    assert story["group_id"] == "manual-1"


def test_stories_for_author_groups_multipart_story_into_one_row(conn, make_sig):
    sig1 = make_sig("s1", title="Part 1", author="Jane")
    db.upsert_story(conn, sig1)
    sig2 = make_sig("s2", title="Part 2", author="Jane")
    sig2.group_id = "s1"
    sig2.part_index = 1
    db.upsert_story(conn, sig2)
    sig3 = make_sig("s3", title="Other", author="Bob")
    db.upsert_story(conn, sig3)
    conn.commit()

    results = db.stories_for_author(conn, "Jane")
    assert [r["title"] for r in results] == ["Part 1"]
    assert results[0]["part_count"] == 2


def test_delete_tag_removes_links_but_keeps_story(conn, make_sig):
    db.upsert_story(conn, make_sig("s1"))
    tag_id = db.get_or_create_tag(conn, "mystery", "2026-01-01T00:00:00Z")
    db.link_story_tag(conn, "s1", tag_id, source="human")
    conn.commit()

    db.delete_tag(conn, tag_id)
    conn.commit()

    assert db.get_tag(conn, tag_id) is None
    assert db.tags_for_story(conn, "s1") == []
    assert db.get_story(conn, "s1") is not None


def test_libraries_unregister_and_rename(tmp_path):
    path = tmp_path / "libs.json"
    libraries.register("a", "/x/a.sqlite", path)
    libraries.register("b", "/x/b.sqlite", path)
    libraries.set_active("a", path)

    libraries.rename_library("a", "renamed-a", path)
    data = libraries.load(path)
    assert "a" not in data["libraries"]
    assert data["libraries"]["renamed-a"] == "/x/a.sqlite"
    assert data["active"] == "renamed-a"

    libraries.unregister("renamed-a", path)
    data = libraries.load(path)
    assert "renamed-a" not in data["libraries"]
    assert data["active"] == "b"  # fell back to the remaining library

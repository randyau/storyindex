from storyindex import db


def test_stories_by_author_excludes_self_and_other_authors(conn, make_sig):
    db.upsert_story(conn, make_sig("s1", title="First", author="Jane"))
    sig2 = make_sig("s2", title="Second", author="Jane")
    sig2.group_id = "g2"
    db.upsert_story(conn, sig2)
    sig3 = make_sig("s3", title="Third", author="Other")
    sig3.group_id = "g3"
    db.upsert_story(conn, sig3)
    conn.commit()

    results = db.stories_by_author(conn, "Jane", exclude_group_id="s1")
    titles = [r["title"] for r in results]
    assert titles == ["Second"]
    # part_index must be selected: story.html's "more by author" list
    # renders it via the shared story_list macro unconditionally.
    assert results[0]["part_index"] == 0


def test_stories_by_author_excludes_removed(conn, make_sig):
    sig2 = make_sig("s2", title="Second", author="Jane")
    sig2.group_id = "g2"
    db.upsert_story(conn, sig2)
    db.set_story_status(conn, "s2", "removed")
    conn.commit()

    results = db.stories_by_author(conn, "Jane", exclude_group_id="g1")
    assert results == []

from storyindex import db


def test_fts_backfills_existing_rows_on_first_ensure(conn, make_sig):
    db.upsert_story(conn, make_sig("s1", title="The Dragon's Hoard", body="A dragon guards gold."))
    conn.commit()
    row = conn.execute("SELECT rowid FROM stories_fts WHERE stories_fts MATCH 'dragon'").fetchone()
    assert row is not None


def test_fts_stays_in_sync_on_update(conn, make_sig):
    db.upsert_story(conn, make_sig("s1", body="A dragon guards gold."))
    conn.commit()
    db.upsert_story(conn, make_sig("s1", body="A phoenix rises from ash."))
    conn.commit()
    assert conn.execute(
        "SELECT 1 FROM stories_fts WHERE stories_fts MATCH 'dragon'"
    ).fetchone() is None
    assert conn.execute(
        "SELECT 1 FROM stories_fts WHERE stories_fts MATCH 'phoenix'"
    ).fetchone() is not None


def test_search_stories_fts_returns_snippet(conn, make_sig):
    db.upsert_story(conn, make_sig("s1", title="The Dragon's Hoard", author="A. Writer",
                                     body="A dragon guards a pile of gold in the mountain."))
    conn.commit()
    results = db.search_stories_fts(conn, "dragon")
    assert len(results) == 1
    assert results[0]["id"] == "s1"
    assert "dragon" in results[0]["snippet"].lower()


def test_search_stories_fts_no_match(conn, make_sig):
    db.upsert_story(conn, make_sig("s1", body="Nothing relevant here."))
    conn.commit()
    assert db.search_stories_fts(conn, "zzzznomatch") == []


def test_search_stories_fts_snippet_escapes_raw_content(conn, make_sig):
    db.upsert_story(conn, make_sig(
        "s1", body="A dragon <script>alert(1)</script> guards gold & treasure."
    ))
    conn.commit()
    results = db.search_stories_fts(conn, "dragon")
    assert "<script>" not in results[0]["snippet"]
    assert "&lt;script&gt;" in results[0]["snippet"]
    assert "<mark>" in results[0]["snippet"]  # our own tag survives escaping


def test_search_stories_fts_excludes_removed(conn, make_sig):
    db.upsert_story(conn, make_sig("s1", body="A dragon guards gold."))
    conn.commit()
    db.set_story_status(conn, "s1", "removed")
    conn.commit()
    assert db.search_stories_fts(conn, "dragon") == []

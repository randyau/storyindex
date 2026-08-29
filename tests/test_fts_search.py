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


def test_search_stories_intersects_include_tags(conn, make_sig):
    db.upsert_story(conn, make_sig("s1", title="A"))
    db.upsert_story(conn, make_sig("s2", title="B"))
    db.add_story_tag_by_name(conn, "s1", "battle of wits", "2026-01-01T00:00:00Z")
    db.add_story_tag_by_name(conn, "s1", "sherlock holmes", "2026-01-01T00:00:00Z")
    db.add_story_tag_by_name(conn, "s2", "battle of wits", "2026-01-01T00:00:00Z")
    conn.commit()

    wits_id = db.get_or_create_tag(conn, "battle of wits", "2026-01-01T00:00:00Z")
    holmes_id = db.get_or_create_tag(conn, "sherlock holmes", "2026-01-01T00:00:00Z")

    results = db.search_stories(conn, include_tag_ids=[wits_id])
    assert {r["id"] for r in results} == {"s1", "s2"}

    results = db.search_stories(conn, include_tag_ids=[wits_id, holmes_id])
    assert {r["id"] for r in results} == {"s1"}


def test_search_stories_excludes_tags(conn, make_sig):
    db.upsert_story(conn, make_sig("s1", title="A"))
    db.upsert_story(conn, make_sig("s2", title="B"))
    db.add_story_tag_by_name(conn, "s1", "battle of wits", "2026-01-01T00:00:00Z")
    db.add_story_tag_by_name(conn, "s1", "sherlock holmes", "2026-01-01T00:00:00Z")
    db.add_story_tag_by_name(conn, "s2", "battle of wits", "2026-01-01T00:00:00Z")
    conn.commit()

    wits_id = db.get_or_create_tag(conn, "battle of wits", "2026-01-01T00:00:00Z")
    holmes_id = db.get_or_create_tag(conn, "sherlock holmes", "2026-01-01T00:00:00Z")

    results = db.search_stories(conn, include_tag_ids=[wits_id], exclude_tag_ids=[holmes_id])
    assert {r["id"] for r in results} == {"s2"}


def test_search_stories_combines_keyword_and_tags(conn, make_sig):
    db.upsert_story(conn, make_sig("s1", title="A", body="A dragon guards gold."))
    db.upsert_story(conn, make_sig("s2", title="B", body="A dragon flies away."))
    db.add_story_tag_by_name(conn, "s1", "hoarding", "2026-01-01T00:00:00Z")
    conn.commit()

    hoarding_id = db.get_or_create_tag(conn, "hoarding", "2026-01-01T00:00:00Z")
    results = db.search_stories(conn, query="dragon", include_tag_ids=[hoarding_id])
    assert {r["id"] for r in results} == {"s1"}
    assert "dragon" in results[0]["snippet"].lower()


def test_search_stories_falls_back_to_part_zero_title(conn, make_sig):
    # Some sites only print a title on a multi-chapter story's first page;
    # later parts land in the DB with title == "". Browsing should show
    # (and sort) those under the story's real title, not blank.
    part0 = make_sig("g1", title="The Long Saga")
    part0.group_id = "g1"
    part0.part_index = 0
    db.upsert_story(conn, part0)

    part1 = make_sig("g1-p2", title="")
    part1.group_id = "g1"
    part1.part_index = 1
    db.upsert_story(conn, part1)
    conn.commit()

    results = db.search_stories(conn)
    assert len(results) == 1
    assert results[0]["id"] == "g1"
    assert results[0]["title"] == "The Long Saga"


def test_search_stories_groups_multipart_story_into_one_row(conn, make_sig):
    for i in range(3):
        p = make_sig(f"g1-{i}", title="Saga" if i == 0 else "")
        p.group_id = "g1"
        p.part_index = i
        db.upsert_story(conn, p)
    db.upsert_story(conn, make_sig("s2", title="Other"))
    conn.commit()

    results = db.search_stories(conn)
    ids = [r["id"] for r in results]
    assert ids.count("g1-0") == 1
    assert "g1-1" not in ids and "g1-2" not in ids
    by_id = {r["id"]: r for r in results}
    assert by_id["g1-0"]["part_count"] == 3
    assert by_id["s2"]["part_count"] == 1


def test_search_stories_tag_filter_matches_if_any_part_has_it(conn, make_sig):
    # Tags are attached per-part, but a filter should match the whole
    # story: a tag on chapter 2 alone must still surface the story (via
    # its representative row) under an include filter, and exclude it
    # under an exclude filter - "does this story have this element
    # anywhere", not "does this specific chapter".
    for i in range(2):
        p = make_sig(f"g1-{i}", title="Saga" if i == 0 else "")
        p.group_id = "g1"
        p.part_index = i
        db.upsert_story(conn, p)
    conn.commit()
    now = "2026-01-01T00:00:00Z"
    tag_id = db.get_or_create_tag(conn, "twist", now)
    db.link_story_tag(conn, "g1-1", tag_id, source="human")
    conn.commit()

    included = db.search_stories(conn, include_tag_ids=[tag_id])
    assert [r["id"] for r in included] == ["g1-0"]

    excluded = db.search_stories(conn, exclude_tag_ids=[tag_id])
    assert excluded == []


def test_search_stories_sorts_ignoring_leading_article(conn, make_sig):
    db.upsert_story(conn, make_sig("s1", title="The Zebra"))
    db.upsert_story(conn, make_sig("s2", title="An Apple"))
    db.upsert_story(conn, make_sig("s3", title="A Banana"))
    db.upsert_story(conn, make_sig("s4", title="Circus"))
    conn.commit()

    results = db.search_stories(conn)
    # displayed titles keep their article; sort order goes by Apple, Banana,
    # Circus, Zebra - not by the leading A/An/The.
    assert [r["title"] for r in results] == ["An Apple", "A Banana", "Circus", "The Zebra"]


def test_search_stories_sort_handles_irregular_spacing_after_article(conn, make_sig):
    # A real title in the library had a doubled space ("THE  CHANGE"),
    # which without an LTRIM after stripping the article left a leading
    # space in the sort key - sorting it before every other title instead
    # of alphabetically under "C".
    db.upsert_story(conn, make_sig("s1", title="THE  CHANGE"))
    db.upsert_story(conn, make_sig("s2", title="Banana"))
    conn.commit()

    results = db.search_stories(conn)
    assert [r["title"] for r in results] == ["Banana", "THE  CHANGE"]

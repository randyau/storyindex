from storyindex import db
from storyindex.app import app


def _client(db_path):
    app.config["DB_PATH"] = db_path
    return app.test_client()


def test_index_search_renders_snippet(tmp_path, make_sig):
    dbpath = tmp_path / "t.sqlite"
    conn = db.connect(dbpath)
    db.upsert_story(conn, make_sig("s1", title="The Dragon's Hoard", body="A dragon guards gold."))
    conn.commit()
    conn.close()

    client = _client(dbpath)
    r = client.get("/?q=dragon")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "<mark>" in body
    assert "The Dragon" in body


def test_index_search_no_results(tmp_path, make_sig):
    dbpath = tmp_path / "t.sqlite"
    conn = db.connect(dbpath)
    db.upsert_story(conn, make_sig("s1", body="Nothing relevant."))
    conn.commit()
    conn.close()

    client = _client(dbpath)
    r = client.get("/?q=zzzznomatch")
    assert r.status_code == 200
    assert "no stories" in r.get_data(as_text=True)


def test_index_tag_cloud_is_capped_with_see_all_link(tmp_path, make_sig, monkeypatch):
    from storyindex import app as app_module
    monkeypatch.setattr(app_module, "TAG_CLOUD_SIZE", 2)

    dbpath = tmp_path / "t.sqlite"
    conn = db.connect(dbpath)
    db.upsert_story(conn, make_sig("s1"))
    for name in ["alpha", "beta", "gamma"]:
        tag_id = db.get_or_create_tag(conn, name, "2026-01-01T00:00:00Z")
        db.link_story_tag(conn, "s1", tag_id, source="human")
    conn.commit()
    conn.close()

    client = _client(dbpath)
    r = client.get("/")
    body = r.get_data(as_text=True)
    assert "browse all tags" in body
    assert "/tags" in body


def test_tags_autocomplete_json(tmp_path, make_sig):
    dbpath = tmp_path / "t.sqlite"
    conn = db.connect(dbpath)
    db.upsert_story(conn, make_sig("s1"))
    for name in ["battle of wits", "battlefield tactics", "sherlock holmes"]:
        tag_id = db.get_or_create_tag(conn, name, "2026-01-01T00:00:00Z")
        db.link_story_tag(conn, "s1", tag_id, source="human")
    conn.commit()
    conn.close()

    client = _client(dbpath)
    r = client.get("/tags/autocomplete.json?q=battl")
    data = r.get_json()
    names = {t["name"] for t in data["tags"]}
    assert names == {"battle of wits", "battlefield tactics"}

    # An empty query still returns a starter list (most-used tags first),
    # so a picker can show options on focus before the user types anything.
    r = client.get("/tags/autocomplete.json")
    empty_q_names = {t["name"] for t in r.get_json()["tags"]}
    assert empty_q_names == {"battle of wits", "battlefield tactics", "sherlock holmes"}


def test_index_add_tag_filter_by_typed_name_resolves_and_redirects(tmp_path, make_sig):
    dbpath = tmp_path / "t.sqlite"
    conn = db.connect(dbpath)
    db.upsert_story(conn, make_sig("s1", title="Wits Story"))
    tag_id = db.get_or_create_tag(conn, "battle of wits", "2026-01-01T00:00:00Z")
    db.link_story_tag(conn, "s1", tag_id, source="human")
    conn.commit()
    conn.close()

    client = _client(dbpath)
    r = client.get("/?add_tag=battle+of+wits")
    assert r.status_code == 302
    assert f"tags={tag_id}" in r.headers["Location"]

    r = client.get(r.headers["Location"])
    assert "Wits Story" in r.get_data(as_text=True)


def test_index_filters_by_include_and_exclude_tags(tmp_path, make_sig):
    dbpath = tmp_path / "t.sqlite"
    conn = db.connect(dbpath)
    db.upsert_story(conn, make_sig("s1", title="Wits Only"))
    db.upsert_story(conn, make_sig("s2", title="Wits And Holmes"))
    db.add_story_tag_by_name(conn, "s1", "battle of wits", "2026-01-01T00:00:00Z")
    db.add_story_tag_by_name(conn, "s2", "battle of wits", "2026-01-01T00:00:00Z")
    db.add_story_tag_by_name(conn, "s2", "sherlock holmes", "2026-01-01T00:00:00Z")
    conn.commit()
    wits_id = db.get_or_create_tag(conn, "battle of wits", "2026-01-01T00:00:00Z")
    holmes_id = db.get_or_create_tag(conn, "sherlock holmes", "2026-01-01T00:00:00Z")
    conn.close()

    client = _client(dbpath)
    r = client.get(f"/?tags={wits_id}&exclude_tags={holmes_id}")
    body = r.get_data(as_text=True)
    assert "Wits Only" in body
    assert "Wits And Holmes" not in body
    assert "not sherlock holmes" in body
    assert "battle of wits" in body

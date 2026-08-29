from storyindex import db
from storyindex.app import app


def _client(tmp_path):
    app.config["DB_PATH"] = tmp_path / "t.sqlite"
    app.config["LIBRARIES_PATH"] = tmp_path / "libs.json"
    return app.test_client()


def test_new_story_form_and_submit(tmp_path):
    client = _client(tmp_path)
    assert client.get("/stories/new").status_code == 200

    r = client.post("/stories/new", data={"title": "Hello", "author": "Me", "body_text": "Once upon a time."})
    assert r.status_code == 302

    conn = db.connect(tmp_path / "t.sqlite")
    story = conn.execute("SELECT title, author FROM stories").fetchone()
    assert story[0] == "Hello"
    assert story[1] == "Me"
    conn.close()


def test_new_story_requires_title_and_body(tmp_path):
    client = _client(tmp_path)
    r = client.post("/stories/new", data={"title": "", "body_text": ""})
    assert r.status_code == 200
    assert b"required" in r.data
    conn = db.connect(tmp_path / "t.sqlite")
    assert conn.execute("SELECT COUNT(*) FROM stories").fetchone()[0] == 0
    conn.close()


def test_removed_stories_list_and_restore(tmp_path, make_sig):
    dbpath = tmp_path / "t.sqlite"
    conn = db.connect(dbpath)
    db.upsert_story(conn, make_sig("s1", title="Gone"))
    db.set_story_status(conn, "s1", "removed")
    conn.commit()
    conn.close()

    client = _client(tmp_path)
    r = client.get("/stories/removed")
    assert b"Gone" in r.data

    r2 = client.post("/story/s1/restore")
    assert r2.status_code == 302
    conn = db.connect(dbpath)
    assert conn.execute("SELECT status FROM stories WHERE id='s1'").fetchone()[0] == "active"
    conn.close()


def test_author_detail_page(tmp_path, make_sig):
    dbpath = tmp_path / "t.sqlite"
    conn = db.connect(dbpath)
    db.upsert_story(conn, make_sig("s1", title="Story One", author="Jane Doe"))
    conn.commit()
    conn.close()

    client = _client(tmp_path)
    r = client.get("/author/Jane Doe")
    assert r.status_code == 200
    assert b"Story One" in r.data


def test_author_detail_paginates(tmp_path, make_sig, monkeypatch):
    from storyindex import app as app_module
    monkeypatch.setattr(app_module, "PAGE_SIZE", 1)

    dbpath = tmp_path / "t.sqlite"
    conn = db.connect(dbpath)
    sig1 = make_sig("s1", title="First", author="Jane Doe")
    db.upsert_story(conn, sig1)
    sig2 = make_sig("s2", title="Second", author="Jane Doe")
    sig2.group_id = "g2"
    db.upsert_story(conn, sig2)
    conn.commit()
    conn.close()

    client = _client(tmp_path)
    r = client.get("/author/Jane Doe")
    body = r.get_data(as_text=True)
    assert "First" in body
    assert "Second" not in body
    assert "next" in body

    r = client.get("/author/Jane Doe?page=2")
    body = r.get_data(as_text=True)
    assert "Second" in body
    assert "First" not in body


def test_removed_stories_paginates(tmp_path, make_sig, monkeypatch):
    from storyindex import app as app_module
    monkeypatch.setattr(app_module, "PAGE_SIZE", 1)

    dbpath = tmp_path / "t.sqlite"
    conn = db.connect(dbpath)
    db.upsert_story(conn, make_sig("s1", title="Older"))
    db.set_story_status(conn, "s1", "removed")
    sig2 = make_sig("s2", title="Newer")
    sig2.group_id = "g2"
    db.upsert_story(conn, sig2)
    db.set_story_status(conn, "s2", "removed")
    conn.commit()
    conn.close()

    client = _client(tmp_path)
    r = client.get("/stories/removed")
    body = r.get_data(as_text=True)
    assert "Newer" in body
    assert "Older" not in body
    assert "next" in body


def test_delete_tag_route(tmp_path, make_sig):
    dbpath = tmp_path / "t.sqlite"
    conn = db.connect(dbpath)
    db.upsert_story(conn, make_sig("s1"))
    tag_id = db.get_or_create_tag(conn, "adventure", "2026-01-01T00:00:00Z")
    db.link_story_tag(conn, "s1", tag_id, source="human")
    conn.commit()
    conn.close()

    client = _client(tmp_path)
    r = client.post(f"/tags/{tag_id}/delete")
    assert r.status_code == 302
    conn = db.connect(dbpath)
    assert conn.execute("SELECT COUNT(*) FROM tags WHERE id=?", (tag_id,)).fetchone()[0] == 0
    conn.close()


def test_tag_detail_paginates(tmp_path, make_sig, monkeypatch):
    from storyindex import app as app_module
    monkeypatch.setattr(app_module, "PAGE_SIZE", 1)

    dbpath = tmp_path / "t.sqlite"
    conn = db.connect(dbpath)
    sig1 = make_sig("s1", title="First")
    db.upsert_story(conn, sig1)
    sig2 = make_sig("s2", title="Second")
    sig2.group_id = "g2"
    db.upsert_story(conn, sig2)
    tag_id = db.get_or_create_tag(conn, "adventure", "2026-01-01T00:00:00Z")
    db.link_story_tag(conn, "s1", tag_id, source="human")
    db.link_story_tag(conn, "s2", tag_id, source="human")
    conn.commit()
    conn.close()

    client = _client(tmp_path)
    r = client.get(f"/tag/{tag_id}")
    body = r.get_data(as_text=True)
    assert "(2)" in body
    assert "next" in body
    assert "First" in body
    assert "Second" not in body

    r = client.get(f"/tag/{tag_id}?page=2")
    body = r.get_data(as_text=True)
    assert "Second" in body


def test_tags_admin_filters_and_paginates(tmp_path, make_sig, monkeypatch):
    from storyindex import app as app_module
    monkeypatch.setattr(app_module, "TAGS_PAGE_SIZE", 1)

    dbpath = tmp_path / "t.sqlite"
    conn = db.connect(dbpath)
    db.get_or_create_tag(conn, "adventure", "2026-01-01T00:00:00Z")
    db.get_or_create_tag(conn, "mystery", "2026-01-01T00:00:00Z")
    conn.commit()
    conn.close()

    client = _client(tmp_path)
    r = client.get("/tags?tag_q=myst")
    body = r.get_data(as_text=True)
    assert "mystery" in body
    assert "adventure" not in body

    r = client.get("/tags")
    body = r.get_data(as_text=True)
    assert "next" in body


def test_library_rename_and_remove_routes(tmp_path):
    client = _client(tmp_path)
    client.post("/libraries/add", data={"name": "lib-a", "path": str(tmp_path / "a.sqlite")})
    client.post("/libraries/add", data={"name": "lib-b", "path": str(tmp_path / "b.sqlite")})
    client.post("/libraries/switch", data={"name": "lib-a"})

    r = client.post("/libraries/rename", data={"old_name": "lib-a", "new_name": "renamed"})
    assert r.status_code == 302
    r2 = client.get("/libraries")
    assert b"renamed" in r2.data
    assert b"lib-a" not in r2.data

    r3 = client.post("/libraries/remove", data={"name": "renamed"})
    assert r3.status_code == 302
    r4 = client.get("/libraries")
    assert b"renamed" not in r4.data

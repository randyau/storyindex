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

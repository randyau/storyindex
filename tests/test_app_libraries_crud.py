from storyindex import db, libraries
from storyindex.app import app


def _client(db_path, libs_path):
    app.config["DB_PATH"] = db_path
    app.config["LIBRARIES_PATH"] = libs_path
    return app.test_client()


def test_switch_library_changes_active_db(tmp_path, make_sig):
    db_a = tmp_path / "a.sqlite"
    db_b = tmp_path / "b.sqlite"
    libs_path = tmp_path / "libs.json"

    conn = db.connect(db_a)
    db.upsert_story(conn, make_sig("s1", title="Story In A"))
    conn.commit(); conn.close()

    conn = db.connect(db_b)
    db.upsert_story(conn, make_sig("s2", title="Story In B"))
    conn.commit(); conn.close()

    libraries.register("lib-a", str(db_a), libs_path)
    libraries.register("lib-b", str(db_b), libs_path)
    libraries.set_active("lib-a", libs_path)

    client = _client(db_a, libs_path)
    r = client.get("/")
    assert "Story In A" in r.get_data(as_text=True)

    r = client.post("/libraries/switch", data={"name": "lib-b"})
    assert r.status_code == 302
    r = client.get("/")
    assert "Story In B" in r.get_data(as_text=True)
    assert "Story In A" not in r.get_data(as_text=True)


def test_remove_and_restore_story(tmp_path, make_sig):
    dbpath = tmp_path / "t.sqlite"
    libs_path = tmp_path / "libs.json"
    conn = db.connect(dbpath)
    db.upsert_story(conn, make_sig("s1", title="Removable"))
    conn.commit(); conn.close()

    client = _client(dbpath, libs_path)
    r = client.get("/")
    assert "Removable" in r.get_data(as_text=True)

    client.post("/story/s1/remove")
    r = client.get("/")
    assert "Removable" not in r.get_data(as_text=True)

    r = client.get("/story/s1")
    assert "restore" in r.get_data(as_text=True)

    client.post("/story/s1/restore")
    r = client.get("/")
    assert "Removable" in r.get_data(as_text=True)

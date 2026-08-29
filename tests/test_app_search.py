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

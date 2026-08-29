"""Local browse/search/read/review web app.

Single-command launch, binds 0.0.0.0 so it's reachable from the Windows
side of WSL2 at http://localhost:<port>/. Talks only to the local SQLite
DB — no network calls, no model calls, nothing leaves the machine.
"""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path

from flask import Flask, g, redirect, render_template, request, url_for

from storyindex import db

app = Flask(__name__)
app.config["DB_PATH"] = Path("storyindex.sqlite")


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = db.connect(app.config["DB_PATH"])
    return g.db


@app.teardown_appcontext
def close_db(exc=None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def _now() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"


def _combined_tag_cloud(conn: sqlite3.Connection) -> list[dict]:
    """Merge tags + site_tags into one list for display. Two separate
    storage systems underneath (see docs/crawler-parser-contract.md 3a);
    unified only here, at the UI layer."""
    combined = [
        {"kind": "site", "code": t["code"], "label": t["label"], "count": t["story_count"]}
        for t in db.list_site_tags_with_counts(conn)
    ] + [
        {"kind": "tag", "id": t["id"], "label": t["name"], "count": t["story_count"]}
        for t in db.list_tags_with_counts(conn)
    ]
    combined.sort(key=lambda t: (-t["count"], t["label"]))
    return combined


@app.route("/")
def index():
    conn = get_db()
    q = request.args.get("q", "").strip()
    stories = db.search_stories(conn, q) if q else db.list_stories(conn, limit=50)
    tags = _combined_tag_cloud(conn)
    return render_template("index.html", stories=stories, tags=tags, q=q)


@app.route("/tag/<int:tag_id>")
def tag_detail(tag_id: int):
    conn = get_db()
    tag = db.get_tag(conn, tag_id)
    if tag is None:
        return "tag not found", 404
    stories = db.stories_for_tag(conn, tag_id)
    return render_template("tag.html", label=tag["name"], stories=stories)


@app.route("/site-tag/<code>")
def site_tag_detail(code: str):
    conn = get_db()
    tag = db.get_site_tag(conn, code)
    if tag is None:
        return "tag not found", 404
    stories = db.stories_for_site_tag(conn, code)
    return render_template("tag.html", label=tag["label"], stories=stories)


@app.route("/story/<story_id>")
def story_detail(story_id: str):
    conn = get_db()
    story = db.get_story(conn, story_id)
    if story is None:
        return "story not found", 404
    parts = db.get_group_parts(conn, story["group_id"])
    tags = db.tags_for_story(conn, story_id)
    site_tags = db.site_tags_for_story(conn, story_id)
    return render_template(
        "story.html", story=story, parts=parts, tags=tags, site_tags=site_tags
    )


@app.route("/story/<story_id>/tags", methods=["POST"])
def add_tag(story_id: str):
    conn = get_db()
    name = request.form.get("name", "").strip().lower()
    if name:
        db.add_story_tag_by_name(conn, story_id, name, created_at=_now(), source="human")
        conn.commit()
    return redirect(url_for("story_detail", story_id=story_id))


@app.route("/story/<story_id>/tags/<int:tag_id>/delete", methods=["POST"])
def delete_tag(story_id: str, tag_id: int):
    conn = get_db()
    db.delete_story_tag(conn, story_id, tag_id)
    conn.commit()
    return redirect(url_for("story_detail", story_id=story_id))


@app.route("/story/<story_id>/tags/<int:tag_id>/approve", methods=["POST"])
def approve_tag(story_id: str, tag_id: int):
    conn = get_db()
    db.set_story_tag_source(conn, story_id, tag_id, "human")
    conn.commit()
    return redirect(url_for("story_detail", story_id=story_id))


@app.route("/tags")
def tags_admin():
    conn = get_db()
    tags = db.list_tags_with_counts(conn)
    site_tags = db.list_site_tags_with_counts(conn)
    return render_template("tags.html", tags=tags, site_tags=site_tags)


@app.route("/tags/<int:tag_id>/rename", methods=["POST"])
def rename_tag(tag_id: int):
    conn = get_db()
    new_name = request.form.get("name", "").strip().lower()
    if new_name:
        db.rename_tag(conn, tag_id, new_name)
        conn.commit()
    return redirect(url_for("tags_admin"))

@app.route("/tags/merge", methods=["POST"])
def merge_tags():
    conn = get_db()
    src = request.form.get("src_id", type=int)
    dst = request.form.get("dst_id", type=int)
    if src and dst and src != dst:
        db.merge_tags(conn, src, dst)
        conn.commit()
    return redirect(url_for("tags_admin"))


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Local story index browse/review app")
    parser.add_argument("--db", type=Path, default=Path("storyindex.sqlite"))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    app.config["DB_PATH"] = args.db
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()

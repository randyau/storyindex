import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from storyindex import db


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "test.sqlite")
    yield c
    c.close()


class FakeSig:
    def __init__(self, id_, title="Title", author="Author", body="Once upon a time.", tags=()):
        self.id = id_
        self.group_id = id_
        self.part_index = 0
        self.title = title
        self.author = author
        self.body_text = body
        self.source_relpath = f"{id_}.html"
        self.content_hash = "h" + id_
        self.ingested_at = "2026-01-01T00:00:00Z"
        self.tags = tags


@pytest.fixture()
def make_sig():
    return FakeSig

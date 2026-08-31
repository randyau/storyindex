from storyindex.file_text import read_file_text


def _make_pdf(text: str) -> bytes:
    """Hand-built minimal single-page PDF with one text line, valid enough
    for pypdf to parse without any other PDF-writing dependency."""
    objs = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/Resources<</Font<</F1 4 0 R>>>>/MediaBox[0 0 200 200]/Contents 5 0 R>>",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    stream = f"BT /F1 24 Tf 20 100 Td ({text}) Tj ET".encode()
    objs.append(b"<</Length %d>>\nstream\n" % len(stream) + stream + b"\nendstream")

    body = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objs, start=1):
        offsets.append(len(body))
        body += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_offset = len(body)
    n = len(objs) + 1
    body += f"xref\n0 {n}\n".encode()
    body += b"0000000000 65535 f \n"
    for off in offsets:
        body += f"{off:010d} 00000 n \n".encode()
    body += b"trailer\n"
    body += f"<</Size {n}/Root 1 0 R>>\n".encode()
    body += b"startxref\n"
    body += f"{xref_offset}\n".encode()
    body += b"%%EOF"
    return bytes(body)


def test_read_file_text_plain_text_default(tmp_path):
    path = tmp_path / "story.txt"
    path.write_text("Once upon a time.", encoding="utf-8")
    assert read_file_text(path) == "Once upon a time."


def test_read_file_text_extracts_pdf_text(tmp_path):
    path = tmp_path / "paper.pdf"
    path.write_bytes(_make_pdf("Hello PDF"))
    assert "Hello PDF" in read_file_text(path)


def test_read_file_text_suffix_is_case_insensitive(tmp_path):
    path = tmp_path / "paper.PDF"
    path.write_bytes(_make_pdf("Upper Case"))
    assert "Upper Case" in read_file_text(path)

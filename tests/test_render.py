from pathlib import Path

import pytest

from tech_forum_archiver import cli, parse
from tech_forum_archiver.db import Archive
from tech_forum_archiver.render import format_topic, render_html

FIXTURES = Path(__file__).parent / "fixtures"


def test_quote_with_cite() -> None:
    html = (
        "<blockquote><div><cite>"
        '<a href="./memberlist.php?mode=viewprofile&amp;u=91">johnshaw</a>'
        ' wrote: <a href="./viewtopic.php?p=5#p5" data-post-id="5">↑</a>'
        '<span class="responsive-hide">Thu Jan 26, 2017 2:26 pm</span></cite>'
        "It does seem dead"
        "</div></blockquote>I disagree."
    )
    assert render_html(html) == (
        "> johnshaw wrote (Thu Jan 26, 2017 2:26 pm):\n"
        "> It does seem dead\n"
        "\n"
        "I disagree."
    )


def test_nested_quotes() -> None:
    html = (
        "<blockquote><div><blockquote><div>inner</div></blockquote>"
        "outer</div></blockquote>"
    )
    assert render_html(html) == "> > inner\n>\n> outer"


def test_code_block_is_indented_not_wrapped() -> None:
    long_line = "x" * 120
    html = (
        '<div class="codebox"><p>Code: <a href="#">Select all</a></p>'
        f"<pre><code>line 1\n  {long_line}</code></pre></div>after"
    )
    assert render_html(html, width=40) == f"    line 1\n      {long_line}\n\nafter"


def test_lists_links_images_and_wrapping() -> None:
    html = (
        "<ul><li>one</li><li>two</li></ul>"
        '<a href="https://example.com/x" class="postlink">site</a> '
        '<a href="https://example.com/y" class="postlink">https://example.com/y</a> '
        '<img class="smilies" src="./images/smilies/icon_e_smile.gif" alt=":)"> '
        '<img src="https://example.com/pic.png" class="postimage" alt="Image"><br>'
        + "word "
        * 30
    )
    lines = render_html(html, width=100).split("\n")
    assert lines[:3] == [
        "* one",
        "* two",
        "site <https://example.com/x> https://example.com/y :) [image: Image]",
    ]
    narrow = render_html(html, width=40).split("\n")
    assert all(len(line) <= 40 for line in narrow)
    assert len(narrow) > len(lines)


def _fixture_archive(tmp_path: Path) -> Archive:
    a = Archive(tmp_path / "read.sqlite")
    a.set_meta("base_url", "https://tech.churchofjesuschrist.org/forum/")
    for f in parse.parse_index(
        (FIXTURES / "index.html").read_text(encoding="utf-8")
    ).forums:
        a.upsert_forum(f, f.parent_id, set_parent=True)
    page = parse.parse_viewtopic(
        (FIXTURES / "viewtopic_t46914_attachment.html").read_text(encoding="utf-8")
    )
    a.queue_topic(
        page.topic_id, 1
    )  # the crawler creates the topic row before fetching it
    a.apply_topic_page(page)
    for post in page.posts:
        a.save_post(page.topic_id, post)
    a.commit()
    return a


def test_format_topic(tmp_path: Path) -> None:
    a = _fixture_archive(tmp_path)
    text = format_topic(a, 46914, width=78)
    lines = text.split("\n")
    assert lines[0] == "Church Technology for Members > Main Church Website"
    assert lines[1] == "Temple Reservations down?"
    assert lines[2] == "topic 46914, 3 posts archived"
    assert (
        lines[3] == "https://tech.churchofjesuschrist.org/forum/viewtopic.php?t=46914"
    )
    assert text.index("#1  JeffMarble (New Member)") < text.index(
        "#3  marshallkl (Church Employee)"
    )
    flat = " ".join(text.split())  # ignore where lines wrap
    assert (
        "- ID 5925: Screenshot_20260610_084129_Firefox.jpg (339.4 KiB), "
        "not downloaded -"
    ) in flat
    assert "It's back up for me." in text
    assert format_topic(a, 1, width=78) is None
    # once the file is downloaded, the note goes away
    a.save_attachment_data(5925, b"jpeg", "image/jpeg", None)
    text = format_topic(a, 46914, width=78)
    assert "  - ID 5925: Screenshot_20260610_084129_Firefox.jpg (339.4 KiB)\n" in text
    assert "not downloaded" not in text
    a.close()


def test_export_attachment(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    a = _fixture_archive(tmp_path)
    db = str(a.path)

    def export(*args: str) -> tuple[int, str]:
        code = cli.main(["--db", db, "export-attachment", *args])
        captured = capsys.readouterr()
        return code, captured.out + captured.err

    # not downloaded yet, and not in the archive at all
    code, message = export("5925")
    assert code == 1
    assert "hasn't been downloaded" in message
    assert "update --attachments" in message
    assert export("12345") == (1, "Attachment 12345 is not in the archive.\n")

    a.save_attachment_data(5925, b"\xff\xd8 jpeg bytes", "image/jpeg", None)
    a.close()
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    saved = out_dir / "Screenshot_20260610_084129_Firefox.jpg"

    # into an existing folder, under the attachment's own name
    code, message = export("5925", "--out", str(out_dir))
    assert code == 0
    assert saved.read_bytes() == b"\xff\xd8 jpeg bytes"
    assert str(saved.resolve()) in message

    # an existing file is only replaced with --force
    saved.write_bytes(b"something else")
    code, message = export("5925", "--out", str(saved))
    assert code == 1
    assert "--force" in message
    assert saved.read_bytes() == b"something else"
    assert export("5925", "--out", str(saved), "--force")[0] == 0
    assert saved.read_bytes() == b"\xff\xd8 jpeg bytes"

    # to an exact file name, and to a folder that doesn't exist
    assert export("5925", "--out", str(out_dir / "copy.jpg"))[0] == 0
    assert (out_dir / "copy.jpg").read_bytes() == b"\xff\xd8 jpeg bytes"
    code, message = export("5925", "--out", str(tmp_path / "missing" / "x.jpg"))
    assert code == 1
    assert "doesn't exist" in message


def test_export_attachment_after_failed_download(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a = _fixture_archive(tmp_path)
    a.save_attachment_data(5925, None, None, "HTTP 404")
    a.close()
    assert cli.main(["--db", str(a.path), "export-attachment", "5925"]) == 1
    assert "An earlier download failed (HTTP 404)." in capsys.readouterr().err


def test_read_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _fixture_archive(tmp_path).close()
    db = str(tmp_path / "read.sqlite")
    assert cli.main(["--db", db, "read", "46914", "--no-pager", "--width", "60"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Church Technology for Members > Main Church Website\n")
    assert max(len(line) for line in out.splitlines()) <= 64
    assert cli.main(["--db", db, "read", "999999", "--no-pager"]) == 1
    assert "not in the archive" in capsys.readouterr().err


def test_delete_attachment_data(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a = _fixture_archive(tmp_path)
    columns = (
        "attach_id, post_id, filename, size_text, download_count, is_image, is_inline, "
        "data, content_type, size_bytes, fetched_at, fetch_error"
    )

    def row() -> tuple:
        return tuple(
            a.conn.execute(
                f"SELECT {columns} FROM attachments WHERE attach_id = 5925"  # noqa: S608
            ).fetchone()
        )

    never_downloaded = row()
    a.save_attachment_data(5925, b"jpeg bytes", "image/jpeg", None)
    assert row() != never_downloaded
    a.close()
    db = str(tmp_path / "read.sqlite")

    # a downloaded file, an unknown ID, and nothing left to delete, in one call
    assert cli.main(["--db", db, "delete-attachment-data", "5925", "777", "5925"]) == 1
    captured = capsys.readouterr()
    assert "Deleted the downloaded file (10 bytes) for attachment 5925." in captured.out
    assert "Attachment 5925 has no downloaded file. Nothing changed." in captured.out
    assert "Attachment 777 is not in the archive." in captured.err

    a = Archive(db)
    assert row() == never_downloaded
    assert [r["attach_id"] for r in a.pending_attachments()] == [5925]

    # a failed download is cleared too, so it will be tried again
    a.save_attachment_data(5925, None, None, "HTTP 404")
    assert a.pending_attachments() == []
    a.close()
    assert cli.main(["--db", db, "delete-attachment-data", "5925"]) == 0
    assert "the failed download attempt (HTTP 404)" in capsys.readouterr().out
    a = Archive(db)
    assert row() == never_downloaded
    a.close()


def test_vacuum_frees_deleted_attachment_data(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a = _fixture_archive(tmp_path)
    a.save_attachment_data(5925, b"x" * 2 * 1024**2, "image/jpeg", None)
    a.close()
    db = str(tmp_path / "read.sqlite")
    assert cli.main(["--db", db, "delete-attachment-data", "5925"]) == 0
    capsys.readouterr()

    a = Archive(db)
    before = a.file_size()
    assert a.unused_bytes() >= 2 * 1024**2
    a.close()
    assert cli.main(["--db", db, "vacuum"]) == 0
    out = capsys.readouterr().out
    assert "2.0 MiB of it is marked as unused." in out
    assert "The archive was " in out

    a = Archive(db)
    assert a.unused_bytes() == 0
    after = a.file_size()
    assert after <= before - 2 * 1024**2
    # the attachment itself is still there, waiting to be downloaded again
    assert [r["attach_id"] for r in a.pending_attachments()] == [5925]
    a.close()

    # running it again is harmless
    assert cli.main(["--db", db, "vacuum"]) == 0
    assert "0 bytes of it is marked as unused." in capsys.readouterr().out
    a = Archive(db)
    assert a.file_size() <= after
    a.close()


def test_vacuum_while_the_archive_is_in_use(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import sqlite3

    a = _fixture_archive(tmp_path)
    a.save_attachment_data(5925, b"x" * 1024**2, "image/jpeg", None)
    a.reset_attachment(5925)
    a.conn.execute("PRAGMA busy_timeout = 50")  # don't wait the usual 30 seconds
    other = sqlite3.connect(a.path)  # e.g. an update that is writing
    other.execute("BEGIN IMMEDIATE")
    try:
        assert cli.cmd_vacuum(a) == 1
    finally:
        other.rollback()
        other.close()
    assert "Another program is using the archive" in capsys.readouterr().err
    assert a.unused_bytes() > 0  # nothing was changed
    a.close()

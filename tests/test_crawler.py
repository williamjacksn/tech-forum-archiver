"""Crawler behaviour against a small fake phpBB board served from memory."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path

import pytest

from tech_forum_archiver.crawler import Crawler, Options
from tech_forum_archiver.db import Archive

TOPICS_PER_PAGE = 20
POSTS_PER_PAGE = 10
EPOCH = datetime(2020, 1, 1, tzinfo=UTC)


def ts(n: int) -> str:
    return (EPOCH + timedelta(minutes=n)).isoformat()


@dataclass
class FakePost:
    post_id: int
    text: str


@dataclass
class FakeTopic:
    topic_id: int
    title: str
    posts: list[FakePost] = field(default_factory=list)


class FakeBoard:
    def __init__(self, n_topics: int) -> None:
        self.next_post = 1000
        self.topics: dict[int, FakeTopic] = {}
        for tid in range(1, n_topics + 1):
            t = FakeTopic(tid, f"Topic {tid}")
            self.topics[tid] = t
            self.reply(tid, n=1 + tid % 3 * 7)  # 1, 8 or 15 posts

    def reply(self, tid: int, n: int = 1) -> None:
        for _ in range(n):
            self.next_post += 1
            self.topics[tid].posts.append(
                FakePost(self.next_post, f"post {self.next_post}")
            )

    def sorted_topics(self) -> list[FakeTopic]:
        return sorted(
            self.topics.values(), key=lambda t: t.posts[-1].post_id, reverse=True
        )

    # -- pages

    @staticmethod
    def _user(uid: int = 2, name: str = "alice") -> str:
        return (
            f'<a href="./memberlist.php?mode=viewprofile&amp;u={uid}" '
            f'class="username">{name}</a>'
        )

    def _bar(self, total: str, base: str, start: int, per_page: int, count: int) -> str:
        nxt = start + per_page
        link = (
            f'<a rel="next" href="{base}&amp;start={nxt}">Next</a>'
            if nxt < count
            else ""
        )
        return (
            '<div class="action-bar bar-top">'
            f'<div class="pagination">{total}<ul>{link}</ul></div></div>'
        )

    def index(self) -> str:
        return """
        <div class="forabg"><ul class="topiclist"><li class="header">
          <dl class="row-item"><dt><div class="list-inner">
            <a href="./viewforum.php?f=1">Category</a></div></dt></dl></li></ul>
        <ul class="topiclist forums"><li class="row">
          <dl class="row-item forum_read"><dt><div class="list-inner">
            <a href="./viewforum.php?f=2" class="forumtitle">Forum</a>
          <br />Desc</div></dt><dd class="topics">1 <dfn>Topics</dfn></dd>
          <dd class="posts">1 <dfn>Posts</dfn></dd></dl></li></ul></div>
        """

    def viewforum(self, f: int, start: int) -> str:
        crumbs = (
            '<ul id="nav-breadcrumbs">'
            '<span class="crumb" data-forum-id="1">Category</span>'
        )
        if f == 1:
            return (
                crumbs + '</ul><h2 class="forum-title">'
                '<a href="./viewforum.php?f=1">Category</a></h2>'
            )
        topics = self.sorted_topics()
        rows = []
        for t in topics[start : start + TOPICS_PER_PAGE]:
            last = t.posts[-1]
            rows.append(f"""
            <li class="row"><dl class="row-item topic_read"><dt><div class="list-inner">
              <a href="./viewtopic.php?t={t.topic_id}"
                 class="topictitle">{escape(t.title)}</a>
              <div class="topic-poster">by {self._user()} &raquo;
                <time datetime="{ts(t.posts[0].post_id)}">x</time></div>
            </div></dt>
            <dd class="posts">{len(t.posts) - 1} <dfn>Replies</dfn></dd>
            <dd class="views">5 <dfn>Views</dfn></dd>
            <dd class="lastpost"><span>by {self._user()}
              <a href="./viewtopic.php?p={last.post_id}#p{last.post_id}">go</a>
              <time datetime="{ts(last.post_id)}">x</time></span></dd></dl></li>""")
        return (
            crumbs
            + '<span class="crumb" data-forum-id="2">Forum</span></ul>'
            + '<h2 class="forum-title"><a href="./viewforum.php?f=2">Forum</a></h2>'
            + self._bar(
                f"{len(topics)} topics",
                "./viewforum.php?f=2",
                start,
                TOPICS_PER_PAGE,
                len(topics),
            )
            + '<ul class="topiclist topics">'
            + "".join(rows)
            + "</ul>"
        )

    def viewtopic(self, tid: int, start: int) -> str | None:
        t = self.topics.get(tid)
        if t is None:
            return None
        start = min(start, (len(t.posts) - 1) // POSTS_PER_PAGE * POSTS_PER_PAGE)
        posts = []
        for i, p in enumerate(t.posts[start : start + POSTS_PER_PAGE], start + 1):
            posts.append(f"""
            <div id="p{p.post_id}" class="post has-profile bg1"><div class="inner">
              <dl class="postprofile"><dt>{self._user()}</dt>
                <dd class="profile-posts"><strong>Posts:</strong> 3</dd></dl>
              <div class="postbody">
                <h3><a href="#p{p.post_id}">Re: {escape(t.title)}</a></h3>
                <p class="author post-number"><a href="#p{p.post_id}">#{i}</a></p>
                <p class="author">by {self._user()}
                  <time datetime="{ts(p.post_id)}">x</time></p>
                <div class="content">{escape(p.text)}</div></div></div></div>""")
        return (
            '<ul id="nav-breadcrumbs">'
            '<span class="crumb" data-forum-id="1">Category</span>'
            '<span class="crumb" data-forum-id="2">Forum</span></ul>'
            f'<h2 class="topic-title"><a href="./viewtopic.php?t={tid}">'
            f"{escape(t.title)}</a></h2>"
            + self._bar(
                f"{len(t.posts)} posts",
                f"./viewtopic.php?t={tid}",
                start,
                POSTS_PER_PAGE,
                len(t.posts),
            )
            + "".join(posts)
        )


class FakeResponse:
    def __init__(self, text: str, status: int = 200) -> None:
        self.text = text
        self.content = text.encode("utf-8")
        self.status_code = status
        self.is_redirect = False
        self.headers: dict[str, str] = {}


class FakeFetcher:
    base_url = "https://example.invalid/forum/"

    def __init__(self, board: FakeBoard) -> None:
        self.board = board
        self.log: list[tuple[str, dict]] = []
        self.request_count = 0
        self.fail_after: int | None = None
        self.missing_files: set[int] = set()  # attachment ids that answer "not found"

    def get(self, path: str, params: dict | None = None) -> FakeResponse:
        params = params or {}
        if self.fail_after is not None and self.request_count >= self.fail_after:
            raise KeyboardInterrupt
        self.request_count += 1
        self.log.append((path, params))
        start = params.get("start", 0)
        if path == "index.php":
            return FakeResponse(self.board.index())
        if path == "viewforum.php":
            return FakeResponse(self.board.viewforum(params["f"], start))
        if path == "viewtopic.php":
            html = self.board.viewtopic(params["t"], start)
            if html is None:
                return FakeResponse(
                    '<div id="message"><h2>Information</h2>'
                    "The requested topic does not exist.</div>",
                    404,
                )
            return FakeResponse(html)
        if "download/file.php" in path:  # attachments (id=...) and avatars (avatar=...)
            if params.get("id") in self.missing_files:
                return FakeResponse("not found", 404)
            return FakeResponse(f"file {params or path}")
        raise AssertionError(path)

    def count(self, path: str) -> int:
        return sum(1 for p, _ in self.log if p == path)


@pytest.fixture
def archive(tmp_path: Path) -> Iterator[Archive]:
    a = Archive(tmp_path / "test.sqlite")
    yield a
    a.close()


def run(
    archive: Archive, board: FakeBoard, **opts: bool | int | list[int] | None
) -> FakeFetcher:
    fetcher = FakeFetcher(board)
    Crawler(archive, fetcher).run(Options(**opts))
    return fetcher


def archived_posts(archive: Archive) -> set[int]:
    return {
        r[0]
        for r in archive.conn.execute(
            "SELECT post_id FROM posts WHERE removed_at IS NULL"
        )
    }


def board_posts(board: FakeBoard) -> set[int]:
    return {p.post_id for t in board.topics.values() for p in t.posts}


def test_initial_crawl_gets_everything(archive: Archive) -> None:
    board = FakeBoard(45)
    f = run(archive, board)
    assert archived_posts(archive) == board_posts(board)
    assert f.count("viewforum.php") == 1 + 3  # category page + 3 listing pages
    forums = dict(
        archive.conn.execute("SELECT forum_id, parent_id FROM forums").fetchall()
    )
    assert forums == {1: None, 2: 1}
    assert archive.stats()["topics_pending"] == 0


def test_update_with_no_changes_is_cheap(archive: Archive) -> None:
    board = FakeBoard(45)
    run(archive, board)
    f = run(archive, board)
    assert f.count("viewforum.php") == 2  # one page per forum
    assert f.count("viewtopic.php") == 0


def test_update_fetches_only_new_posts(archive: Archive) -> None:
    board = FakeBoard(45)
    run(archive, board)
    old_topic = board.sorted_topics()[-1]  # deepest in the listing
    board.reply(old_topic.topic_id, n=1)
    big = next(
        t for t in board.topics.values() if len(t.posts) == 15 and t is not old_topic
    )
    board.reply(big.topic_id, n=12)  # spills over two new pages
    f = run(archive, board)
    assert archived_posts(archive) == board_posts(board)
    assert f.count("viewforum.php") == 2
    topic_requests = [p["t"] for path, p in f.log if path == "viewtopic.php"]
    assert topic_requests.count(old_topic.topic_id) == 1
    # last page (start=20) then back to start=10, whose first post was known already
    assert topic_requests.count(big.topic_id) == 2


def test_interrupted_initial_crawl_resumes(archive: Archive) -> None:
    board = FakeBoard(65)
    fetcher = FakeFetcher(board)
    fetcher.fail_after = 3  # index, category, first listing page
    with pytest.raises(KeyboardInterrupt):
        Crawler(archive, fetcher).run(Options())
    assert archive.forum_listing_state(2) == 20
    board.reply(1)  # new activity moves topic 1 to the top
    f = run(archive, board)
    assert archived_posts(archive) == board_posts(board)
    starts = [
        p.get("start", 0)
        for path, p in f.log
        if path == "viewforum.php" and p["f"] == 2
    ]
    # first page has unknown topics so the walk continues through every page
    assert starts == [0, 20, 40, 60]
    assert archive.forum_listing_state(2) is None


def test_full_refresh_detects_edits_and_deletions(archive: Archive) -> None:
    board = FakeBoard(5)
    run(archive, board)
    t = board.topics[1]
    edited, deleted = t.posts[1], t.posts[2]
    edited.text = "edited text"
    t.posts.remove(deleted)
    run(archive, board, full=True)
    row = archive.conn.execute(
        "SELECT content_text FROM posts WHERE post_id = ?", (edited.post_id,)
    ).fetchone()
    assert row[0] == "edited text"
    revisions = archive.conn.execute(
        "SELECT content_html FROM post_revisions WHERE post_id = ?", (edited.post_id,)
    ).fetchall()
    assert [r[0] for r in revisions] == [f"post {edited.post_id}"]
    removed = archive.conn.execute(
        "SELECT removed_at FROM posts WHERE post_id = ?", (deleted.post_id,)
    ).fetchone()[0]
    assert removed is not None
    hits = archive.conn.execute(
        "SELECT rowid FROM posts_fts WHERE posts_fts MATCH 'edited'"
    ).fetchall()
    assert [tuple(h) for h in hits] == [(edited.post_id,)]


def test_deleted_topic_is_marked_removed(archive: Archive) -> None:
    board = FakeBoard(5)
    run(archive, board)
    del board.topics[2]
    run(archive, board, topic_ids=[2])
    removed_at, error = archive.conn.execute(
        "SELECT removed_at, fetch_error FROM topics WHERE topic_id = 2"
    ).fetchone()
    assert removed_at is not None
    assert "does not exist" in error


def test_pending_topics_do_not_force_full_listing(archive: Archive) -> None:
    board = FakeBoard(45)
    run(archive, board, fetch_topics=False)
    assert archive.stats()["topics_pending"] == 45
    f = run(archive, board, fetch_topics=False)
    assert f.count("viewforum.php") == 2  # category page + first listing page
    f = run(archive, board)
    assert archived_posts(archive) == board_posts(board)
    assert f.count("viewforum.php") == 2


def test_search_output_shows_topic(
    archive: Archive, capsys: pytest.CaptureFixture[str]
) -> None:
    from tech_forum_archiver import cli

    board = FakeBoard(3)
    run(archive, board)
    target = board.topics[2].posts[0]
    assert (
        cli.main(["--db", str(archive.path), "search", f'"post {target.post_id}"']) == 0
    )
    out = capsys.readouterr().out
    assert "topic 2: Topic 2" in out
    assert f"viewtopic.php?p={target.post_id}#p{target.post_id}" in out
    print(out)


def test_queued_only_skips_index_and_listings(archive: Archive) -> None:
    board = FakeBoard(45)
    run(archive, board, fetch_topics=False)
    f = run(archive, board, queued_only=True, max_topics=10)
    assert f.count("index.php") == 0
    assert f.count("viewforum.php") == 0
    assert archive.stats()["topics_pending"] == 35
    # --forum with a category includes the forums below it
    f = run(archive, board, queued_only=True, forum_ids=[1])
    assert f.count("viewforum.php") == 0
    assert archived_posts(archive) == board_posts(board)
    assert archive.stats()["topics_pending"] == 0
    # nothing queued: no requests at all
    assert run(archive, board, queued_only=True).request_count == 0


def test_progress_is_reported_every_30_seconds(
    archive: Archive, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    import itertools
    import logging

    from tech_forum_archiver import crawler

    board = FakeBoard(8)
    run(archive, board, fetch_topics=False)
    clock = itertools.count(0, 10)  # each reading of the clock is 10 seconds later
    monkeypatch.setattr(crawler.time, "monotonic", lambda: next(clock))
    with caplog.at_level(logging.INFO, logger="tech_forum_archiver.crawler"):
        run(archive, board, queued_only=True)
    reported = [m.split()[1] for m in caplog.messages if m.startswith("progress:")]
    # clock starts at 0; topic i finishes at 10*i seconds -> reports at 30s and 60s,
    # plus the end
    assert reported == ["3/8", "6/8", "8/8"]


@pytest.mark.parametrize(
    "query", ["post AND", '"unclosed', "e-mail", "ward.directory", "NOT post"]
)
def test_search_explains_bad_queries(
    archive: Archive, capsys: pytest.CaptureFixture[str], query: str
) -> None:
    from tech_forum_archiver import cli

    run(archive, FakeBoard(3))
    assert cli.main(["--db", str(archive.path), "search", query]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "couldn't be understood" in captured.err
    assert "'\"e-mail\"'" in captured.err


def test_search_with_no_matches(
    archive: Archive, capsys: pytest.CaptureFixture[str]
) -> None:
    from tech_forum_archiver import cli

    run(archive, FakeBoard(3))
    assert cli.main(["--db", str(archive.path), "search", "zebra"]) == 0
    assert capsys.readouterr().err.strip() == "No matching posts."


def test_file_downloads_report_progress(
    archive: Archive, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    import itertools
    import logging

    from tech_forum_archiver import crawler

    board = FakeBoard(3)
    run(archive, board)
    post_id = board.topics[1].posts[0].post_id
    for attach_id in range(1, 6):
        archive.conn.execute(
            "INSERT INTO attachments (attach_id, post_id, first_seen_at, last_seen_at) "
            "VALUES (?, ?, '', '')",
            (attach_id, post_id),
        )
    for n in range(4):
        archive.conn.execute(
            "INSERT INTO avatars (url) VALUES (?)",
            (f"./download/file.php?avatar={n}.jpg",),
        )
    archive.commit()

    fetcher = FakeFetcher(board)
    fetcher.missing_files = {3}
    clock = itertools.count(0, 10)  # each reading of the clock is 10 seconds later
    monkeypatch.setattr(crawler.time, "monotonic", lambda: next(clock))
    with caplog.at_level(logging.INFO, logger="tech_forum_archiver.crawler"):
        Crawler(archive, fetcher).run(
            Options(queued_only=True, attachments=True, avatars=True)
        )
    progress = [m for m in caplog.messages if m.startswith("progress:")]
    # each list starts its own 30-second timer, and always reports its last item
    assert [" ".join(m.split()[1:3]) for m in progress] == [
        "3/5 attachments,",
        "5/5 attachments,",
        "3/4 avatars,",
        "4/4 avatars,",
    ]
    assert "2 downloaded, 1 failed" in progress[0]
    assert "4 downloaded, 1 failed" in progress[1]
    assert "4 downloaded, 0 failed" in progress[3]
    assert archive.stats()["attachments_downloaded"] == 4
    assert archive.stats()["avatars_downloaded"] == 4


def test_stats_shows_attachment_sizes(
    archive: Archive, capsys: pytest.CaptureFixture[str]
) -> None:
    from tech_forum_archiver import cli

    board = FakeBoard(1)
    run(archive, board)
    post_id = board.topics[1].posts[0].post_id
    for attach_id, size_text in [(1, "1.5 MiB"), (2, "512 KiB"), (3, None)]:
        archive.conn.execute(
            "INSERT INTO attachments (attach_id, post_id, size_text, first_seen_at, "
            "last_seen_at) VALUES (?, ?, ?, '', '')",
            (attach_id, post_id, size_text),
        )
    archive.commit()
    archive.save_attachment_data(1, b"x" * 1_000_000, "image/png", None)
    for name, size in [("a.jpg", 1500), ("b.jpg", 2000), ("c.jpg", None)]:
        url = f"./download/file.php?avatar={name}"
        archive.conn.execute("INSERT INTO avatars (url) VALUES (?)", (url,))
        if size is not None:  # c.jpg is known but not downloaded
            archive.save_avatar_data(url, b"x" * size, "image/jpeg", None)
    archive.commit()
    stats = archive.stats()
    assert stats["attachments_listed_bytes"] == 2 * 1024**2
    assert stats["attachments_downloaded_bytes"] == 1_000_000
    assert stats["avatars_downloaded_bytes"] == 3500

    assert cli.main(["--db", str(archive.path), "stats"]) == 0
    lines = {
        line.split()[0]: line.split()[1:]
        for line in capsys.readouterr().out.split("\n")
        if line.strip()
    }
    assert lines["attachments_listed_size"] == ["2.0", "MiB", "(approximate)"]
    assert lines["attachments_downloaded_size"] == ["976.6", "KiB"]
    assert lines["avatars_downloaded"] == ["2"]
    assert lines["avatars_downloaded_size"] == ["3.4", "KiB"]


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (0, "0 bytes"),
        (1023, "1,023 bytes"),
        (1024, "1.0 KiB"),
        (90_349_525, "86.2 MiB"),
        (5 * 1024**4, "5.0 TiB"),
        (3000 * 1024**4, "3,000.0 TiB"),
    ],
)
def test_format_bytes(n: int, expected: str) -> None:
    from tech_forum_archiver.cli import format_bytes

    assert format_bytes(n) == expected


def test_delete_all_attachment_data(
    archive: Archive,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    from tech_forum_archiver import cli

    board = FakeBoard(1)
    run(archive, board)
    post_id = board.topics[1].posts[0].post_id
    for attach_id in (1, 2, 3, 4):
        archive.conn.execute(
            "INSERT INTO attachments (attach_id, post_id, filename, size_text, "
            "first_seen_at, last_seen_at) VALUES (?, ?, ?, '1 KiB', '', '')",
            (attach_id, post_id, f"file{attach_id}.txt"),
        )
    archive.commit()
    archive.save_attachment_data(1, b"x" * 1024, "text/plain", None)
    archive.save_attachment_data(2, b"y" * 2048, "text/plain", None)
    archive.save_attachment_data(3, None, None, "HTTP 404")  # 4 was never tried
    db = str(archive.path)

    def delete_all(*extra: str) -> int:
        return cli.main(["--db", db, "delete-attachment-data", "--all", *extra])

    # without a terminal to ask in, --yes is required
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert delete_all() == 1
    assert (
        "This would delete 2 downloaded attachment files (3.0 KiB) and 1 failed "
        "download attempt. Add --yes to confirm." in capsys.readouterr().err
    )

    # in a terminal it asks, and only "yes" goes ahead
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "no")
    assert delete_all() == 1
    assert "Nothing changed." in capsys.readouterr().out
    assert archive.attachment_download_summary() == (2, 3072, 1)

    monkeypatch.setattr("builtins.input", lambda prompt: "yes")
    assert delete_all() == 0
    assert "Run `vacuum`" in capsys.readouterr().out
    assert archive.attachment_download_summary() == (0, 0, 0)
    assert [r["attach_id"] for r in archive.pending_attachments()] == [1, 2, 3, 4]
    names = archive.conn.execute(
        "SELECT filename, size_text FROM attachments ORDER BY attach_id"
    ).fetchall()
    assert [tuple(r) for r in names] == [
        (f"file{i}.txt", "1 KiB") for i in (1, 2, 3, 4)
    ]

    # nothing left to delete; --yes skips the question
    assert delete_all("--yes") == 0
    assert "Nothing changed." in capsys.readouterr().out


def test_download_attachment(
    archive: Archive, capsys: pytest.CaptureFixture[str]
) -> None:
    from tech_forum_archiver import cli

    board = FakeBoard(1)
    run(archive, board)
    post_id = board.topics[1].posts[0].post_id
    for attach_id in (1, 2, 3, 4):
        archive.conn.execute(
            "INSERT INTO attachments (attach_id, post_id, first_seen_at, last_seen_at) "
            "VALUES (?, ?, '', '')",
            (attach_id, post_id),
        )
    archive.commit()
    archive.save_attachment_data(2, b"old copy", "text/plain", None)
    archive.save_attachment_data(4, None, None, "HTTP 503")  # failed earlier
    fetcher = FakeFetcher(board)
    fetcher.missing_files = {3}

    def download(*ids: int, force: bool = False) -> int:
        return cli.cmd_download_attachment(
            archive, list(ids), force=force, fetcher=fetcher
        )

    assert download(1, 2, 3, 4, 99) == 1
    captured = capsys.readouterr()
    assert "Downloaded attachment 1 (" in captured.out
    assert "Attachment 2 is already downloaded (8 bytes). Use --force" in captured.out
    assert "Downloaded attachment 4 (" in captured.out  # the earlier failure is retried
    assert "The forum couldn't send attachment 3 (HTTP 404)." in captured.err
    assert "The problem is recorded in the archive." in captured.err
    assert "Attachment 99 is not in the archive." in captured.err
    # only the attachments that needed it were requested
    assert [p["id"] for path, p in fetcher.log if "file.php" in path] == [1, 3, 4]
    assert archive.get_attachment(1)["data"] == b"file {'id': 1}"
    assert archive.get_attachment(2)["data"] == b"old copy"
    assert archive.get_attachment(3)["fetch_error"] == "HTTP 404"
    assert archive.get_attachment(4)["fetch_error"] is None

    # --force downloads again, but a failed attempt keeps the copy that was there
    fetcher.missing_files = {2}
    assert download(2, force=True) == 1
    assert "The copy already in the archive is unchanged." in capsys.readouterr().err
    assert archive.get_attachment(2)["data"] == b"old copy"
    assert archive.get_attachment(2)["fetch_error"] is None
    fetcher.missing_files = set()
    assert download(2, force=True) == 0
    assert archive.get_attachment(2)["data"] == b"file {'id': 2}"

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import pydoc
import re
import shutil
import sqlite3
import sys
from pathlib import Path

from tech_forum_archiver import render
from tech_forum_archiver.crawler import Crawler, Options
from tech_forum_archiver.db import Archive
from tech_forum_archiver.fetch import (
    DEFAULT_BASE_URL,
    DEFAULT_USER_AGENT,
    Fetcher,
    FetchError,
)
from tech_forum_archiver.publish import publish

log = logging.getLogger("tech_forum_archiver")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tech-forum-archiver",
        description="Archive the public phpBB forum at "
        "tech.churchofjesuschrist.org into SQLite.",
    )
    p.add_argument(
        "--db", default="tech-forum.sqlite", help="archive file (default: %(default)s)"
    )
    p.add_argument(
        "-v", "--verbose", action="count", default=0, help="more logging (-vv for HTTP)"
    )
    sub = p.add_subparsers(dest="command", required=True)

    u = sub.add_parser(
        "update",
        help="create or incrementally update the archive",
        description="Create the archive, or bring an existing one up to date. Safe to "
        "interrupt and re-run: progress is saved continuously.",
    )
    u.add_argument(
        "--base-url", default=DEFAULT_BASE_URL, help="board URL (default: %(default)s)"
    )
    u.add_argument(
        "--forum", type=int, action="append", metavar="ID",
        help="only crawl this forum and its subforums (repeatable)",
    )  # fmt: skip
    what = u.add_mutually_exclusive_group()
    what.add_argument(
        "--topic", type=int, action="append", metavar="ID",
        help="only fetch this topic (repeatable); skips the forum listings",
    )  # fmt: skip
    what.add_argument(
        "--queued-only", action="store_true",
        help="skip the board index and forum listings; only fetch topics that earlier "
        "runs queued as new or changed (combine with --forum to limit it to known "
        "forums)",
    )  # fmt: skip
    what.add_argument(
        "--no-topics", action="store_true",
        help="only update forums and topic lists; do not fetch posts",
    )  # fmt: skip
    u.add_argument(
        "--full", action="store_true",
        help="re-read every listing page and every topic page, to pick up edits and "
        "deletions in older posts (slow: as long as the initial crawl)",
    )  # fmt: skip
    u.add_argument(
        "--max-topics",
        type=int,
        metavar="N",
        help="fetch at most N queued topics in this run",
    )
    u.add_argument(
        "--attachments", action="store_true", help="download attachment files"
    )
    u.add_argument("--avatars", action="store_true", help="download user avatar images")
    u.add_argument(
        "--max-requests", type=int, default=10, metavar="N",
        help="at most N requests per window "
        "(default: %(default)s; the server allows 10/30s)",
    )  # fmt: skip
    u.add_argument(
        "--window", type=float, default=32.0, metavar="SECONDS",
        help="rate-limit window in seconds (default: %(default)s)",
    )  # fmt: skip
    u.add_argument("--user-agent", default=DEFAULT_USER_AGENT)

    sub.add_parser("stats", help="show what the archive contains")

    s = sub.add_parser("search", help="full-text search of archived posts")
    s.add_argument("query", help="SQLite FTS5 query, e.g. 'ward AND directory'")
    s.add_argument("--limit", type=int, default=20)

    r = sub.add_parser(
        "read",
        help="print every post in a topic, to read it in the terminal",
        description="Print an archived topic as text, oldest post first. Times are "
        "shown in your local timezone. Output goes through a pager when printing to a "
        "terminal.",
    )
    r.add_argument(
        "topic_id", type=int, metavar="TOPIC_ID", help="topic number (the t= in URLs)"
    )
    r.add_argument(
        "--width", type=int, metavar="N",
        help="wrap text to N columns (default: terminal width, at most 100)",
    )  # fmt: skip
    r.add_argument(
        "--no-pager", action="store_true", help="print straight to standard output"
    )

    pub = sub.add_parser(
        "publish",
        help="write the archive out as a static HTML site",
        description="Write the archive as a static website that can be browsed "
        "offline or hosted anywhere. Files are only rewritten when they change.",
    )
    pub.add_argument(
        "--out", metavar="DIR",
        help="output directory (default: next to the database, named after it with "
        "-site, e.g. tech-forum-site)",
    )  # fmt: skip
    pub.add_argument(
        "--title",
        default="Tech Forum Archive",
        help="site title (default: %(default)s)",
    )
    pub.add_argument(
        "--no-search",
        action="store_true",
        help="leave out the search page and its index",
    )

    dl = sub.add_parser(
        "download-attachment",
        help="download the files for specific attachments into the archive",
        description="Download the files for one or more attachments, given their IDs, "
        "into the archive. The read command shows attachment IDs. Attachments whose "
        "files are already in the archive are skipped unless you use --force. This "
        "uses the same speed limit as update, so don't run it while an update is "
        "running.",
    )
    dl.add_argument(
        "attach_ids", type=int, nargs="+", metavar="ID", help="attachment ID"
    )
    dl.add_argument(
        "--force",
        action="store_true",
        help="download the file again even if it is already in the archive",
    )

    exp = sub.add_parser(
        "export-attachment",
        help="save one downloaded attachment from the archive as a normal file",
        description="Save an attachment's file from the archive. The attachment ID is "
        "shown by the read command. The file must have been downloaded with "
        "update --attachments.",
    )
    exp.add_argument("attach_id", type=int, metavar="ID", help="attachment ID")
    exp.add_argument(
        "--out", metavar="PATH",
        help="file or existing folder to save to (default: the attachment's own file "
        "name, in the current folder)",
    )  # fmt: skip
    exp.add_argument(
        "--force", action="store_true", help="replace the file if it already exists"
    )

    dele = sub.add_parser(
        "delete-attachment-data",
        help="delete downloaded attachment files from the archive",
        description="Delete the downloaded file for one or more attachments, or for "
        "all of them with --all. The attachments stay in the archive as if they had "
        "not been downloaded yet, so the next update --attachments downloads them "
        "again. This also clears a failed download, so that it is tried again.",
    )
    dele.add_argument(
        "attach_ids", type=int, nargs="*", metavar="ID", help="attachment ID"
    )
    dele.add_argument(
        "--all", action="store_true", help="delete every downloaded attachment file"
    )
    dele.add_argument(
        "--yes", action="store_true", help="with --all, don't ask for confirmation"
    )

    sub.add_parser(
        "vacuum",
        help="shrink the archive file by freeing unused space",
        description="Rebuild the archive file without its unused space, such as the "
        "space left by delete-attachment-data. Stop any update and close other "
        "programs that use the archive first. This needs free disk space of up to "
        "the size of the archive while it runs.",
    )
    return p


# Characters that Windows doesn't allow in file names, plus control characters.
UNSAFE_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}  # fmt: skip


def safe_filename(name: str | None, fallback: str) -> str:
    """A file name that is safe to create on Windows, macOS, and Linux."""
    name = UNSAFE_FILENAME_CHARS.sub("_", name or "").strip(" .")
    if not name:
        return fallback
    if name.split(".")[0].upper() in WINDOWS_RESERVED_NAMES:
        name = f"_{name}"
    return name


def cmd_vacuum(archive: Archive) -> int:
    # The rebuild also packs partly empty pages together, so it usually frees more
    # than the space that is marked as unused.
    unused = archive.unused_bytes()
    before = archive.file_size()
    print(
        f"Rebuilding the archive. {format_bytes(unused)} of it is marked as unused. "
        "This can take a few minutes for a large archive."
    )
    try:
        finished = archive.vacuum()
    except sqlite3.OperationalError as e:
        if "locked" not in str(e) and "busy" not in str(e):
            raise
        print(
            "Another program is using the archive, so it can't be shrunk now. "
            "Stop any update that is running, close other programs that have the "
            "archive open, and try again.",
            file=sys.stderr,
        )
        return 1
    after = archive.file_size()
    print(f"The archive was {format_bytes(before)} and is now {format_bytes(after)}.")
    if not finished:
        print(
            "Another program is reading the archive, so some of the space will only "
            "be freed after that program closes it."
        )
    return 0


def cmd_delete_all_attachment_data(archive: Archive, *, confirmed: bool) -> int:
    files, size, failed = archive.attachment_download_summary()
    if not files and not failed:
        print("No attachment files have been downloaded. Nothing changed.")
        return 0
    parts = []
    if files:
        noun = "file" if files == 1 else "files"
        parts.append(f"{files:,} downloaded attachment {noun} ({format_bytes(size)})")
    if failed:
        noun = "attempt" if failed == 1 else "attempts"
        parts.append(f"{failed:,} failed download {noun}")
    summary = " and ".join(parts)
    if not confirmed:
        if not sys.stdin.isatty():
            print(
                f"This would delete {summary}. Add --yes to confirm.", file=sys.stderr
            )
            return 1
        answer = input(f"Delete {summary}? Type yes to continue: ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Nothing changed.")
            return 1
    archive.reset_all_attachments()
    print(
        f"Deleted {summary}. The next `update --attachments` will download them "
        "again. Run `vacuum` to make the archive file smaller."
    )
    return 0


def cmd_delete_attachment_data(archive: Archive, attach_ids: list[int]) -> int:
    status = 0
    for attach_id in attach_ids:
        a = archive.get_attachment(attach_id)
        if a is None:
            print(f"Attachment {attach_id} is not in the archive.", file=sys.stderr)
            status = 1
            continue
        if a["data"] is not None:
            what = f"the downloaded file ({format_bytes(len(a['data']))})"
        elif a["fetch_error"]:
            what = f"the failed download attempt ({a['fetch_error']})"
        else:
            print(f"Attachment {attach_id} has no downloaded file. Nothing changed.")
            continue
        archive.reset_attachment(attach_id)
        print(
            f"Deleted {what} for attachment {attach_id}. The next "
            "`update --attachments` will download it again."
        )
    return status


def cmd_download_attachment(
    archive: Archive,
    attach_ids: list[int],
    *,
    force: bool,
    fetcher: Fetcher | None = None,
) -> int:
    status = 0
    wanted: list[int] = []
    for attach_id in attach_ids:
        a = archive.get_attachment(attach_id)
        if a is None:
            print(
                f"Attachment {attach_id} is not in the archive. Attachments are added "
                "when the post that contains them is archived.",
                file=sys.stderr,
            )
            status = 1
        elif a["data"] is not None and not force:
            print(
                f"Attachment {attach_id} is already downloaded "
                f"({format_bytes(len(a['data']))}). Use --force to download it again."
            )
        else:
            wanted.append(attach_id)
    if not wanted:
        return status
    session = fetcher or open_session(archive)
    crawler = Crawler(archive, session)
    try:
        for attach_id in wanted:
            try:
                error = crawler.download_attachment(attach_id)
            except FetchError as e:
                print(
                    f"Couldn't reach the forum to download attachment {attach_id}: {e}",
                    file=sys.stderr,
                )
                return 1
            if error:
                kept = archive.has_attachment_data(attach_id)
                print(
                    f"The forum couldn't send attachment {attach_id} ({error}). "
                    + (
                        "The copy already in the archive is unchanged."
                        if kept
                        else "The problem is recorded in the archive."
                    ),
                    file=sys.stderr,
                )
                status = 1
            else:
                size = archive.get_attachment(attach_id)["size_bytes"]
                print(f"Downloaded attachment {attach_id} ({format_bytes(size)}).")
    finally:
        if fetcher is None:
            close_session(archive, session)
    return status


def cmd_export_attachment(
    archive: Archive, attach_id: int, out: str | None, force: bool
) -> int:
    a = archive.get_attachment(attach_id)
    if a is None:
        print(f"Attachment {attach_id} is not in the archive.", file=sys.stderr)
        return 1
    if a["data"] is None:
        reason = (
            f"An earlier download failed ({a['fetch_error']})."
            if a["fetch_error"]
            else "Run `update --attachments` to download it."
        )
        print(
            f"The file for attachment {attach_id} hasn't been downloaded. {reason}",
            file=sys.stderr,
        )
        return 1
    name = safe_filename(a["filename"], f"attachment-{attach_id}")
    target = Path(out) if out else Path(name)
    if target.is_dir():
        target /= name
    if not target.parent.is_dir():
        print(f"The folder {target.parent} doesn't exist.", file=sys.stderr)
        return 1
    if target.exists() and not force:
        print(f"{target} already exists. Use --force to replace it.", file=sys.stderr)
        return 1
    target.write_bytes(a["data"])
    print(
        f"Saved attachment {attach_id} ({format_bytes(len(a['data']))}) "
        f"to {target.resolve()}"
    )
    return 0


def default_site_dir(db: str) -> Path:
    path = Path(db)
    return path.with_name(f"{path.stem}-site")


def open_session(
    archive: Archive,
    *,
    base_url: str | None = None,
    max_requests: int = 10,
    window: float = 32.0,
    user_agent: str = DEFAULT_USER_AGENT,
) -> Fetcher:
    """A rate-limited client that continues the previous run's forum session.

    Reusing the saved cookies avoids the much stricter limit for cookieless
    visitors, and the saved request times keep back-to-back runs within the
    forum's limit.
    """
    fetcher = Fetcher(
        base_url or archive.get_meta("base_url") or DEFAULT_BASE_URL,
        max_requests=max_requests,
        window=window,
        user_agent=user_agent,
    )
    if saved := archive.get_meta("cookies"):
        fetcher.import_cookies(json.loads(saved))
    if saved := archive.get_meta("recent_requests"):
        fetcher.import_recent_requests(json.loads(saved))
    return fetcher


def close_session(archive: Archive, fetcher: Fetcher) -> None:
    """Save the session for the next run, and close the client."""
    archive.set_meta("cookies", json.dumps(fetcher.export_cookies()))
    archive.set_meta("recent_requests", json.dumps(fetcher.export_recent_requests()))
    archive.commit()
    fetcher.close()


def cmd_update(args: argparse.Namespace, archive: Archive) -> int:
    opts = Options(
        forum_ids=args.forum,
        topic_ids=args.topic,
        full=args.full,
        fetch_topics=not args.no_topics,
        queued_only=args.queued_only,
        max_topics=args.max_topics,
        attachments=args.attachments,
        avatars=args.avatars,
    )
    fetcher = open_session(
        archive,
        base_url=args.base_url,
        max_requests=args.max_requests,
        window=args.window,
        user_agent=args.user_agent,
    )
    crawler = Crawler(archive, fetcher)
    run_id = archive.start_run(dataclasses.asdict(opts))
    status = "interrupted"
    try:
        crawler.run(opts)
        status = "complete"
    except KeyboardInterrupt:
        log.warning("interrupted; progress so far is saved, re-run to continue")
    except Exception:
        status = "failed"
        log.exception("update failed; progress so far is saved, re-run to continue")
    finally:
        st = crawler.stats
        board = getattr(crawler, "index_stats", None)
        archive.finish_run(
            run_id,
            status=status,
            requests=fetcher.request_count,
            forums_listed=st.forums_listed,
            topics_fetched=st.topics_fetched,
            posts_new=st.posts_new,
            posts_changed=st.posts_changed,
            board=board,
        )
        close_session(archive, fetcher)
    log.info(
        "%s: %d requests; %d forums listed; %d topics fetched; %d new posts, "
        "%d changed, %d removed; %d attachments, %d avatars downloaded",
        status,
        fetcher.request_count,
        st.forums_listed,
        st.topics_fetched,
        st.posts_new,
        st.posts_changed,
        st.posts_removed,
        st.attachments,
        st.avatars,
    )
    return 0 if status == "complete" else 1


def format_bytes(n: int) -> str:
    """A byte count in the binary units the forum uses, e.g. ``86.2 MiB``."""
    if n < 1024:
        return f"{n:,} bytes"
    value = float(n)
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        value /= 1024
        if value < 1024 or unit == "TiB":
            break
    return f"{value:,.1f} {unit}"


def cmd_stats(archive: Archive) -> int:
    for k, v in archive.stats().items():
        if k.endswith("_bytes"):
            # sizes print as e.g. "attachments_downloaded_size   86.2 MiB"
            note = "  (approximate)" if k == "attachments_listed_bytes" else ""
            label = k.removesuffix("_bytes") + "_size"
            print(f"{label:28} {format_bytes(v):>12}{note}")
        else:
            print(f"{k:28} {v:>12,}")
    last = archive.conn.execute(
        "SELECT * FROM crawl_runs WHERE board_total_posts IS NOT NULL "
        "ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    if last:
        print(
            f"\nboard totals at last run ({last['started_at']}): "
            f"{last['board_total_topics']:,} topics, "
            f"{last['board_total_posts']:,} posts, "
            f"{last['board_total_members']:,} members"
        )
    return 0


# Errors SQLite reports when a full-text search query can't be understood. A word
# with a hyphen (e-mail) is read as a column filter, hence "no such column".
SEARCH_SYNTAX_ERRORS = ("fts5:", "unterminated string", "no such column", "MATCH")

SEARCH_HELP = """\
Put words that contain punctuation in double quotes, for example: '"e-mail"'.
Put AND, OR, and NOT between words, in capital letters; a search can't start
with NOT. Put a phrase in double quotes to find the words next to each other."""


def cmd_search(archive: Archive, query: str, limit: int) -> int:
    base = (archive.get_meta("base_url") or DEFAULT_BASE_URL).rstrip("/")
    try:
        rows = archive.conn.execute(
            """
            SELECT p.post_id, p.topic_id, p.posted_at, p.poster_name, t.title,
                   snippet(posts_fts, 1, '[', ']', ' ... ', 16) AS snip
            FROM posts_fts JOIN posts p ON p.post_id = posts_fts.rowid
            JOIN topics t ON t.topic_id = p.topic_id
            WHERE posts_fts MATCH ? ORDER BY rank LIMIT ?
            """,
            (query, limit),
        ).fetchall()
    except sqlite3.OperationalError as e:
        if not any(marker in str(e) for marker in SEARCH_SYNTAX_ERRORS):
            raise
        log.debug("search query %r failed: %s", query, e)
        print(f"The search {query!r} couldn't be understood.", file=sys.stderr)
        print(SEARCH_HELP, file=sys.stderr)
        return 2
    if not rows:
        print("No matching posts.", file=sys.stderr)
        return 0
    for r in rows:
        print(
            f"{r['posted_at']}  {r['poster_name']}  topic {r['topic_id']}: {r['title']}"
        )
        print(f"  {base}/viewtopic.php?p={r['post_id']}#p{r['post_id']}")
        print(f"  {r['snip']}\n")
    return 0


def cmd_read(
    archive: Archive, topic_id: int, width: int | None, use_pager: bool
) -> int:
    if width is None:
        width = min(shutil.get_terminal_size((80, 24)).columns - 1, 100)
    text = render.format_topic(archive, topic_id, width)
    if text is None:
        print(f"topic {topic_id} is not in the archive", file=sys.stderr)
        return 1
    if use_pager and sys.stdout.isatty():
        pydoc.pager(text)
    else:
        sys.stdout.write(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "delete-attachment-data" and bool(args.attach_ids) == args.all:
        parser.error("delete-attachment-data needs either attachment IDs or --all")
    level = logging.INFO if args.verbose == 0 else logging.DEBUG
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)-7s %(message)s")
    if args.verbose < 2:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    archive = Archive(args.db)
    try:
        if args.command == "update":
            return cmd_update(args, archive)
        if args.command == "stats":
            return cmd_stats(archive)
        if args.command == "search":
            return cmd_search(archive, args.query, args.limit)
        if args.command == "read":
            return cmd_read(archive, args.topic_id, args.width, not args.no_pager)
        if args.command == "publish":
            out = Path(args.out) if args.out else default_site_dir(args.db)
            publish(archive, out, args.title, search=not args.no_search)
            print(f"site written to {out.resolve() / 'index.html'}")
            return 0
        if args.command == "download-attachment":
            return cmd_download_attachment(archive, args.attach_ids, force=args.force)
        if args.command == "export-attachment":
            return cmd_export_attachment(archive, args.attach_id, args.out, args.force)
        if args.command == "delete-attachment-data" and args.all:
            return cmd_delete_all_attachment_data(archive, confirmed=args.yes)
        if args.command == "delete-attachment-data":
            return cmd_delete_attachment_data(archive, args.attach_ids)
        if args.command == "vacuum":
            return cmd_vacuum(archive)
    finally:
        archive.close()
    return 2

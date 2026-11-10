"""Crawl orchestration.

Strategy
--------
1. Fetch the board index: categories, top-level forums, board statistics.
2. Walk every forum's topic list (``viewforum.php?f=N&start=K``). phpBB sorts
   topics by last-post time, newest first, so once an archive exists we only
   need to read pages until we reach a topic that has not changed. Every new
   or changed topic is flagged ``needs_fetch`` in the DB.
3. Fetch each flagged topic (``viewtopic.php?t=N&start=K``). New topics are
   read from the first page; topics already in the archive are read from the
   last page backwards until we reach a page whose first post we already
   have, so an update costs about one request per changed topic.

All crawl state is kept in the database, so an interrupted run (Ctrl+C, network
failure, being throttled) simply continues where it left off next time.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field

from tech_forum_archiver import parse
from tech_forum_archiver.db import Archive
from tech_forum_archiver.fetch import Fetcher

log = logging.getLogger(__name__)

DEFAULT_POSTS_PER_PAGE = 10
PROGRESS_INTERVAL = 30.0  # seconds between progress messages while fetching topics


class Gone(Exception):
    """The requested forum or topic is not (or no longer) visible to guests."""


@dataclass
class RunStats:
    forums_listed: int = 0
    listing_pages: int = 0
    topics_queued: int = 0
    topics_fetched: int = 0
    topic_pages: int = 0
    posts_new: int = 0
    posts_changed: int = 0
    posts_removed: int = 0
    topics_removed: int = 0
    attachments: int = 0
    avatars: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class Options:
    forum_ids: list[int] | None = None  # restrict to these forums (and their subforums)
    topic_ids: list[int] | None = None  # only fetch these topics
    full: bool = False  # walk every listing page and refetch every topic page
    fetch_topics: bool = True
    queued_only: bool = (
        False  # skip the index and listings; only fetch already-queued topics
    )
    max_topics: int | None = None
    attachments: bool = False
    avatars: bool = False


class Crawler:
    def __init__(self, archive: Archive, fetcher: Fetcher) -> None:
        self.db = archive
        self.http = fetcher
        self.stats = RunStats()
        self.progress_interval = PROGRESS_INTERVAL
        pp = archive.get_meta("posts_per_page")
        self.posts_per_page = int(pp) if pp else DEFAULT_POSTS_PER_PAGE
        tz = archive.get_meta("board_timezone")
        if tz:
            parse.set_board_timezone(tz)

    # --------------------------------------------------------------- driver

    def run(self, opts: Options) -> RunStats:
        if opts.topic_ids:
            for tid in opts.topic_ids:
                self.db.queue_topic(tid, 2 if opts.full else 1)
            self.crawl_topics(None, opts.max_topics, only=set(opts.topic_ids))
        elif opts.queued_only:
            # no requests for the index or forum listings: work through the topics that
            # earlier runs already flagged as new or changed
            selected = (
                self.db.descendant_forum_ids(opts.forum_ids) if opts.forum_ids else None
            )
            if opts.full:
                n = self.db.queue_all_topics(selected)
                log.info("queued %d topics for a full refresh", n)
            self.crawl_topics(selected, opts.max_topics)
        else:
            self.crawl_index()
            selected = self.crawl_forums(opts.forum_ids, opts.full)
            if opts.full:
                n = self.db.queue_all_topics(selected)
                log.info("queued %d topics for a full refresh", n)
            if opts.fetch_topics:
                self.crawl_topics(selected, opts.max_topics)
        if opts.attachments:
            self.fetch_attachments()
        if opts.avatars:
            self.fetch_avatars()
        return self.stats

    # ---------------------------------------------------------------- index

    def crawl_index(self) -> parse.IndexPage:
        resp = self.http.get("index.php")
        if resp.status_code != 200:
            raise RuntimeError(f"board index returned HTTP {resp.status_code}")
        index = parse.parse_index(resp.text)
        if index.stats.timezone:
            parse.set_board_timezone(index.stats.timezone)
            self.db.set_meta("board_timezone", index.stats.timezone)
        self.db.set_meta("base_url", self.http.base_url)
        for f in index.forums:
            self.db.upsert_forum(f, f.parent_id, set_parent=True)
        if index.stats.newest_member:
            self.db.upsert_user_ref(index.stats.newest_member)
        self.db.commit()
        self.index_stats = index.stats
        log.info(
            "board index: %d forums; board reports %s topics, %s posts, %s members",
            len(index.forums),
            index.stats.total_topics,
            index.stats.total_posts,
            index.stats.total_members,
        )
        return index

    # --------------------------------------------------------------- forums

    def crawl_forums(self, forum_ids: list[int] | None, full: bool) -> set[int] | None:
        """List topics in every (selected) forum. Returns the set of forums crawled
        when a selection was given, else None (meaning all)."""
        if forum_ids:
            queue = deque(forum_ids)
        else:
            queue = deque(r["forum_id"] for r in self.db.crawlable_forums())
        done: set[int] = set()
        while queue:
            fid = queue.popleft()
            if fid in done:
                continue
            done.add(fid)
            try:
                children = self.crawl_forum(fid, full)
            except Gone as e:
                log.warning("forum %d: %s", fid, e)
                continue
            queue.extend(c for c in children if c not in done)
        return done if forum_ids else None

    def _get_forum_page(self, fid: int, start: int) -> parse.ForumPage:
        params = {"f": fid, "start": start} if start else {"f": fid}
        resp = self.http.get("viewforum.php", params)
        self.stats.listing_pages += 1
        if resp.is_redirect:
            raise Gone(f"redirected to {resp.headers.get('location')}")
        page = parse.parse_viewforum(resp.text)
        if page.forum_id != fid:
            msg = parse.error_message(resp.text) or f"HTTP {resp.status_code}"
            if resp.status_code == 404 or "does not exist" in msg:
                self.db.mark_forum_removed(fid)
                self.db.commit()
            raise Gone(msg)
        return page

    def crawl_forum(self, fid: int, full: bool) -> list[int]:
        resume = 0 if full else self.db.forum_listing_state(fid)
        complete = resume is None
        start = 0
        children: list[int] = []
        queued = changed_topics = 0
        while True:
            page = self._get_forum_page(fid, start)
            if start == 0:
                self.db.update_forum_page(page)
                for sf in page.subforums:
                    self.db.upsert_forum(sf, sf.parent_id, set_parent=True)
                    children.append(sf.forum_id)
                if self.db.forum_type(fid) == "category":
                    # category pages show recent topics from their child forums; those
                    # topics are recorded when their own forums are listed
                    break
            # topics are sorted by last post time, so once the oldest normal (not
            # sticky or announcement) topic on a page is unchanged, so is everything
            # after it
            tail_unchanged = False
            for t in page.topics:
                if t.is_moved:  # shadow of a topic that now lives in another forum
                    continue
                listing_changed, is_queued = self.db.upsert_topic_row(t, fid)
                queued += is_queued
                changed_topics += listing_changed
                if t.topic_type == "normal":
                    tail_unchanged = not listing_changed
            nxt = page.next_start
            if not complete and nxt is not None and nxt > resume:
                resume = nxt
                self.db.set_forum_listing_state(fid, resume)
            self.db.commit()
            if nxt is None:
                break
            if tail_unchanged:
                if complete:
                    break
                # skip ahead to where an unfinished first walk left off (minus a page
                # of slack, since new activity pushes topics further down the list)
                nxt = max(nxt, resume - (nxt - start))
            start = nxt
        self.db.set_forum_listing_state(fid, None)
        self.db.commit()
        self.stats.forums_listed += 1
        self.stats.topics_queued += queued
        log.info(
            "forum %d %r: %d topics changed in listing, %d queued for fetching",
            fid,
            page.name,
            changed_topics,
            queued,
        )
        return children

    # --------------------------------------------------------------- topics

    def crawl_topics(
        self,
        forum_ids: set[int] | None,
        limit: int | None,
        only: set[int] | None = None,
    ) -> None:
        rows = self.db.topics_to_fetch(forum_ids, None if only else limit)
        if only:
            rows = [r for r in rows if r["topic_id"] in only]
        total = len(rows)
        if total:
            log.info("fetching %d topics", total)
        progress = Progress("topics", total, self.progress_interval)
        for i, row in enumerate(rows, 1):
            tid = row["topic_id"]
            try:
                self.crawl_topic(tid, row["needs_fetch"], row["reply_count"])
            except Gone as e:
                self.stats.topics_removed += 1
                log.warning("[%d/%d] topic %d: %s", i, total, tid, e)
            progress.step(
                i,
                f"{self.stats.posts_new} new posts, {self.http.request_count} requests",
            )

    def _get_topic_page(self, tid: int, start: int) -> parse.TopicPage:
        params = {"t": tid, "start": start} if start else {"t": tid}
        resp = self.http.get("viewtopic.php", params)
        self.stats.topic_pages += 1
        if resp.is_redirect:
            reason = f"redirected to {resp.headers.get('location')}"
            self.db.mark_topic_removed(tid, reason)
            raise Gone(reason)
        page = parse.parse_viewtopic(resp.text)
        if page.topic_id != tid:
            reason = parse.error_message(resp.text) or f"HTTP {resp.status_code}"
            self.db.mark_topic_removed(tid, reason)
            raise Gone(reason)
        return page

    def _store_page(self, tid: int, page: parse.TopicPage, seen: set[int]) -> None:
        self.db.apply_topic_page(page)
        for post in page.posts:
            status = self.db.save_post(tid, post)
            seen.add(post.post_id)
            if status == "new":
                self.stats.posts_new += 1
            elif status == "changed":
                self.stats.posts_changed += 1
        self.db.commit()

    def crawl_topic(self, tid: int, mode: int, reply_count: int | None) -> None:
        known = self.db.known_post_ids(tid)
        seen: set[int] = set()
        pp = self.posts_per_page
        pages = 0
        if mode == 2 or not known or reply_count is None:
            # read every page from the start
            start = 0
            while True:
                page = self._get_topic_page(tid, start)
                pages += 1
                self._store_page(tid, page, seen)
                if page.next_start is None:
                    break
                if start == 0 and page.next_start != pp:
                    pp = self.posts_per_page = page.next_start
                    self.db.set_meta("posts_per_page", str(pp))
                start = page.next_start
            last = page
            if mode == 2:
                gone = self.db.mark_missing_posts_removed(tid, seen)
                self.stats.posts_removed += gone
        else:
            # new posts are always appended: read from the last page backwards
            start = (reply_count // pp) * pp
            page = self._get_topic_page(tid, start)
            pages += 1
            self._store_page(tid, page, seen)
            last = page
            while last.next_start is not None:  # more posts than the listing said
                last = self._get_topic_page(tid, last.next_start)
                pages += 1
                self._store_page(tid, last, seen)
            while start > 0 and (not page.posts or page.posts[0].post_id not in known):
                start = max(0, start - pp)
                page = self._get_topic_page(tid, start)
                pages += 1
                self._store_page(tid, page, seen)
        last_post_id = last.posts[-1].post_id if last.posts else None
        self.db.finish_topic(tid, last.total_posts or len(seen), last_post_id)
        self.stats.topics_fetched += 1
        log.debug(
            "topic %d %r: %d pages, %d posts seen", tid, last.title, pages, len(seen)
        )

    # ---------------------------------------------------------------- files

    def fetch_attachments(self) -> None:
        pending = self.db.pending_attachments()
        if pending:
            log.info("fetching %d attachments", len(pending))
        progress = Progress("attachments", len(pending), self.progress_interval)
        failed = 0
        for i, row in enumerate(pending, 1):
            if self.download_attachment(row["attach_id"]) is not None:
                failed += 1
            progress.step(i, self._file_details(i - failed, failed))

    def download_attachment(self, attach_id: int) -> str | None:
        """Download one attachment's file into the archive.

        Returns None on success, or the error. A failed download doesn't touch a
        copy that is already in the archive; otherwise the error is recorded.
        """
        resp = self.http.get("download/file.php", {"id": attach_id})
        if resp.status_code != 200:
            error = f"HTTP {resp.status_code}"
            if not self.db.has_attachment_data(attach_id):
                self.db.save_attachment_data(attach_id, None, None, error)
            return error
        self.db.save_attachment_data(
            attach_id, resp.content, resp.headers.get("content-type"), None
        )
        self.stats.attachments += 1
        return None

    def fetch_avatars(self) -> None:
        pending = self.db.pending_avatars()
        if pending:
            log.info("fetching %d avatars", len(pending))
        progress = Progress("avatars", len(pending), self.progress_interval)
        failed = 0
        for i, url in enumerate(pending, 1):
            resp = self.http.get(url)
            if resp.status_code == 200:
                self.db.save_avatar_data(
                    url, resp.content, resp.headers.get("content-type"), None
                )
                self.stats.avatars += 1
            else:
                failed += 1
                self.db.save_avatar_data(url, None, None, f"HTTP {resp.status_code}")
            progress.step(i, self._file_details(i - failed, failed))

    def _file_details(self, downloaded: int, failed: int) -> str:
        return (
            f"{downloaded} downloaded, {failed} failed, "
            f"{self.http.request_count} requests"
        )


class Progress:
    """Log a progress line at most every ``interval`` seconds, and after the last item.

    Call :meth:`step` after each item; it only checks the clock, so no timer
    thread is needed.
    """

    def __init__(
        self,
        label: str,
        total: int,
        interval: float,
        logger: logging.Logger | None = None,
    ) -> None:
        self.label = label
        self.total = total
        self.interval = interval
        self.log = logger or log
        self.started = self.last_report = time.monotonic()

    def step(self, done: int, details: str = "") -> None:
        now = time.monotonic()
        if now - self.last_report < self.interval and done != self.total:
            return
        self.last_report = now
        eta = (now - self.started) / done * (self.total - done)
        self.log.info(
            "progress: %d/%d %s, %sETA %s",
            done,
            self.total,
            self.label,
            f"{details}, " if details else "",
            _fmt_duration(eta),
        )


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d{h:02d}h{m:02d}m"
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m{s:02d}s"

"""SQLite storage for the forum archive.

All timestamps are stored as ISO 8601 strings in UTC (``YYYY-MM-DDTHH:MM:SSZ``).
Columns ending in ``_html`` hold HTML exactly as rendered by phpBB (relative
URLs are relative to the board URL stored in ``meta.base_url``).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from tech_forum_archiver import parse

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Categories and forums. parent_id is NULL for top-level items.
CREATE TABLE IF NOT EXISTS forums (
    forum_id             INTEGER PRIMARY KEY,
    parent_id            INTEGER REFERENCES forums (forum_id),
    forum_type           TEXT NOT NULL DEFAULT 'forum'
                         CHECK (forum_type IN ('category', 'forum')),
    name                 TEXT NOT NULL,
    description_html     TEXT,
    display_order        INTEGER,
    is_locked            INTEGER NOT NULL DEFAULT 0,
    topic_count          INTEGER,
    post_count           INTEGER,
    last_post_id         INTEGER,
    last_post_at         TEXT,
    first_seen_at        TEXT NOT NULL,
    last_seen_at         TEXT NOT NULL,
    removed_at           TEXT,
    -- crawl state: offset to resume an unfinished full listing walk;
    -- NULL once the topic list has been walked to the end at least once
    listing_resume_start INTEGER DEFAULT 0,
    last_listed_at       TEXT
);

-- Only users who appear somewhere on public pages (as posters, last posters,
-- editors) are known. Profile pages are not visible to guests, so profile
-- details come from the mini-profile shown next to each post.
CREATE TABLE IF NOT EXISTS users (
    user_id             INTEGER PRIMARY KEY,
    username            TEXT NOT NULL,
    username_colour     TEXT,
    rank_title          TEXT,
    rank_image_url      TEXT,
    post_count          INTEGER,
    joined_at           TEXT,
    location            TEXT,
    website             TEXT,
    profile_fields_json TEXT,
    contact_links_json  TEXT,
    avatar_url          TEXT,
    avatar_width        INTEGER,
    avatar_height       INTEGER,
    signature_html      TEXT,
    first_seen_at       TEXT NOT NULL,
    last_seen_at        TEXT NOT NULL,
    profile_seen_at     TEXT
);

CREATE TABLE IF NOT EXISTS topics (
    topic_id              INTEGER PRIMARY KEY,
    forum_id              INTEGER REFERENCES forums (forum_id),
    title                 TEXT NOT NULL,
    topic_type            TEXT NOT NULL DEFAULT 'normal'
                          CHECK (topic_type IN (
                              'normal', 'sticky', 'announce', 'global'
                          )),
    is_locked             INTEGER NOT NULL DEFAULT 0,
    is_hot                INTEGER NOT NULL DEFAULT 0,
    has_poll              INTEGER NOT NULL DEFAULT 0,
    has_attachments       INTEGER NOT NULL DEFAULT 0,
    poster_user_id        INTEGER REFERENCES users (user_id),
    poster_name           TEXT,
    created_at            TEXT,
    reply_count           INTEGER,
    view_count            INTEGER,
    last_post_id          INTEGER,
    last_poster_user_id   INTEGER REFERENCES users (user_id),
    last_poster_name      TEXT,
    last_post_at          TEXT,
    first_seen_at         TEXT NOT NULL,
    last_seen_at          TEXT NOT NULL,
    removed_at            TEXT,
    -- crawl state
    -- needs_fetch: 0 = up to date, 1 = fetch new posts, 2 = refetch every page
    needs_fetch           INTEGER NOT NULL DEFAULT 1,
    -- post count reported by the topic page at last fetch
    archived_post_count   INTEGER,
    archived_last_post_id INTEGER,  -- last post id at last fetch
    last_fetched_at       TEXT,
    fetch_error           TEXT
);
CREATE INDEX IF NOT EXISTS topics_forum ON topics (forum_id, last_post_at);
CREATE INDEX IF NOT EXISTS topics_needs_fetch ON topics (needs_fetch)
    WHERE needs_fetch > 0;

CREATE TABLE IF NOT EXISTS posts (
    post_id                INTEGER PRIMARY KEY,
    topic_id               INTEGER NOT NULL REFERENCES topics (topic_id),
    post_number            INTEGER,  -- 1-based position in the topic when last seen
    poster_user_id         INTEGER REFERENCES users (user_id),
    poster_name            TEXT,
    poster_colour          TEXT,
    subject                TEXT,
    posted_at              TEXT,
    content_html           TEXT NOT NULL,
    content_text           TEXT NOT NULL,
    edit_count             INTEGER NOT NULL DEFAULT 0,
    last_edited_by_user_id INTEGER REFERENCES users (user_id),
    last_edited_by_name    TEXT,
    last_edited_at         TEXT,
    edit_reason            TEXT,
    first_seen_at          TEXT NOT NULL,
    last_seen_at           TEXT NOT NULL,
    -- set when a full refetch no longer finds the post
    removed_at             TEXT
);
CREATE INDEX IF NOT EXISTS posts_topic ON posts (topic_id, posted_at);
CREATE INDEX IF NOT EXISTS posts_poster ON posts (poster_user_id);

-- Earlier versions of posts whose content changed between crawls.
CREATE TABLE IF NOT EXISTS post_revisions (
    revision_id   INTEGER PRIMARY KEY,
    post_id       INTEGER NOT NULL REFERENCES posts (post_id),
    subject       TEXT,
    content_html  TEXT NOT NULL,
    edit_count    INTEGER,
    last_edited_at TEXT,
    archived_from TEXT NOT NULL,  -- first_seen_at / last change of this version
    replaced_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS post_revisions_post ON post_revisions (post_id);

CREATE TABLE IF NOT EXISTS attachments (
    attach_id      INTEGER PRIMARY KEY,
    post_id        INTEGER NOT NULL REFERENCES posts (post_id),
    filename       TEXT,
    comment        TEXT,
    size_text      TEXT,
    download_count INTEGER,
    is_image       INTEGER NOT NULL DEFAULT 0,
    is_inline      INTEGER NOT NULL DEFAULT 0,
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    -- file contents (only with --attachments)
    content_type   TEXT,
    size_bytes     INTEGER,
    data           BLOB,
    fetched_at     TEXT,
    fetch_error    TEXT
);
CREATE INDEX IF NOT EXISTS attachments_post ON attachments (post_id);

CREATE TABLE IF NOT EXISTS avatars (
    url          TEXT PRIMARY KEY,
    content_type TEXT,
    size_bytes   INTEGER,
    data         BLOB,
    fetched_at   TEXT,
    fetch_error  TEXT
);

CREATE TABLE IF NOT EXISTS polls (
    topic_id      INTEGER PRIMARY KEY REFERENCES topics (topic_id),
    question_html TEXT NOT NULL,
    info_text     TEXT,
    total_votes   INTEGER,
    last_seen_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS poll_options (
    topic_id     INTEGER NOT NULL REFERENCES polls (topic_id),
    option_index INTEGER NOT NULL,
    option_id    INTEGER,
    option_html  TEXT NOT NULL,
    votes        INTEGER,
    percent_text TEXT,
    PRIMARY KEY (topic_id, option_index)
);

CREATE TABLE IF NOT EXISTS crawl_runs (
    run_id          INTEGER PRIMARY KEY,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    status          TEXT NOT NULL DEFAULT 'running',
    options_json    TEXT,
    requests        INTEGER,
    forums_listed   INTEGER,
    topics_fetched  INTEGER,
    posts_new       INTEGER,
    posts_changed   INTEGER,
    board_total_posts   INTEGER,
    board_total_topics  INTEGER,
    board_total_members INTEGER,
    newest_member_id    INTEGER
);

-- Full-text search over posts.
CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts USING fts5 (
    subject, content_text, content = 'posts', content_rowid = 'post_id'
);
CREATE TRIGGER IF NOT EXISTS posts_fts_ai AFTER INSERT ON posts BEGIN
    INSERT INTO posts_fts (rowid, subject, content_text)
    VALUES (new.post_id, new.subject, new.content_text);
END;
CREATE TRIGGER IF NOT EXISTS posts_fts_ad AFTER DELETE ON posts BEGIN
    INSERT INTO posts_fts (posts_fts, rowid, subject, content_text)
    VALUES ('delete', old.post_id, old.subject, old.content_text);
END;
CREATE TRIGGER IF NOT EXISTS posts_fts_au
AFTER UPDATE OF subject, content_text ON posts BEGIN
    INSERT INTO posts_fts (posts_fts, rowid, subject, content_text)
    VALUES ('delete', old.post_id, old.subject, old.content_text);
    INSERT INTO posts_fts (rowid, subject, content_text)
    VALUES (new.post_id, new.subject, new.content_text);
END;
"""


def utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Archive:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self.conn.executescript(SCHEMA)
        self.set_meta("schema_version", str(SCHEMA_VERSION))
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    def commit(self) -> None:
        self.conn.commit()

    # ---------------------------------------------------------------- size

    def file_size(self) -> int:
        """Bytes on disk: the archive file plus its write-ahead log, if any."""
        wal = self.path.with_name(self.path.name + "-wal")
        return sum(p.stat().st_size for p in (self.path, wal) if p.exists())

    def unused_bytes(self) -> int:
        """Space inside the file that is free, e.g. after deleting attachment data."""
        page_size = self.conn.execute("PRAGMA page_size").fetchone()[0]
        free_pages = self.conn.execute("PRAGMA freelist_count").fetchone()[0]
        return page_size * free_pages

    def vacuum(self) -> bool:
        """Rebuild the archive file without its unused space.

        In WAL mode the rebuilt pages go to the write-ahead log first, so a
        checkpoint copies them into the main file and truncates both. Returns
        False if another connection kept the checkpoint from finishing; the
        space is then returned when that connection closes.
        """
        self.conn.commit()
        self.conn.execute("VACUUM")
        busy, _, _ = self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        return not busy

    # ------------------------------------------------------------------ meta

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str | None) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # ---------------------------------------------------------------- runs

    def start_run(self, options: dict) -> int:
        cur = self.conn.execute(
            "INSERT INTO crawl_runs (started_at, options_json) VALUES (?, ?)",
            (utcnow(), json.dumps(options, sort_keys=True)),
        )
        self.conn.commit()
        return cur.lastrowid

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        requests: int,
        forums_listed: int,
        topics_fetched: int,
        posts_new: int,
        posts_changed: int,
        board: parse.BoardStats | None,
    ) -> None:
        newest = board.newest_member if board else None
        self.conn.execute(
            """
            UPDATE crawl_runs SET
                finished_at = ?, status = ?, requests = ?, forums_listed = ?,
                topics_fetched = ?, posts_new = ?, posts_changed = ?,
                board_total_posts = ?, board_total_topics = ?,
                board_total_members = ?, newest_member_id = ?
            WHERE run_id = ?
            """,
            (
                utcnow(),
                status,
                requests,
                forums_listed,
                topics_fetched,
                posts_new,
                posts_changed,
                board.total_posts if board else None,
                board.total_topics if board else None,
                board.total_members if board else None,
                newest.user_id if newest else None,
                run_id,
            ),
        )
        self.conn.commit()

    # -------------------------------------------------------------- forums

    def upsert_forum(
        self, f: parse.ForumRow, parent_id: int | None, *, set_parent: bool
    ) -> bool:
        """Insert or update a forum from a forum-list row. Returns True if new."""
        now = utcnow()
        exists = self.conn.execute(
            "SELECT 1 FROM forums WHERE forum_id = ?", (f.forum_id,)
        ).fetchone()
        self.conn.execute(
            """
            INSERT INTO forums (
                forum_id, parent_id, forum_type, name, description_html,
                display_order, is_locked, topic_count, post_count,
                last_post_id, last_post_at, first_seen_at, last_seen_at
            ) VALUES (
                :forum_id, :parent_id, :forum_type, :name, :description_html,
                :display_order, :is_locked, :topic_count, :post_count,
                :last_post_id, :last_post_at, :now, :now
            )
            ON CONFLICT (forum_id) DO UPDATE SET
                parent_id        = CASE WHEN :set_parent
                                        THEN excluded.parent_id ELSE parent_id END,
                forum_type       = excluded.forum_type,
                name             = excluded.name,
                description_html = COALESCE(
                                       excluded.description_html, description_html
                                   ),
                display_order    = COALESCE(excluded.display_order, display_order),
                is_locked        = excluded.is_locked,
                topic_count      = COALESCE(excluded.topic_count, topic_count),
                post_count       = COALESCE(excluded.post_count, post_count),
                last_post_id     = COALESCE(excluded.last_post_id, last_post_id),
                last_post_at     = COALESCE(excluded.last_post_at, last_post_at),
                last_seen_at     = excluded.last_seen_at,
                removed_at       = NULL
            """,
            {
                "forum_id": f.forum_id,
                "parent_id": parent_id,
                "set_parent": set_parent,
                "forum_type": f.forum_type,
                "name": f.name,
                "description_html": f.description_html,
                "display_order": f.display_order,
                "is_locked": int(f.is_locked),
                "topic_count": f.topic_count,
                "post_count": f.post_count,
                "last_post_id": f.last_post_id,
                "last_post_at": f.last_post_at,
                "now": now,
            },
        )
        if f.last_poster and f.last_poster.user_id:
            self.upsert_user_ref(f.last_poster)
        return exists is None

    def update_forum_page(self, page: parse.ForumPage) -> None:
        """Apply information that is only on the forum's own page."""
        parent_id = (
            page.breadcrumb_forum_ids[-2]
            if len(page.breadcrumb_forum_ids) >= 2
            else None
        )
        self.conn.execute(
            """
            UPDATE forums SET
                parent_id = ?,
                name = COALESCE(?, name),
                topic_count = COALESCE(?, topic_count),
                last_seen_at = ?,
                removed_at = NULL
            WHERE forum_id = ?
            """,
            (parent_id, page.name, page.total_topics, utcnow(), page.forum_id),
        )

    def ensure_forum(self, forum_id: int, name: str, parent_id: int | None) -> None:
        now = utcnow()
        self.conn.execute(
            """
            INSERT INTO forums (forum_id, parent_id, name, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?) ON CONFLICT (forum_id) DO NOTHING
            """,
            (forum_id, parent_id, name, now, now),
        )

    def mark_forum_removed(self, forum_id: int) -> None:
        self.conn.execute(
            "UPDATE forums SET removed_at = COALESCE(removed_at, ?) WHERE forum_id = ?",
            (utcnow(), forum_id),
        )

    def forum_type(self, forum_id: int) -> str | None:
        row = self.conn.execute(
            "SELECT forum_type FROM forums WHERE forum_id = ?", (forum_id,)
        ).fetchone()
        return row[0] if row else None

    def forum_listing_state(self, forum_id: int) -> int | None:
        row = self.conn.execute(
            "SELECT listing_resume_start FROM forums WHERE forum_id = ?", (forum_id,)
        ).fetchone()
        return row[0] if row else 0

    def set_forum_listing_state(self, forum_id: int, resume_start: int | None) -> None:
        self.conn.execute(
            "UPDATE forums SET listing_resume_start = ?, last_listed_at = ? "
            "WHERE forum_id = ?",
            (resume_start, utcnow(), forum_id),
        )

    def crawlable_forums(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT forum_id, forum_type, name FROM forums
            WHERE removed_at IS NULL
            ORDER BY COALESCE(display_order, 1e9), forum_id
            """
        ).fetchall()

    def descendant_forum_ids(self, roots: Iterable[int]) -> set[int]:
        roots = list(roots)
        if not roots:
            return set()
        rows = self.conn.execute(
            """
            WITH RECURSIVE tree (forum_id) AS (
                SELECT forum_id FROM forums
                WHERE forum_id IN (SELECT value FROM json_each(?))
                UNION
                SELECT f.forum_id FROM forums f JOIN tree t ON f.parent_id = t.forum_id
            ) SELECT forum_id FROM tree
            """,
            (json.dumps(roots),),
        ).fetchall()
        return {r[0] for r in rows} | set(roots)

    # --------------------------------------------------------------- users

    def upsert_user_ref(self, u: parse.UserRef) -> None:
        """Record a user seen only as a name/link (topic lists, edit notices)."""
        if not u.user_id:
            return
        now = utcnow()
        self.conn.execute(
            """
            INSERT INTO users (
                user_id, username, username_colour, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (user_id) DO UPDATE SET
                username = excluded.username,
                username_colour = excluded.username_colour,
                last_seen_at = excluded.last_seen_at
            """,
            (u.user_id, u.name, u.colour, now, now),
        )

    def upsert_user_profile(
        self, p: parse.PostProfile, signature_html: str | None
    ) -> None:
        if not p.user.user_id:
            return
        now = utcnow()
        self.conn.execute(
            """
            INSERT INTO users (
                user_id, username, username_colour, rank_title, rank_image_url,
                post_count, joined_at, location, website, profile_fields_json,
                contact_links_json, avatar_url, avatar_width, avatar_height,
                signature_html, first_seen_at, last_seen_at, profile_seen_at
            ) VALUES (
                :user_id, :username, :colour, :rank_title, :rank_image_url,
                :post_count, :joined_at, :location, :website, :fields,
                :contacts, :avatar_url, :avatar_width, :avatar_height,
                :signature_html, :now, :now, :now
            )
            ON CONFLICT (user_id) DO UPDATE SET
                username = excluded.username,
                username_colour = excluded.username_colour,
                rank_title = excluded.rank_title,
                rank_image_url = excluded.rank_image_url,
                post_count = excluded.post_count,
                joined_at = COALESCE(excluded.joined_at, joined_at),
                location = excluded.location,
                website = excluded.website,
                profile_fields_json = excluded.profile_fields_json,
                contact_links_json = excluded.contact_links_json,
                avatar_url = excluded.avatar_url,
                avatar_width = excluded.avatar_width,
                avatar_height = excluded.avatar_height,
                -- a post may be shown without its author's signature
                signature_html = COALESCE(excluded.signature_html, signature_html),
                last_seen_at = excluded.last_seen_at,
                profile_seen_at = excluded.profile_seen_at
            """,
            {
                "user_id": p.user.user_id,
                "username": p.user.name,
                "colour": p.user.colour,
                "rank_title": p.rank_title,
                "rank_image_url": p.rank_image_url,
                "post_count": p.post_count,
                "joined_at": p.joined_at,
                "location": p.fields.get("phpbb_location", {}).get("value"),
                "website": p.contacts.get("Website"),
                "fields": json.dumps(p.fields, ensure_ascii=False)
                if p.fields
                else None,
                "contacts": json.dumps(p.contacts, ensure_ascii=False)
                if p.contacts
                else None,
                "avatar_url": p.avatar_url,
                "avatar_width": p.avatar_width,
                "avatar_height": p.avatar_height,
                "signature_html": signature_html,
                "now": now,
            },
        )
        if p.avatar_url:
            self.conn.execute(
                "INSERT INTO avatars (url) VALUES (?) ON CONFLICT (url) DO NOTHING",
                (p.avatar_url,),
            )

    # -------------------------------------------------------------- topics

    def upsert_topic_row(
        self, t: parse.TopicRow, listing_forum_id: int
    ) -> tuple[bool, bool]:
        """Record a topic seen in a forum's topic list.

        Returns ``(listing_changed, queued)``: whether the row differs from the
        last time this topic was listed (new topic, new last post, different
        reply count), and whether this call newly queued it for fetching
        because its posts differ from what is archived.
        """
        now = utcnow()
        row = self.conn.execute(
            "SELECT last_post_id, reply_count, archived_post_count, "
            "archived_last_post_id, needs_fetch, removed_at "
            "FROM topics WHERE topic_id = ?",
            (t.topic_id,),
        ).fetchone()
        if row is None:
            listing_changed = changed = True
        else:
            listing_changed = (
                row["last_post_id"] != t.last_post_id
                or row["reply_count"] != t.reply_count
            )
            changed = (
                row["removed_at"] is not None
                or row["archived_last_post_id"] != t.last_post_id
                or (
                    t.reply_count is not None
                    and row["archived_post_count"] != t.reply_count + 1
                )
            )
        queued = changed and (row is None or row["needs_fetch"] == 0)
        # global announcements are listed in every forum; their real forum comes
        # from the topic page
        forum_id = None if t.topic_type == "global" else listing_forum_id
        self.conn.execute(
            """
            INSERT INTO topics (
                topic_id, forum_id, title, topic_type, is_locked, is_hot, has_poll,
                has_attachments, poster_user_id, poster_name, created_at,
                reply_count, view_count, last_post_id, last_poster_user_id,
                last_poster_name, last_post_at, first_seen_at, last_seen_at,
                needs_fetch
            ) VALUES (
                :topic_id, :forum_id, :title, :topic_type, :is_locked, :is_hot,
                :has_poll, :has_attachments, :poster_user_id, :poster_name,
                :created_at, :reply_count, :view_count, :last_post_id,
                :last_poster_user_id, :last_poster_name, :last_post_at, :now, :now, 1
            )
            ON CONFLICT (topic_id) DO UPDATE SET
                forum_id = COALESCE(excluded.forum_id, forum_id),
                title = excluded.title,
                topic_type = excluded.topic_type,
                is_locked = excluded.is_locked,
                is_hot = excluded.is_hot,
                has_poll = excluded.has_poll,
                has_attachments = excluded.has_attachments,
                poster_user_id = excluded.poster_user_id,
                poster_name = excluded.poster_name,
                created_at = excluded.created_at,
                reply_count = excluded.reply_count,
                view_count = excluded.view_count,
                last_post_id = excluded.last_post_id,
                last_poster_user_id = excluded.last_poster_user_id,
                last_poster_name = excluded.last_poster_name,
                last_post_at = excluded.last_post_at,
                last_seen_at = excluded.last_seen_at,
                removed_at = NULL,
                needs_fetch = CASE WHEN :changed
                                   THEN MAX(needs_fetch, 1) ELSE needs_fetch END
            """,
            {
                "topic_id": t.topic_id,
                "forum_id": forum_id,
                "title": t.title,
                "topic_type": t.topic_type,
                "is_locked": int(t.is_locked),
                "is_hot": int(t.is_hot),
                "has_poll": int(t.has_poll),
                "has_attachments": int(t.has_attachments),
                "poster_user_id": t.poster.user_id if t.poster else None,
                "poster_name": t.poster.name if t.poster else None,
                "created_at": t.created_at,
                "reply_count": t.reply_count,
                "view_count": t.view_count,
                "last_post_id": t.last_post_id,
                "last_poster_user_id": t.last_poster.user_id if t.last_poster else None,
                "last_poster_name": t.last_poster.name if t.last_poster else None,
                "last_post_at": t.last_post_at,
                "now": now,
                "changed": changed,
            },
        )
        for u in (t.poster, t.last_poster):
            if u:
                self.upsert_user_ref(u)
        return listing_changed, queued

    def queue_all_topics(self, forum_ids: set[int] | None) -> int:
        # forum_ids None means every forum
        cur = self.conn.execute(
            """
            UPDATE topics SET needs_fetch = 2
            WHERE removed_at IS NULL
              AND (?1 IS NULL OR forum_id IN (SELECT value FROM json_each(?1)))
            """,
            (json.dumps(sorted(forum_ids)) if forum_ids is not None else None,),
        )
        self.conn.commit()
        return cur.rowcount

    def queue_topic(self, topic_id: int, mode: int) -> None:
        now = utcnow()
        self.conn.execute(
            """
            INSERT INTO topics (
                topic_id, title, first_seen_at, last_seen_at, needs_fetch
            ) VALUES (?, '', ?, ?, ?)
            ON CONFLICT (topic_id) DO UPDATE SET
                needs_fetch = MAX(needs_fetch, excluded.needs_fetch)
            """,
            (topic_id, now, now, mode),
        )
        self.conn.commit()

    def topics_to_fetch(
        self, forum_ids: set[int] | None, limit: int | None
    ) -> list[sqlite3.Row]:
        # forum_ids None means every forum; topics whose forum is not known yet
        # (global announcements) are always included. A limit of None or 0 means all.
        return self.conn.execute(
            """
            SELECT topic_id, title, needs_fetch, reply_count FROM topics
            WHERE needs_fetch > 0 AND removed_at IS NULL
              AND (
                ?1 IS NULL
                OR forum_id IS NULL
                OR forum_id IN (SELECT value FROM json_each(?1))
              )
            ORDER BY last_post_at DESC, topic_id DESC
            LIMIT ?2
            """,
            (
                json.dumps(sorted(forum_ids)) if forum_ids is not None else None,
                limit or -1,
            ),
        ).fetchall()

    def known_post_ids(self, topic_id: int) -> set[int]:
        return {
            r[0]
            for r in self.conn.execute(
                "SELECT post_id FROM posts WHERE topic_id = ?", (topic_id,)
            )
        }

    def apply_topic_page(self, page: parse.TopicPage) -> None:
        """Update topic attributes visible on its own pages."""
        self.conn.execute(
            """
            UPDATE topics SET
                title = COALESCE(?, title),
                forum_id = COALESCE(?, forum_id),
                is_locked = ?,
                has_poll = ?,
                last_seen_at = ?,
                removed_at = NULL
            WHERE topic_id = ?
            """,
            (
                page.title,
                page.forum_id,
                int(page.is_locked),
                int(page.poll is not None),
                utcnow(),
                page.topic_id,
            ),
        )
        crumbs = page.breadcrumbs
        for i, (fid, name) in enumerate(crumbs):
            self.ensure_forum(fid, name, crumbs[i - 1][0] if i else None)
        if page.poll is not None:
            self.save_poll(page.topic_id, page.poll)

    def save_poll(self, topic_id: int, poll: parse.Poll) -> None:
        self.conn.execute(
            """
            INSERT INTO polls (
                topic_id, question_html, info_text, total_votes, last_seen_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (topic_id) DO UPDATE SET
                question_html = excluded.question_html, info_text = excluded.info_text,
                total_votes = excluded.total_votes, last_seen_at = excluded.last_seen_at
            """,
            (topic_id, poll.question_html, poll.info_text, poll.total_votes, utcnow()),
        )
        self.conn.execute("DELETE FROM poll_options WHERE topic_id = ?", (topic_id,))
        self.conn.executemany(
            """
            INSERT INTO poll_options (
                topic_id, option_index, option_id, option_html, votes, percent_text
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (topic_id, i, o.option_id, o.option_html, o.votes, o.percent_text)
                for i, o in enumerate(poll.options)
            ],
        )

    def finish_topic(
        self, topic_id: int, post_count: int | None, last_post_id: int | None
    ) -> None:
        self.conn.execute(
            """
            UPDATE topics SET needs_fetch = 0, archived_post_count = ?,
                   archived_last_post_id = ?, last_fetched_at = ?, fetch_error = NULL
            WHERE topic_id = ?
            """,
            (post_count, last_post_id, utcnow(), topic_id),
        )
        self.conn.commit()

    def mark_topic_removed(self, topic_id: int, reason: str) -> None:
        self.conn.execute(
            """
            UPDATE topics SET
                needs_fetch = 0, removed_at = COALESCE(removed_at, ?), fetch_error = ?
            WHERE topic_id = ?
            """,
            (utcnow(), reason, topic_id),
        )
        self.conn.commit()

    def mark_missing_posts_removed(self, topic_id: int, seen: set[int]) -> int:
        known = self.known_post_ids(topic_id)
        gone = known - seen
        now = utcnow()
        self.conn.executemany(
            "UPDATE posts SET removed_at = COALESCE(removed_at, ?) WHERE post_id = ?",
            [(now, pid) for pid in gone],
        )
        return len(gone)

    # --------------------------------------------------------------- posts

    def save_post(self, topic_id: int, p: parse.Post) -> str:
        """Insert or update a post. Returns 'new', 'changed' or 'same'."""
        now = utcnow()
        old = self.conn.execute(
            "SELECT topic_id, subject, content_html, edit_count, last_edited_at, "
            "first_seen_at, last_seen_at FROM posts WHERE post_id = ?",
            (p.post_id,),
        ).fetchone()
        status = "new"
        if old is not None:
            status = "same"
            if old["content_html"] != p.content_html or old["subject"] != p.subject:
                status = "changed"
                prev = self.conn.execute(
                    "SELECT MAX(replaced_at) FROM post_revisions WHERE post_id = ?",
                    (p.post_id,),
                ).fetchone()[0]
                self.conn.execute(
                    """
                    INSERT INTO post_revisions (
                        post_id, subject, content_html, edit_count, last_edited_at,
                        archived_from, replaced_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        p.post_id,
                        old["subject"],
                        old["content_html"],
                        old["edit_count"],
                        old["last_edited_at"],
                        prev or old["first_seen_at"],
                        now,
                    ),
                )
        edit = p.edit
        self.conn.execute(
            """
            INSERT INTO posts (
                post_id, topic_id, post_number, poster_user_id, poster_name,
                poster_colour, subject, posted_at, content_html, content_text,
                edit_count, last_edited_by_user_id, last_edited_by_name,
                last_edited_at, edit_reason, first_seen_at, last_seen_at
            ) VALUES (
                :post_id, :topic_id, :post_number, :poster_user_id, :poster_name,
                :poster_colour, :subject, :posted_at, :content_html, :content_text,
                :edit_count, :edit_user_id, :edit_name, :edit_at, :edit_reason,
                :now, :now
            )
            ON CONFLICT (post_id) DO UPDATE SET
                topic_id = excluded.topic_id,
                post_number = excluded.post_number,
                poster_user_id = excluded.poster_user_id,
                poster_name = excluded.poster_name,
                poster_colour = excluded.poster_colour,
                subject = excluded.subject,
                posted_at = excluded.posted_at,
                content_html = excluded.content_html,
                content_text = excluded.content_text,
                edit_count = excluded.edit_count,
                last_edited_by_user_id = excluded.last_edited_by_user_id,
                last_edited_by_name = excluded.last_edited_by_name,
                last_edited_at = excluded.last_edited_at,
                edit_reason = excluded.edit_reason,
                last_seen_at = excluded.last_seen_at,
                removed_at = NULL
            """,
            {
                "post_id": p.post_id,
                "topic_id": topic_id,
                "post_number": p.post_number,
                "poster_user_id": p.profile.user.user_id,
                "poster_name": p.profile.user.name,
                "poster_colour": p.profile.user.colour,
                "subject": p.subject,
                "posted_at": p.posted_at,
                "content_html": p.content_html,
                "content_text": p.content_text,
                "edit_count": edit.count if edit else 0,
                "edit_user_id": edit.user.user_id if edit and edit.user else None,
                "edit_name": edit.user.name if edit and edit.user else None,
                "edit_at": edit.edited_at if edit else None,
                "edit_reason": edit.reason if edit else None,
                "now": now,
            },
        )
        self.upsert_user_profile(p.profile, p.signature_html)
        if edit and edit.user:
            self.upsert_user_ref(edit.user)
        for a in p.attachments:
            self.conn.execute(
                """
                INSERT INTO attachments (
                    attach_id, post_id, filename, comment, size_text, download_count,
                    is_image, is_inline, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (attach_id) DO UPDATE SET
                    post_id = excluded.post_id,
                    filename = COALESCE(excluded.filename, filename),
                    comment = COALESCE(excluded.comment, comment),
                    size_text = COALESCE(excluded.size_text, size_text),
                    download_count = COALESCE(excluded.download_count, download_count),
                    is_image = excluded.is_image,
                    is_inline = excluded.is_inline,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    a.attach_id,
                    p.post_id,
                    a.filename,
                    a.comment,
                    a.size_text,
                    a.download_count,
                    int(a.is_image),
                    int(a.is_inline),
                    now,
                    now,
                ),
            )
        return status

    # ------------------------------------------------------- binary files

    def get_attachment(self, attach_id: int) -> sqlite3.Row | None:
        """One attachment, including its file contents if they were downloaded."""
        return self.conn.execute(
            "SELECT attach_id, post_id, filename, size_text, content_type, size_bytes, "
            "data, fetched_at, fetch_error FROM attachments WHERE attach_id = ?",
            (attach_id,),
        ).fetchone()

    def has_attachment_data(self, attach_id: int) -> bool:
        row = self.conn.execute(
            "SELECT data IS NOT NULL FROM attachments WHERE attach_id = ?",
            (attach_id,),
        ).fetchone()
        return bool(row and row[0])

    def reset_attachment(self, attach_id: int) -> None:
        """Forget an attachment's downloaded file (or failed download attempt).

        The attachment itself stays in the archive, as if it had been found but
        not downloaded, so the next ``update --attachments`` downloads it again.
        """
        self.conn.execute(
            """
            UPDATE attachments SET
                data = NULL, content_type = NULL, size_bytes = NULL,
                fetched_at = NULL, fetch_error = NULL
            WHERE attach_id = ?
            """,
            (attach_id,),
        )
        self.conn.commit()

    def attachment_download_summary(self) -> tuple[int, int, int]:
        """How many attachment files are downloaded, their total size in bytes,
        and how many downloads failed."""
        files, size, failed = self.conn.execute(
            "SELECT COUNT(data), COALESCE(SUM(LENGTH(data)), 0), COUNT(fetch_error) "
            "FROM attachments"
        ).fetchone()
        return files, size, failed

    def reset_all_attachments(self) -> None:
        """Forget every downloaded attachment file and failed download attempt."""
        self.conn.execute(
            """
            UPDATE attachments SET
                data = NULL, content_type = NULL, size_bytes = NULL,
                fetched_at = NULL, fetch_error = NULL
            WHERE data IS NOT NULL OR fetched_at IS NOT NULL OR fetch_error IS NOT NULL
            """
        )
        self.conn.commit()

    def pending_attachments(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT attach_id, filename FROM attachments "
            "WHERE fetched_at IS NULL AND fetch_error IS NULL ORDER BY attach_id"
        ).fetchall()

    def save_attachment_data(
        self,
        attach_id: int,
        data: bytes | None,
        content_type: str | None,
        error: str | None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE attachments SET
                data = ?, content_type = ?, size_bytes = ?,
                fetched_at = ?, fetch_error = ?
            WHERE attach_id = ?
            """,
            (
                data,
                content_type,
                len(data) if data is not None else None,
                utcnow() if data is not None else None,
                error,
                attach_id,
            ),
        )
        self.conn.commit()

    def pending_avatars(self) -> list[str]:
        return [
            r[0]
            for r in self.conn.execute(
                "SELECT url FROM avatars "
                "WHERE fetched_at IS NULL AND fetch_error IS NULL"
            )
        ]

    def save_avatar_data(
        self, url: str, data: bytes | None, content_type: str | None, error: str | None
    ) -> None:
        self.conn.execute(
            """
            UPDATE avatars SET
                data = ?, content_type = ?, size_bytes = ?,
                fetched_at = ?, fetch_error = ?
            WHERE url = ?
            """,
            (
                data,
                content_type,
                len(data) if data is not None else None,
                utcnow() if data is not None else None,
                error,
                url,
            ),
        )
        self.conn.commit()

    # --------------------------------------------------------------- stats

    def stats(self) -> dict[str, int]:
        q = self.conn.execute
        return {
            "forums": q(
                "SELECT COUNT(*) FROM forums WHERE removed_at IS NULL"
            ).fetchone()[0],
            "topics": q(
                "SELECT COUNT(*) FROM topics WHERE removed_at IS NULL"
            ).fetchone()[0],
            "topics_pending": q(
                "SELECT COUNT(*) FROM topics "
                "WHERE needs_fetch > 0 AND removed_at IS NULL"
            ).fetchone()[0],
            "posts": q(
                "SELECT COUNT(*) FROM posts WHERE removed_at IS NULL"
            ).fetchone()[0],
            "post_revisions": q("SELECT COUNT(*) FROM post_revisions").fetchone()[0],
            "users": q("SELECT COUNT(*) FROM users").fetchone()[0],
            "attachments": q("SELECT COUNT(*) FROM attachments").fetchone()[0],
            "attachments_downloaded": q(
                "SELECT COUNT(*) FROM attachments WHERE fetched_at IS NOT NULL"
            ).fetchone()[0],
            # approximate: the sum of the rounded sizes the forum lists
            "attachments_listed_bytes": sum(
                parse.size_text_to_bytes(r[0]) or 0
                for r in q("SELECT size_text FROM attachments")
            ),
            "attachments_downloaded_bytes": q(
                "SELECT COALESCE(SUM(size_bytes), 0) FROM attachments "
                "WHERE fetched_at IS NOT NULL"
            ).fetchone()[0],
            "avatars_downloaded": q(
                "SELECT COUNT(*) FROM avatars WHERE fetched_at IS NOT NULL"
            ).fetchone()[0],
            "avatars_downloaded_bytes": q(
                "SELECT COALESCE(SUM(size_bytes), 0) FROM avatars "
                "WHERE fetched_at IS NOT NULL"
            ).fetchone()[0],
            "polls": q("SELECT COUNT(*) FROM polls").fetchone()[0],
        }

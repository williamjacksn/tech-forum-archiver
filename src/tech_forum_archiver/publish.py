"""Publish the archive as a static HTML site.

Layout of the output directory::

    index.html          forum index
    style.css
    f/<forum id>.html   forum: subforums and every topic
    t/<topic id>.html   topic: every post on one page (posts have #p<post id> anchors)
    u/index.html        members with archived posts
    u/<user id>.html    member profile and their posts
    files/<attach id>/<name>   downloaded attachments
    avatars/<name>      downloaded avatars: uploaded ones keep the forum's file
                        name, others (e.g. Gravatar) are named remote-<hash>.<ext>

Files are only rewritten when their content changes, so publishing again
after an update touches only what changed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import re
from collections import defaultdict
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from tech_forum_archiver import pages, search_index
from tech_forum_archiver.crawler import Progress
from tech_forum_archiver.db import Archive
from tech_forum_archiver.fetch import DEFAULT_BASE_URL
from tech_forum_archiver.links import LinkMap
from tech_forum_archiver.pages import ForumView, LastPost, Site, TopicView

log = logging.getLogger(__name__)

PROGRESS_INTERVAL = 30.0
TYPE_ORDER = {"global": 0, "announce": 0, "sticky": 1, "normal": 2}


@dataclass
class PublishStats:
    written: int = 0
    unchanged: int = 0
    forums: int = 0
    topics: int = 0
    users: int = 0
    files: int = 0


def _safe_name(name: str | None, fallback: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name or "").strip("._")
    return safe[:120] or fallback


def uploaded_avatar_name(url: str) -> str | None:
    """The forum's file name for an uploaded avatar, e.g. ``91_1355256303.jpg``."""
    if "file.php" not in urlsplit(url).path:
        return None
    return parse_qs(urlsplit(url).query).get("avatar", [None])[0]


def avatar_path(url: str, content_type: str | None) -> str:
    """Where a downloaded avatar goes in the site."""
    if name := uploaded_avatar_name(url):
        return f"avatars/{_safe_name(name, 'avatar')}"
    # Gravatar and other remote images have no file name of their own
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    mime = (content_type or "").split(";")[0].strip().lower()
    ext = (mimetypes.guess_extension(mime) or "") if mime else ""
    return f"avatars/remote-{digest}{ext}"


class Publisher:
    def __init__(
        self,
        archive: Archive,
        out_dir: Path,
        title: str = "Tech Forum Archive",
        *,
        search: bool = True,
    ) -> None:
        self.db = archive.conn
        self.archive = archive
        self.out = Path(out_dir)
        self.stats = PublishStats()
        base_url = archive.get_meta("base_url") or DEFAULT_BASE_URL
        self.links = LinkMap(base_url=base_url)
        updated = self.db.execute(
            "SELECT MAX(finished_at) FROM crawl_runs WHERE status = 'complete'"
        ).fetchone()[0]
        self.site = Site(
            title=title,
            links=self.links,
            original_url=self.links.base_url,
            updated_at=updated,
            search=search,
        )

    # ------------------------------------------------------------- output

    def write(self, rel: str, content: str | bytes) -> None:
        path = self.out / rel
        data = content.encode("utf-8") if isinstance(content, str) else content
        try:
            if path.stat().st_size == len(data) and path.read_bytes() == data:
                self.stats.unchanged += 1
                return
        except FileNotFoundError:
            path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self.stats.written += 1

    # -------------------------------------------------------------- run

    def run(self) -> PublishStats:
        self.out.mkdir(parents=True, exist_ok=True)
        log.info("publishing to %s", self.out.resolve())
        self.load_link_targets()
        self.write_files()
        self.write(
            "style.css",
            (resources.files(__package__) / "static" / "style.css").read_bytes(),
        )
        top, by_id = self.forum_tree()
        self.write("index.html", str(pages.index_page(self.site, top)))
        self.write_forums(by_id)
        self.write_topics(by_id)
        self.write_users()
        if self.site.search:
            self.write_search()
        log.info(
            "published %d forums, %d topics, %d members, %d files: "
            "%d files written, %d unchanged",
            self.stats.forums,
            self.stats.topics,
            self.stats.users,
            self.stats.files,
            self.stats.written,
            self.stats.unchanged,
        )
        return self.stats

    def load_link_targets(self) -> None:
        q = self.db.execute
        self.links.post_topics = dict(
            q("SELECT post_id, topic_id FROM posts").fetchall()
        )
        self.links.topics = set(self.links.post_topics.values())
        self.links.forums = {r[0] for r in q("SELECT forum_id FROM forums")}
        self.links.users = {
            r[0]
            for r in q(
                "SELECT DISTINCT p.poster_user_id FROM posts p "
                "JOIN users u ON u.user_id = p.poster_user_id"
            )
        }
        for r in q(
            "SELECT attach_id, filename FROM attachments WHERE data IS NOT NULL"
        ):
            name = _safe_name(r["filename"], f"attachment-{r['attach_id']}")
            self.links.attachments[r["attach_id"]] = f"files/{r['attach_id']}/{name}"
        for r in q("SELECT url, content_type FROM avatars WHERE data IS NOT NULL"):
            path = avatar_path(r["url"], r["content_type"])
            self.links.avatars[r["url"]] = path
            if name := uploaded_avatar_name(r["url"]):
                self.links.avatar_names[name] = path

    def write_files(self) -> None:
        for attach_id, path in self.links.attachments.items():
            data = self.db.execute(
                "SELECT data FROM attachments WHERE attach_id = ?", (attach_id,)
            ).fetchone()[0]
            self.write(path, data)
            self.stats.files += 1
        for r in self.db.execute(
            "SELECT url, data FROM avatars WHERE data IS NOT NULL"
        ):
            self.write(self.links.avatars[r["url"]], r["data"])
            self.stats.files += 1

    # ----------------------------------------------------------- forums

    def topic_activity(self) -> tuple[dict[int, int], dict[int, LastPost]]:
        """Archived post counts and last archived post for every topic."""
        titles = dict(self.db.execute("SELECT topic_id, title FROM topics").fetchall())
        counts: dict[int, int] = {}
        last: dict[int, LastPost] = {}
        for r in self.db.execute(
            """
            SELECT topic_id, COUNT(*) AS n, MAX(posted_at) AS at,
                   post_id, poster_user_id, poster_name
            FROM posts GROUP BY topic_id
            """
        ):
            counts[r["topic_id"]] = r["n"]
            last[r["topic_id"]] = LastPost(
                topic_id=r["topic_id"],
                topic_title=titles.get(r["topic_id"]) or f"Topic {r['topic_id']}",
                post_id=r["post_id"],
                poster_id=r["poster_user_id"]
                if r["poster_user_id"] in self.links.users
                else None,
                poster_name=r["poster_name"],
                posted_at=r["at"],
            )
        return counts, last

    def forum_tree(self) -> tuple[list[ForumView], dict[int, ForumView]]:
        self.post_counts, self.last_posts = self.topic_activity()
        by_id: dict[int, ForumView] = {}
        for r in self.db.execute(
            "SELECT * FROM forums ORDER BY COALESCE(display_order, 1e9), forum_id"
        ):
            by_id[r["forum_id"]] = ForumView(
                forum_id=r["forum_id"],
                name=r["name"],
                forum_type=r["forum_type"],
                description_html=r["description_html"],
                parent_id=r["parent_id"],
            )
        top: list[ForumView] = []
        for f in by_id.values():
            parent = by_id.get(f.parent_id) if f.parent_id is not None else None
            (parent.children if parent else top).append(f)
        # archived topics and posts per forum, then roll up into parents
        for r in self.db.execute(
            "SELECT topic_id, forum_id FROM topics WHERE forum_id IS NOT NULL"
        ):
            f = by_id.get(r["forum_id"])
            n = self.post_counts.get(r["topic_id"])
            if f is None or not n:
                continue
            f.topics += 1
            f.posts += n
            lp = self.last_posts[r["topic_id"]]
            if f.last is None or (lp.posted_at or "") > (f.last.posted_at or ""):
                f.last = lp

        def roll_up(f: ForumView) -> None:
            for c in f.children:
                roll_up(c)
                f.topics += c.topics
                f.posts += c.posts
                if c.last and (
                    f.last is None
                    or (c.last.posted_at or "") > (f.last.posted_at or "")
                ):
                    f.last = c.last

        for f in top:
            roll_up(f)
        return top, by_id

    @staticmethod
    def ancestors(by_id: dict[int, ForumView], forum_id: int | None) -> list[ForumView]:
        trail: list[ForumView] = []
        seen: set[int] = set()
        while forum_id is not None and forum_id in by_id and forum_id not in seen:
            seen.add(forum_id)
            trail.append(by_id[forum_id])
            forum_id = by_id[forum_id].parent_id
        return list(reversed(trail))

    def write_forums(self, by_id: dict[int, ForumView]) -> None:
        topics_by_forum: dict[int, list[TopicView]] = defaultdict(list)
        activity: dict[int, str] = {}
        for r in self.db.execute("SELECT * FROM topics WHERE forum_id IS NOT NULL"):
            tid = r["topic_id"]
            last = self.last_posts.get(tid)
            if last is None and r["last_post_id"]:
                # not archived yet: show the last post as listed on the forum (unlinked)
                last = LastPost(
                    topic_id=tid,
                    topic_title=r["title"],
                    post_id=r["last_post_id"],
                    poster_id=(
                        r["last_poster_user_id"]
                        if r["last_poster_user_id"] in self.links.users
                        else None
                    ),
                    poster_name=r["last_poster_name"],
                    posted_at=r["last_post_at"],
                )
            activity[tid] = max(
                (last.posted_at or "") if last else "", r["last_post_at"] or ""
            )
            topics_by_forum[r["forum_id"]].append(
                TopicView(
                    topic_id=tid,
                    title=r["title"] or f"Topic {tid}",
                    topic_type=r["topic_type"],
                    is_locked=bool(r["is_locked"]),
                    removed=r["removed_at"] is not None,
                    archived_posts=self.post_counts.get(tid, 0),
                    reply_count=r["reply_count"],
                    view_count=r["view_count"],
                    poster_id=r["poster_user_id"]
                    if r["poster_user_id"] in self.links.users
                    else None,
                    poster_name=r["poster_name"],
                    created_at=r["created_at"],
                    last=last,
                )
            )
        for fid, forum in by_id.items():
            # announcements, then stickies, then everything else; newest activity first
            topics = sorted(
                topics_by_forum.get(fid, []),
                key=lambda t: activity[t.topic_id],
                reverse=True,
            )
            topics.sort(key=lambda t: TYPE_ORDER.get(t.topic_type, 2))
            page = pages.forum_page(
                self.site, forum, self.ancestors(by_id, forum.parent_id), topics
            )
            self.write(f"f/{fid}.html", str(page))
            self.stats.forums += 1

    # ----------------------------------------------------------- topics

    def write_topics(self, by_id: dict[int, ForumView]) -> None:
        topic_ids = sorted(self.links.topics)
        log.info("writing %d topic pages", len(topic_ids))
        progress = Progress("topic pages", len(topic_ids), PROGRESS_INTERVAL, log)
        for i, tid in enumerate(topic_ids, 1):
            self.write_topic(tid, by_id)
            progress.step(i)

    def write_topic(self, tid: int, by_id: dict[int, ForumView]) -> None:
        q = self.db.execute
        topic = q("SELECT * FROM topics WHERE topic_id = ?", (tid,)).fetchone()
        if topic is None:
            return
        posts = q(
            "SELECT * FROM posts WHERE topic_id = ? ORDER BY posted_at, post_id", (tid,)
        ).fetchall()
        user_ids = {
            p["poster_user_id"] for p in posts if p["poster_user_id"] is not None
        }
        users = {
            u["user_id"]: u
            for u in q(
                "SELECT * FROM users WHERE user_id IN (SELECT value FROM json_each(?))",
                (json.dumps(sorted(user_ids)),),
            )
        }
        attachments: dict[int, list] = defaultdict(list)
        for a in q(
            "SELECT a.attach_id, a.post_id, a.filename, a.size_text, a.is_image, "
            "a.is_inline FROM attachments a JOIN posts p ON p.post_id = a.post_id "
            "WHERE p.topic_id = ? ORDER BY a.attach_id",
            (tid,),
        ):
            attachments[a["post_id"]].append(a)
        poll = q("SELECT * FROM polls WHERE topic_id = ?", (tid,)).fetchone()
        options = (
            q(
                "SELECT * FROM poll_options WHERE topic_id = ? ORDER BY option_index",
                (tid,),
            ).fetchall()
            if poll
            else []
        )
        page = pages.topic_page(
            self.site,
            topic,
            self.ancestors(by_id, topic["forum_id"]),
            posts,
            users,
            attachments,
            poll,
            options,
        )
        self.write(f"t/{tid}.html", str(page))
        self.stats.topics += 1

    # ------------------------------------------------------------ users

    def write_users(self) -> None:
        titles = dict(self.db.execute("SELECT topic_id, title FROM topics").fetchall())
        members = self.db.execute(
            """
            SELECT u.user_id, u.username, u.rank_title, u.joined_at,
                   COUNT(*) AS posts, MAX(p.posted_at) AS last_post_at
            FROM posts p JOIN users u ON u.user_id = p.poster_user_id
            GROUP BY u.user_id ORDER BY posts DESC, u.username COLLATE NOCASE
            """
        ).fetchall()
        self.write("u/index.html", str(pages.members_page(self.site, members)))
        log.info("writing %d member pages", len(members))
        progress = Progress("member pages", len(members), PROGRESS_INTERVAL, log)
        for i, m in enumerate(members, 1):
            user = self.db.execute(
                "SELECT * FROM users WHERE user_id = ?", (m["user_id"],)
            ).fetchone()
            posts = self.db.execute(
                "SELECT post_id, topic_id, posted_at FROM posts "
                "WHERE poster_user_id = ? ORDER BY posted_at DESC, post_id DESC",
                (m["user_id"],),
            ).fetchall()
            self.write(
                f"u/{m['user_id']}.html",
                str(pages.user_page(self.site, user, posts, titles)),
            )
            self.stats.users += 1
            progress.step(i)

    # ----------------------------------------------------------- search

    def write_search(self) -> None:
        total = self.db.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        log.info("indexing %d posts for search", total)
        progress = Progress("posts indexed", total, PROGRESS_INTERVAL, log)
        index = search_index.build(self.db, progress.step)
        log.info(
            "writing the search index: %d words, %d very common words left out",
            len(index.words),
            len(index.common_words),
        )
        for path, content in search_index.files(index):
            self.write(path, content)
        self.write(
            "search.js",
            (resources.files(__package__) / "static" / "search.js").read_bytes(),
        )
        self.write("search.html", str(pages.search_page(self.site)))


def publish(
    archive: Archive,
    out_dir: Path,
    title: str = "Tech Forum Archive",
    *,
    search: bool = True,
) -> PublishStats:
    return Publisher(archive, out_dir, title, search=search).run()

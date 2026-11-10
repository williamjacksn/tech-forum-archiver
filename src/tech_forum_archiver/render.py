"""Render archived topics as plain text for reading in a terminal."""

from __future__ import annotations

import re
import textwrap
from datetime import datetime
from sqlite3 import Row

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from tech_forum_archiver.db import Archive

BLOCK_TAGS = {
    "p", "div", "dl", "dt", "dd", "table", "tr", "hr",
    "h1", "h2", "h3", "h4", "h5", "h6",
}  # fmt: skip


class _Renderer:
    """Turn phpBB post HTML into wrapped text. Quotes become ``> `` blocks, code blocks
    are indented and left unwrapped, and links show their target when it differs from
    the link text."""

    def __init__(self, width: int) -> None:
        self.width = max(width, 20)
        self.blocks: list[str] = []
        self.inline: list[str] = []

    def render(self, node: Tag) -> str:
        self.walk(node)
        self.flush()
        text = "\n".join(self.blocks)
        return re.sub(r"\n{3,}", "\n\n", text).strip("\n")

    def flush(self) -> None:
        text = "".join(self.inline)
        self.inline.clear()
        if not text.strip():
            return
        for line in text.split("\n"):
            line = re.sub(r"[ \t\r\xa0]+", " ", line).strip()
            self.blocks.append(textwrap.fill(line, self.width) if line else "")

    def block(self, text: str) -> None:
        self.flush()
        if text:
            self.blocks.append(text)

    def blank(self) -> None:
        self.flush()
        self.blocks.append("")

    def sub(self, node: Tag, width: int) -> str:
        return _Renderer(width).render(node)

    def walk(self, node: Tag) -> None:
        for child in node.children:
            if isinstance(child, Comment):
                continue
            if isinstance(child, NavigableString):
                self.inline.append(str(child))
            elif isinstance(child, Tag):
                self.element(child)

    def element(self, el: Tag) -> None:
        name = el.name
        classes = el.get("class", [])
        if name == "br":
            self.inline.append("\n")
        elif name == "blockquote":
            inner = self.sub(el, self.width - 2)
            self.block(
                "\n".join(f"> {line}" if line else ">" for line in inner.split("\n"))
            )
            self.blank()
        elif name == "cite":
            # "name wrote: ↑ date" -> "name wrote (date):"
            date = el.select_one(".responsive-hide")
            date_text = date.get_text(" ", strip=True) if date else ""
            if date:
                date.decompose()
            who = re.sub(r"\s+", " ", el.get_text(" ")).replace("↑", "").strip()
            who = who.removesuffix(":").removesuffix(" wrote").strip()
            line = f"{who} wrote" if who else "Quote"
            self.block(f"{line} ({date_text}):" if date_text else f"{line}:")
        elif (name == "div" and "codebox" in classes) or name == "pre":
            code = el.find("code") or el.find("pre") or el
            text = code.get_text().rstrip("\n")
            self.block(textwrap.indent(text, "    ", lambda _: True))
            self.blank()
        elif name in ("ul", "ol"):
            self.flush()
            for i, li in enumerate(el.find_all("li", recursive=False), 1):
                bullet = f"{i}. " if name == "ol" else "* "
                inner = self.sub(li, self.width - len(bullet))
                lines = inner.split("\n")
                pad = " " * len(bullet)
                self.blocks.append(
                    "\n".join([bullet + lines[0], *(pad + ln for ln in lines[1:])])
                )
        elif name == "img":
            alt = el.get("alt") or ""
            if "smilies" in classes:
                self.inline.append(alt)
            else:
                self.inline.append(f"[image: {alt or el.get('src', '')}]")
        elif name == "a":
            text = el.get_text()
            href = el.get("href", "")
            self.walk(el)
            if (
                href
                and not href.startswith("#")
                and href.strip() != text.strip()
                and not el.find("img")
            ):
                self.inline.append(f" <{href}>")
        elif name in ("script", "style"):
            return
        elif name in BLOCK_TAGS or name == "li":
            self.flush()
            self.walk(el)
            self.flush()
        else:  # inline formatting: strong, em, span, u, ...
            self.walk(el)


def render_html(html: str, width: int = 80) -> str:
    return _Renderer(width).render(BeautifulSoup(html, "html.parser"))


def local_time(iso: str | None) -> str:
    if not iso:
        return "unknown date"
    dt = datetime.fromisoformat(iso).astimezone()
    return dt.strftime("%Y-%m-%d %H:%M")


def _forum_path(archive: Archive, forum_id: int | None) -> list[str]:
    names: list[str] = []
    seen: set[int] = set()
    while forum_id is not None and forum_id not in seen:
        seen.add(forum_id)
        row = archive.conn.execute(
            "SELECT name, parent_id FROM forums WHERE forum_id = ?", (forum_id,)
        ).fetchone()
        if row is None:
            break
        names.append(row["name"])
        forum_id = row["parent_id"]
    return list(reversed(names))


def _attachment_line(a: Row) -> str:
    """For example ``  - ID 5925: photo.jpg (339.4 KiB), shown in the post``."""
    line = f"  - ID {a['attach_id']}: {a['filename'] or 'unnamed file'}"
    if a["size_text"]:
        line += f" ({a['size_text']})"
    notes = []
    if a["is_inline"]:
        notes.append("shown in the post")
    if not a["downloaded"]:
        notes.append("not downloaded")
    return line + "".join(f", {n}" for n in notes)


def format_topic(archive: Archive, topic_id: int, width: int = 80) -> str | None:
    """The whole topic as text, or None if the topic is not in the archive."""
    db = archive.conn
    topic = db.execute(
        "SELECT * FROM topics WHERE topic_id = ?", (topic_id,)
    ).fetchone()
    if topic is None:
        return None
    base = (archive.get_meta("base_url") or "").rstrip("/")
    posts = db.execute(
        """
        SELECT p.*, u.rank_title
        FROM posts p LEFT JOIN users u ON u.user_id = p.poster_user_id
        WHERE p.topic_id = ? ORDER BY p.posted_at, p.post_id
        """,
        (topic_id,),
    ).fetchall()
    attachments: dict[int, list] = {}
    for a in db.execute(
        """
        SELECT a.attach_id, a.post_id, a.filename, a.size_text, a.is_inline,
               a.fetched_at IS NOT NULL AS downloaded
        FROM attachments a JOIN posts p ON p.post_id = a.post_id
        WHERE p.topic_id = ? ORDER BY a.attach_id
        """,
        (topic_id,),
    ):
        attachments.setdefault(a["post_id"], []).append(a)

    rule = "=" * width
    out: list[str] = []
    path = _forum_path(archive, topic["forum_id"])
    if path:
        out.append(" > ".join(path))
    out.append(textwrap.fill(topic["title"] or f"Topic {topic_id}", width))
    flags = [f"topic {topic_id}", f"{len(posts)} posts archived"]
    if topic["topic_type"] != "normal":
        flags.append(topic["topic_type"])
    if topic["is_locked"]:
        flags.append("locked")
    if topic["removed_at"]:
        flags.append(f"removed from forum {local_time(topic['removed_at'])}")
    out.append(", ".join(flags))
    if base:
        out.append(f"{base}/viewtopic.php?t={topic_id}")
    out.append(rule)

    poll = db.execute("SELECT * FROM polls WHERE topic_id = ?", (topic_id,)).fetchone()
    if poll is not None:
        out.append("Poll: " + render_html(poll["question_html"], width - 6))
        for o in db.execute(
            "SELECT * FROM poll_options WHERE topic_id = ? ORDER BY option_index",
            (topic_id,),
        ):
            votes = f"{o['votes']} votes" if o["votes"] is not None else ""
            pct = f" ({o['percent_text']})" if o["percent_text"] else ""
            out.append(f"  - {render_html(o['option_html'], width)}: {votes}{pct}")
        if poll["total_votes"] is not None:
            out.append(f"  Total votes: {poll['total_votes']}")
        out.append(rule)

    if not posts:
        out.append("(no posts archived for this topic yet; run `update` to fetch them)")
        return "\n".join(out) + "\n"

    title = topic["title"] or ""
    for i, p in enumerate(posts):
        number = p["post_number"] or i + 1
        who = p["poster_name"] or "unknown"
        if p["rank_title"]:
            who += f" ({p['rank_title']})"
        out.append(f"#{number}  {who}  {local_time(p['posted_at'])}")
        if p["edit_count"]:
            times = "time" if p["edit_count"] == 1 else "times"
            edit = f"edited {p['edit_count']} {times}"
            if p["last_edited_by_name"]:
                edit += f", last by {p['last_edited_by_name']}"
            if p["last_edited_at"]:
                edit += f" on {local_time(p['last_edited_at'])}"
            if p["edit_reason"]:
                edit += f"; reason: {p['edit_reason']}"
            out.append(f"({edit})")
        if p["removed_at"]:
            out.append(f"[no longer on the forum since {local_time(p['removed_at'])}]")
        subject = p["subject"] or ""
        if subject and subject not in (title, f"Re: {title}"):
            out.append(f"Subject: {subject}")
        out.append("")
        out.append(render_html(p["content_html"], width) or "(empty post)")
        files = attachments.get(p["post_id"], [])
        if files:
            out.append("")
            out.append(
                textwrap.fill(
                    "Attachments (export one with `export-attachment ID`):", width
                )
            )
            out.extend(
                textwrap.fill(
                    _attachment_line(a),
                    width,
                    subsequent_indent="    ",
                    break_long_words=False,
                    break_on_hyphens=False,
                )
                for a in files
            )
        out.append("")
        out.append("-" * width if i < len(posts) - 1 else rule)
    return "\n".join(out) + "\n"

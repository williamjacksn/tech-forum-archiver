"""Parsers for phpBB 3.3 (prosilver-based) pages as served to guests."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, NavigableString, Tag

# Times without a machine-readable datetime attribute (join dates, edit
# notices) are rendered in the board's default timezone for guests.
BOARD_TZ = ZoneInfo("America/Denver")
LOCAL_TIME_FORMAT = "%a %b %d, %Y %I:%M %p"
LOCAL_TIME_RE = r"[A-Z][a-z]{2} [A-Z][a-z]{2} \d{1,2}, \d{4} \d{1,2}:\d{2} [ap]m"


def set_board_timezone(name: str) -> None:
    global BOARD_TZ
    BOARD_TZ = ZoneInfo(name)


# ---------------------------------------------------------------- dataclasses


@dataclass
class UserRef:
    user_id: int | None  # None for guests and deleted users
    name: str
    colour: str | None = None


@dataclass
class ForumRow:
    forum_id: int
    name: str
    forum_type: str = "forum"  # category | forum
    description_html: str | None = None
    display_order: int | None = None
    is_locked: bool = False
    topic_count: int | None = None
    post_count: int | None = None
    last_post_id: int | None = None
    last_post_at: str | None = None
    last_poster: UserRef | None = None
    parent_id: int | None = None  # as implied by the page layout


@dataclass
class TopicRow:
    topic_id: int
    title: str
    topic_type: str = "normal"  # normal | sticky | announce | global
    is_locked: bool = False
    is_hot: bool = False
    is_moved: bool = False
    has_poll: bool = False
    has_attachments: bool = False
    poster: UserRef | None = None
    created_at: str | None = None
    reply_count: int | None = None
    view_count: int | None = None
    last_post_id: int | None = None
    last_poster: UserRef | None = None
    last_post_at: str | None = None


@dataclass
class BoardStats:
    total_posts: int | None = None
    total_topics: int | None = None
    total_members: int | None = None
    newest_member: UserRef | None = None
    timezone: str | None = None


@dataclass
class IndexPage:
    forums: list[ForumRow]
    stats: BoardStats


@dataclass
class ForumPage:
    forum_id: int | None
    name: str | None
    breadcrumbs: list[tuple[int, str]]
    total_topics: int | None
    next_start: int | None
    subforums: list[ForumRow]
    topics: list[TopicRow]

    @property
    def breadcrumb_forum_ids(self) -> list[int]:
        return [fid for fid, _ in self.breadcrumbs]


@dataclass
class PostProfile:
    user: UserRef
    rank_title: str | None = None
    rank_image_url: str | None = None
    post_count: int | None = None
    joined_at: str | None = None
    avatar_url: str | None = None
    avatar_width: int | None = None
    avatar_height: int | None = None
    fields: dict[str, dict[str, str]] = field(default_factory=dict)
    contacts: dict[str, str] = field(default_factory=dict)


@dataclass
class EditInfo:
    count: int
    user: UserRef | None
    edited_at: str | None
    reason: str | None


@dataclass
class Attachment:
    attach_id: int
    filename: str | None = None
    comment: str | None = None
    size_text: str | None = None
    download_count: int | None = None
    is_image: bool = False
    is_inline: bool = False


@dataclass
class Post:
    post_id: int
    post_number: int | None
    subject: str | None
    posted_at: str | None
    profile: PostProfile
    content_html: str
    content_text: str
    signature_html: str | None
    edit: EditInfo | None
    attachments: list[Attachment]


@dataclass
class PollOption:
    option_id: int | None
    option_html: str
    votes: int | None
    percent_text: str | None


@dataclass
class Poll:
    question_html: str
    info_text: str | None
    total_votes: int | None
    options: list[PollOption]


@dataclass
class TopicPage:
    topic_id: int | None
    title: str | None
    breadcrumbs: list[tuple[int, str]]
    total_posts: int | None
    next_start: int | None
    is_locked: bool
    poll: Poll | None
    posts: list[Post]

    @property
    def forum_id(self) -> int | None:
        return self.breadcrumbs[-1][0] if self.breadcrumbs else None


# -------------------------------------------------------------------- helpers


def soupify(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def query_int(href: str | None, key: str) -> int | None:
    if not href:
        return None
    values = parse_qs(urlsplit(href).query).get(key)
    if not values:
        return None
    try:
        return int(values[0])
    except ValueError:
        return None


def first_int(text: str | None) -> int | None:
    if not text:
        return None
    m = re.search(r"\d[\d,]*", text)
    return int(m.group(0).replace(",", "")) if m else None


def iso_utc(value: str | None) -> str | None:
    """Normalise a phpBB ``<time datetime>`` value to UTC ``...Z``."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_to_utc(text: str | None) -> str | None:
    """Convert a board-formatted local time such as ``Sat Oct 06, 2007 9:28 pm``."""
    if not text:
        return None
    m = re.search(LOCAL_TIME_RE, text)
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group(0), LOCAL_TIME_FORMAT).replace(tzinfo=BOARD_TZ)
    except ValueError:
        return None
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def time_attr(el: Tag | None) -> str | None:
    if el is None:
        return None
    t = el if el.name == "time" else el.find("time")
    return iso_utc(t.get("datetime")) if t else None


def clean_text(el: Tag | NavigableString | None) -> str:
    if el is None:
        return ""
    return re.sub(
        r"\s+", " ", el.get_text(" ") if isinstance(el, Tag) else str(el)
    ).strip()


def inner_html(el: Tag) -> str:
    return el.decode_contents().strip()


BLOCK_TAGS = [
    "p", "div", "blockquote", "li", "ul", "ol", "pre", "tr", "table", "dl", "dt", "dd",
    "h1", "h2", "h3", "h4", "h5", "h6", "cite",
]  # fmt: skip


def html_to_text(el: Tag) -> str:
    """Plain-text rendering of post HTML, used for full-text search."""
    el = copy.copy(el)
    for br in el.find_all("br"):
        br.replace_with("\n")
    for img in el.find_all("img"):
        alt = img.get("alt")
        img.replace_with(f" {alt} " if alt else " ")
    for blk in el.find_all(BLOCK_TAGS):
        blk.insert_before("\n")
        blk.insert_after("\n")
    text = el.get_text()
    lines = [re.sub(r"[ \t\xa0]+", " ", line).strip() for line in text.splitlines()]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def user_ref(el: Tag | None) -> UserRef | None:
    """Parse a phpBB username element.

    For example ``a.username`` or ``span.username-coloured``.
    """
    if el is None:
        return None
    if not ({"username", "username-coloured"} & set(el.get("class", []))):
        el = el.select_one(".username, .username-coloured")
        if el is None:
            return None
    name = clean_text(el)
    if not name:
        return None
    colour = None
    m = re.search(r"color:\s*(#[0-9A-Fa-f]{3,6})", el.get("style", ""))
    if m:
        colour = m.group(1).upper()
    user_id = query_int(el.get("href"), "u") if el.name == "a" else None
    return UserRef(user_id=user_id, name=name, colour=colour)


def breadcrumbs(soup: BeautifulSoup) -> list[tuple[int, str]]:
    crumbs: list[tuple[int, str]] = []
    for c in soup.select("#nav-breadcrumbs span.crumb[data-forum-id]"):
        try:
            fid = int(c["data-forum-id"])
        except ValueError:
            continue
        crumbs.append((fid, clean_text(c)))
    return crumbs


def top_pagination(soup: BeautifulSoup) -> tuple[int | None, int | None]:
    """Return (item count, next page start) from the top action bar."""
    bar = soup.select_one("div.action-bar.bar-top")
    if bar is None:
        return None, None
    pag = bar.select_one("div.pagination")
    total = None
    if pag is not None:
        total = first_int(pag.find(string=True, recursive=False) or clean_text(pag))
    nxt = bar.select_one('a[rel="next"]')
    next_start = query_int(nxt.get("href"), "start") if nxt else None
    return total, next_start


# ----------------------------------------------------------------- forum lists


def _forum_description(title: Tag) -> str | None:
    """HTML between the forum title link and the subforum list / hidden mobile block."""
    parts: list[str] = []
    for node in title.next_siblings:
        if isinstance(node, Tag) and (
            node.name in ("div", "strong") or "subforum" in node.get("class", [])
        ):
            break
        parts.append(str(node))
    html = "".join(parts)
    html = re.sub(r"^(?:\s|<br\s*/?>)+|(?:\s|<br\s*/?>)+$", "", html)
    return html or None


def parse_forum_row(
    row: Tag, order: int | None, parent_id: int | None
) -> ForumRow | None:
    dl = row.find("dl")
    title = row.select_one("a.forumtitle")
    if dl is None or title is None:
        return None
    classes = " ".join(dl.get("class", []))
    if "forum_link" in classes:
        # Link forums only expose their target URL, not their own id (here they point at
        # other forums of this board), so they cannot be stored reliably.
        return None
    forum_id = query_int(title.get("href"), "f")
    if forum_id is None:
        return None
    f = ForumRow(
        forum_id=forum_id,
        name=clean_text(title),
        description_html=_forum_description(title),
        display_order=order,
        is_locked="locked" in classes,
        parent_id=parent_id,
    )
    topics = row.select_one("dd.topics")
    posts = row.select_one("dd.posts")
    f.topic_count = (
        first_int(topics.find(string=True, recursive=False)) if topics else None
    )
    f.post_count = (
        first_int(posts.find(string=True, recursive=False)) if posts else None
    )
    last = row.select_one("dd.lastpost")
    if last is not None:
        link = last.select_one('a[href*="viewtopic.php"][href*="p="]')
        f.last_post_id = query_int(link.get("href"), "p") if link else None
        f.last_post_at = time_attr(last)
        f.last_poster = user_ref(last)
    return f


def parse_forum_lists(soup: BeautifulSoup, page_forum_id: int | None) -> list[ForumRow]:
    """Parse the category/forum tables on the index or a forum page."""
    forums: list[ForumRow] = []
    order = 0
    for block in soup.select("div.forabg"):
        parent_id = page_forum_id
        header_link = block.select_one('li.header dt a[href*="viewforum.php"]')
        if header_link is not None:
            cat_id = query_int(header_link.get("href"), "f")
            if cat_id is not None:
                order += 1
                forums.append(
                    ForumRow(
                        forum_id=cat_id,
                        name=clean_text(header_link),
                        forum_type="category",
                        display_order=order,
                        parent_id=page_forum_id,
                    )
                )
                parent_id = cat_id
        for row in block.select("ul.forums > li.row"):
            order += 1
            f = parse_forum_row(row, order, parent_id)
            if f is not None:
                forums.append(f)
    return forums


def parse_index(html: str) -> IndexPage:
    soup = soupify(html)
    stats = BoardStats()
    block = soup.select_one("div.stat-block.statistics") or soup
    text = clean_text(block)
    for attr, label in (
        ("total_posts", "Total posts"),
        ("total_topics", "Total topics"),
        ("total_members", "Total members"),
    ):
        m = re.search(label + r"\s+([\d,]+)", text)
        if m:
            setattr(stats, attr, int(m.group(1).replace(",", "")))
    newest = soup.find(string=re.compile("newest member"))
    if newest is not None:
        stats.newest_member = user_ref(newest.find_next(class_=re.compile("^username")))
    tz = None
    for span in soup.find_all("span", title=True):
        if span.parent and "All times are" in clean_text(span.parent):
            tz = span
            break
    if tz is not None and "/" in tz.get("title", ""):
        stats.timezone = tz["title"]
    return IndexPage(forums=parse_forum_lists(soup, None), stats=stats)


# ------------------------------------------------------------------ topic list


def parse_topic_row(row: Tag) -> TopicRow | None:
    dl = row.find("dl")
    title = row.select_one("a.topictitle")
    if dl is None or title is None:
        return None
    topic_id = query_int(title.get("href"), "t")
    if topic_id is None:
        return None
    classes = " ".join(dl.get("class", []))
    topic_type = "normal"
    for prefix, kind in (
        ("global_", "global"),
        ("announce_", "announce"),
        ("sticky_", "sticky"),
    ):
        if prefix in classes:
            topic_type = kind
    t = TopicRow(
        topic_id=topic_id,
        title=clean_text(title),
        topic_type=topic_type,
        is_locked="locked" in classes,
        is_hot="_hot" in classes,
        is_moved="moved" in classes,
    )
    poster = row.select_one("div.topic-poster")
    if poster is not None:
        t.poster = user_ref(poster)
        t.created_at = time_attr(poster)
        t.has_attachments = poster.select_one("i.fa-paperclip") is not None
        t.has_poll = poster.select_one("i.fa-bar-chart") is not None
    replies = row.select_one("dd.posts")
    views = row.select_one("dd.views")
    t.reply_count = (
        first_int(replies.find(string=True, recursive=False)) if replies else None
    )
    t.view_count = (
        first_int(views.find(string=True, recursive=False)) if views else None
    )
    last = row.select_one("dd.lastpost")
    if last is not None:
        link = last.select_one('a[href*="p="]')
        t.last_post_id = query_int(link.get("href"), "p") if link else None
        t.last_poster = user_ref(last)
        t.last_post_at = time_attr(last)
    return t


def parse_viewforum(html: str) -> ForumPage:
    soup = soupify(html)
    crumbs = breadcrumbs(soup)
    title = soup.select_one("h2.forum-title a")
    forum_id = query_int(title.get("href"), "f") if title else None
    if forum_id is None and crumbs:
        forum_id = crumbs[-1][0]
    total, next_start = top_pagination(soup)
    topics = [
        t for row in soup.select("ul.topics > li.row") if (t := parse_topic_row(row))
    ]
    return ForumPage(
        forum_id=forum_id,
        name=clean_text(title) if title else None,
        breadcrumbs=crumbs,
        total_topics=total,
        next_start=next_start,
        subforums=parse_forum_lists(soup, forum_id),
        topics=topics,
    )


# ------------------------------------------------------------------ topic page


def _profile_value(dd: Tag) -> str:
    dd = copy.copy(dd)
    label = dd.find("strong")
    if label is not None:
        label.decompose()
    return clean_text(dd)


def parse_profile(dl: Tag | None) -> PostProfile:
    if dl is None:
        return PostProfile(user=UserRef(None, ""))
    dt = dl.find("dt")
    name_el = None
    if dt is not None:
        for el in dt.select(".username, .username-coloured"):
            name_el = el
            break
    user = user_ref(name_el) or UserRef(None, clean_text(dt) if dt else "")
    p = PostProfile(user=user)
    # Uploaded avatars are <img class="avatar">; Gravatar avatars are a plain <img>
    # (with a protocol-relative //gravatar.com/... address) in the same box.
    avatar = dl.select_one(".avatar-container img, a.avatar img, img.avatar")
    if avatar is not None and avatar.get("src"):
        p.avatar_url = avatar.get("src")
        p.avatar_width = first_int(avatar.get("width"))
        p.avatar_height = first_int(avatar.get("height"))
    rank = dl.select_one("dd.profile-rank")
    if rank is not None:
        p.rank_title = clean_text(rank) or None
        img = rank.find("img")
        if img is not None:
            p.rank_image_url = img.get("src")
            p.rank_title = p.rank_title or img.get("alt") or img.get("title")
    posts = dl.select_one("dd.profile-posts")
    if posts is not None:
        p.post_count = first_int(_profile_value(posts))
    joined = dl.select_one("dd.profile-joined")
    if joined is not None:
        p.joined_at = local_to_utc(_profile_value(joined))
    for dd in dl.select("dd.profile-custom-field"):
        key = next(
            (
                c.removeprefix("profile-")
                for c in dd.get("class", [])
                if c != "profile-custom-field"
            ),
            None,
        )
        label = dd.find("strong")
        if key:
            p.fields[key] = {
                "label": clean_text(label).rstrip(":") if label else key,
                "value": _profile_value(dd),
            }
    for a in dl.select("dd.profile-contact .contact-icons a[href]"):
        href = a["href"]
        if href and href != "#":
            p.contacts[a.get("title") or clean_text(a)] = href
    return p


def parse_edit_notice(notice: Tag | None) -> EditInfo | None:
    if notice is None:
        return None
    text = clean_text(notice)
    m = re.search(r"edited (\d+) times? in total", text)
    if not m:
        return None
    user = user_ref(notice)
    if user is None:
        nm = re.search(r"Last edited by (.+?) on " + LOCAL_TIME_RE, text)
        if nm:
            user = UserRef(None, nm.group(1))
    reason = None
    rm = re.search(r"Reason:\s*(.+)$", text)
    if rm:
        reason = rm.group(1).strip()
    t = notice.find("time")
    edited_at = iso_utc(t.get("datetime")) if t else local_to_utc(text)
    return EditInfo(
        count=int(m.group(1)), user=user, edited_at=edited_at, reason=reason
    )


ATTACH_SIZE_RE = re.compile(r"\(([\d.,]+\s*(?:[KMGT]i?B|Bytes?))\)", re.IGNORECASE)
ATTACH_COUNT_RE = re.compile(
    r"(?:Viewed|Downloaded)\s+([\d,]+)\s+times?", re.IGNORECASE
)

# The forum shows file sizes in binary units (1 KiB = 1024 bytes), rounded. The
# decimal units are accepted too, in case the board's settings change.
SIZE_UNITS = {
    "b": 1,
    "byte": 1,
    "bytes": 1,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
    "tib": 1024**4,
    "kb": 1000,
    "mb": 1000**2,
    "gb": 1000**3,
    "tb": 1000**4,
}
SIZE_TEXT_RE = re.compile(r"\s*([\d][\d.,]*)\s*([A-Za-z]+)\s*")


def size_text_to_bytes(text: str | None) -> int | None:
    """Convert a size shown on the forum, such as ``339.4 KiB``, to bytes."""
    m = SIZE_TEXT_RE.fullmatch(text or "")
    if not m:
        return None
    unit = SIZE_UNITS.get(m.group(2).lower())
    try:
        number = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return round(number * unit) if unit else None


def parse_attachments(post: Tag, content: Tag | None) -> list[Attachment]:
    """Attachments shown in a post, in phpBB's attachment boxes.

    Only links and images inside an attachment box count. A plain link to a
    ``download/file.php`` URL in the post text is just a link, often to an
    attachment of a different post, and its text is the shortened URL rather
    than the file name.
    """
    found: dict[int, Attachment] = {}
    refs = post.select('a[href*="download/file.php"], img[src*="download/file.php"]')
    for el in refs:
        url = el.get("href") or el.get("src")
        attach_id = query_int(url, "id")
        if attach_id is None:
            continue
        container = el.find_parent(
            ["dl", "div"], class_=["file", "thumbnail", "inline-attachment"]
        )
        if container is None:
            continue
        a = found.setdefault(attach_id, Attachment(attach_id=attach_id))
        if content is not None and content in el.parents:
            a.is_inline = True
        if el.name == "img":
            a.is_image = True
            a.filename = a.filename or el.get("alt")
            title = el.get("title") or ""
        else:
            title = ""
            if not a.filename and clean_text(el):
                a.filename = clean_text(el)
        blob = " ".join(
            filter(None, [title, clean_text(container) if container else ""])
        )
        if m := ATTACH_SIZE_RE.search(blob):
            a.size_text = a.size_text or m.group(1)
        if m := ATTACH_COUNT_RE.search(blob):
            a.download_count = int(m.group(1).replace(",", ""))
        if container is not None and container.name == "dl" and a.comment is None:
            for dd in container.find_all("dd"):
                txt = clean_text(dd)
                if (
                    txt
                    and not ATTACH_SIZE_RE.search(txt)
                    and not ATTACH_COUNT_RE.search(txt)
                ):
                    a.comment = txt
                    break
    return list(found.values())


def parse_post(div: Tag) -> Post | None:
    m = re.fullmatch(r"p(\d+)", div.get("id", ""))
    if not m:
        return None
    post_id = int(m.group(1))
    body = div.select_one("div.postbody") or div
    subject_el = body.select_one("h3 a") or body.find("h3")
    number_el = body.select_one("p.post-number a")
    post_number = first_int(clean_text(number_el)) if number_el else None
    posted_at = None
    for p in body.select("p.author"):
        if (t := time_attr(p)) is not None:
            posted_at = t
            break
    content = body.select_one("div.content")
    if content is not None:
        content_html = inner_html(content)
        content_text = html_to_text(content)
    else:
        content_html = content_text = ""
    sig = div.select_one("div.signature")
    return Post(
        post_id=post_id,
        post_number=post_number,
        subject=clean_text(subject_el) if subject_el else None,
        posted_at=posted_at,
        profile=parse_profile(div.select_one("dl.postprofile")),
        content_html=content_html,
        content_text=content_text,
        signature_html=inner_html(sig) if sig else None,
        edit=parse_edit_notice(body.select_one("div.notice")),
        attachments=parse_attachments(div, content),
    )


def parse_poll(soup: BeautifulSoup) -> Poll | None:
    title = soup.select_one(".poll-title")
    if title is None:
        return None
    panel = (
        title.find_parent("form") or title.find_parent("div", class_="panel") or soup
    )
    info = panel.select_one("p.author")
    options: list[PollOption] = []
    total = None
    for dl in panel.select("fieldset.polls dl"):
        bar = dl.select_one("dd.resultbar")
        if bar is not None and "poll_total_votes" in bar.get("class", []):
            total = first_int(clean_text(bar))
            continue
        dt = dl.find("dt")
        if dt is None or not clean_text(dt):
            continue
        label = dt.find("label")
        option_id = first_int(dl.get("data-poll-option-id"))
        inp = dl.find("input")
        if option_id is None and inp is not None:
            option_id = first_int(inp.get("value"))
        percent = dl.select_one("dd.poll_option_percent")
        options.append(
            PollOption(
                option_id=option_id,
                option_html=inner_html(label or dt),
                votes=first_int(clean_text(bar)) if bar is not None else None,
                percent_text=clean_text(percent) if percent else None,
            )
        )
    if total is None:
        cnt = panel.select_one(".poll_total_vote_cnt")
        total = first_int(clean_text(cnt)) if cnt else None
    return Poll(
        question_html=inner_html(title),
        info_text=clean_text(info) or None if info else None,
        total_votes=total,
        options=options,
    )


def parse_viewtopic(html: str) -> TopicPage:
    soup = soupify(html)
    title = soup.select_one("h2.topic-title a")
    topic_id = query_int(title.get("href"), "t") if title else None
    total, next_start = top_pagination(soup)
    bar = soup.select_one("div.action-bar.bar-top")
    is_locked = bar is not None and bar.select_one("a.button i.fa-lock") is not None
    posts = [p for div in soup.select("div.post[id]") if (p := parse_post(div))]
    return TopicPage(
        topic_id=topic_id,
        title=clean_text(title) if title else None,
        breadcrumbs=breadcrumbs(soup),
        total_posts=total,
        next_start=next_start,
        is_locked=is_locked,
        poll=parse_poll(soup),
        posts=posts,
    )


def error_message(html: str) -> str | None:
    """Return the text of a phpBB information/error box, if the page is one."""
    soup = soupify(html)
    box = soup.select_one("#message .inner, div.panel#message, #message")
    if box is None:
        return None
    for h in box.find_all("h2"):
        h.decompose()
    return clean_text(box) or None

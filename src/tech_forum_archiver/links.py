"""Rewrite archived phpBB HTML so it works inside the published static site.

Links to forum pages that are in the archive point at the matching local page;
everything else that was relative to the board points back at the original
site. Scripting hooks (``on*`` attributes, ``javascript:`` URLs, ``<script>``)
are removed.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning, Tag
from markupsafe import Markup

# hosts the board has lived on; links to these are treated as internal
BOARD_HOSTS = {"tech.churchofjesuschrist.org", "tech.lds.org"}


@dataclass
class LinkMap:
    """What exists in the published site, and where."""

    base_url: (
        str  # original board URL, e.g. https://tech.churchofjesuschrist.org/forum/
    )
    post_topics: dict[int, int] = field(default_factory=dict)  # post id -> topic id
    topics: set[int] = field(default_factory=set)  # topics with a page
    forums: set[int] = field(default_factory=set)
    users: set[int] = field(default_factory=set)  # users with a page
    attachments: dict[int, str] = field(
        default_factory=dict
    )  # attach id -> path in site
    # avatar address as stored (e.g. ./download/file.php?avatar=... or
    # //gravatar.com/avatar/...) -> path in site
    avatars: dict[str, str] = field(default_factory=dict)
    # file name of an avatar uploaded to the forum (the avatar= value) -> path in site
    avatar_names: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.base_url.endswith("/"):
            self.base_url += "/"
        self.hosts = BOARD_HOSTS | {urlsplit(self.base_url).hostname or ""}

    def absolute(self, url: str) -> str:
        return urljoin(self.base_url, url)

    def topic(self, topic_id: int, root: str, post_id: int | None = None) -> str:
        anchor = f"#p{post_id}" if post_id else ""
        return f"{root}t/{topic_id}.html{anchor}"

    def user(self, user_id: int | None, root: str) -> str | None:
        return f"{root}u/{user_id}.html" if user_id in self.users else None

    def avatar(self, url: str | None, root: str) -> str | None:
        if not url:
            return None
        if url in self.avatars:
            return root + self.avatars[url]
        return self.absolute(url)

    def attachment(self, attach_id: int, root: str) -> str:
        if attach_id in self.attachments:
            return root + self.attachments[attach_id]
        return self.absolute(f"download/file.php?id={attach_id}")

    def resolve(self, url: str, root: str) -> str:
        """Where a link found in archived HTML should point in the static site."""
        url = url.strip()
        if not url or url.startswith(("#", "mailto:", "data:")):
            return url
        absolute = self.absolute(url)
        parts = urlsplit(absolute)
        if (
            parts.scheme not in ("http", "https")
            or (parts.hostname or "") not in self.hosts
        ):
            return absolute
        page = parts.path.rsplit("/", 1)[-1]
        q = _query(absolute)
        fragment = f"#{parts.fragment}" if parts.fragment else ""
        if page == "viewtopic.php":
            post_id = _int(q.get("p"))
            if post_id in self.post_topics:
                return self.topic(self.post_topics[post_id], root, post_id)
            topic_id = _int(q.get("t"))
            if topic_id in self.topics:
                return self.topic(topic_id, root) + fragment
        elif page == "viewforum.php":
            forum_id = _int(q.get("f"))
            if forum_id in self.forums:
                return f"{root}f/{forum_id}.html"
        elif page == "memberlist.php" and q.get("mode") == "viewprofile":
            if local := self.user(_int(q.get("u")), root):
                return local
        elif page == "file.php":
            attach_id = _int(q.get("id"))
            if attach_id in self.attachments:
                return root + self.attachments[attach_id]
            if (name := q.get("avatar")) and name in self.avatar_names:
                return root + self.avatar_names[name]
        elif page in ("", "index.php") and parts.path.rstrip("/").endswith("/forum"):
            return f"{root}index.html"
        return absolute


def _query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items() if v}


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def rewrite_html(html: str | None, links: LinkMap, root: str) -> Markup:
    """Archived post/description/signature HTML, made safe and local for the site."""
    if not html:
        return Markup("")
    with warnings.catch_warnings():
        # A post or signature that is only a bare address looks to BeautifulSoup
        # like a URL passed by mistake. It is always HTML here, so the warning is
        # just noise.
        warnings.simplefilter("ignore", MarkupResemblesLocatorWarning)
        soup = BeautifulSoup(html, "html.parser")
    for bad in soup.find_all(["script", "noscript"]):
        bad.decompose()
    for el in soup.find_all(True):
        if not isinstance(el, Tag):
            continue
        # code boxes have a "Select all" link that only works with phpBB's scripts
        if el.name == "a" and el.get("href") == "#" and el.get("onclick"):
            el.decompose()
            continue
        for attr in [a for a in el.attrs if a.lower().startswith("on")]:
            del el[attr]
        for attr in ("href", "src"):
            value = el.get(attr)
            if not isinstance(value, str):
                continue
            if value.strip().lower().startswith("javascript:"):
                del el[attr]
            else:
                el[attr] = links.resolve(value, root)
        if el.name == "a" and el.get("href", "").startswith("http"):
            el["rel"] = "nofollow noopener"
    # scripts, event handlers and javascript: URLs were removed above
    return Markup(str(soup))  # noqa: S704

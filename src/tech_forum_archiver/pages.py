"""htpy components for the published static site.

Every page lives either at the site root (``root = ""``) or one directory
down (``root = "../"``); all links are relative so the site works when opened
from disk or hosted under any path.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from sqlite3 import Row

import htpy as h
from htpy import Node
from markupsafe import Markup

from tech_forum_archiver.links import LinkMap, rewrite_html

# Show times in the reader's own timezone when JavaScript is available; the
# page text (UTC) is the fallback and is kept as a tooltip.
LOCALIZE_TIMES = Markup(
    """<script>
for (const t of document.querySelectorAll("time[datetime]")) {
  const d = new Date(t.getAttribute("datetime"));
  if (isNaN(d)) continue;
  t.title = t.textContent;
  t.textContent = t.dataset.date !== undefined
    ? d.toLocaleDateString(undefined, {dateStyle: "medium"})
    : d.toLocaleString(undefined, {dateStyle: "medium", timeStyle: "short"});
}
</script>"""
)


NO_VALUE = "\N{EN DASH}"  # shown for missing numbers and dates

# ------------------------------------------------------------------ view data


@dataclass
class Site:
    title: str
    links: LinkMap
    original_url: str
    updated_at: str | None = None
    search: bool = True  # whether the site has a search index


@dataclass
class LastPost:
    topic_id: int
    topic_title: str
    post_id: int
    poster_id: int | None
    poster_name: str | None
    posted_at: str | None


@dataclass
class ForumView:
    forum_id: int
    name: str
    forum_type: str
    description_html: str | None
    parent_id: int | None
    children: list[ForumView] = field(default_factory=list)
    topics: int = 0  # archived topics, including subforums
    posts: int = 0  # archived posts, including subforums
    last: LastPost | None = None


@dataclass
class TopicView:
    topic_id: int
    title: str
    topic_type: str
    is_locked: bool
    removed: bool
    archived_posts: int
    reply_count: int | None
    view_count: int | None
    poster_id: int | None
    poster_name: str | None
    created_at: str | None
    last: LastPost | None


# ------------------------------------------------------------------- helpers


def when(iso: str | None, *, date_only: bool = False) -> Node:
    if not iso:
        return None
    dt = datetime.fromisoformat(iso)
    if date_only:
        return h.time({"data-date": ""}, datetime=iso)[dt.strftime("%Y-%m-%d")]
    return h.time(datetime=iso)[dt.strftime("%Y-%m-%d %H:%M UTC")]


def number(n: int | None) -> str:
    return f"{n:,}" if n is not None else NO_VALUE


def plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def user_link(site: Site, root: str, user_id: int | None, name: str | None) -> Node:
    name = name or "unknown"
    href = site.links.user(user_id, root)
    return h.a(href=href)[name] if href else h.span[name]


def avatar(site: Site, root: str, url: str | None, name: str | None) -> Node:
    src = site.links.avatar(url, root)
    if src:
        return h.img(".avatar", src=src, alt="", loading="lazy")
    initial = (name or "?").strip()[:1].upper() or "?"
    return h.div(".avatar.placeholder", {"aria-hidden": "true"})[initial]


def last_post(
    site: Site,
    root: str,
    last: LastPost | None,
    *,
    show_topic: bool,
    linked: bool = True,
) -> Node:
    if last is None:
        return None
    href = site.links.topic(last.topic_id, root, last.post_id) if linked else None
    return h.div(".last")[
        show_topic and [h.a(href=href)[last.topic_title], h.br],
        h.span(".m-only")["Last post "],
        "by ",
        user_link(site, root, last.poster_id, last.poster_name),
        h.br,
        h.a(href=href)[when(last.posted_at)] if href else when(last.posted_at),
    ]


def crumbs(root: str, trail: list[tuple[str, str | None]]) -> Node:
    items: list[Node] = [h.a(href=f"{root}index.html")["Forum"]]
    for label, href in trail:
        items.append(h.a(href=href)[label] if href else h.span[label])
    return h.nav(".crumbs", {"aria-label": "Breadcrumb"})[items]


def search_box(root: str, *, autofocus: bool = False) -> h.Element:
    """A plain form that opens the search page; it works without JavaScript."""
    return h.form(
        ".site-search", action=f"{root}search.html", method="get", role="search"
    )[
        h.input(
            type="search",
            name="q",
            placeholder="Search posts",
            aria_label="Search posts",
            autofocus=autofocus,
        ),
        h.button(type="submit")["Search"],
    ]


def layout(
    site: Site,
    *,
    root: str,
    title: str,
    body: Node,
    scripts: list[str] | None = None,
    header_search: bool = True,
) -> h.Element:
    page_title = f"{title} · {site.title}" if title != site.title else title
    return h.html(lang="en")[
        h.head[
            h.meta(charset="utf-8"),
            h.meta(name="viewport", content="width=device-width, initial-scale=1"),
            h.title[page_title],
            h.link(rel="stylesheet", href=f"{root}style.css"),
        ],
        h.body[
            h.header(".site-header")[
                h.div(".wrap")[
                    h.a(".site-name", href=f"{root}index.html")[site.title],
                    h.nav(".site-nav")[
                        h.a(href=f"{root}index.html")["Forums"],
                        h.a(href=f"{root}u/index.html")["Members"],
                    ],
                    site.search and header_search and search_box(root),
                ]
            ],
            h.main(".wrap")[body],
            h.footer(".site-footer")[
                h.div(".wrap")[
                    "Archived from ",
                    h.a(href=site.original_url)[site.original_url],
                    site.updated_at and [". Last updated ", when(site.updated_at), "."],
                ]
            ],
            LOCALIZE_TIMES,
            [h.script(src=f"{root}{src}") for src in scripts or []],
        ],
    ]


def search_page(site: Site) -> h.Element:
    return layout(
        site,
        root="",
        title="Search",
        body=[
            h.h1["Search"],
            h.div(".search-page")[search_box("", autofocus=True)],
            h.p("#search-status.lede")[
                h.noscript[
                    "Search needs JavaScript, which is turned off in this browser."
                ]
            ],
            h.ol("#search-results.results"),
            h.button("#search-more.more", type="button", hidden=True)[
                "Show more results"
            ],
        ],
        scripts=["search.js"],
        header_search=False,  # the page has its own, bigger search box
    )


# -------------------------------------------------------------- forum lists


def forum_list(site: Site, root: str, forums: list[ForumView]) -> Node:
    if not forums:
        return None
    rows: list[Node] = [
        h.div(".row.head")[
            h.div["Forum"],
            h.div(".num")["Topics"],
            h.div(".num.secondary")["Posts"],
            h.div(".last")["Last post"],
        ]
    ]
    for f in forums:
        children = [c for c in f.children if c.forum_type != "category"]
        rows.append(
            h.div(".row")[
                h.div[
                    h.a(".title", href=f"{root}f/{f.forum_id}.html")[f.name],
                    f.description_html
                    and h.div(".sub")[
                        rewrite_html(f.description_html, site.links, root)
                    ],
                    children
                    and h.div(".sub")[
                        "Subforums: ",
                        _join(
                            h.a(href=f"{root}f/{c.forum_id}.html")[c.name]
                            for c in children
                        ),
                    ],
                ],
                h.div(".num")[number(f.topics)],
                h.div(".num.secondary")[number(f.posts)],
                last_post(site, root, f.last, show_topic=True),
            ]
        )
    return h.div(".list")[rows]


def _join(items: Iterable[Node], sep: str = ", ") -> list[Node]:
    out: list[Node] = []
    for i, item in enumerate(items):
        if i:
            out.append(sep)
        out.append(item)
    return out


def topic_badges(t: TopicView) -> list[Node]:
    labels = {"global": "Announcement", "announce": "Announcement", "sticky": "Sticky"}
    return [
        t.topic_type in labels and h.span(".badge")[labels[t.topic_type]],
        t.is_locked and h.span(".badge")["Locked"],
        t.removed and h.span(".badge.warn")["Removed from forum"],
    ]


def topic_list(site: Site, root: str, topics: list[TopicView]) -> Node:
    if not topics:
        return h.p(".empty")["No topics in the archive."]
    rows: list[Node] = [
        h.div(".row.head")[
            h.div["Topic"],
            h.div(".num")["Replies"],
            h.div(".num.secondary")["Views"],
            h.div(".last")["Last post"],
        ]
    ]
    for t in topics:
        archived = t.archived_posts > 0
        title = (
            h.a(".title", href=site.links.topic(t.topic_id, root))[t.title]
            if archived
            else h.span(".title")[t.title]
        )
        rows.append(
            h.div(".row" if archived else ".row.muted")[
                h.div[
                    topic_badges(t),
                    title,
                    h.div(".sub")[
                        "by ",
                        user_link(site, root, t.poster_id, t.poster_name),
                        t.created_at and [" » ", when(t.created_at)],
                        not archived and " · posts not archived yet",
                    ],
                ],
                h.div(".num")[number(t.reply_count)],
                h.div(".num.secondary")[number(t.view_count)],
                last_post(site, root, t.last, show_topic=False, linked=archived),
            ]
        )
    return h.div(".list")[rows]


# --------------------------------------------------------------------- pages


def index_page(site: Site, top: list[ForumView]) -> h.Element:
    root = ""
    sections: list[Node] = []
    loose = [f for f in top if f.forum_type != "category"]
    if loose:
        sections.append(forum_list(site, root, loose))
    for cat in (f for f in top if f.forum_type == "category"):
        sections.append(h.h2[h.a(href=f"f/{cat.forum_id}.html")[cat.name]])
        sections.append(forum_list(site, root, cat.children))
    total_topics = sum(f.topics for f in top)
    total_posts = sum(f.posts for f in top)
    return layout(
        site,
        root=root,
        title=site.title,
        body=[
            h.h1[site.title],
            h.p(".lede")[
                (
                    f"An offline archive of {plural(total_topics, 'topic')} and "
                    f"{plural(total_posts, 'post')} from "
                ),
                h.a(href=site.original_url)[site.original_url],
                ".",
            ],
            sections,
        ],
    )


def forum_trail(root: str, ancestors: list[ForumView]) -> list[tuple[str, str | None]]:
    return [(f.name, f"{root}f/{f.forum_id}.html") for f in ancestors]


def forum_page(
    site: Site, forum: ForumView, ancestors: list[ForumView], topics: list[TopicView]
) -> h.Element:
    root = "../"
    return layout(
        site,
        root=root,
        title=forum.name,
        body=[
            crumbs(root, forum_trail(root, ancestors)),
            h.h1[forum.name],
            forum.description_html
            and h.p(".lede")[rewrite_html(forum.description_html, site.links, root)],
            forum.children and [h.h2["Forums"], forum_list(site, root, forum.children)],
            (topics or forum.forum_type != "category")
            and [h.h2[f"Topics ({len(topics):,})"], topic_list(site, root, topics)],
        ],
    )


def post_article(
    site: Site,
    root: str,
    topic_title: str,
    post: Row,
    user: Row | None,
    attachments: list[Row],
) -> Node:
    """One post. ``post`` and ``user`` are rows from the posts/users tables."""
    rank = user["rank_title"] if user else None
    subject = post["subject"] or ""
    edit_note = None
    if post["edit_count"]:
        edit_note = h.div(".note")[
            f"Edited {plural(post['edit_count'], 'time')}",
            post["last_edited_by_name"] and [", last by ", post["last_edited_by_name"]],
            post["last_edited_at"] and [" on ", when(post["last_edited_at"])],
            post["edit_reason"] and [". Reason: ", post["edit_reason"]],
        ]
    files = [a for a in attachments if not a["is_inline"]]
    return h.article(".post", id=f"p{post['post_id']}")[
        h.header(".post-head")[
            avatar(
                site, root, user["avatar_url"] if user else None, post["poster_name"]
            ),
            h.div(".who")[
                h.div(".name")[
                    user_link(site, root, post["poster_user_id"], post["poster_name"])
                ],
                rank and h.div(".rank")[rank],
            ],
            h.div(".when")[
                h.a(".num", href=f"#p{post['post_id']}")[
                    f"#{post['post_number'] or ''}"
                ],
                h.br,
                when(post["posted_at"]),
            ],
        ],
        edit_note,
        post["removed_at"]
        and h.div(".note.warn")[
            "This post is no longer on the original forum (noticed ",
            when(post["removed_at"], date_only=True),
            ").",
        ],
        h.div(".post-body")[
            subject
            and subject not in (topic_title, f"Re: {topic_title}")
            and h.p(".subject")[subject],
            h.div(".content")[rewrite_html(post["content_html"], site.links, root)],
            files and h.div(".attachments")[(attachment(site, root, a) for a in files)],
            user
            and user["signature_html"]
            and h.div(".signature")[
                rewrite_html(user["signature_html"], site.links, root)
            ],
        ],
    ]


def attachment(site: Site, root: str, a: Row) -> Node:
    href = site.links.attachment(a["attach_id"], root)
    name = a["filename"] or f"attachment {a['attach_id']}"
    size = f" ({a['size_text']})" if a["size_text"] else ""
    if a["is_image"]:
        return h.figure[
            h.a(href=href)[h.img(src=href, alt=name, loading="lazy")],
            h.figcaption[name, size],
        ]
    return h.div(".file")["📎 ", h.a(href=href)[name], size]


def poll_box(site: Site, root: str, poll: Row | None, options: list[Row]) -> Node:
    if poll is None:
        return None
    total = poll["total_votes"] or sum(o["votes"] or 0 for o in options) or 0
    rows = []
    for o in options:
        votes = o["votes"] or 0
        pct = round(100 * votes / total) if total else 0
        rows.append(
            h.div(".option")[
                h.div(".label")[
                    h.span[rewrite_html(o["option_html"], site.links, root)],
                    h.span(".meta")[f"{votes:,} ({o['percent_text'] or f'{pct}%'})"],
                ],
                h.div(".bar")[h.span(style=f"width:{pct}%")],
            ]
        )
    return h.section(".poll")[
        h.h2["Poll: ", rewrite_html(poll["question_html"], site.links, root)],
        poll["info_text"] and h.div(".meta")[poll["info_text"]],
        rows,
        h.div(".meta", style="margin-top:.6rem")[f"Total votes: {total:,}"],
    ]


def topic_page(
    site: Site,
    topic: Row,
    ancestors: list[ForumView],
    posts: list[Row],
    users: dict[int, Row],
    attachments: dict[int, list[Row]],
    poll: Row | None,
    poll_options: list[Row],
) -> h.Element:
    root = "../"
    title = topic["title"] or f"Topic {topic['topic_id']}"
    view = TopicView(
        topic_id=topic["topic_id"],
        title=title,
        topic_type=topic["topic_type"],
        is_locked=bool(topic["is_locked"]),
        removed=topic["removed_at"] is not None,
        archived_posts=len(posts),
        reply_count=topic["reply_count"],
        view_count=topic["view_count"],
        poster_id=None,
        poster_name=None,
        created_at=None,
        last=None,
    )
    first = posts[0] if posts else None
    original = f"{site.links.base_url}viewtopic.php?t={topic['topic_id']}"
    return layout(
        site,
        root=root,
        title=title,
        body=[
            crumbs(root, forum_trail(root, ancestors)),
            h.h1[topic_badges(view), title],
            h.p(".meta")[
                h.span[plural(len(posts), "post")],
                first
                and h.span[
                    "started by ",
                    user_link(
                        site, root, first["poster_user_id"], first["poster_name"]
                    ),
                    " ",
                    when(first["posted_at"]),
                ],
                topic["view_count"] is not None
                and h.span[plural(topic["view_count"], "view")],
                h.span[h.a(href=original, rel="nofollow noopener")["original topic"]],
            ],
            poll_box(site, root, poll, poll_options),
            [
                post_article(
                    site,
                    root,
                    title,
                    p,
                    users.get(p["poster_user_id"]),
                    attachments.get(p["post_id"], []),
                )
                for p in posts
            ]
            or h.p(".empty")["The posts in this topic have not been archived yet."],
        ],
    )


def user_page(
    site: Site, user: Row, posts: list[Row], topic_titles: dict[int, str]
) -> h.Element:
    root = "../"
    name = user["username"]
    details: list[Node] = []

    def detail(label: str, value: Node) -> None:
        if value:
            details.extend([h.dt[label], h.dd[value]])

    detail("Rank", user["rank_title"])
    detail("Joined", when(user["joined_at"], date_only=True))
    detail("Location", user["location"])
    if user["website"]:
        detail(
            "Website",
            h.a(href=user["website"], rel="nofollow noopener")[user["website"]],
        )
    detail(
        "Posts on the forum", number(user["post_count"]) if user["post_count"] else None
    )
    detail("Posts in the archive", number(len(posts)))
    rows: list[Node] = [
        h.div(".row.two.head")[h.div["Topic"], h.div(".last")["Posted"]]
    ]
    for p in posts:
        title = topic_titles.get(p["topic_id"], f"Topic {p['topic_id']}")
        href = site.links.topic(p["topic_id"], root, p["post_id"])
        rows.append(
            h.div(".row.two")[
                h.div[h.a(".title", href=href)[title]],
                h.div(".last")[h.a(href=href)[when(p["posted_at"])]],
            ]
        )
    return layout(
        site,
        root=root,
        title=name,
        body=[
            crumbs(root, [("Members", "index.html"), (name, None)]),
            h.section(".profile")[
                avatar(site, root, user["avatar_url"], name),
                h.div[
                    h.h1[name],
                    h.dl[details],
                    user["signature_html"]
                    and h.div(".signature")[
                        rewrite_html(user["signature_html"], site.links, root)
                    ],
                ],
            ],
            h.h2[f"Posts ({len(posts):,})"],
            h.div(".list")[rows] if posts else h.p(".empty")["No archived posts."],
        ],
    )


def members_page(site: Site, members: list[Row]) -> h.Element:
    """``members``: rows with user_id, username, rank_title, joined_at, posts and
    last_post_at."""
    root = "../"
    rows: list[Node] = [
        h.div(".row.head")[
            h.div["Member"],
            h.div(".num")["Posts"],
            h.div(".num.secondary")["Joined"],
            h.div(".last")["Last post"],
        ]
    ]
    rows.extend(
        h.div(".row")[
            h.div[
                h.a(".title", href=f"{m['user_id']}.html")[m["username"]],
                m["rank_title"] and h.div(".sub")[m["rank_title"]],
            ],
            h.div(".num")[number(m["posts"])],
            h.div(".num.secondary")[when(m["joined_at"], date_only=True) or NO_VALUE],
            h.div(".last")[when(m["last_post_at"])],
        ]
        for m in members
    )
    return layout(
        site,
        root=root,
        title="Members",
        body=[
            h.h1["Members"],
            h.p(".lede")[
                f"{plural(len(members), 'member')} with posts in the archive."
            ],
            h.div(".list")[rows],
        ],
    )

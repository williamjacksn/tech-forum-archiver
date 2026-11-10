from collections.abc import Iterator
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest
from bs4 import BeautifulSoup
from test_crawler import FakeBoard, FakeFetcher

from tech_forum_archiver import cli, parse
from tech_forum_archiver.crawler import Crawler, Options
from tech_forum_archiver.db import Archive
from tech_forum_archiver.links import LinkMap, rewrite_html
from tech_forum_archiver.publish import publish

FIXTURES = Path(__file__).parent / "fixtures"
# williamjackson's Gravatar, as shown on viewtopic_t5278_gravatar.html
GRAVATAR = (
    "//gravatar.com/avatar/"
    "08a98b786db4418b5fffa2087ca77b7f9e01cc24746e4d1c5b210ae5be9f630b?s=90"
)
BASE = "https://tech.churchofjesuschrist.org/forum/"


@pytest.fixture
def archive(tmp_path: Path) -> Iterator[Archive]:
    a = Archive(tmp_path / "pub.sqlite")
    # a small crawled board (forums 1 and 2, 30 topics, posts up to 15 per topic)
    Crawler(a, FakeFetcher(FakeBoard(30))).run(Options())
    a.set_meta("base_url", BASE)
    # plus real pages: forum index, a topic with an attachment and rank image
    for f in parse.parse_index(
        (FIXTURES / "index.html").read_text(encoding="utf-8")
    ).forums:
        a.upsert_forum(f, f.parent_id, set_parent=True)
    for name in (
        "viewtopic_t46914_attachment.html",
        "viewtopic_t26052.html",
        "viewtopic_t5278_gravatar.html",
    ):
        page = parse.parse_viewtopic((FIXTURES / name).read_text(encoding="utf-8"))
        a.queue_topic(page.topic_id, 1)
        a.apply_topic_page(page)
        for post in page.posts:
            a.save_post(page.topic_id, post)
        a.finish_topic(page.topic_id, page.total_posts, page.posts[-1].post_id)
    # a downloaded attachment and avatar
    a.save_attachment_data(5925, b"\xff\xd8 fake jpeg", "image/jpeg", None)
    a.save_avatar_data(
        "./download/file.php?avatar=91_1355256303.jpg", b"avatar", "image/jpeg", None
    )
    a.save_avatar_data(GRAVATAR, b"\x89PNG fake", "image/png", None)
    # a post with links back into the forum, a code box, and some scripting
    a.conn.execute(
        "UPDATE posts SET content_html = ? WHERE post_id = 161197",
        (
            (
                'See <a href="./viewtopic.php?p=263570#p263570" class="postlink">'
                "this</a>, "
                '<a href="https://tech.lds.org/forum/viewtopic.php?t=46914">'
                "old link</a>, "
                '<a href="./viewtopic.php?t=999999">missing topic</a> and '
                '<a href="./viewforum.php?f=30" onclick="evil()">forum</a> '
                '<img src="./images/smilies/icon_e_smile.gif" alt=":)" class="smilies">'
                '<a href="javascript:alert(1)">x</a><script>alert(1)</script>'
                '<div class="codebox"><p>Code: '
                '<a href="#" onclick="selectCode(this); return false;">'
                "Select all</a></p><pre><code>x = 1</code></pre></div>"
            ),
        ),
    )
    a.commit()
    yield a
    a.close()


def site_pages(out: Path) -> list[Path]:
    return sorted(out.rglob("*.html"))


def test_every_local_link_resolves(archive: Archive, tmp_path: Path) -> None:
    out = tmp_path / "site"
    publish(archive, out)
    ids_cache: dict[Path, set[str]] = {}

    def ids(path: Path) -> set[str]:
        if path not in ids_cache:
            soup = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
            ids_cache[path] = {el["id"] for el in soup.find_all(id=True)}
        return ids_cache[path]

    checked = 0
    for page in site_pages(out):
        soup = BeautifulSoup(page.read_text(encoding="utf-8"), "html.parser")
        for el in soup.find_all(["a", "img", "link", "script", "form"]):
            url = el.get("href") or el.get("src") or el.get("action")
            if not url or urlsplit(url).scheme:
                continue
            parts = urlsplit(url)
            target = (
                (page.parent / unquote(parts.path)).resolve() if parts.path else page
            )
            assert target.is_file(), f"{page.relative_to(out)}: broken link {url}"
            if parts.fragment and target.suffix == ".html":
                assert parts.fragment in ids(target), (
                    f"{page.relative_to(out)}: no #{parts.fragment}"
                )
            checked += 1
    assert checked > 1000


def test_site_structure(archive: Archive, tmp_path: Path) -> None:
    out = tmp_path / "site"
    stats = publish(archive, out)
    assert (out / "index.html").is_file()
    assert (out / "style.css").is_file()
    assert (out / "u" / "index.html").is_file()
    assert (out / "f" / "30.html").is_file()
    assert (
        out / "files" / "5925" / "Screenshot_20260610_084129_Firefox.jpg"
    ).read_bytes() == (b"\xff\xd8 fake jpeg")
    assert (out / "avatars" / "91_1355256303.jpg").read_bytes() == b"avatar"
    assert stats.topics == 33
    assert stats.written > 0

    # every post of a 15-post (two page) topic is on one page
    topic = next(t for t in FakeBoard(30).topics.values() if len(t.posts) == 15)
    soup = BeautifulSoup(
        (out / "t" / f"{topic.topic_id}.html").read_text(encoding="utf-8"),
        "html.parser",
    )
    assert [a["id"] for a in soup.select("article.post")] == [
        f"p{p.post_id}" for p in topic.posts
    ]

    # topic with the attachment: shown under the post, linked to the local copy
    soup = BeautifulSoup(
        (out / "t" / "46914.html").read_text(encoding="utf-8"), "html.parser"
    )
    assert soup.title.text == "Temple Reservations down? · Tech Forum Archive"
    crumbs = [a.text for a in soup.select("nav.crumbs > *")]
    assert crumbs == ["Forum", "Church Technology for Members", "Main Church Website"]
    img = soup.select_one(".attachments figure img")
    assert img["src"] == "../files/5925/Screenshot_20260610_084129_Firefox.jpg"
    assert "Church Employee" in soup.select("article.post")[2].select_one(".rank").text

    # avatars: local when downloaded, placeholder otherwise
    soup = BeautifulSoup(
        (out / "t" / "26052.html").read_text(encoding="utf-8"), "html.parser"
    )
    srcs = {img["src"] for img in soup.select("img.avatar")}
    assert "../avatars/91_1355256303.jpg" in srcs

    # members page lists posters with links to their pages
    soup = BeautifulSoup(
        (out / "u" / "index.html").read_text(encoding="utf-8"), "html.parser"
    )
    assert soup.find("a", href="91.html").text == "johnshaw"


def test_post_html_is_cleaned_and_links_rewritten(
    archive: Archive, tmp_path: Path
) -> None:
    out = tmp_path / "site"
    publish(archive, out)
    html = (out / "t" / "26052.html").read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")
    content = soup.select_one("#p161197 .content")
    hrefs = {a.text: a.get("href") for a in content.find_all("a")}
    assert hrefs["this"] == "../t/46914.html#p263570"
    assert hrefs["old link"] == "../t/46914.html"
    assert hrefs["missing topic"] == BASE + "viewtopic.php?t=999999"
    assert hrefs["forum"] == "../f/30.html"
    assert hrefs["x"] is None
    assert "Select all" not in hrefs
    assert content.find("img")["src"] == BASE + "images/smilies/icon_e_smile.gif"
    assert "onclick" not in html
    assert "javascript:" not in html
    assert html.count("<script>") == 1  # only the time-localising script


def test_republish_writes_nothing_new(archive: Archive, tmp_path: Path) -> None:
    out = tmp_path / "site"
    first = publish(archive, out)
    second = publish(archive, out)
    assert second.written == 0
    assert second.unchanged == first.written + first.unchanged


def test_default_output_directory(
    archive: Archive, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive.commit()
    monkeypatch.chdir(tmp_path)
    assert cli.main(["--db", str(archive.path), "publish"]) == 0
    assert (tmp_path / "pub-site" / "index.html").is_file()


def test_link_map_resolve() -> None:
    links = LinkMap(
        base_url=BASE, post_topics={5: 2}, topics={2}, forums={7}, users={9}
    )
    assert links.resolve("./viewtopic.php?f=7&t=2", "../") == "../t/2.html"
    assert links.resolve("viewtopic.php?p=5#p5", "") == "t/2.html#p5"
    assert links.resolve("./viewforum.php?f=7&start=20", "../") == "../f/7.html"
    assert (
        links.resolve("./memberlist.php?mode=viewprofile&u=9", "../") == "../u/9.html"
    )
    assert links.resolve("./memberlist.php?mode=viewprofile&u=8", "../") == (
        BASE + "memberlist.php?mode=viewprofile&u=8"
    )
    assert (
        links.resolve("https://tech.churchofjesuschrist.org/forum/", "../")
        == "../index.html"
    )
    assert links.resolve("https://example.com/x", "../") == "https://example.com/x"
    assert links.resolve("#top", "../") == "#top"
    assert rewrite_html("plain <b>text</b>", links, "../") == "plain <b>text</b>"


@pytest.mark.parametrize(
    "html",
    [
        # real examples: a post and a signature that are only a bare address
        "https://lcr.churchofjesuschrist.org/records/ordinances/priesthood?lang=eng",
        "http://www.leemeierlaw.com",
    ],
)
def test_bare_address_does_not_warn(html: str) -> None:
    import warnings

    from bs4 import MarkupResemblesLocatorWarning

    links = LinkMap(base_url=BASE)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = rewrite_html(html, links, "../")
    assert not [w for w in caught if w.category is MarkupResemblesLocatorWarning]
    assert result == html


def test_gravatar_avatars_are_published_locally(
    archive: Archive, tmp_path: Path
) -> None:
    from tech_forum_archiver.publish import avatar_path

    out = tmp_path / "site"
    publish(archive, out)
    local = avatar_path(GRAVATAR, "image/png")
    assert local.startswith("avatars/remote-")
    assert local.endswith(".png")
    assert (out / local).read_bytes() == b"\x89PNG fake"
    soup = BeautifulSoup(
        (out / "t" / "5278.html").read_text(encoding="utf-8"), "html.parser"
    )
    srcs = [img["src"] for img in soup.select("article.post img.avatar")]
    assert f"../{local}" in srcs
    # user 219's uploaded avatar wasn't downloaded, so it loads from the forum
    assert BASE + "download/file.php?avatar=219_1355256303.jpg" in srcs
    profile = BeautifulSoup(
        (out / "u" / "9492.html").read_text(encoding="utf-8"), "html.parser"
    )
    assert profile.select_one(".profile img.avatar")["src"] == f"../{local}"


def test_avatar_that_was_not_downloaded_loads_from_its_site() -> None:
    links = LinkMap(base_url=BASE)
    assert links.avatar(GRAVATAR, "../") == "https:" + GRAVATAR
    assert links.avatar("./download/file.php?avatar=5_1.jpg", "../") == (
        BASE + "download/file.php?avatar=5_1.jpg"
    )


def test_publish_reports_progress(
    archive: Archive,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import itertools
    import logging

    from tech_forum_archiver import crawler

    clock = itertools.count(0, 10)  # each reading of the clock is 10 seconds later
    monkeypatch.setattr(crawler.time, "monotonic", lambda: next(clock))
    with caplog.at_level(logging.INFO, logger="tech_forum_archiver.publish"):
        stats = publish(archive, tmp_path / "site")
    progress = [m for m in caplog.messages if m.startswith("progress:")]
    members = [m for m in progress if " member pages" in m]
    topics = [m for m in progress if " topic pages" in m]
    n = stats.users
    assert n > 6
    # a line once 30 seconds have passed (every 3rd page here), and at the end
    assert members[0].startswith(f"progress: 3/{n} member pages, ETA ")
    assert members[-1].startswith(f"progress: {n}/{n} member pages, ETA ")
    assert len(members) == n // 3 + (1 if n % 3 else 0)
    assert topics[-1].startswith(f"progress: {stats.topics}/{stats.topics} topic pages")

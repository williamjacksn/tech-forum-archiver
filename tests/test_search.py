"""The static site's search index, and its agreement with search.js."""

import json
import random
import shutil
import subprocess
from importlib import resources
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from test_crawler import FakeBoard, FakeFetcher

from tech_forum_archiver import search_index
from tech_forum_archiver.crawler import Crawler, Options
from tech_forum_archiver.db import Archive
from tech_forum_archiver.publish import publish

SEARCH_JS = resources.files("tech_forum_archiver") / "static" / "search.js"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="Node.js is not installed")

TRICKY_TEXT = [
    "São Paulo, SP, Brasil",
    "e-mail: someone@example.com, LDS.org/tools",
    "ÑANDÚ Größe straße İstanbul",
    "Ⅻ ﬁle ① 2010-11-15 v1.2.3 C++ C#",
    "日本語のテキスト 中文 한국어",
    "naïve café résumé coöperate",
    "under_score, tab\tnew\nline",
    "x" * 41 + " ok a",
]


def run_js(calls: list[list]) -> list:
    """Call functions in search.js under Node: each call is [name, *args]."""
    script = (
        f"const s = require({json.dumps(str(SEARCH_JS))});"
        "const calls = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
        "process.stdout.write(JSON.stringify(calls.map(([f, ...a]) => s[f](...a))));"
    )
    result = subprocess.run(  # noqa: S603 - runs Node on our own file
        [NODE, "-e", script],
        input=json.dumps(calls),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return json.loads(result.stdout)


# ------------------------------------------------------------------- python


def test_tokenize() -> None:
    assert search_index.tokenize("São Paulo: e-mail TEST_case 2010 a ñandú") == [
        "sao",
        "paulo",
        "mail",
        "test",
        "case",
        "2010",
        "nandu",
    ]


def test_fnv1a_reference_values() -> None:
    # published FNV-1a 32-bit test vectors
    assert search_index.fnv1a("") == 0x811C9DC5
    assert search_index.fnv1a("a") == 0xE40C292C
    assert search_index.fnv1a("foobar") == 0xBF9CF968


def test_encode_decode_round_trip() -> None:
    rng = random.Random(1)  # noqa: S311 - test data
    for size in (0, 1, 5, 500):
        numbers = sorted(rng.sample(range(2**31 - 1), size))
        assert search_index.decode(search_index.encode(numbers)) == numbers
    assert search_index.encode([0, 1, 2]) == "111"
    assert search_index.encode([31, 32]) == "A11"  # first gap is 32: two digits
    assert search_index.encode([1000]) == "Jv"  # gap 1001 = 9 + 31 * 32


def test_excerpt() -> None:
    assert search_index.excerpt("  short \n text ") == "short text"
    long = search_index.excerpt("word " * 100)
    assert long.endswith("word\N{HORIZONTAL ELLIPSIS}")
    assert len(long) <= search_index.EXCERPT_LENGTH + 1


def build_index(tmp_path: Path, topics: int = 12) -> search_index.SearchIndex:
    archive = Archive(tmp_path / "search.sqlite")
    Crawler(archive, FakeFetcher(FakeBoard(topics))).run(Options())
    index = search_index.build(archive.conn)
    archive.close()
    return index


def test_build(tmp_path: Path) -> None:
    index = build_index(tmp_path)
    board = FakeBoard(12)
    assert len(index.docs) == sum(len(t.posts) for t in board.topics.values())
    # doc numbers run from oldest to newest
    times = [d[4] for d in index.docs]
    assert times == sorted(times)
    # "topic" and "12" are in the title "Topic 12"; every post of that topic is a
    # title match for them, and no post of another topic is
    doc_topics = [d[1] for d in index.docs]
    in_title, other = index.words["12"]
    assert {doc_topics[d] for d in in_title} == {12}
    assert all(doc_topics[d] != 12 for d in other)
    # each post's text ("post 1001") is indexed
    first_post = board.topics[1].posts[0].post_id
    entry = index.words[str(first_post)]
    assert [index.docs[d][0] for d in [*entry[0], *entry[1]]] == [first_post]
    # the archive is too small to leave out common words
    assert index.common_words == []


def test_common_words_are_left_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(search_index, "COMMON_WORD_MIN_POSTS", 10)
    index = build_index(tmp_path)
    # "post" and "re" are in every post, so they aren't indexed
    assert {"post", "re"} <= set(index.common_words)
    assert "post" not in index.words


# ----------------------------------------------------------------- search.js


@needs_node
def test_js_splits_words_like_python() -> None:
    js = run_js([["tokenize", t] for t in TRICKY_TEXT])
    assert js == [search_index.tokenize(t) for t in TRICKY_TEXT]


@needs_node
def test_js_hashes_like_python() -> None:
    words = sorted({w for t in TRICKY_TEXT for w in search_index.tokenize(t)})
    words += ["", "a", "foobar"]
    assert run_js([["fnv1a", w] for w in words]) == [
        search_index.fnv1a(w) for w in words
    ]


@needs_node
def test_js_decodes_like_python() -> None:
    rng = random.Random(2)  # noqa: S311 - test data
    lists = [sorted(rng.sample(range(2**31 - 1), n)) for n in (0, 1, 7, 300)]
    lists.append(list(range(1000)))
    encoded = [search_index.encode(n) for n in lists]
    assert run_js([["decode", e] for e in encoded]) == lists


@needs_node
def test_js_ranks_title_matches_first_then_newest(tmp_path: Path) -> None:
    index = build_index(tmp_path)

    def entry(word: str) -> str:
        a, b = index.words[word]
        return f"{search_index.encode(a)}.{search_index.encode(b)}"

    # "topic" + "12": every post of Topic 12 has both words in its title
    post = str(FakeBoard(12).topics[3].posts[0].post_id)
    ranked = run_js(
        [
            ["rank", [entry("topic"), entry("12")]],
            ["rank", [entry("topic"), entry(post)]],
            ["rank", [entry("12"), entry(post)]],
        ]
    )
    topic_12 = [d for d, doc in enumerate(index.docs) if doc[1] == 12]
    assert ranked[0][: len(topic_12)] == sorted(topic_12, reverse=True)
    # post text matches but not in the title: the one post that has both words
    assert [index.docs[d][0] for d in ranked[1]] == [int(post)]
    assert ranked[2] == []


# -------------------------------------------------------------- published


@pytest.fixture
def site(tmp_path: Path) -> Path:
    archive = Archive(tmp_path / "site.sqlite")
    Crawler(archive, FakeFetcher(FakeBoard(12))).run(Options())
    publish(archive, tmp_path / "site")
    archive.close()
    return tmp_path / "site"


def load_js_data(path: Path) -> list:
    """The arguments of a ``tfaSearch.name(...)`` index file."""
    text = path.read_text(encoding="ascii")  # index files are plain ASCII
    assert text.startswith("tfaSearch.") and text.rstrip().endswith(");")
    args = text[text.index("(") + 1 : text.rstrip().rindex(")")]
    return json.loads(f"[{args}]")


def test_published_index(site: Path) -> None:
    manifest = load_js_data(site / "search" / "manifest.js")[0]
    board = FakeBoard(12)
    assert manifest["docs"] == sum(len(t.posts) for t in board.topics.values())
    assert manifest["shards"] == 16
    assert len(list((site / "search" / "i").iterdir())) == manifest["shards"]
    # find a post through the published files, the way search.js does
    post_id = board.topics[5].posts[-1].post_id
    word = str(post_id)
    _, words = load_js_data(
        site / "search" / "i" / f"{search_index.fnv1a(word) % manifest['shards']}.js"
    )
    title_part, other_part = words[word].split(".")
    (doc,) = search_index.decode(title_part) + search_index.decode(other_part)
    _, data = load_js_data(
        site / "search" / "d" / f"{doc // manifest['docsPerShard']}.js"
    )
    entry = data["docs"][doc % manifest["docsPerShard"]]
    assert entry[:3] == [post_id, 5, len(board.topics[5].posts)]
    assert data["topics"]["5"] == "Topic 5"
    assert entry[5] == f"post {post_id}"
    assert (site / "search.js").read_bytes() == SEARCH_JS.read_bytes()


def test_search_box_and_page(site: Path) -> None:
    topic = BeautifulSoup(
        (site / "t" / "1.html").read_text(encoding="utf-8"), "html.parser"
    )
    form = topic.select_one("header form.site-search")
    assert form["action"] == "../search.html"
    assert form.select_one('input[name="q"]') is not None
    page = BeautifulSoup(
        (site / "search.html").read_text(encoding="utf-8"), "html.parser"
    )
    assert page.select_one("#search-results") is not None
    assert [s["src"] for s in page.select("script[src]")] == ["search.js"]
    # one search box on the search page: its own, not a second one in the header
    assert len(page.select("form.site-search")) == 1
    assert page.select_one("header form.site-search") is None
    assert page.select_one("#search-status noscript") is not None


def test_publish_without_search(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "plain.sqlite")
    Crawler(archive, FakeFetcher(FakeBoard(3))).run(Options())
    publish(archive, tmp_path / "site", search=False)
    archive.close()
    site = tmp_path / "site"
    assert not (site / "search").exists()
    assert not (site / "search.html").exists()
    html = (site / "index.html").read_text(encoding="utf-8")
    assert "site-search" not in html

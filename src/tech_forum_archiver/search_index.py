"""Build the search index for the published static site.

The index is a set of small JavaScript files that ``search.js`` loads with
``<script>`` tags, so search works both from a web server and from a site that
is opened straight from disk (where browsers don't allow ``fetch()``).

Layout, under ``search/`` in the site::

    manifest.js     counts, shard sizes, and the common words that aren't indexed
    i/<n>.js        word shards: for each word, the posts that contain it
    d/<n>.js        post shards: what a search result shows for each post

Posts are numbered 0, 1, 2, ... from oldest to newest ("doc numbers"). A word
goes in shard ``fnv1a(word) % shards``, and its entry lists doc numbers in two
parts separated by ``.``: posts whose topic title contains the word, then the
other posts that contain it. Each part is a sorted list stored as the gaps
between numbers, written in a compact base-32 form (see :func:`encode`).

``search.js`` must split text into words, hash words, and decode lists exactly
as this module does; the tests check both against each other.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from array import array
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

# Words are runs of letters and digits, lowercased, with accents removed.
WORD_RE = re.compile(r"[^\W_]+")
MIN_WORD = 2
MAX_WORD = 40
# A word in more than this share of posts is too common to be worth indexing...
COMMON_WORD_SHARE = 0.10
# ...but only once the archive is big enough for the share to mean something.
COMMON_WORD_MIN_POSTS = 1000
DOCS_PER_SHARD = 500
EXCERPT_LENGTH = 180
# Aim for about this many list entries per word shard.
POSTINGS_PER_SHARD = 15_000

# Base-32 digits: the last digit of each number uses TERMINAL, earlier ones use
# CONTINUE. Neither set needs escaping in a JavaScript or JSON string.
TERMINAL = "0123456789abcdefghijklmnopqrstuv"
CONTINUE = "ABCDEFGHIJKLMNOPQRSTUVWXYZwxyz_-"


def normalize(text: str) -> str:
    """Lowercase, then drop accents: ``São`` becomes ``sao``."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(
        ch for ch in decomposed if not unicodedata.category(ch).startswith("M")
    )


def tokenize(text: str) -> list[str]:
    return [
        w for w in WORD_RE.findall(normalize(text)) if MIN_WORD <= len(w) <= MAX_WORD
    ]


def fnv1a(word: str) -> int:
    """32-bit FNV-1a hash of the word's UTF-8 bytes."""
    h = 0x811C9DC5
    for byte in word.encode("utf-8"):
        h ^= byte
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


def encode(numbers: Iterator[int] | array) -> str:
    """A strictly increasing list of numbers, as base-32 gaps."""
    out: list[str] = []
    previous = -1
    for n in numbers:
        gap = n - previous
        previous = n
        while gap >= 32:
            out.append(CONTINUE[gap & 31])
            gap >>= 5
        out.append(TERMINAL[gap])
    return "".join(out)


def decode(text: str) -> list[int]:
    numbers: list[int] = []
    previous = -1
    value = shift = 0
    for ch in text:
        if (i := CONTINUE.find(ch)) >= 0:
            value |= i << shift
            shift += 5
            continue
        previous += value | (TERMINAL.index(ch) << shift)
        numbers.append(previous)
        value = shift = 0
    return numbers


def excerpt(text: str | None) -> str:
    text = " ".join((text or "").split())
    if len(text) <= EXCERPT_LENGTH:
        return text
    cut = text[:EXCERPT_LENGTH].rsplit(" ", 1)[0]
    return cut + "\N{HORIZONTAL ELLIPSIS}"


@dataclass
class SearchIndex:
    # one entry per post, by doc number: post id, topic id, post number in the
    # topic, author, posted time, excerpt
    docs: list[tuple] = field(default_factory=list)
    titles: dict[int, str] = field(default_factory=dict)
    # word -> (doc numbers whose topic title has the word, other doc numbers)
    words: dict[str, tuple[array, array]] = field(default_factory=dict)
    common_words: list[str] = field(default_factory=list)

    @property
    def shards(self) -> int:
        postings = sum(len(a) + len(b) for a, b in self.words.values())
        shards = 16
        while shards < 4096 and postings / shards > POSTINGS_PER_SHARD:
            shards *= 2
        return shards


def build(
    conn: sqlite3.Connection, step: Callable[[int], None] | None = None
) -> SearchIndex:
    """Index every archived post. ``step(n)`` is called after each post."""
    index = SearchIndex()
    rows = conn.execute(
        """
        SELECT p.post_id, p.topic_id, p.post_number, p.poster_name, p.posted_at,
               p.subject, p.content_text, t.title
        FROM posts p JOIN topics t USING (topic_id)
        ORDER BY p.posted_at, p.post_id
        """
    )
    title_words: dict[int, set[str]] = {}
    for doc, r in enumerate(rows):
        topic_id = r["topic_id"]
        if topic_id not in title_words:
            index.titles[topic_id] = r["title"] or f"Topic {topic_id}"
            title_words[topic_id] = set(tokenize(r["title"] or ""))
        in_title = title_words[topic_id]
        body = set(tokenize(f"{r['subject'] or ''} {r['content_text'] or ''}"))
        for word in in_title | body:
            entry = index.words.get(word)
            if entry is None:
                entry = index.words[word] = (array("I"), array("I"))
            entry[0 if word in in_title else 1].append(doc)
        index.docs.append(
            (
                r["post_id"],
                topic_id,
                r["post_number"],
                r["poster_name"] or "",
                r["posted_at"] or "",
                excerpt(r["content_text"]),
            )
        )
        if step:
            step(doc + 1)
    total = len(index.docs)
    if total >= COMMON_WORD_MIN_POSTS:
        limit = total * COMMON_WORD_SHARE
        index.common_words = sorted(
            w for w, (a, b) in index.words.items() if len(a) + len(b) > limit
        )
        for word in index.common_words:
            del index.words[word]
    return index


def _js(callback: str, *args: object) -> str:
    data = ",".join(json.dumps(a, separators=(",", ":")) for a in args)
    return f"tfaSearch.{callback}({data});\n"


def files(index: SearchIndex) -> Iterator[tuple[str, str]]:
    """The index files, as (path in site, contents)."""
    shards = index.shards
    yield (
        "search/manifest.js",
        _js(
            "manifest",
            {
                "docs": len(index.docs),
                "docsPerShard": DOCS_PER_SHARD,
                "shards": shards,
                "commonWords": index.common_words,
            },
        ),
    )
    by_shard: list[dict[str, str]] = [{} for _ in range(shards)]
    for word in sorted(index.words):
        in_title, other = index.words[word]
        by_shard[fnv1a(word) % shards][word] = f"{encode(in_title)}.{encode(other)}"
    for n, words in enumerate(by_shard):
        yield f"search/i/{n}.js", _js("words", n, words)
    for n in range(0, len(index.docs), DOCS_PER_SHARD):
        docs = index.docs[n : n + DOCS_PER_SHARD]
        topics = {str(d[1]): index.titles[d[1]] for d in docs}
        yield (
            f"search/d/{n // DOCS_PER_SHARD}.js",
            _js("docs", n // DOCS_PER_SHARD, {"topics": topics, "docs": docs}),
        )

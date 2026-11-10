// Search for the published Tech Forum archive.
//
// The index is a set of small script files written by `publish` (see
// search_index.py). They are loaded with <script> tags rather than fetch(), so
// search also works when the site is opened straight from disk.
//
// Splitting text into words, hashing words, and decoding lists must match
// search_index.py exactly; the tests run this file under Node to check that.
"use strict";

const TERMINAL = "0123456789abcdefghijklmnopqrstuv";
const CONTINUE = "ABCDEFGHIJKLMNOPQRSTUVWXYZwxyz_-";
const MIN_WORD = 2;
const MAX_WORD = 40;
const PAGE_SIZE = 20;
const WORD_RE = /[\p{L}\p{N}]+/gu;

function normalize(text) {
  return text.toLowerCase().normalize("NFKD").replace(/\p{M}/gu, "");
}

function tokenize(text) {
  return (normalize(text).match(WORD_RE) || []).filter(
    (w) => w.length >= MIN_WORD && w.length <= MAX_WORD,
  );
}

function fnv1a(word) {
  let h = 0x811c9dc5;
  for (const byte of new TextEncoder().encode(word)) {
    h ^= byte;
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h >>> 0;
}

function decode(text) {
  const numbers = [];
  let previous = -1;
  let value = 0;
  let scale = 1;
  for (const ch of text) {
    const more = CONTINUE.indexOf(ch);
    if (more >= 0) {
      value += more * scale;
      scale *= 32;
      continue;
    }
    previous += value + TERMINAL.indexOf(ch) * scale;
    numbers.push(previous);
    value = 0;
    scale = 1;
  }
  return numbers;
}

// Both lists are sorted; the results are too.
function intersect(a, b) {
  const out = [];
  let i = 0;
  let j = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) {
      out.push(a[i]);
      i++;
      j++;
    } else if (a[i] < b[j]) {
      i++;
    } else {
      j++;
    }
  }
  return out;
}

function merge(a, b) {
  const out = [];
  let i = 0;
  let j = 0;
  while (i < a.length || j < b.length) {
    if (j >= b.length || (i < a.length && a[i] < b[j])) {
      out.push(a[i++]);
    } else if (i >= a.length || b[j] < a[i]) {
      out.push(b[j++]);
    } else {
      out.push(a[i++]);
      j++;
    }
  }
  return out;
}

// Posts that contain every word, newest first, with posts whose topic title
// contains every word ahead of the rest. `entries` are the index entries for
// the words ("titleList.otherList").
function rank(entries) {
  let all = null;
  let inTitle = null;
  for (const entry of entries) {
    const [title, other] = entry.split(".").map(decode);
    const both = merge(title, other);
    all = all === null ? both : intersect(all, both);
    inTitle = inTitle === null ? title : intersect(inTitle, title);
  }
  const titleSet = new Set(inTitle);
  const rest = all.filter((doc) => !titleSet.has(doc));
  return [...inTitle.reverse(), ...rest.reverse()];
}

if (typeof module !== "undefined") {
  module.exports = { normalize, tokenize, fnv1a, decode, intersect, merge, rank };
}

// ------------------------------------------------------------------ browser

if (typeof document !== "undefined") {
  const loaded = new Map(); // script key -> promise of its data
  const waiting = new Map(); // script key -> resolve function

  function load(key, src) {
    if (!loaded.has(key)) {
      loaded.set(
        key,
        new Promise((resolve, reject) => {
          waiting.set(key, resolve);
          const script = document.createElement("script");
          script.src = src;
          script.onerror = () => reject(new Error(`couldn't load ${src}`));
          document.head.append(script);
        }),
      );
    }
    return loaded.get(key);
  }

  function arrived(key, data) {
    const resolve = waiting.get(key);
    if (resolve) {
      waiting.delete(key);
      resolve(data);
    }
  }

  // called by the index files
  window.tfaSearch = {
    manifest: (data) => arrived("manifest", data),
    words: (n, data) => arrived(`i${n}`, data),
    docs: (n, data) => arrived(`d${n}`, data),
  };

  // Text with the searched-for words wrapped in <mark>.
  function highlighted(text, words) {
    const fragment = document.createDocumentFragment();
    let last = 0;
    for (const m of text.matchAll(WORD_RE)) {
      if (!words.has(normalize(m[0]))) continue;
      fragment.append(text.slice(last, m.index));
      const mark = document.createElement("mark");
      mark.textContent = m[0];
      fragment.append(mark);
      last = m.index + m[0].length;
    }
    fragment.append(text.slice(last));
    return fragment;
  }

  function formatDate(iso) {
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return "";
    return date.toLocaleDateString(undefined, { dateStyle: "medium" });
  }

  async function showResults(docs, manifest, words, list, start) {
    const page = docs.slice(start, start + PAGE_SIZE);
    const shards = await Promise.all(
      page.map((doc) => {
        const n = Math.floor(doc / manifest.docsPerShard);
        return load(`d${n}`, `search/d/${n}.js`);
      }),
    );
    page.forEach((doc, i) => {
      const shard = shards[i];
      const [postId, topicId, postNumber, author, postedAt, excerpt] =
        shard.docs[doc % manifest.docsPerShard];
      const title = shard.topics[String(topicId)];
      const item = document.createElement("li");
      item.className = "result";
      const link = document.createElement("a");
      link.className = "title";
      link.href = `t/${topicId}.html#p${postId}`;
      link.append(highlighted(title, words));
      const meta = document.createElement("div");
      meta.className = "meta";
      const parts = [postNumber ? `#${postNumber}` : "", author ? `by ${author}` : "", formatDate(postedAt)];
      meta.textContent = parts.filter(Boolean).join(" · ");
      const text = document.createElement("p");
      text.className = "excerpt";
      text.append(highlighted(excerpt, words));
      item.append(link, meta, text);
      list.append(item);
    });
  }

  async function search(query) {
    const status = document.getElementById("search-status");
    const list = document.getElementById("search-results");
    const more = document.getElementById("search-more");
    list.replaceChildren();
    more.hidden = true;
    let manifest;
    try {
      manifest = await load("manifest", "search/manifest.js");
    } catch {
      status.textContent =
        "The search index is missing. Publish the site again to create it.";
      return;
    }
    const common = new Set(manifest.commonWords);
    const words = [...new Set(tokenize(query))];
    const used = words.filter((w) => !common.has(w));
    const skipped = words.filter((w) => common.has(w));
    const note = skipped.length
      ? ` Very common words aren't searched: ${skipped.join(", ")}.`
      : "";
    if (!words.length) {
      status.textContent = "Type one or more words to search for.";
      return;
    }
    if (!used.length) {
      status.textContent = `Every word you searched for is too common to search for.${note}`;
      return;
    }
    status.textContent = "Searching…";
    let entries;
    try {
      entries = await Promise.all(
        used.map(async (word) => {
          const n = fnv1a(word) % manifest.shards;
          const data = await load(`i${n}`, `search/i/${n}.js`);
          return data[word];
        }),
      );
    } catch (error) {
      status.textContent = `The search index couldn't be loaded (${error.message}). Publish the site again.`;
      return;
    }
    const docs = entries.some((e) => e === undefined) ? [] : rank(entries);
    if (!docs.length) {
      status.textContent = `No posts match.${note}`;
      return;
    }
    const count = docs.length.toLocaleString();
    status.textContent = `${count} ${docs.length === 1 ? "post matches" : "posts match"}, newest first. Posts whose topic title matches are listed first.${note}`;
    const wordSet = new Set(used);
    let shown = 0;
    const showMore = async () => {
      more.hidden = true;
      await showResults(docs, manifest, wordSet, list, shown);
      shown += PAGE_SIZE;
      more.hidden = shown >= docs.length;
    };
    more.onclick = showMore;
    await showMore();
  }

  document.addEventListener("DOMContentLoaded", () => {
    const query = new URLSearchParams(location.search).get("q") || "";
    for (const input of document.querySelectorAll('input[name="q"]')) {
      input.value = query;
    }
    if (document.getElementById("search-results")) {
      search(query);
    }
  });
}

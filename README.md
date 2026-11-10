# tech-forum-archiver

This tool saves a copy of the public [Tech Forum](https://tech.churchofjesuschrist.org/forum/)
in a single SQLite database file on your computer. After the first download, you can run it again
at any time to add new topics and replies. You can search and read the saved posts in a terminal,
and you can turn the archive into a website that works offline or on any web server.

## What the archive contains

The archive holds everything that a visitor who isn't signed in can see on the forum:

- It saves every category, forum, and subforum, with their descriptions.
- It saves every topic, along with its title, author, reply and view counts, and whether it is a
  sticky, an announcement, or locked.
- It saves every post, with its author, date, text, and any edit notes.
- It saves the member details that appear next to each post, such as the member's rank, join date,
  location, website, avatar, and signature.
- It saves polls and their results.
- It saves a list of attachments, and it can also download the attachment files and avatar images
  if you ask it to.

Some things aren't saved because the forum only shows them to signed-in members. These include
full member profiles, the member list, and any forums that are closed to the public.

## Getting started

You need [uv](https://docs.astral.sh/uv/) installed. The first time you run a command, uv
downloads the right version of Python and everything else the tool needs.

Open a terminal in this project's folder, then follow these steps:

1. Run `uv run tech-forum-archiver update` to download the forum. The first download takes about
   two days. You can stop it at any time with Ctrl+C, and the next `update` continues from where it
   stopped.
2. Run the same `update` command again whenever you want to add new topics and replies. A daily
   update usually needs a few hundred pages and finishes in well under an hour.
3. Use `search` and `read` to look through the archive in the terminal.
4. Run `publish` to create a website from the archive.

Each command is described below.

## Commands

Every command follows this pattern:

```sh
uv run tech-forum-archiver [--db FILE] [-v] COMMAND [OPTIONS]
```

The options `--db` and `-v` go before the command name, and they work with every command.

| Option | What it does |
| --- | --- |
| `--db FILE` | This option chooses the archive file. The default is `tech-forum.sqlite` in the current folder. |
| `-v` | This option shows more detail while the tool runs, including a line for each topic and for each page it downloads. |
| `-vv` | This option shows everything that `-v` shows, plus messages from the library that the tool uses to download pages. |

### update

The `update` command creates the archive the first time you run it. After that, it adds anything
that has changed on the forum since the last run.

```sh
uv run tech-forum-archiver update
uv run tech-forum-archiver --db other.sqlite update --attachments --avatars
```

| Option | What it does |
| --- | --- |
| `--forum ID` | This option limits the run to one forum and its subforums. You can repeat it to include several forums. |
| `--topic ID` | This option downloads only the topic with this number and skips the forum's home page and topic lists. You can repeat it to download several topics. |
| `--queued-only` | This option skips the forum's home page and topic lists, and it downloads only the topics that earlier runs found to be new or changed. |
| `--no-topics` | This option updates the forum lists and records which topics are new or changed, but it doesn't download any posts. |
| `--max-topics N` | This option downloads at most N topics in this run. The rest stay in the queue for a later run. A value of 0 means there is no limit. |
| `--full` | This option downloads every page again, which takes about as long as the first download. It is the only way to pick up edits to older posts and posts that were deleted. |
| `--attachments` | This option also downloads the attachment files. |
| `--avatars` | This option also downloads the members' avatar images. |
| `--max-requests N` | This option sets how many pages the tool may download in each time window. The default is 10. |
| `--window SECONDS` | This option sets the length of that time window. The default is 32 seconds. |
| `--base-url URL` | This option sets the forum's address. You shouldn't need to change it. |
| `--user-agent TEXT` | This option sets the name that the tool uses to identify itself to the forum. |

You can stop an update at any time with Ctrl+C. The tool saves its progress continuously, so the
next `update` continues where the last one stopped. Don't run two updates at the same time,
because together they would download pages faster than the forum allows.

While it downloads topics, `update` prints a progress line about every 30 seconds. The line shows
how many topics are done, how many new posts it has found, how many pages it has requested, and an
estimate of the time remaining. It prints the same kind of line while it downloads attachment files
and avatar images. Those lines show how many files are done, how many were downloaded, and how many
couldn't be downloaded. Each kind of download also prints a final line when it finishes.

A normal update only looks for new topics and new replies. If you want to keep working through
topics that an earlier run already found, without checking the forum lists again, use this command:

```sh
uv run tech-forum-archiver update --queued-only
```

This is useful after you stop a long first download. It doesn't find new topics or new replies,
so run a plain `update` for that.

The tool downloads attachment files and avatar images only after it has finished with the topics
in a run. To get files sooner during a long first download, you can work in smaller batches:

```sh
uv run tech-forum-archiver update --queued-only --max-topics 500 --attachments --avatars
```

If a file can't be downloaded, for example because it was removed from the forum, the tool records
the problem and doesn't try that file again.

### stats

The `stats` command shows how much the archive contains.

```sh
uv run tech-forum-archiver stats
```

It lists the number of forums, topics, posts, earlier versions of edited posts, members,
attachments, and polls. It also shows how many attachment files and avatars have been downloaded
and how many topics are still waiting to be downloaded.

Three lines show how much space the downloaded files take. The `attachments_listed_size` line adds
up the sizes that the forum lists for every attachment in the archive, which tells you roughly how
much space `update --attachments` would need to download them all. The forum rounds each size, so
this total is approximate. The `attachments_downloaded_size` line shows the exact size of the
attachment files that have already been downloaded. The `avatars_downloaded_size` line shows the
exact size of the avatar images that have already been downloaded. The forum doesn't list avatar
sizes, so there is no estimate for the avatars that haven't been downloaded yet.

The last line shows the totals that the
forum itself reported the last time an update read the forum's home page, so you can compare the
two.

### search

The `search` command finds archived posts that contain the words you give it. It lists the best
matches first. Each result shows the date of the post, its author, the topic number and title, a
link to the post on the forum, and a short excerpt with the matching words in `[brackets]`.

```sh
uv run tech-forum-archiver search "ward directory"
uv run tech-forum-archiver search 'calendar OR "member tools"' --limit 50
```

| Option | What it does |
| --- | --- |
| `--limit N` | This option shows at most N results. The default is 20. |

A search finds posts that contain all of the words you give it. You can also use these forms:

- Use `OR` between two words to find posts that contain either word.
- Use `NOT` between two words, as in `calendar NOT android`, to find posts that contain the first
  word but not the second. A search can't start with `NOT`.
- Put a phrase in double quotes, such as `"member tools"`, to find those words next to each other.
- End a word with `*`, such as `calend*`, to match every word that starts that way.

The words `AND`, `OR`, and `NOT` must be in capital letters. If a word contains punctuation, such
as `e-mail` or `lds.org`, put it in double quotes, as in `'"e-mail"'`. If the tool can't
understand a search, it says so and explains how to fix it. If nothing matches, it prints
"No matching posts." The full rules are in
[SQLite's full-text search documentation](https://www.sqlite.org/fts5.html#full_text_query_syntax).

### read

The `read` command prints every archived post in one topic, from oldest to newest, so you can read
the whole conversation in the terminal.

```sh
uv run tech-forum-archiver read 26052
```

The topic number is the number after `t=` in the topic's web address. The `search` command shows
it too.

The output starts with the forum the topic belongs to, the topic's title, its number, and its web
address. If the topic has a poll, the poll results come next. Each post then shows its number in
the topic, its author and the author's rank, its date, and any edit notes, followed by the text of
the post. Quoted text is marked with `>` at the start of each line, and code is indented. Times are
shown in your computer's time zone.

If a post has attachments, they are listed under the post with their ID numbers, like this:

```
Attachments (export one with `export-attachment ID`):
  - ID 5925: Screenshot_20260610_084129_Firefox.jpg (339.4 KiB), not downloaded
```

The list includes attachments that also appear inside the text of the post, marked "shown in the
post". It marks attachments whose files haven't been downloaded yet as "not downloaded". You can
save a downloaded attachment as a normal file with the `export-attachment` command.

| Option | What it does |
| --- | --- |
| `--width N` | This option wraps the text to N characters per line. By default, the tool uses the width of your terminal, up to 100 characters. |
| `--no-pager` | This option prints all of the text at once. By default, the tool shows long topics one screen at a time when you run it in a terminal. |

### download-attachment

The `download-attachment` command downloads the files for specific attachments into the archive,
without downloading anything else. The attachment IDs are shown by the `read` command.

```sh
uv run tech-forum-archiver download-attachment 5925
uv run tech-forum-archiver download-attachment 5925 5926 5930
```

| Option | What it does |
| --- | --- |
| `--force` | This option downloads a file again even if it is already in the archive. If the new download fails, the copy that was already in the archive is kept. |

The command reports what happened to each attachment. It skips attachments whose files are already
in the archive, unless you use `--force`. It does try again for attachments whose earlier download
failed. If the forum can't send a file, the command says so and records the problem, and it
finishes with an error code.

An attachment must already be in the archive before you can download its file. The archive learns
about an attachment when it saves the post that contains it, so if an ID isn't in the archive, run
`update --topic` with the number of the topic that contains it first.

This command follows the same speed limit as `update` and continues the same session with the
forum. Don't run it while an update is running, because together they would download pages faster
than the forum allows.

### export-attachment

The `export-attachment` command saves one attachment from the archive as a normal file, so you can
open it with any program. The attachment's ID is shown by the `read` command.

```sh
uv run tech-forum-archiver export-attachment 5925
uv run tech-forum-archiver export-attachment 5925 --out Downloads
```

The first example saves the file in the current folder under the attachment's own name. The second
example saves it in the `Downloads` folder. If the name contains characters that aren't allowed in
file names, the tool replaces them with underscores.

| Option | What it does |
| --- | --- |
| `--out PATH` | This option chooses where to save the file. If PATH is an existing folder, the tool saves the file in that folder under the attachment's own name. Otherwise, PATH is the name of the new file, and its folder must already exist. |
| `--force` | This option replaces the file if it already exists. Without it, the tool refuses to overwrite an existing file. |

The file must already be in the archive, which means it was downloaded by `update --attachments`.
If it wasn't, the tool says so and suggests that command. If an earlier attempt to download the file
failed, for example because the file was removed from the forum, the tool says that instead.

### delete-attachment-data

The `delete-attachment-data` command deletes the downloaded files for one or more attachments from
the archive. Each attachment stays in the archive, along with its name and listed size, exactly as
if it had been found on the forum but not downloaded yet.

```sh
uv run tech-forum-archiver delete-attachment-data 5925
uv run tech-forum-archiver delete-attachment-data 5925 5926 5930
uv run tech-forum-archiver delete-attachment-data --all
```

| Option | What it does |
| --- | --- |
| `--all` | This option deletes every downloaded attachment file in the archive, instead of the attachments you list by ID. It shows how many files it will delete and how much space they take, and it asks you to type "yes" before it deletes anything. |
| `--yes` | This option skips the question that `--all` asks. You need it when you use `--all` in a script or scheduled task, because the tool can't ask a question there. |

Because the attachment is marked as not downloaded, the next `update --attachments` downloads it
again. If the file has since been removed from the forum, that download fails and the file is gone
from the archive for good, so export a copy first if you might want it later. The command also
clears a failed download attempt, which makes the next `update --attachments` try that file again.
With `--all`, it clears every failed download attempt as well.

When you give IDs, the command reports what it deleted for each one. If an ID isn't in the archive,
it says so, carries on with the other IDs, and finishes with an error code.

Deleting files doesn't make the archive file smaller right away. SQLite keeps the freed space inside
the file and reuses it for new data. To shrink the file now, use the `vacuum` command.

### vacuum

The `vacuum` command rebuilds the archive file without its unused space, which makes the file
smaller. Use it after `delete-attachment-data` to give the space back to your disk.

```sh
uv run tech-forum-archiver vacuum
```

The command first shows how much of the archive is marked as unused. When it finishes, it shows the
size of the archive before and after. The file usually shrinks by more than the unused amount,
because the rebuild also packs together data that has become spread out over many updates. Running
the command when nothing has been deleted is safe, and it often still saves some space.

Before you run it, stop any `update` that is running and close any other program that has the
archive open. If another program is writing to the archive, the command says so and changes
nothing. While it runs, it needs free disk space of up to the size of the archive. A large archive
can take a few minutes.

### publish

The `publish` command turns the archive into a website. You can open the website directly from
your computer, without an internet connection, or you can copy it to any web server. The pages
have their own simple design rather than the original forum's. They work on phones, and they
follow each reader's choice of light or dark mode.

```sh
uv run tech-forum-archiver publish
uv run tech-forum-archiver publish --out /srv/www/forum-archive
```

The first example creates a folder named `tech-forum-site` next to the archive file. To view the
website, open `index.html` in that folder with a web browser.

| Option | What it does |
| --- | --- |
| `--out FOLDER` | This option chooses where to write the website. By default, the folder is next to the archive file and is named after it, so `tech-forum.sqlite` produces `tech-forum-site`. |
| `--title TEXT` | This option sets the site name that appears at the top of every page. The default is "Tech Forum Archive". |
| `--no-search` | This option leaves out the search page and its index, along with the search box at the top of each page. |

The website contains these pages and folders:

| Path | Contents |
| --- | --- |
| `index.html` | The home page lists every category and forum. It shows how many topics and posts the archive has for each one, along with the most recent post. |
| `f/<number>.html` | Each forum has a page that lists its subforums and all of its topics. Announcements and sticky topics come first, followed by the topics with the most recent activity. |
| `t/<number>.html` | Each topic has a page that shows **all of its posts on one page**. Each post also has its own link, such as `t/26052.html#p161187`, that opens the page at that post. |
| `u/index.html` | This page lists every member who has posts in the archive. |
| `u/<number>.html` | Each member has a page with their details and a list of their posts. |
| `files/` | This folder holds the attachment files, if you downloaded them with `update --attachments`. |
| `avatars/` | This folder holds the avatar images, if you downloaded them with `update --avatars`. |
| `search.html` | This page searches the archived posts. Every other page has a search box at the top that opens it. |
| `search.js`, `search/` | These files contain the search code and the search index. |
| `style.css` | This file controls the look of the pages. |

The links between pages work no matter which folder or web address the website is in. When a post
links to another topic, forum, or member on the forum, the link goes to the matching page in the
archive if that page exists. If it doesn't exist, the link goes to the original forum. Attachments,
avatars, and pictures that you didn't download are shown from where they are stored online, so
they need an internet connection. For most of them that is the original forum, but some members'
avatars come from the [Gravatar](https://gravatar.com/) service instead. Run
`update --avatars` before you publish to include every avatar in the website. Topics whose posts
haven't been downloaded yet are listed in grey, without a link.

Times are saved in the pages in Coordinated Universal Time (UTC). When the reader's browser allows
JavaScript, it changes them to the reader's own time zone.

Publishing a full archive takes several minutes. While the tool writes the topic pages and the
member pages and while it builds the search index, it prints a progress line about every 30
seconds. The line shows how much is done and an estimate of the time remaining.

#### Searching the website

The search page finds posts that contain every word you type. It lists posts from topics whose
title contains all of the words first, and then the other matching posts. Within each group, the
newest posts come first. Each result shows the topic's title, the post's number in the topic, its
author and date, and the beginning of the post, with the words you searched for highlighted. The
page shows 20 results at a time, and a button at the bottom shows 20 more.

The search works in the browser without a server, both when you open the website straight from your
computer and when it is on a web server. It also works without an internet connection. The browser
needs JavaScript turned on for the search page, but not for the rest of the website.

The search has these limits:

- It matches whole words only. A search for "calendar" doesn't find "calendars", and a search for
  "cal" doesn't find "calendar".
- It ignores letter case and accents, so "Sao Paulo" finds "São Paulo".
- It can't search for an exact phrase. Quotation marks and other punctuation are ignored.
- It leaves out the most common words, which appear in more than 1 post in 10, such as "the" and
  "and". The search page lists any words it left out of your search.
- It doesn't correct spelling mistakes.
- The text under each result is the beginning of the post, which might not include the words you
  searched for.

The search index is built from the archive each time you publish, so it always matches the pages.
For an archive of about 175,000 posts, building the index takes about half a minute and the index
takes about 50 MB of disk space. Each search downloads only the small parts of the index that it
needs.

When you publish again after an update, the tool only rewrites the files that changed. This keeps
uploads small if you use a tool that skips unchanged files. The tool never deletes old files from
the folder, so if you want a completely fresh copy, delete the folder before you publish.

## How the tool works

The forum limits how quickly any one visitor can download pages. A visitor who doesn't keep the
forum's cookies gets one page every 30 seconds. A visitor who keeps the cookies gets 10 pages
every 30 seconds, and any extra requests made while it is being held back still count against it.
The tool saves the forum's cookies and the times of its most recent requests in the archive, so it
stays within the limit even across separate runs. It downloads about one page every 3.2 seconds,
which adds up to about 27,000 pages per day. If the forum still asks it to slow down, it waits until
the forum is ready again.

Because members' profile pages aren't public, the tool works through the forum's public pages in
this order:

1. It reads the forum's home page to find the categories and forums and to record the forum's own
   totals.
2. It reads the list of topics in each forum, 20 topics per page. These lists show the newest
   activity first, so after the first download the tool stops reading a forum's list as soon as it
   reaches a topic that hasn't changed. It adds every new or changed topic to a queue.
3. It downloads each topic in the queue, 10 posts per page. For a new topic, it starts at the first
   page. For a topic that is already in the archive, it starts at the last page and works backwards
   until it reaches posts that it already has. That usually means only one page for each updated
   topic.

The first download needs about 60,000 pages, which takes about two days. After that, a daily
update usually needs a few hundred pages.

A normal update only reads pages that have new posts, so it can't see edits to older posts or posts
that were deleted. `update --full` reads every page again to find those changes. When a post's text
changes, the archive keeps the earlier version. When a post or topic disappears from the forum, the
archive keeps it and records when it disappeared.

While the tool or any other program has the archive open, SQLite keeps two extra files next to it,
whose names end in `-wal` and `-shm`. SQLite removes them when the last program closes the archive.
Don't delete them yourself, because they can hold recently saved changes that aren't in the main
file yet.

## The database

You can open the archive with any SQLite program if you want to explore it directly. Times are
stored in UTC and are written like `2016-10-28T15:42:16Z`. Columns whose names end in `_html`
contain the HTML exactly as the forum showed it. Many links in that HTML leave out the forum's
address. The address they belong to is stored in the `meta` table under `base_url`.

| Table | Contents |
| --- | --- |
| `forums` | Each row is a category or forum. The `parent_id` column links a forum to the category or forum that contains it. |
| `topics` | Each row is a topic, with its forum, title, type (`normal`, `sticky`, `announce`, or `global`), author, counts, and last post. Other columns, such as `needs_fetch`, record the tool's own progress. |
| `posts` | Each row is a post, with its topic, its number in the topic, its author, subject, and date, its text as HTML (`content_html`) and as plain text (`content_text`), and its edit notes. The `removed_at` column is set if the post has disappeared from the forum. |
| `post_revisions` | Each row is an earlier version of a post whose text changed between updates. |
| `users` | Each row is a member who appears on a public page. The details come from the information shown next to the member's posts. |
| `attachments` | Each row describes an attachment. The `data` column holds the file itself if you downloaded it with `--attachments`. |
| `avatars` | Each row is an avatar image, identified by its web address. The `data` column holds the image if you downloaded it with `--avatars`. |
| `polls`, `poll_options` | These tables hold poll questions, choices, and vote counts. |
| `posts_fts` | This table is the search index that the `search` command uses. |
| `crawl_runs` | Each row records one run of `update`, including its options, how many pages it requested, what changed, and the forum's own totals at that time. |
| `meta` | This table holds settings that the tool saves for itself, such as the forum's address and time zone and the saved cookies. |

These example queries show how the tables fit together:

```sql
-- Show every post in topic 26052, oldest first.
SELECT post_number, poster_name, posted_at, content_text
FROM posts WHERE topic_id = 26052 ORDER BY posted_at;

-- List the 20 members with the most posts in the archive.
SELECT u.username, COUNT(*) AS posts
FROM posts p JOIN users u ON u.user_id = p.poster_user_id
GROUP BY u.user_id ORDER BY posts DESC LIMIT 20;

-- Find the 10 posts that best match "calendar sync".
SELECT p.post_id, t.title, snippet(posts_fts, 1, '[', ']', '...', 12)
FROM posts_fts JOIN posts p ON p.post_id = posts_fts.rowid JOIN topics t USING (topic_id)
WHERE posts_fts MATCH 'calendar sync' ORDER BY rank LIMIT 10;
```

## Development

Run the tests with this command:

```sh
uv run pytest
```

The tests never connect to the forum. The page-reading tests use copies of real forum pages saved
in `tests/fixtures/`, and the download tests use a small imitation forum that runs inside the test.

Some company networks inspect secure (HTTPS) connections and use their own security certificates.
The tool checks the forum's certificate using the certificates installed on your computer, so it
works on those networks without extra setup.

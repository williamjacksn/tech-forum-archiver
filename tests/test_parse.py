from pathlib import Path

import pytest

from tech_forum_archiver import parse

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_index() -> None:
    page = parse.parse_index(load("index.html"))
    assert page.stats.total_posts == 255561
    assert page.stats.total_topics == 41394
    assert page.stats.total_members == 64674
    assert page.stats.newest_member == parse.UserRef(773913, "Joecool001")
    assert page.stats.timezone == "America/Denver"

    by_id = {f.forum_id: f for f in page.forums}
    assert by_id[3].forum_type == "category"
    assert by_id[3].parent_id is None
    announcements = by_id[4]
    assert announcements.name == "Announcements & Policies"
    assert announcements.parent_id == 3
    assert announcements.description_html == "The latest forum news and information"
    assert (announcements.topic_count, announcements.post_count) == (67, 581)
    assert announcements.last_post_id == 264034
    assert announcements.last_post_at == "2026-07-29T13:23:43Z"
    assert announcements.last_poster == parse.UserRef(14217, "carsonm")
    # the "Virtual Meetings" link forum (pointing at forum 146) is skipped
    assert "Virtual Meetings" not in [f.name for f in page.forums]
    # description stops before the subforum list
    assert (
        by_id[30].description_html
        == "Discuss ideas and suggestions around the Church website."
    )
    # subforums are not rows on the index
    assert 110 not in by_id


def test_viewforum_with_subforums() -> None:
    page = parse.parse_viewforum(load("viewforum_f30.html"))
    assert page.forum_id == 30
    assert page.breadcrumbs == [
        (18, "Church Technology for Members"),
        (30, "Main Church Website"),
    ]
    assert page.total_topics == 1182
    assert page.next_start == 20
    assert [f.forum_id for f in page.subforums] == [110, 111, 112, 19, 156, 124]
    assert all(f.parent_id == 30 for f in page.subforums)
    assert len(page.topics) == 20
    t = page.topics[0]
    assert t.topic_id == 47249
    assert t.title == "Patriarchal Blessing portal not working"
    assert t.poster == parse.UserRef(773697, "markcain")
    assert t.created_at == "2026-10-26T18:39:41Z"
    assert (t.reply_count, t.last_post_id) == (1, 265022)
    assert t.last_poster == parse.UserRef(127, "russellhltn")
    assert any(t.has_attachments for t in page.topics)
    assert any(t.is_hot for t in page.topics)
    coloured = [
        t.last_poster for t in page.topics if t.last_poster and t.last_poster.colour
    ]
    assert coloured and coloured[0].colour.startswith("#")


def test_viewforum_stickies() -> None:
    page = parse.parse_viewforum(load("viewforum_f4.html"))
    kinds = [(t.topic_id, t.topic_type, t.is_locked) for t in page.topics[:8]]
    assert kinds[0] == (33843, "sticky", True)
    assert kinds[1] == (14468, "sticky", False)
    assert kinds[7] == (47016, "normal", False)
    assert page.subforums == []


def test_viewtopic() -> None:
    page = parse.parse_viewtopic(load("viewtopic_t26052.html"))
    assert page.topic_id == 26052
    assert page.title == "LDSTech is dead?"
    assert page.forum_id == 4
    assert page.total_posts == 34
    assert page.next_start == 10
    assert not page.is_locked
    assert page.poll is None
    assert len(page.posts) == 10
    first = page.posts[0]
    assert first.post_id == 161187
    assert first.post_number == 1
    assert first.subject == "LDSTech is dead?"
    assert first.posted_at == "2016-10-28T15:42:16Z"
    assert first.content_text.startswith("Hi people,\n\nOne question...")
    assert 'class="postlink"' in first.content_html
    prof = first.profile
    assert prof.user == parse.UserRef(1532, "mdantasf")
    assert prof.rank_title == "Member"
    assert prof.post_count == 102
    # "Sat Oct 06, 2007 9:28 pm" America/Denver (MDT, UTC-6)
    assert prof.joined_at == "2007-10-07T03:28:00Z"
    assert prof.fields["phpbb_location"] == {
        "label": "Location",
        "value": "São Paulo, SP, Brasil",
    }
    assert prof.contacts == {"Website": "http://www.mdantas.com.br"}
    third = page.posts[2]
    assert third.profile.avatar_url == "./download/file.php?avatar=91_1355256303.jpg"
    assert (third.profile.avatar_width, third.profile.avatar_height) == (61, 80)
    assert third.signature_html.startswith("“A long habit")


def test_viewtopic_attachment_and_rank_image() -> None:
    page = parse.parse_viewtopic(load("viewtopic_t46914_attachment.html"))
    assert page.forum_id == 30
    assert page.total_posts == 3
    assert page.next_start is None
    att = page.posts[0].attachments
    assert att == [
        parse.Attachment(
            attach_id=5925,
            filename="Screenshot_20260610_084129_Firefox.jpg",
            size_text="339.4 KiB",
            download_count=551,
            is_image=True,
            is_inline=False,
        )
    ]
    employee = page.posts[2].profile
    assert employee.rank_title == "Church Employee"
    assert employee.rank_image_url == "./images/ranks/ldsemployee.png"


def test_edit_notice() -> None:
    html = (
        '<div class="notice">Last edited by '
        '<a href="./memberlist.php?mode=viewprofile&amp;u=127" '
        'class="username">russellhltn</a> on Mon Jan 15, 2018 1:05 pm, '
        "edited 2 times in total."
        "<br /><strong>Reason:</strong> typo</div>"
    )
    notice = parse.soupify(html).div
    edit = parse.parse_edit_notice(notice)
    assert edit == parse.EditInfo(
        count=2,
        user=parse.UserRef(127, "russellhltn"),
        edited_at="2018-01-15T20:05:00Z",  # MST, UTC-7
        reason="typo",
    )


def test_guest_and_deleted_users() -> None:
    assert parse.user_ref(
        parse.soupify('<span class="username">Guest Bob</span>').span
    ) == (parse.UserRef(None, "Guest Bob"))
    el = parse.soupify(
        '<a href="./memberlist.php?mode=viewprofile&amp;u=2" style="color: #aa0000;" '
        'class="username-coloured">admin</a>'
    ).a
    assert parse.user_ref(el) == parse.UserRef(2, "admin", "#AA0000")


def test_poll() -> None:
    html = """
    <form method="post" action="./viewtopic.php?t=5" class="topic_poll">
    <div class="panel">
      <h2 class="poll-title">Favourite <b>app</b>?</h2>
      <p class="author">Poll ended at Mon Jan 15, 2018 1:05 pm</p>
      <fieldset class="polls">
        <dl data-poll-option-id="1"><dt>Gospel Library</dt>
          <dd class="resultbar"><div class="pollbar5" style="width:75%;">3</div></dd>
          <dd class="poll_option_percent">75%</dd></dl>
        <dl data-poll-option-id="2"><dt>Member Tools</dt>
          <dd class="resultbar"><div class="pollbar1" style="width:25%;">1</div></dd>
          <dd class="poll_option_percent">25%</dd></dl>
        <dl><dt>&nbsp;</dt><dd class="resultbar poll_total_votes">Total votes:
          <span class="poll_total_vote_cnt">4</span></dd></dl>
      </fieldset></div></form>
    """
    poll = parse.parse_poll(parse.soupify(html))
    assert poll.question_html == "Favourite <b>app</b>?"
    assert poll.total_votes == 4
    assert [
        (o.option_id, o.option_html, o.votes, o.percent_text) for o in poll.options
    ] == [
        (1, "Gospel Library", 3, "75%"),
        (2, "Member Tools", 1, "25%"),
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Sat Oct 06, 2007 9:28 pm", "2007-10-07T03:28:00Z"),
        ("Thu Jan 24, 2008 4:34 pm", "2008-01-24T23:34:00Z"),
        ("Fri Jan 19, 2007 12:05 am", "2007-01-19T07:05:00Z"),
        ("nonsense", None),
    ],
)
def test_local_to_utc(text: str, expected: str | None) -> None:
    assert parse.local_to_utc(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("339.4 KiB", round(339.4 * 1024)),
        ("2.72 MiB", round(2.72 * 1024**2)),
        ("1.5 GiB", round(1.5 * 1024**3)),
        ("66 Bytes", 66),
        ("1,024 Bytes", 1024),
        ("12 KB", 12_000),
        ("", None),
        (None, None),
        ("big", None),
        ("12 parsecs", None),
    ],
)
def test_size_text_to_bytes(text: str | None, expected: int | None) -> None:
    assert parse.size_text_to_bytes(text) == expected


def test_plain_links_to_attachments_are_not_attachments() -> None:
    # a post that links to another post's attachment and also has its own
    html = """
    <div id="p2" class="post"><div class="postbody"><div class="content">
      You need <a class="postlink"
         href="https://tech.churchofjesuschrist.org/forum/download/file.php?id=4955"
         >https://tech.churchofjesuschrist.org/fo ... hp?id=4955</a>.
      <div class="inline-attachment"><dl class="file">
        <dt><a class="postlink"
               href="./download/file.php?id=4951">scriptures.tar</a></dt>
        <dd>(2.72 MiB) Downloaded 790 times</dd>
      </dl></div>
    </div></div></div>
    """
    post = parse.parse_post(parse.soupify(html).div)
    assert post.attachments == [
        parse.Attachment(
            attach_id=4951,
            filename="scriptures.tar",
            size_text="2.72 MiB",
            download_count=790,
            is_inline=True,
        )
    ]


def test_gravatar_and_uploaded_avatars() -> None:
    # live page: williamjackson uses Gravatar, which the forum shows as a plain
    # <img> without class="avatar"; user 219 has an uploaded avatar
    page = parse.parse_viewtopic(load("viewtopic_t5278_gravatar.html"))
    by_user = {p.profile.user.user_id: p.profile for p in page.posts}
    gravatar = by_user[9492]
    assert gravatar.avatar_url == (
        "//gravatar.com/avatar/"
        "08a98b786db4418b5fffa2087ca77b7f9e01cc24746e4d1c5b210ae5be9f630b?s=90"
    )
    assert (gravatar.avatar_width, gravatar.avatar_height) == (90, 90)
    assert by_user[219].avatar_url == "./download/file.php?avatar=219_1355256303.jpg"
    # members without an avatar still have none
    assert (
        parse.parse_viewtopic(load("viewtopic_t26052.html")).posts[0].profile.avatar_url
        is None
    )

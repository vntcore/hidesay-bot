"""The operations Bot API 2.0 added, on both clients: what goes over the wire, what comes back."""

from __future__ import annotations

import inspect
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from hidesay_bot import AsyncClient, Client, MediaIn, Sticker, _spec
from hidesay_bot.errors import (
    APIError,
    BadMediaError,
    BadRequestError,
    ChatFullError,
    ChatIsProtectedError,
    ChatIsPublicError,
    CommentsClosedError,
    ConflictError,
    ForbiddenError,
    InviteNotFoundError,
    JoinRequestNotFoundError,
    NotFoundError,
    PayloadTooLargeError,
    ReadsAllNeededError,
    RequestTimeoutError,
    StickerNotFoundError,
    ValidationError,
    WrongBodyKindError,
    error_from_envelope,
)
from hidesay_bot.models import KEEP, ChatKind

from .conftest import FAST_RETRY
from .fakeapi import BASE, BOT, MEDIA_ID, TOKEN, FakeAPI, channel, group, person

PRIVATE = person(7781)
GROUP = group(9100)
CHANNEL = channel(9200)
ANNA = person(55)
BRUNO = person(56)
POST = 2001
SEND_ID = "6f9619ff-8b86-4011-b42d-00c04fc964ff"
WHEN = datetime(2026, 10, 3, 4, 0, tzinfo=UTC)
WHEN_MS = 1791000000000


class Either:
    """Both clients behind one face: every method is awaited, whichever flavour is underneath."""

    def __init__(self, inner: AsyncClient | Client) -> None:
        self.inner = inner

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self.inner, name)

        async def call(*args: Any, **kwargs: Any) -> Any:
            result = attr(*args, **kwargs)
            return await result if inspect.isawaitable(result) else result

        return call


@pytest.fixture(params=["async", "sync"])
async def client(request: pytest.FixtureRequest, fake: FakeAPI) -> AsyncIterator[Either]:
    cls = AsyncClient if request.param == "async" else Client
    inner = cls(TOKEN, api_url=BASE, transport=fake, retry=FAST_RETRY, rate_limiter=False)
    yield Either(inner)
    if isinstance(inner, AsyncClient):
        await inner.aclose()
    else:
        inner.close()


def message(chat: str, message_id: int, **fields: Any) -> dict[str, Any]:
    kind = {"G": "group", "C": "channel"}.get(chat[0], "private")
    return {
        "id": message_id,
        "chat": {"id": chat, "kind": kind},
        "from": {"id": ANNA, "username": "anna", "firstName": "Anna", "isBot": False},
        "date": WHEN_MS,
        "kind": "user",
        **fields,
    }


@pytest.fixture
def world(fake: FakeAPI) -> FakeAPI:
    """A readable chat with Anna, a readable group, a channel with a post, and what the bot was handed there."""
    fake.add_chat(PRIVATE)
    fake.add_chat(
        GROUP,
        "group",
        title="Hikers",
        history_visible=True,
        members=[
            {"user": {"id": ANNA, "username": "anna", "isBot": False}, "status": "member", "readsAll": False},
            {"user": {"id": BRUNO, "username": "bruno", "isBot": False}, "status": "member"},
        ],
    )
    fake.add_chat(CHANNEL, "channel", title="News", history_visible=True)
    fake.hand(PRIVATE, message(PRIVATE, 1001, text="hello"))
    fake.hand(GROUP, message(GROUP, 1101, text="/start"))
    fake.hand(GROUP, message(GROUP, 1102, text="/help", replyToMessage=message(GROUP, 1101, text="/start")))
    fake.hand(CHANNEL, message(CHANNEL, POST, text="the news", **{"from": None}))
    fake.hand(CHANNEL, message(CHANNEL, 2002, text="nice", threadRoot=POST))
    fake.stickers[77] = {"id": 77, "packId": 5, "mediaId": MEDIA_ID, "emoji": "👍", "public": True}
    fake.requests.clear()
    return fake


def sent(fake: FakeAPI) -> dict[str, Any]:
    [request] = fake.requests
    return request


# --- the wire ---

WIRE = [
    pytest.param(
        "delete_messages",
        (GROUP, [1101, 1102]),
        {},
        "POST",
        "/bot/v1/messages/delete",
        {},
        {"chatId": GROUP, "ids": [1101, 1102]},
        id="delete_messages",
    ),
    pytest.param(
        "set_reaction",
        (PRIVATE, 1001, " 👍 "),
        {},
        "PUT",
        "/bot/v1/messages/1001/reaction",
        {},
        {"chatId": PRIVATE, "emoji": "👍"},
        id="set_reaction",
    ),
    pytest.param(
        "remove_reaction",
        (PRIVATE, 1001),
        {},
        "DELETE",
        "/bot/v1/messages/1001/reaction",
        {"chatId": PRIVATE},
        None,
        id="remove_reaction",
    ),
    pytest.param("get_sticker", (77,), {}, "GET", "/bot/v1/stickers/77", {}, None, id="get_sticker"),
    pytest.param(
        "get_sticker",
        (77, MEDIA_ID),
        {},
        "GET",
        "/bot/v1/stickers/77",
        {"mediaId": MEDIA_ID},
        None,
        id="get_sticker-proof",
    ),
    pytest.param("get_chats", (), {}, "GET", "/bot/v1/chats", {}, None, id="get_chats"),
    pytest.param(
        "get_history",
        (GROUP,),
        {"before": 1102, "limit": 10},
        "GET",
        f"/bot/v1/chats/{GROUP}/messages",
        {"before": "1102", "limit": "10"},
        None,
        id="get_history",
    ),
    pytest.param(
        "get_message", (GROUP, 1101), {}, "GET", f"/bot/v1/chats/{GROUP}/messages/1101", {}, None, id="get_message"
    ),
    pytest.param("read_chat", (PRIVATE,), {}, "POST", f"/bot/v1/chats/{PRIVATE}/read", {}, {}, id="read_chat"),
    pytest.param(
        "read_chat",
        (PRIVATE, 1001),
        {},
        "POST",
        f"/bot/v1/chats/{PRIVATE}/read",
        {},
        {"upTo": 1001},
        id="read_chat-up_to",
    ),
    pytest.param(
        "pin_message", (GROUP, 1101), {}, "PUT", f"/bot/v1/chats/{GROUP}/pins/1101", {}, None, id="pin_message"
    ),
    pytest.param(
        "unpin_message", (GROUP, 1101), {}, "DELETE", f"/bot/v1/chats/{GROUP}/pins/1101", {}, None, id="unpin_message"
    ),
    pytest.param("unpin_all", (GROUP,), {}, "DELETE", f"/bot/v1/chats/{GROUP}/pins", {}, None, id="unpin_all"),
    pytest.param(
        "get_comments",
        (CHANNEL, POST),
        {"limit": 5},
        "GET",
        f"/bot/v1/chats/{CHANNEL}/posts/{POST}/comments",
        {"limit": "5"},
        None,
        id="get_comments",
    ),
    pytest.param(
        "send_comment",
        (CHANNEL, POST, "thanks"),
        {"reply_to": 2002, "as_author": True, "mentions": [ANNA]},
        "POST",
        f"/bot/v1/chats/{CHANNEL}/posts/{POST}/comments",
        {},
        {"text": "thanks", "replyTo": 2002, "asAuthor": True, "mentions": [ANNA]},
        id="send_comment",
    ),
    pytest.param(
        "update_chat",
        (GROUP,),
        {"title": " Hikers ", "description": None, "permissions": {"send_media": False, "react": True}},
        "PATCH",
        f"/bot/v1/chats/{GROUP}",
        {},
        {"title": "Hikers", "description": None, "permissions": {"sendMedia": False, "react": True}},
        id="update_chat",
    ),
    pytest.param(
        "update_chat",
        (CHANNEL,),
        {"signatures": True, "allowed_reactions": None, "auto_delete_seconds": 0},
        "PATCH",
        f"/bot/v1/chats/{CHANNEL}",
        {},
        {"signatures": True, "allowedReactions": None, "autoDeleteSeconds": 0},
        id="update_chat-channel",
    ),
    pytest.param(
        "update_chat",
        (GROUP,),
        {"reactions_enabled": True, "allowed_reactions": [" 👍 ", "👍" * 8]},
        "PATCH",
        f"/bot/v1/chats/{GROUP}",
        {},
        {"reactionsEnabled": True, "allowedReactions": ["👍", "👍" * 8]},
        id="update_chat-reactions",
    ),
    pytest.param(
        "update_member",
        (GROUP, ANNA),
        {"role": "admin", "admin_rights": ["delete_messages", "pinMessages"], "custom_title": "guide"},
        "PATCH",
        f"/bot/v1/chats/{GROUP}/members/{ANNA}",
        {},
        {"role": "admin", "adminRights": ["deleteMessages", "pinMessages"], "customTitle": "guide"},
        id="update_member-promote",
    ),
    pytest.param(
        "update_member",
        (GROUP, ANNA),
        {"restrictions": ["send_media"], "restricted_until": WHEN},
        "PATCH",
        f"/bot/v1/chats/{GROUP}/members/{ANNA}",
        {},
        {"restrictions": ["sendMedia"], "restrictedUntil": WHEN_MS},
        id="update_member-restrict",
    ),
    pytest.param(
        "remove_member",
        (GROUP, ANNA),
        {},
        "DELETE",
        f"/bot/v1/chats/{GROUP}/members/{ANNA}",
        {},
        None,
        id="remove_member",
    ),
    pytest.param(
        "remove_member",
        (GROUP, ANNA),
        {"ban": True},
        "DELETE",
        f"/bot/v1/chats/{GROUP}/members/{ANNA}",
        {"ban": "true"},
        None,
        id="remove_member-ban",
    ),
    pytest.param(
        "remove_member",
        (GROUP, ANNA),
        {"ban": True, "banned_until": WHEN},
        "DELETE",
        f"/bot/v1/chats/{GROUP}/members/{ANNA}",
        {"ban": "true"},
        {"ban": True, "bannedUntil": WHEN_MS},
        id="remove_member-ban-until",
    ),
    pytest.param(
        "purge_member",
        (GROUP, ANNA),
        {"up_to": 1102},
        "DELETE",
        f"/bot/v1/chats/{GROUP}/members/{ANNA}/messages",
        {"upTo": "1102"},
        None,
        id="purge_member",
    ),
    pytest.param("get_bans", (GROUP,), {}, "GET", f"/bot/v1/chats/{GROUP}/bans", {}, None, id="get_bans"),
    pytest.param("unban", (GROUP, ANNA), {}, "DELETE", f"/bot/v1/chats/{GROUP}/bans/{ANNA}", {}, None, id="unban"),
    pytest.param("get_invites", (GROUP,), {}, "GET", f"/bot/v1/chats/{GROUP}/invites", {}, None, id="get_invites"),
    pytest.param(
        "create_invite",
        (GROUP,),
        {"label": " flyer ", "expires_at": WHEN, "max_uses": 100, "needs_approval": True},
        "POST",
        f"/bot/v1/chats/{GROUP}/invites",
        {},
        {"label": "flyer", "expiresAt": WHEN_MS, "maxUses": 100, "needsApproval": True},
        id="create_invite",
    ),
    pytest.param(
        "get_join_requests", (GROUP,), {}, "GET", f"/bot/v1/chats/{GROUP}/requests", {}, None, id="get_join_requests"
    ),
    pytest.param(
        "set_comments",
        (CHANNEL, True),
        {"signing": True},
        "PATCH",
        f"/bot/v1/chats/{CHANNEL}/comments",
        {},
        {"enabled": True, "signing": True},
        id="set_comments",
    ),
    pytest.param(
        "set_post_comments",
        (CHANNEL, POST, True),
        {},
        "PATCH",
        f"/bot/v1/chats/{CHANNEL}/posts/{POST}",
        {},
        {"commentsOff": True},
        id="set_post_comments",
    ),
    pytest.param(
        "get_channel_stats", (CHANNEL,), {}, "GET", f"/bot/v1/chats/{CHANNEL}/stats", {}, None, id="get_channel_stats"
    ),
    pytest.param(
        "delete_chat_photo", (GROUP,), {}, "DELETE", f"/bot/v1/chats/{GROUP}/photo", {}, None, id="delete_chat_photo"
    ),
    pytest.param(
        "delete_chat_banner",
        (GROUP,),
        {},
        "DELETE",
        f"/bot/v1/chats/{GROUP}/banner",
        {},
        None,
        id="delete_chat_banner",
    ),
]


@pytest.mark.parametrize(("op", "args", "kwargs", "method", "path", "query", "body"), WIRE)
async def test_every_new_operation_goes_out_as_the_api_describes_it(
    client: Either,
    world: FakeAPI,
    op: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    method: str,
    path: str,
    query: dict[str, str],
    body: Any,
) -> None:
    world.join_requests[GROUP] = [{"user": {"id": ANNA}, "date": WHEN_MS}]
    world.bans[GROUP] = [{"user": {"id": ANNA}, "bannedUntil": None, "bannedAt": WHEN_MS}]
    world.chats[GROUP].settings["photoUrl"] = f"/media/{MEDIA_ID}"
    world.chats[GROUP].settings["bannerUrl"] = f"/media/{MEDIA_ID}"
    world.chats[GROUP].me = {"status": "admin", "readsAll": True}
    await getattr(client, op)(*args, **kwargs)
    request = sent(world)
    assert (request["method"], request["path"], request["query"], request["json"]) == (method, path, query, body)


async def test_send_message_puts_files_a_sticker_and_mentions_on_the_wire(client: Either, world: FakeAPI) -> None:
    world.media[MEDIA_ID] = b"jpeg"
    media = MediaIn(id=MEDIA_ID.upper(), kind="image", mime="image/jpeg", width=640, height=480, duration_ms=None)
    await client.send_message(GROUP, "  ", media=[media], mentions=[ANNA, 10000056], send_id=SEND_ID)
    await client.send_message(GROUP, sticker=Sticker(id=77, media_id=MEDIA_ID), send_id=None)
    await client.send_message(GROUP, "look", media={"id": MEDIA_ID, "kind": "file", "name": "notes.txt"})

    first, second, third = (r["json"] for r in world.calls("POST", "/bot/v1/messages"))
    # a blank caption is no caption
    assert first == {
        "chatId": GROUP,
        "media": [{"id": MEDIA_ID, "kind": "image", "mime": "image/jpeg", "width": 640, "height": 480}],
        "mentions": [ANNA, BRUNO],
        "sendId": SEND_ID,
    }
    assert second["sticker"] == {"id": 77, "mediaId": MEDIA_ID}
    assert "text" not in second
    assert third["text"] == "look"
    assert third["media"] == [{"id": MEDIA_ID, "kind": "file", "name": "notes.txt"}]


@pytest.mark.parametrize(
    ("kwargs", "field"),
    [
        ({"media": [], "text": "x"}, "media"),
        ({"media": [{"id": MEDIA_ID, "kind": "image"}] * 11}, "media"),
        ({"media": [{"id": MEDIA_ID, "kind": "image"}, {"id": MEDIA_ID, "kind": "file"}]}, "media"),
        ({"media": [{"id": "not-a-uuid", "kind": "image"}]}, "media"),
        ({"media": [{"id": MEDIA_ID, "kind": "picture"}]}, "media"),
        ({"media": [{"id": MEDIA_ID, "kind": "file", "name": "a/b.txt"}]}, "media"),
        ({"media": [{"id": MEDIA_ID, "kind": "image", "width": 0}]}, "media"),
        ({"media": [{"id": MEDIA_ID, "kind": "image", "sha256": "abc"}]}, "media"),
        ({"media": [{"id": MEDIA_ID, "kind": "image", "colour": "red"}]}, "media"),
        ({"media": [{"id": MEDIA_ID, "kind": "image"}], "armored": "-----BEGIN PGP MESSAGE-----"}, "armored"),
        ({"sticker": 77, "media": [{"id": MEDIA_ID, "kind": "image"}]}, "sticker"),
        ({"sticker": 77, "text": "hi"}, "text"),
        ({"sticker": 0}, "sticker"),
        ({"sticker": Sticker(id=None, media_id=MEDIA_ID)}, "sticker"),
        ({"text": "hi", "mentions": ["G10009100"]}, "mentions"),
        ({"text": "hi", "mentions": [ANNA] * 21}, "mentions"),
    ],
)
async def test_bad_files_stickers_and_mentions_are_refused_before_anything_is_sent(
    client: Either, world: FakeAPI, kwargs: dict[str, Any], field: str
) -> None:
    with pytest.raises(ValidationError) as caught:
        await client.send_message(GROUP, **kwargs)
    assert caught.value.field == field
    assert world.requests == []


async def test_a_file_that_is_not_the_bots_is_bad_media(client: Either, world: FakeAPI) -> None:
    with pytest.raises(BadMediaError):
        await client.send_message(GROUP, media=[{"id": MEDIA_ID, "kind": "image"}])


async def test_files_into_an_encrypted_chat_come_back_with_the_body_it_expects(client: Either, world: FakeAPI) -> None:
    world.chats[GROUP].history_visible = False
    world.media[MEDIA_ID] = b"jpeg"
    with pytest.raises(WrongBodyKindError) as caught:
        await client.send_message(GROUP, media=[{"id": MEDIA_ID, "kind": "image"}])
    assert caught.value.expected == "armored"


async def test_upload_file_sends_the_bytes_as_multipart_under_a_neutral_name(client: Either, world: FakeAPI) -> None:
    uploaded = await client.upload_file(b"\x00secret bytes\xff")

    request = sent(world)
    assert (request["method"], request["path"], request["json"]) == ("POST", "/bot/v1/attachments", None)
    assert request["files"] == {"file": b"\x00secret bytes\xff"}
    assert request["headers"]["content-type"].startswith("multipart/form-data; boundary=")
    # nothing about the file travels in clear but its bytes
    assert b'name="file"; filename="blob"' in request["raw"]
    assert b"Content-Type: application/octet-stream" in request["raw"]
    assert world.media[uploaded.id] == b"\x00secret bytes\xff"
    assert (uploaded.url, uploaded.size) == (f"/media/{uploaded.id}", 14)


async def test_an_upload_over_the_limit_is_refused_before_anything_is_sent(client: Either, world: FakeAPI) -> None:
    with pytest.raises(ValidationError) as caught:
        await client.upload_file(b"x" * 11, limit=10)
    assert caught.value.field == "data"
    with pytest.raises(ValidationError):
        await client.upload_file("not bytes")
    assert world.requests == []


async def test_a_file_the_server_finds_too_large_is_payload_too_large(client: Either, fake: FakeAPI) -> None:
    fake.fail("POST", "/attachments$", status=413, code="too_large")
    with pytest.raises(PayloadTooLargeError):
        await client.upload_file(b"x")


async def test_a_chat_picture_goes_up_as_multipart_and_is_never_repeated(client: Either, world: FakeAPI) -> None:
    picture = await client.set_chat_photo(GROUP, b"\x89PNG....")
    assert (picture.mime, picture.url.startswith("/media/")) == ("image/png", True)
    banner = await client.set_chat_banner(GROUP, b"\xff\xd8\xff....")
    assert banner.mime == "image/jpeg"
    photo, ban = world.calls("PUT")
    assert (photo["path"], ban["path"]) == (f"/bot/v1/chats/{GROUP}/photo", f"/bot/v1/chats/{GROUP}/banner")
    assert photo["files"] == {"file": b"\x89PNG...."}

    world.requests.clear()
    world.fail("PUT", "/photo$", after=True)
    with pytest.raises(APIError if False else Exception):
        await client.set_chat_photo(GROUP, b"\x89PNG....")
    assert len(world.calls("PUT")) == 1
    world.requests.clear()
    world.fail("DELETE", "/photo$", after=True)
    with pytest.raises(RequestTimeoutError):
        await client.delete_chat_photo(GROUP)
    assert len(world.calls("DELETE")) == 1


async def test_a_picture_that_is_not_one_is_refused_by_the_server(client: Either, world: FakeAPI) -> None:
    with pytest.raises(APIError) as caught:
        await client.set_chat_photo(GROUP, b"GIF89a")
    assert (caught.value.status, caught.value.code) == (415, "bad_request")


# --- what comes back ---


async def test_history_and_one_message_come_back_as_messages(client: Either, world: FakeAPI) -> None:
    history = await client.get_history(GROUP)
    assert [m.id for m in history] == [1102, 1101]
    quoted = history[0].reply_to_message
    assert quoted is not None
    assert (quoted.id, quoted.text) == (1101, "/start")
    one = await client.get_message(GROUP, 1101)
    assert (one.id, one.text, one.chat.id) == (1101, "/start", GROUP)


async def test_comments_come_with_whether_the_post_takes_them(client: Either, world: FakeAPI) -> None:
    page = await client.get_comments(CHANNEL, POST)
    assert page.open
    [comment] = page.messages
    assert (comment.id, comment.thread_root, comment.is_comment) == (2002, POST, True)


async def test_a_comment_is_sent_under_its_post_and_never_repeated(client: Either, world: FakeAPI) -> None:
    comment = await client.send_comment(CHANNEL, POST, "thanks")
    assert (comment.thread_root, comment.text) == (POST, "thanks")
    world.requests.clear()
    world.fail("POST", "/comments$", after=True)
    with pytest.raises(RequestTimeoutError):
        await client.send_comment(CHANNEL, POST, "once")
    assert len(world.calls("POST")) == 1


async def test_closed_comments_answer_comments_closed(client: Either, world: FakeAPI) -> None:
    world.chats[CHANNEL].settings["commentsClosed"] = True
    with pytest.raises(CommentsClosedError):
        await client.send_comment(CHANNEL, POST, "too late")


async def test_reactions_pins_and_read_marks(client: Either, world: FakeAPI) -> None:
    reactions = await client.set_reaction(PRIVATE, 1001, "👍")
    assert reactions.message_id == 1001
    assert [(r.emoji, r.mine) for r in reactions.reactions] == [("👍", True)]
    assert reactions.counts == {"👍": 1}
    assert await client.remove_reaction(PRIVATE, 1001) is True
    assert await client.remove_reaction(PRIVATE, 1001) is False
    assert await client.pin_message(GROUP, 1101) == [1101]
    assert await client.pin_message(GROUP, 1102) == [1101, 1102]
    assert await client.unpin_message(GROUP, 1101) == [1102]
    assert await client.unpin_all(GROUP) == []
    assert await client.read_chat(PRIVATE) == 1001


async def test_marking_a_room_read_needs_the_room_to_let_the_bot_read_everything(
    client: Either, world: FakeAPI
) -> None:
    with pytest.raises(ReadsAllNeededError):
        await client.read_chat(GROUP)
    world.chats[GROUP].me["readsAll"] = True
    assert await client.read_chat(GROUP, 1101) == 1101


async def test_get_chats_lists_the_rooms_with_the_bots_seat(client: Either, world: FakeAPI) -> None:
    world.chats[GROUP].me = {"status": "member", "readsAll": True, "permissions": {"sendMessages": True}}
    chats = {c.id: c for c in await client.get_chats()}
    assert set(chats) == {GROUP, CHANNEL}
    assert chats[GROUP].reads_all
    assert chats[GROUP].me is not None
    assert chats[GROUP].me.permissions is not None
    assert chats[GROUP].me.permissions.send_messages
    assert not chats[CHANNEL].reads_all


async def test_a_sticker_of_a_private_pack_needs_its_proof(client: Either, world: FakeAPI) -> None:
    world.stickers[78] = {"id": 78, "packId": 6, "mediaId": MEDIA_ID, "emoji": None, "public": False}
    with pytest.raises(StickerNotFoundError):
        await client.get_sticker(78)
    sticker = await client.get_sticker(78, MEDIA_ID)
    assert (sticker.id, sticker.pack_id, sticker.media_id, sticker.emoji) == (78, 6, MEDIA_ID, None)
    assert sticker.url == f"/media/{MEDIA_ID}"


async def test_a_profile_never_has_a_phone_number(client: Either, world: FakeAPI) -> None:
    world.profiles[ANNA] = {
        "id": ANNA,
        "username": "anna",
        "usernames": ["anna_r"],
        "firstName": "Anna",
        "lastName": "Rossi",
        "bio": "hiking",
        "avatarUrl": f"/media/{MEDIA_ID}",
        "avatarMime": "image/jpeg",
        "bannerUrl": None,
        "bannerMime": None,
        "isBot": False,
        "hasKey": True,
        "fingerprint": "AB" * 20,
    }
    profile = await client.get_user(ANNA)
    assert (profile.full_name, profile.mention, profile.usernames, profile.bio) == (
        "Anna Rossi",
        "@anna",
        ("anna_r",),
        "hiking",
    )
    assert (profile.has_key, profile.fingerprint, profile.banner_url) == (True, "AB" * 20, None)


async def test_running_a_room(client: Either, world: FakeAPI) -> None:
    room = await client.update_chat(GROUP, slow_mode_seconds=30, permissions={"send_media": False})
    assert room.slow_mode_seconds == 30
    assert room.permissions is not None
    assert not room.permissions.send_media

    member = await client.update_member(GROUP, ANNA, role="admin", admin_rights=["ban_users"])
    assert member.is_admin
    assert member.can is not None
    assert member.can.ban_users

    assert await client.remove_member(GROUP, BRUNO, ban=True) is True
    [ban] = await client.get_bans(GROUP)
    assert ban.user.id == BRUNO
    assert await client.unban(GROUP, BRUNO) is True
    assert await client.unban(GROUP, BRUNO) is False
    assert await client.purge_member(GROUP, ANNA) is True

    invite = await client.create_invite(GROUP, label="flyer", needs_approval=True)
    assert (invite.code, invite.label, invite.needs_approval, invite.uses) == ("code1", "flyer", True, 0)
    assert [i.code for i in await client.get_invites(GROUP)] == ["code1"]
    assert await client.revoke_invite(GROUP, "code1") is True
    with pytest.raises(InviteNotFoundError):
        await client.revoke_invite(GROUP, "code1")

    world.join_requests[GROUP] = [{"user": {"id": ANNA, "username": "anna"}, "date": WHEN_MS}]
    [request] = await client.get_join_requests(GROUP)
    assert (request.chat.id, request.chat.kind, request.from_user.id, request.date) == (
        GROUP,
        ChatKind.GROUP,
        ANNA,
        WHEN,
    )
    assert await client.decide_join_request(GROUP, ANNA, True) is True
    with pytest.raises(JoinRequestNotFoundError):
        await client.decide_join_request(GROUP, ANNA, False)


async def test_a_public_chat_has_no_invite_links(client: Either, world: FakeAPI) -> None:
    world.chats[GROUP].username = "hikers"
    with pytest.raises(ChatIsPublicError):
        await client.create_invite(GROUP)


async def test_a_channel_and_its_comments(client: Either, world: FakeAPI) -> None:
    world.stats = {
        "subscribers": 1280,
        "windows": [{"days": 7, "joined": 12, "left": 3}, {"days": 30, "joined": 40, "left": 9}],
        "growth": [{"at": WHEN_MS, "joined": 2, "left": 1}],
        "posts": 2,
        "totalViews": 900,
        "averageViews": 450,
        "topPosts": [{"id": POST, "views": 800, "date": WHEN_MS}],
    }
    stats = await client.get_channel_stats(CHANNEL)
    assert (stats.subscribers, stats.posts, stats.total_views, stats.average_views) == (1280, 2, 900, 450)
    assert [(w.days, w.joined, w.left) for w in stats.windows] == [(7, 12, 3), (30, 40, 9)]
    assert (stats.growth[0].at, stats.top_posts[0].id, stats.top_posts[0].views) == (WHEN, POST, 800)

    chat = await client.set_comments(CHANNEL, True, signing=True)
    assert (chat.comments_enabled, chat.comment_signing) == (True, True)
    assert await client.set_post_comments(CHANNEL, POST, True) is True


@pytest.mark.parametrize(
    ("op", "args", "kwargs", "field"),
    [
        ("delete_messages", (GROUP, []), {}, "message_ids"),
        ("delete_messages", (GROUP, list(range(1, 102))), {}, "message_ids"),
        ("set_reaction", (PRIVATE, 1001, "   "), {}, "emoji"),
        ("set_reaction", (PRIVATE, 1001, "x" * 49), {}, "emoji"),
        ("get_history", (GROUP,), {"limit": 101}, "limit"),
        ("get_history", (GROUP,), {"before": -1}, "before"),
        ("unpin_all", (PRIVATE,), {}, "chat_id"),
        ("get_comments", (GROUP, POST), {}, "chat_id"),
        ("send_comment", (CHANNEL, POST), {}, "text"),
        ("get_channel_stats", (GROUP,), {}, "chat_id"),
        ("update_chat", (GROUP,), {}, None),
        ("update_chat", (CHANNEL,), {"slow_mode_seconds": 10}, "slowModeSeconds"),
        ("update_chat", (GROUP,), {"signatures": True}, "signatures"),
        ("update_chat", (GROUP,), {"slow_mode_seconds": 3601}, "slow_mode_seconds"),
        ("update_chat", (GROUP,), {"permissions": {"fly": True}}, "permissions"),
        ("update_chat", (GROUP,), {"permissions": {"react": "yes"}}, "permissions"),
        ("update_chat", (GROUP,), {"title": "  "}, "title"),
        ("update_chat", (GROUP,), {"allowed_reactions": ["x"] * 41}, "allowed_reactions"),
        # a reaction may be 48 characters, the room's list takes 16
        ("update_chat", (GROUP,), {"allowed_reactions": ["👍" * 9]}, "allowed_reactions"),
        ("update_member", (GROUP, ANNA), {}, None),
        ("update_member", (GROUP, ANNA), {"role": "owner"}, "role"),
        ("update_member", (GROUP, ANNA), {"admin_rights": "ban_users"}, "admin_rights"),
        ("update_member", (GROUP, ANNA), {"restricted_until": WHEN}, "restricted_until"),
        (
            "update_member",
            (GROUP, ANNA),
            # a moment without a timezone is no moment at all
            {"restrictions": [], "restricted_until": datetime(2026, 1, 1)},  # noqa: DTZ001
            "restricted_until",
        ),
        ("update_member", (GROUP, ANNA), {"custom_title": "x" * 17}, "custom_title"),
        ("remove_member", (GROUP, ANNA), {"banned_until": WHEN}, "banned_until"),
        ("remove_member", (PRIVATE, ANNA), {}, "chat_id"),
        ("create_invite", (GROUP,), {"max_uses": 0}, "max_uses"),
        ("create_invite", (GROUP,), {"label": "x" * 49}, "label"),
        ("revoke_invite", (GROUP, ".."), {}, "code"),
        ("decide_join_request", (GROUP, ANNA, "yes"), {}, "approve"),
        ("get_sticker", (0,), {}, "sticker"),
    ],
)
async def test_bad_arguments_of_the_new_operations_are_refused_before_anything_is_sent(
    client: Either, world: FakeAPI, op: str, args: tuple[Any, ...], kwargs: dict[str, Any], field: str | None
) -> None:
    with pytest.raises(ValidationError) as caught:
        await getattr(client, op)(*args, **kwargs)
    assert caught.value.field == field
    assert world.requests == []


async def test_keep_leaves_a_field_alone_and_none_clears_it(client: Either, world: FakeAPI) -> None:
    await client.update_chat(GROUP, title="Hikers", description=KEEP)
    await client.update_chat(GROUP, description=None)
    first, second = (r["json"] for r in world.calls("PATCH"))
    assert first == {"title": "Hikers"}
    assert second == {"description": None}


@pytest.mark.parametrize(
    ("op", "args", "path", "status", "code", "value"),
    [
        ("remove_member", (GROUP, ANNA), f"/members/{ANNA}$", 404, "member_not_found", False),
        ("revoke_invite", (GROUP, "code9"), "/invites/code9$", 404, "invite_not_found", True),
        ("decide_join_request", (GROUP, ANNA, True), f"/requests/{ANNA}$", 404, "request_not_found", True),
    ],
)
async def test_a_done_code_after_a_lost_answer_means_the_first_try_worked(
    client: Either, world: FakeAPI, op: str, args: tuple[Any, ...], path: str, status: int, code: str, value: Any
) -> None:
    world.fail("DELETE" if op != "decide_join_request" else "POST", path, after=True)
    world.fail("DELETE" if op != "decide_join_request" else "POST", path, status=status, code=code)
    assert await getattr(client, op)(*args) == value


async def test_an_invite_is_never_made_twice_by_a_retry(client: Either, world: FakeAPI) -> None:
    world.fail("POST", "/invites$", after=True)
    with pytest.raises(RequestTimeoutError):
        await client.create_invite(GROUP)
    assert len(world.calls("POST")) == 1


async def test_the_server_forwards_between_readable_chats_and_refuses_sealed_ones(
    client: Either, world: FakeAPI
) -> None:
    forwarded = await client.forward_message(CHANNEL, GROUP, 1101, hide_sender=True, send_id=SEND_ID)
    assert forwarded.chat.id == CHANNEL
    [request] = world.calls("POST", "/bot/v1/messages/forward")
    assert request["json"] == {
        "chatId": CHANNEL,
        "fromChatId": GROUP,
        "messageId": 1101,
        "hideSender": True,
        "sendId": SEND_ID,
    }
    world.chats[GROUP].history_visible = False
    with pytest.raises(ChatIsProtectedError):
        await client.forward_message(CHANNEL, GROUP, 1101)


def test_a_forward_carries_a_send_id_of_its_own_and_is_paced_like_a_send() -> None:
    call = _spec.forward_message(CHANNEL, GROUP, 1101)
    assert call.send
    assert (call.chat_id, call.chat_kind) == (CHANNEL, ChatKind.CHANNEL)
    assert len(call.body["sendId"]) == 36
    comment = _spec.send_comment(CHANNEL, POST, "hi")
    assert comment.send
    assert not comment.repeatable
    assert comment.chat_kind == ChatKind.CHANNEL


NEWER_CODES = {
    "bad_media": (BadMediaError, 400),
    "chat_is_protected": (ChatIsProtectedError, 400),
    "reads_all_needed": (ReadsAllNeededError, 403),
    "comments_closed": (CommentsClosedError, 403),
    "sticker_not_found": (StickerNotFoundError, 404),
    "invite_not_found": (InviteNotFoundError, 404),
    "request_not_found": (JoinRequestNotFoundError, 404),
    "chat_is_public": (ChatIsPublicError, 409),
    "chat_full": (ChatFullError, 409),
}


@pytest.mark.parametrize("code", sorted(NEWER_CODES))
def test_every_code_the_new_operations_answer_with_has_its_class(code: str) -> None:
    cls, status = NEWER_CODES[code]
    error = error_from_envelope({"code": code, "message": "said the server", "status": status}, http_status=status)
    assert type(error) is cls
    family = {400: BadRequestError, 403: ForbiddenError, 404: NotFoundError, 409: ConflictError}[status]
    assert isinstance(error, family)


def test_the_codes_the_operations_name_are_the_ones_mapped() -> None:
    spec = (Path(__file__).parents[1] / "docs" / "openapi.json").read_text(encoding="utf-8")
    named = {code for code in NEWER_CODES if f"`{code}`" in spec}
    assert named == set(NEWER_CODES)
    assert set(NEWER_CODES) <= set(json.loads(spec)["components"]["schemas"]["ErrorCode"]["enum"])
    assert json.loads(spec)["info"]["version"] == "2.0.0"


def test_ids_with_their_letter_say_what_the_new_operations_pace() -> None:
    assert _spec._kind_of(GROUP) == ChatKind.GROUP
    assert _spec._kind_of(CHANNEL) == ChatKind.CHANNEL
    assert _spec._kind_of(PRIVATE) == ChatKind.PRIVATE
    assert BOT.startswith("B")

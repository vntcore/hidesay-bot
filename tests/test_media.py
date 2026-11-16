"""Files, stickers, forwards and history through the bot, in readable and in encrypted chats.

In an encrypted chat the bot writes what the phones read: the files sealed to the chat, and the
message that describes them sealed and signed. These tests open what the bot sent with the
phone's own key, the way a phone would.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from hidesay_bot import (
    Bot,
    BotKey,
    EncryptionRequiredError,
    InputFile,
    LinkCard,
    Media,
    MediaIntegrityError,
    Message,
    NotARecipientError,
    PublicKey,
    SentMessage,
    SignatureStatus,
    SyncBot,
    SyncMessage,
    ValidationError,
    wire,
)
from hidesay_bot.files import input_files

from .conftest import FIXTURES, fixture_text
from .fakeapi import ANNA, BRUNO, MEDIA_ID, FakeAPI, channel, group, person

CHAT = person(7)
GROUP = group(900)
OPEN_GROUP = group(901)
CHANNEL = channel(950)
ATTACHMENT = b"\xff\xd8\xffJPEGDATA"
JPEG = b"\xff\xd8\xff\xe0 a picture"
CLIP = b"\x00\x00\x00\x18ftypmp42 a clip"


def key_row(user_id: str, fixture: str, fingerprint: str) -> dict[str, Any]:
    return {"userId": user_id, "fingerprint": fingerprint, "publicKey": fixture_text(fixture)}


@pytest.fixture(scope="module")
def phone_secret() -> BotKey:
    return BotKey.from_file(FIXTURES / "phone_ed.sec.asc")


@pytest.fixture(scope="module")
def rsa_secret() -> BotKey:
    return BotKey.from_file(FIXTURES / "phone_rsa.sec.asc")


@pytest.fixture
def anna_row(phone_key: PublicKey) -> dict[str, Any]:
    return key_row(ANNA, "phone_ed.pub.asc", phone_key.fingerprint)


@pytest.fixture
def bruno_row(rsa_phone_key: PublicKey) -> dict[str, Any]:
    return key_row(BRUNO, "phone_rsa.pub.asc", rsa_phone_key.fingerprint)


@pytest.fixture
async def new_bot(make_bot: Callable[..., Bot]) -> AsyncIterator[Callable[..., Bot]]:
    made: list[Bot] = []

    def factory(**options: Any) -> Bot:
        bot = make_bot(**options)
        made.append(bot)
        return bot

    yield factory
    for bot in made:
        await bot.stop()


@pytest.fixture
def new_sync_bot(make_sync_bot: Callable[..., SyncBot]) -> Iterator[Callable[..., SyncBot]]:
    made: list[SyncBot] = []

    def factory(**options: Any) -> SyncBot:
        bot = make_sync_bot(**options)
        made.append(bot)
        return bot

    yield factory
    for bot in made:
        bot.stop()
        bot.api.close()


@pytest.fixture
def sealed(fake: FakeAPI, anna_row: dict[str, Any], bruno_row: dict[str, Any]) -> FakeAPI:
    """A protected bot: its chat with Anna is encrypted, and so is a group with Anna and Bruno."""
    fake.me["mode"] = "protected"
    fake.add_chat(7, keys=[anna_row])
    fake.add_chat(GROUP, "group", title="Team", history_visible=False, keys=[anna_row, bruno_row])
    fake.add_chat(OPEN_GROUP, "group", title="Lobby", history_visible=True)
    fake.add_chat(CHANNEL, "channel", title="News", history_visible=False, keys=[anna_row])
    return fake


@pytest.fixture
def readable(fake: FakeAPI) -> FakeAPI:
    fake.add_chat(7)
    fake.add_chat(OPEN_GROUP, "group", title="Lobby", history_visible=True)
    fake.add_chat(CHANNEL, "channel", title="News", history_visible=True)
    return fake


def posts(fake: FakeAPI) -> list[dict[str, Any]]:
    return [call["json"] for call in fake.calls("POST", "/bot/v1/messages")]


def uploads(fake: FakeAPI) -> list[bytes]:
    return [call["files"]["file"] for call in fake.calls("POST", "/bot/v1/attachments")]


def envelope(raw: str, mark: str) -> dict[str, Any]:
    assert raw.startswith(mark), raw[:40]
    return json.loads(raw[len(mark) :])  # type: ignore[no-any-return]


# --- files ---


def test_an_input_file_works_out_its_kind_and_type_from_the_name(tmp_path: Path) -> None:
    photo = InputFile(data=JPEG, name="sunset.JPG")
    assert (photo.content_kind, photo.content_type) == ("image", "image/jpeg")
    clip = InputFile(data=CLIP, name="clip.mp4", duration_ms=1200)
    assert (clip.content_kind, clip.content_type) == ("video", "video/mp4")
    assert InputFile(data=b"x", name="notes.txt").content_kind == "file"
    assert InputFile(data=b"x", kind="audio").content_type == "audio/ogg"
    assert InputFile(data=b"x").content_type == "application/octet-stream"
    assert photo.sha256 == hashlib.sha256(JPEG).hexdigest()
    # the bytes stay out of logs
    assert repr(photo) == "InputFile(name='sunset.JPG', kind='image', 14 bytes)"

    path = tmp_path / "report.pdf"
    path.write_bytes(b"%PDF")
    loaded = InputFile.from_path(path)
    assert (loaded.name, loaded.content_type, loaded.data) == ("report.pdf", "application/pdf", b"%PDF")
    with pytest.raises(ValidationError):
        InputFile.from_path(tmp_path / "missing.pdf")


@pytest.mark.parametrize(
    ("given", "field"),
    [
        ([], "files"),
        ([JPEG] * 11, "files"),
        ([InputFile(data=JPEG, name="a.jpg"), InputFile(data=b"x", name="a.txt")], "files"),
        (InputFile(data=JPEG, name="a/b.jpg"), "name"),
        (InputFile(data=JPEG, kind="sticker"), "kind"),
        (InputFile(data=JPEG, mime="jpeg"), "mime"),
        (InputFile(data=JPEG, width=0), "width"),
        (InputFile(data=JPEG, thumb="not base64!"), "thumb"),
        (InputFile(data=b"x" * 101), "data"),
        (42, "files"),
    ],
)
def test_files_that_break_a_rule_are_refused_before_anything_is_sent(given: Any, field: str) -> None:
    with pytest.raises(ValidationError) as caught:
        input_files(given, limit=100)
    assert caught.value.field == field


async def test_a_file_into_a_readable_chat_goes_up_as_it_is(readable: FakeAPI, new_bot: Callable[..., Bot]) -> None:
    bot = new_bot()
    sent = await bot.send_media(
        CHAT, InputFile(data=JPEG, name="sunset.jpg", width=640, height=480), caption="Tonight", silent=True
    )

    assert uploads(readable) == [JPEG]
    [post] = posts(readable)
    [media] = post["media"]
    assert post["text"] == "Tonight"
    assert post["silent"] is True
    assert media == {
        "id": media["id"],
        "kind": "image",
        "mime": "image/jpeg",
        "name": "sunset.jpg",
        "size": len(JPEG),
        "width": 640,
        "height": 480,
        "sha256": hashlib.sha256(JPEG).hexdigest(),
    }
    assert readable.media[media["id"]] == JPEG
    assert isinstance(sent, SentMessage)


async def test_an_album_of_pictures_and_clips_into_a_readable_group(
    readable: FakeAPI, new_bot: Callable[..., Bot], tmp_path: Path
) -> None:
    path = tmp_path / "clip.mp4"
    path.write_bytes(CLIP)
    bot = new_bot()
    await bot.send_media(OPEN_GROUP, [InputFile(data=JPEG, name="a.jpg"), str(path)], mentions=[ANNA])

    [post] = posts(readable)
    assert [m["kind"] for m in post["media"]] == ["image", "video"]
    assert post["media"][1]["name"] == "clip.mp4"
    assert post["mentions"] == [ANNA]
    assert "text" not in post


async def test_an_album_with_something_that_is_not_a_picture_is_refused_first(
    readable: FakeAPI, new_bot: Callable[..., Bot]
) -> None:
    bot = new_bot()
    with pytest.raises(ValidationError):
        await bot.send_media(OPEN_GROUP, [InputFile(data=JPEG, name="a.jpg"), InputFile(data=b"%PDF", name="b.pdf")])
    assert readable.requests == []


async def test_a_file_into_an_encrypted_chat_goes_up_sealed_and_the_message_names_it(
    sealed: FakeAPI, new_bot: Callable[..., Bot], bot_key: BotKey, phone_secret: BotKey
) -> None:
    bot = new_bot(pgp_private_key=bot_key)
    sent = await bot.send_media(
        CHAT, InputFile(data=JPEG, name="sunset.jpg", width=640, height=480, thumb="AAAA"), caption="Tonight"
    )

    [blob] = uploads(sealed)
    assert JPEG not in blob
    # the phone opens the file with its own key; files go unsigned, as the phones send them
    opened_file = phone_secret.decrypt(blob, verify_with=[bot_key.public_key])
    assert opened_file.data == JPEG
    assert opened_file.verification.status == SignatureStatus.UNSIGNED
    assert bot_key.decrypt(blob).data == JPEG

    [post] = posts(sealed)
    assert set(post) == {"chatId", "armored", "sendId"}
    opened = phone_secret.decrypt(post["armored"], verify_with=[bot_key.public_key])
    assert opened.verification.valid
    env = envelope(opened.text, wire.MEDIA)
    file_id = next(k for k, v in sealed.media.items() if v == blob)
    assert env == {
        "media": file_id,
        "kind": "image",
        "mime": "image/jpeg",
        "name": "sunset.jpg",
        "size": len(JPEG),
        "w": 640,
        "h": 480,
        "dur": None,
        "thumb": "AAAA",
        "sha256": hashlib.sha256(JPEG).hexdigest(),
        "cap": "Tonight",
    }
    # and the bot reads its own message back the way a handler would
    body = wire.parse_body(opened.text)
    assert (body.text, body.media[0].encrypted, body.media[0].sha256) == ("Tonight", True, env["sha256"])
    assert sent.encrypted


async def test_an_album_into_an_encrypted_group_is_sealed_for_every_member(
    sealed: FakeAPI, new_bot: Callable[..., Bot], bot_key: BotKey, phone_secret: BotKey, rsa_secret: BotKey
) -> None:
    bot = new_bot(pgp_private_key=bot_key)
    await bot.send_media(GROUP, [InputFile(data=JPEG, name="a.jpg"), InputFile(data=CLIP, name="b.mp4")])

    blobs = uploads(sealed)
    assert [rsa_secret.decrypt(b).data for b in blobs] == [JPEG, CLIP]
    [post] = posts(sealed)
    env = envelope(rsa_secret.decrypt(post["armored"]).text, wire.ALBUM)
    assert [item["kind"] for item in env["items"]] == ["image", "video"]
    assert env["cap"] is None
    assert all("cap" not in item and "plain" not in item for item in env["items"])
    assert phone_secret.decrypt(post["armored"]).text.startswith(wire.ALBUM)


async def test_a_chat_that_turned_encrypted_gets_the_files_sealed_and_sent_again(
    sealed: FakeAPI, new_bot: Callable[..., Bot], bot_key: BotKey, phone_secret: BotKey
) -> None:
    bot = new_bot(pgp_private_key=bot_key)
    sealed.chats[GROUP].history_visible = True
    await bot.get_chat(GROUP)
    sealed.chats[GROUP].history_visible = False

    await bot.send_media(GROUP, InputFile(data=JPEG, name="a.jpg"), send_id="6f9619ff-8b86-4011-b42d-00c04fc964ff")

    clear, sealed_blob = uploads(sealed)
    assert clear == JPEG
    assert phone_secret.decrypt(sealed_blob).data == JPEG
    refused, accepted = (c["json"] for c in sealed.calls("POST", "/bot/v1/messages"))
    assert "media" in refused
    assert "armored" in accepted
    assert refused["sendId"] == accepted["sendId"]


async def test_files_never_go_in_clear_when_encryption_is_required(
    readable: FakeAPI, new_bot: Callable[..., Bot], bot_key: BotKey
) -> None:
    bot = new_bot(pgp_private_key=bot_key, require_encryption=True)
    with pytest.raises(EncryptionRequiredError):
        await bot.send_media(CHAT, JPEG)
    assert uploads(readable) == []


# --- stickers ---


async def test_a_sticker_into_a_readable_chat_is_named_by_its_id(
    readable: FakeAPI, new_bot: Callable[..., Bot]
) -> None:
    readable.stickers[77] = {"id": 77, "packId": 5, "mediaId": MEDIA_ID, "emoji": "🙂", "public": True}
    bot = new_bot()
    await bot.send_sticker(CHAT, 77, reply_to=1001)
    [post] = posts(readable)
    assert (post["sticker"], post["replyTo"]) == ({"id": 77}, 1001)
    assert readable.calls("GET", "/bot/v1/stickers/77") == []


async def test_a_sticker_into_an_encrypted_chat_is_looked_up_and_sealed(
    sealed: FakeAPI, new_bot: Callable[..., Bot], bot_key: BotKey, phone_secret: BotKey
) -> None:
    sealed.stickers[78] = {"id": 78, "packId": 6, "mediaId": MEDIA_ID, "emoji": "🙂", "public": False}
    bot = new_bot(pgp_private_key=bot_key)
    received = Message.from_json(
        {
            "id": 5,
            "chat": {"id": CHAT, "kind": "private"},
            "from": {"id": ANNA},
            "date": 1791000000000,
            "sticker": {"id": 78, "packId": 6, "mediaId": MEDIA_ID, "emoji": "🙂"},
        }
    )
    assert received.sticker is not None
    await bot.send_sticker(CHAT, received.sticker)

    [lookup] = sealed.calls("GET", "/bot/v1/stickers/78")
    assert lookup["query"] == {"mediaId": MEDIA_ID}
    [post] = posts(sealed)
    env = envelope(phone_secret.decrypt(post["armored"]).text, wire.STICKER)
    assert env == {"id": 78, "packId": 6, "mediaId": MEDIA_ID, "emoji": "🙂"}


# --- forwards ---


async def test_between_readable_chats_the_server_forwards(readable: FakeAPI, new_bot: Callable[..., Bot]) -> None:
    readable.push_message(OPEN_GROUP, "meet at nine", message_id=40)
    bot = new_bot()
    await bot.forward_message(CHANNEL, OPEN_GROUP, 40, silent=True)
    [forward] = readable.calls("POST", "/bot/v1/messages/forward")
    assert forward["json"]["silent"] is True
    assert posts(readable) == []


async def test_an_encrypted_message_with_a_file_is_opened_and_sealed_again_for_the_other_chat(
    sealed: FakeAPI, new_bot: Callable[..., Bot], bot_key: BotKey, phone_secret: BotKey, rsa_secret: BotKey
) -> None:
    sealed.media[MEDIA_ID] = (FIXTURES / "attachment.pgp").read_bytes()
    sealed.push_message(7, armored=fixture_text("msg_media.asc"), message_id=41)
    bot = new_bot(pgp_private_key=bot_key)

    await bot.forward_message(GROUP, CHAT, 41)

    assert sealed.calls("POST", "/bot/v1/messages/forward") == []
    [blob] = uploads(sealed)
    assert rsa_secret.decrypt(blob).data == ATTACHMENT
    [post] = posts(sealed)
    opened = rsa_secret.decrypt(post["armored"], verify_with=[bot_key.public_key])
    assert opened.verification.valid
    env = envelope(opened.text, wire.FORWARD)
    # the bot can't know if Anna lets a forward link to her profile: her name, and no link
    assert env["origin"] == {"id": 10000055, "name": "User", "username": "user55", "linkable": False}
    inner = envelope(env["body"], wire.MEDIA)
    assert inner["media"] != MEDIA_ID
    assert sealed.media[inner["media"]] == blob
    assert inner["cap"] == "guarda che tramonto"

    body = wire.parse_body(opened.text)
    assert body.forward is not None
    assert (body.forward.name, body.forward.user_id) == ("User", None)


async def test_a_received_message_is_passed_on_without_fetching_it_again(
    sealed: FakeAPI, new_bot: Callable[..., Bot], bot_key: BotKey, phone_secret: BotKey
) -> None:
    bot = new_bot(pgp_private_key=bot_key)
    forwarded: list[SentMessage] = []

    @bot.on_message()
    async def relay(message: Message) -> None:
        forwarded.append(await message.forward_to(GROUP, hide_sender=True))

    sealed.push_message(7, armored=fixture_text("msg_text_signed.asc"), message_id=42)
    await bot.process_update(sealed.queue[-1])

    assert sealed.calls("GET", f"/bot/v1/chats/{CHAT}/messages/42") == []
    [post] = posts(sealed)
    text = phone_secret.decrypt(post["armored"]).text
    # a copy: no "forwarded from"
    assert not text.startswith(wire.FORWARD)
    assert forwarded[0].encrypted


async def test_a_readable_message_with_a_file_goes_sealed_into_an_encrypted_group(
    sealed: FakeAPI, new_bot: Callable[..., Bot], bot_key: BotKey, rsa_secret: BotKey
) -> None:
    file_id = "11111111-2222-3333-4444-555555555555"
    sealed.media[file_id] = JPEG
    sealed.push_message(
        OPEN_GROUP,
        "from the lobby",
        message_id=43,
        media=[
            {
                "id": file_id,
                "kind": "image",
                "mime": "image/jpeg",
                "url": f"/media/{file_id}",
                "plain": True,
                "size": len(JPEG),
                "sha256": hashlib.sha256(JPEG).hexdigest(),
            }
        ],
    )
    bot = new_bot(pgp_private_key=bot_key)
    await bot.forward_message(GROUP, OPEN_GROUP, 43)

    [blob] = uploads(sealed)
    assert rsa_secret.decrypt(blob).data == JPEG
    env = envelope(rsa_secret.decrypt(posts(sealed)[0]["armored"]).text, wire.FORWARD)
    assert env["origin"]["group"] == {"id": 10000901, "title": "Lobby", "username": None, "messageId": 43}
    inner = envelope(env["body"], wire.MEDIA)
    assert "plain" not in inner
    assert (inner["cap"], inner["sha256"]) == ("from the lobby", hashlib.sha256(JPEG).hexdigest())


async def test_a_sealed_message_goes_into_a_readable_chat_only_as_a_copy(
    sealed: FakeAPI, new_bot: Callable[..., Bot], bot_key: BotKey
) -> None:
    sealed.media[MEDIA_ID] = (FIXTURES / "attachment.pgp").read_bytes()
    sealed.push_message(7, armored=fixture_text("msg_media.asc"), message_id=44)
    bot = new_bot(pgp_private_key=bot_key)

    with pytest.raises(ValidationError) as caught:
        await bot.forward_message(OPEN_GROUP, CHAT, 44)
    assert caught.value.field == "hide_sender"
    assert uploads(sealed) == []

    await bot.forward_message(OPEN_GROUP, CHAT, 44, hide_sender=True)
    [plain] = uploads(sealed)
    assert plain == ATTACHMENT
    [post] = posts(sealed)
    assert post["text"] == "guarda che tramonto"
    assert post["media"][0]["kind"] == "image"
    assert post["media"][0]["sha256"] == hashlib.sha256(ATTACHMENT).hexdigest()


async def test_comments_and_protected_chats_are_not_forwarded(
    sealed: FakeAPI, new_bot: Callable[..., Bot], bot_key: BotKey
) -> None:
    bot = new_bot(pgp_private_key=bot_key)
    sealed.hand(
        CHANNEL,
        {"id": 45, "chat": {"id": CHANNEL, "kind": "channel"}, "date": 1791000000000, "text": "c", "threadRoot": 44},
    )
    with pytest.raises(ValidationError, match="comments"):
        await bot.forward_message(GROUP, CHANNEL, 45)
    sealed.chats[OPEN_GROUP].settings["protectContent"] = True
    sealed.push_message(OPEN_GROUP, "keep it here", message_id=46)
    sealed.fail("POST", "/messages/forward$", status=400, code="chat_is_protected")
    with pytest.raises(ValidationError, match="protects its content"):
        await bot.forward_message(GROUP, OPEN_GROUP, 46)
    assert posts(sealed) == []


# --- reading back ---


async def test_history_comes_back_opened_and_the_bots_own_messages_check_out(
    sealed: FakeAPI, new_bot: Callable[..., Bot], bot_key: BotKey
) -> None:
    bot = new_bot(pgp_private_key=bot_key)
    sealed.push_message(7, armored=fixture_text("msg_text_signed.asc"), message_id=50)
    sealed.push_message(7, armored=fixture_text("msg_not_for_bot.asc"), message_id=51)
    await bot.send_message(CHAT, "my own words")

    history = await bot.get_history(CHAT)
    assert [type(m) for m in history] == [Message] * 3
    own, stranger, signed = history
    assert (own.text, own.verification is not None and own.verification.valid) == ("my own words", True)
    # one that can't be opened stays as it came, and does not hide the others
    assert (stranger.text, stranger.encrypted, stranger.plaintext) == (None, True, None)
    assert signed.text
    assert signed.verification is not None

    with pytest.raises(NotARecipientError):
        await bot.get_message(CHAT, 51)
    one = await bot.get_message(CHAT, 50)
    assert one.text == signed.text


async def test_the_quoted_message_of_an_encrypted_update_is_opened_too(
    sealed: FakeAPI, new_bot: Callable[..., Bot], bot_key: BotKey, phone_secret: BotKey
) -> None:
    bot = new_bot(pgp_private_key=bot_key)
    got: list[Message] = []

    @bot.on_message()
    async def keep(message: Message) -> None:
        got.append(message)

    quoted_armor = phone_secret.seal("the question", [bot_key.public_key])
    sealed.push_message(7, armored=phone_secret.seal("the answer", [bot_key.public_key]), message_id=61, reply_to=60)
    sealed.queue[-1]["message"]["replyToMessage"] = {
        "id": 60,
        "chat": {"id": CHAT, "kind": "private"},
        "from": {"id": ANNA},
        "date": 1791000000000,
        "armored": quoted_armor,
    }
    await bot.process_update(sealed.queue[-1])

    [message] = got
    assert message.text == "the answer"
    assert message.reply_to_message is not None
    assert message.reply_to_message.text == "the question"


async def test_a_sticker_a_forward_and_a_link_card_are_read_out_of_an_encrypted_message(
    sealed: FakeAPI, new_bot: Callable[..., Bot], bot_key: BotKey, phone_secret: BotKey
) -> None:
    bot = new_bot(pgp_private_key=bot_key)
    got: list[Message] = []

    @bot.on_message()
    async def keep(message: Message) -> None:
        got.append(message)

    body = wire.forward_body(
        {"id": 10000056, "name": "Bruno", "username": "bruno", "linkable": True, "channelId": None},
        wire.link_body("look https://example.org", LinkCard(url="https://example.org", title="Example")),
    )
    sticker = wire.STICKER + json.dumps({"id": 3, "packId": 4, "mediaId": MEDIA_ID, "emoji": "🙂"})
    for text in (body, sticker):
        sealed.push_message(7, armored=phone_secret.seal(text, [bot_key.public_key]))
        await bot.process_update(sealed.queue[-1])

    forwarded, stuck = got
    assert forwarded.text == "look https://example.org"
    assert forwarded.forward is not None
    assert (forwarded.forward.name, forwarded.forward.user_id) == ("Bruno", "10000056")
    assert forwarded.link is not None
    assert forwarded.link.title == "Example"
    assert stuck.sticker is not None
    assert (stuck.sticker.id, stuck.sticker.media_id, stuck.text) == (3, MEDIA_ID, None)


async def test_a_download_whose_sha256_is_not_the_one_given_is_refused(
    readable: FakeAPI, new_bot: Callable[..., Bot]
) -> None:
    file_id = "11111111-2222-3333-4444-555555555555"
    readable.media[file_id] = JPEG
    bot = new_bot()
    good = Media(id=file_id, kind="image", sha256=hashlib.sha256(JPEG).hexdigest())
    assert await bot.download_media(good) == JPEG
    bad = Media(id=file_id, kind="image", sha256="0" * 64)
    with pytest.raises(MediaIntegrityError) as caught:
        await bot.download_media(bad)
    assert (caught.value.expected, caught.value.actual) == ("0" * 64, hashlib.sha256(JPEG).hexdigest())


async def test_a_comment_in_an_encrypted_channel_is_sealed(
    sealed: FakeAPI, new_bot: Callable[..., Bot], bot_key: BotKey, phone_secret: BotKey
) -> None:
    sealed.hand(CHANNEL, {"id": 70, "chat": {"id": CHANNEL, "kind": "channel"}, "date": 1791000000000, "armored": "x"})
    bot = new_bot(pgp_private_key=bot_key)
    sent = await bot.send_comment(CHANNEL, 70, "noted", as_author=True)
    [call] = sealed.calls("POST", f"/bot/v1/chats/{CHANNEL}/posts/70/comments")
    assert phone_secret.decrypt(call["json"]["armored"]).text == "noted"
    assert call["json"]["asAuthor"] is True
    assert sent.thread_root == 70


async def test_rooms_are_run_through_the_bot(readable: FakeAPI, new_bot: Callable[..., Bot]) -> None:
    readable.chats[OPEN_GROUP].members = [{"user": {"id": ANNA, "username": "anna"}, "status": "member"}]
    bot = new_bot()
    chat = await bot.update_chat(OPEN_GROUP, title="Lobby 2", protect_content=True)
    assert (chat.title, chat.protect_content) == ("Lobby 2", True)
    # what came back is kept, as get_chat keeps it
    assert (await bot.get_chat(OPEN_GROUP)).title == "Lobby 2"
    assert readable.calls("GET", f"/bot/v1/chats/{OPEN_GROUP}") == []
    members = await bot.get_chat_members(OPEN_GROUP)
    assert (await bot.update_member(OPEN_GROUP, members.members[0], role="admin")).is_admin
    picture = await bot.set_chat_photo(OPEN_GROUP, b"\x89PNG....")
    assert picture.mime == "image/png"
    assert (await bot.get_chats())[0].id == OPEN_GROUP


# --- the same on SyncBot ---


def test_sync_files_and_forwards_into_encrypted_chats(
    sealed: FakeAPI, new_sync_bot: Callable[..., SyncBot], bot_key: BotKey, phone_secret: BotKey, rsa_secret: BotKey
) -> None:
    bot = new_sync_bot(pgp_private_key=bot_key)
    bot.send_media(CHAT, InputFile(data=JPEG, name="a.jpg"), caption="hi")
    [blob] = uploads(sealed)
    assert phone_secret.decrypt(blob).data == JPEG
    env = envelope(phone_secret.decrypt(posts(sealed)[0]["armored"]).text, wire.MEDIA)
    assert (env["cap"], env["sha256"]) == ("hi", hashlib.sha256(JPEG).hexdigest())

    sealed.media[MEDIA_ID] = (FIXTURES / "attachment.pgp").read_bytes()
    sealed.push_message(7, armored=fixture_text("msg_media.asc"), message_id=81)
    bot.forward_message(GROUP, CHAT, 81)
    forwarded = rsa_secret.decrypt(posts(sealed)[1]["armored"]).text
    assert forwarded.startswith(wire.FORWARD)
    assert rsa_secret.decrypt(uploads(sealed)[1]).data == ATTACHMENT

    history = bot.get_history(CHAT)
    assert all(isinstance(m, SyncMessage) for m in history)
    assert history[0].reply_to_message is None
    assert {m.text for m in history} >= {"guarda che tramonto"}


def test_sync_stickers_and_integrity(
    sealed: FakeAPI, new_sync_bot: Callable[..., SyncBot], bot_key: BotKey, phone_secret: BotKey
) -> None:
    sealed.stickers[77] = {"id": 77, "packId": 5, "mediaId": MEDIA_ID, "emoji": "🙂", "public": True}
    bot = new_sync_bot(pgp_private_key=bot_key)
    bot.send_sticker(CHAT, 77)
    env = envelope(phone_secret.decrypt(posts(sealed)[0]["armored"]).text, wire.STICKER)
    assert env["id"] == 77

    file_id = "11111111-2222-3333-4444-555555555555"
    sealed.media[file_id] = JPEG
    with pytest.raises(MediaIntegrityError):
        bot.download_media(Media(id=file_id, kind="image", sha256="1" * 64))

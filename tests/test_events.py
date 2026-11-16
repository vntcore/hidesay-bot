"""The update kinds of Bot API 2.0, their handlers and filters, and what a handler can do with a message."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import pytest

from hidesay_bot import (
    Bot,
    Chat,
    ChatKind,
    ChatMemberUpdate,
    ChatRef,
    ChatUpdated,
    JoinRequest,
    Message,
    MessageReactions,
    ReactionUpdate,
    SentMessage,
    SyncBot,
    SyncMessage,
    SyncSentMessage,
    Update,
    UpdateKind,
    ValidationError,
    filters,
)
from hidesay_bot.filters import FilterContext

from .fakeapi import ANNA, BOT, BRUNO, FakeAPI, channel, group, person

GROUP = group(900)
CHANNEL = channel(950)
CHAT = person(7)
POST = 3001
WHEN_MS = 1791000000000


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
def rooms(fake: FakeAPI) -> FakeAPI:
    fake.add_chat(7)
    fake.add_chat(GROUP, "group", title="Hikers", history_visible=True)
    fake.add_chat(CHANNEL, "channel", title="News", history_visible=True)
    fake.hand(CHANNEL, message_json(CHANNEL, POST, "the news", sender=None))
    return fake


def user(user_id: str, username: str | None = None) -> dict[str, Any]:
    return {"id": user_id, "username": username, "firstName": "Anna", "lastName": None, "isBot": user_id[0] == "B"}


def chat_ref(chat_id: str) -> dict[str, Any]:
    kind = {"G": "group", "C": "channel"}.get(chat_id[0], "private")
    return {"id": chat_id, "kind": kind} | ({"title": "Hikers"} if kind != "private" else {})


def message_json(
    chat_id: str, message_id: int, text: str | None, *, sender: str | None = ANNA, **fields: Any
) -> dict[str, Any]:
    return {
        "id": message_id,
        "chat": chat_ref(chat_id),
        "from": user(sender, "anna") if sender else None,
        "date": WHEN_MS,
        "kind": "user",
        "text": text,
        "media": [],
        **fields,
    }


def update(kind: str, **fields: Any) -> dict[str, Any]:
    return {"updateId": 1, "kind": kind, **fields}


def seat(status: str = "member", **fields: Any) -> dict[str, Any]:
    return {"status": status, **fields}


def room_json(chat_id: str = GROUP, **fields: Any) -> dict[str, Any]:
    return {
        **chat_ref(chat_id),
        "mode": "open",
        "historyVisible": True,
        "memberCount": 3,
        "description": "Boots on",
        "permissions": {"sendMessages": True, "sendMedia": False},
        "slowModeSeconds": 0,
        "pinnedMessageIds": [5],
        **fields,
    }


# --- parsing ---


def test_a_comment_and_its_edit_carry_the_post_they_hang_under() -> None:
    for kind in ("comment", "edited_comment"):
        parsed = Update.from_json(update(kind, message=message_json(CHANNEL, 3002, "nice", threadRoot=POST)))
        assert parsed.kind == kind
        assert parsed.message is not None
        assert (parsed.message.thread_root, parsed.message.is_comment) == (POST, True)
        assert parsed.chat is not None
        assert parsed.chat.id == CHANNEL
        assert parsed.event is parsed.message


def test_a_reaction_update_says_what_changed_and_the_totals() -> None:
    parsed = Update.from_json(
        update(
            "message_reaction",
            chat=chat_ref(GROUP),
            messageId=12,
            threadRoot=None,
            **{"from": user(ANNA, "anna")},
            date=WHEN_MS,
            old=None,
            new="👍",
            reactions=[{"emoji": "👍", "count": 2}, {"emoji": "❤️", "count": 1}],
        )
    )
    reaction = parsed.reaction
    assert isinstance(reaction, ReactionUpdate)
    assert (reaction.message_id, reaction.old, reaction.new, reaction.added, reaction.removed) == (
        12,
        None,
        "👍",
        True,
        False,
    )
    assert reaction.counts == {"👍": 2, "❤️": 1}
    assert parsed.from_user is not None
    assert parsed.from_user.id == ANNA
    assert parsed.event is reaction


def test_the_bots_own_seat_comes_with_the_whole_room_and_reads_all() -> None:
    parsed = Update.from_json(
        update(
            "my_chat_member",
            chat=room_json(),
            **{"from": user(ANNA, "anna")},
            date=WHEN_MS,
            old=seat("left"),
            new=seat("member", readsAll=False, permissions={"sendMessages": True}),
        )
    )
    change = parsed.chat_member
    assert isinstance(change, ChatMemberUpdate)
    assert isinstance(change.chat, Chat)
    assert change.chat.permissions is not None
    assert not change.chat.permissions.send_media
    assert change.chat.pinned_message_ids == (5,)
    assert change.joined
    assert not change.reads_all
    assert change.user is None
    assert change.new.permissions is not None
    assert change.new.permissions.send_messages


def test_a_private_chat_in_a_seat_change_stays_a_reference() -> None:
    parsed = Update.from_json(
        update("my_chat_member", chat=chat_ref(CHAT), old=seat("member"), new=seat("kicked"), date=WHEN_MS)
    )
    assert parsed.chat_member is not None
    assert type(parsed.chat_member.chat) is ChatRef
    assert parsed.chat_member.blocked


@pytest.mark.parametrize(
    ("old", "new", "changed", "reads_all"),
    [
        (seat("member", readsAll=False), seat("member", readsAll=True), True, True),
        (seat("member", readsAll=True), seat("member", readsAll=False), True, False),
        (seat("left"), seat("member", readsAll=True), False, True),
        (seat("member", readsAll=True), seat("admin", readsAll=True), False, True),
    ],
)
def test_reads_all_changed_names_the_room_letting_the_bot_read_or_stopping_it(
    old: dict[str, Any], new: dict[str, Any], changed: bool, reads_all: bool
) -> None:
    parsed = Update.from_json(update("my_chat_member", chat=room_json(), old=old, new=new))
    assert parsed.chat_member is not None
    assert (parsed.chat_member.reads_all_changed, parsed.chat_member.reads_all) == (changed, reads_all)


def test_somebody_elses_seat_carries_whose_it_is() -> None:
    parsed = Update.from_json(
        update(
            "chat_member",
            chat=chat_ref(GROUP),
            **{"from": None},
            date=WHEN_MS,
            user=user(BRUNO, "bruno"),
            old=seat("left"),
            new=seat("member"),
        )
    )
    change = parsed.member_update
    assert change is not None
    assert parsed.chat_member is None
    assert change.user is not None
    assert change.user.id == BRUNO
    assert change.joined
    assert change.from_user is None


def test_a_join_request_and_a_room_change() -> None:
    asked = Update.from_json(update("join_request", chat=chat_ref(GROUP), **{"from": user(BRUNO)}, date=WHEN_MS))
    assert isinstance(asked.join_request, JoinRequest)
    assert (asked.join_request.chat.id, asked.join_request.from_user.id) == (GROUP, BRUNO)
    assert asked.from_user is not None

    moved = Update.from_json(
        update("chat_updated", chat=room_json(title="Walkers"), **{"from": None}, date=WHEN_MS, changed=["title", 7])
    )
    assert isinstance(moved.chat_updated, ChatUpdated)
    assert (moved.chat_updated.chat.title, moved.chat_updated.changed) == ("Walkers", ("title",))
    assert moved.from_user is None


def test_a_message_carries_what_it_quotes_its_sticker_forward_and_link_card() -> None:
    quoted = message_json(CHAT, 9, "the question")
    parsed = Update.from_json(
        update(
            "message",
            message=message_json(
                CHAT,
                10,
                "see https://example.org",
                replyTo=9,
                replyToMessage=quoted,
                markup={"inlineKeyboard": [[{"text": "Yes", "callbackData": "y"}]]},
                forward={"name": "News", "username": "news", "userId": None, "chat": {"id": CHANNEL, "messageId": 4}},
                link={"url": "https://example.org", "title": "Example", "description": None, "siteName": "ex"},
                sticker={"id": 77, "packId": 5, "mediaId": "0f3c5a3e-1111-2222-3333-444455556666", "emoji": ""},
            ),
        )
    )
    message = parsed.message
    assert message is not None
    assert message.reply_to_message is not None
    assert message.reply_to_message.text == "the question"
    assert message.markup is not None
    assert message.forward is not None
    assert message.forward.chat is not None
    assert (message.forward.chat.id, message.forward.chat.message_id) == (CHANNEL, 4)
    assert message.link is not None
    assert (message.link.title, message.link.site_name) == ("Example", "ex")
    assert message.sticker is not None
    # an empty emoji is no emoji
    assert (message.sticker.id, message.sticker.emoji) == (77, None)


# --- handlers ---


async def test_each_new_kind_reaches_its_own_handler(rooms: FakeAPI, new_bot: Callable[..., Bot]) -> None:
    bot = new_bot()
    seen: list[tuple[str, Any]] = []

    @bot.on_message()
    async def any_message(message: Message) -> None:
        seen.append(("message", message))

    @bot.on_comment()
    async def comment(message: Message) -> None:
        seen.append(("comment", message))

    @bot.on_edited_comment()
    async def edited(message: Message) -> None:
        seen.append(("edited_comment", message))

    @bot.on_reaction()
    async def reaction(event: ReactionUpdate) -> None:
        seen.append(("reaction", event))

    @bot.on_chat_member()
    async def mine(change: ChatMemberUpdate) -> None:
        seen.append(("mine", change))

    @bot.on_member_update()
    async def theirs(change: ChatMemberUpdate) -> None:
        seen.append(("theirs", change))

    @bot.on_join_request()
    async def asked(request: JoinRequest) -> None:
        seen.append(("join", request))

    @bot.on_chat_updated()
    async def room(event: ChatUpdated) -> None:
        seen.append(("room", event))

    comment_json = message_json(CHANNEL, 3002, "nice", threadRoot=POST)
    for raw in (
        update("comment", message=comment_json),
        update("edited_comment", message=comment_json),
        update("message_reaction", chat=chat_ref(GROUP), messageId=12, **{"from": user(ANNA)}, new="👍", old=None),
        update("my_chat_member", chat=room_json(), old=seat("left"), new=seat("member")),
        update("chat_member", chat=chat_ref(GROUP), user=user(BRUNO), old=seat("left"), new=seat("member")),
        update("join_request", chat=chat_ref(GROUP), **{"from": user(BRUNO)}),
        update("chat_updated", chat=room_json(), changed=["title"]),
    ):
        await bot.process_update(raw)

    kinds = [kind for kind, _ in seen]
    assert kinds == ["comment", "edited_comment", "reaction", "mine", "theirs", "join", "room"]
    assert isinstance(seen[0][1], Message)
    assert seen[0][1].thread_root == POST


async def test_a_reaction_handler_can_ask_for_one_emoji(rooms: FakeAPI, new_bot: Callable[..., Bot]) -> None:
    bot = new_bot()
    liked: list[str | None] = []

    @bot.on_reaction(emoji="👍")
    async def thumbs(event: ReactionUpdate) -> None:
        liked.append(event.new)

    for new in ("👍", "❤️", None):
        await bot.process_update(
            update("message_reaction", chat=chat_ref(GROUP), messageId=12, **{"from": user(ANNA)}, new=new, old="👍")
        )
    assert liked == ["👍"]


async def test_a_room_change_handler_can_ask_for_some_settings(rooms: FakeAPI, new_bot: Callable[..., Bot]) -> None:
    bot = new_bot()
    pins: list[tuple[int, ...]] = []

    @bot.on_chat_updated(filters.changed("pinnedMessageIds"))
    async def pinned(event: ChatUpdated) -> None:
        pins.append(event.chat.pinned_message_ids)

    await bot.process_update(update("chat_updated", chat=room_json(), changed=["title"]))
    await bot.process_update(
        update("chat_updated", chat=room_json(pinnedMessageIds=[5, 6]), changed=["pinnedMessageIds"])
    )
    assert pins == [(5, 6)]


async def test_a_comment_is_answered_under_its_post(rooms: FakeAPI, new_bot: Callable[..., Bot]) -> None:
    rooms.hand(CHANNEL, message_json(CHANNEL, 3002, "nice", threadRoot=POST))
    bot = new_bot()
    replies: list[SentMessage] = []

    @bot.on_comment()
    async def thanks(message: Message) -> None:
        replies.append(await message.reply("thanks!"))
        replies.append(await message.answer("and welcome"))
        with pytest.raises(ValidationError) as caught:
            await message.reply("no buttons here", markup={"inlineKeyboard": [[{"text": "x", "callbackData": "x"}]]})
        assert caught.value.field == "markup"

    await bot.process_update(update("comment", message=message_json(CHANNEL, 3002, "nice", threadRoot=POST)))

    first, second = (c["json"] for c in rooms.calls("POST", f"/bot/v1/chats/{CHANNEL}/posts/{POST}/comments"))
    assert first == {"text": "thanks!", "replyTo": 3002}
    assert second == {"text": "and welcome"}
    assert rooms.calls("POST", "/bot/v1/messages") == []
    assert [r.thread_root for r in replies] == [POST, POST]


async def test_a_message_can_react_pin_and_be_passed_on(rooms: FakeAPI, new_bot: Callable[..., Bot]) -> None:
    bot = new_bot()
    done: dict[str, Any] = {}

    @bot.on_message()
    async def act(message: Message) -> None:
        done["react"] = await message.react("👍")
        done["pin"] = await message.pin()
        done["unpin"] = await message.unpin()
        done["unreact"] = await message.remove_reaction()
        done["forward"] = await message.forward_to(CHANNEL)

    rooms.push_message(GROUP, "plan for saturday", message_id=50)
    await bot.process_update(rooms.queue[-1])

    assert isinstance(done["react"], MessageReactions)
    assert done["react"].counts == {"👍": 1}
    assert (done["pin"], done["unpin"], done["unreact"]) == ([50], [], True)
    assert isinstance(done["forward"], SentMessage)
    [forward] = rooms.calls("POST", "/bot/v1/messages/forward")
    assert (forward["json"]["chatId"], forward["json"]["fromChatId"], forward["json"]["messageId"]) == (
        CHANNEL,
        GROUP,
        50,
    )


async def test_a_sent_message_can_be_pinned(rooms: FakeAPI, new_bot: Callable[..., Bot]) -> None:
    bot = new_bot()
    sent = await bot.send_message(GROUP, "scoreboard: 0 - 0")
    assert await sent.pin() == [sent.id]
    assert await sent.unpin() == []


async def test_the_quoted_message_acts_through_the_same_bot(rooms: FakeAPI, new_bot: Callable[..., Bot]) -> None:
    bot = new_bot()
    quoted: list[Any] = []

    @bot.on_message()
    async def show(message: Message) -> None:
        quoted.append(message.reply_to_message)
        await message.reply_to_message.reply("this one")

    raw = message_json(GROUP, 61, "and this?", replyTo=60, replyToMessage=message_json(GROUP, 60, "first"))
    await bot.process_update(update("message", message=raw))
    assert isinstance(quoted[0], Message)
    [post] = rooms.calls("POST", "/bot/v1/messages")
    assert (post["json"]["replyTo"], post["json"]["text"]) == (60, "this one")


# --- what the bot learns from an update ---


async def test_a_seat_change_fills_the_chat_cache_with_the_room_and_the_seat(
    rooms: FakeAPI, new_bot: Callable[..., Bot]
) -> None:
    bot = new_bot()

    @bot.on_chat_member()
    async def noted(change: ChatMemberUpdate) -> None:
        pass

    await bot.process_update(
        update("my_chat_member", chat=room_json(), old=seat("left"), new=seat("member", readsAll=True))
    )
    chat = await bot.get_chat(GROUP)
    assert rooms.calls("GET", f"/bot/v1/chats/{GROUP}") == []
    assert chat.reads_all
    assert chat.description == "Boots on"


async def test_somebody_coming_or_going_drops_the_keys_the_bot_kept(
    rooms: FakeAPI, new_bot: Callable[..., Bot]
) -> None:
    bot = new_bot()
    bot._keys.put(GROUP, "the old list")
    await bot.process_update(
        update("chat_member", chat=chat_ref(GROUP), user=user(BRUNO), old=seat("left"), new=seat("member"))
    )
    assert bot._keys.get(GROUP) is None


async def test_a_room_change_drops_what_the_bot_knew_about_the_room(
    rooms: FakeAPI, new_bot: Callable[..., Bot]
) -> None:
    bot = new_bot()
    await bot.get_chat(GROUP)
    await bot.process_update(update("chat_updated", chat=room_json(), changed=["title"]))
    await bot.get_chat(GROUP)
    assert len(rooms.calls("GET", f"/bot/v1/chats/{GROUP}")) == 2


# --- filters ---


def context(**fields: Any) -> FilterContext:
    values: dict[str, Any] = {"bot_username": "testbot", "update": None, "bot": None}
    values.update(fields)
    return FilterContext(**values)


def message_model(text: str | None, **fields: Any) -> Message:
    return Message.from_json(message_json(GROUP, 1, text, **fields))


@pytest.mark.parametrize(
    ("text", "matches"),
    [
        ("hey @testbot", True),
        ("hey @ForecastBot!", True),
        ("hey @forecastbotx", False),
        ("mail@forecastbot", False),
        ("ask @[Weather](uB10000412) please", True),
        ("ask @[Weather](uB10000413) please", False),
        ("@[](uB10000412)", False),
        (None, False),
    ],
)
def test_mentioned_knows_every_handle_and_the_tag_the_app_writes(text: str | None, matches: bool) -> None:
    ctx = context(bot_usernames=("testbot", "forecastbot"), bot_id=BOT)
    assert bool(filters.mentioned(message_model(text), ctx)) is matches


@pytest.mark.parametrize(
    ("text", "matches"),
    [("/start@forecastbot", True), ("/start@TestBot", True), ("/start@otherbot", False), ("/start", True)],
)
def test_a_command_may_name_any_of_the_bots_handles(text: str, matches: bool) -> None:
    ctx = context(bot_usernames=("testbot", "forecastbot"))
    assert bool(filters.command("start")(message_model(text), ctx)) is matches


def test_without_the_list_of_handles_the_main_one_still_counts() -> None:
    assert filters.command("start")(message_model("/start@testbot"), context())
    assert filters.mentioned(message_model("hi @testbot"), context())
    assert not filters.mentioned(message_model("hi @testbot"), context(bot_username=None))


def test_the_filters_for_what_a_message_is() -> None:
    sticker = {"id": 1, "mediaId": "0f3c5a3e-1111-2222-3333-444455556666"}
    ctx = context()
    assert filters.comment(message_model("x", threadRoot=4), ctx)
    assert not filters.comment(message_model("x"), ctx)
    assert filters.has_sticker(message_model(None, sticker=sticker), ctx)
    assert not filters.has_sticker(message_model("x"), ctx)
    assert filters.forwarded(message_model("x", forward={"name": "Anna"}), ctx)
    assert not filters.forwarded(message_model("x"), ctx)


def test_reaction_and_changed_filters_look_only_at_their_events() -> None:
    put = ReactionUpdate(chat=ChatRef(id=GROUP, kind=ChatKind.GROUP), message_id=1, new="👍")
    taken = ReactionUpdate(chat=ChatRef(id=GROUP, kind=ChatKind.GROUP), message_id=1, old="👍", new=None)
    ctx = context()
    assert filters.reaction()(put, ctx)
    assert filters.reaction("👍", "❤️")(put, ctx)
    assert not filters.reaction("❤️")(put, ctx)
    assert not filters.reaction()(taken, ctx)
    assert not filters.reaction()(message_model("👍"), ctx)
    moved = ChatUpdated(chat=Chat.from_json(room_json()), changed=("title", "photo"))
    assert filters.changed("photo")(moved, ctx)
    assert not filters.changed("banner")(moved, ctx)
    assert not filters.changed("title")(put, ctx)
    assert filters.group(moved, ctx)
    assert filters.group(put, ctx)


async def test_filters_get_every_handle_of_the_bot(rooms: FakeAPI, new_bot: Callable[..., Bot]) -> None:
    rooms.me["usernames"] = ["forecastbot"]
    bot = new_bot()
    got: list[str] = []

    @bot.on_message(filters.mentioned)
    async def called(message: Message) -> None:
        got.append(message.text or "")

    await bot.get_me()
    await bot.process_update(update("message", message=message_json(GROUP, 1, "ciao @forecastbot")))
    await bot.process_update(update("message", message=message_json(GROUP, 2, "ciao @[Test](uB10000412)")))
    await bot.process_update(update("message", message=message_json(GROUP, 3, "ciao a tutti")))
    assert got == ["ciao @forecastbot", "ciao @[Test](uB10000412)"]


# --- the same on SyncBot ---


def test_sync_handlers_get_the_new_kinds_and_answer_comments_under_the_post(
    rooms: FakeAPI, new_sync_bot: Callable[..., SyncBot]
) -> None:
    rooms.hand(CHANNEL, message_json(CHANNEL, 3002, "nice", threadRoot=POST))
    bot = new_sync_bot()
    seen: list[Any] = []

    @bot.on_comment()
    def comment(message: SyncMessage) -> None:
        seen.append(message.reply("thanks!"))

    @bot.on_reaction(emoji="👍")
    def liked(event: ReactionUpdate) -> None:
        seen.append(event)

    @bot.on_member_update()
    def joined(change: ChatMemberUpdate) -> None:
        seen.append(change)

    bot.process_update(update("comment", message=message_json(CHANNEL, 3002, "nice", threadRoot=POST)))
    bot.process_update(
        update("message_reaction", chat=chat_ref(GROUP), messageId=12, **{"from": user(ANNA)}, new="👍", old=None)
    )
    bot.process_update(
        update("chat_member", chat=chat_ref(GROUP), user=user(BRUNO), old=seat("left"), new=seat("member"))
    )

    assert isinstance(seen[0], SyncSentMessage)
    assert seen[0].thread_root == POST
    assert isinstance(seen[1], ReactionUpdate)
    assert isinstance(seen[2], ChatMemberUpdate)
    [comment_call] = rooms.calls("POST", f"/bot/v1/chats/{CHANNEL}/posts/{POST}/comments")
    assert comment_call["json"] == {"text": "thanks!", "replyTo": 3002}


def test_a_sync_message_can_react_pin_and_be_passed_on(rooms: FakeAPI, new_sync_bot: Callable[..., SyncBot]) -> None:
    bot = new_sync_bot()
    done: dict[str, Any] = {}

    @bot.on_message()
    def act(message: SyncMessage) -> None:
        done["react"] = message.react("👍")
        done["pin"] = message.pin()
        done["unpin"] = message.unpin()
        done["unreact"] = message.remove_reaction()
        done["forward"] = message.forward_to(CHANNEL)

    rooms.push_message(GROUP, "plan for saturday", message_id=50)
    bot.process_update(rooms.queue[-1])

    assert done["react"].counts == {"👍": 1}
    assert (done["pin"], done["unpin"], done["unreact"]) == ([50], [], True)
    assert isinstance(done["forward"], SyncSentMessage)
    sent = bot.send_message(GROUP, "scores")
    assert sent.pin() == [sent.id]


def test_the_update_kinds_this_version_names() -> None:
    assert {k.value for k in UpdateKind} >= {
        "comment",
        "edited_comment",
        "message_reaction",
        "chat_member",
        "join_request",
        "chat_updated",
    }

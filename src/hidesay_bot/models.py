"""Typed views of what the server sends: updates, messages, chats, people, keys and the bot.

Every model is a frozen dataclass. You can read its fields, compare two models and use one as a
dictionary key, but you cannot change it. Field names are the API's JSON names in Python style:
``from`` becomes ``from_user``, ``replyTo`` becomes ``reply_to``. Times are aware
`~datetime.datetime` objects in UTC; the server sends them as Unix milliseconds.

Each model made from JSON keeps that JSON, untouched, in ``raw``. So a field that a newer server
adds is never lost: this version has no attribute for it, but ``message.raw.get("someNewField")``
reads it. ``raw`` is left out of ``repr()``, of comparisons and of the hash. A model you build
by hand has an empty ``raw``.

The library builds the models for you. To build one yourself from the JSON of an API answer
(the ``result`` of the envelope), most models have a ``from_json()`` class method;
`~hidesay_bot.Update.from_json` builds a whole update, its event included. A required field that is
missing or has the wrong type raises `~hidesay_bot.PayloadError`; an optional one gets its default
instead.

The enums of values the server sends (`~hidesay_bot.ChatKind`, `~hidesay_bot.ChatMode`,
`~hidesay_bot.SeatStatus`, `~hidesay_bot.UpdateKind` and `~hidesay_bot.BotStatus`) are ``str`` enums: a
member equals its value, so ``chat.kind == "group"`` works. A value this version does not know
does not raise: it becomes a member of its own that equals its string and matches none of the
named members.

Three models can act: `~hidesay_bot.Message`, `~hidesay_bot.CallbackQuery` and `~hidesay_bot.SentMessage`,
with `~hidesay_bot.SyncMessage`, `~hidesay_bot.SyncCallbackQuery` and `~hidesay_bot.SyncSentMessage` as
their twins for `~hidesay_bot.SyncBot`. They hold the same data as their bases, plus methods such as
``message.reply(...)`` that go through the bot that received them.

Everything public here is also importable from ``hidesay_bot``.

Example:
    >>> from hidesay_bot import User
    >>> anna = User.from_json({"id": "48213907", "username": "anna", "someNewField": 1})
    >>> anna
    User(id='48213907', username='anna', first_name=None, last_name=None, is_bot=False)
    >>> anna.raw["someNewField"]
    1
"""

from __future__ import annotations

import enum
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, TypeVar

from .errors import PayloadError, ValidationError
from .keyboard import InlineKeyboard

if TYPE_CHECKING:
    from .bot import Bot, SyncBot

__all__ = [
    "KEEP",
    "AdminRights",
    "Ban",
    "BotInfo",
    "BotStatus",
    "CallbackQuery",
    "CallbackQueryBase",
    "ChannelStats",
    "Chat",
    "ChatCard",
    "ChatKeys",
    "ChatKind",
    "ChatMemberUpdate",
    "ChatMembers",
    "ChatMode",
    "ChatPeer",
    "ChatRef",
    "ChatUpdated",
    "Command",
    "CommandScope",
    "Comments",
    "DeletedMessages",
    "Forward",
    "ForwardChat",
    "GrowthPoint",
    "HandleKind",
    "Invite",
    "JoinRequest",
    "Limits",
    "LinkCard",
    "Media",
    "MediaIn",
    "Member",
    "MemberPermissions",
    "Message",
    "MessageBase",
    "MessageReactions",
    "Picture",
    "PostViews",
    "Profile",
    "PublishedKey",
    "Reaction",
    "ReactionCount",
    "ReactionUpdate",
    "Resolved",
    "Seat",
    "SeatStatus",
    "SentMessage",
    "SentMessageBase",
    "SignatureStatus",
    "StatsWindow",
    "Sticker",
    "SyncCallbackQuery",
    "SyncMessage",
    "SyncSentMessage",
    "Update",
    "UpdateKind",
    "UploadedFile",
    "User",
    "UserKey",
    "Verification",
    "WebhookInfo",
    "WebhookSummary",
]

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class _Keep:
    """The default of ``markup`` in every edit, meaning "leave the buttons as they are".

    ``markup=None`` removes the buttons, and a keyboard replaces them. You rarely write ``KEEP``
    yourself: `Bot.edit_message`, `SentMessage.edit` and `CallbackQuery.edit_message` use it when
    ``markup`` is left out. It is false in a boolean test, so check for it with ``markup is KEEP``.
    The settings of a room and of a seat use it the same way for the fields where ``None`` means
    something (``description=None`` clears the description).
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "KEEP"

    def __bool__(self) -> bool:
        return False


KEEP: Any = _Keep()
"""The default of ``markup`` in every edit, meaning "leave the buttons as they are".

``markup=None`` removes the buttons, and a keyboard replaces them. You rarely write ``KEEP``
yourself: `Bot.edit_message`, `SentMessage.edit` and `CallbackQuery.edit_message` use it when
``markup`` is left out. It is false in a boolean test, so check for it with ``markup is KEEP``.

`Bot.update_chat` and `Bot.update_member` use it the same way for the fields where ``None``
means something: ``description=None`` clears the description, ``description=KEEP`` (the
default) leaves it alone.
"""


class _OpenEnum(enum.StrEnum):
    """A StrEnum that does not blow up on a value a newer server sends.

    The unknown value becomes a pseudo member: it compares equal to its string
    and simply matches none of the named members.
    """

    @classmethod
    def _missing_(cls, value: object) -> Any:
        if not isinstance(value, str) or not value:
            return None
        member = str.__new__(cls, value)
        member._name_ = value
        member._value_ = value
        return member


class ChatKind(_OpenEnum):
    """The kind of a chat: private, group or channel.

    A ``str`` enum, so ``chat.kind == "group"`` and ``chat.kind == ChatKind.GROUP`` are the same
    test. A kind that a newer server adds does not raise: it becomes a member of its own that
    equals its string and matches none of the names below.

    Attributes:
        PRIVATE: ``"private"``, a chat between the bot and one person.
        GROUP: ``"group"``, a group chat.
        CHANNEL: ``"channel"``, a channel. A post there may speak in the channel's name, and
            then it has no sender.

    Examples:
        >>> ChatKind.GROUP == "group"
        True
        >>> ChatKind("supergroup")  # a kind this version does not know
        <ChatKind.supergroup: 'supergroup'>
    """

    PRIVATE = "private"
    GROUP = "group"
    CHANNEL = "channel"


class ChatMode(_OpenEnum):
    """Whether a chat is readable by the server or end-to-end encrypted.

    The server decides, and the mode says which body a message must carry. A private chat follows
    the bot: it is encrypted once the bot has published a key. A group or channel follows its
    history setting: readable when new members can see the history, encrypted otherwise. `Bot`
    finds out the mode before it sends, and encrypts when it must.

    Attributes:
        PROTECTED: ``"protected"``, end-to-end encrypted. Messages travel as ``armored``
            OpenPGP messages, which the server cannot read.
        OPEN: ``"open"``, readable. Messages travel as ``text``, which the server reads and
            passes on.
    """

    PROTECTED = "protected"
    OPEN = "open"


class SeatStatus(_OpenEnum):
    """Where somebody stands in a chat: the ``status`` of a `Seat`.

    In a private chat the bot's own seat is ``member``, or ``kicked`` while the person blocks it.

    Attributes:
        ADMIN: ``"admin"``, an administrator. `Seat.can` says what they may do.
        MEMBER: ``"member"``, an ordinary member.
        RESTRICTED: ``"restricted"``, a member who may do less than the others, until
            `Seat.restricted_until` when that is set.
        LEFT: ``"left"``, not in the chat, for example after leaving it.
        KICKED: ``"kicked"``, removed from the chat by somebody else. In a private chat, the
            person blocked the bot.
    """

    ADMIN = "admin"
    MEMBER = "member"
    RESTRICTED = "restricted"
    LEFT = "left"
    KICKED = "kicked"


class UpdateKind(_OpenEnum):
    """What an `Update` is about, and so which of its fields is set.

    A kind that a newer server adds becomes a member of its own. Such an update has none of those
    fields set, and its JSON is still in ``raw``. The two kinds a bot's Web-Bot causes,
    ``"web_bot_write_access"`` and ``"web_bot_contact"``, come through that way in this version.

    Attributes:
        MESSAGE: ``"message"``, a new message for the bot: in a private chat, or one meant for
            it in a group or channel. It is in the update's ``message``.
        EDITED_MESSAGE: ``"edited_message"``, somebody edited a message the bot had received.
            The new version is in ``message``.
        COMMENT: ``"comment"``, a comment under a channel post, by the same rule as a message.
            It is in ``message``, whose ``thread_root`` is the post.
        EDITED_COMMENT: ``"edited_comment"``, a comment the bot had received was edited. The
            new version is in ``message``.
        DELETED_MESSAGES: ``"deleted_messages"``, messages were deleted for everyone. They are
            listed in ``deleted``.
        MESSAGE_REACTION: ``"message_reaction"``, somebody put, changed or took back a reaction
            on a message the bot was handed. The change is in ``reaction``.
        CALLBACK_QUERY: ``"callback_query"``, somebody pressed a button with callback data under
            one of the bot's messages. The press is in ``callback_query``.
        MY_CHAT_MEMBER: ``"my_chat_member"``, the bot's own seat in a chat changed: added,
            promoted, restricted, let read every message or stopped, removed or left; in a
            private chat, blocked or unblocked. The change is in ``chat_member``.
        CHAT_MEMBER: ``"chat_member"``, somebody else's seat changed: joined, left, removed,
            banned, promoted or restricted. The change is in ``member_update``.
        JOIN_REQUEST: ``"join_request"``, somebody asked to come in through a link that needs
            approval. The request is in ``join_request``.
        CHAT_UPDATED: ``"chat_updated"``, the group or channel itself changed: its name, its
            picture, a setting, the pins. The change is in ``chat_updated``.
    """

    MESSAGE = "message"
    EDITED_MESSAGE = "edited_message"
    COMMENT = "comment"
    EDITED_COMMENT = "edited_comment"
    DELETED_MESSAGES = "deleted_messages"
    MESSAGE_REACTION = "message_reaction"
    CALLBACK_QUERY = "callback_query"
    MY_CHAT_MEMBER = "my_chat_member"
    CHAT_MEMBER = "chat_member"
    JOIN_REQUEST = "join_request"
    CHAT_UPDATED = "chat_updated"


class BotStatus(_OpenEnum):
    """Whether the bot is running or paused by its owner: `BotInfo.status`.

    Attributes:
        ACTIVE: ``"active"``, the bot works normally.
        PAUSED: ``"paused"``, the owner paused the bot. While it is paused, every API call
            raises `BotPausedError`, except ``GET /me``, ``GET /webhook``, ``DELETE /webhook``
            and ``POST /token/revoke``.
    """

    ACTIVE = "active"
    PAUSED = "paused"


class SignatureStatus(enum.StrEnum):
    """What the bot could tell about the OpenPGP signature of an encrypted message.

    It is the ``status`` of a `Verification`. For a message it receives, the bot checks the
    signature against the key the server lists for the sender; `BotKey.decrypt` checks it
    against the keys given in ``verify_with``. A ``str`` enum like the others, but the library
    makes it, not the server, so it only ever holds these five values.

    Attributes:
        VALID: ``"valid"``, signed with one of those keys, and the signature checks out.
        INVALID: ``"invalid"``, signed with one of those keys, but the signature does not check
            out; or the two OpenPGP engines that open every message disagree on what it says.
        UNKNOWN_KEY: ``"unknown_key"``, signed, but with none of those keys.
        UNSIGNED: ``"unsigned"``, no signature at all.
        NOT_CHECKED: ``"not_checked"``, the signature was not checked. Checking is off
            (``verify_signatures="off"``), there is no key to check against (a post in a
            channel's name has no sender; the chat's keys could not be fetched, or hold no
            usable key for the sender), or the engine that checks signatures could not read the
            message or the keys. `Verification.detail` says why, unless checking is off or no
            keys were given.
    """

    VALID = "valid"
    INVALID = "invalid"
    UNKNOWN_KEY = "unknown_key"
    UNSIGNED = "unsigned"
    NOT_CHECKED = "not_checked"


# --- parsing helpers ---


def _obj(data: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        raise PayloadError(f"{where}: expected an object, got {type(data).__name__}")
    return data


def _req_int(data: Mapping[str, Any], key: str, where: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise PayloadError(f"{where}.{key}: expected an integer, got {value!r}")
    return value


def _req_id(data: Mapping[str, Any], key: str, where: str) -> str:
    value = data.get(key)
    # ids are strings with their letter ("G48213907"); a bare number can only be a person
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str) and value:
        return value
    raise PayloadError(f"{where}.{key}: expected an id, got {value!r}")


def _int(data: Mapping[str, Any], key: str) -> int | None:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _num(data: Mapping[str, Any], key: str) -> float | None:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _str(data: Mapping[str, Any], key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) else None


def _bool(data: Mapping[str, Any], key: str, default: bool = False) -> bool:
    value = data.get(key)
    return value if isinstance(value, bool) else default


def _when(data: Mapping[str, Any], key: str) -> datetime | None:
    ms = _int(data, key)
    if ms is None:
        return None
    try:
        return _EPOCH + timedelta(milliseconds=ms)
    except OverflowError:
        return None


def _list(data: Mapping[str, Any], key: str) -> list[Any]:
    value = data.get(key)
    return value if isinstance(value, list) else []


def _ints(data: Mapping[str, Any], key: str) -> tuple[int, ...]:
    return tuple(i for i in _list(data, key) if isinstance(i, int) and not isinstance(i, bool))


def _opt_bool(data: Mapping[str, Any], key: str) -> bool | None:
    value = data.get(key)
    return value if isinstance(value, bool) else None


def _objects(data: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    return [item for item in _list(data, key) if isinstance(item, Mapping)]


_M = TypeVar("_M", bound="_Model")


@dataclass(frozen=True, slots=True, kw_only=True)
class _Model:
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False, hash=False)
    """The JSON object exactly as the server sent it."""


def _rebind(cls: type[_M], obj: _Model, bot: Any) -> _M:
    values = {f.name: getattr(obj, f.name) for f in fields(obj)}
    values["_bot"] = bot
    quoted = values.get("reply_to_message")
    if isinstance(quoted, _Model):
        # the quoted message acts through the same bot: message.reply_to_message.reply(...)
        values["reply_to_message"] = _rebind(cls, quoted, bot)
    return cls(**values)


# --- people and chats ---


@dataclass(frozen=True, slots=True, kw_only=True)
class User(_Model):
    """Somebody as a bot may know them: an ID, a handle and a name. Never a phone number.

    Attributes:
        id: The user's ID, eight digits: ``"48213907"``, or ``"B48213907"`` for a bot.
        username: Their handle, without ``@``, or ``None`` if they have none.
        first_name: Their first name, or ``None``.
        last_name: Their last name, or ``None``.
        is_bot: ``True`` for a bot.

    Example:
        >>> anna = User.from_json({"id": "48213907", "username": "anna", "firstName": "Anna"})
        >>> anna.full_name, anna.mention
        ('Anna', '@anna')
    """

    id: str
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    is_bot: bool = False

    @property
    def full_name(self) -> str:
        """The first and last name, joined by a space.

        Without a name, the handle with ``@``; without either, ``"user <id>"``, as in
        ``"user 48213907"``.
        """
        name = " ".join(p for p in (self.first_name, self.last_name) if p)
        return name or (f"@{self.username}" if self.username else f"user {self.id}")

    @property
    def mention(self) -> str:
        """The ``@handle`` when the user has one, `full_name` otherwise."""
        return f"@{self.username}" if self.username else self.full_name

    @classmethod
    def from_json(cls, data: Any) -> User:
        """Builds a user from the API's JSON (``id``, ``username``, ``firstName``, ...).

        Args:
            data: The user object, such as the ``from`` of a message.

        Returns:
            The user, with ``data`` kept in ``raw``.

        Raises:
            PayloadError: ``data`` is not an object, or its ``id`` is missing.
        """
        d = _obj(data, "user")
        return cls(
            id=_req_id(d, "id", "user"),
            username=_str(d, "username"),
            first_name=_str(d, "firstName"),
            last_name=_str(d, "lastName"),
            is_bot=_bool(d, "isBot"),
            raw=d,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatPeer(User):
    """Somebody with a key: `Chat.user`, the other side of a private chat, and `Resolved.user`.

    It has every field of `User`, and two more. ``isBot`` is sent for a lookup but not for
    the person in a private chat, where ``is_bot`` stays ``False``; ``deleted`` is the other
    way round, since a lookup never finds a closed account to begin with.

    Attributes:
        deleted: ``True`` when the person has deleted their account.
        has_key: ``True`` when the person has published an OpenPGP key. Without one, the bot
            cannot send them anything in an encrypted chat.
    """

    deleted: bool = False
    has_key: bool = False

    @classmethod
    def from_json(cls, data: Any, where: str = "chat.user") -> ChatPeer:
        """Builds the person from a ``user`` object the server sent.

        Args:
            data: The ``user`` object: of a private chat in ``GET /chats/{id}``, or of
                ``GET /resolve/{username}``.
            where: The name this object goes by in an error message.

        Returns:
            The person, with ``data`` kept in ``raw``.

        Raises:
            PayloadError: ``data`` is not an object, or its ``id`` is missing.
        """
        d = _obj(data, where)
        return cls(
            id=_req_id(d, "id", where),
            username=_str(d, "username"),
            first_name=_str(d, "firstName"),
            last_name=_str(d, "lastName"),
            is_bot=_bool(d, "isBot"),
            deleted=_bool(d, "deleted"),
            has_key=_bool(d, "hasKey"),
            raw=d,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatRef(_Model):
    """A chat as updates name it: its ID and kind, and the title of a group or channel.

    Every `Bot` method that takes a chat takes a ``ChatRef`` too, so ``message.chat`` can be
    passed as it is.

    Attributes:
        id: The chat's ID: ``"G48213907"`` a group, ``"C48213907"`` a channel, and in a
            private chat the other person's ID, the same as ``from_user.id``.
        kind: Private chat, group or channel.
        title: The name of a group or channel. ``None`` in a private chat.
        username: The public handle of a group or channel, without ``@``. ``None`` in a private
            chat, and for a group or channel that has none.
    """

    id: str
    kind: ChatKind
    title: str | None = None
    username: str | None = None

    @property
    def is_private(self) -> bool:
        """``True`` for a private chat between the bot and one person."""
        return self.kind == ChatKind.PRIVATE

    @property
    def is_group(self) -> bool:
        """``True`` for a group."""
        return self.kind == ChatKind.GROUP

    @property
    def is_channel(self) -> bool:
        """``True`` for a channel."""
        return self.kind == ChatKind.CHANNEL

    @classmethod
    def from_json(cls, data: Any) -> ChatRef:
        """Builds a chat from the API's JSON (``id``, ``kind``, ``title``, ``username``).

        Args:
            data: The chat object, such as the ``chat`` of a message.

        Returns:
            The chat, with ``data`` kept in ``raw``. A missing ``kind`` is read as ``"private"``.

        Raises:
            PayloadError: ``data`` is not an object, or its ``id`` is missing.
        """
        d = _obj(data, "chat")
        return cls(
            id=_req_id(d, "id", "chat"),
            kind=ChatKind(_str(d, "kind") or "private"),
            title=_str(d, "title"),
            username=_str(d, "username"),
            raw=d,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class AdminRights(_Model):
    """What an administrator may do, one flag per right: `Seat.can`.

    A right that this version does not know is still in ``raw``, under its JSON name.

    Attributes:
        delete_messages: May delete other people's messages.
        ban_users: May ban users.
        add_members: May add members, bots included.
        pin_messages: May pin messages.
        change_info: May change the chat's info.
        manage_admins: May manage the administrators.
        invite_links: May manage invite links.
        post_messages: May post messages.
        edit_messages: May edit messages.
        manage_comments: May manage comments.
        manage_dividers: May draw and remove the dividers that split a chat into sections.
    """

    delete_messages: bool = False
    ban_users: bool = False
    add_members: bool = False
    pin_messages: bool = False
    change_info: bool = False
    manage_admins: bool = False
    invite_links: bool = False
    post_messages: bool = False
    edit_messages: bool = False
    manage_comments: bool = False
    manage_dividers: bool = False

    @classmethod
    def from_json(cls, data: Any) -> AdminRights:
        """Builds the rights from the ``can`` object of an administrator's seat.

        Args:
            data: The ``can`` object, with one boolean per right (``deleteMessages``,
                ``banUsers``, ...).

        Returns:
            The rights, with ``data`` kept in ``raw``. A right that is missing is ``False``.

        Raises:
            PayloadError: ``data`` is not an object.
        """
        d = _obj(data, "can")
        return cls(
            delete_messages=_bool(d, "deleteMessages"),
            ban_users=_bool(d, "banUsers"),
            add_members=_bool(d, "addMembers"),
            pin_messages=_bool(d, "pinMessages"),
            change_info=_bool(d, "changeInfo"),
            manage_admins=_bool(d, "manageAdmins"),
            invite_links=_bool(d, "inviteLinks"),
            post_messages=_bool(d, "postMessages"),
            edit_messages=_bool(d, "editMessages"),
            manage_comments=_bool(d, "manageComments"),
            manage_dividers=_bool(d, "manageDividers"),
            raw=d,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class MemberPermissions(_Model):
    """What an ordinary member may do, one flag per permission.

    `Chat.permissions` holds what every member of a group may do; `Seat.permissions` what one
    seat may do, which is less for a restricted member and everything for an admin. A
    permission this version does not know is still in ``raw``, under its JSON name.

    Attributes:
        send_messages: May write.
        send_media: May send files, pictures and clips.
        send_voice: May send voice notes.
        add_members: May add people, and make invite links.
        pin_messages: May pin messages.
        change_info: May change the name, the description and the picture.
        react: May react to messages.
        add_dividers: May draw dividers.
    """

    send_messages: bool = False
    send_media: bool = False
    send_voice: bool = False
    add_members: bool = False
    pin_messages: bool = False
    change_info: bool = False
    react: bool = False
    add_dividers: bool = False

    @classmethod
    def from_json(cls, data: Any) -> MemberPermissions:
        """Builds the permissions from a ``permissions`` object.

        Args:
            data: The object, with one boolean per permission (``sendMessages``, ``react``, ...).

        Returns:
            The permissions, with ``data`` kept in ``raw``. A permission that is missing is
            ``False``.

        Raises:
            PayloadError: ``data`` is not an object.
        """
        d = _obj(data, "permissions")
        return cls(
            send_messages=_bool(d, "sendMessages"),
            send_media=_bool(d, "sendMedia"),
            send_voice=_bool(d, "sendVoice"),
            add_members=_bool(d, "addMembers"),
            pin_messages=_bool(d, "pinMessages"),
            change_info=_bool(d, "changeInfo"),
            react=_bool(d, "react"),
            add_dividers=_bool(d, "addDividers"),
            raw=d,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class Seat(_Model):
    """A place in a chat: the bot's own (`Chat.me`), or somebody's (`Member`).

    Which fields are set depends on `status`. An ``admin`` seat carries ``admin_rights``,
    ``can`` and maybe ``custom_title``; a ``member`` or ``restricted`` seat carries ``rights``
    and maybe ``restricted_until``; both carry ``permissions``. ``left`` and ``kicked`` carry
    nothing else.

    A bot's seat also says whether the room lets it read every message (`reads_all`). That is
    the room's decision: it is off when an admin adds the bot, an admin turns it on or off in
    the app, and the bot hears of every change in a ``my_chat_member`` update. Nothing in the
    API changes it. With it off, the bot gets in a group or channel only commands, mentions of
    it, replies to its messages (a comment under its post counts) and the presses of its
    buttons.

    Attributes:
        status: Where the seat stands: admin, member, restricted, left or kicked.
        admin_rights: An administrator's rights, as a bit mask. `can` has the same rights by
            name.
        can: An administrator's rights, by name.
        custom_title: An administrator's title, or ``None``.
        rights: What a member or a restricted member may do, as a bit mask.
        restricted_until: When a restriction ends. ``None`` when it has no end, and for a seat
            that is not restricted.
        permissions: What this seat may do as a member, by name: everything for an admin.
            ``None`` for a seat that is not in the chat.
        reads_all: Bots only: ``True`` when the room lets this bot read every message, not
            only what is meant for it. ``False`` for everybody else.
    """

    status: SeatStatus
    admin_rights: int | None = None
    can: AdminRights | None = None
    custom_title: str | None = None
    rights: int | None = None
    restricted_until: datetime | None = None
    permissions: MemberPermissions | None = None
    reads_all: bool = False

    @property
    def is_admin(self) -> bool:
        """``True`` for an administrator's seat."""
        return self.status == SeatStatus.ADMIN

    @property
    def is_inside(self) -> bool:
        """``True`` when the seat is in the chat (admin, member or restricted)."""
        return self.status in (SeatStatus.ADMIN, SeatStatus.MEMBER, SeatStatus.RESTRICTED)

    @classmethod
    def from_json(cls, data: Any) -> Seat:
        """Builds a seat from the API's JSON (``status``, ``adminRights``, ``can``, ...).

        Args:
            data: The seat object, such as the ``me`` of a chat or the ``old`` and ``new`` of a
                ``my_chat_member`` update.

        Returns:
            The seat, with ``data`` kept in ``raw``. A missing ``status`` is read as ``"left"``.

        Raises:
            PayloadError: ``data`` is not an object.
        """
        d = _obj(data, "seat")
        return cls(**_seat_fields(d), raw=d)


def _seat_fields(d: Mapping[str, Any]) -> dict[str, Any]:
    can = d.get("can")
    permissions = d.get("permissions")
    return {
        "status": SeatStatus(_str(d, "status") or "left"),
        "admin_rights": _int(d, "adminRights"),
        "can": AdminRights.from_json(can) if isinstance(can, Mapping) else None,
        "custom_title": _str(d, "customTitle"),
        "rights": _int(d, "rights"),
        "restricted_until": _when(d, "restrictedUntil"),
        "permissions": MemberPermissions.from_json(permissions) if isinstance(permissions, Mapping) else None,
        "reads_all": _bool(d, "readsAll"),
    }


@dataclass(frozen=True, slots=True, kw_only=True)
class Member(Seat):
    """Somebody's seat in a group or channel, with who they are.

    `Bot.get_chat_member` returns one, and `ChatMembers.members` lists them. It has every field
    of `Seat`, and one more.

    Attributes:
        user: Who holds the seat.
    """

    user: User

    @classmethod
    def from_json(cls, data: Any) -> Member:
        """Builds a member from the API's JSON: a seat with a ``user`` in it.

        Args:
            data: The member object.

        Returns:
            The member, with ``data`` kept in ``raw``.

        Raises:
            PayloadError: ``data`` is not an object, or its ``user`` is missing or has no
                integer ``id``.
        """
        d = _obj(data, "member")
        return cls(user=User.from_json(d.get("user")), **_seat_fields(d), raw=d)


@dataclass(frozen=True, slots=True, kw_only=True)
class Chat(ChatRef):
    """A chat the bot is in, with its mode: what `Bot.get_chat` returns (``GET /chats/{id}``).

    It has every field of `ChatRef`, and more. A private chat fills ``user`` and ``blocked``; a
    group or channel fills the others, including the bot's own seat in ``me``. A
    ``my_chat_member`` and a ``chat_updated`` update carry a group or channel this way too,
    without ``me`` (a ``my_chat_member`` has the seat in its ``new``). A field the server did
    not send is ``None``.

    Attributes:
        mode: Readable or end-to-end encrypted, and so which body a message must carry. If a
            server ever left it out, ``protected``: the safe guess, since a sealed message sent
            to a readable chat is refused without giving anything away.
        user: In a private chat, the person on the other side.
        blocked: In a private chat, ``True`` when the person blocked the bot.
        description: The description of a group or channel, or ``None``.
        member_count: How many members a group or channel has.
        history_visible: In a group or channel, ``True`` when new members can read the history
            from before they joined. Such a chat is readable; any other is encrypted.
        slow_mode_seconds: In a group, the slow mode delay in seconds, ``0`` when it is off.
            Sending faster than that raises `SlowModeError`.
        me: In a group or channel, the bot's own seat.
        photo_url: Where the picture of a group or channel is, ``/media/<id>`` on the API
            server, or ``None`` when it has none.
        banner_url: The same for its banner.
        permissions: What an ordinary member of a group may do.
        members_hidden: ``True`` when the member list is shown to the admins only.
        protect_content: ``True`` when messages here can't be forwarded, copied or saved.
        auto_delete_seconds: After how many seconds new messages delete themselves, ``0`` when
            they don't.
        reactions_enabled: ``True`` when people may react to messages.
        allowed_reactions: The only emoji people may react with, or ``None`` for any.
        pinned_message_ids: The IDs of the pinned messages the bot could open; a pin it never saw is left out.
        signatures: In a channel, ``True`` when posts show who wrote them.
        signature_profiles: In a channel, ``True`` when a signature links to the profile.
        admins_hidden: In a channel, ``True`` when the admins are kept from the subscribers.
        comments_enabled: In a channel, ``True`` when posts take comments.
        comment_signing: In a channel, ``True`` when its admins may comment under their own
            name.
    """

    mode: ChatMode
    user: ChatPeer | None = None
    blocked: bool | None = None
    description: str | None = None
    member_count: int | None = None
    history_visible: bool | None = None
    slow_mode_seconds: int | None = None
    me: Seat | None = None
    photo_url: str | None = None
    banner_url: str | None = None
    permissions: MemberPermissions | None = None
    members_hidden: bool | None = None
    protect_content: bool | None = None
    auto_delete_seconds: int | None = None
    reactions_enabled: bool | None = None
    allowed_reactions: tuple[str, ...] | None = None
    pinned_message_ids: tuple[int, ...] = ()
    signatures: bool | None = None
    signature_profiles: bool | None = None
    admins_hidden: bool | None = None
    comments_enabled: bool | None = None
    comment_signing: bool | None = None

    @property
    def protected(self) -> bool:
        """``True`` when the chat is end-to-end encrypted (`mode` is ``protected``)."""
        return self.mode == ChatMode.PROTECTED

    @property
    def reads_all(self) -> bool:
        """``True`` when the room lets the bot read every message (``me.reads_all``).

        ``False`` when `me` is not known, and in a private chat, where the bot reads
        everything anyway.
        """
        return self.me is not None and self.me.reads_all

    @classmethod
    def from_json(cls, data: Any) -> Chat:
        """Builds a chat from the answer of ``GET /chats/{id}``.

        Args:
            data: The chat object.

        Returns:
            The chat, with ``data`` kept in ``raw``. A missing ``kind`` is read as
            ``"private"``, a missing ``mode`` as ``"protected"``.

        Raises:
            PayloadError: ``data`` is not an object, or it or its ``user`` has no ``id``.
        """
        d = _obj(data, "chat")
        user = d.get("user")
        me = d.get("me")
        permissions = d.get("permissions")
        allowed = d.get("allowedReactions")
        return cls(
            id=_req_id(d, "id", "chat"),
            kind=ChatKind(_str(d, "kind") or "private"),
            title=_str(d, "title"),
            username=_str(d, "username"),
            # missing, assume the stricter one: sealing for a readable chat is refused
            # without disclosing anything, words in clear for an encrypted one are not
            mode=ChatMode(_str(d, "mode") or "protected"),
            user=ChatPeer.from_json(user) if isinstance(user, Mapping) else None,
            blocked=_opt_bool(d, "blocked"),
            description=_str(d, "description"),
            member_count=_int(d, "memberCount"),
            history_visible=_opt_bool(d, "historyVisible"),
            slow_mode_seconds=_int(d, "slowModeSeconds"),
            me=Seat.from_json(me) if isinstance(me, Mapping) else None,
            photo_url=_str(d, "photoUrl"),
            banner_url=_str(d, "bannerUrl"),
            permissions=MemberPermissions.from_json(permissions) if isinstance(permissions, Mapping) else None,
            members_hidden=_opt_bool(d, "membersHidden"),
            protect_content=_opt_bool(d, "protectContent"),
            auto_delete_seconds=_int(d, "autoDeleteSeconds"),
            reactions_enabled=_opt_bool(d, "reactionsEnabled"),
            allowed_reactions=tuple(e for e in allowed if isinstance(e, str)) if isinstance(allowed, list) else None,
            pinned_message_ids=_ints(d, "pinnedMessageIds"),
            signatures=_opt_bool(d, "signatures"),
            signature_profiles=_opt_bool(d, "signatureProfiles"),
            admins_hidden=_opt_bool(d, "adminsHidden"),
            comments_enabled=_opt_bool(d, "commentsEnabled"),
            comment_signing=_opt_bool(d, "commentSigning"),
            raw=d,
        )


# --- names ---


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatCard(ChatRef):
    """A public group or channel seen from outside: `Resolved.chat`.

    What its short-link page shows and no more, since the bot is not in it: no ``mode`` to
    pick a body with and no seat. Pass it straight to `Bot.get_chat` once an administrator
    has added the bot.

    Attributes:
        description: The description, or ``None``.
        member_count: How many members it has.
        history_visible: ``True`` when somebody walking in can read what was said before.
    """

    description: str | None = None
    member_count: int | None = None
    history_visible: bool | None = None

    @classmethod
    def from_json(cls, data: Any) -> ChatCard:
        """Builds the card from the ``chat`` of ``GET /resolve/{username}``.

        Args:
            data: The ``chat`` object of the answer.

        Returns:
            The card, with ``data`` kept in ``raw``.

        Raises:
            PayloadError: ``data`` is not an object, or its ``id`` is missing.
        """
        d = _obj(data, "resolved.chat")
        history = d.get("historyVisible")
        return cls(
            id=_req_id(d, "id", "resolved.chat"),
            kind=ChatKind(_str(d, "kind") or "group"),
            title=_str(d, "title"),
            username=_str(d, "username"),
            description=_str(d, "description"),
            member_count=_int(d, "memberCount"),
            history_visible=history if isinstance(history, bool) else None,
            raw=d,
        )


class HandleKind(_OpenEnum):
    """What a handle turned out to belong to: a person, a group or a channel.

    A ``str`` enum, so ``found.kind == "user"`` and ``found.kind == HandleKind.USER`` are the
    same test. A kind a newer server adds becomes a member of its own rather than raising.

    Attributes:
        USER: ``"user"``, somebody's account - a bot's included.
        GROUP: ``"group"``, a public group.
        CHANNEL: ``"channel"``, a public channel.
    """

    USER = "user"
    GROUP = "group"
    CHANNEL = "channel"


@dataclass(frozen=True, slots=True, kw_only=True)
class Resolved(_Model):
    """What a handle turned out to be: the answer of `Bot.resolve`.

    `id` is filled in whichever it was, so a caller that only wants the number never has to
    look at `kind`. Exactly one of `user` and `chat` carries the rest.

    Attributes:
        kind: Whether the handle belongs to a person, a group or a channel.
        id: The user ID, or the chat ID.
        username: The handle it actually goes by, which is not always the one that was asked
            with: somebody who answers to several returns their main one.
        user: The person, when `kind` is ``user``. ``None`` otherwise.
        chat: The group or channel, when `kind` is ``group`` or ``channel``. ``None`` otherwise.

    Example:
        >>> found = Resolved.from_json({"kind": "user", "id": "48213907", "username": "anna",
        ...                             "user": {"id": "48213907", "username": "anna"}})
        >>> found.id, found.is_user, found.chat
        ('48213907', True, None)
    """

    kind: HandleKind
    id: str
    username: str | None = None
    user: ChatPeer | None = None
    chat: ChatCard | None = None

    @property
    def is_user(self) -> bool:
        """``True`` when the handle belongs to a person or a bot."""
        return self.kind == HandleKind.USER

    @property
    def is_chat(self) -> bool:
        """``True`` when the handle belongs to a group or a channel."""
        return self.kind in (HandleKind.GROUP, HandleKind.CHANNEL)

    @property
    def mention(self) -> str:
        """The handle with an ``@`` in front, or ``"id <n>"`` for something that has none."""
        return f"@{self.username}" if self.username else f"id {self.id}"

    @classmethod
    def from_json(cls, data: Any) -> Resolved:
        """Builds the answer of ``GET /resolve/{username}``.

        Args:
            data: The result object.

        Returns:
            What the handle was, with ``data`` kept in ``raw``. A missing ``kind`` is read as
            ``"user"``, which is what a bare ``user`` object means.

        Raises:
            PayloadError: ``data`` is not an object, or its ``id`` is missing.
        """
        d = _obj(data, "resolved")
        user = d.get("user")
        chat = d.get("chat")
        return cls(
            kind=HandleKind(_str(d, "kind") or "user"),
            id=_req_id(d, "id", "resolved"),
            username=_str(d, "username"),
            user=ChatPeer.from_json(user, "resolved.user") if isinstance(user, Mapping) else None,
            chat=ChatCard.from_json(chat) if isinstance(chat, Mapping) else None,
            raw=d,
        )


# --- keys ---


@dataclass(frozen=True, slots=True, kw_only=True)
class UserKey(_Model):
    """Somebody's OpenPGP public key, as the server hands it out.

    `Bot.get_user_key` returns one, and `ChatKeys.keys` lists them. The fingerprint is what the
    server *says*: `PublicKey.from_user_key` reads the key and checks that it has that
    fingerprint before anything is sealed to it.

    Attributes:
        user_id: Whose key it is.
        fingerprint: The key's fingerprint as the server gives it, or ``None``.
        public_key: The key, as ASCII-armored text.

    Warning:
        The keys come from the server. A server that lies could hand out a key of its own in
        place of a person's. ``Bot(pin_keys=True)`` notices when a person's key changes.
    """

    user_id: str
    fingerprint: str | None
    public_key: str

    @classmethod
    def from_json(cls, data: Any) -> UserKey:
        """Builds a key from the API's JSON (``userId``, ``fingerprint``, ``publicKey``).

        Args:
            data: The key object, such as the answer of ``GET /users/{userId}/key``.

        Returns:
            The key, with ``data`` kept in ``raw``.

        Raises:
            PayloadError: ``data`` is not an object, its ``publicKey`` is missing, or its
                ``userId`` is missing.
        """
        d = _obj(data, "key")
        public_key = _str(d, "publicKey")
        if public_key is None:
            raise PayloadError("key.publicKey: missing")
        return cls(
            user_id=_req_id(d, "userId", "key"), fingerprint=_str(d, "fingerprint"), public_key=public_key, raw=d
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatKeys(_Model):
    """The keys a message into an encrypted chat must be sealed to: `Bot.get_chat_keys`.

    The answer of ``GET /chats/{id}/keys``. `Bot` fetches these keys by itself before it sends
    into an encrypted chat. The bot's own key is never in the list: add it yourself when you seal
    by hand (`BotKey.seal` does). Other bots are in it only when the room lets them read every
    message, as for the app.

    Attributes:
        keys: One `UserKey` per member who has a key. In a private chat, the person's only.
        member_count: In a group or channel, how many members it has. Always more than the
            number of keys: the bot itself is not in ``keys``, nor is a member without a key.
            ``None`` in a private chat.
    """

    keys: tuple[UserKey, ...]
    member_count: int | None = None

    @classmethod
    def from_json(cls, data: Any) -> ChatKeys:
        """Builds the list from the answer of ``GET /chats/{id}/keys``.

        Args:
            data: The answer, with ``keys`` and, in a group or channel, ``memberCount``.

        Returns:
            The keys, with ``data`` kept in ``raw``.

        Raises:
            PayloadError: ``data`` is not an object, or one of its keys is not a valid key
                object (see `UserKey.from_json`).
        """
        d = _obj(data, "keys")
        return cls(
            keys=tuple(UserKey.from_json(k) for k in _list(d, "keys")),
            member_count=_int(d, "memberCount"),
            raw=d,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatMembers(_Model):
    """The members of a group or channel, as the bot may see them: `Bot.get_chat_members`.

    The answer of ``GET /chats/{id}/members``. The bot sees what a member in its seat would see:
    where the member list is hidden, a bot that is not an admin sees the admins only.

    Attributes:
        members: Each member, with their seat.
        member_count: How many members are in ``members``. It is the length of the list the bot
            may see, not the size of the chat: `Chat.member_count` counts everyone.
    """

    members: tuple[Member, ...]
    member_count: int

    @classmethod
    def from_json(cls, data: Any) -> ChatMembers:
        """Builds the list from the answer of ``GET /chats/{id}/members``.

        Args:
            data: The answer, with ``members`` and ``memberCount``.

        Returns:
            The members, with ``data`` kept in ``raw``. Without ``memberCount``,
            ``member_count`` is the length of ``members``.

        Raises:
            PayloadError: ``data`` is not an object, or one of its members is not a valid
                member object (see `Member.from_json`).
        """
        d = _obj(data, "members")
        members = tuple(Member.from_json(m) for m in _list(d, "members"))
        count = _int(d, "memberCount")
        return cls(members=members, member_count=count if count is not None else len(members), raw=d)


@dataclass(frozen=True, slots=True, kw_only=True)
class PublishedKey(_Model):
    """What the server answers when the bot publishes its key (``PUT /key``).

    `Bot.publish_key` and `Bot.rotate_key` return it. From then on the bot is protected: its
    private chats are end-to-end encrypted, and only its owner can turn that off, from the app.

    Attributes:
        fingerprint: The fingerprint of the key now published.
        mode: The bot's mode now, ``protected``.
    """

    fingerprint: str
    mode: ChatMode

    @classmethod
    def from_json(cls, data: Any) -> PublishedKey:
        """Builds the answer of ``PUT /key`` from its JSON.

        Args:
            data: The answer, with ``fingerprint`` and ``mode``.

        Returns:
            The published key, with ``data`` kept in ``raw``. A missing ``fingerprint`` is
            ``""``, a missing ``mode`` is ``"protected"``.

        Raises:
            PayloadError: ``data`` is not an object.
        """
        d = _obj(data, "key")
        return cls(fingerprint=_str(d, "fingerprint") or "", mode=ChatMode(_str(d, "mode") or "protected"), raw=d)


# --- the bot ---


@dataclass(frozen=True, slots=True, kw_only=True)
class Limits(_Model):
    """The numbers the server you are on enforces: `BotInfo.limits`.

    Read them rather than hard-coding them. `Bot` uses some by itself: ``text_max`` to check a
    text before sending it, and the four rate limits to pace its own `RateLimiter`.

    Each field is a JSON name of ``limits`` in Python style: ``text_max`` is ``textMax``,
    ``poll_timeout_max_s`` is ``pollTimeoutMaxS``. The defaults are the documented numbers. They
    apply only when a server leaves a name out, or sends something that is not a positive whole
    number. A limit this version has no field for is still in ``raw``: read it with `get`.

    Attributes:
        text_max: The longest ``text``, in UTF-16 units, as JavaScript counts them: most emoji
            count 2. Default 4096.
        armored_max: The longest ``armored`` body, in characters. Default 131072.
        markup_bytes: The largest keyboard, in bytes of JSON. Default 8192.
        keyboard_rows: The most rows of buttons under one message. Default 20.
        buttons_per_row: The most buttons in one row. Default 8.
        buttons_total: The most buttons under one message. Default 100.
        button_text_max: The longest button text, in UTF-16 units. Default 64.
        callback_data_bytes: The longest ``callback_data`` of a button, in bytes of UTF-8.
            Default 64.
        url_max: The longest URL of a link button, a callback answer or a webhook, in
            characters. Default 1024.
        callback_answer_max: The longest text of a callback answer, in characters. Default 200.
        callback_window_ms: How long a button press can still be answered, in milliseconds.
            Default 60000.
        poll_timeout_max_s: The longest wait of a long poll, in seconds. Default 50.
        poll_limit_max: The most updates one poll hands over. Default 100.
        webhook_timeout_ms: How long the server waits for the webhook to answer, in
            milliseconds. Default 10000.
        sends_per_second: Messages per second, over all chats. Default 30.
        sends_per_group_per_minute: Messages per minute into one group or channel. Default 20.
        sends_per_private_per_second: Messages per second into one private chat. Default 1.
        api_calls_per_minute: API calls per minute, of any kind. Default 600.
        commands_per_scope: The most commands in one menu. Default 100.
        command_description_max: The longest command description, in characters. Default 256.
        upload_bytes_max: The largest file one upload takes, in bytes. Default 52428800
            (50 MiB).
        uploads_per_minute: Uploads per minute. Default 30.
        history_limit_max: The most messages one page of history hands over. Default 100.
    """

    text_max: int = 4096
    armored_max: int = 131072
    markup_bytes: int = 8192
    keyboard_rows: int = 20
    buttons_per_row: int = 8
    buttons_total: int = 100
    button_text_max: int = 64
    callback_data_bytes: int = 64
    url_max: int = 1024
    callback_answer_max: int = 200
    callback_window_ms: int = 60000
    poll_timeout_max_s: int = 50
    poll_limit_max: int = 100
    webhook_timeout_ms: int = 10000
    sends_per_second: int = 30
    sends_per_group_per_minute: int = 20
    sends_per_private_per_second: int = 1
    api_calls_per_minute: int = 600
    commands_per_scope: int = 100
    command_description_max: int = 256
    upload_bytes_max: int = 50 * 1024 * 1024
    uploads_per_minute: int = 30
    history_limit_max: int = 100

    def get(self, name: str, default: int | None = None) -> int | None:
        """Reads any limit by its JSON name, including the ones this version has no field for.

        It reads what the server sent, in ``raw``. A name the server left out gives ``default``,
        even when a field has a documented default for it.

        Args:
            name: The JSON name, such as ``"botsPerChat"``.
            default: What to return when the server did not send that limit as an integer.

        Returns:
            The server's number, or ``default``.

        Example:
            >>> limits = Limits.from_json({"textMax": 4096, "botsPerChat": 20})
            >>> limits.get("botsPerChat")
            20
            >>> limits.get("pollLimitMax") is None
            True
            >>> limits.poll_limit_max
            100
        """
        value = self.raw.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) else default

    @classmethod
    def from_json(cls, data: Any) -> Limits:
        """Builds the limits from the ``limits`` object of ``GET /me``.

        Args:
            data: The ``limits`` object. Anything else, ``None`` included, gives the defaults.

        Returns:
            The limits. The object itself is kept in ``raw``.
        """
        d = data if isinstance(data, Mapping) else {}
        values: dict[str, Any] = {}
        for f in fields(cls):
            if f.name == "raw":
                continue
            # text_max -> textMax, poll_timeout_max_s -> pollTimeoutMaxS
            head, *rest = f.name.split("_")
            value = d.get(head + "".join(p.capitalize() for p in rest))
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                values[f.name] = value
        return cls(**values, raw=d)


@dataclass(frozen=True, slots=True, kw_only=True)
class WebhookSummary(_Model):
    """The webhook in brief, as part of `BotInfo`.

    `Bot.get_webhook` gives the whole picture, as a `WebhookInfo`.

    Attributes:
        url: The address updates are posted to, or ``None`` when the bot uses long polling.
        has_secret: ``True`` when a secret is set. The secret itself is never shown.
        failures: Failed deliveries in a row. After ten, the owner is told in the app.
        last_error: What went wrong last, or ``None``.
    """

    url: str | None = None
    has_secret: bool = False
    failures: int = 0
    last_error: str | None = None

    @classmethod
    def from_json(cls, data: Any) -> WebhookSummary:
        """Builds the summary from the ``webhook`` object of ``GET /me``.

        Args:
            data: The ``webhook`` object. Anything else, ``None`` included, gives the defaults.

        Returns:
            The summary. The object itself is kept in ``raw``.
        """
        d = data if isinstance(data, Mapping) else {}
        return cls(
            url=_str(d, "url"),
            has_secret=_bool(d, "hasSecret"),
            failures=_int(d, "failures") or 0,
            last_error=_str(d, "lastError"),
            raw=d,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class BotInfo(_Model):
    """The bot as the server holds it, and the limits the server enforces (``GET /me``).

    `Bot.get_me` returns it, and `Bot` keeps the answer it got at start-up in `Bot.me`. It works
    while the bot is paused, so it is the way to find out why nothing else does.

    What the bot reads in a group or channel is not here: each room decides it for itself, and
    the bot's seat there says what it decided (`Seat.reads_all`).

    Attributes:
        id: The bot's ID, with its letter: ``"B48213907"``.
        username: Its main handle, without ``@``. It ends in ``bot``.
        name: Its display name.
        last_name: The surname the owner gave it, or ``None``.
        usernames: The other handles the owner added, oldest first. Each one reaches the bot
            like the main one: in a link, in a mention, after the ``@`` of a command.
        about: The bio under the name on its profile, or ``None``.
        description: What the bot does, shown before the first message, or ``None``.
        mode: The mode of the bot's private chats: ``protected`` once it has published a key,
            ``open`` before.
        fingerprint: The fingerprint of the published key, or ``None``.
        allow_groups: ``True`` when people can add the bot to groups.
        status: ``active``, or ``paused`` when the owner paused the bot.
        webhook: The webhook in brief.
        queued: How many updates are waiting to be fetched or delivered.
        limits: The numbers this server enforces.

    Example:
        Read a limit instead of hard-coding it::

            me = await bot.get_me()
            if len(report) > me.limits.text_max:
                report = report[: me.limits.text_max - 3] + "..."
    """

    id: str
    username: str
    name: str
    last_name: str | None = None
    usernames: tuple[str, ...] = ()
    about: str | None = None
    description: str | None = None
    mode: ChatMode = ChatMode.OPEN
    fingerprint: str | None = None
    allow_groups: bool = True
    status: BotStatus = BotStatus.ACTIVE
    webhook: WebhookSummary = field(default_factory=WebhookSummary)
    queued: int = 0
    limits: Limits = field(default_factory=Limits)

    @property
    def handles(self) -> tuple[str, ...]:
        """Every handle the bot answers to, the main one first and then `usernames`."""
        return tuple(h for h in (self.username, *self.usernames) if h)

    @property
    def protected(self) -> bool:
        """``True`` when `mode` is ``protected``, so the bot's private chats are encrypted."""
        return self.mode == ChatMode.PROTECTED

    @property
    def paused(self) -> bool:
        """``True`` when the owner paused the bot.

        While it is paused, most API calls raise `BotPausedError` (see `BotStatus.PAUSED`).
        """
        return self.status == BotStatus.PAUSED

    @classmethod
    def from_json(cls, data: Any) -> BotInfo:
        """Builds the bot from the answer of ``GET /me``.

        Args:
            data: The answer.

        Returns:
            The bot, with ``data`` kept in ``raw``. A missing ``mode`` is read as
            ``"protected"`` when ``fingerprint`` is set, ``"open"`` otherwise.

        Raises:
            PayloadError: ``data`` is not an object, or its ``id`` is missing.
        """
        d = _obj(data, "me")
        return cls(
            id=_req_id(d, "id", "me"),
            username=_str(d, "username") or "",
            name=_str(d, "name") or "",
            last_name=_str(d, "lastName"),
            usernames=tuple(u for u in _list(d, "usernames") if isinstance(u, str) and u),
            about=_str(d, "about"),
            description=_str(d, "description"),
            # a bot is protected exactly when it has a published key
            mode=ChatMode(_str(d, "mode") or ("protected" if _str(d, "fingerprint") else "open")),
            fingerprint=_str(d, "fingerprint"),
            allow_groups=_bool(d, "allowGroups", True),
            status=BotStatus(_str(d, "status") or "active"),
            webhook=WebhookSummary.from_json(d.get("webhook")),
            queued=_int(d, "queued") or 0,
            limits=Limits.from_json(d.get("limits")),
            raw=d,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class WebhookInfo(_Model):
    """The webhook, if any, and how deliveries are going: `Bot.get_webhook`.

    ``GET /webhook`` fills every field. ``POST /webhook`` and ``DELETE /webhook``
    (`Bot.set_webhook` and `Bot.delete_webhook`) answer with ``url``, ``has_secret`` and
    ``pending`` only, and the other fields keep their defaults.

    Attributes:
        url: The address updates are posted to, or ``None`` when the bot uses long polling.
        has_secret: ``True`` when a secret is set. The secret itself is never shown.
        pending: How many updates are still waiting.
        failures: Failed deliveries in a row. After ten, the owner is told in the app.
        last_error: What went wrong last, or ``None``.
        last_ok_at: When a delivery last worked, or ``None``.
        next_attempt_at: While the server backs off after a failure, when it tries again;
            otherwise ``None``.

    Example:
        See how the webhook is doing::

            info = await bot.get_webhook()
            if info.failures:
                print(f"{info.failures} failures in a row, last: {info.last_error}")
    """

    url: str | None = None
    has_secret: bool = False
    pending: int = 0
    failures: int = 0
    last_error: str | None = None
    last_ok_at: datetime | None = None
    next_attempt_at: datetime | None = None

    @classmethod
    def from_json(cls, data: Any) -> WebhookInfo:
        """Builds the webhook's state from the answer of ``GET /webhook``.

        ``POST /webhook`` and ``DELETE /webhook`` answer with the same object, with fewer fields.

        Args:
            data: The answer.

        Returns:
            The webhook's state, with ``data`` kept in ``raw``.

        Raises:
            PayloadError: ``data`` is not an object.
        """
        d = _obj(data, "webhook")
        return cls(
            url=_str(d, "url"),
            has_secret=_bool(d, "hasSecret"),
            pending=_int(d, "pending") or 0,
            failures=_int(d, "failures") or 0,
            last_error=_str(d, "lastError"),
            last_ok_at=_when(d, "lastOkAt"),
            next_attempt_at=_when(d, "nextAttemptAt"),
            raw=d,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class Command(_Model):
    """One entry of the menu the app shows when somebody types ``/``.

    `Bot.get_commands`, given a scope, returns a list of them. You can also make one to pass to
    `Bot.set_commands`, which takes ``(name, description)`` pairs as well.

    Attributes:
        command: The name, without the ``/``: 1 to 32 lowercase letters, digits and ``_``.
        description: What the command does, 1 to 256 characters.
    """

    command: str
    description: str

    def to_json(self) -> dict[str, str]:
        """Turns the command into the API's JSON form.

        Returns:
            A dict with ``command`` and ``description``, as ``PUT /commands`` takes it.

        Example:
            >>> Command(command="start", description="Say hello").to_json()
            {'command': 'start', 'description': 'Say hello'}
        """
        return {"command": self.command, "description": self.description}

    @classmethod
    def from_json(cls, data: Any) -> Command:
        """Builds a command from the API's JSON (``command``, ``description``).

        Args:
            data: One command object, as ``GET /commands`` lists them.

        Returns:
            The command, with ``data`` kept in ``raw``. A missing field is ``""``.

        Raises:
            PayloadError: ``data`` is not an object.
        """
        d = _obj(data, "command")
        return cls(command=_str(d, "command") or "", description=_str(d, "description") or "", raw=d)


@dataclass(frozen=True, slots=True, kw_only=True)
class CommandScope(_Model):
    """One command menu, and where the app shows it.

    `Bot.get_commands`, without a scope, lists one per scope; `Bot.set_commands` and
    `Bot.delete_commands` answer with the one they changed. The app shows the narrowest scope
    that has commands: in a private chat ``private``, then ``default``; in a group
    ``chat:<id>``, then ``groupAdmins`` (for admins), then ``groups``, then ``default``.

    Attributes:
        scope: ``"default"``, ``"private"``, ``"groups"``, ``"groupAdmins"``, or
            ``"chat:<id>"`` (``"chat:G48213907"``) for one chat.
        commands: The commands of the menu.
    """

    scope: str
    commands: tuple[Command, ...]

    @classmethod
    def from_json(cls, data: Any) -> CommandScope:
        """Builds a menu from the API's JSON (``scope``, ``commands``).

        Args:
            data: One scope object, as ``GET /commands`` lists them and ``PUT /commands``
                answers.

        Returns:
            The menu, with ``data`` kept in ``raw``. A missing ``scope`` is ``"default"``.

        Raises:
            PayloadError: ``data`` is not an object, or one of its commands is not an object.
        """
        d = _obj(data, "scope")
        return cls(
            scope=_str(d, "scope") or "default",
            commands=tuple(Command.from_json(c) for c in _list(d, "commands")),
            raw=d,
        )


# --- messages ---


@dataclass(frozen=True, slots=True, kw_only=True)
class Media(_Model):
    """A picture, clip, voice note or file attached to a message: one item of ``message.media``.

    In a readable chat the server reads the list out of the message; in an encrypted chat the
    bot reads it from inside the decrypted message. Either way it describes every kind of
    attachment. `Bot.download_media` downloads a file, decrypts it when it is ``encrypted`` and
    checks its ``sha256``. A file of an encrypted group is sealed only to the keys the
    sender's phone sealed it to, so a bot gets at it only when the room lets it read every
    message (`Seat.reads_all`); otherwise the download raises `NotARecipientError`.

    Attributes:
        id: The file's ID, a UUID.
        kind: What it is: ``"image"``, ``"video"``, ``"audio"`` (a voice note) or ``"file"``
            (anything else, music included).
        mime: The MIME type, such as ``"image/jpeg"``, or ``None``.
        url: Where the file is, ``/media/<id>`` on the API server. `Bot.download_media` uses it.
        width: The width in pixels, or ``None``.
        height: The height in pixels, or ``None``.
        duration_ms: The length of a clip or a voice note, in milliseconds, or ``None``. The
            API calls it ``duration``.
        name: The file name, when the sender's app gives one, or ``None``.
        size: The size in bytes, when the sender's app gives it, or ``None``.
        encrypted: ``True`` when the file on the server is sealed with OpenPGP: always in an
            encrypted chat, and in a readable one for a file first sent to an encrypted chat.
            `Bot.download_media` then decrypts it.
        thumb: A tiny JPEG of a picture or a clip, base64, for a blurred preview, or ``None``.
        sha256: The SHA-256 of the file as it opens, lowercase hex, when the sender gave it.
            `Bot.download_media` checks it.
    """

    id: str
    kind: str
    mime: str | None = None
    url: str | None = None
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None
    name: str | None = None
    size: int | None = None
    encrypted: bool = False
    thumb: str | None = None
    sha256: str | None = None

    @classmethod
    def from_json(cls, data: Any) -> Media:
        """Builds an attachment from an item of the ``media`` of a message in a readable chat.

        Attachments of encrypted chats are read from inside the decrypted message instead.

        Args:
            data: The media object (``id``, ``kind``, ``mime``, ``url``, ``width``,
                ``height``, ``duration``, ``name``, ``size``, ``plain``, ``thumb``,
                ``sha256``).

        Returns:
            The attachment, with ``data`` kept in ``raw``. A missing ``kind`` is ``"file"``;
            ``encrypted`` is ``True`` only when ``plain`` is ``false``.

        Raises:
            PayloadError: ``data`` is not an object, or its ``id`` is missing.
        """
        d = _obj(data, "media")
        media_id = _str(d, "id")
        if not media_id:
            raise PayloadError("media.id: missing")
        duration = _num(d, "duration")
        sha256 = _str(d, "sha256")
        return cls(
            id=media_id,
            kind=_str(d, "kind") or "file",
            mime=_str(d, "mime"),
            url=_str(d, "url"),
            width=_int(d, "width"),
            height=_int(d, "height"),
            duration_ms=round(duration) if duration is not None else None,
            name=_str(d, "name"),
            size=_int(d, "size"),
            # an older server sends no "plain": everything it listed went up in the clear
            encrypted=d.get("plain") is False,
            thumb=_str(d, "thumb"),
            sha256=sha256.lower() if sha256 else None,
            raw=d,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class MediaIn(_Model):
    """A file to send into a readable chat, described the way phones lay it out.

    Upload the file first (`AsyncClient.upload_file`), then pass one ``MediaIn`` per file in the
    ``media`` of `AsyncClient.send_message`. `Bot.send_media` does all of it, and also works in
    encrypted chats, where the description travels sealed inside the message instead.

    Attributes:
        id: The ID `AsyncClient.upload_file` returned.
        kind: ``"image"``, ``"video"``, ``"audio"`` (a voice note) or ``"file"`` (anything
            else, music included).
        mime: The MIME type, such as ``"image/jpeg"``, or ``None`` for the default of the kind.
        name: The file name, up to 255 characters, without slashes, or ``None``.
        size: The size in bytes, or ``None``.
        width: The width of a picture or a clip, in pixels, or ``None``.
        height: The height, in pixels, or ``None``.
        duration_ms: The length of a clip or a voice note, in milliseconds, or ``None``.
        thumb: A tiny JPEG, base64, up to 12000 characters, shown blurred until the file is
            fetched, or ``None``.
        sha256: The SHA-256 of the file, 64 hex digits, which phones check after downloading,
            or ``None``.

    Example:
        >>> MediaIn(id="0f3c5a3e-1111-2222-3333-444455556666", kind="image", width=640).to_json()
        {'id': '0f3c5a3e-1111-2222-3333-444455556666', 'kind': 'image', 'width': 640}
    """

    id: str
    kind: str = "file"
    mime: str | None = None
    name: str | None = None
    size: int | None = None
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None
    thumb: str | None = None
    sha256: str | None = None

    def to_json(self) -> dict[str, Any]:
        """Turns the description into the API's JSON form, leaving out what is ``None``.

        Returns:
            A dict as ``POST /messages`` takes it in ``media``.
        """
        names = (
            ("id", self.id),
            ("kind", self.kind),
            ("mime", self.mime),
            ("name", self.name),
            ("size", self.size),
            ("width", self.width),
            ("height", self.height),
            ("duration", self.duration_ms),
            ("thumb", self.thumb),
            ("sha256", self.sha256),
        )
        return {key: value for key, value in names if value is not None}


@dataclass(frozen=True, slots=True, kw_only=True)
class Sticker(_Model):
    """A sticker: the one a message carries, or what `Bot.get_sticker` says about one.

    Attributes:
        id: The sticker's ID, or ``None`` when the message did not say.
        pack_id: The pack it belongs to, or ``None``.
        media_id: The ID of the file it is drawn from. Pass it to `Bot.send_sticker` as
            ``media_id``: it proves the bot was shown a sticker of a pack that is not public.
        emoji: The emoji it stands for, or ``None``.
        url: Where its file is, ``/media/<id>`` on the API server, or ``None``.
    """

    id: int | None = None
    pack_id: int | None = None
    media_id: str = ""
    emoji: str | None = None
    url: str | None = None

    @classmethod
    def from_json(cls, data: Any) -> Sticker:
        """Builds a sticker from the API's JSON (``id``, ``packId``, ``mediaId``, ``emoji``, ``url``).

        Args:
            data: The sticker object.

        Returns:
            The sticker, with ``data`` kept in ``raw``.

        Raises:
            PayloadError: ``data`` is not an object.
        """
        d = _obj(data, "sticker")
        return cls(
            id=_int(d, "id"),
            pack_id=_int(d, "packId"),
            media_id=_str(d, "mediaId") or "",
            emoji=_str(d, "emoji") or None,
            url=_str(d, "url"),
            raw=d,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ForwardChat(_Model):
    """The group or channel a forwarded message was said in: `Forward.chat`.

    Attributes:
        id: The chat's ID, ``"G48213907"`` or ``"C48213907"``, or ``None`` when it is not known.
        title: Its name as the forward remembers it, or ``None``.
        username: Its public handle as the forward remembers it, or ``None``.
        message_id: The message there, or the channel's post, or ``None``.
    """

    id: str | None = None
    title: str | None = None
    username: str | None = None
    message_id: int | None = None

    @classmethod
    def from_json(cls, data: Any) -> ForwardChat:
        """Builds the chat from the ``chat`` of a forward.

        Args:
            data: The ``chat`` object.

        Returns:
            The chat, with ``data`` kept in ``raw``.

        Raises:
            PayloadError: ``data`` is not an object.
        """
        d = _obj(data, "forward.chat")
        return cls(
            id=_str(d, "id"),
            title=_str(d, "title"),
            username=_str(d, "username"),
            message_id=_int(d, "messageId"),
            raw=d,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class Forward(_Model):
    """Who a forwarded message came from, as the forward says: `MessageBase.forward`.

    The forward is the word of whoever forwarded it. In a readable chat the server keeps an ID
    only for an account or a chat that exists; in an encrypted chat the bot reads the forward
    itself, and the IDs are what the forwarder's app wrote, unchecked.

    Attributes:
        name: The name of the person or the channel it came from, or ``None``.
        username: Their handle, or ``None``.
        user_id: The person it came from, unless they switched the link to their profile off,
            or it came from a channel. ``None`` then.
        chat: The group or channel it was said in, or ``None`` for a private chat.
    """

    name: str | None = None
    username: str | None = None
    user_id: str | None = None
    chat: ForwardChat | None = None

    @classmethod
    def from_json(cls, data: Any) -> Forward:
        """Builds the forward from the ``forward`` of a message in a readable chat.

        Args:
            data: The ``forward`` object.

        Returns:
            The forward, with ``data`` kept in ``raw``.

        Raises:
            PayloadError: ``data`` is not an object.
        """
        d = _obj(data, "forward")
        chat = d.get("chat")
        return cls(
            name=_str(d, "name"),
            username=_str(d, "username"),
            user_id=_str(d, "userId"),
            chat=ForwardChat.from_json(chat) if isinstance(chat, Mapping) else None,
            raw=d,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class LinkCard(_Model):
    """The card the sender's phone made for the first link in a message: `MessageBase.link`.

    Attributes:
        url: The link.
        title: The page's title, or ``None``.
        description: The page's description, or ``None``.
        site_name: The site's name, or ``None``.
    """

    url: str
    title: str | None = None
    description: str | None = None
    site_name: str | None = None

    @classmethod
    def from_json(cls, data: Any) -> LinkCard:
        """Builds the card from the ``link`` of a message.

        Args:
            data: The ``link`` object.

        Returns:
            The card, with ``data`` kept in ``raw``. A missing ``url`` is ``""``.

        Raises:
            PayloadError: ``data`` is not an object.
        """
        d = _obj(data, "link")
        return cls(
            url=_str(d, "url") or "",
            title=_str(d, "title"),
            description=_str(d, "description"),
            site_name=_str(d, "siteName"),
            raw=d,
        )


@dataclass(frozen=True, slots=True)
class Verification:
    """The outcome of checking the signature of an encrypted message.

    The bot sets one on every message it decrypts (``message.verification``; ``None`` in a
    readable chat), and `BotKey.decrypt` returns one in its `Decrypted`. By default
    (``verify_signatures="auto"``) the bot only reports what it found: a message with a bad
    signature, or none, still reaches the handlers. With ``verify_signatures="require"``, a
    message that is not ``valid`` raises `SignatureError` and goes to the error handlers instead.

    Attributes:
        status: What the check found.
        signer_fingerprint: When `status` is ``valid``, the fingerprint of the key that made the
            signature, in upper-case hex. ``None`` otherwise.
        detail: Why the status is not ``valid``, in a few words for a log, or ``None``. It is
            written for people: branch on `status`, not on this text.

    Example:
        Tell the sender what the bot found::

            from hidesay_bot import filters as f

            @bot.on_message(f.encrypted)
            async def check(message):
                v = message.verification
                if v.valid:
                    await message.reply("Signed with your key.")
                else:
                    await message.reply(f"Signature: {v.status} ({v.detail})")

    Note:
        A ``valid`` signature proves that the message comes from whoever holds the key the
        server lists for the sender, and that nobody changed it. It does not prove that the key
        belongs to that person: the server hands out the keys.
    """

    status: SignatureStatus
    signer_fingerprint: str | None = None
    detail: str | None = None

    @property
    def valid(self) -> bool:
        """``True`` when `status` is ``valid``."""
        return self.status == SignatureStatus.VALID


@dataclass(frozen=True, slots=True, kw_only=True)
class MessageBase(_Model):
    """A message a person wrote, as the bot receives it: the data, without methods.

    Handlers get it as a `Message` (a `SyncMessage` on a `SyncBot`), which adds `Message.reply`
    and the other methods. The bot's own messages never arrive as updates: sends and edits
    return them as `SentMessage` objects.

    In an end-to-end encrypted chat, the bot decrypts the message before any handler sees it.
    `text`, `media`, `sticker`, `forward` and `link` then come from inside it, `armored` keeps
    the original, `plaintext` the exact decrypted body, and `verification` says what the
    signature check found. A message the bot cannot decrypt goes to the error handlers instead.
    In a readable chat the server reads the message itself: `armored`, `plaintext` and
    `verification` are ``None``.

    Attributes:
        id: The message's ID, unique inside its chat.
        chat: The chat it was written in.
        from_user: Who wrote it. ``None`` for a channel post in the channel's name: the bot,
            like any reader, is not told who wrote it.
        date: When it was sent, in UTC.
        reply_to: The ID of the message it answers, or ``None`` (also when that message never reached the bot).
        kind: The kind of message, as the server says: ``"user"`` for anything a person typed
            or attached.
        text: What the person wrote, or the caption of a file or an album. ``None`` when there
            are no words, as for a sticker or a call notice. In an encrypted chat it is the
            decrypted text as the person wrote it; only ``CR LF`` line ends become ``LF``.
        media: The pictures, clips, voice notes and files that came with it, one `Media` each.
            Empty when there are none.
        signature: For a channel post in the channel's name, the name a signed post shows its
            readers. ``None`` when the channel does not sign its posts, and for any other
            message.
        armored: In an encrypted chat, the OpenPGP message as it arrived, ASCII-armored.
            ``None`` in a readable chat.
        plaintext: In an encrypted chat, the decrypted body exactly as the sender's app wrote
            it, in the app's internal format: a picture or a sticker is a marker followed by
            JSON. The bot reads `text` and `media` out of it. ``None`` in a readable chat.
        verification: In an encrypted chat, what the check of the sender's signature found.
            ``None`` in a readable chat.
        reply_to_message: The message `reply_to` quotes, when the bot was handed that one too:
            its own, or one the room delivered to it. ``None`` otherwise, even when
            `reply_to` is set. It quotes nothing further.
        thread_root: For a comment, the channel post it hangs under. ``None`` for anything
            else.
        markup: The buttons under it. Only ever on a bot's message, as in a quoted one.
        sticker: The sticker, when the message is one.
        forward: Who the message was forwarded from, when it is a forward.
        link: The card the sender's phone made for the first link in the text, if it made one.

    Example:
        >>> message = MessageBase.from_json({
        ...     "id": 1834,
        ...     "chat": {"id": "48213907", "kind": "private"},
        ...     "from": {"id": "48213907", "username": "anna"},
        ...     "date": 1767225600000,
        ...     "text": "/weather rome",
        ... })
        >>> message.chat_id, message.from_user.mention, message.is_command
        ('48213907', '@anna', True)
        >>> message.date.isoformat()
        '2026-01-01T00:00:00+00:00'

    Note:
        In a readable chat, the server marks a message ``armored`` whenever its text holds an
        OpenPGP armor line, as when somebody pastes a PGP block. If the bot cannot decrypt it,
        the handlers get it as words anyone in the chat can read: `text` holds the block, and
        `armored` is ``None``.
    """

    id: int
    chat: ChatRef
    from_user: User | None
    date: datetime
    reply_to: int | None = None
    kind: str = "user"
    text: str | None = None
    media: tuple[Media, ...] = ()
    signature: str | None = None
    armored: str | None = None
    plaintext: str | None = None
    verification: Verification | None = None
    reply_to_message: MessageBase | None = None
    thread_root: int | None = None
    markup: InlineKeyboard | None = None
    sticker: Sticker | None = None
    forward: Forward | None = None
    link: LinkCard | None = None
    _bot: Any = field(default=None, repr=False, compare=False, hash=False)

    @property
    def encrypted(self) -> bool:
        """``True`` when the message came end-to-end encrypted, so `armored` is set."""
        return self.armored is not None

    @property
    def chat_id(self) -> str:
        """The ID of the chat, the same as ``message.chat.id``."""
        return self.chat.id

    @property
    def is_comment(self) -> bool:
        """``True`` for a comment under a channel post (`thread_root` is set)."""
        return self.thread_root is not None

    @property
    def is_command(self) -> bool:
        """``True`` when `text` starts with ``/``.

        It looks at the first character only. `~hidesay_bot.filters.parse_command` tells a real
        command from a text such as ``"/ hi"``, and reads its name and arguments.
        """
        return bool(self.text) and self.text.startswith("/")  # type: ignore[union-attr]

    @classmethod
    def from_json(cls, data: Any) -> MessageBase:
        """Builds a message from the API's JSON, as an update carries it.

        Nothing is decrypted here: from an encrypted chat, ``text`` stays ``None`` and
        ``armored`` holds the message. The bot decrypts messages before its handlers get them.

        Args:
            data: The ``message`` object of an update.

        Returns:
            The message, with ``data`` kept in ``raw``. A missing ``kind`` is ``"user"``.
            Called on `Message` or `SyncMessage`, it builds one of those, attached to no bot
            (see `Message.bind`).

        Raises:
            PayloadError: ``data`` is not an object; its ``id`` or ``date`` is missing or not
                an integer; its ``chat`` is missing or not valid; or its ``from``, one of its
                ``media`` items or the message it quotes is not valid.
        """
        d = _obj(data, "message")
        date = _when(d, "date")
        if date is None:
            raise PayloadError("message.date: expected unix milliseconds")
        sender = d.get("from")
        quoted = d.get("replyToMessage")
        markup = d.get("markup")
        sticker = d.get("sticker")
        forward = d.get("forward")
        link = d.get("link")
        return cls(
            id=_req_int(d, "id", "message"),
            chat=ChatRef.from_json(d.get("chat")),
            from_user=User.from_json(sender) if isinstance(sender, Mapping) else None,
            date=date,
            reply_to=_int(d, "replyTo"),
            kind=_str(d, "kind") or "user",
            text=_str(d, "text"),
            media=tuple(Media.from_json(m) for m in _list(d, "media") if isinstance(m, Mapping)),
            signature=_str(d, "signature"),
            armored=_str(d, "armored"),
            # the quoted one is plain data, whatever class reads the outer message
            reply_to_message=MessageBase.from_json(quoted) if isinstance(quoted, Mapping) else None,
            thread_root=_int(d, "threadRoot"),
            markup=InlineKeyboard.from_json(markup) if isinstance(markup, Mapping) else None,
            sticker=Sticker.from_json(sticker) if isinstance(sticker, Mapping) else None,
            forward=Forward.from_json(forward) if isinstance(forward, Mapping) else None,
            link=LinkCard.from_json(link) if isinstance(link, Mapping) else None,
            raw=d,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class SentMessageBase(_Model):
    """A message of the bot's own, as a send or an edit returns it: the data, without methods.

    `Bot.send_message`, `Bot.edit_message` and the methods that send, such as `Message.reply`,
    return it as a `SentMessage` (a `SyncSentMessage` on a `SyncBot`), which adds
    `SentMessage.edit` and `SentMessage.delete`. The fields are what the server stored.

    Attributes:
        id: The message's ID, unique inside its chat.
        chat: The chat it was sent to.
        date: When it was sent, in UTC.
        reply_to: The ID of the message it quotes, or ``None``.
        markup: Its buttons, or ``None`` when it has none.
        text: Its text, in a readable chat. ``None`` in an encrypted chat, where the server
            cannot read it. For a file, a sticker or a forward it is the body the phones read,
            the app's internal format (a marker followed by JSON).
        armored: In an encrypted chat, the OpenPGP message that was sent, sealed and signed,
            ASCII-armored. ``None`` in a readable chat.
        thread_root: For a comment, the channel post it hangs under. ``None`` for anything
            else.
    """

    id: int
    chat: ChatRef
    date: datetime
    reply_to: int | None = None
    markup: InlineKeyboard | None = None
    text: str | None = None
    armored: str | None = None
    thread_root: int | None = None
    _bot: Any = field(default=None, repr=False, compare=False, hash=False)

    @property
    def encrypted(self) -> bool:
        """``True`` when the message went out end-to-end encrypted, so `armored` is set."""
        return self.armored is not None

    @classmethod
    def from_json(cls, data: Any) -> SentMessageBase:
        """Builds the message from the answer of ``POST /messages`` or ``PATCH /messages/{id}``.

        Args:
            data: The answer.

        Returns:
            The message, with ``data`` kept in ``raw``. Called on `SentMessage` or
            `SyncSentMessage`, it builds one of those, attached to no bot (see
            `SentMessage.bind`).

        Raises:
            PayloadError: ``data`` is not an object; its ``id`` or ``date`` is missing or not
                an integer; or its ``chat`` is missing or not valid.
        """
        d = _obj(data, "sent message")
        date = _when(d, "date")
        if date is None:
            raise PayloadError("message.date: expected unix milliseconds")
        markup = d.get("markup")
        return cls(
            id=_req_int(d, "id", "message"),
            chat=ChatRef.from_json(d.get("chat")),
            date=date,
            reply_to=_int(d, "replyTo"),
            markup=InlineKeyboard.from_json(markup) if isinstance(markup, Mapping) else None,
            text=_str(d, "text"),
            armored=_str(d, "armored"),
            thread_root=_int(d, "threadRoot"),
            raw=d,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class CallbackQueryBase(_Model):
    """A press on a button with callback data: the data, without methods.

    Handlers get it as a `CallbackQuery` (a `SyncCallbackQuery` on a `SyncBot`), which adds
    `CallbackQuery.answer` and the methods that change the message. The pressed button spins on
    the person's phone until the bot answers, which it can do once, within 60 seconds of the
    press. A press that nothing answered gets an empty answer from the bot once the update is
    handled, so the button stops spinning (unless ``auto_answer_callbacks=False``).

    Attributes:
        id: The ID of the press, to answer it with.
        from_user: Who pressed the button.
        chat: The chat of the message the button is under.
        message_id: The ID of the message the button is under, one of the bot's own.
        data: The ``callback_data`` of the button that was pressed.
        expires_at: The deadline to answer, 60 seconds after the press, in UTC. ``None`` when
            the server does not say.

    Warning:
        The data of a button travels in clear, even in an encrypted chat: the server has to know
        which button was pressed. Put in it an ID that means something only to your bot
        (``order:8812``), not personal data.
    """

    id: str
    from_user: User
    chat: ChatRef
    message_id: int
    data: str
    expires_at: datetime | None = None
    _bot: Any = field(default=None, repr=False, compare=False, hash=False)

    @property
    def expired(self) -> bool:
        """``True`` once `expires_at` has passed, by this computer's clock.

        After that the server refuses an answer with `CallbackExpiredError`. ``False`` when
        `expires_at` is ``None``.
        """
        return self.expires_at is not None and datetime.now(UTC) >= self.expires_at

    @classmethod
    def from_json(cls, data: Any) -> CallbackQueryBase:
        """Builds a press from the ``callback`` object of a ``callback_query`` update.

        Args:
            data: The ``callback`` object.

        Returns:
            The press, with ``data`` kept in ``raw``. When the object has no ``data`` string,
            the `data` field is ``""``. Called on `CallbackQuery` or `SyncCallbackQuery`, it
            builds one of those, attached to no bot (see `CallbackQuery.bind`).

        Raises:
            PayloadError: ``data`` is not an object; its ``id`` is missing or not a string; its
                ``messageId`` is missing or not an integer; or its ``from`` or ``chat`` is
                missing or not valid.
        """
        d = _obj(data, "callback")
        query_id = _str(d, "id")
        if not query_id:
            raise PayloadError("callback.id: missing")
        return cls(
            id=query_id,
            from_user=User.from_json(d.get("from")),
            chat=ChatRef.from_json(d.get("chat")),
            message_id=_req_int(d, "messageId", "callback"),
            data=_str(d, "data") or "",
            expires_at=_when(d, "expiresAt"),
            raw=d,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class DeletedMessages(_Model):
    """Messages deleted for everyone in one chat: what a ``deleted_messages`` update carries.

    Handlers registered with `Router.on_deleted_messages` get it. In a group it lists the
    messages the bot had received. In a private chat it lists any message of the chat, the
    bot's own included: also the ones the bot deleted itself, and a whole history the person
    cleared. It does not carry who deleted them. Its ``raw`` is the JSON of the whole update.

    Attributes:
        chat: The chat the messages were deleted from.
        message_ids: The IDs of the deleted messages.

    Example:
        Forget what the bot kept about them::

            @bot.on_deleted_messages()
            async def forget(deleted):
                for message_id in deleted.message_ids:
                    await db.forget(deleted.chat.id, message_id)
    """

    chat: ChatRef
    message_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class ReactionCount(_Model):
    """How many people reacted to a message with one emoji: an item of `ReactionUpdate.reactions`.

    Attributes:
        emoji: The emoji, or a custom emoji as the app writes it.
        count: How many people reacted with it.
    """

    emoji: str
    count: int = 0

    @classmethod
    def from_json(cls, data: Any) -> ReactionCount:
        """Builds the count from the API's JSON (``emoji``, ``count``).

        Args:
            data: One item of ``reactions``.

        Returns:
            The count, with ``data`` kept in ``raw``.

        Raises:
            PayloadError: ``data`` is not an object.
        """
        d = _obj(data, "reaction")
        return cls(emoji=_str(d, "emoji") or "", count=_int(d, "count") or 0, raw=d)


@dataclass(frozen=True, slots=True, kw_only=True)
class ReactionUpdate(_Model):
    """Somebody put, changed or took back a reaction: what a ``message_reaction`` update carries.

    Handlers registered with `Router.on_reaction` get it. Only reactions on a message the bot
    was handed come through, its own messages included. Its ``raw`` is the JSON of the whole
    update.

    Attributes:
        chat: The chat of the message.
        message_id: The message that was reacted to.
        thread_root: When the message is a comment, the post it hangs under; otherwise ``None``.
        from_user: Who reacted.
        date: When, in UTC, or ``None`` when the update does not say.
        old: The emoji this person had on the message before, or ``None``.
        new: The emoji they have on it now, or ``None`` when they took it back.
        reactions: Every reaction on the message now, by emoji, with how many people put it.

    Example:
        Count the thumbs up under a poll::

            @bot.on_reaction(emoji="👍")
            async def liked(reaction):
                log.info("message %s now has %s", reaction.message_id, reaction.counts.get("👍", 0))
    """

    chat: ChatRef
    message_id: int
    thread_root: int | None = None
    from_user: User | None = None
    date: datetime | None = None
    old: str | None = None
    new: str | None = None
    reactions: tuple[ReactionCount, ...] = ()

    @property
    def added(self) -> bool:
        """``True`` when the person had no reaction there and now has one."""
        return self.old is None and self.new is not None

    @property
    def removed(self) -> bool:
        """``True`` when the person took their reaction back."""
        return self.old is not None and self.new is None

    @property
    def counts(self) -> dict[str, int]:
        """`reactions` as a dict, emoji to count."""
        return {r.emoji: r.count for r in self.reactions}


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatMemberUpdate(_Model):
    """A change of a seat in a chat: what a ``my_chat_member`` or a ``chat_member`` update carries.

    With `user` left ``None`` it is the bot's own seat (``my_chat_member``), and handlers
    registered with `Router.on_chat_member` get it: the bot was added, promoted, restricted,
    let read every message or stopped, removed, or it left; in a private chat, the person
    blocked it (the seat becomes ``kicked``) or unblocked it (``member`` again). With `user`
    set it is somebody else's seat (``chat_member``), for `Router.on_member_update`: they
    joined, left, were removed, banned, promoted or restricted. `joined`, `left`, `blocked`,
    `unblocked` and `reads_all_changed` name the usual cases; compare `old` and `new` for the
    others, such as a promotion (``change.new.is_admin``). Its ``raw`` is the JSON of the whole
    update.

    When the bot is added to a group or channel, `chat` is the whole room, a `Chat` with its
    settings and permissions, so the bot knows where it is and what it may do without asking.
    The room does not let a new bot read every message (``new.reads_all`` is ``False``): if
    that is what the bot is for, this is the moment to say so in the room, and why.

    Attributes:
        chat: The chat where the seat changed: a `Chat` for the bot's own seat in a group or
            channel, a `ChatRef` otherwise.
        from_user: Who made the change, or ``None`` when the update does not say (a channel
            that hides its admins hides them from a bot that isn't one).
        date: When it changed, in UTC, or ``None`` when the update does not say.
        old: The seat before the change.
        new: The seat after it.
        user: Whose seat it is, for a ``chat_member`` update. ``None`` for the bot's own.

    Example:
        Say hello in a new group, ask to read everything when the bot needs it, and log who
        blocks the bot::

            @bot.on_chat_member()
            async def seat_changed(change):
                if change.joined and change.chat.is_group:
                    await bot.send_message(change.chat, "Hello. Type /help to see what I do.")
                    if not change.reads_all:
                        await bot.send_message(
                            change.chat,
                            "I keep the minutes, so I need to read every message: an admin can "
                            "allow it from my entry in the member list.",
                        )
                elif change.blocked:
                    log.info("blocked in chat %s", change.chat.id)
    """

    chat: ChatRef
    from_user: User | None
    date: datetime | None
    old: Seat
    new: Seat
    user: User | None = None

    @property
    def reads_all(self) -> bool:
        """``True`` when the room lets this seat's bot read every message now (``new.reads_all``)."""
        return self.new.reads_all

    @property
    def reads_all_changed(self) -> bool:
        """``True`` when the room just let the bot read every message, or stopped it.

        Only counts while the bot stays in the chat: being added or removed is `joined` or
        `left`.
        """
        return self.old.is_inside and self.new.is_inside and self.old.reads_all != self.new.reads_all

    @property
    def joined(self) -> bool:
        """``True`` when the bot is in the chat now and was not before.

        It was added to the chat, or unblocked in a private chat. In the chat means an
        ``admin``, ``member`` or ``restricted`` seat (`Seat.is_inside`).
        """
        return self.new.is_inside and not self.old.is_inside

    @property
    def left(self) -> bool:
        """``True`` when the bot was in the chat and is not any more.

        It was removed or it left, or the person blocked it in a private chat.
        """
        return self.old.is_inside and not self.new.is_inside

    @property
    def blocked(self) -> bool:
        """``True`` when the person in a private chat blocked the bot.

        Until they unblock it, a send to them raises `BlockedByUserError`.
        """
        return self.chat.is_private and self.new.status == SeatStatus.KICKED

    @property
    def unblocked(self) -> bool:
        """``True`` when the person in a private chat unblocked the bot."""
        return self.chat.is_private and self.old.status == SeatStatus.KICKED and self.new.is_inside


@dataclass(frozen=True, slots=True, kw_only=True)
class JoinRequest(_Model):
    """Somebody asked to come into a group or channel through a link that needs approval.

    A ``join_request`` update carries one, for the bots with the *add members* right, and
    `Bot.get_join_requests` lists the ones waiting. Answer with `Bot.decide_join_request`.

    Attributes:
        chat: The group or channel.
        from_user: Who asked.
        date: When they asked, in UTC, or ``None`` when the server did not say.
    """

    chat: ChatRef
    from_user: User
    date: datetime | None = None

    @classmethod
    def from_json(cls, data: Any, chat: ChatRef | None = None) -> JoinRequest:
        """Builds a request from an item of ``GET /chats/{id}/requests``, or a ``join_request`` update.

        Args:
            data: The item (``user``, ``date``) or the update (``chat``, ``from``, ``date``).
            chat: The chat the list was asked for, when ``data`` does not name it.

        Returns:
            The request, with ``data`` kept in ``raw``.

        Raises:
            PayloadError: ``data`` is not an object, it names no chat and none is given, or
                its ``user`` (``from``) is missing or not valid.
        """
        d = _obj(data, "join request")
        if chat is None:
            chat = ChatRef.from_json(d.get("chat"))
        return cls(
            chat=chat,
            from_user=User.from_json(d.get("from", d.get("user"))),
            date=_when(d, "date"),
            raw=d,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatUpdated(_Model):
    """A group or channel changed: what a ``chat_updated`` update carries.

    Handlers registered with `Router.on_chat_updated` get it. Its ``raw`` is the JSON of the
    whole update.

    Attributes:
        chat: The room as it is now.
        from_user: Who changed it, or ``None`` when the update does not say (a channel that
            hides its admins hides them from a bot that isn't one).
        date: When, in UTC, or ``None`` when the update does not say.
        changed: What moved, by the names of `Bot.update_chat` in the API's spelling
            (``"title"``, ``"slowModeSeconds"``...), or ``"photo"``, ``"banner"``,
            ``"pinnedMessageIds"``, ``"commentsEnabled"``.

    Example:
        Keep a copy of the pins::

            @bot.on_chat_updated(f.changed("pinnedMessageIds"))
            async def pins(update):
                pinned[update.chat.id] = update.chat.pinned_message_ids
    """

    chat: Chat
    from_user: User | None = None
    date: datetime | None = None
    changed: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class Update(_Model):
    """One thing that happened, as the server delivers it.

    `kind` says what happened, and so which of the other fields is set: `message` for a new or
    an edited message or comment, `callback_query` for a button press, `deleted` for messages
    deleted for everyone, `reaction` for a reaction, `chat_member` for a change of the bot's own
    seat, `member_update` for somebody else's, `join_request` for a request to join and
    `chat_updated` for a change of the room. The others are ``None``.

    Handlers get the event, not the update: a `Message`, a `CallbackQuery`, a `DeletedMessages`,
    a `ReactionUpdate`, a `ChatMemberUpdate`, a `JoinRequest` or a `ChatUpdated`. A handler
    registered with `Router.on_update`, and any handler with an ``update`` parameter, gets the
    `Update` itself. Its events are data only: a `MessageBase` or a `CallbackQueryBase`, without
    the methods. A kind that a newer server adds still parses: such an update has no event, and
    its JSON is in ``raw``. So do ``web_bot_write_access`` and ``web_bot_contact``, the two a
    bot's Web-Bot causes; a `Router.on_update` handler gets them.

    Attributes:
        update_id: The update's ID. It grows by one with each update the bot gets.
        kind: What happened.
        message: For a ``message`` or ``comment`` update, the new message; for an
            ``edited_message`` or ``edited_comment`` update, the message as it is after the
            edit.
        callback_query: For a ``callback_query`` update, the button press.
        deleted: For a ``deleted_messages`` update, the messages that were deleted.
        chat_member: For a ``my_chat_member`` update, the change of the bot's seat.
        reaction: For a ``message_reaction`` update, the reaction.
        member_update: For a ``chat_member`` update, the change of somebody else's seat.
        join_request: For a ``join_request`` update, the request.
        chat_updated: For a ``chat_updated`` update, the room as it is now and what moved.

    Example:
        >>> update = Update.from_json({
        ...     "updateId": 412,
        ...     "kind": "deleted_messages",
        ...     "chat": {"id": "48213907", "kind": "private"},
        ...     "messageIds": [1834, 1835],
        ... })
        >>> update.kind
        <UpdateKind.DELETED_MESSAGES: 'deleted_messages'>
        >>> update.deleted.message_ids, update.chat.id, update.message
        ((1834, 1835), '48213907', None)
    """

    update_id: int
    kind: UpdateKind
    message: MessageBase | None = None
    callback_query: CallbackQueryBase | None = None
    deleted: DeletedMessages | None = None
    chat_member: ChatMemberUpdate | None = None
    reaction: ReactionUpdate | None = None
    member_update: ChatMemberUpdate | None = None
    join_request: JoinRequest | None = None
    chat_updated: ChatUpdated | None = None

    @property
    def chat(self) -> ChatRef | None:
        """The chat the update is about, whatever its kind.

        ``None`` only for a kind this version does not know.
        """
        event = self.event
        chat = getattr(event, "chat", None)
        return chat if isinstance(chat, ChatRef) else None

    @property
    def from_user(self) -> User | None:
        """Who caused the update, when it says.

        The sender of a message, the person who pressed a button or reacted, who changed a
        seat or the room, or who asked to join. ``None`` for deleted messages, for a channel
        post in the channel's name, for a change that does not say who, and for a kind this
        version does not know.
        """
        sender = getattr(self.event, "from_user", None)
        return sender if isinstance(sender, User) else None

    @property
    def event(self) -> Any:
        """The event the update carries, whichever field holds it.

        A `MessageBase`, a `CallbackQueryBase`, a `DeletedMessages`, a `ReactionUpdate`, a
        `ChatMemberUpdate`, a `JoinRequest` or a `ChatUpdated`, as `kind` says. ``None`` for a
        kind this version does not know.
        """
        for event in (
            self.message,
            self.callback_query,
            self.deleted,
            self.chat_member,
            self.reaction,
            self.member_update,
            self.join_request,
            self.chat_updated,
        ):
            if event is not None:
                return event
        return None

    @classmethod
    def from_json(cls, data: Any) -> Update:
        """Builds an update, its event included, from the API's JSON.

        The bot does this for every update it fetches or is sent. The event is built as plain
        data (a `MessageBase`, a `CallbackQueryBase`, ...): attached to no bot, and not
        decrypted.

        Args:
            data: One update, as ``GET /updates`` lists them or a webhook posts it.

        Returns:
            The update, with ``data`` kept in ``raw``. A missing ``kind`` is read as
            ``"unknown"``, and an update of a kind this version does not know has no event.

        Raises:
            PayloadError: ``data`` is not an object; its ``updateId`` is missing or not an
                integer; or the event its ``kind`` calls for is missing or not valid (see
                `MessageBase.from_json`, `CallbackQueryBase.from_json`, `ChatRef.from_json`
                and `Seat.from_json`).
        """
        d = _obj(data, "update")
        update_id = _req_int(d, "updateId", "update")
        kind = UpdateKind(_str(d, "kind") or "unknown")
        values: dict[str, Any] = {}
        if kind in (UpdateKind.MESSAGE, UpdateKind.EDITED_MESSAGE, UpdateKind.COMMENT, UpdateKind.EDITED_COMMENT):
            values["message"] = MessageBase.from_json(d.get("message"))
        elif kind == UpdateKind.CALLBACK_QUERY:
            values["callback_query"] = CallbackQueryBase.from_json(d.get("callback"))
        elif kind == UpdateKind.DELETED_MESSAGES:
            values["deleted"] = DeletedMessages(
                chat=ChatRef.from_json(d.get("chat")), message_ids=_ints(d, "messageIds"), raw=d
            )
        elif kind == UpdateKind.MESSAGE_REACTION:
            values["reaction"] = ReactionUpdate(
                chat=ChatRef.from_json(d.get("chat")),
                message_id=_req_int(d, "messageId", "update"),
                thread_root=_int(d, "threadRoot"),
                from_user=_sender(d),
                date=_when(d, "date"),
                old=_str(d, "old"),
                new=_str(d, "new"),
                reactions=tuple(ReactionCount.from_json(r) for r in _list(d, "reactions") if isinstance(r, Mapping)),
                raw=d,
            )
        elif kind in (UpdateKind.MY_CHAT_MEMBER, UpdateKind.CHAT_MEMBER):
            mine = kind == UpdateKind.MY_CHAT_MEMBER
            change = ChatMemberUpdate(
                chat=_room_or_ref(d.get("chat")) if mine else ChatRef.from_json(d.get("chat")),
                from_user=_sender(d),
                date=_when(d, "date"),
                old=Seat.from_json(d.get("old")),
                new=Seat.from_json(d.get("new")),
                user=None if mine else User.from_json(d.get("user")),
                raw=d,
            )
            values["chat_member" if mine else "member_update"] = change
        elif kind == UpdateKind.JOIN_REQUEST:
            values["join_request"] = JoinRequest.from_json(d)
        elif kind == UpdateKind.CHAT_UPDATED:
            values["chat_updated"] = ChatUpdated(
                chat=Chat.from_json(d.get("chat")),
                from_user=_sender(d),
                date=_when(d, "date"),
                changed=tuple(c for c in _list(d, "changed") if isinstance(c, str)),
                raw=d,
            )
        return cls(update_id=update_id, kind=kind, **values, raw=d)


def _sender(d: Mapping[str, Any]) -> User | None:
    sender = d.get("from")
    return User.from_json(sender) if isinstance(sender, Mapping) else None


def _room_or_ref(data: Any) -> ChatRef:
    # a group or channel comes whole, with its mode; a private chat stays the short reference
    d = _obj(data, "chat")
    return Chat.from_json(d) if "mode" in d else ChatRef.from_json(d)


# --- what the newer operations answer ---


@dataclass(frozen=True, slots=True, kw_only=True)
class Reaction(_Model):
    """One reaction on a message, as `Bot.set_reaction` lists them: `MessageReactions.reactions`.

    Attributes:
        emoji: The emoji.
        mine: ``True`` for the bot's own reaction.
    """

    emoji: str
    mine: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class MessageReactions(_Model):
    """The reactions on a message after the bot reacted: what `Bot.set_reaction` returns.

    Attributes:
        message_id: The message.
        reactions: Every reaction on it, one item per person who reacted.
    """

    message_id: int
    reactions: tuple[Reaction, ...] = ()

    @property
    def counts(self) -> dict[str, int]:
        """How many people reacted with each emoji."""
        counts: dict[str, int] = {}
        for reaction in self.reactions:
            counts[reaction.emoji] = counts.get(reaction.emoji, 0) + 1
        return counts

    @classmethod
    def from_json(cls, data: Any) -> MessageReactions:
        """Builds the reactions from the answer of ``PUT /messages/{id}/reaction``.

        Args:
            data: The answer, with ``messageId`` and ``reactions``.

        Returns:
            The reactions, with ``data`` kept in ``raw``.

        Raises:
            PayloadError: ``data`` is not an object, or its ``messageId`` is missing.
        """
        d = _obj(data, "reactions")
        items = [r for r in _list(d, "reactions") if isinstance(r, Mapping)]
        return cls(
            message_id=_req_int(d, "messageId", "reactions"),
            reactions=tuple(Reaction(emoji=_str(r, "emoji") or "", mine=_bool(r, "mine"), raw=r) for r in items),
            raw=d,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class UploadedFile(_Model):
    """A file the bot uploaded, ready to go into a message: what `Bot.upload_file` returns.

    Attributes:
        id: The file's ID, for the ``media`` of a send, or for the envelope of an encrypted
            message.
        url: Where it is, ``/media/<id>`` on the API server.
        size: How many bytes the server stored.
    """

    id: str
    url: str | None = None
    size: int | None = None

    @classmethod
    def from_json(cls, data: Any) -> UploadedFile:
        """Builds the file from the answer of ``POST /attachments``.

        Args:
            data: The answer, with ``id``, ``url`` and ``size``.

        Returns:
            The file, with ``data`` kept in ``raw``.

        Raises:
            PayloadError: ``data`` is not an object, or its ``id`` is missing.
        """
        d = _obj(data, "attachment")
        file_id = _str(d, "id")
        if not file_id:
            raise PayloadError("attachment.id: missing")
        return cls(id=file_id, url=_str(d, "url"), size=_int(d, "size"), raw=d)


@dataclass(frozen=True, slots=True, kw_only=True)
class Picture(_Model):
    """A chat's new picture or banner: what `Bot.set_chat_photo` and `Bot.set_chat_banner` return.

    Attributes:
        url: Where it is, ``/media/<id>`` on the API server.
        mime: What the server found it to be, such as ``"image/png"`` or ``"video/mp4"``.
    """

    url: str
    mime: str | None = None

    @classmethod
    def from_json(cls, data: Any) -> Picture:
        """Builds the picture from the answer of ``PUT /chats/{id}/photo`` or ``/banner``.

        Args:
            data: The answer, with ``url`` and ``mime``.

        Returns:
            The picture, with ``data`` kept in ``raw``. A missing ``url`` is ``""``.

        Raises:
            PayloadError: ``data`` is not an object.
        """
        d = _obj(data, "picture")
        return cls(url=_str(d, "url") or "", mime=_str(d, "mime"), raw=d)


@dataclass(frozen=True, slots=True, kw_only=True)
class Invite(_Model):
    """An invite link of a group or channel: `Bot.get_invites` lists them, `Bot.create_invite` makes one.

    Attributes:
        code: The link's code, what `Bot.revoke_invite` takes.
        url: The link to share, or ``None`` when the server does not know its public address.
        label: The name it was given, or ``None``.
        expires_at: When it stops working, in UTC, or ``None`` for never.
        max_uses: How many people may use it, or ``None`` for no limit.
        uses: How many people used it.
        needs_approval: ``True`` when whoever uses it asks first, and waits for an admin or a
            bot to let them in.
        created_at: When it was made, in UTC, or ``None``.
    """

    code: str
    url: str | None = None
    label: str | None = None
    expires_at: datetime | None = None
    max_uses: int | None = None
    uses: int = 0
    needs_approval: bool = False
    created_at: datetime | None = None

    @classmethod
    def from_json(cls, data: Any) -> Invite:
        """Builds an invite link from the API's JSON.

        Args:
            data: One invite object.

        Returns:
            The link, with ``data`` kept in ``raw``.

        Raises:
            PayloadError: ``data`` is not an object, or its ``code`` is missing.
        """
        d = _obj(data, "invite")
        code = _str(d, "code")
        if not code:
            raise PayloadError("invite.code: missing")
        return cls(
            code=code,
            url=_str(d, "url"),
            label=_str(d, "label"),
            expires_at=_when(d, "expiresAt"),
            max_uses=_int(d, "maxUses"),
            uses=_int(d, "uses") or 0,
            needs_approval=_bool(d, "needsApproval"),
            created_at=_when(d, "createdAt"),
            raw=d,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class Ban(_Model):
    """Somebody banned from a group or channel: `Bot.get_bans` lists them.

    Attributes:
        user: Who is banned.
        banned_until: When the ban ends, in UTC, or ``None`` for good.
        banned_at: When they were banned, in UTC, or ``None``.
    """

    user: User
    banned_until: datetime | None = None
    banned_at: datetime | None = None

    @classmethod
    def from_json(cls, data: Any) -> Ban:
        """Builds a ban from the API's JSON (``user``, ``bannedUntil``, ``bannedAt``).

        Args:
            data: One ban object.

        Returns:
            The ban, with ``data`` kept in ``raw``.

        Raises:
            PayloadError: ``data`` is not an object, or its ``user`` is missing or not valid.
        """
        d = _obj(data, "ban")
        return cls(
            user=User.from_json(d.get("user")),
            banned_until=_when(d, "bannedUntil"),
            banned_at=_when(d, "bannedAt"),
            raw=d,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class Profile(User):
    """Somebody's public profile, as anybody in a chat with them sees it: `Bot.get_user`.

    It has every field of `User`, and more. Never a phone number. Somebody who blocked the bot
    keeps their pictures to themselves: the URLs are ``None`` then.

    Attributes:
        usernames: The other handles they answer to.
        bio: The text under their name, or ``None``.
        avatar_url: Their picture, ``/media/<id>`` on the API server, or ``None``.
        avatar_mime: What kind of file the picture is, or ``None``.
        banner_url: Their banner, or ``None``.
        banner_mime: What kind of file the banner is, or ``None``.
        has_key: ``True`` when they published an OpenPGP key.
        fingerprint: The fingerprint of that key, or ``None``.
    """

    usernames: tuple[str, ...] = ()
    bio: str | None = None
    avatar_url: str | None = None
    avatar_mime: str | None = None
    banner_url: str | None = None
    banner_mime: str | None = None
    has_key: bool = False
    fingerprint: str | None = None

    @classmethod
    def from_json(cls, data: Any) -> Profile:
        """Builds a profile from the answer of ``GET /users/{id}``.

        Args:
            data: The answer.

        Returns:
            The profile, with ``data`` kept in ``raw``.

        Raises:
            PayloadError: ``data`` is not an object, or its ``id`` is missing.
        """
        d = _obj(data, "profile")
        return cls(
            id=_req_id(d, "id", "profile"),
            username=_str(d, "username"),
            first_name=_str(d, "firstName"),
            last_name=_str(d, "lastName"),
            is_bot=_bool(d, "isBot"),
            usernames=tuple(u for u in _list(d, "usernames") if isinstance(u, str) and u),
            bio=_str(d, "bio"),
            avatar_url=_str(d, "avatarUrl"),
            avatar_mime=_str(d, "avatarMime"),
            banner_url=_str(d, "bannerUrl"),
            banner_mime=_str(d, "bannerMime"),
            has_key=_bool(d, "hasKey"),
            fingerprint=_str(d, "fingerprint"),
            raw=d,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class StatsWindow(_Model):
    """Subscribers who came and went over the last few days: an item of `ChannelStats.windows`.

    Attributes:
        days: How many days the window covers.
        joined: How many subscribed.
        left: How many left.
    """

    days: int
    joined: int = 0
    left: int = 0


@dataclass(frozen=True, slots=True, kw_only=True)
class GrowthPoint(_Model):
    """One step of a channel's growth: an item of `ChannelStats.growth`.

    Attributes:
        at: The start of the step, in UTC, or ``None``.
        joined: How many subscribed in it.
        left: How many left in it.
    """

    at: datetime | None = None
    joined: int = 0
    left: int = 0


@dataclass(frozen=True, slots=True, kw_only=True)
class PostViews(_Model):
    """A post and how many times it was seen: an item of `ChannelStats.top_posts`.

    Attributes:
        id: The post's ID. Only the ID: the numbers never hand over a post's words.
        views: How many times it was seen.
        date: When it was posted, in UTC, or ``None``.
    """

    id: int
    views: int = 0
    date: datetime | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ChannelStats(_Model):
    """A channel's numbers, for its admins: what `Bot.get_channel_stats` returns.

    Attributes:
        subscribers: How many subscribers it has now.
        windows: Who came and went over the last 7 and 30 days.
        growth: Who came and went step by step, over 30 days.
        posts: How many recent posts the views are counted over.
        total_views: The views of those posts together.
        average_views: The views of one of them, on average.
        top_posts: The most seen of them, best first.
    """

    subscribers: int = 0
    windows: tuple[StatsWindow, ...] = ()
    growth: tuple[GrowthPoint, ...] = ()
    posts: int = 0
    total_views: int = 0
    average_views: int = 0
    top_posts: tuple[PostViews, ...] = ()

    @classmethod
    def from_json(cls, data: Any) -> ChannelStats:
        """Builds the numbers from the answer of ``GET /chats/{id}/stats``.

        Args:
            data: The answer.

        Returns:
            The numbers, with ``data`` kept in ``raw``. A number that is missing is ``0``.

        Raises:
            PayloadError: ``data`` is not an object.
        """
        d = _obj(data, "stats")
        return cls(
            subscribers=_int(d, "subscribers") or 0,
            windows=tuple(
                StatsWindow(days=_int(w, "days") or 0, joined=_int(w, "joined") or 0, left=_int(w, "left") or 0, raw=w)
                for w in _objects(d, "windows")
            ),
            growth=tuple(
                GrowthPoint(at=_when(g, "at"), joined=_int(g, "joined") or 0, left=_int(g, "left") or 0, raw=g)
                for g in _objects(d, "growth")
            ),
            posts=_int(d, "posts") or 0,
            total_views=_int(d, "totalViews") or 0,
            average_views=_int(d, "averageViews") or 0,
            top_posts=tuple(
                PostViews(id=_int(p, "id") or 0, views=_int(p, "views") or 0, date=_when(p, "date"), raw=p)
                for p in _objects(d, "topPosts")
            ),
            raw=d,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class Comments(_Model):
    """One page of the comments under a channel post: what `Bot.get_comments` returns.

    Attributes:
        open: ``True`` when the post takes comments right now.
        messages: The comments the bot was handed, newest first. `Bot` hands them over
            decrypted and ready to answer; the low-level clients as they came.
    """

    open: bool = False
    messages: tuple[MessageBase, ...] = ()

    @classmethod
    def from_json(cls, data: Any) -> Comments:
        """Builds the page from the answer of ``GET /chats/{id}/posts/{postId}/comments``.

        Args:
            data: The answer, with ``open`` and ``messages``.

        Returns:
            The page, with ``data`` kept in ``raw``.

        Raises:
            PayloadError: ``data`` is not an object, or one of its messages is not valid.
        """
        d = _obj(data, "comments")
        return cls(
            open=_bool(d, "open"),
            messages=tuple(MessageBase.from_json(m) for m in _list(d, "messages")),
            raw=d,
        )


# --- the acting flavours ---
#
# Same fields as the bases, plus methods that go through the bot that received
# them. Bot hands out the async ones, SyncBot the sync ones; parity between the
# two is checked by the test suite.


def _need(bot: Any) -> Any:
    if bot is None:
        raise RuntimeError("this object is not bound to a bot: it was not handed out by Bot or SyncBot")
    return bot


@dataclass(frozen=True, slots=True, kw_only=True)
class Message(MessageBase):
    """A received message whose methods use the `Bot` that got it."""

    @classmethod
    def bind(cls, message: MessageBase, bot: Bot) -> Message:
        """Copies a plain `MessageBase` into one that acts through ``bot``.

        The bot does this to every message before a handler sees it. You need it yourself only
        for a message you built or read back from somewhere, in a test for example.

        Args:
            message: The message as the API gave it.
            bot: The bot its methods will go through.

        Returns:
            The same fields, as a `Message` bound to ``bot``.
        """
        return _rebind(cls, message, bot)

    async def reply(
        self,
        text: str,
        *,
        markup: InlineKeyboard | None = None,
        quote: bool = True,
        silent: bool = False,
        encrypt: bool | None = None,
        send_id: str | None = None,
    ) -> SentMessage:
        """Answers in the same chat, quoting this message unless ``quote=False``.

        A send like any other: `Bot.send_message` does the work, so the answer is sealed and
        signed when the chat is end-to-end encrypted, and the same errors come out of it. The
        answer to a comment is a comment under the same post (`Bot.send_comment`), which takes
        no buttons, no ``silent`` and no ``send_id``.

        Args:
            text: The answer, with the rules of `Bot.send_message`.
            markup: Buttons under the answer. Never encrypted.
            quote: ``False`` writes into the chat without quoting this message.
            silent: ``True`` posts without a notification. It only matters in channels.
            encrypt: ``None`` follows the chat; ``True`` refuses to write in clear, ``False``
                refuses to write sealed.
            send_id: A UUID that makes this send safe to repeat. One is made for you.

        Returns:
            The `SentMessage` as the server stored it, which can be edited or deleted.

        Raises:
            ValidationError: Buttons on the answer to a comment.
            RuntimeError: The message was not handed out by a `Bot`, so it has nothing to
                answer through.
        """
        bot = _need(self._bot)
        if self.thread_root is not None:
            if markup is not None:
                raise ValidationError("a comment carries no buttons", field="markup")
            comment: SentMessage = await bot.send_comment(
                self.chat.id, self.thread_root, text, reply_to=self.id if quote else None, encrypt=encrypt
            )
            return comment
        sent: SentMessage = await bot.send_message(
            self.chat.id,
            text,
            reply_to=self.id if quote else None,
            markup=markup,
            silent=silent,
            encrypt=encrypt,
            send_id=send_id,
        )
        return sent

    async def answer(
        self,
        text: str,
        *,
        markup: InlineKeyboard | None = None,
        silent: bool = False,
        encrypt: bool | None = None,
        send_id: str | None = None,
    ) -> SentMessage:
        """Writes into the same chat without quoting: `reply` with ``quote=False``.

        Args:
            text: The message, with the rules of `Bot.send_message`.
            markup: Buttons under it. Never encrypted.
            silent: ``True`` posts without a notification. It only matters in channels.
            encrypt: ``None`` follows the chat; ``True`` refuses to write in clear, ``False``
                refuses to write sealed.
            send_id: A UUID that makes this send safe to repeat. One is made for you.

        Returns:
            The `SentMessage` as the server stored it.

        Raises:
            RuntimeError: The message was not handed out by a `Bot`.
        """
        return await self.reply(text, markup=markup, quote=False, silent=silent, encrypt=encrypt, send_id=send_id)

    async def delete(self) -> bool:
        """Deletes this message for everyone.

        Anybody's in a private chat; in a group or channel, it needs the *delete messages*
        right (a comment, the *manage comments* right).

        Returns:
            ``True`` once the message is gone.

        Raises:
            ForbiddenError: The bot may not delete other people's messages here.
            RuntimeError: The message was not handed out by a `Bot`.
        """
        return bool(await _need(self._bot).delete_message(self.chat.id, self.id))

    async def typing(self, typing: bool = True) -> bool:
        """Shows "typing..." in this chat for a few seconds.

        Args:
            typing: ``False`` clears the indicator early.

        Returns:
            ``True`` if the indicator was shown, ``False`` if one went out to this chat less
            than 3 seconds ago.

        Raises:
            RuntimeError: The message was not handed out by a `Bot`.
        """
        return bool(await _need(self._bot).send_typing(self.chat.id, typing))

    async def react(self, emoji: str) -> MessageReactions:
        """Puts the bot's reaction on this message, in place of the one it had: `Bot.set_reaction`.

        Args:
            emoji: The emoji, or a custom emoji as the app writes it.

        Returns:
            Every reaction on the message now.

        Raises:
            ForbiddenError: Reactions are off here, the chat does not take this emoji, or the
                bot may not react.
            RuntimeError: The message was not handed out by a `Bot`.
        """
        reactions: MessageReactions = await _need(self._bot).set_reaction(self.chat.id, self.id, emoji)
        return reactions

    async def remove_reaction(self) -> bool:
        """Takes the bot's reaction off this message: `Bot.remove_reaction`.

        Returns:
            ``True`` when there was one, ``False`` when the bot had none there.

        Raises:
            RuntimeError: The message was not handed out by a `Bot`.
        """
        return bool(await _need(self._bot).remove_reaction(self.chat.id, self.id))

    async def pin(self) -> list[int]:
        """Pins this message for everybody in the chat: `Bot.pin_message`.

        Returns:
            The IDs of the messages pinned in the chat now.

        Raises:
            ForbiddenError: The bot may not pin messages here.
            RuntimeError: The message was not handed out by a `Bot`.
        """
        pinned: list[int] = await _need(self._bot).pin_message(self.chat.id, self.id)
        return pinned

    async def unpin(self) -> list[int]:
        """Unpins this message: `Bot.unpin_message`.

        Returns:
            The IDs of the messages still pinned in the chat.

        Raises:
            ForbiddenError: The bot may not unpin messages here.
            RuntimeError: The message was not handed out by a `Bot`.
        """
        pinned: list[int] = await _need(self._bot).unpin_message(self.chat.id, self.id)
        return pinned

    async def forward_to(self, chat: Any, *, hide_sender: bool = False, silent: bool = False) -> SentMessage:
        """Sends this message on into another chat the bot is in: `Bot.forward_message`.

        In an encrypted chat on either side the bot opens the message and seals it again for
        the other chat, files included.

        Args:
            chat: Where to: a chat ID, or a chat object such as ``message.chat``.
            hide_sender: ``True`` sends a plain copy, without "forwarded from" over it.
            silent: ``True`` posts without a notification. It only matters in channels.

        Returns:
            The `SentMessage` the forward became.

        Raises:
            ValidationError: The message is a comment, or its chat protects its content.
            RuntimeError: The message was not handed out by a `Bot`.
        """
        sent: SentMessage = await _need(self._bot).forward_message(
            chat, self.chat.id, self, hide_sender=hide_sender, silent=silent
        )
        return sent


@dataclass(frozen=True, slots=True, kw_only=True)
class SyncMessage(MessageBase):
    """A received message whose methods use the `SyncBot` that got it."""

    @classmethod
    def bind(cls, message: MessageBase, bot: SyncBot) -> SyncMessage:
        """Copies a plain `MessageBase` into one that acts through ``bot``.

        The bot does this to every message before a handler sees it. You need it yourself only
        for a message you built or read back from somewhere, in a test for example.

        Args:
            message: The message as the API gave it.
            bot: The bot its methods will go through.

        Returns:
            The same fields, as a `SyncMessage` bound to ``bot``.
        """
        return _rebind(cls, message, bot)

    def reply(
        self,
        text: str,
        *,
        markup: InlineKeyboard | None = None,
        quote: bool = True,
        silent: bool = False,
        encrypt: bool | None = None,
        send_id: str | None = None,
    ) -> SyncSentMessage:
        """Answers in the same chat, quoting this message unless ``quote=False``.

        A send like any other: `SyncBot.send_message` does the work, so the answer is sealed
        and signed when the chat is end-to-end encrypted, and the same errors come out of it.
        The answer to a comment is a comment under the same post (`SyncBot.send_comment`),
        which takes no buttons, no ``silent`` and no ``send_id``.

        Args:
            text: The answer, with the rules of `SyncBot.send_message`.
            markup: Buttons under the answer. Never encrypted.
            quote: ``False`` writes into the chat without quoting this message.
            silent: ``True`` posts without a notification. It only matters in channels.
            encrypt: ``None`` follows the chat; ``True`` refuses to write in clear, ``False``
                refuses to write sealed.
            send_id: A UUID that makes this send safe to repeat. One is made for you.

        Returns:
            The `SyncSentMessage` as the server stored it, which can be edited or deleted.

        Raises:
            ValidationError: Buttons on the answer to a comment.
            RuntimeError: The message was not handed out by a `SyncBot`, so it has nothing to
                answer through.
        """
        bot = _need(self._bot)
        if self.thread_root is not None:
            if markup is not None:
                raise ValidationError("a comment carries no buttons", field="markup")
            return bot.send_comment(  # type: ignore[no-any-return]
                self.chat.id, self.thread_root, text, reply_to=self.id if quote else None, encrypt=encrypt
            )
        return bot.send_message(  # type: ignore[no-any-return]
            self.chat.id,
            text,
            reply_to=self.id if quote else None,
            markup=markup,
            silent=silent,
            encrypt=encrypt,
            send_id=send_id,
        )

    def answer(
        self,
        text: str,
        *,
        markup: InlineKeyboard | None = None,
        silent: bool = False,
        encrypt: bool | None = None,
        send_id: str | None = None,
    ) -> SyncSentMessage:
        """Writes into the same chat without quoting: `reply` with ``quote=False``.

        Args:
            text: The message, with the rules of `SyncBot.send_message`.
            markup: Buttons under it. Never encrypted.
            silent: ``True`` posts without a notification. It only matters in channels.
            encrypt: ``None`` follows the chat; ``True`` refuses to write in clear, ``False``
                refuses to write sealed.
            send_id: A UUID that makes this send safe to repeat. One is made for you.

        Returns:
            The `SyncSentMessage` as the server stored it.

        Raises:
            RuntimeError: The message was not handed out by a `SyncBot`.
        """
        return self.reply(text, markup=markup, quote=False, silent=silent, encrypt=encrypt, send_id=send_id)

    def delete(self) -> bool:
        """Deletes this message for everyone.

        Anybody's in a private chat; in a group or channel, it needs the *delete messages*
        right (a comment, the *manage comments* right).

        Returns:
            ``True`` once the message is gone.

        Raises:
            ForbiddenError: The bot may not delete other people's messages here.
            RuntimeError: The message was not handed out by a `SyncBot`.
        """
        return bool(_need(self._bot).delete_message(self.chat.id, self.id))

    def typing(self, typing: bool = True) -> bool:
        """Shows "typing..." in this chat for a few seconds.

        Args:
            typing: ``False`` clears the indicator early.

        Returns:
            ``True`` if the indicator was shown, ``False`` if one went out to this chat less
            than 3 seconds ago.

        Raises:
            RuntimeError: The message was not handed out by a `SyncBot`.
        """
        return bool(_need(self._bot).send_typing(self.chat.id, typing))

    def react(self, emoji: str) -> MessageReactions:
        """Puts the bot's reaction on this message, in place of the one it had: `SyncBot.set_reaction`.

        Args:
            emoji: The emoji, or a custom emoji as the app writes it.

        Returns:
            Every reaction on the message now.

        Raises:
            ForbiddenError: Reactions are off here, the chat does not take this emoji, or the
                bot may not react.
            RuntimeError: The message was not handed out by a `SyncBot`.
        """
        return _need(self._bot).set_reaction(self.chat.id, self.id, emoji)  # type: ignore[no-any-return]

    def remove_reaction(self) -> bool:
        """Takes the bot's reaction off this message: `SyncBot.remove_reaction`.

        Returns:
            ``True`` when there was one, ``False`` when the bot had none there.

        Raises:
            RuntimeError: The message was not handed out by a `SyncBot`.
        """
        return bool(_need(self._bot).remove_reaction(self.chat.id, self.id))

    def pin(self) -> list[int]:
        """Pins this message for everybody in the chat: `SyncBot.pin_message`.

        Returns:
            The IDs of the messages pinned in the chat now.

        Raises:
            ForbiddenError: The bot may not pin messages here.
            RuntimeError: The message was not handed out by a `SyncBot`.
        """
        return _need(self._bot).pin_message(self.chat.id, self.id)  # type: ignore[no-any-return]

    def unpin(self) -> list[int]:
        """Unpins this message: `SyncBot.unpin_message`.

        Returns:
            The IDs of the messages still pinned in the chat.

        Raises:
            ForbiddenError: The bot may not unpin messages here.
            RuntimeError: The message was not handed out by a `SyncBot`.
        """
        return _need(self._bot).unpin_message(self.chat.id, self.id)  # type: ignore[no-any-return]

    def forward_to(self, chat: Any, *, hide_sender: bool = False, silent: bool = False) -> SyncSentMessage:
        """Sends this message on into another chat the bot is in: `SyncBot.forward_message`.

        In an encrypted chat on either side the bot opens the message and seals it again for
        the other chat, files included.

        Args:
            chat: Where to: a chat ID, or a chat object such as ``message.chat``.
            hide_sender: ``True`` sends a plain copy, without "forwarded from" over it.
            silent: ``True`` posts without a notification. It only matters in channels.

        Returns:
            The `SyncSentMessage` the forward became.

        Raises:
            ValidationError: The message is a comment, or its chat protects its content.
            RuntimeError: The message was not handed out by a `SyncBot`.
        """
        return _need(self._bot).forward_message(  # type: ignore[no-any-return]
            chat, self.chat.id, self, hide_sender=hide_sender, silent=silent
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class SentMessage(SentMessageBase):
    """One of the bot's messages, with `edit()` and `delete()`."""

    @classmethod
    def bind(cls, message: SentMessageBase, bot: Bot) -> SentMessage:
        """Copies a plain `SentMessageBase` into one that acts through ``bot``.

        The methods that send do this to what they return, so you rarely call it yourself.

        Args:
            message: The message as the API gave it back.
            bot: The bot its methods will go through.

        Returns:
            The same fields, as a `SentMessage` bound to ``bot``.
        """
        return _rebind(cls, message, bot)

    async def edit(
        self,
        text: str | None = None,
        *,
        markup: InlineKeyboard | None = KEEP,
        encrypt: bool | None = None,
    ) -> SentMessage:
        """New words, new buttons, or both: `Bot.edit_message` on this message.

        Args:
            text: The new text, with the rules of `Bot.send_message`. ``None`` keeps it.
            markup: The buttons. :data:`KEEP`, the default, keeps them; ``None`` removes them;
                a keyboard replaces them. Never encrypted.
            encrypt: ``None`` follows the chat; ``True`` refuses to write in clear, ``False``
                refuses to write sealed.

        Returns:
            The edited message, as the server stored it.

        Raises:
            ValidationError: There is nothing to change: no ``text``, and ``markup`` left at
                :data:`KEEP`.
            RuntimeError: The message did not come from a `Bot`.
        """
        return await _need(self._bot).edit_message(  # type: ignore[no-any-return]
            self.chat.id, self.id, text, markup=markup, encrypt=encrypt
        )

    async def delete(self) -> bool:
        """Deletes this message for everyone. A bot may always delete its own.

        Returns:
            ``True`` once the message is gone.

        Raises:
            MessageNotFoundError: It was deleted already.
            RuntimeError: The message did not come from a `Bot`.
        """
        return bool(await _need(self._bot).delete_message(self.chat.id, self.id))

    async def pin(self) -> list[int]:
        """Pins this message for everybody in the chat: `Bot.pin_message`.

        Returns:
            The IDs of the messages pinned in the chat now.

        Raises:
            ForbiddenError: The bot may not pin messages here.
            RuntimeError: The message did not come from a `Bot`.
        """
        pinned: list[int] = await _need(self._bot).pin_message(self.chat.id, self.id)
        return pinned

    async def unpin(self) -> list[int]:
        """Unpins this message: `Bot.unpin_message`.

        Returns:
            The IDs of the messages still pinned in the chat.

        Raises:
            ForbiddenError: The bot may not unpin messages here.
            RuntimeError: The message did not come from a `Bot`.
        """
        pinned: list[int] = await _need(self._bot).unpin_message(self.chat.id, self.id)
        return pinned


@dataclass(frozen=True, slots=True, kw_only=True)
class SyncSentMessage(SentMessageBase):
    """One of the bot's messages, with `edit()` and `delete()` (sync)."""

    @classmethod
    def bind(cls, message: SentMessageBase, bot: SyncBot) -> SyncSentMessage:
        """Copies a plain `SentMessageBase` into one that acts through ``bot``.

        The methods that send do this to what they return, so you rarely call it yourself.

        Args:
            message: The message as the API gave it back.
            bot: The bot its methods will go through.

        Returns:
            The same fields, as a `SyncSentMessage` bound to ``bot``.
        """
        return _rebind(cls, message, bot)

    def edit(
        self,
        text: str | None = None,
        *,
        markup: InlineKeyboard | None = KEEP,
        encrypt: bool | None = None,
    ) -> SyncSentMessage:
        """New words, new buttons, or both: `SyncBot.edit_message` on this message.

        Args:
            text: The new text, with the rules of `SyncBot.send_message`. ``None`` keeps it.
            markup: The buttons. :data:`KEEP`, the default, keeps them; ``None`` removes them;
                a keyboard replaces them. Never encrypted.
            encrypt: ``None`` follows the chat; ``True`` refuses to write in clear, ``False``
                refuses to write sealed.

        Returns:
            The edited message, as the server stored it.

        Raises:
            ValidationError: There is nothing to change: no ``text``, and ``markup`` left at
                :data:`KEEP`.
            RuntimeError: The message did not come from a `SyncBot`.
        """
        return _need(self._bot).edit_message(  # type: ignore[no-any-return]
            self.chat.id, self.id, text, markup=markup, encrypt=encrypt
        )

    def delete(self) -> bool:
        """Deletes this message for everyone. A bot may always delete its own.

        Returns:
            ``True`` once the message is gone.

        Raises:
            MessageNotFoundError: It was deleted already.
            RuntimeError: The message did not come from a `SyncBot`.
        """
        return bool(_need(self._bot).delete_message(self.chat.id, self.id))

    def pin(self) -> list[int]:
        """Pins this message for everybody in the chat: `SyncBot.pin_message`.

        Returns:
            The IDs of the messages pinned in the chat now.

        Raises:
            ForbiddenError: The bot may not pin messages here.
            RuntimeError: The message did not come from a `SyncBot`.
        """
        return _need(self._bot).pin_message(self.chat.id, self.id)  # type: ignore[no-any-return]

    def unpin(self) -> list[int]:
        """Unpins this message: `SyncBot.unpin_message`.

        Returns:
            The IDs of the messages still pinned in the chat.

        Raises:
            ForbiddenError: The bot may not unpin messages here.
            RuntimeError: The message did not come from a `SyncBot`.
        """
        return _need(self._bot).unpin_message(self.chat.id, self.id)  # type: ignore[no-any-return]


@dataclass(frozen=True, slots=True, kw_only=True)
class CallbackQuery(CallbackQueryBase):
    """A button press, with `answer()` and helpers to change the message."""

    @classmethod
    def bind(cls, query: CallbackQueryBase, bot: Bot) -> CallbackQuery:
        """Copies a plain `CallbackQueryBase` into one that acts through ``bot``.

        The bot does this to every press before a handler sees it.

        Args:
            query: The press as the API gave it.
            bot: The bot its methods will go through.

        Returns:
            The same fields, as a `CallbackQuery` bound to ``bot``.
        """
        return _rebind(cls, query, bot)

    async def answer(self, text: str | None = None, *, alert: bool = False, url: str | None = None) -> bool:
        """Stops the spinner. ``text`` flashes a line, ``alert=True`` shows a box, ``url`` offers a page.

        Once per press, within 60 seconds of it. The bot keeps no spinner turning: a press no
        handler answered gets an empty answer when the update is done.

        Args:
            text: Up to 200 characters on one line. ``None`` shows nothing.
            alert: ``True`` shows ``text`` in a box the person has to close.
            url: An ``http`` or ``https`` address the phone offers to open.

        Returns:
            ``True``. Answering twice raises instead.

        Raises:
            CallbackExpiredError: More than 60 seconds have passed since the press.
            CallbackAnsweredError: This press was answered already.
            RuntimeError: The press was not handed out by a `Bot`.

        Warning:
            Answers are not encrypted, even in an encrypted chat.
        """
        return bool(await _need(self._bot).answer_callback(self.id, text, alert=alert, url=url))

    async def edit_message(
        self,
        text: str | None = None,
        *,
        markup: InlineKeyboard | None = KEEP,
        encrypt: bool | None = None,
    ) -> SentMessage:
        """Edits the message the pressed button sits under.

        Args:
            text: The new text, with the rules of `Bot.send_message`. ``None`` keeps it.
            markup: The buttons. :data:`KEEP` keeps them, ``None`` removes them, a keyboard
                replaces them. Never encrypted.
            encrypt: ``None`` follows the chat; ``True`` refuses to write in clear, ``False``
                refuses to write sealed.

        Returns:
            The edited message, as the server stored it.

        Raises:
            ForbiddenError: The buttons are under somebody else's message, so the bot may not
                edit it.
            RuntimeError: The press was not handed out by a `Bot`.
        """
        return await _need(self._bot).edit_message(  # type: ignore[no-any-return]
            self.chat.id, self.message_id, text, markup=markup, encrypt=encrypt
        )

    async def delete_message(self) -> bool:
        """Deletes the message the pressed button sits under.

        Returns:
            ``True`` once the message is gone.

        Raises:
            ForbiddenError: The message is not the bot's own and it has no right to delete it.
            RuntimeError: The press was not handed out by a `Bot`.
        """
        return bool(await _need(self._bot).delete_message(self.chat.id, self.message_id))

    async def send(
        self,
        text: str,
        *,
        markup: InlineKeyboard | None = None,
        encrypt: bool | None = None,
    ) -> SentMessage:
        """Sends a new message into the chat the button was pressed in.

        Args:
            text: The message, with the rules of `Bot.send_message`.
            markup: Buttons under it. Never encrypted.
            encrypt: ``None`` follows the chat; ``True`` refuses to write in clear, ``False``
                refuses to write sealed.

        Returns:
            The `SentMessage` as the server stored it.

        Raises:
            RuntimeError: The press was not handed out by a `Bot`.
        """
        return await _need(self._bot).send_message(  # type: ignore[no-any-return]
            self.chat.id, text, markup=markup, encrypt=encrypt
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class SyncCallbackQuery(CallbackQueryBase):
    """A button press, with `answer()` and helpers to change the message (sync)."""

    @classmethod
    def bind(cls, query: CallbackQueryBase, bot: SyncBot) -> SyncCallbackQuery:
        """Copies a plain `CallbackQueryBase` into one that acts through ``bot``.

        The bot does this to every press before a handler sees it.

        Args:
            query: The press as the API gave it.
            bot: The bot its methods will go through.

        Returns:
            The same fields, as a `SyncCallbackQuery` bound to ``bot``.
        """
        return _rebind(cls, query, bot)

    def answer(self, text: str | None = None, *, alert: bool = False, url: str | None = None) -> bool:
        """Stops the spinner. ``text`` flashes a line, ``alert=True`` shows a box, ``url`` offers a page.

        Once per press, within 60 seconds of it. The bot keeps no spinner turning: a press no
        handler answered gets an empty answer when the update is done.

        Args:
            text: Up to 200 characters on one line. ``None`` shows nothing.
            alert: ``True`` shows ``text`` in a box the person has to close.
            url: An ``http`` or ``https`` address the phone offers to open.

        Returns:
            ``True``. Answering twice raises instead.

        Raises:
            CallbackExpiredError: More than 60 seconds have passed since the press.
            CallbackAnsweredError: This press was answered already.
            RuntimeError: The press was not handed out by a `SyncBot`.

        Warning:
            Answers are not encrypted, even in an encrypted chat.
        """
        return bool(_need(self._bot).answer_callback(self.id, text, alert=alert, url=url))

    def edit_message(
        self,
        text: str | None = None,
        *,
        markup: InlineKeyboard | None = KEEP,
        encrypt: bool | None = None,
    ) -> SyncSentMessage:
        """Edits the message the pressed button sits under.

        Args:
            text: The new text, with the rules of `SyncBot.send_message`. ``None`` keeps it.
            markup: The buttons. :data:`KEEP` keeps them, ``None`` removes them, a keyboard
                replaces them. Never encrypted.
            encrypt: ``None`` follows the chat; ``True`` refuses to write in clear, ``False``
                refuses to write sealed.

        Returns:
            The edited message, as the server stored it.

        Raises:
            ForbiddenError: The buttons are under somebody else's message, so the bot may not
                edit it.
            RuntimeError: The press was not handed out by a `SyncBot`.
        """
        return _need(self._bot).edit_message(  # type: ignore[no-any-return]
            self.chat.id, self.message_id, text, markup=markup, encrypt=encrypt
        )

    def delete_message(self) -> bool:
        """Deletes the message the pressed button sits under.

        Returns:
            ``True`` once the message is gone.

        Raises:
            ForbiddenError: The message is not the bot's own and it has no right to delete it.
            RuntimeError: The press was not handed out by a `SyncBot`.
        """
        return bool(_need(self._bot).delete_message(self.chat.id, self.message_id))

    def send(
        self,
        text: str,
        *,
        markup: InlineKeyboard | None = None,
        encrypt: bool | None = None,
    ) -> SyncSentMessage:
        """Sends a new message into the chat the button was pressed in.

        Args:
            text: The message, with the rules of `SyncBot.send_message`.
            markup: Buttons under it. Never encrypted.
            encrypt: ``None`` follows the chat; ``True`` refuses to write in clear, ``False``
                refuses to write sealed.

        Returns:
            The `SyncSentMessage` as the server stored it.

        Raises:
            RuntimeError: The press was not handed out by a `SyncBot`.
        """
        return _need(self._bot).send_message(  # type: ignore[no-any-return]
            self.chat.id, text, markup=markup, encrypt=encrypt
        )


def updates_from_json(items: Sequence[Any]) -> list[Update]:
    return [Update.from_json(item) for item in items]

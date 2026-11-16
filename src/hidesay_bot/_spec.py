"""Every operation of `openapi.json`, described once.

Each function checks its arguments the way the server would and returns a
`Call`; `hidesay_bot.client` sends it. The names
follow the operationIds (`getMe` -> `get_me()`).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any

from . import _validate as v
from ._http import Call
from .errors import PayloadError, ValidationError
from .keyboard import InlineKeyboard, markup_payload
from .models import (
    KEEP,
    Ban,
    BotInfo,
    ChannelStats,
    Chat,
    ChatKeys,
    ChatKind,
    ChatMembers,
    ChatRef,
    Command,
    CommandScope,
    Comments,
    Invite,
    JoinRequest,
    MediaIn,
    Member,
    MessageBase,
    MessageReactions,
    Picture,
    Profile,
    PublishedKey,
    Resolved,
    SentMessageBase,
    Sticker,
    Update,
    UploadedFile,
    UserKey,
    WebhookInfo,
)

# what an upload may take on the way up and while the server seals it at rest
UPLOAD_WRITE_TIMEOUT = 300.0
UPLOAD_READ_TIMEOUT = 120.0


def _flag(result: Any, key: str) -> bool:
    if not isinstance(result, Mapping):
        raise PayloadError(f"expected an object with {key!r}, got {type(result).__name__}")
    return result.get(key) is True


def _updates(result: Any) -> list[Update]:
    if not isinstance(result, list):
        raise PayloadError("updates: expected a list")
    return [Update.from_json(item) for item in result]


def _raw_updates(result: Any) -> list[dict[str, Any]]:
    if not isinstance(result, list):
        raise PayloadError("updates: expected a list")
    return [item for item in result if isinstance(item, dict)]


def _scopes(result: Any) -> list[CommandScope]:
    if not isinstance(result, list):
        raise PayloadError("commands: expected a list")
    return [CommandScope.from_json(item) for item in result]


def _commands(result: Any) -> list[Command]:
    if not isinstance(result, list):
        raise PayloadError("commands: expected a list")
    return [Command.from_json(item) for item in result]


def _field(result: Any, key: str) -> Any:
    if not isinstance(result, Mapping):
        raise PayloadError(f"expected an object with {key!r}, got {type(result).__name__}")
    return result.get(key)


def _items(result: Any, key: str) -> list[Any]:
    items = _field(result, key)
    if not isinstance(items, list):
        raise PayloadError(f"{key}: expected a list")
    return items


def _ids(result: Any, key: str) -> list[int]:
    return [i for i in _items(result, key) if isinstance(i, int) and not isinstance(i, bool)]


def _kind_of(chat_id: str) -> ChatKind:
    # the letter says it: G a group, C a channel, bare digits the private chat with a person
    return {"G": ChatKind.GROUP, "C": ChatKind.CHANNEL}.get(chat_id[:1], ChatKind.PRIVATE)


def _room(chat_id: str | int, what: str) -> str:
    """A group or a channel: the server answers bad_request for a private chat."""
    clean = v.check_chat_id(chat_id)
    if _kind_of(clean) == ChatKind.PRIVATE:
        raise ValidationError(f"{what} is for groups and channels, not private chats", field="chat_id")
    return clean


def _channel(chat_id: str | int, what: str) -> str:
    clean = v.check_chat_id(chat_id)
    if _kind_of(clean) != ChatKind.CHANNEL:
        raise ValidationError(f"{what} is a channel's: give a channel's id (C48213907)", field="chat_id")
    return clean


# --- Me ---


def get_me() -> Call[BotInfo]:
    return Call("GET", "/me", BotInfo.from_json)


# --- Updates ---


def get_updates(
    offset: int | None = None, limit: int | None = None, timeout: int | None = None, *, raw: bool = False
) -> Call[Any]:
    params = v.check_poll(offset, limit, timeout)
    # a long poll must outlive its own timeout on our side, or we hang up first
    read = float(timeout) + 15.0 if timeout else None
    # not repeatable here: the poller decides what to do with a lost poll
    return Call(
        "GET", "/updates", _raw_updates if raw else _updates, params=params, read_timeout=read, repeatable=False
    )


def get_webhook() -> Call[WebhookInfo]:
    return Call("GET", "/webhook", WebhookInfo.from_json)


def set_webhook(url: str, secret: str | None = None) -> Call[WebhookInfo]:
    body: dict[str, Any] = {"url": v.check_webhook_url(url)}
    if secret is not None:
        body["secret"] = v.check_secret(secret)
    return Call("POST", "/webhook", WebhookInfo.from_json, body=body, has_body=True)


def delete_webhook() -> Call[WebhookInfo]:
    return Call("DELETE", "/webhook", WebhookInfo.from_json)


# --- Key ---


def set_key(public_key: str, fingerprint: str) -> Call[PublishedKey]:
    body = {"publicKey": v.check_public_key(public_key), "fingerprint": v.check_fingerprint(fingerprint)}
    return Call("PUT", "/key", PublishedKey.from_json, body=body, has_body=True)


# --- Commands ---


def get_commands(scope: str | None = None) -> Call[Any]:
    if scope is None:
        return Call("GET", "/commands", _scopes)
    return Call("GET", "/commands", _commands, params={"scope": v.check_scope(scope)})


def _command_list(commands: Iterable[Command | Mapping[str, Any] | tuple[str, str]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    items = list(commands)
    if len(items) > v.COMMANDS_PER_SCOPE:
        raise ValidationError(f"at most {v.COMMANDS_PER_SCOPE} commands per scope", field="commands")
    for item in items:
        given: object = item
        name: object
        description: object
        if isinstance(given, Command):
            name, description = given.command, given.description
        elif isinstance(given, Mapping):
            name, description = given.get("command"), given.get("description")
        elif isinstance(given, tuple) and len(given) == 2:
            name, description = given
        else:
            raise ValidationError(
                f"a command is a Command, a dict or a (name, description) pair, got {item!r}", field="commands"
            )
        clean = v.clean_command(name)
        text = v.clean_command_description(description, clean)
        # the server folds duplicates to the first one; doing it here keeps the echo identical
        if clean in seen:
            continue
        seen.add(clean)
        out.append({"command": clean, "description": text})
    return out


def set_commands(
    commands: Iterable[Command | Mapping[str, Any] | tuple[str, str]], scope: str | None = None
) -> Call[CommandScope]:
    body: dict[str, Any] = {"commands": _command_list(commands)}
    if scope is not None:
        body["scope"] = v.check_scope(scope)
    return Call("PUT", "/commands", CommandScope.from_json, body=body, has_body=True)


def delete_commands(scope: str | None = None) -> Call[CommandScope]:
    params = {"scope": v.check_scope(scope)} if scope is not None else None
    return Call("DELETE", "/commands", CommandScope.from_json, params=params)


# --- Messages ---


def _body(text: str | None, armored: str | None, *, required: bool, text_max: int) -> dict[str, str]:
    if text is not None and armored is not None:
        raise ValidationError("send either text or armored, not both")
    if text is not None:
        return {"text": v.check_text(text, limit=text_max)}
    if armored is not None:
        return {"armored": v.check_armored(armored)}
    if required:
        raise ValidationError("text or armored is required", field="text")
    return {}


MediaLike = MediaIn | Mapping[str, Any]
StickerLike = int | Sticker | Mapping[str, Any]


def _media_items(media: MediaLike | Iterable[MediaLike]) -> list[dict[str, Any]]:
    given: list[Any] = [media] if isinstance(media, (MediaIn, Mapping)) else list(media)
    items = []
    for item in given:
        if isinstance(item, MediaIn):
            items.append(item.to_json())
        elif isinstance(item, Mapping):
            items.append(dict(item))
        else:
            raise ValidationError(f"a file to send is a MediaIn or a dict, got {type(item).__name__}", field="media")
    return v.check_media_list(items)


def _sticker_ref(sticker: StickerLike, media_id: str | None = None) -> dict[str, Any]:
    proof: object = media_id
    if isinstance(sticker, Sticker):
        sticker_id: object = sticker.id
        proof = proof or sticker.media_id or None
    elif isinstance(sticker, Mapping):
        sticker_id, proof = sticker.get("id"), proof or sticker.get("mediaId")
    else:
        sticker_id = sticker
    ref: dict[str, Any] = {"id": v.check_sticker_id(sticker_id)}
    if proof is not None:
        if not isinstance(proof, str) or not proof or len(proof) > 64:
            raise ValidationError("media_id must be the file id a message carried for the sticker", field="media_id")
        ref["mediaId"] = proof
    return ref


def send_message(
    chat_id: str | int,
    text: str | None = None,
    *,
    armored: str | None = None,
    reply_to: int | None = None,
    markup: InlineKeyboard | Mapping[str, Any] | None = None,
    silent: bool | None = None,
    send_id: str | uuid.UUID | None = None,
    media: MediaLike | Iterable[MediaLike] | None = None,
    sticker: StickerLike | None = None,
    mentions: Iterable[str | int] | None = None,
    chat_kind: ChatKind | None = None,
    text_max: int = v.TEXT_MAX,
) -> Call[SentMessageBase]:
    chat_id = v.check_chat_id(chat_id)
    body: dict[str, Any] = {"chatId": chat_id}
    if media is not None or sticker is not None:
        # files and stickers are the server's to wrap, so readable chats only: in an
        # encrypted one they travel inside the armor
        if armored is not None:
            raise ValidationError("media and sticker go with text, never with armored", field="armored")
        if media is not None and sticker is not None:
            raise ValidationError("send either media or a sticker, not both", field="sticker")
        if sticker is not None:
            if text is not None:
                raise ValidationError("a sticker has no text", field="text")
            body["sticker"] = _sticker_ref(sticker)
        else:
            assert media is not None
            body["media"] = _media_items(media)
            caption = v.check_caption(text, limit=text_max)
            if caption is not None:
                body["text"] = caption
    else:
        body.update(_body(text, armored, required=True, text_max=text_max))
    if reply_to is not None:
        body["replyTo"] = v.check_id(reply_to, "reply_to")
    payload = markup_payload(markup)
    if payload is not None:
        body["markup"] = payload
    if silent is not None:
        body["silent"] = bool(silent)
    if mentions is not None:
        tagged = v.check_mentions(mentions)
        if tagged:
            body["mentions"] = tagged
    # always one: it is what makes a retry after a timeout write nothing twice
    body["sendId"] = v.check_send_id(send_id) if send_id is not None else str(uuid.uuid4())
    return Call(
        "POST",
        "/messages",
        SentMessageBase.from_json,
        body=body,
        has_body=True,
        send=True,
        chat_id=chat_id,
        chat_kind=chat_kind,
    )


def edit_message(
    chat_id: str | int,
    message_id: int,
    text: str | None = None,
    *,
    armored: str | None = None,
    markup: InlineKeyboard | Mapping[str, Any] | None = KEEP,
    chat_kind: ChatKind | None = None,
    text_max: int = v.TEXT_MAX,
) -> Call[SentMessageBase]:
    message_id = v.check_id(message_id, "message_id")
    chat_id = v.check_chat_id(chat_id)
    body: dict[str, Any] = {"chatId": chat_id}
    body.update(_body(text, armored, required=False, text_max=text_max))
    if markup is not KEEP:
        # null is meaningful here: it takes the buttons away
        body["markup"] = markup_payload(markup)
    if len(body) == 1:
        raise ValidationError("nothing to change: give text, armored or markup")
    return Call(
        "PATCH",
        f"/messages/{message_id}",
        SentMessageBase.from_json,
        body=body,
        has_body=True,
        send=True,
        edit=True,
        chat_id=chat_id,
        chat_kind=chat_kind,
    )


def delete_message(chat_id: str | int, message_id: int) -> Call[bool]:
    message_id = v.check_id(message_id, "message_id")
    # chatId in the query: a body on DELETE is legal but some proxies drop it
    return Call(
        "DELETE",
        f"/messages/{message_id}",
        lambda r: _flag(r, "deleted"),
        params={"chatId": v.check_chat_id(chat_id)},
        done_codes=frozenset({"message_not_found"}),
        done_value=True,
    )


# --- Buttons ---


def answer_callback(
    callback_id: str, text: str | None = None, *, alert: bool = False, url: str | None = None
) -> Call[bool]:
    given: object = callback_id
    # "." and ".." would be read as path steps and send the call somewhere else
    if not isinstance(given, str) or not given or len(given) > 64 or given in (".", ".."):
        raise ValidationError("callback_id must be the id of a callback query", field="callback_id")
    clean, url = v.check_callback_answer(text, url)
    body: dict[str, Any] = {}
    if clean is not None:
        body["text"] = clean
    if alert:
        body["alert"] = True
    if url is not None:
        body["url"] = url
    return Call(
        "POST",
        f"/callbacks/{_path_segment(callback_id)}/answer",
        lambda r: _flag(r, "answered"),
        body=body,
        has_body=True,
        done_codes=frozenset({"callback_answered"}),
        done_value=True,
    )


def _path_segment(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")


# --- Chats ---


def get_chat(chat_id: str | int) -> Call[Chat]:
    return Call("GET", f"/chats/{v.check_chat_id(chat_id)}", Chat.from_json)


def get_chat_keys(chat_id: str | int) -> Call[ChatKeys]:
    return Call("GET", f"/chats/{v.check_chat_id(chat_id)}/keys", ChatKeys.from_json)


def get_chat_members(chat_id: str | int) -> Call[ChatMembers]:
    return Call("GET", f"/chats/{v.check_chat_id(chat_id)}/members", ChatMembers.from_json)


def get_chat_member(chat_id: str | int, user_id: str | int) -> Call[Member]:
    return Call(
        "GET",
        f"/chats/{v.check_chat_id(chat_id)}/members/{v.check_user_id(user_id)}",
        Member.from_json,
    )


def resolve(username: str) -> Call[Resolved]:
    return Call("GET", f"/resolve/{v.check_username(username)}", Resolved.from_json)


def leave_chat(chat_id: str | int) -> Call[bool]:
    return Call(
        "POST",
        f"/chats/{v.check_chat_id(chat_id)}/leave",
        lambda r: _flag(r, "left"),
        # gone from the chat: a repeat of a leave that worked finds no chat
        done_codes=frozenset({"chat_not_found"}),
        done_value=True,
    )


def send_typing(chat_id: str | int, typing: bool = True) -> Call[bool]:
    # a typing indicator three seconds late is worse than none: no retries
    return Call(
        "POST",
        f"/chats/{v.check_chat_id(chat_id)}/typing",
        lambda r: _flag(r, "sent"),
        body={"typing": bool(typing)},
        has_body=True,
        repeatable=False,
    )


def get_user_key(user_id: str | int) -> Call[UserKey]:
    return Call("GET", f"/users/{v.check_user_id(user_id)}/key", UserKey.from_json)


# --- Token ---


def _web_bot_key(result: Any) -> str:
    key = result.get("key") if isinstance(result, Mapping) else None
    if not isinstance(key, str) or not key:
        raise PayloadError("webbot/key: expected an object with a 'key' string")
    return key


def get_web_bot_key() -> Call[str]:
    return Call("GET", "/webbot/key", _web_bot_key)


def revoke_token() -> Call[bool]:
    return Call(
        "POST",
        "/token/revoke",
        lambda r: _flag(r, "revoked"),
        # the first one worked if the token is dead by the time we ask again
        done_codes=frozenset({"unauthorized"}),
        done_value=True,
    )


# --- more on messages ---


def delete_messages(chat_id: str | int, message_ids: Iterable[int]) -> Call[list[int]]:
    body = {"chatId": v.check_chat_id(chat_id), "ids": v.check_message_ids(message_ids)}
    # ids already gone are left out of the answer, not refused: a repeat is harmless
    return Call("POST", "/messages/delete", lambda r: _ids(r, "deleted"), body=body, has_body=True)


def forward_message(
    chat_id: str | int,
    from_chat_id: str | int,
    message_id: int,
    *,
    hide_sender: bool | None = None,
    silent: bool | None = None,
    send_id: str | uuid.UUID | None = None,
    chat_kind: ChatKind | None = None,
) -> Call[SentMessageBase]:
    target = v.check_chat_id(chat_id)
    body: dict[str, Any] = {
        "chatId": target,
        "fromChatId": v.check_chat_id(from_chat_id, "from_chat_id"),
        "messageId": v.check_id(message_id, "message_id"),
    }
    if hide_sender is not None:
        body["hideSender"] = bool(hide_sender)
    if silent is not None:
        body["silent"] = bool(silent)
    body["sendId"] = v.check_send_id(send_id) if send_id is not None else str(uuid.uuid4())
    return Call(
        "POST",
        "/messages/forward",
        SentMessageBase.from_json,
        body=body,
        has_body=True,
        send=True,
        chat_id=target,
        chat_kind=chat_kind or _kind_of(target),
    )


def set_reaction(chat_id: str | int, message_id: int, emoji: str) -> Call[MessageReactions]:
    message_id = v.check_id(message_id, "message_id")
    body = {"chatId": v.check_chat_id(chat_id), "emoji": v.check_emoji(emoji)}
    return Call("PUT", f"/messages/{message_id}/reaction", MessageReactions.from_json, body=body, has_body=True)


def remove_reaction(chat_id: str | int, message_id: int) -> Call[bool]:
    message_id = v.check_id(message_id, "message_id")
    return Call(
        "DELETE",
        f"/messages/{message_id}/reaction",
        lambda r: _flag(r, "removed"),
        params={"chatId": v.check_chat_id(chat_id)},
    )


# --- files and stickers ---


def _file(data: object, limit: int) -> dict[str, tuple[str, bytes, str]]:
    # the name and the type travel in clear even for a sealed file: they say nothing here,
    # the real ones go in the message
    return {"file": ("blob", v.check_upload(data, limit=limit), "application/octet-stream")}


def upload_file(data: bytes, *, limit: int = v.UPLOAD_MAX) -> Call[UploadedFile]:
    return Call(
        "POST",
        "/attachments",
        UploadedFile.from_json,
        files=_file(data, limit),
        read_timeout=UPLOAD_READ_TIMEOUT,
        write_timeout=UPLOAD_WRITE_TIMEOUT,
    )


def get_sticker(sticker_id: int, media_id: str | None = None) -> Call[Sticker]:
    ref = _sticker_ref(sticker_id, media_id)
    params = {"mediaId": ref["mediaId"]} if "mediaId" in ref else None
    return Call("GET", f"/stickers/{ref['id']}", Sticker.from_json, params=params)


# --- chats and history ---


def get_chats() -> Call[list[Chat]]:
    return Call("GET", "/chats", lambda r: [Chat.from_json(c) for c in _items(r, "chats")])


def get_history(chat_id: str | int, *, before: int | None = None, limit: int | None = None) -> Call[list[MessageBase]]:
    return Call(
        "GET",
        f"/chats/{v.check_chat_id(chat_id)}/messages",
        lambda r: [MessageBase.from_json(m) for m in _items(r, "messages")],
        params=v.check_page(before, limit) or None,
    )


def get_message(chat_id: str | int, message_id: int) -> Call[MessageBase]:
    message_id = v.check_id(message_id, "message_id")
    return Call("GET", f"/chats/{v.check_chat_id(chat_id)}/messages/{message_id}", MessageBase.from_json)


def _last_read(result: Any) -> int | None:
    value = _field(result, "lastReadId")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def read_chat(chat_id: str | int, up_to: int | None = None) -> Call[int | None]:
    body = {"upTo": v.check_id(up_to, "up_to")} if up_to is not None else {}
    return Call("POST", f"/chats/{v.check_chat_id(chat_id)}/read", _last_read, body=body, has_body=True)


def pin_message(chat_id: str | int, message_id: int) -> Call[list[int]]:
    message_id = v.check_id(message_id, "message_id")
    return Call("PUT", f"/chats/{v.check_chat_id(chat_id)}/pins/{message_id}", lambda r: _ids(r, "pinnedMessageIds"))


def unpin_message(chat_id: str | int, message_id: int) -> Call[list[int]]:
    message_id = v.check_id(message_id, "message_id")
    return Call("DELETE", f"/chats/{v.check_chat_id(chat_id)}/pins/{message_id}", lambda r: _ids(r, "pinnedMessageIds"))


def unpin_all(chat_id: str | int) -> Call[list[int]]:
    return Call(
        "DELETE", f"/chats/{_room(chat_id, 'unpinning everything')}/pins", lambda r: _ids(r, "pinnedMessageIds")
    )


def get_comments(
    chat_id: str | int, post_id: int, *, before: int | None = None, limit: int | None = None
) -> Call[Comments]:
    post_id = v.check_id(post_id, "post_id")
    return Call(
        "GET",
        f"/chats/{_channel(chat_id, 'comments')}/posts/{post_id}/comments",
        Comments.from_json,
        params=v.check_page(before, limit) or None,
    )


def send_comment(
    chat_id: str | int,
    post_id: int,
    text: str | None = None,
    *,
    armored: str | None = None,
    reply_to: int | None = None,
    as_author: bool | None = None,
    mentions: Iterable[str | int] | None = None,
    text_max: int = v.TEXT_MAX,
) -> Call[SentMessageBase]:
    channel_id = _channel(chat_id, "a comment")
    post_id = v.check_id(post_id, "post_id")
    body: dict[str, Any] = _body(text, armored, required=True, text_max=text_max)
    if reply_to is not None:
        body["replyTo"] = v.check_id(reply_to, "reply_to")
    if as_author is not None:
        body["asAuthor"] = bool(as_author)
    if mentions is not None:
        tagged = v.check_mentions(mentions)
        if tagged:
            body["mentions"] = tagged
    return Call(
        "POST",
        f"/chats/{channel_id}/posts/{post_id}/comments",
        SentMessageBase.from_json,
        body=body,
        has_body=True,
        # no sendId for comments: a repeat after a lost answer would write it twice
        repeatable=False,
        send=True,
        chat_id=channel_id,
        chat_kind=ChatKind.CHANNEL,
    )


def get_user(user_id: str | int) -> Call[Profile]:
    return Call("GET", f"/users/{v.check_user_id(user_id)}", Profile.from_json)


# --- running a room ---


def update_chat(
    chat_id: str | int,
    *,
    title: str | None = None,
    description: str | None = KEEP,
    permissions: Mapping[str, bool] | None = None,
    slow_mode_seconds: int | None = None,
    members_hidden: bool | None = None,
    protect_content: bool | None = None,
    reactions_enabled: bool | None = None,
    allowed_reactions: Iterable[str] | None = KEEP,
    auto_delete_seconds: int | None = None,
    signatures: bool | None = None,
    signature_profiles: bool | None = None,
    admins_hidden: bool | None = None,
) -> Call[Chat]:
    room = _room(chat_id, "changing settings")
    kind = _kind_of(room)
    body: dict[str, Any] = {}
    if title is not None:
        body["title"] = v.check_chat_title(title)
    if description is not KEEP:
        body["description"] = v.check_chat_description(description)
    if permissions is not None:
        body["permissions"] = v.check_flags(permissions, v.PERMISSIONS, "permissions")
    if slow_mode_seconds is not None:
        body["slowModeSeconds"] = v.check_seconds(slow_mode_seconds, "slow_mode_seconds", v.SLOW_MODE_MAX)
    for key, value in (
        ("membersHidden", members_hidden),
        ("protectContent", protect_content),
        ("reactionsEnabled", reactions_enabled),
        ("signatures", signatures),
        ("signatureProfiles", signature_profiles),
        ("adminsHidden", admins_hidden),
    ):
        if value is not None:
            body[key] = bool(value)
    if allowed_reactions is not KEEP:
        if allowed_reactions is None:
            body["allowedReactions"] = None
        else:
            emoji = [v.check_emoji(e) for e in allowed_reactions]
            if len(emoji) > v.ALLOWED_REACTIONS_MAX:
                raise ValidationError(f"at most {v.ALLOWED_REACTIONS_MAX} reactions", field="allowed_reactions")
            if any(v.utf16_len(e) > v.ALLOWED_REACTION_MAX for e in emoji):
                raise ValidationError(
                    f"a room's reaction is at most {v.ALLOWED_REACTION_MAX} characters", field="allowed_reactions"
                )
            body["allowedReactions"] = emoji
    if auto_delete_seconds is not None:
        body["autoDeleteSeconds"] = v.check_seconds(auto_delete_seconds, "auto_delete_seconds", v.AUTO_DELETE_MAX)
    if not body:
        raise ValidationError("nothing to change")
    wrong = (
        ("permissions", "slowModeSeconds")
        if kind == ChatKind.CHANNEL
        else ("signatures", "signatureProfiles", "adminsHidden")
    )
    for key in wrong:
        if key in body:
            raise ValidationError(f"{key} is not a setting of a {kind.value}", field=key)
    return Call("PATCH", f"/chats/{room}", Chat.from_json, body=body, has_body=True)


def update_member(
    chat_id: str | int,
    user_id: str | int,
    *,
    role: str | None = None,
    admin_rights: Iterable[str] | None = None,
    restrictions: Iterable[str] | None = None,
    restricted_until: datetime | int | None = KEEP,
    custom_title: str | None = KEEP,
    anonymous: bool | None = None,
) -> Call[Member]:
    room = _room(chat_id, "managing members")
    body: dict[str, Any] = {}
    if role is not None:
        if role not in ("admin", "member"):
            raise ValidationError(f"role must be 'admin' or 'member', got {role!r}", field="role")
        body["role"] = role
    if admin_rights is not None:
        body["adminRights"] = v.check_names(admin_rights, v.ADMIN_RIGHTS, "admin_rights")
    if restrictions is not None:
        body["restrictions"] = v.check_names(restrictions, v.PERMISSIONS, "restrictions")
    if restricted_until is not KEEP:
        if restrictions is None:
            # the server reads the end of a restriction only with the restriction itself
            raise ValidationError("restricted_until goes with restrictions", field="restricted_until")
        body["restrictedUntil"] = None if restricted_until is None else v.unix_ms(restricted_until, "restricted_until")
    if custom_title is not KEEP:
        body["customTitle"] = v.check_custom_title(custom_title)
    if anonymous is not None:
        body["anonymous"] = bool(anonymous)
    if not body:
        raise ValidationError("nothing to change")
    return Call(
        "PATCH", f"/chats/{room}/members/{v.check_user_id(user_id)}", Member.from_json, body=body, has_body=True
    )


def remove_member(
    chat_id: str | int, user_id: str | int, *, ban: bool = False, banned_until: datetime | int | None = None
) -> Call[bool]:
    room = _room(chat_id, "removing members")
    if banned_until is not None and not ban:
        raise ValidationError("banned_until goes with ban=True", field="banned_until")
    # the query is enough for a plain ban, a ban that ends needs the body
    params = {"ban": "true"} if ban else None
    body = {"ban": True, "bannedUntil": v.unix_ms(banned_until, "banned_until")} if banned_until is not None else None
    return Call(
        "DELETE",
        f"/chats/{room}/members/{v.check_user_id(user_id)}",
        lambda r: _flag(r, "banned"),
        params=params,
        body=body,
        has_body=body is not None,
        # gone on the first try: the repeat finds nobody to remove
        done_codes=frozenset({"member_not_found"}),
        done_value=bool(ban),
    )


def purge_member(chat_id: str | int, user_id: str | int, *, up_to: int | None = None) -> Call[bool]:
    room = _room(chat_id, "wiping messages")
    params = {"upTo": v.check_id(up_to, "up_to")} if up_to is not None else None
    return Call(
        "DELETE",
        f"/chats/{room}/members/{v.check_user_id(user_id)}/messages",
        lambda r: _flag(r, "purged"),
        params=params,
    )


def get_bans(chat_id: str | int) -> Call[list[Ban]]:
    return Call(
        "GET", f"/chats/{_room(chat_id, 'the ban list')}/bans", lambda r: [Ban.from_json(b) for b in _items(r, "bans")]
    )


def unban(chat_id: str | int, user_id: str | int) -> Call[bool]:
    room = _room(chat_id, "lifting a ban")
    return Call("DELETE", f"/chats/{room}/bans/{v.check_user_id(user_id)}", lambda r: _flag(r, "lifted"))


def get_invites(chat_id: str | int) -> Call[list[Invite]]:
    room = _room(chat_id, "invite links")
    return Call("GET", f"/chats/{room}/invites", lambda r: [Invite.from_json(i) for i in _items(r, "invites")])


def create_invite(
    chat_id: str | int,
    *,
    label: str | None = None,
    expires_at: datetime | int | None = None,
    max_uses: int | None = None,
    needs_approval: bool | None = None,
) -> Call[Invite]:
    room = _room(chat_id, "invite links")
    body: dict[str, Any] = {}
    given: object = label
    if given is not None:
        if not isinstance(given, str) or v.utf16_len(v.js_trim(given)) > v.INVITE_LABEL_MAX:
            raise ValidationError(f"label must be at most {v.INVITE_LABEL_MAX} characters", field="label")
        body["label"] = v.js_trim(given)
    if expires_at is not None:
        body["expiresAt"] = v.unix_ms(expires_at, "expires_at")
    uses: object = max_uses
    if uses is not None:
        if isinstance(uses, bool) or not isinstance(uses, int) or not 1 <= uses <= v.INVITE_USES_MAX:
            raise ValidationError(f"max_uses must be between 1 and {v.INVITE_USES_MAX}", field="max_uses")
        body["maxUses"] = uses
    if needs_approval is not None:
        body["needsApproval"] = bool(needs_approval)
    # each call makes a new link: a repeat after a lost answer would make two
    return Call("POST", f"/chats/{room}/invites", Invite.from_json, body=body, has_body=True, repeatable=False)


def revoke_invite(chat_id: str | int, code: str) -> Call[bool]:
    room = _room(chat_id, "invite links")
    return Call(
        "DELETE",
        f"/chats/{room}/invites/{_path_segment(v.check_invite_code(code))}",
        lambda r: _flag(r, "revoked"),
        done_codes=frozenset({"invite_not_found"}),
        done_value=True,
    )


def get_join_requests(chat_id: str | int) -> Call[list[JoinRequest]]:
    room = _room(chat_id, "join requests")
    where = ChatRef(id=room, kind=_kind_of(room))
    return Call(
        "GET", f"/chats/{room}/requests", lambda r: [JoinRequest.from_json(i, where) for i in _items(r, "requests")]
    )


def decide_join_request(chat_id: str | int, user_id: str | int, approve: bool) -> Call[bool]:
    room = _room(chat_id, "join requests")
    if not isinstance(approve, bool):
        raise ValidationError("approve must be True or False", field="approve")
    return Call(
        "POST",
        f"/chats/{room}/requests/{v.check_user_id(user_id)}",
        lambda r: _flag(r, "approved"),
        body={"approve": approve},
        has_body=True,
        # decided on the first try: the repeat finds the request gone
        done_codes=frozenset({"request_not_found"}),
        done_value=approve,
    )


def set_comments(
    chat_id: str | int, enabled: bool, *, signing: bool | None = None, outside: bool | None = None
) -> Call[Chat]:
    body: dict[str, Any] = {"enabled": bool(enabled)}
    if signing is not None:
        body["signing"] = bool(signing)
    if outside is not None:
        body["outside"] = bool(outside)
    return Call("PATCH", f"/chats/{_channel(chat_id, 'comments')}/comments", Chat.from_json, body=body, has_body=True)


def set_post_comments(chat_id: str | int, post_id: int, comments_off: bool) -> Call[bool]:
    post_id = v.check_id(post_id, "post_id")
    return Call(
        "PATCH",
        f"/chats/{_channel(chat_id, 'comments')}/posts/{post_id}",
        lambda r: _field(r, "commentsOff") is True,
        body={"commentsOff": bool(comments_off)},
        has_body=True,
    )


def get_channel_stats(chat_id: str | int) -> Call[ChannelStats]:
    return Call("GET", f"/chats/{_channel(chat_id, 'statistics')}/stats", ChannelStats.from_json)


def _picture(chat_id: str | int, slot: str, data: object, limit: int) -> Call[Picture]:
    # a second try would put the same picture up twice, one more in the album
    return Call(
        "PUT",
        f"/chats/{_room(chat_id, 'a chat picture')}/{slot}",
        Picture.from_json,
        files=_file(data, limit),
        repeatable=False,
        read_timeout=UPLOAD_READ_TIMEOUT,
        write_timeout=UPLOAD_WRITE_TIMEOUT,
    )


def _drop_picture(chat_id: str | int, slot: str) -> Call[bool]:
    # not repeated: a second delete takes the picture before it off too
    return Call(
        "DELETE", f"/chats/{_room(chat_id, 'a chat picture')}/{slot}", lambda r: _flag(r, "removed"), repeatable=False
    )


def set_chat_photo(chat_id: str | int, data: bytes, *, limit: int = v.UPLOAD_MAX) -> Call[Picture]:
    return _picture(chat_id, "photo", data, limit)


def delete_chat_photo(chat_id: str | int) -> Call[bool]:
    return _drop_picture(chat_id, "photo")


def set_chat_banner(chat_id: str | int, data: bytes, *, limit: int = v.UPLOAD_MAX) -> Call[Picture]:
    return _picture(chat_id, "banner", data, limit)


def delete_chat_banner(chat_id: str | int) -> Call[bool]:
    return _drop_picture(chat_id, "banner")


OPERATIONS: Sequence[str] = (
    "get_me",
    "get_updates",
    "get_webhook",
    "set_webhook",
    "delete_webhook",
    "set_key",
    "get_commands",
    "set_commands",
    "delete_commands",
    "send_message",
    "edit_message",
    "delete_message",
    "answer_callback",
    "get_chat",
    "get_chat_keys",
    "get_chat_members",
    "get_chat_member",
    "leave_chat",
    "send_typing",
    "resolve",
    "get_user_key",
    "revoke_token",
    "get_web_bot_key",
    "delete_messages",
    "forward_message",
    "set_reaction",
    "remove_reaction",
    "upload_file",
    "get_sticker",
    "get_chats",
    "get_history",
    "get_message",
    "read_chat",
    "unpin_all",
    "pin_message",
    "unpin_message",
    "get_comments",
    "send_comment",
    "get_user",
    "update_chat",
    "update_member",
    "remove_member",
    "purge_member",
    "get_bans",
    "unban",
    "get_invites",
    "create_invite",
    "revoke_invite",
    "get_join_requests",
    "decide_join_request",
    "set_comments",
    "set_post_comments",
    "get_channel_stats",
    "set_chat_photo",
    "delete_chat_photo",
    "set_chat_banner",
    "delete_chat_banner",
)

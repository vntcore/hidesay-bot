"""Reads and writes what a decrypted message body actually is.

Internal. Inside the armor the apps do not always write plain words. A photo, an album, a
sticker, a forward, a link card, a call notice or a payment is a marker
(``U+0001 pgpchat-<shape>-v1 U+0001``) followed by JSON: the app's ``MessageWire`` format. The
server reads the same shapes out of readable chats before it hands them to a bot; this module
does it for encrypted ones, with the same rules, so a handler sees ``text``, ``media``,
``sticker``, ``forward`` and ``link`` the same way in both kinds of chat.

It also writes those shapes, for what the bot seals into an encrypted chat itself: a file or
an album, a sticker, a forward. In a readable chat the server writes them.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .models import ChatKind, ChatRef, Forward, ForwardChat, LinkCard, Media, MessageBase, Sticker

__all__ = ["Body", "parse_body"]


def _mark(name: str) -> str:
    return f"\x01pgpchat-{name}-v1\x01"


FORWARD = _mark("fwd")
MEDIA = _mark("media")
ALBUM = _mark("album")
STICKER = _mark("sticker")
CALL = _mark("call")
LINK = _mark("link")
PAY = _mark("pay")

_MEDIA_ID_CHARS = frozenset("0123456789abcdefABCDEF-")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_BASE64 = re.compile(r"[A-Za-z0-9+/=]+")
THUMB_MAX = 12_000
# the numbers ids are drawn from; anything else in an envelope is somebody's word for nothing
_POOL = range(10_000_000, 100_000_000)
DEFAULT_MIME = {"image": "image/jpeg", "video": "video/mp4", "audio": "audio/ogg", "file": "application/octet-stream"}


@dataclass(frozen=True, slots=True)
class Body:
    """A decrypted body, understood.

    Attributes:
        text: The words: the plain text, or the caption of a photo or an album. ``None`` when
            there are none, as for a sticker, a call notice, a payment, or a body of white space.
        media: The attachments: one for ``"media"``, up to 10 for ``"album"``. An entry that
            can't be read is left out.
        shape: What the body is: ``"text"``, ``"media"``, ``"album"``, ``"sticker"``,
            ``"call"``, ``"pay"`` or ``"unknown"`` (a shape this version can't read). A forward
            or a link card has the shape of what it wraps.
        forwarded: ``True`` when the body came wrapped as a forward.
        has_link: ``True`` when the body came wrapped in a link card.
        sticker: The sticker, for a body that is one.
        forward: Who a forward came from, as its envelope says.
        link: The link card, when the body came wrapped in one.
    """

    text: str | None
    media: tuple[Media, ...] = ()
    shape: str = "text"
    forwarded: bool = False
    has_link: bool = False
    sticker: Sticker | None = None
    forward: Forward | None = None
    link: LinkCard | None = None


def _json_after(raw: str, prefix: str) -> Any:
    try:
        return json.loads(raw[len(prefix) :])
    except ValueError:
        return None


def media_of(env: Any) -> Media | None:
    if not isinstance(env, dict):
        return None
    media_id = env.get("media")
    if not isinstance(media_id, str) or len(media_id) != 36 or not set(media_id) <= _MEDIA_ID_CHARS:
        return None
    raw_kind = env.get("kind")
    kind = raw_kind if isinstance(raw_kind, str) and raw_kind in ("image", "video", "audio", "file") else "file"

    def _pos_int(key: str, top: int) -> int | None:
        value = env.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < top:
            return None
        return value

    duration = env.get("dur")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration <= 0:
        duration = None
    size = env.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        size = None
    mime = env.get("mime") if isinstance(env.get("mime"), str) else None
    name = env.get("name") if isinstance(env.get("name"), str) else None
    thumb = env.get("thumb")
    digest = env.get("sha256")
    sha256 = digest.lower() if isinstance(digest, str) else None
    return Media(
        id=media_id,
        kind=kind,
        mime=mime[:128] if mime else None,
        url=f"/media/{media_id}",
        width=_pos_int("w", 20000),
        height=_pos_int("h", 20000),
        duration_ms=round(duration) if duration is not None else None,
        name=name,
        size=size,
        # plain means it went up in the clear; everything else is sealed
        encrypted=env.get("plain") is not True,
        thumb=thumb if isinstance(thumb, str) and len(thumb) <= THUMB_MAX and _BASE64.fullmatch(thumb) else None,
        # anything but a clean hash is dropped: a check against garbage would only ever fail
        sha256=sha256 if sha256 and _SHA256.fullmatch(sha256) else None,
        raw=env,
    )


def _text(env: Mapping[str, Any], key: str, top: int) -> str | None:
    value = env.get(key)
    return value[:top] if isinstance(value, str) and value else None


def _pool_id(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value in _POOL else None


def _sticker_of(env: Any) -> Sticker | None:
    if not isinstance(env, dict):
        return None
    media_id = env.get("mediaId")
    if not isinstance(media_id, str) or len(media_id) != 36 or not set(media_id) <= _MEDIA_ID_CHARS:
        return None
    sticker_id, pack_id = env.get("id"), env.get("packId")
    return Sticker(
        id=sticker_id if isinstance(sticker_id, int) and not isinstance(sticker_id, bool) else None,
        pack_id=pack_id if isinstance(pack_id, int) and not isinstance(pack_id, bool) else None,
        media_id=media_id,
        emoji=_text(env, "emoji", 16),
        url=f"/media/{media_id}",
        raw=env,
    )


def _forward_of(origin: Any) -> Forward | None:
    if not isinstance(origin, dict):
        return None
    group = origin.get("group") if isinstance(origin.get("group"), dict) else None
    channel = _pool_id(origin.get("channelId"))
    chat: ForwardChat | None = None
    if channel is not None:
        chat = ForwardChat(
            id=f"C{channel}",
            title=_text(origin, "name", 128),
            username=_text(origin, "username", 32),
            message_id=_pool_int(origin.get("postId")),
            raw=origin,
        )
    elif group is not None:
        group_id = _pool_id(group.get("id"))
        chat = ForwardChat(
            id=f"G{group_id}" if group_id is not None else None,
            title=_text(group, "title", 128),
            username=_text(group, "username", 32),
            message_id=_pool_int(group.get("messageId")),
            raw=group,
        )
    person = _pool_id(origin.get("id"))
    # somebody who switched off the link to their profile keeps the name and loses the id
    linked = origin.get("linkable") is not False and channel is None
    return Forward(
        name=_text(origin, "name", 128),
        username=_text(origin, "username", 32),
        user_id=str(person) if linked and person is not None else None,
        chat=chat,
        raw=origin,
    )


def _pool_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _link_of(link: Any) -> LinkCard | None:
    if not isinstance(link, dict) or not isinstance(link.get("url"), str):
        return None
    return LinkCard(
        url=link["url"][:2048],
        title=_text(link, "title", 300),
        description=_text(link, "description", 1000),
        site_name=_text(link, "siteName", 120),
        raw=link,
    )


def parse_body(raw: str, *, _depth: int = 0) -> Body:
    """Reads one decrypted body. It never raises: a shape it can't read is ``"unknown"``.

    Args:
        raw: The decrypted text.
        _depth: How deep inside forwards and link cards this call is. Callers leave it at 0.

    Returns:
        The body, with its text, its media and its shape.
    """
    if _depth > 2:
        return Body(text=None, shape="unknown")

    if raw.startswith((FORWARD, LINK)):
        is_forward = raw.startswith(FORWARD)
        env = _json_after(raw, FORWARD if is_forward else LINK)
        if not isinstance(env, dict) or not isinstance(env.get("body"), str):
            return Body(text=None, shape="unknown")
        inner = parse_body(env["body"], _depth=_depth + 1)
        return Body(
            text=inner.text,
            media=inner.media,
            shape=inner.shape,
            forwarded=inner.forwarded or is_forward,
            has_link=inner.has_link or not is_forward,
            sticker=inner.sticker,
            forward=_forward_of(env.get("origin")) if is_forward else inner.forward,
            link=inner.link if is_forward else _link_of(env.get("link")),
        )

    if raw.startswith(MEDIA):
        env = _json_after(raw, MEDIA)
        media = media_of(env)
        caption = env.get("cap") if isinstance(env, dict) and isinstance(env.get("cap"), str) else None
        return Body(text=caption or None, media=(media,) if media else (), shape="media")

    if raw.startswith(ALBUM):
        env = _json_after(raw, ALBUM)
        items = env.get("items") if isinstance(env, dict) else None
        found = tuple(m for m in (media_of(i) for i in (items if isinstance(items, list) else [])[:10]) if m)
        caption = env.get("cap") if isinstance(env, dict) and isinstance(env.get("cap"), str) else None
        return Body(text=caption or None, media=found, shape="album")

    if raw.startswith(STICKER):
        return Body(text=None, shape="sticker", sticker=_sticker_of(_json_after(raw, STICKER)))
    if raw.startswith(CALL):
        return Body(text=None, shape="call")
    if raw.startswith(PAY):
        return Body(text=None, shape="pay")

    # any other control character up front is a shape a newer app writes
    if raw.startswith("\x01"):
        return Body(text=None, shape="unknown")

    return Body(text=raw if raw.strip() else None)


# --- writing ---


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def envelope(
    media_id: str,
    kind: str,
    *,
    mime: str | None = None,
    name: str | None = None,
    size: int | None = None,
    width: int | None = None,
    height: int | None = None,
    duration_ms: int | None = None,
    thumb: str | None = None,
    sha256: str | None = None,
    plain: bool = False,
) -> dict[str, Any]:
    """One file as the phones describe it, in their field names (``MessageWire.Envelope``)."""
    env: dict[str, Any] = {
        "media": media_id.lower(),
        "kind": kind,
        "mime": mime or DEFAULT_MIME.get(kind, DEFAULT_MIME["file"]),
        "name": name,
        "size": size or 0,
        "w": width,
        "h": height,
        "dur": duration_ms,
        "thumb": thumb,
        "sha256": sha256.lower() if sha256 else None,
    }
    # the phones write it only when true: a sealed blob says nothing
    if plain:
        env["plain"] = True
    return env


def media_body(envelopes: list[dict[str, Any]], caption: str | None) -> str:
    """One file with its caption, or an album of 2-10 pictures and clips sharing one."""
    cap = caption if caption and caption.strip() else None
    if len(envelopes) == 1:
        return MEDIA + _dump({**envelopes[0], "cap": cap})
    return ALBUM + _dump({"items": envelopes, "cap": cap})


def sticker_body(sticker: Sticker) -> str:
    return STICKER + _dump(
        {
            "id": sticker.id or 0,
            "packId": sticker.pack_id or 0,
            "mediaId": sticker.media_id,
            "emoji": sticker.emoji or "",
        }
    )


def forward_body(origin: Mapping[str, Any], body: str) -> str:
    return FORWARD + _dump({"origin": dict(origin), "body": body})


def link_body(body: str, link: LinkCard) -> str:
    card = {"url": link.url, "title": link.title, "description": link.description, "siteName": link.site_name}
    return LINK + _dump({"body": body, "link": card})


def _number(public_id: str | None) -> int | None:
    # the envelope keeps the bare number, the letter comes from the row on the server
    digits = (public_id or "").lstrip("BGC")
    return int(digits) if digits.isdigit() and int(digits) in _POOL else None


def origin_of(chat: ChatRef, message: MessageBase, *, linkable: bool) -> dict[str, Any]:
    """Who a forward says it came from, built the way the server builds it (``ForwardInfo``)."""
    if chat.kind == ChatKind.CHANNEL:
        return {
            "id": None,
            "name": chat.title or "",
            "username": chat.username,
            "linkable": True,
            "channelId": _number(chat.id),
            "postId": message.id,
        }
    sender = message.from_user
    name = " ".join(p for p in (sender.first_name, sender.last_name) if p) if sender else ""
    if not name and sender is not None and sender.username:
        name = f"@{sender.username}"
    origin: dict[str, Any] = {
        "id": _number(sender.id) if sender else None,
        "name": name,
        "username": sender.username if sender else None,
        "linkable": linkable,
    }
    if chat.kind == ChatKind.GROUP:
        origin["group"] = {
            "id": _number(chat.id),
            "title": chat.title or "",
            "username": chat.username,
            "messageId": message.id,
        }
    return origin


def origin_of_forward(forward: Forward) -> dict[str, Any]:
    """A forward's origin as the server handed it over, back in the envelope's shape."""
    origin: dict[str, Any] = {
        "id": _number(forward.user_id),
        "name": forward.name or "",
        "username": forward.username,
        "linkable": forward.user_id is not None,
    }
    chat = forward.chat
    if chat is not None and (chat.id or "").startswith("C"):
        origin.update(id=None, linkable=True, channelId=_number(chat.id), postId=chat.message_id)
    elif chat is not None and chat.id is not None:
        origin["group"] = {
            "id": _number(chat.id),
            "title": chat.title or "",
            "username": chat.username,
            "messageId": chat.message_id,
        }
    return origin


def body_of(message: MessageBase) -> str:
    """The body a message was sent with, as the phones wrote it.

    An encrypted message has it in ``plaintext``. A readable one was taken apart by the server,
    so it is put back together from ``text``, ``media``, ``sticker``, ``link`` and ``forward``.
    """
    if message.plaintext is not None:
        return message.plaintext
    if message.sticker is not None:
        inner = sticker_body(message.sticker)
    elif message.media:
        inner = media_body([envelope_of(m) for m in message.media], message.text)
    else:
        inner = message.text or ""
    if message.link is not None:
        inner = link_body(inner, message.link)
    if message.forward is not None:
        inner = forward_body(origin_of_forward(message.forward), inner)
    return inner


def envelope_of(media: Media) -> dict[str, Any]:
    return envelope(
        media.id,
        media.kind if media.kind in DEFAULT_MIME else "file",
        mime=media.mime,
        name=media.name,
        size=media.size,
        width=media.width,
        height=media.height,
        duration_ms=media.duration_ms,
        thumb=media.thumb,
        sha256=media.sha256,
        plain=not media.encrypted,
    )


def files_in(raw: str, _depth: int = 0) -> list[dict[str, Any]]:
    """The envelopes of the files a body points at, inside a forward or a link card too."""
    if _depth > 2:
        return []
    for mark in (FORWARD, LINK):
        if raw.startswith(mark):
            env = _json_after(raw, mark)
            return (
                files_in(env["body"], _depth + 1) if isinstance(env, dict) and isinstance(env.get("body"), str) else []
            )
    if raw.startswith(MEDIA):
        env = _json_after(raw, MEDIA)
        return [env] if isinstance(env, dict) and isinstance(env.get("media"), str) else []
    if raw.startswith(ALBUM):
        env = _json_after(raw, ALBUM)
        items = env.get("items") if isinstance(env, dict) else None
        return (
            [i for i in items if isinstance(i, dict) and isinstance(i.get("media"), str)]
            if isinstance(items, list)
            else []
        )
    return []


def remap_files(raw: str, moved: Mapping[str, Mapping[str, Any]], _depth: int = 0) -> str:
    """``raw`` with each file's envelope updated from ``moved`` (old id to the new fields)."""
    if _depth > 2:
        return raw
    for mark in (FORWARD, LINK):
        if raw.startswith(mark):
            env = _json_after(raw, mark)
            if not isinstance(env, dict) or not isinstance(env.get("body"), str):
                return raw
            return mark + _dump({**env, "body": remap_files(env["body"], moved, _depth + 1)})

    def swap(item: Any) -> Any:
        if not isinstance(item, dict):
            return item
        new = moved.get(str(item.get("media", "")).lower())
        if new is None:
            return item
        out = {k: v for k, v in item.items() if k != "plain"}
        out.update(new)
        return {k: v for k, v in out.items() if not (k == "plain" and v is not True)}

    if raw.startswith(MEDIA):
        env = _json_after(raw, MEDIA)
        return MEDIA + _dump(swap(env)) if isinstance(env, dict) else raw
    if raw.startswith(ALBUM):
        env = _json_after(raw, ALBUM)
        if not isinstance(env, dict) or not isinstance(env.get("items"), list):
            return raw
        return ALBUM + _dump({**env, "items": [swap(i) for i in env["items"]]})
    return raw

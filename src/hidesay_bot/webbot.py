r"""Checking who opened a bot's Web-Bot.

A Web-Bot is a web page the Hidesay app opens inside a bot's chat. The page gets
``initData`` from the app (``window.Hidesay.WebBot.initData``): who opened it,
from where, and when, signed by the Hidesay server. The page sends that string
to the bot's own server, which checks it with `validate_web_bot_data` before it
believes any of it. Anything the page says about the user without it, including
``initDataUnsafe``, could have been typed by anybody.

The string is form-encoded, like a URL query:

- ``user``: who opened the page, as JSON in the Bot API's shape (``id``,
  ``firstName``, ``lastName``, ``username``, ``languageCode``, ``photoUrl``,
  ``bannerUrl``, ``allowsWriteToPm``);
- ``chatType``: ``"private"`` from the chat with the bot, ``"link"`` from a link;
- ``startParam``: the word after ``?startapp=`` in the link, when there was one;
- ``authDate``: when the server signed it, in Unix seconds;
- ``botId``: the bot it was signed for;
- ``signature``: Ed25519, base64url without padding.

What is signed is ``"<botId>:WebBotData\n"`` followed by every other field as
``key=value``, sorted by key, one per line. The bot's ID at the front means a
string signed for one bot never passes for another. The key that checks it is
the same for every bot, and public: `~hidesay_bot.AsyncClient.get_web_bot_key`
fetches it, and it can be kept in the bot's configuration.
`~hidesay_bot.Bot.validate_web_bot_data` does the fetching and the keeping.
"""

from __future__ import annotations

import base64
import binascii
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any
from urllib.parse import parse_qsl

from . import _ed25519
from .errors import ConfigurationError, WebBotDataError

__all__ = ["WEB_BOT_DATA_MAX_AGE", "WebBotData", "WebBotUser", "parse_web_bot_data", "validate_web_bot_data"]

WEB_BOT_DATA_MAX_AGE = 24 * 60 * 60
"""How old ``initData`` may be by default, in seconds: one day.

A page left open longer than that sends data the check refuses; the user closing and
opening the Web-Bot again gets fresh data. Pass ``max_age`` to change it.
"""

_TAG = "WebBotData"
# the app never sends more; a bigger string is refused before it is parsed
_MAX_LENGTH = 16 * 1024


@dataclass(frozen=True, slots=True)
class WebBotUser:
    """The person who opened the Web-Bot, as the Hidesay server vouches for them.

    The same person the bot meets in its updates: `id` is the ID it sees in
    ``message.from_user.id``.

    Attributes:
        id: Their ID, eight digits as a string: ``"48213907"``.
        first_name: Their first name, or ``None``.
        last_name: Their last name, or ``None``.
        username: Their handle, without ``@``, or ``None`` if they have none.
        language_code: The language of their app, such as ``"it"`` or ``"en-GB"``, or ``None``.
        photo_url: Where their profile picture can be fetched, or ``None``.
        banner_url: Where their banner can be fetched, or ``None``.
        allows_write_to_pm: Whether the bot may write to them in private right now. Opened
            from a link, without a chat yet, it is ``False`` until the page asks with
            ``requestWriteAccess``.

    Example:
        >>> WebBotUser(id="48213907", first_name="Anna", last_name="Rossi").full_name
        'Anna Rossi'
    """

    id: str
    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None
    language_code: str | None = None
    photo_url: str | None = None
    banner_url: str | None = None
    allows_write_to_pm: bool = False

    @property
    def full_name(self) -> str:
        """The first and last name, joined by a space.

        Without a name, the handle with ``@``; without either, ``"user <id>"``.
        """
        name = " ".join(p for p in (self.first_name, self.last_name) if p)
        return name or (f"@{self.username}" if self.username else f"user {self.id}")


@dataclass(frozen=True, slots=True)
class WebBotData:
    """What a Web-Bot page was opened with, read out of ``initData``.

    Returned checked by `validate_web_bot_data`, or unchecked by `parse_web_bot_data`.

    Attributes:
        user: Who opened the page.
        bot_id: The bot it was signed for, such as ``"B48213907"``.
        chat_type: ``"private"`` when it was opened from the chat with the bot, ``"link"``
            when it was opened from a link anywhere else.
        auth_date: When the server signed it, in UTC.
        start_param: The word after ``?startapp=`` in the link that opened it, or ``None``.
        fields: Every field as it arrived, decoded, the signature included. Read fields a
            newer server adds from here.
    """

    user: WebBotUser
    bot_id: str
    chat_type: str
    auth_date: datetime
    start_param: str | None = None
    fields: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))


def _fields(init_data: str) -> dict[str, str]:
    if not isinstance(init_data, str):
        raise TypeError(f"init_data must be a str, not {type(init_data).__name__}")
    if not init_data or len(init_data) > _MAX_LENGTH:
        raise WebBotDataError("initData is empty or too long", reason="malformed")
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True, errors="strict")
    except (ValueError, UnicodeDecodeError) as exc:
        raise WebBotDataError("initData is not a form-encoded string", reason="malformed") from exc
    found: dict[str, str] = {}
    for key, value in pairs:
        # a key twice is a key somebody added: which one was signed?
        if key in found:
            raise WebBotDataError(f"initData has {key!r} twice", reason="malformed")
        found[key] = value
    return found


def _text(raw: Mapping[str, Any], key: str) -> str | None:
    value = raw.get(key)
    return value if isinstance(value, str) and value else None


def _read(found: Mapping[str, str]) -> WebBotData:
    try:
        raw_user = json.loads(found["user"])
        stamp = int(found["authDate"])
    except (KeyError, ValueError) as exc:
        raise WebBotDataError("initData misses user or authDate, or they do not read", reason="malformed") from exc
    if not isinstance(raw_user, dict) or not isinstance(raw_user.get("id"), str) or not raw_user["id"]:
        raise WebBotDataError("the user in initData has no ID", reason="malformed")
    bot_id = found.get("botId", "")
    if not bot_id:
        raise WebBotDataError("initData names no bot", reason="malformed")
    user = WebBotUser(
        id=raw_user["id"],
        first_name=_text(raw_user, "firstName"),
        last_name=_text(raw_user, "lastName"),
        username=_text(raw_user, "username"),
        language_code=_text(raw_user, "languageCode"),
        photo_url=_text(raw_user, "photoUrl"),
        banner_url=_text(raw_user, "bannerUrl"),
        allows_write_to_pm=raw_user.get("allowsWriteToPm") is True,
    )
    try:
        signed_at = datetime.fromtimestamp(stamp, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise WebBotDataError("authDate is not a date", reason="malformed") from exc
    return WebBotData(
        user=user,
        bot_id=bot_id,
        chat_type=found.get("chatType", ""),
        auth_date=signed_at,
        start_param=found.get("startParam") or None,
        fields=MappingProxyType(dict(found)),
    )


def _key_bytes(public_key: str | bytes) -> bytes:
    if isinstance(public_key, bytes | bytearray):
        raw = bytes(public_key)
    elif isinstance(public_key, str):
        text = public_key.strip()
        try:
            raw = base64.urlsafe_b64decode(text.replace("+", "-").replace("/", "_") + "=" * (-len(text) % 4))
        except (binascii.Error, ValueError) as exc:
            raise ConfigurationError("the Web-Bot public key is not base64") from exc
    else:
        raise TypeError(f"public_key must be str or bytes, not {type(public_key).__name__}")
    if len(raw) != 32:
        raise ConfigurationError(f"the Web-Bot public key must be 32 bytes, this one is {len(raw)}")
    return raw


def _signature_bytes(text: str) -> bytes:
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as exc:
        raise WebBotDataError("the signature is not base64url", reason="malformed") from exc
    if len(raw) != 64:
        raise WebBotDataError("the signature is not 64 bytes", reason="malformed")
    return raw


def check_string(bot_id: str, found: Mapping[str, str]) -> str:
    r"""The text the signature covers, for a bot and the fields of an ``initData``.

    Args:
        bot_id: The bot the data is for, such as ``"B48213907"``.
        found: The decoded fields; ``signature`` is left out if it is there.

    Returns:
        ``"<bot_id>:WebBotData"``, then each other field as ``key=value``, sorted, one per line.

    Example:
        >>> check_string("B1", {"user": "{}", "authDate": "5", "signature": "x"})
        'B1:WebBotData\nauthDate=5\nuser={}'
    """
    lines = [f"{key}={value}" for key, value in sorted(found.items()) if key != "signature"]
    return "\n".join([f"{bot_id}:{_TAG}", *lines])


def parse_web_bot_data(init_data: str) -> WebBotData:
    """Reads ``initData`` without checking it.

    For logs, or to show a name before the check has run. Never act on what it returns:
    without the signature check, every field could have been made up. Use
    `validate_web_bot_data` for anything that matters.

    Args:
        init_data: The string as the page sent it.

    Returns:
        What it says, unverified.

    Raises:
        WebBotDataError: The string does not read as ``initData`` (``reason="malformed"``).

    Example:
        >>> data = parse_web_bot_data(
        ...     "user=%7B%22id%22%3A%2248213907%22%2C%22firstName%22%3A%22Anna%22%7D"
        ...     "&chatType=private&authDate=1700000000&botId=B48213907&signature=x"
        ... )
        >>> data.user.first_name, data.chat_type, data.auth_date.year
        ('Anna', 'private', 2023)
    """
    return _read(_fields(init_data))


def validate_web_bot_data(
    init_data: str,
    *,
    bot_id: str,
    public_key: str | bytes,
    max_age: float | None = WEB_BOT_DATA_MAX_AGE,
    now: float | None = None,
) -> WebBotData:
    """Checks ``initData`` and reads it: the signature, the bot it was signed for, its age.

    Run it on the bot's server, on the string the page sent, before believing who the
    user is. It takes no network: the key is passed in.

    Args:
        init_data: The string as the page sent it (``window.Hidesay.WebBot.initData``).
        bot_id: The bot's own ID, as `~hidesay_bot.AsyncClient.get_me` gives it
            (``"B48213907"``). Data signed for another bot is refused.
        public_key: The server's Web-Bot key, base64url as
            `~hidesay_bot.AsyncClient.get_web_bot_key` returns it, or its 32 raw bytes.
        max_age: The oldest data accepted, in seconds since it was signed, or ``None`` to
            accept any age.
        now: The current time as a Unix timestamp, for tests; the clock when ``None``.

    Returns:
        What the data says, now that it is known to come from the server.

    Raises:
        WebBotDataError: The data is refused. ``reason`` says why: ``"malformed"``,
            ``"signature"`` (not signed by the server, or changed since), ``"wrong_bot"`` or
            ``"expired"``.
        ConfigurationError: ``public_key`` is not a key.

    Example:
        A route in any web framework::

            data = validate_web_bot_data(
                body["initData"], bot_id=me.id, public_key=WEB_BOT_KEY, max_age=3600
            )
            orders = await load_orders(data.user.id)
    """
    if not isinstance(bot_id, str):
        raise TypeError(f"bot_id must be a str, not {type(bot_id).__name__}")
    if not bot_id:
        raise TypeError("bot_id is empty: pass the bot's ID, as get_me() gives it")
    key = _key_bytes(public_key)
    found = _fields(init_data)
    signature = found.get("signature")
    if not signature:
        raise WebBotDataError("initData has no signature", reason="malformed")
    message = check_string(bot_id, found).encode("utf-8")
    # checked against the bot asking, not the one the data names: a string signed for
    # another bot fails here, whatever its botId field says
    if not _ed25519.verify(key, _signature_bytes(signature), message):
        raise WebBotDataError("initData is not signed by the Hidesay server, or was changed", reason="signature")
    data = _read(found)
    if data.bot_id != bot_id:
        raise WebBotDataError(f"initData is for bot {data.bot_id}, not {bot_id}", reason="wrong_bot")
    if max_age is not None:
        age = (time.time() if now is None else now) - data.auth_date.timestamp()
        if age > max_age:
            raise WebBotDataError(f"initData is {int(age)} seconds old, more than {max_age:g}", reason="expired")
    return data

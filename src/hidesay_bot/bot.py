"""Bot and SyncBot: a token, some handlers, and ``run()``.

`~hidesay_bot.Bot` runs ``async def`` handlers; `~hidesay_bot.SyncBot` runs plain functions in a
pool of threads. Both are routers (``@bot.on_message()``, ``@bot.command("start")``, ...)
with the whole Bot API on top, and both open and seal OpenPGP messages on their own when a
chat is end-to-end encrypted, so handlers read and write plain text. Underneath it is the
same engine: the two take the same options, have the same methods (called without ``await``
on a `~hidesay_bot.SyncBot`) and behave the same way.

Import them from the package::

    from hidesay_bot import Bot, SyncBot
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import inspect
import logging
import os
import signal
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Self, TypeVar

import httpx2

from . import _validate as v
from . import wire
from ._http import ENV_TOKEN, RetryPolicy, mask_token
from ._secrets import Secret, install_redaction, read_env, resolve
from ._spec import StickerLike, _kind_of, _sticker_ref
from .client import MEDIA_MAX_BYTES, AsyncClient, Client
from .crypto import BotKey, PublicKey
from .engine import Engine, poll, skip_pending
from .errors import (
    APIError,
    AuthenticationError,
    CallbackAnsweredError,
    CallbackExpiredError,
    CallbackNotFoundError,
    ChatIsOpenError,
    ChatIsProtectedError,
    ConfigurationError,
    DecryptionError,
    EncryptionError,
    EncryptionRequiredError,
    InvalidKeyError,
    KeyChangedError,
    MediaIntegrityError,
    NetworkError,
    NotARecipientError,
    PollingConflictError,
    PollingError,
    ServerError,
    SignatureError,
    ValidationError,
    WrongBodyKindError,
)
from .files import InputFile, input_files
from .filters import FilterContext, merge
from .keyboard import InlineKeyboard
from .models import (
    KEEP,
    Ban,
    BotInfo,
    CallbackQuery,
    CallbackQueryBase,
    ChannelStats,
    Chat,
    ChatKeys,
    ChatKind,
    ChatMembers,
    ChatMode,
    ChatRef,
    Command,
    CommandScope,
    Comments,
    Invite,
    JoinRequest,
    Media,
    MediaIn,
    Member,
    Message,
    MessageBase,
    MessageReactions,
    Picture,
    Profile,
    PublishedKey,
    Resolved,
    SentMessage,
    SentMessageBase,
    SignatureStatus,
    Sticker,
    SyncCallbackQuery,
    SyncMessage,
    SyncSentMessage,
    Update,
    UpdateKind,
    UploadedFile,
    UserKey,
    Verification,
    WebhookInfo,
)
from .ratelimit import RateLimiter
from .router import Handler, Router, SkipHandler, is_async
from .storage import FailedUpdate, MemoryStorage, SQLiteStorage, Storage
from .webbot import WEB_BOT_DATA_MAX_AGE, WebBotData, validate_web_bot_data
from .webhook import WebhookApp
from .wire import parse_body

FileLike = InputFile | bytes | str | os.PathLike[str]

__all__ = ["Bot", "SyncBot"]

log = logging.getLogger("hidesay_bot.bot")

T = TypeVar("T")
ChatLike = str | int | ChatRef
VerifyMode = Literal["auto", "require", "off"]

_CACHE_MAX = 10_000
_ANSWERED_MAX = 2_000
# a group's admins can make it encrypted at any moment: "open" is trusted this long only
_OPEN_TRUST = 10.0
# the app writes at most 4096 characters plus small envelopes: anything far bigger is not a message
_MESSAGE_PLAINTEXT_MAX = 1 << 20


def _chat_id(chat: Any) -> str:
    if isinstance(chat, ChatRef):
        return chat.id
    if isinstance(chat, (MessageBase, CallbackQueryBase, SentMessageBase)):
        return chat.chat.id
    return v.check_chat_id(chat)


def _message_id(message: Any) -> int:
    if isinstance(message, (MessageBase, SentMessageBase)):
        return message.id
    return v.check_id(message, "message_id")


def _user_id(user: Any) -> str:
    # the id itself, a User, or what carries one: a Member, a Ban, a JoinRequest
    if isinstance(user, (str, int)):
        return v.check_user_id(user)
    for attr in ("user", "from_user"):
        held = getattr(user, attr, None)
        if held is not None and hasattr(held, "id"):
            return v.check_user_id(held.id)
    return v.check_user_id(getattr(user, "id", user))


def _media_in(media: Media, media_id: str, data: bytes) -> MediaIn:
    """A received file described again for a readable chat, keeping only what passes the server's checks."""

    def kept(check: Callable[[Any], Any], value: Any) -> Any:
        if value is None:
            return None
        try:
            return check(value)
        except ValidationError:
            return None

    return MediaIn(
        id=media_id,
        kind=media.kind if media.kind in v.MEDIA_KINDS else "file",
        mime=kept(v.check_mime, media.mime),
        name=kept(v.check_media_name, media.name),
        size=len(data) or None,
        width=kept(lambda w: v.check_media_side(w, "width"), media.width),
        height=kept(lambda h: v.check_media_side(h, "height"), media.height),
        duration_ms=media.duration_ms if media.duration_ms and media.duration_ms <= v.MEDIA_DURATION_MAX else None,
        thumb=kept(v.check_thumb, media.thumb),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def _bytes_of(data: bytes | str | os.PathLike[str], field: str) -> bytes:
    if isinstance(data, (bytes, bytearray, memoryview)):
        return bytes(data)
    if isinstance(data, (str, os.PathLike)):
        try:
            return Path(data).read_bytes()
        except OSError as exc:
            raise ValidationError(f"cannot read {data}: {exc.strerror}", field=field) from None
    raise ValidationError(f"{field} is bytes or a path, got {type(data).__name__}", field=field)


class _TTLCache:
    """A small thread-safe cache with a time to live and a size cap."""

    def __init__(self, ttl: float, size: int = _CACHE_MAX) -> None:
        self.ttl = ttl
        self._size = size
        self._data: OrderedDict[Any, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: Any) -> Any:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            if time.monotonic() - item[0] >= self.ttl:
                del self._data[key]
                return None
            return item[1]

    def fresh(self, key: Any, seconds: float) -> bool:
        """Whether the entry for `key` was put less than `seconds` ago."""
        with self._lock:
            item = self._data.get(key)
            return item is not None and time.monotonic() - item[0] < min(seconds, self.ttl)

    def put(self, key: Any, value: Any) -> None:
        with self._lock:
            self._data[key] = (time.monotonic(), value)
            self._data.move_to_end(key)
            while len(self._data) > self._size:
                self._data.popitem(last=False)

    def drop(self, key: Any = None) -> None:
        with self._lock:
            if key is None:
                self._data.clear()
            else:
                self._data.pop(key, None)


def _load_key(value: Any, passphrase: Any) -> BotKey | None:
    if value is None:
        return BotKey.from_env(passphrase)
    if isinstance(value, BotKey):
        return value
    secret = resolve(passphrase, "pgp_passphrase")
    if secret is None:
        secret = read_env("HIDESAY_BOT_KEY_PASSPHRASE")
    if isinstance(value, os.PathLike):
        return BotKey.from_file(value, secret)
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        if "-----BEGIN PGP" in value:
            return BotKey.from_armored(value, secret)
        path = Path(value)
        if path.is_file():
            return BotKey.from_file(path, secret)
        raise ConfigurationError("pgp_private_key is neither an armored key nor the path of an existing file")
    raise ConfigurationError(f"pgp_private_key must be a BotKey, a path or armored text, got {type(value).__name__}")


class _BotBase(Router):
    """Everything Bot and SyncBot share: settings, caches, keys, the engine."""

    _flavour = "async"

    def __init__(
        self,
        token: str | Secret | Callable[[], str] | None = None,
        *,
        api_url: str | None = None,
        pgp_private_key: BotKey | str | bytes | os.PathLike[str] | None = None,
        pgp_passphrase: str | Secret | Callable[[], str] | None = None,
        pgp_previous_keys: Sequence[BotKey] = (),
        storage: Storage | str | os.PathLike[str] | None = None,
        concurrency: int = 8,
        publish_key: bool = False,
        require_encryption: bool = False,
        verify_signatures: VerifyMode = "auto",
        pin_keys: bool = False,
        cache_ttl: float = 60.0,
        auto_answer_callbacks: bool = True,
        retry: RetryPolicy | None = None,
        rate_limit: bool | RateLimiter = True,
        timeout: float | httpx2.Timeout | None = None,
        allow_insecure_http: bool = False,
        max_handler_runs: int = 5,
        shutdown_timeout: float = 30.0,
        transport: Any = None,
        name: str | None = None,
    ) -> None:
        """Sets up the bot. Nothing is sent to the server until it starts.

        The format of the token is checked here, and the private key is loaded and unlocked
        here, so a malformed token or a wrong passphrase fails at once, not at the first
        update. Every option except ``token`` is keyword-only, and a value given here wins over
        the environment.

        Args:
            token: The bot's token, ``pgpbot_<id>.<43 characters>``: a string, a `Secret`, or a
                function that returns one (called once, here). ``None`` reads
                ``HIDESAY_BOT_TOKEN``, or the file named by ``HIDESAY_BOT_TOKEN_FILE``.
            api_url: The API server, like ``https://api.hidesay.com``; a trailing ``/bot/v1``
                is removed. ``None`` reads ``HIDESAY_API``, and uses
                ``https://api.hidesay.com`` when it is not set.
            pgp_private_key: The bot's private key, for end-to-end encrypted chats: a `BotKey`,
                the path of a key file, or the armored key as text or bytes. ``None`` reads
                ``HIDESAY_BOT_KEY`` (the armored key or a path) or the file named by
                ``HIDESAY_BOT_KEY_FILE``; with neither, the bot has no key.
            pgp_passphrase: The passphrase that unlocks the private key: a string, a `Secret`,
                or a function that returns one. ``None`` reads ``HIDESAY_BOT_KEY_PASSPHRASE``
                (or ``HIDESAY_BOT_KEY_PASSPHRASE_FILE``). Not used with a `BotKey`, which is
                unlocked already.
            pgp_previous_keys: Older keys, still tried to decrypt messages after a rotation.
                Keep the old key here for at least 72 hours after `rotate_key`.
            storage: Where updates wait to be handled, with the failed updates and the pinned
                keys: a `Storage`, or a path, which means ``SQLiteStorage(path)``. ``None``
                means a new `MemoryStorage`, which forgets everything when the process ends.
            concurrency: How many chats are handled at the same time, at least 1. The updates
                of one chat are always handled one at a time, in order. On a `SyncBot` it is
                also the number of handler threads.
            publish_key: ``True`` publishes the key at start-up when the server has a
                different one, or none. Publishing is one-way: see the warning below.
            require_encryption: ``True`` never sends anything in clear, anywhere: a send into
                a readable chat raises `EncryptionRequiredError`.
            verify_signatures (str): How the signatures of encrypted messages are checked.
                ``"auto"`` checks them and reports the result in the message's
                ``verification``. ``"require"`` refuses a message without a valid signature: a
                `SignatureError` goes to the error handlers instead. ``"off"`` does not check,
                and does not fetch the members' keys.
            pin_keys: ``True`` remembers the first key fingerprint seen for each person and
                refuses a different one later, with `KeyChangedError`: sends to that person
                fail, and their messages go to the error handlers (when ``verify_signatures``
                is not ``"off"``). `trust_key` accepts a new key. The pins are kept in the
                storage: use a `SQLiteStorage` to keep them across restarts.
            cache_ttl: How long, in seconds, chat details, chat modes and members' keys are
                cached. The readable mode of a group or channel is trusted for 10 seconds at
                most.
            auto_answer_callbacks: ``True`` answers a button press with nothing after the
                handler ends, if the handler did not answer it, so the button stops spinning.
            retry: How often a failed call is tried again. ``None`` means the defaults of
                `RetryPolicy`.
            rate_limit: ``True`` paces the calls with a `RateLimiter` that takes the server's
                numbers from ``GET /me`` at start-up. ``False`` turns pacing off. A
                `RateLimiter` of your own is used as it is, with its own numbers.
            timeout: How long one HTTP request may take: seconds, or an ``httpx2.Timeout``.
                ``None`` means 30 seconds, and 10 to connect. Long polls get extra time on
                their own.
            allow_insecure_http: ``True`` allows an ``http://`` server that is not
                ``localhost``. Only for a test server on your own network: see the warning
                below.
            max_handler_runs: An update whose handlers were started this many times and never
                finished (the process crashed or was stopped half way) goes to the failed list
                instead of running again.
            shutdown_timeout: At stop, how many seconds the running handlers get to finish.
                Those still running are then cancelled, and their updates run again at the
                next start. A `SyncBot` can't cancel a thread: there, a late handler goes on
                until it returns (see `SyncBot.run`).
            transport: Advanced: an httpx2 transport for the bot's HTTP clients, such as a
                proxy setup or a fake server in tests.
            name: The name of the bot's root router; ``None`` means ``"bot"``.

        Raises:
            ConfigurationError: No token was given or found, the token or ``api_url`` is not
                acceptable, an option has a wrong value, or ``pgp_private_key`` is neither a
                `BotKey`, armored text nor a file that can be read.
            InvalidKeyError: The private key is not a readable OpenPGP key, or it is a public
                key.
            PassphraseError: The private key is locked, and the passphrase is missing or
                wrong.

        Attributes:
            bot_id (int): The number in the token. It is not the bot's ID, which `me` has
                (``"B48213907"``), and a token made before IDs became strings carries the old
                number.
            api_url (str): The API server, without ``/bot/v1``.
            key (BotKey | None): The bot's private key, or ``None`` when it has none.
                `rotate_key` replaces it.
            storage (Storage): The storage in use.
            concurrency (int): How many chats are handled at the same time.
            publish_key_on_start (bool): Whether the key is published at start-up: the
                ``publish_key`` option.
            require_encryption (bool): Whether sending in clear is refused everywhere.
            verify_signatures (str): ``"auto"``, ``"require"`` or ``"off"``.
            pin_keys (bool): Whether key fingerprints are pinned.
            auto_answer_callbacks (bool): Whether a button press the handler did not answer is
                answered after it.
            max_handler_runs (int): How many unfinished runs an update gets before it goes to
                the failed list.
            shutdown_timeout (float): How many seconds the running handlers get at stop.

        Example:
            An encrypted bot whose updates wait in a SQLite file. `SyncBot` takes the same
            options::

                import os

                from hidesay_bot import Bot

                bot = Bot(
                    pgp_private_key="bot-key.asc",
                    pgp_passphrase=os.environ["BOT_KEY_PASSPHRASE"],
                    storage="bot.db",
                )

        Note:
            ``repr(bot)`` shows the token masked (``pgpbot_412.****abcd``), and a bot can't be
            pickled: it holds the token, and maybe a private key.

        Warning:
            Publishing a key is one-way: the bot's private chats become end-to-end encrypted,
            and only its owner can undo that, from the console in the app.

            ``allow_insecure_http=True`` lets the token travel in clear, readable by anyone on
            the way to the server.
        """
        super().__init__(name or "bot")
        plain = resolve(token, "token") or read_env(ENV_TOKEN)
        if not plain:
            raise ConfigurationError(f"no bot token: pass token=... or set {ENV_TOKEN} (or {ENV_TOKEN}_FILE)")
        if verify_signatures not in ("auto", "require", "off"):
            raise ConfigurationError("verify_signatures must be 'auto', 'require' or 'off'")
        if concurrency < 1:
            raise ConfigurationError("concurrency must be at least 1")

        if rate_limit is True:
            self._limiter: RateLimiter | None = RateLimiter()
        elif rate_limit is False:
            self._limiter = None
        else:
            self._limiter = rate_limit
        # only the limiter made here takes the server's numbers; one passed in keeps its own
        self._own_limiter = rate_limit is True
        self._client_options: dict[str, Any] = {
            "api_url": api_url,
            "retry": retry,
            "timeout": timeout,
            "allow_insecure_http": allow_insecure_http,
            "rate_limiter": self._limiter if self._limiter is not None else False,
        }
        self._plain_token = Secret(plain)
        # advanced: an httpx2 transport for both clients (a proxy mount, a test double)
        self._transport = transport
        self._aapi = AsyncClient(plain, **self._client_options, transport=transport)
        self.bot_id = self._aapi.bot_id
        self.api_url = self._aapi.api_url
        self._token_hint = mask_token(plain)

        self.key: BotKey | None = _load_key(pgp_private_key, pgp_passphrase)
        self._previous_keys: list[BotKey] = list(pgp_previous_keys)
        if isinstance(storage, (str, os.PathLike)):
            storage = SQLiteStorage(storage)
        self.storage: Storage = storage or MemoryStorage()

        self.concurrency = concurrency
        self.publish_key_on_start = publish_key
        self.require_encryption = require_encryption
        self.verify_signatures: VerifyMode = verify_signatures
        self.pin_keys = pin_keys
        self.auto_answer_callbacks = auto_answer_callbacks
        self.max_handler_runs = max_handler_runs
        self.shutdown_timeout = shutdown_timeout

        self._me: BotInfo | None = None
        self._me_at = float("-inf")
        # the server's Web-Bot key: public, the same for every bot, fetched once
        self._web_bot_key: str | None = None
        self._cache_ttl = cache_ttl
        self._chats = _TTLCache(cache_ttl)
        self._modes = _TTLCache(cache_ttl)
        self._keys = _TTLCache(cache_ttl)
        self._parsed_keys: OrderedDict[tuple[str, str], PublicKey] = OrderedDict()
        self._kinds: OrderedDict[str, ChatKind] = OrderedDict()
        self._answered: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()

        self._engine: Engine | None = None
        self._stop_event: asyncio.Event | None = None
        self._running = False
        self._starting: asyncio.Lock | None = None
        self._webhook: WebhookApp | None = None
        self._background: set[asyncio.Task[Any]] = set()
        self._holds_lock = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._startup_hooks: list[Callable[..., Any]] = []
        self._shutdown_hooks: list[Callable[..., Any]] = []
        install_redaction()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(bot_id={self.bot_id}, api_url={self.api_url!r}, token={self._token_hint!r})"

    def __reduce__(self) -> Any:
        raise TypeError(f"{type(self).__name__} cannot be pickled: it holds the token and maybe a private key")

    # --- small shared pieces ---

    @property
    def me(self) -> BotInfo | None:
        """The bot's own details, as ``GET /me`` last returned them.

        ``None`` until they are first read: when the bot starts, or by `get_me`. The bot reads
        them again when it needs to (after publishing a key, for example), and `get_me` with
        ``refresh=True`` does it when you ask.
        """
        return self._me

    @me.setter
    def me(self, value: BotInfo | None) -> None:
        self._me = value
        self._me_at = time.monotonic()

    def _me_fresh(self) -> bool:
        return time.monotonic() - self._me_at < self._cache_ttl

    @property
    def username(self) -> str | None:
        """The bot's handle, without ``@``, or ``None`` before `me` is known."""
        return self.me.username if self.me else None

    @property
    def text_max(self) -> int:
        """The longest text the server accepts in one message, in UTF-16 units.

        It comes from `me` (``limits.text_max``) once that is known, and is 4096 before. Sends
        and edits check their text against it before anything is sent.
        """
        return self.me.limits.text_max if self.me else v.TEXT_MAX

    @property
    def upload_max(self) -> int:
        """The largest file one upload takes, in bytes.

        It comes from `me` (``limits.upload_bytes_max``) once that is known, and is 50 MiB
        before. `send_media` checks its files against it before anything is sent.
        """
        return self.me.limits.upload_bytes_max if self.me else v.UPLOAD_MAX

    @staticmethod
    def _seal_file(key: BotKey, data: bytes, recipients: list[PublicKey]) -> bytes:
        # as the phones seal files: binary and unsigned. The signed message that points at the
        # file carries its SHA-256, so the file can be passed on without naming who sealed it
        sealed = key.encrypt(data, recipients, sign=False, armor=False)
        assert isinstance(sealed, bytes)
        return sealed

    @staticmethod
    def _check_integrity(media: Media | str, data: bytes) -> bytes:
        expected = media.sha256 if isinstance(media, Media) else None
        if expected:
            actual = hashlib.sha256(data).hexdigest()
            if actual != expected:
                raise MediaIntegrityError(
                    f"file {media.id if isinstance(media, Media) else media} is not the one its message describes: "
                    f"SHA-256 {actual}, the message says {expected}",
                    expected=expected,
                    actual=actual,
                )
        return data

    def _keyring(self) -> list[BotKey]:
        return [k for k in (self.key, *self._previous_keys) if k is not None]

    def _remember_kind(self, chat: ChatRef) -> None:
        with self._lock:
            self._kinds[chat.id] = chat.kind
            self._kinds.move_to_end(chat.id)
            while len(self._kinds) > _CACHE_MAX:
                self._kinds.popitem(last=False)

    def _known_kind(self, chat_id: str) -> ChatKind | None:
        with self._lock:
            return self._kinds.get(chat_id)

    def _mode_without_asking(self, chat_id: str) -> tuple[ChatMode, ChatKind] | None:
        if self._known_kind(chat_id) == ChatKind.PRIVATE:
            # a private chat is protected exactly when the bot has a published key, which
            # can change under a running bot (the console, the command line, another worker)
            if self.me is not None and self._me_fresh():
                return self.me.mode, ChatKind.PRIVATE
            return None
        cached = self._modes.get(chat_id)
        if cached is None:
            return None
        mode, kind = cached
        if mode == ChatMode.OPEN and not self._modes.fresh(chat_id, _OPEN_TRUST):
            # sending words in clear on an old "open" is how a message leaks into a room
            # that was made encrypted a minute ago: ask again
            return None
        return mode, kind

    def _note_chat(self, chat: Chat) -> None:
        self._chats.put(chat.id, chat)
        self._modes.put(chat.id, (chat.mode, chat.kind))
        self._remember_kind(chat)

    def _forget_chat(self, chat_id: str) -> None:
        self._chats.drop(chat_id)
        self._modes.drop(chat_id)
        self._keys.drop(chat_id)

    def _observe(self, update: Update) -> None:
        """What an update tells us about its chat, for free."""
        chat = update.chat
        if chat is None:
            return
        self._remember_kind(chat)
        message = update.message
        if message is not None:
            if chat.is_private:
                if message.encrypted and self.me is not None and not self.me.protected:
                    # phones seal to the bot only once it has a published key: look again before the next send
                    self._me_at = float("-inf")
            else:
                # the body is a hint, never a fact: anyone in the room shapes it. On a
                # mismatch ask the server before the next send, never take the hint itself
                cached = self._modes.get(chat.id)
                seen = ChatMode.PROTECTED if message.encrypted else ChatMode.OPEN
                if cached is not None and cached[0] != seen:
                    self._modes.drop(chat.id)
        change = update.chat_member
        if change is not None:
            # our seat changed: rights, membership, maybe the roster we may see. A room
            # comes whole with it, so the seat and the room make what GET /chats/{id} says
            self._forget_chat(chat.id)
            if isinstance(change.chat, Chat) and change.new.is_inside:
                self._note_chat(replace(change.chat, me=change.new))
        if update.member_update is not None:
            # somebody came or went: the keys to seal to are not the same any more
            self._keys.drop(chat.id)
        if update.chat_updated is not None:
            self._forget_chat(chat.id)

    def _body_kind(self, mode: ChatMode, chat_id: str, encrypt: bool | None) -> Literal["text", "armored"]:
        if mode == ChatMode.PROTECTED:
            if encrypt is False:
                raise ValidationError(
                    f"chat {chat_id} is end-to-end encrypted: it can't take a message in clear (encrypt=False)"
                )
            return "armored"
        if encrypt is True or self.require_encryption:
            raise EncryptionRequiredError(
                f"chat {chat_id} is readable by the server and this message must be encrypted; it was not sent"
            )
        return "text"

    def _need_key(self, chat_id: str) -> BotKey:
        if self.key is None:
            raise EncryptionError(
                f"chat {chat_id} is end-to-end encrypted and the bot has no private key: "
                "pass pgp_private_key=... (or set HIDESAY_BOT_KEY)"
            )
        return self.key

    def _public_key_of(self, user_key: UserKey) -> PublicKey:
        cache_key = (user_key.user_id, user_key.public_key)
        with self._lock:
            hit = self._parsed_keys.get(cache_key)
            if hit is not None:
                return hit
        key = PublicKey.from_user_key(user_key)
        with self._lock:
            self._parsed_keys[cache_key] = key
            while len(self._parsed_keys) > _CACHE_MAX:
                self._parsed_keys.popitem(last=False)
        return key

    def _check_pin(self, user_id: str, fingerprint: str) -> None:
        if not self.pin_keys:
            return
        pinned = self.storage.get_pin(user_id)
        if pinned is None:
            self.storage.set_pin(user_id, fingerprint)
            return
        if pinned.upper() != fingerprint.upper():
            raise KeyChangedError(
                f"the key of user {user_id} changed: pinned {pinned}, the server now gives {fingerprint}. "
                "Check it out of band, then call trust_key()",
                user_id=user_id,
                pinned=pinned,
                current=fingerprint,
            )

    def _is_me(self, user_id: str) -> bool:
        # /me says the bot's id with its B. The token only carries a number, and one made
        # before the ids changed in November 2026 still carries the old one
        if self.me is not None:
            return user_id == self.me.id
        return user_id == f"B{self.bot_id}"

    def _recipients_from(self, chat_id: str, keys: ChatKeys, private: bool) -> list[PublicKey]:
        """Turns ``GET /chats/{id}/keys`` into keys to seal to.

        Refuses rather than seal to fewer people than it should in a private chat; in a group a
        broken key is skipped (as the app does) with a warning.
        """
        out: list[PublicKey] = []
        for user_key in keys.keys:
            if self._is_me(user_key.user_id):
                continue
            try:
                key = self._public_key_of(user_key)
                if key.version != 4:
                    raise InvalidKeyError("a v6 key: phones on PGPainless 1.7 could not read what is sealed to it")
                if not key.usable:
                    raise InvalidKeyError("no live encryption subkey (expired, revoked or sign-only)")
            except InvalidKeyError as exc:
                if private:
                    raise EncryptionError(f"the key of user {user_key.user_id} can't be used: {exc}") from exc
                log.warning("chat %s: leaving out user %s, whose key can't be used: %s", chat_id, user_key.user_id, exc)
                continue
            self._check_pin(user_key.user_id, key.fingerprint)
            out.append(key)
        if not out:
            raise EncryptionError(f"nobody in chat {chat_id} has a key the message could be sealed to")
        return out

    def _decrypt_any(
        self,
        armored: str | bytes,
        candidates: list[PublicKey],
        *,
        max_size: int | None = None,
        allow_compression: bool = True,
    ) -> Any:
        keys = self._keyring()
        if not keys:
            raise DecryptionError(
                "an encrypted message arrived and the bot has no private key: pass pgp_private_key=..."
            )
        for key in keys:
            try:
                return key.decrypt(
                    armored, verify_with=candidates, max_size=max_size, allow_compression=allow_compression
                )
            except NotARecipientError:
                continue
        tried = ", ".join(k.fingerprint for k in keys)
        raise NotARecipientError(
            f"sealed to none of the bot's keys ({tried}). A message written to a key the bot no longer "
            "loads; keep the old key in pgp_previous_keys for a while after a rotation"
        )

    def _apply_decryption(self, message: MessageBase, result: Any) -> MessageBase:
        text = result.text
        # OpenPGP.js writes text literals with CRLF; the server hands readable ones over with LF
        body = parse_body(text.replace("\r\n", "\n"))
        verification: Verification = result.verification
        if self.verify_signatures == "require" and not verification.valid:
            raise SignatureError(
                f"message {message.id} in chat {message.chat.id}: signature {verification.status.value}"
                + (f" ({verification.detail})" if verification.detail else "")
            )
        return replace(
            message,
            text=body.text,
            media=body.media,
            sticker=body.sticker,
            forward=body.forward,
            link=body.link,
            plaintext=text,
            verification=verification,
        )

    def _sender_candidates(self, message: MessageBase, keys: ChatKeys | None) -> tuple[list[PublicKey], str | None]:
        if message.from_user is None:
            return [], "a post that speaks as the channel carries no sender to check against"
        if self._is_me(message.from_user.id):
            # the bot's own message, from its history: it signed it with one of its keys
            return [k.public_key for k in self._keyring()], None
        if keys is None:
            return [], "the chat's keys could not be fetched"
        for user_key in keys.keys:
            if user_key.user_id == message.from_user.id:
                try:
                    key = self._public_key_of(user_key)
                except InvalidKeyError as exc:
                    return [], f"the sender's key is unreadable: {exc}"
                if self.pin_keys:
                    # the same rule as for sending: a changed key is refused, not just reported
                    self._check_pin(user_key.user_id, key.fingerprint)
                return [key], None
        return [], "the server lists no key for the sender in this chat"

    def _note_answered(self, callback_id: str) -> None:
        with self._lock:
            self._answered[callback_id] = None
            while len(self._answered) > _ANSWERED_MAX:
                self._answered.popitem(last=False)

    def _was_answered(self, callback_id: str) -> bool:
        with self._lock:
            return callback_id in self._answered

    def _filter_context(self, update: Update) -> FilterContext:
        me = self.me
        return FilterContext(
            bot_username=self.username,
            update=update,
            bot=self,
            bot_usernames=me.handles if me is not None else (),
            bot_id=me.id if me is not None else f"B{self.bot_id}",
        )

    def _check_flavour(self) -> None:
        """Bot wants coroutines, SyncBot plain functions. Better to say so at start than mid-update."""
        want_async = self._flavour == "async"
        wrong: list[str] = []
        for handler in self.all_handlers():
            if handler.is_async != want_async:
                wrong.append(f"handler {handler.name}")
            if not want_async:
                wrong.extend(f"filter {f!r} of {handler.name}" for f in handler.filters if f.is_async)
        for router in self._walk():
            wrong.extend(
                f"error handler {getattr(eh.callback, '__qualname__', eh.callback)}"
                for eh in router._error_handlers
                if eh.is_async != want_async
            )
            wrong.extend(
                f"middleware {getattr(mw, '__qualname__', mw)}"
                for mw in router._middlewares
                if is_async(mw) != want_async
            )
        wrong.extend(
            f"hook {getattr(hook, '__qualname__', hook)}"
            for hook in (*self._startup_hooks, *self._shutdown_hooks)
            if is_async(hook) != want_async
        )
        if wrong:
            if want_async:
                hint = "Bot runs async def handlers; make them async, or use SyncBot for plain functions"
            else:
                hint = "SyncBot runs plain functions; use Bot for async def handlers"
            raise ConfigurationError(f"{hint}: {', '.join(wrong)}")

    def _event_for(self, update: Update, handler_kind: UpdateKind | None) -> Any:
        if handler_kind is None:
            return update
        if update.message is not None:
            return self._bind_message(update.message)
        if update.callback_query is not None:
            return self._bind_query(update.callback_query)
        event = update.event
        return event if event is not None else update

    def _bind_message(self, message: MessageBase) -> Any:
        raise NotImplementedError

    def _bind_query(self, query: CallbackQueryBase) -> Any:
        raise NotImplementedError

    # --- shared async machinery: both bots run their engine on asyncio ---

    async def _fetch_chat_keys(self, chat_id: str) -> ChatKeys:
        cached = self._keys.get(chat_id)
        if cached is not None:
            return cached  # type: ignore[no-any-return]
        keys = await self._aapi.get_chat_keys(chat_id)
        self._keys.put(chat_id, keys)
        return keys

    async def _open(self, message: MessageBase, *, quiet: bool = False) -> MessageBase:
        """Decrypts and checks an encrypted message, and the one it quotes. A readable one comes back as it is.

        ``quiet`` hands back as it is a message that can't be opened, instead of raising: for the
        history and for a quoted message, where one bad message must not hide the others.
        """
        try:
            opened = await self._open_one(message)
        except (DecryptionError, SignatureError, KeyChangedError) as exc:
            if not quiet:
                raise
            log.debug("message %s in chat %s left sealed: %s", message.id, message.chat.id, exc)
            opened = message
        quoted = opened.reply_to_message
        if quoted is not None and quoted.armored is not None:
            opened = replace(opened, reply_to_message=await self._open(quoted, quiet=True))
        return opened

    async def _open_one(self, message: MessageBase) -> MessageBase:
        if message.armored is None:
            return message
        candidates: list[PublicKey] = []
        reason: str | None = None
        if self.verify_signatures != "off":
            keys: ChatKeys | None = None
            try:
                keys = await self._fetch_chat_keys(message.chat.id)
            except APIError as exc:
                log.debug("chat %s: keys not fetched (%s), signature left unchecked", message.chat.id, exc)
            except NetworkError as exc:
                log.debug("chat %s: keys not fetched (%s), signature left unchecked", message.chat.id, exc)
            candidates, reason = self._sender_candidates(message, keys)
        try:
            result = await asyncio.to_thread(
                self._decrypt_any, message.armored, candidates, max_size=_MESSAGE_PLAINTEXT_MAX
            )
        except DecryptionError:
            if await self._readable(message.chat):
                # a readable chat: the server called the body armored only because it
                # quotes an armor line. It is words anyone in the chat can read
                return replace(message, text=message.armored, armored=None)
            raise
        if reason is not None and result.verification.status == SignatureStatus.NOT_CHECKED:
            result = replace(result, verification=Verification(SignatureStatus.NOT_CHECKED, detail=reason))
        return self._apply_decryption(message, result)

    async def _amode(self, chat_id: str) -> tuple[ChatMode, ChatKind]:
        known = self._mode_without_asking(chat_id)
        if known is not None:
            return known
        if self._known_kind(chat_id) == ChatKind.PRIVATE:
            self.me = await self._aapi.get_me()
            return self.me.mode, ChatKind.PRIVATE
        info = await self._aapi.get_chat(chat_id)
        self._note_chat(info)
        return info.mode, info.kind

    async def _readable(self, chat: ChatRef) -> bool:
        try:
            mode, _ = await self._amode(chat.id)
        except (APIError, NetworkError):
            return False
        return mode == ChatMode.OPEN

    async def _process_raw(self, raw: Mapping[str, Any]) -> None:
        await self._process(Update.from_json(raw))

    async def _process(self, update: Update) -> None:
        data: dict[str, Any] = {"bot": self, "update": update}
        error: BaseException | None = None
        try:
            if update.message is not None:
                update = replace(update, message=await self._open(update.message))
                data["update"] = update
            self._observe(update)
        except Exception as exc:  # noqa: BLE001 - goes to the error handlers like any other
            error = exc
        try:
            await self._dispatch(update, data, error)
        finally:
            query = update.callback_query
            if query is not None and self.auto_answer_callbacks and not self._was_answered(query.id):
                # the person's button is spinning: an empty answer stops it
                await self._quiet_answer(query)

    async def _quiet_answer(self, query: CallbackQueryBase) -> None:
        if query.expired:
            return
        try:
            await self._aapi.answer_callback(query.id)
            self._note_answered(query.id)
        except (CallbackAnsweredError, CallbackExpiredError, CallbackNotFoundError):
            pass
        except (APIError, NetworkError) as exc:
            log.debug("callback %s: automatic answer failed: %s", query.id, exc)

    async def _dispatch(self, update: Update, data: dict[str, Any], error: BaseException | None) -> None:
        raise NotImplementedError

    # --- running ---

    async def _boot(self) -> BotInfo:
        """`GET /me` until it answers: a server that is down at start-up is waited for, a bad token is not."""
        failures = 0
        while True:
            try:
                return await self._aapi.get_me()
            except AuthenticationError:
                raise
            except (NetworkError, ServerError) as exc:
                failures += 1
                wait = min(30.0, 2.0 * failures)
                log.warning("the API is not answering (%s); trying again in %.0fs", exc, wait)
                await asyncio.sleep(wait)

    async def _start(self, *, polling: bool) -> None:
        if self._starting is None:
            self._starting = asyncio.Lock()
        async with self._starting:
            if self._running:
                return
            self._check_flavour()
            self.me = await self._boot()
            if self._limiter is not None and self._own_limiter:
                self._limiter.update_limits(self.me.limits)
            log.info("running as @%s (id %s, %s)", self.me.username, self.me.id, self.me.mode.value)
            if self.me.paused:
                log.warning("the owner paused this bot: nothing goes in or out until it is resumed")
            await self._check_published_key()

            self.storage.open(self.bot_id)
            if polling:
                if isinstance(self.storage, SQLiteStorage):
                    self.storage.require_lock()
                elif not self.storage.try_lock():
                    raise PollingConflictError("this storage is already used by a running bot in this process")
            self._engine = Engine(
                self.storage, self._process_raw, concurrency=self.concurrency, max_runs=self.max_handler_runs
            )
            self._stop_event = asyncio.Event()
            self._loop = asyncio.get_running_loop()
            self._running = True
            if polling or self.storage.try_lock():
                self._holds_lock = True
                await self._engine.recover()
            else:
                self._holds_lock = False
                log.info("another process holds %s: this one only accepts deliveries for now", self.storage)
                self._spawn(self._wait_for_lock())
            if not polling and self.storage.durable:
                self._spawn(self._sweep_inbox())
            try:
                for hook in self._startup_hooks:
                    await self._call_hook(hook)
            except BaseException:
                await self._shutdown(run_hooks=False)
                raise

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _wait_for_lock(self) -> None:
        assert self._stop_event is not None
        assert self._engine is not None
        while not self._stop_event.is_set():
            await asyncio.sleep(5.0)
            if await asyncio.to_thread(self.storage.try_lock):
                self._holds_lock = True
                log.info("took over processing of %s", self.storage)
                await self._engine.recover()
                return

    async def _sweep_inbox(self) -> None:
        # other worker processes accept deliveries into the same file: pick them up
        assert self._stop_event is not None
        assert self._engine is not None
        while not self._stop_event.is_set():
            await asyncio.sleep(2.0)
            if self._holds_lock:
                await self._engine.recover(quiet=True)

    async def _check_published_key(self) -> None:
        assert self.me is not None
        published = (self.me.fingerprint or "").upper() or None
        if self.key is None:
            if self.me.protected:
                log.warning(
                    "the server says this bot is protected (key %s) but no private key was given: "
                    "encrypted messages will fail to open",
                    published,
                )
            return
        if published == self.key.fingerprint:
            return
        if self.publish_key_on_start:
            await self._publish(self.key)
            return
        if published is None:
            log.warning(
                "a private key was given but the bot has no published key, so it is open (readable chats). "
                "Publish it with publish_key=True or bot.publish_key(); it is one-way, only the owner can undo it"
            )
        else:
            log.warning(
                "the published key is %s but the key given is %s: people seal to the published one. "
                "Call publish_key() to rotate",
                published,
                self.key.fingerprint,
            )

    async def _publish(self, key: BotKey) -> PublishedKey:
        if key.public_key.version != 4:
            raise InvalidKeyError("only v4 keys can be published: phones on PGPainless 1.7 can't use v6 keys")
        result = await self._aapi.set_key(key.export_public(), key.fingerprint)
        self.me = await self._aapi.get_me()
        # every private chat just changed mode
        self._modes.drop()
        self._chats.drop()
        log.info("published key %s: the bot is protected", key.fingerprint)
        return result

    async def _run_polling(self, *, poll_timeout: int, limit: int, drop_webhook: bool, skip: bool) -> None:
        await self._start(polling=True)
        assert self._engine is not None
        assert self._stop_event is not None
        assert self.me is not None
        stop = self._stop_event
        restore = self._install_signals(stop)
        try:
            if self.me.webhook.url:
                if not drop_webhook:
                    raise PollingError(
                        f"a webhook is set ({self.me.webhook.url}), so the server refuses to be polled. "
                        "Use run(drop_webhook=True) to remove it, or run the webhook app instead"
                    )
                await self._aapi.delete_webhook()
                log.info("webhook removed, polling from now on")

            async def fetch(offset: int | None, lim: int, wait: int) -> list[dict[str, Any]]:
                from . import _spec

                updates: list[dict[str, Any]] = await self._aapi.execute(_spec.get_updates(offset, lim, wait, raw=True))
                return updates

            if skip:
                skipped = await skip_pending(fetch, self._engine)
                if skipped:
                    log.info("skipped %d update(s) that were waiting", skipped)
            await poll(fetch, self._engine, stop, poll_timeout=poll_timeout, limit=limit)
        finally:
            restore()
            await self._shutdown()

    def _install_signals(self, stop: asyncio.Event) -> Callable[[], None]:
        if threading.current_thread() is not threading.main_thread():
            return lambda: None
        loop = asyncio.get_running_loop()
        previous: dict[int, Any] = {}

        def handler(signum: int, frame: Any) -> None:
            log.info("signal %s: stopping", signal.Signals(signum).name)
            loop.call_soon_threadsafe(stop.set)

        # SIGINT is asyncio.Runner's: it cancels the main task, which ends up in stop() too
        for name in ("SIGTERM", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            with contextlib.suppress(ValueError, OSError):
                previous[sig] = signal.signal(sig, handler)

        def restore() -> None:
            for sig, old in previous.items():
                with contextlib.suppress(ValueError, OSError):
                    signal.signal(sig, old)

        return restore

    def on_startup(self, func: Callable[..., Any]) -> Callable[..., Any]:
        """Registers ``func`` to run when the bot starts, once it is connected.

        Use it as a decorator. ``func`` gets the bot as its only argument. It runs after
        ``GET /me`` (so `me` is set and API calls work) and once the storage is open, before
        the bot takes new updates: the place to publish the command menu or to open a database
        pool. Hooks run in the order they were registered. If one raises, the bot does not
        start, and the error is raised where the bot was started (`run`, `Bot.start`, ...).

        Args:
            func: The hook: an ``async def`` function on a `Bot`, a plain function on a
                `SyncBot`. The other kind makes the bot refuse to start, with a
                `ConfigurationError`.

        Returns:
            ``func`` itself, unchanged.

        Raises:
            TypeError: ``func`` is not callable.

        Example:
            Publish the command menu as soon as the bot is up::

                @bot.on_startup
                async def ready(bot):
                    await bot.sync_commands()

            On a `SyncBot` the hook is a plain function that calls ``bot.sync_commands()``.

        Note:
            With a `SQLiteStorage`, the updates left pending by the last run are queued just
            before the hooks run, so they can start while a hook is still running.
        """
        if not callable(func):
            raise TypeError("a startup hook must be callable")
        self._startup_hooks.append(func)
        return func

    def on_shutdown(self, func: Callable[..., Any]) -> Callable[..., Any]:
        """Registers ``func`` to run when the bot stops, while it can still send.

        Use it as a decorator. ``func`` gets the bot as its only argument. It runs once the
        running handlers have finished, or once ``shutdown_timeout`` has run out, and before
        the storage and the connections close, so it can still call the API. Hooks run in the
        order they were registered; an error in one is logged, not raised, and the next one
        still runs. The hooks don't run when the bot did not start, or when a startup hook
        failed.

        Args:
            func: The hook: an ``async def`` function on a `Bot`, a plain function on a
                `SyncBot`.

        Returns:
            ``func`` itself, unchanged.

        Raises:
            TypeError: ``func`` is not callable.

        Example:
            Close what a startup hook opened::

                @bot.on_shutdown
                async def closing(bot):
                    await database.disconnect()
        """
        if not callable(func):
            raise TypeError("a shutdown hook must be callable")
        self._shutdown_hooks.append(func)
        return func

    async def _call_hook(self, hook: Callable[..., Any]) -> None:
        await hook(self)

    def request_stop(self) -> None:
        """Asks a running bot to stop, and returns at once.

        Safe to call from a handler, from another thread or from a signal handler. `run` (or
        `Bot.start_polling`) then stops the way it does on Ctrl+C: no new updates, the running
        handlers get ``shutdown_timeout`` seconds to finish, then everything closes. A webhook
        app answers ``503`` from then on, so the server keeps the updates and delivers them
        again later; stopping the web server is up to you. Does nothing when the bot is not
        running.

        Example:
            A command that stops the bot (on a `SyncBot`, the same without ``async`` and
            ``await``)::

                @bot.command("shutdown", filters=(f.user(ADMIN_ID),))
                async def shutdown(message):
                    await message.reply("Bye")
                    bot.request_stop()
        """
        loop, event = self._loop, self._stop_event
        if loop is None or event is None or loop.is_closed():
            return
        try:
            here = asyncio.get_running_loop()
        except RuntimeError:
            here = None
        if here is loop:
            event.set()
        else:
            loop.call_soon_threadsafe(event.set)

    async def _shutdown(self, *, run_hooks: bool = True) -> None:
        """Stops taking updates, lets running handlers finish (up to `shutdown_timeout`), closes everything."""
        if not self._running:
            await self._aapi.aclose()
            return
        self._running = False
        if self._stop_event is not None:
            self._stop_event.set()
        for task in list(self._background):
            task.cancel()
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)
        if self._engine is not None:
            await self._engine.stop(self.shutdown_timeout)
        if run_hooks:
            for hook in self._shutdown_hooks:
                try:
                    await self._call_hook(hook)
                except Exception:
                    log.exception("shutdown hook %s failed", getattr(hook, "__qualname__", hook))
        with contextlib.suppress(Exception):
            self.storage.unlock()
        with contextlib.suppress(Exception):
            self.storage.close()
        await self._aapi.aclose()
        self._starting = None
        log.info("stopped")

    # --- webhook ---

    def create_webhook_app(
        self,
        secret: str | None,
        *,
        url: str | None = None,
        path: str | None = None,
        max_body: int = 1024 * 1024,
        ack_timeout: float = 8.0,
        allow_no_secret: bool = False,
    ) -> WebhookApp:
        """Makes the ASGI application the server POSTs updates to.

        The app checks the secret of every delivery, stores the update and runs the handlers.
        Serve it with any ASGI server (``uvicorn bot:app``): there it starts and stops the bot
        itself. Or add it as a route of a FastAPI or Starlette application, and pass
        ``lifespan=bot.lifespan`` to that application (see `lifespan`). The bot keeps the last
        app made here, and registers its ``url`` when it starts.

        When the server gets its ``200`` depends on the storage: with a `SQLiteStorage`, as
        soon as the update is written to it; with a `MemoryStorage`, only once the handlers are
        done, so the bot handles one update at a time. Use a `SQLiteStorage` for webhooks.

        Args:
            secret: The secret the server sends with every delivery, in the
                ``X-Hidesay-Bot-Api-Secret-Token`` header: 1 to 256 characters of
                ``A-Z a-z 0-9 _ -``. `generate_secret` makes a good one. A delivery with a
                missing or wrong secret gets ``401``.
            url: Your public ``https`` address. When given, the webhook and its secret are
                registered with the server every time the bot starts.
            path: Answer only on this path, and ``404`` elsewhere. Leave it out when another
                application routes to this one.
            max_body: The largest body accepted, in bytes (1 MiB by default); a bigger one
                gets ``413``.
            ack_timeout: With a `MemoryStorage`, how many seconds to wait for the handlers
                before answering ``503``, so that the server delivers the update again later.
                Under 10, the server's own limit.
            allow_no_secret: ``True`` allows ``secret=None``. Anyone who finds the address
                could then send fake updates.

        Returns:
            The `WebhookApp`.

        Raises:
            ConfigurationError: ``secret`` is ``None`` without ``allow_no_secret=True``, or
                ``ack_timeout`` is not between 0 and 10 seconds (both excluded).
            ValidationError: ``secret`` has characters the server does not accept, or ``url``
                is not an absolute ``http`` or ``https`` address of up to 1024 characters, or
                it carries a user name, a password or a ``#fragment``.

        Example:
            A webhook bot, served with ``uvicorn bot:app``::

                bot = Bot(storage="bot.db")
                app = bot.create_webhook_app(
                    secret=os.environ["WEBHOOK_SECRET"],
                    url="https://bot.example.org/hidesay",
                )

        Note:
            Inside FastAPI or Starlette, add it with ``app.add_route(...)``, not
            ``app.mount(...)``: a mount answers the address without the trailing slash with a
            redirect, which the server counts as a failed delivery, and the queue stops.
        """
        app = WebhookApp(
            self,
            secret=secret,
            path=path,
            url=url,
            max_body=max_body,
            ack_timeout=ack_timeout,
            allow_no_secret=allow_no_secret,
        )
        self._webhook = app
        return app

    async def _start_webhook(self, app: WebhookApp | None = None) -> None:
        await self._start(polling=False)
        app = app or self._webhook
        if app is not None and app.url is not None:
            # every time: the server never shows the secret it holds, so a secret
            # changed on this side can't be told apart from the old one. It is idempotent
            await self._aapi.set_webhook(app.url, app.secret)
            log.info("webhook registered at %s", app.url)

    @contextlib.asynccontextmanager
    async def lifespan(self, app: Any = None) -> AsyncIterator[None]:
        """Starts the bot with a FastAPI or Starlette application, and stops it at shutdown.

        Pass it as ``FastAPI(lifespan=bot.lifespan)`` or ``Starlette(lifespan=bot.lifespan)``
        when the app made by `create_webhook_app` is a route of that application: inside
        another application, the webhook app gets no start and stop events of its own. On
        entry the bot starts as `Bot.start` does, and registers the webhook if the app has a
        ``url``; on exit it stops as `Bot.stop` does. Without it, the bot starts at the first
        delivery and logs a warning.

        Args:
            app: The application, passed in by the framework. Not used.

        Yields:
            Nothing; the bot runs until the application shuts down.

        Examples:
            With FastAPI::

                webhook = bot.create_webhook_app(
                    secret=os.environ["WEBHOOK_SECRET"], url="https://example.org/hidesay"
                )
                app = FastAPI(lifespan=bot.lifespan)
                app.add_route("/hidesay", webhook, methods=["POST"])

            An application with a lifespan of its own enters this one inside it::

                @asynccontextmanager
                async def lifespan(app):
                    async with bot.lifespan(app):
                        await database.connect()
                        yield
                        await database.disconnect()
        """
        await self._start_webhook()
        try:
            yield
        finally:
            await self._shutdown()

    async def _accept_delivery(self, update: dict[str, Any], *, ack_timeout: float) -> int:
        if self._stop_event is None:
            # never started: the app was mounted somewhere that never ran its lifespan
            log.warning("webhook delivery before the bot started; starting now (wire bot.lifespan into your app)")
            await self._start_webhook()
        engine = self._engine
        if engine is None or not self._running or self._stop_event is None or self._stop_event.is_set():
            # stopping or stopped: the server keeps the update and tries again later
            return 503
        uid = update["updateId"]
        durable = self.storage.durable
        try:
            if durable and (not self._holds_lock or engine.busy >= engine.max_pending):
                # stored is safe: the worker that holds the lock (or this one, once it has
                # room) picks it up from the inbox. Running it here too would run it twice
                await engine.store([update])
                return 200
            if not durable and engine.busy >= engine.max_pending:
                return 503
            await engine.submit([update])
        except Exception:
            log.exception("webhook update %s could not be stored", uid)
            return 503
        if durable:
            return 200
        # nothing on disk: answer only once the handlers are through, or the server keeps it
        done = await engine.wait_for(uid, ack_timeout)
        return 200 if done else 503

    # --- feeding updates by hand ---

    async def _process_now(self, update: Update | Mapping[str, Any]) -> None:
        if self.me is None:
            self.me = await self._aapi.get_me()
        await self._process(update if isinstance(update, Update) else Update.from_json(update))


# --- Bot: async handlers ---


class Bot(_BotBase):
    """A Hidesay bot whose handlers are ``async def`` functions.

    Give it a token, register handlers, and call `run`: the bot long-polls the server, runs the
    handler that matches each update, and stops cleanly on Ctrl+C or ``SIGTERM``. The updates
    of one chat are handled one at a time, in order; different chats at the same time. In an
    end-to-end encrypted chat the bot opens incoming messages and seals what it sends on its
    own, so handlers read and write plain text. For plain ``def`` handlers, use `SyncBot`: it
    takes the same options and has the same methods.

    A bot is a `Router`: the decorators that register handlers (`Router.on_message`,
    `Router.command`, `Router.on_callback_query`, `Router.on_error`, ...) and
    `Router.include_router` come from there. Its own methods cover the whole Bot API, with the
    OpenPGP part done for you, and `api` is the low-level client underneath. Besides the errors
    each method lists, every method that calls the server can raise the ones in
    :ref:`common-errors`.

    It is also an async context manager: ``async with Bot() as bot:`` calls `start`, and `stop`
    at the end, so a script can use the bot's methods without polling.

    Example:
        An echo bot; the token comes from ``HIDESAY_BOT_TOKEN``::

            from hidesay_bot import Bot

            bot = Bot()


            @bot.on_message()
            async def echo(message):
                await message.reply(message.text or "...")


            bot.run()
    """

    _flavour = "async"

    @property
    def api(self) -> AsyncClient:
        """The low-level `AsyncClient` underneath the bot.

        It has one method per operation of the Bot API, and shares the bot's connections and
        rate limiter. It sends what you give it as it is: in an encrypted chat, nothing is
        sealed or opened for you.
        """
        return self._aapi

    def _bind_message(self, message: MessageBase) -> Message:
        return Message.bind(message, self)

    def _bind_query(self, query: CallbackQueryBase) -> CallbackQuery:
        return CallbackQuery.bind(query, self)

    # --- running ---

    def run(
        self,
        *,
        poll_timeout: int = 50,
        limit: int = 100,
        drop_webhook: bool = False,
        skip_pending: bool = False,
        configure_logging: bool = True,
    ) -> None:
        """Long-polls the server until Ctrl+C or ``SIGTERM``, then stops cleanly.

        Blocks until the bot has stopped. First the bot starts: it reads ``GET /me`` (waiting
        for the server if it is down), checks its key, opens the storage and runs the
        `on_startup` hooks. Then it asks the server for updates and runs their handlers until
        it is told to stop: Ctrl+C, ``SIGTERM``, Ctrl+Break on Windows, or `request_stop`. Then
        it stops as `stop` does. Network failures, server errors and a paused bot are waited
        out; see :doc:`/guides/long-polling`.

        Args:
            poll_timeout: How long each poll may wait on the server for something to arrive,
                0 to 50 seconds.
            limit: How many updates one poll may bring, 1 to 100.
            drop_webhook: ``True`` removes a webhook that is set, and polls. Without it, a set
                webhook stops the bot with `PollingError`.
            skip_pending: ``True`` throws away, without handling them, the updates that waited
                on the server while the bot was off. See the warning below.
            configure_logging: ``True`` sets up logging at ``INFO`` level, if your program has
                not set up logging itself.

        Raises:
            AuthenticationError: The token is wrong or was revoked.
            PollingError: A webhook is set and ``drop_webhook`` is ``False``, or one was set
                while the bot was polling.
            PollingConflictError: Another process polls this bot with the same `SQLiteStorage`
                file, or the storage is in use by another running bot in this process.
            ConfigurationError: A handler, middleware, error handler or hook is a plain
                function: a `Bot` needs ``async def``.
            RuntimeError: It was called where an event loop is running already (inside an
                ``async def`` function, in a Jupyter cell): ``run()`` makes its own loop.
                Nothing is sent. Use `start_polling` there.

        Example:
            Take over from a webhook, and skip what queued while the bot was off::

                bot.run(drop_webhook=True, skip_pending=True)

        Warning:
            ``skip_pending=True`` confirms the waiting updates to the server, which deletes
            them for good. Updates already in a `SQLiteStorage` still run.
        """
        if configure_logging:
            _basic_logging()
        try:
            with asyncio.Runner() as runner:
                runner.run(
                    self.start_polling(
                        poll_timeout=poll_timeout, limit=limit, drop_webhook=drop_webhook, skip_pending=skip_pending
                    )
                )
        except KeyboardInterrupt:
            pass

    async def start_polling(
        self, *, poll_timeout: int = 50, limit: int = 100, drop_webhook: bool = False, skip_pending: bool = False
    ) -> None:
        """Long-polls the server inside your own event loop, until the bot is stopped.

        It does what `run` does, without making an event loop or setting up logging. Start it
        as a task. It returns once the bot has stopped: after `stop` or `request_stop`, on
        ``SIGTERM`` (in the main thread), or when the task is cancelled.

        Args:
            poll_timeout: How long each poll may wait on the server for something to arrive,
                0 to 50 seconds.
            limit: How many updates one poll may bring, 1 to 100.
            drop_webhook: ``True`` removes a webhook that is set, and polls. Without it, a set
                webhook stops the bot with `PollingError`.
            skip_pending: ``True`` throws away, without handling them, the updates that waited
                on the server while the bot was off. The server deletes them for good.

        Raises:
            AuthenticationError: The token is wrong or was revoked.
            PollingError: A webhook is set and ``drop_webhook`` is ``False``, or one was set
                while the bot was polling.
            PollingConflictError: Another process polls this bot with the same `SQLiteStorage`
                file, or the storage is in use by another running bot in this process.
            ConfigurationError: A handler, middleware, error handler or hook is a plain
                function: a `Bot` needs ``async def``.

        Example:
            Poll in the background while the program does other work::

                async def main():
                    polling = asyncio.create_task(bot.start_polling())
                    await other_work()
                    await bot.stop()          # lets the running handlers finish
                    await polling
        """
        await self._run_polling(poll_timeout=poll_timeout, limit=limit, drop_webhook=drop_webhook, skip=skip_pending)

    async def start(self) -> None:
        """Starts the bot without polling: for a webhook, or to use its methods from a script.

        It reads ``GET /me`` (waiting for the server if it is down), checks the bot's key (and
        publishes it with ``publish_key=True``), opens the storage, runs the `on_startup` hooks
        and, if `create_webhook_app` was given a ``url``, registers the webhook. Handlers then
        run for the updates the webhook app receives. Stop the bot with `stop`;
        ``async with bot:`` calls both.

        Raises:
            AuthenticationError: The token is wrong or was revoked.
            ConfigurationError: A handler, middleware, error handler or hook is a plain
                function: a `Bot` needs ``async def``.
            InvalidKeyError: ``publish_key=True`` and the key is a v6 key, which phones can't
                use.
            StorageError: The storage can't be opened.
            BadWebhookError: The server refused the webhook ``url``.

        Example:
            Send from a script, into an encrypted chat too::

                async def main():
                    async with Bot(pgp_private_key="bot-key.asc", pgp_passphrase=passphrase) as bot:
                        await bot.send_message("48213907", "Nightly job done")


                asyncio.run(main())
        """
        await self._start_webhook()

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._shutdown()

    async def stop(self) -> None:
        """Stops the bot, and returns once it has stopped.

        It stops taking updates (a poll in progress is dropped, a webhook app answers ``503``),
        waits up to ``shutdown_timeout`` seconds for the running handlers and cancels the ones
        still running (their updates run again at the next start), runs the `on_shutdown`
        hooks, and closes the storage and the connections. On a bot that is not running, it
        only closes the connections.

        Called from code that runs for an update (a handler, a middleware, an error handler),
        it only asks for the stop, as `request_stop` does, and returns at once: waiting for
        the running handlers would mean waiting for itself.
        """
        if self._engine is not None and self._engine.inside():
            self.request_stop()
            return
        await self._shutdown()

    async def process_update(self, update: Update | Mapping[str, Any]) -> None:
        """Runs one update through decryption, filters and handlers, now.

        Meant for tests, and for updates that come from somewhere else. The update skips the
        queue: nothing is stored, nothing is confirmed to the server, and a repeat is not
        recognised. The bot does not need to be started, but this still calls the API: for
        ``GET /me`` if `me` is not known yet, for what the handlers send, and for the automatic
        answer to a button press.

        Args:
            update: An `Update`, or one in the JSON form the server sends (a dict with
                ``updateId`` and ``kind``).

        Raises:
            Exception: What a handler raised, or what opening an encrypted message raised,
                when no error handler took it.

        Example:
            In a test, with the bot on a fake transport or a test server::

                await bot.process_update({
                    "updateId": 1,
                    "kind": "message",
                    "message": {
                        "id": 10,
                        "chat": {"id": "48213907", "kind": "private"},
                        "from": {"id": "48213907", "username": "anna"},
                        "date": 1791462000000,
                        "kind": "user",
                        "text": "/start",
                    },
                })
        """
        await self._process_now(update)

    # --- dispatch ---

    async def _dispatch(self, update: Update, data: dict[str, Any], error: BaseException | None) -> None:
        if error is not None:
            await self._handle_error(error, data)
            return
        try:
            await self._route(update, data)
        except Exception as exc:  # noqa: BLE001
            await self._handle_error(exc, data)

    async def _route(self, update: Update, data: dict[str, Any]) -> bool:
        context = self._filter_context(update)
        for handler, middlewares in self.iter_handlers(update.kind):
            event = self._event_for(update, handler.kind)
            extra: dict[str, Any] = {}
            if not await self._passes(handler, event, context, extra):
                continue
            try:
                await self._call(handler, middlewares, event, {**data, **extra})
            except SkipHandler:
                continue
            return True
        log.debug("update %s (%s): no handler took it", update.update_id, update.kind)
        return False

    @staticmethod
    async def _passes(handler: Handler, event: Any, context: FilterContext, extra: dict[str, Any]) -> bool:
        for f in handler.filters:
            result = f(event, context)
            if inspect.isawaitable(result):
                result = await result
            if not merge(result, extra):
                return False
        return True

    @staticmethod
    async def _call(handler: Handler, middlewares: Sequence[Any], event: Any, data: dict[str, Any]) -> Any:
        async def last(ev: Any, d: dict[str, Any]) -> Any:
            return await handler.callback(ev, **handler.kwargs_for(d))

        call: Callable[[Any, dict[str, Any]], Awaitable[Any]] = last
        for mw in reversed(middlewares):
            call = functools.partial(_async_step, mw, call)
        return await call(event, data)

    async def _handle_error(self, error: BaseException, data: dict[str, Any]) -> None:
        for eh in self.iter_error_handlers():
            if not eh.matches(error):
                continue
            try:
                await eh.callback(error, **eh.kwargs_for({**data, "error": error}))
            except SkipHandler:
                continue
            return
        raise error

    # --- the API, with OpenPGP done for you ---

    async def get_me(self, *, refresh: bool = False) -> BotInfo:
        """Reads the bot's own details (``GET /me``).

        The copy in `me` is returned without asking the server, unless there is none yet or
        ``refresh`` is ``True``. ``GET /me`` works while the owner has paused the bot, so it is
        the way to find out why nothing else does.

        Args:
            refresh: ``True`` asks the server even when a copy is kept, and keeps the new one.

        Returns:
            The bot's `BotInfo`, with its handle, mode, status and the server's limits.
        """
        if self.me is None or refresh:
            self.me = await self._aapi.get_me()
        return self.me

    async def get_chat(self, chat: ChatLike, *, refresh: bool = False) -> Chat:
        """Reads a chat the bot is in (``GET /chats/{id}``).

        The answer is cached for ``cache_ttl`` seconds; ``refresh=True`` asks the server again.

        Args:
            chat: The chat's ID, or a chat object such as a `Chat`, a `ChatRef` or
                ``message.chat``.
            refresh: ``True`` skips the cache.

        Returns:
            The `Chat`, with its kind, its mode (``open`` or ``protected``) and, in a group or
            channel, the bot's own seat.

        Raises:
            ChatNotFoundError: There is no such chat, or the bot is not in it.
        """
        chat_id = _chat_id(chat)
        if not refresh:
            cached = self._chats.get(chat_id)
            if cached is not None:
                return cached  # type: ignore[no-any-return]
        info = await self._aapi.get_chat(chat_id)
        self._note_chat(info)
        return info

    async def _mode(self, chat_id: str) -> tuple[ChatMode, ChatKind]:
        return await self._amode(chat_id)

    async def _body(self, mode: ChatMode, chat_id: str, text: str, encrypt: bool | None) -> dict[str, str]:
        if self._body_kind(mode, chat_id, encrypt) == "armored":
            return {"armored": await self.seal(chat_id, text)}
        return {"text": text}

    async def _mode_changed(self, chat_id: str, kind: ChatKind) -> None:
        self._forget_chat(chat_id)
        if kind == ChatKind.PRIVATE:
            with contextlib.suppress(APIError, NetworkError):
                self.me = await self._aapi.get_me()

    async def recipients(self, chat: ChatLike) -> list[PublicKey]:
        """Lists the checked keys a message into this end-to-end encrypted chat is sealed to.

        The keys come from ``GET /chats/{id}/keys``, cached for ``cache_ttl`` seconds, and each
        one is checked: its fingerprint must match the one the server gives, it must not be
        revoked or expired, it must have a live encryption subkey, and it must be a v4 key
        (phones can't open what is sealed to a v6 key). In a group, a member whose key fails a
        check is left out with a warning in the log, as the app does; in a private chat, it
        raises `EncryptionError`. The bot's own key is not in the list: `seal` adds it.

        Args:
            chat: The chat's ID, or a chat object such as a `Chat`, a `ChatRef` or
                ``message.chat``.

        Returns:
            The `PublicKey` of every member the message can be sealed to.

        Raises:
            EncryptionError: Nobody in the chat has a usable key, or the key of the person in
                a private chat can't be used.
            KeyChangedError: With ``pin_keys=True``, a member's key is not the one pinned for
                them.
            ChatIsOpenError: The chat is readable, not end-to-end encrypted.
            NoUserKeyError: The person in a private chat has no published key.
        """
        chat_id = _chat_id(chat)
        keys = await self._fetch_chat_keys(chat_id)
        kind = self._known_kind(chat_id)
        return await asyncio.to_thread(self._recipients_from, chat_id, keys, kind == ChatKind.PRIVATE)

    async def seal(self, chat: ChatLike, text: str) -> str:
        """Encrypts ``text`` for everyone in an end-to-end encrypted chat, signed by the bot.

        Nothing is sent. The text is sealed to the keys `recipients` gives and to the bot's own
        key, and signed with the bot's key: what `send_message` does before sending, with the
        same checks. Unlike `send_message`, the text is not checked against the server's
        limits.

        Args:
            chat: The chat's ID, or a chat object such as a `Chat`, a `ChatRef` or
                ``message.chat``.
            text: The text to encrypt.

        Returns:
            The ASCII-armored OpenPGP message, to send as ``armored``.

        Raises:
            EncryptionError: The bot has no private key, or the message can't be sealed to the
                chat's keys.
            KeyChangedError: With ``pin_keys=True``, a member's key is not the one pinned for
                them.
            ChatIsOpenError: The chat is readable, not end-to-end encrypted.

        Example:
            What `send_message` does, by hand::

                armored = await bot.seal(chat_id, "draft")
                await bot.api.send_message(chat_id, armored=armored)
        """
        chat_id = _chat_id(chat)
        key = self._need_key(chat_id)
        recipients = await self.recipients(chat_id)
        return await asyncio.to_thread(key.seal, text, recipients)

    async def send_message(
        self,
        chat: ChatLike,
        text: str,
        *,
        reply_to: int | MessageBase | None = None,
        markup: InlineKeyboard | Mapping[str, Any] | None = None,
        silent: bool = False,
        encrypt: bool | None = None,
        send_id: str | uuid.UUID | None = None,
    ) -> SentMessage:
        """Sends a text message, sealed and signed when the chat is end-to-end encrypted.

        Before sending, the bot finds out the chat's mode, and asks the server when it does not
        know it yet. Into a readable chat the text goes as it is. Into an encrypted chat it is
        sealed to every member with a usable key and to the bot, and signed with the bot's key
        (see `recipients`). If it can't be sealed, nothing is sent: the bot never falls back to
        plain text.

        The mode can change between the check and the send. If the chat became encrypted, the
        server refuses the text, and the bot seals it and sends it again with the same
        ``send_id``; a warning is logged, because the text did reach the server in the refused
        request. If the chat became readable, the server refuses the sealed message, and it is
        not sent again in clear: this raises, and the next send follows the new mode.

        Args:
            chat: The chat's ID, or a chat object such as a `Chat`, a `ChatRef` or
                ``message.chat``.
            text: The text: 1 to `text_max` (4096) characters, counted in UTF-16 units as
                JavaScript does. ``CR LF`` and ``CR`` line ends become ``LF``; other control
                characters, except tabs, are refused.
            reply_to: The message to quote: its ID, or the message itself. The server ignores
                a wrong ID.
            markup: Buttons under the message: an `InlineKeyboard`, or the same in the JSON
                form of the API. Never encrypted: see the warning below.
            silent: ``True`` posts without a notification. It only matters in channels.
            encrypt: ``None`` follows the chat. ``True`` refuses to send in clear
                (`EncryptionRequiredError` in a readable chat); ``False`` refuses to send
                sealed (`ValidationError` in an encrypted chat).
            send_id: A UUID that makes the send safe to repeat: the server writes one message
                per ``send_id``, and answers a repeat with the message it already wrote. One is
                made for you; pass your own, saved with your job, to make your own retries safe
                too.

        Returns:
            The `SentMessage` as the server stored it, which can be edited or deleted.

        Raises:
            ValidationError: The text, the buttons or an ID breaks a rule, or
                ``encrypt=False`` in an encrypted chat. Nothing was sent.
            EncryptionRequiredError: The chat is readable, and ``encrypt=True`` or
                ``require_encryption=True``. Nothing was sent.
            EncryptionError: The chat is encrypted and the message can't be sealed: the bot
                has no private key, nobody has a usable key, or the key of the person in a
                private chat can't be used. Nothing was sent.
            KeyChangedError: With ``pin_keys=True``, a member's key is not the one pinned for
                them. Nothing was sent.
            WrongBodyKindError: The chat became readable since the bot last looked. The sealed
                message was refused, and is not sent again in clear.
            ChatIsOpenError: The same, noticed while fetching the members' keys.
            ForbiddenError: The bot may not write in this chat.
            BlockedByUserError: The person blocked the bot.
            UserGoneError: The person closed their account.
            ChatNotFoundError: There is no such chat, or the bot is not in it.
            SendIdConflictError: The ``send_id`` was used already, in another chat.

        Examples:
            Into any chat the bot is in::

                await bot.send_message("48213907", "Your report is ready")

            With buttons, and a ``send_id`` saved with the job, so that a job run twice still
            sends once::

                keyboard = InlineKeyboard.build(
                    [Button.callback("Monday", "day:mon"), Button.callback("Friday", "day:fri")],
                )
                await bot.send_message(
                    group_id, "Vote for the next meetup", markup=keyboard, send_id=job.send_id
                )

        Warning:
            Buttons are never encrypted: their text and their callback data travel in clear,
            even in an encrypted chat. Don't put personal data in them.

        See Also:
            `Message.reply` and `Message.answer`, which send into the chat a message came from.
        """
        chat_id = _chat_id(chat)
        text = v.check_text(text, limit=self.text_max)
        reply_id = _message_id(reply_to) if reply_to is not None else None
        send_id = send_id or uuid.uuid4()
        for attempt in range(2):
            mode, kind = await self._mode(chat_id)
            try:
                # sealing is inside: its key fetch is where a chat that turned readable shows up
                body = await self._body(mode, chat_id, text, encrypt)
                sent = await self._aapi.send_message(
                    chat_id,
                    body.get("text"),
                    armored=body.get("armored"),
                    reply_to=reply_id,
                    markup=markup,
                    silent=silent or None,
                    send_id=send_id,
                    chat_kind=kind,
                )
            except (WrongBodyKindError, ChatIsOpenError) as exc:
                await self._mode_changed(chat_id, kind)
                if attempt == 0 and getattr(exc, "expected", None) == "armored":
                    # the words went to the server in clear and were refused: the chat is
                    # encrypted now. seal them and try again
                    log.warning("chat %s turned end-to-end encrypted; sealing and sending again", chat_id)
                    continue
                raise
            return SentMessage.bind(sent, self)
        raise AssertionError("unreachable")  # pragma: no cover

    async def edit_message(
        self,
        chat: ChatLike,
        message: int | SentMessageBase,
        text: str | None = None,
        *,
        markup: InlineKeyboard | Mapping[str, Any] | None = KEEP,
        encrypt: bool | None = None,
    ) -> SentMessage:
        """Edits one of the bot's messages: new text, new buttons, or both.

        The bot can edit only its own ordinary messages, at any age, and people see the edit
        live. New text is sealed like a send when the chat is end-to-end encrypted, and a
        change of the chat's mode is handled as in `send_message`.

        Args:
            chat: The chat's ID, or a chat object such as a `Chat`, a `ChatRef` or
                ``message.chat``.
            message: The bot's message: its ID, or the `SentMessage` a send returned.
            text: The new text, with the same rules as in `send_message`. ``None`` keeps the
                text.
            markup: The buttons. :data:`KEEP`, the default, keeps them; ``None`` removes them;
                a keyboard replaces them. Never encrypted.
            encrypt: ``None`` follows the chat. ``True`` refuses to send the text in clear;
                ``False`` refuses to send it sealed.

        Returns:
            The edited message, as the server stored it.

        Raises:
            ValidationError: There is nothing to change (no ``text``, and ``markup`` left at
                :data:`KEEP`), or the text, the buttons or an ID breaks a rule.
            MessageNotFoundError: There is no such message in this chat.
            ForbiddenError: The message is not one of the bot's own ordinary messages.
            EncryptionRequiredError: The chat is readable, and ``encrypt=True`` or
                ``require_encryption=True``.
            EncryptionError: The chat is encrypted and the new text can't be sealed.
            WrongBodyKindError: The chat became readable since the bot last looked. The sealed
                text was refused, and is not sent again in clear.
            ChatIsOpenError: The same, noticed while fetching the members' keys.

        Example:
            A message that counts::

                sent = await bot.send_message(chat_id, "Counting...")
                for n in range(1, 4):
                    await asyncio.sleep(1)
                    await bot.edit_message(chat_id, sent, f"Counting... {n}")
        """
        chat_id = _chat_id(chat)
        message_id = _message_id(message)
        if text is not None:
            text = v.check_text(text, limit=self.text_max)
        for attempt in range(2):
            mode, kind = await self._mode(chat_id)
            try:
                body = await self._body(mode, chat_id, text, encrypt) if text is not None else {}
                edited = await self._aapi.edit_message(chat_id, message_id, markup=markup, chat_kind=kind, **body)
            except (WrongBodyKindError, ChatIsOpenError) as exc:
                await self._mode_changed(chat_id, kind)
                if attempt == 0 and getattr(exc, "expected", None) == "armored":
                    log.warning("chat %s turned end-to-end encrypted; sealing and sending again", chat_id)
                    continue
                raise
            return SentMessage.bind(edited, self)
        raise AssertionError("unreachable")  # pragma: no cover

    async def delete_message(self, chat: ChatLike, message: int | MessageBase | SentMessageBase) -> bool:
        """Deletes a message for everyone, for good.

        The bot can delete its own messages anywhere, and other people's in a group or channel
        where it has the "delete messages" right. Secret messages and service notices can't be
        deleted.

        Args:
            chat: The chat's ID, or a chat object such as a `Chat`, a `ChatRef` or
                ``message.chat``.
            message: The message: its ID, a received `Message`, or a `SentMessage`.

        Returns:
            ``True``. When an attempt timed out and the retry finds the message gone, the first
            attempt worked, and the result is ``True`` too.

        Raises:
            MessageNotFoundError: There is no such message, or it is gone already.
            ForbiddenError: The bot may not delete this message.
            ChatNotFoundError: There is no such chat, or the bot is not in it.
        """
        return await self._aapi.delete_message(_chat_id(chat), _message_id(message))

    async def answer_callback(
        self, callback_id: str, text: str | None = None, *, alert: bool = False, url: str | None = None
    ) -> bool:
        """Answers a button press, which stops its spinner.

        Each press can be answered once, within 60 seconds. The answer can also show a line of
        text, a box, or a link. In a handler, `CallbackQuery.answer` does the same. The bot
        remembers the answer, so it does not send its automatic empty one after the handler
        (see ``auto_answer_callbacks``).

        Args:
            callback_id: The ID of the press, ``query.id``.
            text: Up to 200 characters on one line, shown over the chat for a moment. ``None``
                shows nothing.
            alert: ``True`` shows ``text`` in a box the person has to close.
            url: An ``http`` or ``https`` address, up to 1024 characters, that the phone offers
                to open.

        Returns:
            ``True``. A retry after a timeout that finds the press answered already counts as
            done.

        Raises:
            CallbackExpiredError: More than 60 seconds have passed since the press.
            CallbackAnsweredError: The press was answered already.
            CallbackNotFoundError: There is no such press.

        Warning:
            Answers are not encrypted, even in an encrypted chat.
        """
        result = await self._aapi.answer_callback(callback_id, text, alert=alert, url=url)
        self._note_answered(callback_id)
        return result

    async def send_typing(self, chat: ChatLike, typing: bool = True) -> bool:
        """Shows "typing..." in a chat for a few seconds.

        The indicator goes away by itself, or when the bot's message arrives. The server shows
        at most one every 3 seconds per chat; a call in between does nothing and returns
        ``False``. It is not tried again after a network error: a late indicator is useless.

        Args:
            chat: The chat's ID, or a chat object such as a `Chat`, a `ChatRef` or
                ``message.chat``.
            typing: ``False`` clears the indicator early.

        Returns:
            ``True`` if the indicator was shown, ``False`` if one went out to this chat less
            than 3 seconds ago.

        Raises:
            ForbiddenError: The bot may not write in this chat.
            ChatNotFoundError: There is no such chat, or the bot is not in it.
        """
        return await self._aapi.send_typing(_chat_id(chat), typing)

    async def get_chat_keys(self, chat: ChatLike) -> ChatKeys:
        """Reads the public keys of an end-to-end encrypted chat, as the server lists them.

        They come straight from ``GET /chats/{id}/keys``: not cached, and not checked.
        `recipients` gives the checked keys a message is sealed to. The bot's own key is not in
        the list, and other bots are in it only when the room lets them read every message, as
        for the app.

        Args:
            chat: The chat's ID, or a chat object such as a `Chat`, a `ChatRef` or
                ``message.chat``.

        Returns:
            The `ChatKeys`, with one `UserKey` per member who has a key and, in a group, the
            member count.

        Raises:
            ChatIsOpenError: The chat is readable, not end-to-end encrypted.
            NoUserKeyError: The person in a private chat has no published key.
            ChatNotFoundError: There is no such chat, or the bot is not in it.
        """
        return await self._aapi.get_chat_keys(_chat_id(chat))

    async def get_chat_members(self, chat: ChatLike) -> ChatMembers:
        """Reads the member list of a group or channel, as a member in the bot's seat sees it.

        Where the member list is hidden, a bot that is not an admin sees the admins only.
        Phone numbers are never included.

        Args:
            chat: The group or channel: its ID, or a chat object such as a `Chat`, a `ChatRef`
                or ``message.chat``.

        Returns:
            The `ChatMembers`, with the members the bot may see and how many members there are
            in all.

        Raises:
            BadRequestError: The chat is a private chat.
            ChatNotFoundError: There is no such chat, or the bot is not in it.
        """
        return await self._aapi.get_chat_members(_chat_id(chat))

    async def get_chat_member(self, chat: ChatLike, user_id: str | int) -> Member:
        """Reads one member's seat in a group or channel.

        Args:
            chat: The group or channel: its ID, or a chat object such as a `Chat`, a `ChatRef`
                or ``message.chat``.
            user_id: The person's ID: ``"48213907"``, or a bot's ``"B48213907"``.

        Returns:
            The `Member`, with the person, their status and their admin rights.

        Raises:
            MemberNotFoundError: The person is not a member, or is hidden from the bot.
            BadRequestError: The chat is a private chat.
            ChatNotFoundError: There is no such chat, or the bot is not in it.
        """
        return await self._aapi.get_chat_member(_chat_id(chat), user_id)

    async def leave_chat(self, chat: ChatLike) -> bool:
        """Leaves a group or channel; its members are told.

        The bot also forgets what it had cached about the chat.

        Args:
            chat: The group or channel: its ID, or a chat object such as a `Chat`, a `ChatRef`
                or ``message.chat``.

        Returns:
            ``True``. A retry after a timeout that finds the bot gone already counts as done.

        Raises:
            BadRequestError: The chat is a private chat, which can't be left.
            ChatNotFoundError: There is no such chat, or the bot is not in it.
        """
        chat_id = _chat_id(chat)
        left = await self._aapi.leave_chat(chat_id)
        self._forget_chat(chat_id)
        return left

    async def resolve(self, username: str) -> Resolved:
        """Turns a public handle into an ID (``GET /resolve/{username}``).

        For the handle somebody writes at the bot, when no update has handed that ID over.
        People, groups and channels share one pool of names, so a handle belongs to exactly
        one of the three: `Resolved.kind` says which and `Resolved.id` is filled in either
        way. A leading ``@`` and the case do not matter.

        Nothing is unlocked by knowing the number. Sending still needs the bot to be in the
        chat, and `Bot.get_user_key` still needs somebody the bot may look up.

        Args:
            username: The handle, with or without the ``@``.

        Returns:
            A `Resolved`: the ID, the handle it goes by, and either the person (`ChatPeer`)
            or the chat (`ChatCard`).

        Raises:
            ValidationError: Not a handle: 3-32 characters, letters, digits and underscore.
            UsernameNotFoundError: Nobody and no chat answers to it. Also the answer for a
                group with no public name and for an account that has been closed.
            FloodWaitError: More than 60 lookups this minute, where a handle nobody holds
                counts as five.

        Example:
            Answer "chi e @anna"::

                @bot.on_text()
                async def who(message: Message) -> None:
                    found = await bot.resolve(message.text.split("@")[-1])
                    await message.reply(f"{found.mention} ha id {found.id}")
        """
        return await self._aapi.resolve(username)

    async def get_user_key(self, user_id: str | int) -> UserKey:
        """Reads one person's public key (``GET /users/{id}/key``).

        The key is not checked here: `PublicKey.from_user_key` reads it and checks its
        fingerprint.

        Args:
            user_id: The person's ID (``"48213907"``, or a bot's ``"B48213907"``). The bot must
                be able to look them up, as for ``get_user``.

        Returns:
            The `UserKey`, with the user ID, the fingerprint and the armored public key.

        Raises:
            UserNotFoundError: There is no such person, or the bot shares no chat with them.
            NoUserKeyError: The person never published a key.
        """
        return await self._aapi.get_user_key(user_id)

    async def get_commands(self, scope: str | None = None) -> Any:
        """Reads the bot's command menus: every scope, or one.

        Args:
            scope: ``None`` for every scope, or one of ``"default"``, ``"private"``,
                ``"groups"``, ``"groupAdmins"`` and ``"chat:<id>"`` (``"chat:G48213907"``).

        Returns:
            With no ``scope``, a list of `CommandScope`, one per scope with its commands. With
            a ``scope``, the list of `Command` of that scope.
        """
        return await self._aapi.get_commands(scope)

    async def set_commands(
        self, commands: Iterable[Command | Mapping[str, Any] | tuple[str, str]], scope: str | None = None
    ) -> CommandScope:
        """Replaces the whole command menu of one scope.

        The app shows the menu when someone types ``/``. The owner can change the same lists
        in the app: whoever writes last wins. `sync_commands` builds the list from the bot's
        own handlers.

        Args:
            commands: Up to 100 commands: `Command` objects, dicts with ``command`` and
                ``description``, or ``(name, description)`` pairs. A name has 1 to 32
                characters of ``a-z``, ``0-9`` and ``_`` (a leading ``/`` is dropped, and
                capitals are made lowercase); a description has 1 to 256 characters. When a
                name comes twice, the first one is kept.
            scope: Which menu: ``"default"`` (also when ``None``), ``"private"``,
                ``"groups"``, ``"groupAdmins"`` or ``"chat:<id>"`` (``"chat:G48213907"``).

        Returns:
            The `CommandScope` as the server saved it.

        Raises:
            ChatNotFoundError: A ``chat:G…`` or ``chat:C…`` scope names a chat the bot is not in.

        Example:
            A menu for every chat, and one for a single group::

                await bot.set_commands([("start", "Say hello"), ("help", "What I can do")])
                await bot.set_commands([("rules", "Show the rules")], scope="chat:G48213907")
        """
        return await self._aapi.set_commands(commands, scope)

    async def delete_commands(self, scope: str | None = None) -> CommandScope:
        """Empties the command menu of one scope.

        An empty scope does not hide the wider ones: the app shows the next scope that has
        commands, ``"default"`` last.

        Args:
            scope: Which menu: ``"default"`` (also when ``None``), ``"private"``,
                ``"groups"``, ``"groupAdmins"`` or ``"chat:<id>"`` (``"chat:G48213907"``).

        Returns:
            The `CommandScope`, now with no commands.
        """
        return await self._aapi.delete_commands(scope)

    async def sync_commands(self, scope: str | None = None) -> CommandScope:
        """Publishes the commands registered with a description as the menu of one scope.

        Every `Router.command` with a ``description``, on the bot and on its included routers,
        goes into the list; when a name comes twice, the first description wins. It replaces
        the whole list of that scope, as `set_commands` does. An `on_startup` hook is a good
        place to call it.

        Args:
            scope: Which menu: ``"default"`` (also when ``None``), ``"private"``,
                ``"groups"``, ``"groupAdmins"`` or ``"chat:<id>"`` (``"chat:G48213907"``).

        Returns:
            The `CommandScope` as the server saved it.

        Raises:
            ConfigurationError: No command was registered with a description.

        Example:
            Publish the menu at every start::

                @bot.command("weather", description="Forecast for a town")
                async def weather(message, command): ...


                @bot.on_startup
                async def ready(bot):
                    await bot.sync_commands()
        """
        declared = self.declared_commands()
        if not declared:
            raise ConfigurationError("no command was registered with a description")
        return await self._aapi.set_commands(declared, scope)

    async def get_webhook(self) -> WebhookInfo:
        """Reads the webhook, if one is set, and how its deliveries are going.

        Works while the owner has paused the bot.

        Returns:
            The `WebhookInfo`, with the address, whether it has a secret, how many updates are
            waiting, and the failures and last error of the deliveries.
        """
        return await self._aapi.get_webhook()

    async def set_webhook(self, url: str, secret: str | None = None) -> WebhookInfo:
        """Sets a webhook: from now on the server POSTs every update to ``url``.

        Setting it ends any poll in progress, and the server refuses polls while it is set.
        You rarely need this: `create_webhook_app` with a ``url`` registers the webhook every
        time the bot starts.

        Args:
            url: The address: ``https``, up to 1024 characters, with no user name, password or
                ``#fragment``, pointing to a public IP address.
            secret: 1 to 256 characters of ``A-Z a-z 0-9 _ -``, sent back in the
                ``X-Hidesay-Bot-Api-Secret-Token`` header of every delivery. Always set one:
                without it, anyone who finds the address can send fake updates.

        Returns:
            The `WebhookInfo` of the new webhook.

        Raises:
            BadWebhookError: The server refused the address, for example because it points to
                a private IP address.
        """
        return await self._aapi.set_webhook(url, secret)

    async def delete_webhook(self) -> WebhookInfo:
        """Removes the webhook, so the bot can poll again.

        The updates that queued meanwhile are not lost: the next poll gets them. Works while
        the owner has paused the bot.

        Returns:
            The `WebhookInfo`, now with no ``url``; ``pending`` counts the waiting updates.
        """
        return await self._aapi.delete_webhook()

    async def publish_key(self, key: BotKey | None = None) -> PublishedKey:
        """Publishes the bot's public key, which makes its private chats end-to-end encrypted.

        It sends the public part of the key (``PUT /key``) with a fingerprint computed from the
        key itself, so the two always match: a wrong one would make phones seal to a
        key the bot does not have. A v6 key is refused before anything is sent, because phones
        can't use it. Afterwards `me` is read again and shows the new mode. Publishing does not
        change the key the bot uses: to switch to a new key, use `rotate_key`.

        Args:
            key: The key to publish. ``None`` publishes the bot's own key.

        Returns:
            The `PublishedKey`, with the fingerprint and the bot's new mode.

        Raises:
            ConfigurationError: There is no key to publish.
            InvalidKeyError: The key is a v6 key.

        Warning:
            Publishing is one-way: there is no API to remove the key. Only the bot's owner can
            make the bot readable again, from the console in the app.
        """
        key = key or self.key
        if key is None:
            raise ConfigurationError("no key to publish: pass one or set pgp_private_key")
        return await self._publish(key)

    async def rotate_key(self, new_key: BotKey) -> PublishedKey:
        """Publishes ``new_key`` and makes it the bot's key.

        From then on, phones seal to the new key. Messages sealed to the old one may still be
        on their way, or waiting on the server, for up to 72 hours: the old key stays loaded,
        and is still tried for decryption as long as this process runs. When you restart the
        bot, pass the old key in ``pgp_previous_keys`` for at least 72 hours.

        Args:
            new_key: The new private key. `BotKey.generate` makes one of the kind phones can
                use.

        Returns:
            The `PublishedKey`, with the new fingerprint.

        Raises:
            InvalidKeyError: ``new_key`` is a v6 key, which phones can't use.

        Example:
            Make a new key, save it, and switch to it::

                new_key = BotKey.generate("Weather", username="weatherbot", passphrase=passphrase)
                new_key.save("bot-key-2026-11.asc", passphrase=None)
                await bot.rotate_key(new_key)
        """
        result = await self._publish(new_key)
        if self.key is not None and self.key.fingerprint != new_key.fingerprint:
            self._previous_keys.insert(0, self.key)
        self.key = new_key
        return result

    async def revoke_token(self) -> bool:
        """Burns the bot's token, for good.

        From then on every call with this token fails, the bot's own included, until the owner
        makes a new token in the console; the owner is told at once. Use it when the token has
        leaked. It works while the owner has paused the bot.

        Returns:
            ``True``. A retry after a timeout that finds the token burnt already counts as
            done.

        Warning:
            There is no undo: every call made with the token fails from then on, with
            `AuthenticationError`, and a polling bot stops.
        """
        return await self._aapi.revoke_token()

    async def validate_web_bot_data(
        self, init_data: str, *, max_age: float | None = WEB_BOT_DATA_MAX_AGE
    ) -> WebBotData:
        """Checks the ``initData`` a page of this bot's Web-Bot sent, and reads it.

        The bot's ID and the server's Web-Bot key are fetched the first time and kept, so
        after that the check runs without any network. See
        `~hidesay_bot.validate_web_bot_data` for what is checked.

        Args:
            init_data: The string as the page sent it (``window.Hidesay.WebBot.initData``).
            max_age: The oldest data accepted, in seconds since it was signed, or ``None`` to
                accept any age.

        Returns:
            What the data says, now that it is known to come from the server.

        Raises:
            WebBotDataError: The data is refused; ``reason`` says why.

        Example:
            In a route of any web framework::

                try:
                    data = await bot.validate_web_bot_data(request.headers["x-init-data"])
                except WebBotDataError:
                    return Response(status_code=401)
                cart = carts[data.user.id]
        """
        me = await self.get_me()
        if self._web_bot_key is None:
            self._web_bot_key = await self._aapi.get_web_bot_key()
        return validate_web_bot_data(init_data, bot_id=me.id, public_key=self._web_bot_key, max_age=max_age)

    async def download_media(self, media: Media | str, *, max_bytes: int = MEDIA_MAX_BYTES) -> bytes:
        """Downloads the file behind a `Media`, decrypted when it came from an encrypted chat.

        The file comes from the ``/media/<id>`` address of the API server; the token is not
        sent, because that address does not use it. An attachment of an encrypted chat is
        decrypted with the bot's key. In a private chat with the bot this always works. In an
        encrypted group the phones seal attachments only to the keys of the group, which have
        the bot among them only when the room lets it read every message (`Seat.reads_all`);
        otherwise it raises `NotARecipientError`. When the message gave the file's SHA-256
        (`Media.sha256`), the file is checked against it before it is handed over.

        Args:
            media: An item of ``message.media``. A ``/media/<id>`` path or a bare ID works too,
                but is downloaded as it is, never decrypted: pass the `Media` to get it
                decrypted.
            max_bytes: The largest file accepted, in bytes, 64 MiB by default. It also limits
                the decrypted size.

        Returns:
            The file's bytes, decrypted when needed.

        Raises:
            NotARecipientError: The file was not sealed to any of the bot's keys.
            DecryptionError: The bot has no private key, or the file can't be decrypted: it is
                damaged, it opens to more than ``max_bytes``, or it is compressed. Phones never
                compress attachments, and a compressed file could open to many gigabytes.
            ValidationError: The file is bigger than ``max_bytes``, or the address is not a
                media address of this server.
            MediaIntegrityError: The file's SHA-256 is not the one its message gives.
            NotFoundError: The server has no such file.

        Example:
            Save every attachment of a message::

                @bot.on_message(f.has_media)
                async def save(message):
                    for item in message.media:
                        data = await bot.download_media(item)
                        Path(f"{item.id}.bin").write_bytes(data)

        Note:
            The message that carried the attachment had its signature checked, but the file
            itself is not checked against a signature.
        """
        data = await self._aapi.download_media(media, max_bytes=max_bytes)
        if isinstance(media, Media) and media.encrypted:
            result = await asyncio.to_thread(self._decrypt_any, data, [], max_size=max_bytes, allow_compression=False)
            data = result.data
        return self._check_integrity(media, data)

    async def trust_key(self, user_id: str | int, fingerprint: str) -> None:
        """Accepts ``fingerprint`` as the key of ``user_id`` from now on.

        With ``pin_keys=True`` the bot refuses a person's key when it is not the one pinned for
        them, with `KeyChangedError`. Once you have checked the new key with the person, out of
        band, call this to pin it: sends to them work again, and their new messages reach the
        handlers. Their messages that failed meanwhile wait in the failed list, and
        `retry_failed` runs them again. The pin is saved in the storage.

        Args:
            user_id: The person's ID: ``"48213907"``, or a bot's ``"B48213907"``.
            fingerprint: The fingerprint you checked: 40 (v4) or 64 (v6) hex digits, without
                spaces.

        Raises:
            ValidationError: ``fingerprint`` is not 40 or 64 hex digits.

        Example:
            After checking the fingerprint with the person::

                await bot.trust_key(user_id, "3F1A9C0D52E7B8A46C110D93E2F47A559B10C6D2")

        Note:
            The bot opens a `SQLiteStorage` when it starts and closes it when it stops: with
            one, call this while the bot runs (from a handler, from a hook, or after `start`).
        """
        await asyncio.to_thread(self.storage.set_pin, v.check_user_id(user_id), v.check_fingerprint(fingerprint))
        self._keys.drop()

    async def failed_updates(self, limit: int = 100) -> list[FailedUpdate]:
        """Lists the updates whose handlers failed, oldest first.

        An update fails when a handler raises and no error handler takes the error. It is
        confirmed to the server like any other, so it does not block the queue, and kept in
        the failed list with the error. For an update from an encrypted chat, a handler's own
        exception is kept as its type and the place it was raised, without its message, which
        could quote the decrypted text. The list keeps the newest ones only: 1000 with a
        `MemoryStorage`, 10 000 per bot with a `SQLiteStorage` (their ``max_failed`` option).

        Args:
            limit: The most updates to return.

        Returns:
            The failed updates, each a `FailedUpdate` with ``update_id``, ``payload`` (the
            update as the server sent it), ``error`` and ``failed_at``.

        Example:
            Look at what failed::

                for item in await bot.failed_updates():
                    print(item.update_id, item.failed_at, item.error)

        Note:
            The bot opens a `SQLiteStorage` when it starts and closes it when it stops: with
            one, call this while the bot runs (from a handler, from a hook, or after `start`).
        """
        return await asyncio.to_thread(self.storage.failed, limit)

    async def retry_failed(self, update_ids: Sequence[int] | None = None) -> int:
        """Puts failed updates back in the queue, to run through the handlers again.

        They run as if they had just arrived: at once when the bot is running, otherwise at
        its next start. A common pattern is to fix the bug, restart the bot, and call this.

        Args:
            update_ids: The IDs of the updates to retry. ``None`` retries all of them.

        Returns:
            How many updates were put back.

        Note:
            The bot opens a `SQLiteStorage` when it starts and closes it when it stops: with
            one, call this while the bot runs (from a handler, from a hook, or after `start`).
        """
        count = await asyncio.to_thread(self.storage.requeue_failed, update_ids)
        if self._engine is not None and count:
            await self._engine.recover(quiet=True)
        return count

    async def discard_failed(self, update_ids: Sequence[int] | None = None) -> int:
        """Forgets failed updates, for good.

        Args:
            update_ids: The IDs of the updates to forget. ``None`` forgets all of them.

        Returns:
            How many updates were forgotten.

        Note:
            The bot opens a `SQLiteStorage` when it starts and closes it when it stops: with
            one, call this while the bot runs (from a handler, from a hook, or after `start`).
        """
        return await asyncio.to_thread(self.storage.discard_failed, update_ids)

    # --- files, stickers, forwards, comments ---

    async def upload_file(self, data: bytes | str | os.PathLike[str]) -> UploadedFile:
        """Uploads a file as it is, for the ``media`` of a send into a readable chat.

        `send_media` does the upload for you, sealed when the chat is encrypted: this is for
        building a send by hand with `api`. The file goes up under a neutral name and type.

        Args:
            data: The file: its bytes, or the path of a file to read.

        Returns:
            The `UploadedFile`, with the ID to send.

        Raises:
            ValidationError: The file can't be read, or is larger than `upload_max`.
            FloodWaitError: More than 30 uploads this minute.
        """
        return await self._aapi.upload_file(_bytes_of(data, "data"), limit=self.upload_max)

    async def send_media(
        self,
        chat: ChatLike,
        files: FileLike | Sequence[FileLike],
        *,
        caption: str | None = None,
        reply_to: int | MessageBase | None = None,
        markup: InlineKeyboard | Mapping[str, Any] | None = None,
        silent: bool = False,
        mentions: Iterable[str | int] | None = None,
        encrypt: bool | None = None,
        send_id: str | uuid.UUID | None = None,
    ) -> SentMessage:
        """Sends a file, or an album of 2 to 10 pictures and clips, with a caption.

        Into a readable chat each file is uploaded as it is, and the server writes the message
        the phones read. Into an end-to-end encrypted chat each file is sealed first to every
        member with a usable key and to the bot (unsigned, as the phones seal files), uploaded
        sealed, and the message that describes the files is sealed and signed like a text: the
        server sees neither the files nor what they are. Every file carries its SHA-256, which
        the phones check after downloading.

        A change of the chat's mode is handled as in `send_message`: if a readable chat turned
        encrypted, the files are sealed and sent again with the same ``send_id`` (the ones
        uploaded in clear stay on the server, unused, and a warning is logged).

        Args:
            chat: The chat's ID, or a chat object such as a `Chat`, a `ChatRef` or
                ``message.chat``.
            files: One file, or a list of 2 to 10 pictures and clips for an album. Each one is an
                `InputFile`, the bytes, or the path of a file; the kind and the type are guessed
                from the name when they are not given.
            caption: The text under the files, with the rules of a text; blank means none.
            reply_to: The message to quote: its ID, or the message itself.
            markup: Buttons under the message. Never encrypted.
            silent: ``True`` posts without a notification. It only matters in channels.
            mentions: In a group, the IDs of the people the caption tags, up to 20.
            encrypt: ``None`` follows the chat. ``True`` refuses to send in clear, ``False``
                refuses to send sealed.
            send_id: A UUID that makes the send safe to repeat. One is made for you.

        Returns:
            The `SentMessage` as the server stored it.

        Raises:
            ValidationError: No files or more than 10, an album with something that is not a
                picture or a clip, a file over `upload_max`, a field that breaks a rule, or the
                caption. Nothing was sent.
            EncryptionRequiredError: The chat is readable, and ``encrypt=True`` or
                ``require_encryption=True``. Nothing was sent.
            EncryptionError: The chat is encrypted and the files can't be sealed.
            ForbiddenError: The bot may not send files, or voice notes, here.
            FloodWaitError: More than 30 uploads this minute, or too many messages.

        Example:
            A picture from disk, and an album::

                await bot.send_media(chat_id, "sunset.jpg", caption="Tonight")
                await bot.send_media(chat_id, [InputFile.from_path(p) for p in photos])
        """
        chat_id = _chat_id(chat)
        items = input_files(files, limit=self.upload_max)
        caption = v.check_caption(caption, limit=self.text_max)
        reply_id = _message_id(reply_to) if reply_to is not None else None
        tagged = v.check_mentions(mentions) if mentions is not None else None
        send_id = send_id or uuid.uuid4()
        for attempt in range(2):
            mode, kind = await self._mode(chat_id)
            try:
                if self._body_kind(mode, chat_id, encrypt) == "armored":
                    key = self._need_key(chat_id)
                    recipients = await self.recipients(chat_id)
                    envelopes = []
                    for item in items:
                        sealed = await asyncio.to_thread(self._seal_file, key, item.data, recipients)
                        uploaded = await self._aapi.upload_file(sealed, limit=self.upload_max)
                        envelopes.append(item.envelope(uploaded.id))
                    armored = await asyncio.to_thread(key.seal, wire.media_body(envelopes, caption), recipients)
                    sent = await self._aapi.send_message(
                        chat_id,
                        armored=armored,
                        reply_to=reply_id,
                        markup=markup,
                        silent=silent or None,
                        send_id=send_id,
                        mentions=tagged,
                        chat_kind=kind,
                    )
                else:
                    media: list[MediaIn] = []
                    for item in items:
                        uploaded = await self._aapi.upload_file(item.data, limit=self.upload_max)
                        media.append(item.media_in(uploaded.id))
                    sent = await self._aapi.send_message(
                        chat_id,
                        caption,
                        media=media,
                        reply_to=reply_id,
                        markup=markup,
                        silent=silent or None,
                        send_id=send_id,
                        mentions=tagged,
                        chat_kind=kind,
                    )
            except (WrongBodyKindError, ChatIsOpenError) as exc:
                await self._mode_changed(chat_id, kind)
                if attempt == 0 and getattr(exc, "expected", None) == "armored":
                    log.warning("chat %s turned end-to-end encrypted; sealing the files and sending again", chat_id)
                    continue
                raise
            return SentMessage.bind(sent, self)
        raise AssertionError("unreachable")  # pragma: no cover

    async def send_sticker(
        self,
        chat: ChatLike,
        sticker: StickerLike,
        *,
        media_id: str | None = None,
        reply_to: int | MessageBase | None = None,
        markup: InlineKeyboard | Mapping[str, Any] | None = None,
        silent: bool = False,
        encrypt: bool | None = None,
        send_id: str | uuid.UUID | None = None,
    ) -> SentMessage:
        """Sends a sticker, sealed when the chat is end-to-end encrypted.

        A sticker of a public pack, or one the bot was shown: a `Sticker` from a message
        carries its file ID, which proves it. Into a readable chat the server writes the
        message; into an encrypted one the bot asks the server what the sticker is
        (`get_sticker`) and seals the message itself. A sticker has no text.

        Args:
            chat: The chat's ID, or a chat object such as a `Chat`, a `ChatRef` or
                ``message.chat``.
            sticker: The sticker: its ID, a `Sticker` (``message.sticker``), or a dict with
                ``id`` and ``mediaId``.
            media_id: The sticker's file ID, when ``sticker`` is a bare ID of a pack that is not
                public.
            reply_to: The message to quote: its ID, or the message itself.
            markup: Buttons under the sticker. Never encrypted.
            silent: ``True`` posts without a notification. It only matters in channels.
            encrypt: ``None`` follows the chat. ``True`` refuses to send in clear, ``False``
                refuses to send sealed.
            send_id: A UUID that makes the send safe to repeat. One is made for you.

        Returns:
            The `SentMessage` as the server stored it.

        Raises:
            ValidationError: Not a sticker ID.
            StickerNotFoundError: No such sticker, or one of a pack the bot can't use.
            EncryptionRequiredError: The chat is readable, and ``encrypt=True`` or
                ``require_encryption=True``.
            EncryptionError: The chat is encrypted and the message can't be sealed.

        Example:
            Answer a sticker with the same sticker::

                @bot.on_message(f.has_sticker)
                async def mirror(message):
                    await bot.send_sticker(message.chat, message.sticker, reply_to=message)
        """
        chat_id = _chat_id(chat)
        ref = _sticker_ref(sticker, media_id)
        reply_id = _message_id(reply_to) if reply_to is not None else None
        send_id = send_id or uuid.uuid4()
        for attempt in range(2):
            mode, kind = await self._mode(chat_id)
            try:
                if self._body_kind(mode, chat_id, encrypt) == "armored":
                    found = await self._aapi.get_sticker(ref["id"], ref.get("mediaId"))
                    armored = await self.seal(chat_id, wire.sticker_body(found))
                    sent = await self._aapi.send_message(
                        chat_id,
                        armored=armored,
                        reply_to=reply_id,
                        markup=markup,
                        silent=silent or None,
                        send_id=send_id,
                        chat_kind=kind,
                    )
                else:
                    sent = await self._aapi.send_message(
                        chat_id,
                        sticker=ref,
                        reply_to=reply_id,
                        markup=markup,
                        silent=silent or None,
                        send_id=send_id,
                        chat_kind=kind,
                    )
            except (WrongBodyKindError, ChatIsOpenError) as exc:
                await self._mode_changed(chat_id, kind)
                if attempt == 0 and getattr(exc, "expected", None) == "armored":
                    log.warning("chat %s turned end-to-end encrypted; sealing the sticker and sending again", chat_id)
                    continue
                raise
            return SentMessage.bind(sent, self)
        raise AssertionError("unreachable")  # pragma: no cover

    async def get_sticker(self, sticker_id: int, media_id: str | None = None) -> Sticker:
        """Reads a sticker the bot may send (``GET /stickers/{id}``).

        Args:
            sticker_id: The sticker's ID.
            media_id: Its file ID, as a message carried it, for a pack that is not public.

        Returns:
            The `Sticker`.

        Raises:
            StickerNotFoundError: No such sticker, or one of a pack the bot can't use.
        """
        return await self._aapi.get_sticker(sticker_id, media_id)

    async def forward_message(
        self,
        chat: ChatLike,
        from_chat: ChatLike,
        message: int | MessageBase,
        *,
        hide_sender: bool = False,
        silent: bool = False,
        send_id: str | uuid.UUID | None = None,
    ) -> SentMessage:
        """Sends a message the bot was handed on into another chat it is in.

        Between two readable chats the server does it (``POST /messages/forward``): "forwarded
        from" goes over it, and the files are copied for the bot. When either chat is end-to-end
        encrypted the bot does it itself, the way a phone does: it opens the message, downloads
        and opens its files, seals them again to the new chat's keys, uploads them, and seals
        the message with "forwarded from" over it. A forward of a forward keeps the first
        origin. The bot can't know whether the person who wrote it lets a forward link to their
        profile, so its own forwards show the person's name without the link, unless the message
        is the bot's own.

        A readable chat takes from the bot only what the server writes for it, which has no
        "forwarded from": a message of an encrypted chat goes into a readable one only as a
        copy (``hide_sender=True``), the words and the files without the link card.

        A chat that protects its content does not let its messages out, and comments are not
        forwarded.

        Args:
            chat: Where to: the chat's ID, or a chat object such as ``message.chat``.
            from_chat: The chat the message is in.
            message: The message: its ID, or the message itself (a received `Message` saves
                fetching and opening it again).
            hide_sender: ``True`` sends a plain copy, without "forwarded from".
            silent: ``True`` posts without a notification. It only matters in channels.
            send_id: A UUID that makes the forward safe to repeat. One is made for you.

        Returns:
            The `SentMessage` the forward became.

        Raises:
            ValidationError: The message is a comment, its chat protects its content, or it
                would go from an encrypted chat into a readable one without
                ``hide_sender=True``.
            EncryptionRequiredError: The target is readable, and ``require_encryption=True``.
            EncryptionError: The target is encrypted and the message can't be sealed to it.
            NotARecipientError: A file of the message was not sealed to the bot.
            MessageNotFoundError: No such message, or one the bot was never handed.
            ForbiddenError: The bot may not write in the target.

        Example:
            Pass every message with a file on to an archive channel::

                @bot.on_message(f.has_media)
                async def archive(message):
                    await message.forward_to(ARCHIVE_ID)
        """
        target = _chat_id(chat)
        source = _chat_id(from_chat)
        message_id = _message_id(message)
        send_id = send_id or uuid.uuid4()
        source_mode, _ = await self._mode(source)
        target_mode, target_kind = await self._mode(target)
        # the forward below writes in clear: the same promise as a send
        self._body_kind(target_mode, target, None)
        if source_mode == ChatMode.OPEN and target_mode == ChatMode.OPEN:
            try:
                sent = await self._aapi.forward_message(
                    target,
                    source,
                    message_id,
                    hide_sender=hide_sender or None,
                    silent=silent or None,
                    send_id=send_id,
                    chat_kind=target_kind,
                )
                return SentMessage.bind(sent, self)
            except ChatIsProtectedError:
                # the message itself is sealed, or one of the chats just changed mode
                await self._mode_changed(source, _kind_of(source))
                await self._mode_changed(target, target_kind)
        original = await self._forwarded_message(source, message, message_id)
        raw = wire.body_of(original)
        if not hide_sender and not raw.startswith(wire.FORWARD):
            sender = original.from_user
            linkable = sender is not None and self._is_me(sender.id)
            raw = wire.forward_body(wire.origin_of(original.chat, original, linkable=linkable), raw)
        for attempt in range(2):
            mode, kind = await self._mode(target)
            try:
                if self._body_kind(mode, target, None) == "armored":
                    key = self._need_key(target)
                    recipients = await self.recipients(target)
                    moved: dict[str, dict[str, Any]] = {}
                    for env in wire.files_in(raw):
                        media = wire.media_of(env)
                        if media is None or media.id.lower() in moved:
                            continue
                        data = await self.download_media(media, max_bytes=self.upload_max)
                        sealed = await asyncio.to_thread(self._seal_file, key, data, recipients)
                        uploaded = await self._aapi.upload_file(sealed, limit=self.upload_max)
                        moved[media.id.lower()] = {"media": uploaded.id}
                    armored = await asyncio.to_thread(key.seal, wire.remap_files(raw, moved), recipients)
                    sent = await self._aapi.send_message(
                        target, armored=armored, silent=silent or None, send_id=send_id, chat_kind=kind
                    )
                else:
                    if not hide_sender:
                        raise ValidationError(
                            f"chat {target} is readable and the message comes sealed: it can go there only as a "
                            "copy, hide_sender=True",
                            field="hide_sender",
                        )
                    sent = await self._copy(target, original, silent=silent, send_id=send_id, chat_kind=kind)
            except (WrongBodyKindError, ChatIsOpenError) as exc:
                await self._mode_changed(target, kind)
                if attempt == 0 and getattr(exc, "expected", None) == "armored":
                    log.warning("chat %s turned end-to-end encrypted; sealing the forward and sending again", target)
                    continue
                raise
            return SentMessage.bind(sent, self)
        raise AssertionError("unreachable")  # pragma: no cover

    async def _forwarded_message(self, source: str, message: int | MessageBase, message_id: int) -> MessageBase:
        """The message to forward, opened, after the checks the server makes for its own forwards."""
        if (
            isinstance(message, MessageBase)
            and message.chat.id == source
            and (message.plaintext is not None or message.armored is None)
        ):
            original = message
        else:
            original = await self._open(await self._aapi.get_message(source, message_id))
        if original.thread_root is not None:
            raise ValidationError("comments are not forwarded", field="message")
        if original.armored is not None and original.plaintext is None:
            raise DecryptionError(f"message {message_id} of chat {source} could not be opened to forward it")
        if _kind_of(source) != ChatKind.PRIVATE and (await self.get_chat(source)).protect_content:
            raise ValidationError(
                f"chat {source} protects its content: its messages can't be forwarded", field="from_chat"
            )
        return original

    async def _copy(
        self, target: str, original: MessageBase, *, silent: bool, send_id: str | uuid.UUID, chat_kind: ChatKind
    ) -> SentMessageBase:
        """A message as a readable chat takes it from the bot: the words, the files, a sticker."""
        if original.sticker is not None:
            if original.sticker.id is None:
                raise ValidationError("this sticker carries no ID, so it can't be sent on", field="message")
            ref = _sticker_ref(original.sticker)
            return await self._aapi.send_message(
                target, sticker=ref, silent=silent or None, send_id=send_id, chat_kind=chat_kind
            )
        if original.media:
            media = []
            for item in original.media:
                data = await self.download_media(item, max_bytes=self.upload_max)
                uploaded = await self._aapi.upload_file(data, limit=self.upload_max)
                media.append(_media_in(item, uploaded.id, data))
            return await self._aapi.send_message(
                target, original.text, media=media, silent=silent or None, send_id=send_id, chat_kind=chat_kind
            )
        if not original.text:
            raise ValidationError("the message has nothing a readable chat can take", field="message")
        return await self._aapi.send_message(
            target, original.text, silent=silent or None, send_id=send_id, chat_kind=chat_kind
        )

    async def send_comment(
        self,
        chat: ChatLike,
        post: int | MessageBase,
        text: str,
        *,
        reply_to: int | MessageBase | None = None,
        as_author: bool = False,
        mentions: Iterable[str | int] | None = None,
        encrypt: bool | None = None,
    ) -> SentMessage:
        """Comments under a channel post, sealed when the channel is end-to-end encrypted.

        The rules of a person's comment hold: the channel's comments are on and this post's
        are not closed, and ``reply_to`` points at a comment under the same post. A bot that
        runs the channel speaks as the channel unless the channel lets its admins sign and
        ``as_author`` is ``True``. `Message.reply` on a comment does this for you.

        The server keeps no ``sendId`` for comments, so a comment is never sent twice by a
        retry: after a network error it raises instead.

        Args:
            chat: The channel: its ID, or a chat object such as ``message.chat``.
            post: The post: its ID, or the message itself.
            text: The comment, with the rules of `send_message`.
            reply_to: A comment under the same post to answer: its ID, or the message itself.
            as_author: ``True`` signs the comment with the bot's own name, where the channel
                lets its admins sign.
            mentions: The IDs of the people the comment tags, up to 20.
            encrypt: ``None`` follows the channel. ``True`` refuses to send in clear, ``False``
                refuses to send sealed.

        Returns:
            The comment, as the server stored it.

        Raises:
            ValidationError: Not a channel, or the text breaks a rule.
            CommentsClosedError: The channel takes no comments, or this post doesn't.
            EncryptionRequiredError: The channel is readable, and ``encrypt=True`` or
                ``require_encryption=True``.
            EncryptionError: The channel is encrypted and the comment can't be sealed.
        """
        chat_id = _chat_id(chat)
        post_id = _message_id(post)
        text = v.check_text(text, limit=self.text_max)
        reply_id = _message_id(reply_to) if reply_to is not None else None
        mode, _ = await self._mode(chat_id)
        body = await self._body(mode, chat_id, text, encrypt)
        try:
            sent = await self._aapi.send_comment(
                chat_id, post_id, reply_to=reply_id, as_author=as_author or None, mentions=mentions, **body
            )
        except (WrongBodyKindError, ChatIsOpenError):
            # not sent again on its own: a comment has no sendId to make a repeat safe
            await self._mode_changed(chat_id, ChatKind.CHANNEL)
            raise
        return SentMessage.bind(sent, self)

    async def get_comments(
        self, chat: ChatLike, post: int | MessageBase, *, before: int | None = None, limit: int | None = None
    ) -> Comments:
        """Reads the comments the bot was handed under a channel post, newest first, opened.

        In an encrypted channel each comment is decrypted and its signature checked, as an
        update's would be; one that can't be opened comes back as it is, with ``text`` left
        ``None``.

        Args:
            chat: The channel: its ID, or a chat object such as ``message.chat``.
            post: The post: its ID, or the message itself.
            before: The oldest comment ID you hold, for the next page back.
            limit: How many, 1 to 100. ``None`` leaves it to the server, which gives 50.

        Returns:
            The `Comments`, with whether the post takes comments right now and each comment as
            a `Message` that can be answered.

        Raises:
            ValidationError: Not a channel, or ``before`` or ``limit`` out of range.
            MessageNotFoundError: No such post, or one the bot was never handed.
        """
        page = await self._aapi.get_comments(_chat_id(chat), _message_id(post), before=before, limit=limit)
        opened = [self._bind_message(await self._open(m, quiet=True)) for m in page.messages]
        return replace(page, messages=tuple(opened))

    async def get_history(
        self, chat: ChatLike, *, before: int | None = None, limit: int | None = None
    ) -> list[Message]:
        """Reads what the bot was handed in a chat, newest first, opened.

        In a private chat that is every message of it; in a group or channel the bot's own
        messages and the ones the room delivered to it, and nothing else: what the room did not
        let it read stays out, even after the room lets it read everything. In an encrypted
        chat each message is decrypted and its signature checked, as an update's would be; one
        that can't be opened comes back as it is, with ``text`` left ``None``.

        Args:
            chat: The chat's ID, or a chat object such as ``message.chat``.
            before: The oldest ID you hold, for the next page back. ``None`` for the newest.
            limit: How many, 1 to 100. ``None`` leaves it to the server, which gives 50.

        Returns:
            The messages, each a `Message` that can be answered.

        Raises:
            ValidationError: ``before`` or ``limit`` out of range.
            ChatNotFoundError: There is no such chat, or the bot is not in it.

        Example:
            What the bot was told in a chat, oldest first::

                for message in reversed(await bot.get_history(chat_id, limit=100)):
                    print(message.date, message.text)
        """
        messages = await self._aapi.get_history(_chat_id(chat), before=before, limit=limit)
        return [self._bind_message(await self._open(m, quiet=True)) for m in messages]

    async def get_message(self, chat: ChatLike, message_id: int) -> Message:
        """Reads one message the bot was handed, opened.

        Args:
            chat: The chat's ID, or a chat object such as ``message.chat``.
            message_id: The message.

        Returns:
            The `Message`, decrypted when it came sealed.

        Raises:
            MessageNotFoundError: No such message, or one the bot was never handed.
            DecryptionError: The message is sealed and can't be opened.
        """
        message: Message = self._bind_message(
            await self._open(await self._aapi.get_message(_chat_id(chat), message_id))
        )
        return message

    # --- reactions, pins, read marks ---

    async def delete_messages(
        self, chat: ChatLike, messages: Iterable[int | MessageBase | SentMessageBase]
    ) -> list[int]:
        """Deletes up to 100 messages of one chat at once, for everyone, for good.

        Each with the rules of `delete_message`. If one is not the bot's to delete, nothing is
        deleted. Messages the bot was never handed are left out.

        Args:
            chat: The chat's ID, or a chat object such as ``message.chat``.
            messages: The messages: IDs, or the messages themselves.

        Returns:
            The IDs that went, the comments taken with their posts included.

        Raises:
            ValidationError: No messages, or more than 100.
            ForbiddenError: One of them is not the bot's to delete. Nothing was deleted.
        """
        return await self._aapi.delete_messages(_chat_id(chat), [_message_id(m) for m in messages])

    async def set_reaction(
        self, chat: ChatLike, message: int | MessageBase | SentMessageBase, emoji: str
    ) -> MessageReactions:
        """Puts the bot's reaction on a message, in place of the one it had there.

        The rules of a person's reaction hold: in a group or channel reactions must be on, the
        emoji one the chat takes, and the bot allowed to react. `Message.react` does the same.

        Args:
            chat: The chat's ID, or a chat object such as ``message.chat``.
            message: The message: its ID, or the message itself.
            emoji: The emoji, or a custom emoji as the app writes it.

        Returns:
            Every reaction on the message now.

        Raises:
            ValidationError: ``emoji`` is empty or longer than 48 characters.
            ForbiddenError: Reactions are off, the chat does not take this emoji, or the bot
                may not react.
        """
        return await self._aapi.set_reaction(_chat_id(chat), _message_id(message), emoji)

    async def remove_reaction(self, chat: ChatLike, message: int | MessageBase | SentMessageBase) -> bool:
        """Takes the bot's reaction off a message.

        Args:
            chat: The chat's ID, or a chat object such as ``message.chat``.
            message: The message: its ID, or the message itself.

        Returns:
            ``True`` when the bot had a reaction there, ``False`` when it had none.
        """
        return await self._aapi.remove_reaction(_chat_id(chat), _message_id(message))

    async def pin_message(self, chat: ChatLike, message: int | MessageBase | SentMessageBase) -> list[int]:
        """Pins a message for everybody in the chat.

        In a group or channel it needs the *pin messages* right, or the members' permission to
        pin; in a private chat the person sees a notice.

        Args:
            chat: The chat's ID, or a chat object such as ``message.chat``.
            message: The message: its ID, or the message itself.

        Returns:
            The IDs of the messages pinned in the chat now.

        Raises:
            ForbiddenError: The bot may not pin messages here.
        """
        return await self._aapi.pin_message(_chat_id(chat), _message_id(message))

    async def unpin_message(self, chat: ChatLike, message: int | MessageBase | SentMessageBase) -> list[int]:
        """Unpins a message.

        Args:
            chat: The chat's ID, or a chat object such as ``message.chat``.
            message: The message: its ID, or the message itself.

        Returns:
            The IDs of the messages still pinned.

        Raises:
            ForbiddenError: The bot may not unpin messages here.
        """
        return await self._aapi.unpin_message(_chat_id(chat), _message_id(message))

    async def unpin_all(self, chat: ChatLike) -> list[int]:
        """Unpins every message of a group or channel.

        Args:
            chat: The group or channel: its ID, or a chat object such as ``message.chat``.

        Returns:
            The IDs still pinned: none.

        Raises:
            ValidationError: A private chat.
            ForbiddenError: The bot may not unpin messages here.
        """
        return await self._aapi.unpin_all(_chat_id(chat))

    async def read_chat(self, chat: ChatLike, up_to: int | MessageBase | None = None) -> int | None:
        """Marks a chat read: the ticks people see.

        In a group or channel only a bot the room lets read every message may do it.

        Args:
            chat: The chat's ID, or a chat object such as ``message.chat``.
            up_to: The newest message read: its ID, or the message itself. ``None`` marks
                everything.

        Returns:
            The ID of the last message marked read, or ``None`` when the server did not say.

        Raises:
            ReadsAllNeededError: A group or channel that does not let the bot read every
                message.
        """
        return await self._aapi.read_chat(_chat_id(chat), _message_id(up_to) if up_to is not None else None)

    # --- chats and people ---

    async def get_chats(self) -> list[Chat]:
        """Lists the groups and channels the bot is in, with its seat in each.

        Private chats are not listed. The bot keeps what it learns, as `get_chat` does.

        Returns:
            Each group and channel, as `get_chat` describes it.

        Example:
            Where the bot may not read everything::

                for chat in await bot.get_chats():
                    if not chat.reads_all:
                        print(chat.title)
        """
        chats = await self._aapi.get_chats()
        for chat in chats:
            self._note_chat(chat)
        return chats

    async def get_user(self, user: str | int | Any) -> Profile:
        """Reads somebody's public profile (``GET /users/{id}``).

        Only for somebody the bot may look up: a private chat with them, a room whose member list
        shows them to the bot, or a room where their words reached it. No phone number, no presence.

        Args:
            user: The person: an ID (``"48213907"``, or a bot's ``"B48213907"``) or a `User`.

        Returns:
            The `Profile`.

        Raises:
            UserNotFoundError: No such person, or nobody the bot may look up.
        """
        return await self._aapi.get_user(_user_id(user))

    # --- running a group or channel ---

    async def update_chat(
        self,
        chat: ChatLike,
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
    ) -> Chat:
        """Changes the settings of a group or channel; a field left out stays as it is.

        Each field needs the right the app asks for it (see `AsyncClient.update_chat`). Whether
        the bot reads every message is not one of them: the room decides that in the app.

        Args:
            chat: The group or channel: its ID, or a chat object such as ``message.chat``.
            title: The new name, 1 to 64 characters.
            description: The new description, up to 500 characters; ``None`` clears it.
            permissions: What members may do, laid over what they may do now, by name:
                ``{"send_media": False}``. Groups only.
            slow_mode_seconds: Seconds between two messages of a member, 0 to 3600. Groups
                only.
            members_hidden: ``True`` shows the member list to the admins only.
            protect_content: ``True`` stops messages from being forwarded, copied or saved.
            reactions_enabled: ``False`` turns reactions off.
            allowed_reactions: The only emoji people may react with, up to 40 of at most 16
                characters each; ``None`` allows any.
            auto_delete_seconds: After how many seconds new messages delete themselves; ``0``
                turns it off.
            signatures: ``True`` shows who wrote each post. Channels only.
            signature_profiles: ``True`` links a signature to the profile. Channels only.
            admins_hidden: ``True`` keeps the admins from the subscribers. Channels only.

        Returns:
            The chat with the new settings.

        Raises:
            ValidationError: Nothing to change, or a value the server would refuse.
            ForbiddenError: The bot lacks the right one of the fields needs.
        """
        updated = await self._aapi.update_chat(
            _chat_id(chat),
            title=title,
            description=description,
            permissions=permissions,
            slow_mode_seconds=slow_mode_seconds,
            members_hidden=members_hidden,
            protect_content=protect_content,
            reactions_enabled=reactions_enabled,
            allowed_reactions=allowed_reactions,
            auto_delete_seconds=auto_delete_seconds,
            signatures=signatures,
            signature_profiles=signature_profiles,
            admins_hidden=admins_hidden,
        )
        self._note_chat(updated)
        return updated

    async def update_member(
        self,
        chat: ChatLike,
        user: str | int | Any,
        *,
        role: str | None = None,
        admin_rights: Iterable[str] | None = None,
        restrictions: Iterable[str] | None = None,
        restricted_until: datetime | int | None = KEEP,
        custom_title: str | None = KEEP,
        anonymous: bool | None = None,
    ) -> Member:
        """Promotes, demotes, restricts or gives a title to a member of a group or channel.

        The app's rules hold to the letter (see `AsyncClient.update_member`): nobody hands out
        a right they don't hold, the owner is nobody's to change, and a bot doesn't change its
        own seat.

        Args:
            chat: The group or channel: its ID, or a chat object such as ``message.chat``.
            user: The member: an ID, a `User`, or a `Member`.
            role: ``"admin"`` promotes, ``"member"`` demotes.
            admin_rights: The rights of the badge, by name: ``["delete_messages", "ban_users"]``.
            restrictions: The permissions taken away from this member, by name:
                ``["send_messages"]``; an empty list gives them all back.
            restricted_until: When the restriction ends: an aware `~datetime.datetime` or unix
                milliseconds; ``None`` for good. Only with ``restrictions``.
            custom_title: An admin's title, up to 16 characters; ``None`` removes it.
            anonymous: ``True`` lets an admin post without their name.

        Returns:
            The member's seat now.

        Raises:
            ValidationError: Nothing to change, or a value the server would refuse.
            ForbiddenError: The bot lacks the right, or the member is out of its reach.

        Example:
            Mute somebody for an hour::

                until = datetime.now(UTC) + timedelta(hours=1)
                await bot.update_member(chat, user_id, restrictions=["send_messages"], restricted_until=until)
        """
        return await self._aapi.update_member(
            _chat_id(chat),
            _user_id(user),
            role=role,
            admin_rights=admin_rights,
            restrictions=restrictions,
            restricted_until=restricted_until,
            custom_title=custom_title,
            anonymous=anonymous,
        )

    async def remove_member(
        self, chat: ChatLike, user: str | int | Any, *, ban: bool = False, banned_until: datetime | int | None = None
    ) -> bool:
        """Throws somebody out of a group or channel, and bans them if asked.

        It needs the *ban users* right; never the owner, and an admin only by the owner.

        Args:
            chat: The group or channel: its ID, or a chat object such as ``message.chat``.
            user: The member: an ID, a `User`, or a `Member`.
            ban: ``True`` bans them as well.
            banned_until: When the ban ends: an aware `~datetime.datetime` or unix milliseconds;
                ``None`` for good. Only with ``ban=True``.

        Returns:
            ``True`` when they were banned as well, ``False`` when only removed.

        Raises:
            ForbiddenError: The bot lacks the right, or the member is the owner or an admin.
            MemberNotFoundError: Not a member, or hidden from the bot.
        """
        return await self._aapi.remove_member(_chat_id(chat), _user_id(user), ban=ban, banned_until=banned_until)

    async def purge_member(
        self, chat: ChatLike, user: str | int | Any, *, up_to: int | MessageBase | None = None
    ) -> bool:
        """Deletes everything one member wrote in a group or channel.

        It needs the *delete messages* right.

        Args:
            chat: The group or channel: its ID, or a chat object such as ``message.chat``.
            user: The member: an ID, a `User`, or a `Member`.
            up_to: Only up to this message: its ID, or the message itself. ``None`` deletes all
                of it.

        Returns:
            ``True`` once it is done.

        Raises:
            ForbiddenError: The bot lacks the right.
        """
        last = _message_id(up_to) if up_to is not None else None
        return await self._aapi.purge_member(_chat_id(chat), _user_id(user), up_to=last)

    async def get_bans(self, chat: ChatLike) -> list[Ban]:
        """Lists who is banned from a group or channel. It needs the *ban users* right.

        Args:
            chat: The group or channel: its ID, or a chat object such as ``message.chat``.

        Returns:
            The bans.

        Raises:
            ForbiddenError: The bot lacks the right.
        """
        return await self._aapi.get_bans(_chat_id(chat))

    async def unban(self, chat: ChatLike, user: str | int | Any) -> bool:
        """Lifts a ban: the person can come back through a link. It needs the *ban users* right.

        Args:
            chat: The group or channel: its ID, or a chat object such as ``message.chat``.
            user: The person: an ID, a `User`, or a `Ban`.

        Returns:
            ``True`` when there was a ban, ``False`` when there was none.

        Raises:
            ForbiddenError: The bot lacks the right.
        """
        return await self._aapi.unban(_chat_id(chat), _user_id(user))

    async def get_invites(self, chat: ChatLike) -> list[Invite]:
        """Lists the invite links of a group or channel.

        Args:
            chat: The group or channel: its ID, or a chat object such as ``message.chat``.

        Returns:
            The links.

        Raises:
            ForbiddenError: The bot may not manage links here.
        """
        return await self._aapi.get_invites(_chat_id(chat))

    async def create_invite(
        self,
        chat: ChatLike,
        *,
        label: str | None = None,
        expires_at: datetime | int | None = None,
        max_uses: int | None = None,
        needs_approval: bool | None = None,
    ) -> Invite:
        """Makes an invite link. Not repeated after a network error, which could make two.

        Args:
            chat: The group or channel: its ID, or a chat object such as ``message.chat``.
            label: A name for the link, up to 48 characters.
            expires_at: When it stops working: an aware `~datetime.datetime` or unix
                milliseconds. ``None`` for never.
            max_uses: How many people may use it, 1 to 100000. ``None`` for no limit.
            needs_approval: ``True`` makes people ask first; the bots that may let them in get
                a ``join_request``.

        Returns:
            The new link.

        Raises:
            ForbiddenError: The bot may not make links here.
            ChatIsPublicError: The chat is public: its handle is its link.
        """
        return await self._aapi.create_invite(
            _chat_id(chat), label=label, expires_at=expires_at, max_uses=max_uses, needs_approval=needs_approval
        )

    async def revoke_invite(self, chat: ChatLike, code: str | Invite) -> bool:
        """Revokes an invite link. It needs the *invite links* right.

        Args:
            chat: The group or channel: its ID, or a chat object such as ``message.chat``.
            code: The link: its code, or the `Invite`.

        Returns:
            ``True`` once it is revoked.

        Raises:
            InviteNotFoundError: No such link in this chat.
        """
        return await self._aapi.revoke_invite(_chat_id(chat), code.code if isinstance(code, Invite) else code)

    async def get_join_requests(self, chat: ChatLike) -> list[JoinRequest]:
        """Lists who asked to come into a group or channel. It needs the *add members* right.

        Args:
            chat: The group or channel: its ID, or a chat object such as ``message.chat``.

        Returns:
            The requests waiting.

        Raises:
            ForbiddenError: The bot lacks the right.
        """
        return await self._aapi.get_join_requests(_chat_id(chat))

    async def decide_join_request(self, chat: ChatLike, user: str | int | Any, approve: bool) -> bool:
        """Lets somebody in who asked, or turns them away. It needs the *add members* right.

        Only somebody who asked: it is never a way round a person's "don't add me to groups".

        Args:
            chat: The group or channel: its ID, or a chat object such as ``request.chat``.
            user: Who asked: an ID, a `User`, or the `JoinRequest` itself.
            approve: ``True`` lets them in, ``False`` turns them away.

        Returns:
            ``approve``, as the server confirms it.

        Raises:
            JoinRequestNotFoundError: They have no request waiting.
            ChatFullError: The chat can't take anybody else. The request stays, to
                be decided when there is room.
        """
        return await self._aapi.decide_join_request(_chat_id(chat), _user_id(user), approve)

    async def set_comments(
        self, chat: ChatLike, enabled: bool, *, signing: bool | None = None, outside: bool | None = None
    ) -> Chat:
        """Turns a channel's comments on or off. It needs the *manage comments* right.

        Args:
            chat: The channel: its ID, or a chat object such as ``message.chat``.
            enabled: ``True`` turns the comments on.
            signing: ``True`` lets the admins comment under their own name. Left out, the
                server takes it as ``False``.
            outside: ``True`` lets people who aren't subscribed comment on a public readable
                channel. Left out, it stays as it is.

        Returns:
            The channel with the new settings.
        """
        updated = await self._aapi.set_comments(_chat_id(chat), enabled, signing=signing, outside=outside)
        self._note_chat(updated)
        return updated

    async def set_post_comments(self, chat: ChatLike, post: int | MessageBase, comments_off: bool) -> bool:
        """Closes or opens again the comments of one post. It needs the *manage comments* right.

        Args:
            chat: The channel: its ID, or a chat object such as ``message.chat``.
            post: The post: its ID, or the message itself.
            comments_off: ``True`` closes them, ``False`` opens them.

        Returns:
            Whether the post's comments are closed now.
        """
        return await self._aapi.set_post_comments(_chat_id(chat), _message_id(post), comments_off)

    async def get_channel_stats(self, chat: ChatLike) -> ChannelStats:
        """Reads a channel's numbers: subscribers coming and going, views per post. Admins only.

        Args:
            chat: The channel: its ID, or a chat object such as ``message.chat``.

        Returns:
            The `ChannelStats`.

        Raises:
            ForbiddenError: The bot is not one of the channel's admins.
        """
        return await self._aapi.get_channel_stats(_chat_id(chat))

    async def set_chat_photo(self, chat: ChatLike, photo: bytes | str | os.PathLike[str]) -> Picture:
        """Changes the picture of a group or channel: a PNG, JPEG or WebP, or a short clip.

        Whoever may change the chat's info may. The picture it replaces stays in the chat's
        album.

        Args:
            chat: The group or channel: its ID, or a chat object such as ``message.chat``.
            photo: The picture: its bytes, or the path of a file.

        Returns:
            Where the new picture is, and what it turned out to be.

        Raises:
            ValidationError: The file can't be read, or is too large.
            ForbiddenError: The bot may not change the chat's info.
        """
        return await self._aapi.set_chat_photo(_chat_id(chat), _bytes_of(photo, "photo"), limit=self.upload_max)

    async def delete_chat_photo(self, chat: ChatLike) -> bool:
        """Removes the picture of a group or channel; the one before it, if any, comes back.

        Args:
            chat: The group or channel: its ID, or a chat object such as ``message.chat``.

        Returns:
            ``True`` when there was a picture to remove.
        """
        return await self._aapi.delete_chat_photo(_chat_id(chat))

    async def set_chat_banner(self, chat: ChatLike, banner: bytes | str | os.PathLike[str]) -> Picture:
        """Changes the banner of a group or channel, like `set_chat_photo`.

        Args:
            chat: The group or channel: its ID, or a chat object such as ``message.chat``.
            banner: The banner: its bytes, or the path of a file.

        Returns:
            Where the new banner is, and what it turned out to be.

        Raises:
            ValidationError: The file can't be read, or is too large.
            ForbiddenError: The bot may not change the chat's info.
        """
        return await self._aapi.set_chat_banner(_chat_id(chat), _bytes_of(banner, "banner"), limit=self.upload_max)

    async def delete_chat_banner(self, chat: ChatLike) -> bool:
        """Removes the banner of a group or channel, like `delete_chat_photo`.

        Args:
            chat: The group or channel: its ID, or a chat object such as ``message.chat``.

        Returns:
            ``True`` when there was a banner to remove.
        """
        return await self._aapi.delete_chat_banner(_chat_id(chat))


async def _async_step(
    mw: Any, nxt: Callable[[Any, dict[str, Any]], Awaitable[Any]], event: Any, data: dict[str, Any]
) -> Any:
    return await mw(nxt, event, data)


# --- SyncBot: plain functions ---


class SyncBot(_BotBase):
    """A Hidesay bot whose handlers are plain functions, for code that does not use asyncio.

    Give it a token, register handlers, and call `run`: the bot long-polls the server, runs the
    handler that matches each update, and stops cleanly on Ctrl+C or ``SIGTERM``. Handlers run
    in a pool of ``concurrency`` threads (8 by default), named ``hidesay_bot-handler_0``,
    ``hidesay_bot-handler_1`` and so on. The updates of one chat are handled one at a time, in
    order; different chats at the same time, so code that handlers share must be thread-safe.
    In an end-to-end encrypted chat the bot opens incoming messages and seals what it sends on
    its own, so handlers read and write plain text. For ``async def`` handlers, use `Bot`: it
    takes the same options and has the same methods.

    A bot is a `Router`: the decorators that register handlers (`Router.on_message`,
    `Router.command`, `Router.on_callback_query`, `Router.on_error`, ...) and
    `Router.include_router` come from there. Handlers, filters, middlewares, error handlers and
    hooks are plain functions: an ``async def`` one makes the bot refuse to start, with a
    `ConfigurationError` that names it. The bot's own methods cover the whole Bot API, with the
    OpenPGP part done for you. They are the methods of `Bot`, called without ``await``, and
    each one blocks until it is done. `api` is the low-level client underneath. Besides the
    errors each method lists, every method that calls the server can raise the ones in
    :ref:`common-errors`.

    Unlike `Bot`, it has no ``start`` or ``start_polling`` and is not a context manager: its
    methods also work on a bot that is not running, so a script can make one and send. A
    `SQLiteStorage`, though, is open only while `run` runs. Outside it, whatever uses the
    storage raises `StorageError`: `failed_updates`, `trust_key`, or a send into an encrypted
    chat with ``pin_keys=True``. `run` and `process_update` make an event loop of their own, so
    they can't be called inside a running one: in async code, use `Bot`.

    Examples:
        An echo bot; the token comes from ``HIDESAY_BOT_TOKEN``::

            from hidesay_bot import SyncBot

            bot = SyncBot()


            @bot.on_message()
            def echo(message):
                message.reply(message.text or "...")


            bot.run()

        A script that only sends, into an encrypted chat too::

            bot = SyncBot(pgp_private_key="bot-key.asc", pgp_passphrase=passphrase)
            bot.send_message("48213907", "Nightly job done")
    """

    _flavour = "sync"

    _sync_client: Client | None = None
    _pool: ThreadPoolExecutor | None = None

    @property
    def api(self) -> Client:
        """The low-level `Client` that the bot's methods call.

        It has one method per operation of the Bot API, and shares the bot's rate limiter. It
        sends what you give it as it is: in an encrypted chat, nothing is sealed or opened for
        you. The bot makes it when it is first needed and closes it when `run` returns. Leave
        closing it to the bot: a closed `Client` can't be used again, so the bot's own calls
        would fail with `ConfigurationError`.
        """
        return self._api

    @property
    def _api(self) -> Client:
        client = self._sync_client
        if client is None:
            with self._lock:
                if self._sync_client is None:
                    self._sync_client = Client(
                        self._plain_token.reveal(), **self._client_options, transport=self._transport
                    )
                client = self._sync_client
        return client

    def _bind_message(self, message: MessageBase) -> SyncMessage:
        return SyncMessage.bind(message, self)

    def _bind_query(self, query: CallbackQueryBase) -> SyncCallbackQuery:
        return SyncCallbackQuery.bind(query, self)

    def _executor(self) -> ThreadPoolExecutor:
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix="hidesay_bot-handler")
        return self._pool

    def stop(self) -> None:
        """Asks the running bot to stop, and returns at once.

        It does what `request_stop` does, and is just as safe to call from a handler, from
        another thread or from a signal handler. `run` then stops cleanly: no new updates, the
        running handlers get up to ``shutdown_timeout`` seconds to finish, the `on_shutdown`
        hooks run, the storage and the connections close, and `run` returns. Does nothing when
        the bot is not running.

        Unlike `Bot.stop`, it does not wait for all that: called from a handler, waiting would
        mean waiting for itself.
        """
        self.request_stop()

    async def _shutdown(self, *, run_hooks: bool = True) -> None:
        await super()._shutdown(run_hooks=run_hooks)
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None

    async def _call_hook(self, hook: Callable[..., Any]) -> None:
        await asyncio.get_running_loop().run_in_executor(self._executor(), hook, self)

    # --- running ---

    def run(
        self,
        *,
        poll_timeout: int = 50,
        limit: int = 100,
        drop_webhook: bool = False,
        skip_pending: bool = False,
        configure_logging: bool = True,
    ) -> None:
        """Long-polls the server until Ctrl+C or ``SIGTERM``, then stops cleanly.

        Blocks until the bot has stopped. First the bot starts: it reads ``GET /me`` (waiting
        for the server if it is down), checks its key, opens the storage and runs the
        `on_startup` hooks. Then it asks the server for updates and runs their handlers in the
        handler threads, until it is told to stop: Ctrl+C, ``SIGTERM``, Ctrl+Break on Windows,
        `stop` or `request_stop`. Then it stops taking updates, gives the running handlers up to
        ``shutdown_timeout`` seconds to finish, runs the `on_shutdown` hooks, and closes the
        storage and the connections. Network failures, server errors and a paused bot are
        waited out; see :doc:`/guides/long-polling`.

        Args:
            poll_timeout: How long each poll may wait on the server for something to arrive,
                0 to 50 seconds.
            limit: How many updates one poll may bring, 1 to 100.
            drop_webhook: ``True`` removes a webhook that is set, and polls. Without it, a set
                webhook stops the bot with `PollingError`.
            skip_pending: ``True`` throws away, without handling them, the updates that waited
                on the server while the bot was off. See the warning below.
            configure_logging: ``True`` sets up logging at ``INFO`` level, if your program has
                not set up logging itself.

        Raises:
            AuthenticationError: The token is wrong or was revoked.
            PollingError: A webhook is set and ``drop_webhook`` is ``False``, or one was set
                while the bot was polling.
            PollingConflictError: Another process polls this bot with the same `SQLiteStorage`
                file, or the storage is in use by another running bot in this process.
            ConfigurationError: A handler, filter, middleware, error handler or hook is an
                ``async def`` function: a `SyncBot` needs plain functions.
            RuntimeError: It was called where an event loop is running already (inside an
                ``async def`` function, in a Jupyter cell): ``run()`` makes its own loop.
                Nothing is sent. In async code, use `Bot` and ``await bot.start_polling()``.

        Example:
            Take over from a webhook, and skip what queued while the bot was off::

                bot.run(drop_webhook=True, skip_pending=True)

        Note:
            A handler runs in a thread, and a thread can't be stopped from outside. A handler
            still running after ``shutdown_timeout`` goes on until it returns, while the bot
            shuts down without it, and the process waits for it before it exits. Its update
            was not finished, so it runs again at the next start.

        Warning:
            ``skip_pending=True`` confirms the waiting updates to the server, which deletes
            them for good. Updates already in a `SQLiteStorage` still run.
        """
        if configure_logging:
            _basic_logging()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("SyncBot.run() was called inside a running event loop: use Bot and await it there")
        try:
            with asyncio.Runner() as runner:
                runner.run(
                    self._run_polling(
                        poll_timeout=poll_timeout, limit=limit, drop_webhook=drop_webhook, skip=skip_pending
                    )
                )
        except KeyboardInterrupt:
            pass
        finally:
            if self._sync_client is not None:
                self._sync_client.close()
                self._sync_client = None

    def process_update(self, update: Update | Mapping[str, Any]) -> None:
        """Runs one update through decryption, filters and handlers, now.

        Meant for tests, and for updates that come from somewhere else. The update skips the
        queue: nothing is stored, nothing is confirmed to the server, and a repeat is not
        recognised. The bot does not need to be running, but this still calls the API: for
        ``GET /me`` if `me` is not known yet, for what the handlers send, and for the automatic
        answer to a button press. It blocks until the handlers are done; they run in the
        handler threads, as under `run`.

        Args:
            update: An `Update`, or one in the JSON form the server sends (a dict with
                ``updateId`` and ``kind``).

        Raises:
            Exception: What a handler raised, or what opening an encrypted message raised,
                when no error handler took it.
            RuntimeError: It was called where an event loop is running already, in an
                ``async def`` test for example: like `run`, it makes its own loop.

        Example:
            In a test, with the bot on a fake transport or a test server::

                bot.process_update({
                    "updateId": 1,
                    "kind": "message",
                    "message": {
                        "id": 10,
                        "chat": {"id": "48213907", "kind": "private"},
                        "from": {"id": "48213907", "username": "anna"},
                        "date": 1791462000000,
                        "kind": "user",
                        "text": "/start",
                    },
                })
        """

        async def once() -> None:
            try:
                await self._process_now(update)
            finally:
                # the async client belongs to this short-lived loop: let it go with it
                await self._aapi.aclose()

        asyncio.run(once())

    # --- dispatch ---

    async def _dispatch(self, update: Update, data: dict[str, Any], error: BaseException | None) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._executor(), self._dispatch_blocking, update, data, error)

    def _dispatch_blocking(self, update: Update, data: dict[str, Any], error: BaseException | None) -> None:
        if error is not None:
            self._handle_error(error, data)
            return
        try:
            self._route(update, data)
        except Exception as exc:  # noqa: BLE001
            self._handle_error(exc, data)

    def _route(self, update: Update, data: dict[str, Any]) -> bool:
        context = self._filter_context(update)
        for handler, middlewares in self.iter_handlers(update.kind):
            event = self._event_for(update, handler.kind)
            extra: dict[str, Any] = {}
            if not all(merge(f(event, context), extra) for f in handler.filters):
                continue
            try:
                self._call(handler, middlewares, event, {**data, **extra})
            except SkipHandler:
                continue
            return True
        log.debug("update %s (%s): no handler took it", update.update_id, update.kind)
        return False

    @staticmethod
    def _call(handler: Handler, middlewares: Sequence[Any], event: Any, data: dict[str, Any]) -> Any:
        def last(ev: Any, d: dict[str, Any]) -> Any:
            return handler.callback(ev, **handler.kwargs_for(d))

        call: Callable[[Any, dict[str, Any]], Any] = last
        for mw in reversed(middlewares):
            call = functools.partial(_sync_step, mw, call)
        return call(event, data)

    def _handle_error(self, error: BaseException, data: dict[str, Any]) -> None:
        for eh in self.iter_error_handlers():
            if not eh.matches(error):
                continue
            try:
                eh.callback(error, **eh.kwargs_for({**data, "error": error}))
            except SkipHandler:
                continue
            return
        raise error

    # --- the API, with OpenPGP done for you ---

    def _fetch_keys_blocking(self, chat_id: str) -> ChatKeys:
        cached = self._keys.get(chat_id)
        if cached is not None:
            return cached  # type: ignore[no-any-return]
        keys = self._api.get_chat_keys(chat_id)
        self._keys.put(chat_id, keys)
        return keys

    def get_me(self, *, refresh: bool = False) -> BotInfo:
        """Reads the bot's own details (``GET /me``).

        The copy in `me` is returned without asking the server, unless there is none yet or
        ``refresh`` is ``True``. ``GET /me`` works while the owner has paused the bot, so it is
        the way to find out why nothing else does.

        Args:
            refresh: ``True`` asks the server even when a copy is kept, and keeps the new one.

        Returns:
            The bot's `BotInfo`, with its handle, mode, status and the server's limits.
        """
        if self.me is None or refresh:
            self.me = self._api.get_me()
        return self.me

    def get_chat(self, chat: ChatLike, *, refresh: bool = False) -> Chat:
        """Reads a chat the bot is in (``GET /chats/{id}``).

        The answer is cached for ``cache_ttl`` seconds; ``refresh=True`` asks the server again.

        Args:
            chat: The chat's ID, or a chat object such as a `Chat`, a `ChatRef` or
                ``message.chat``.
            refresh: ``True`` skips the cache.

        Returns:
            The `Chat`, with its kind, its mode (``open`` or ``protected``) and, in a group or
            channel, the bot's own seat.

        Raises:
            ChatNotFoundError: There is no such chat, or the bot is not in it.
        """
        chat_id = _chat_id(chat)
        if not refresh:
            cached = self._chats.get(chat_id)
            if cached is not None:
                return cached  # type: ignore[no-any-return]
        info = self._api.get_chat(chat_id)
        self._note_chat(info)
        return info

    def _mode(self, chat_id: str) -> tuple[ChatMode, ChatKind]:
        if self.me is None:
            self.me = self._api.get_me()
        known = self._mode_without_asking(chat_id)
        if known is not None:
            return known
        if self._known_kind(chat_id) == ChatKind.PRIVATE:
            self.me = self._api.get_me()
            return self.me.mode, ChatKind.PRIVATE
        info = self.get_chat(chat_id, refresh=True)
        return info.mode, info.kind

    def _body(self, mode: ChatMode, chat_id: str, text: str, encrypt: bool | None) -> dict[str, str]:
        if self._body_kind(mode, chat_id, encrypt) == "armored":
            return {"armored": self.seal(chat_id, text)}
        return {"text": text}

    def _mode_changed(self, chat_id: str, kind: ChatKind) -> None:
        self._forget_chat(chat_id)
        if kind == ChatKind.PRIVATE:
            with contextlib.suppress(APIError, NetworkError):
                self.me = self._api.get_me()

    def recipients(self, chat: ChatLike) -> list[PublicKey]:
        """Lists the checked keys a message into this end-to-end encrypted chat is sealed to.

        The keys come from ``GET /chats/{id}/keys``, cached for ``cache_ttl`` seconds, and each
        one is checked: its fingerprint must match the one the server gives, it must not be
        revoked or expired, it must have a live encryption subkey, and it must be a v4 key
        (phones can't open what is sealed to a v6 key). In a group, a member whose key fails a
        check is left out with a warning in the log, as the app does; in a private chat, it
        raises `EncryptionError`. The bot's own key is not in the list: `seal` adds it.

        Args:
            chat: The chat's ID, or a chat object such as a `Chat`, a `ChatRef` or
                ``message.chat``.

        Returns:
            The `PublicKey` of every member the message can be sealed to.

        Raises:
            EncryptionError: Nobody in the chat has a usable key, or the key of the person in
                a private chat can't be used.
            KeyChangedError: With ``pin_keys=True``, a member's key is not the one pinned for
                them.
            ChatIsOpenError: The chat is readable, not end-to-end encrypted.
            NoUserKeyError: The person in a private chat has no published key.
        """
        chat_id = _chat_id(chat)
        keys = self._fetch_keys_blocking(chat_id)
        return self._recipients_from(chat_id, keys, self._known_kind(chat_id) == ChatKind.PRIVATE)

    def seal(self, chat: ChatLike, text: str) -> str:
        """Encrypts ``text`` for everyone in an end-to-end encrypted chat, signed by the bot.

        Nothing is sent. The text is sealed to the keys `recipients` gives and to the bot's own
        key, and signed with the bot's key: what `send_message` does before sending, with the
        same checks. Unlike `send_message`, the text is not checked against the server's
        limits.

        Args:
            chat: The chat's ID, or a chat object such as a `Chat`, a `ChatRef` or
                ``message.chat``.
            text: The text to encrypt.

        Returns:
            The ASCII-armored OpenPGP message, to send as ``armored``.

        Raises:
            EncryptionError: The bot has no private key, or the message can't be sealed to the
                chat's keys.
            KeyChangedError: With ``pin_keys=True``, a member's key is not the one pinned for
                them.
            ChatIsOpenError: The chat is readable, not end-to-end encrypted.

        Example:
            What `send_message` does, by hand::

                armored = bot.seal(chat_id, "draft")
                bot.api.send_message(chat_id, armored=armored)
        """
        chat_id = _chat_id(chat)
        key = self._need_key(chat_id)
        return key.seal(text, self.recipients(chat_id))

    def send_message(
        self,
        chat: ChatLike,
        text: str,
        *,
        reply_to: int | MessageBase | None = None,
        markup: InlineKeyboard | Mapping[str, Any] | None = None,
        silent: bool = False,
        encrypt: bool | None = None,
        send_id: str | uuid.UUID | None = None,
    ) -> SyncSentMessage:
        """Sends a text message, sealed and signed when the chat is end-to-end encrypted.

        Before sending, the bot finds out the chat's mode, and asks the server when it does not
        know it yet. Into a readable chat the text goes as it is. Into an encrypted chat it is
        sealed to every member with a usable key and to the bot, and signed with the bot's key
        (see `recipients`). If it can't be sealed, nothing is sent: the bot never falls back to
        plain text.

        The mode can change between the check and the send. If the chat became encrypted, the
        server refuses the text, and the bot seals it and sends it again with the same
        ``send_id``; a warning is logged, because the text did reach the server in the refused
        request. If the chat became readable, the server refuses the sealed message, and it is
        not sent again in clear: this raises, and the next send follows the new mode.

        Args:
            chat: The chat's ID, or a chat object such as a `Chat`, a `ChatRef` or
                ``message.chat``.
            text: The text: 1 to `text_max` (4096) characters, counted in UTF-16 units as
                JavaScript does. ``CR LF`` and ``CR`` line ends become ``LF``; other control
                characters, except tabs, are refused.
            reply_to: The message to quote: its ID, or the message itself. The server ignores
                a wrong ID.
            markup: Buttons under the message: an `InlineKeyboard`, or the same in the JSON
                form of the API. Never encrypted: see the warning below.
            silent: ``True`` posts without a notification. It only matters in channels.
            encrypt: ``None`` follows the chat. ``True`` refuses to send in clear
                (`EncryptionRequiredError` in a readable chat); ``False`` refuses to send
                sealed (`ValidationError` in an encrypted chat).
            send_id: A UUID that makes the send safe to repeat: the server writes one message
                per ``send_id``, and answers a repeat with the message it already wrote. One is
                made for you; pass your own, saved with your job, to make your own retries safe
                too.

        Returns:
            The `SyncSentMessage` as the server stored it, which can be edited or deleted.

        Raises:
            ValidationError: The text, the buttons or an ID breaks a rule, or
                ``encrypt=False`` in an encrypted chat. Nothing was sent.
            EncryptionRequiredError: The chat is readable, and ``encrypt=True`` or
                ``require_encryption=True``. Nothing was sent.
            EncryptionError: The chat is encrypted and the message can't be sealed: the bot
                has no private key, nobody has a usable key, or the key of the person in a
                private chat can't be used. Nothing was sent.
            KeyChangedError: With ``pin_keys=True``, a member's key is not the one pinned for
                them. Nothing was sent.
            WrongBodyKindError: The chat became readable since the bot last looked. The sealed
                message was refused, and is not sent again in clear.
            ChatIsOpenError: The same, noticed while fetching the members' keys.
            ForbiddenError: The bot may not write in this chat.
            BlockedByUserError: The person blocked the bot.
            UserGoneError: The person closed their account.
            ChatNotFoundError: There is no such chat, or the bot is not in it.
            SendIdConflictError: The ``send_id`` was used already, in another chat.

        Examples:
            Into any chat the bot is in::

                bot.send_message("48213907", "Your report is ready")

            With buttons, and a ``send_id`` saved with the job, so that a job run twice still
            sends once::

                keyboard = InlineKeyboard.build(
                    [Button.callback("Monday", "day:mon"), Button.callback("Friday", "day:fri")],
                )
                bot.send_message(
                    group_id, "Vote for the next meetup", markup=keyboard, send_id=job.send_id
                )

        Warning:
            Buttons are never encrypted: their text and their callback data travel in clear,
            even in an encrypted chat. Don't put personal data in them.

        See Also:
            `SyncMessage.reply` and `SyncMessage.answer`, which send into the chat a message
            came from.
        """
        chat_id = _chat_id(chat)
        text = v.check_text(text, limit=self.text_max)
        reply_id = _message_id(reply_to) if reply_to is not None else None
        send_id = send_id or uuid.uuid4()
        for attempt in range(2):
            mode, kind = self._mode(chat_id)
            try:
                body = self._body(mode, chat_id, text, encrypt)
                sent = self._api.send_message(
                    chat_id,
                    body.get("text"),
                    armored=body.get("armored"),
                    reply_to=reply_id,
                    markup=markup,
                    silent=silent or None,
                    send_id=send_id,
                    chat_kind=kind,
                )
            except (WrongBodyKindError, ChatIsOpenError) as exc:
                self._mode_changed(chat_id, kind)
                if attempt == 0 and getattr(exc, "expected", None) == "armored":
                    log.warning("chat %s turned end-to-end encrypted; sealing and sending again", chat_id)
                    continue
                raise
            return SyncSentMessage.bind(sent, self)
        raise AssertionError("unreachable")  # pragma: no cover

    def edit_message(
        self,
        chat: ChatLike,
        message: int | SentMessageBase,
        text: str | None = None,
        *,
        markup: InlineKeyboard | Mapping[str, Any] | None = KEEP,
        encrypt: bool | None = None,
    ) -> SyncSentMessage:
        """Edits one of the bot's messages: new text, new buttons, or both.

        The bot can edit only its own ordinary messages, at any age, and people see the edit
        live. New text is sealed like a send when the chat is end-to-end encrypted, and a
        change of the chat's mode is handled as in `send_message`.

        Args:
            chat: The chat's ID, or a chat object such as a `Chat`, a `ChatRef` or
                ``message.chat``.
            message: The bot's message: its ID, or the `SyncSentMessage` a send returned.
            text: The new text, with the same rules as in `send_message`. ``None`` keeps the
                text.
            markup: The buttons. :data:`KEEP`, the default, keeps them; ``None`` removes them;
                a keyboard replaces them. Never encrypted.
            encrypt: ``None`` follows the chat. ``True`` refuses to send the text in clear;
                ``False`` refuses to send it sealed.

        Returns:
            The edited message, as the server stored it.

        Raises:
            ValidationError: There is nothing to change (no ``text``, and ``markup`` left at
                :data:`KEEP`), or the text, the buttons or an ID breaks a rule.
            MessageNotFoundError: There is no such message in this chat.
            ForbiddenError: The message is not one of the bot's own ordinary messages.
            EncryptionRequiredError: The chat is readable, and ``encrypt=True`` or
                ``require_encryption=True``.
            EncryptionError: The chat is encrypted and the new text can't be sealed.
            WrongBodyKindError: The chat became readable since the bot last looked. The sealed
                text was refused, and is not sent again in clear.
            ChatIsOpenError: The same, noticed while fetching the members' keys.

        Example:
            A message that counts::

                sent = bot.send_message(chat_id, "Counting...")
                for n in range(1, 4):
                    time.sleep(1)
                    bot.edit_message(chat_id, sent, f"Counting... {n}")
        """
        chat_id = _chat_id(chat)
        message_id = _message_id(message)
        if text is not None:
            text = v.check_text(text, limit=self.text_max)
        for attempt in range(2):
            mode, kind = self._mode(chat_id)
            try:
                body = self._body(mode, chat_id, text, encrypt) if text is not None else {}
                edited = self._api.edit_message(chat_id, message_id, markup=markup, chat_kind=kind, **body)
            except (WrongBodyKindError, ChatIsOpenError) as exc:
                self._mode_changed(chat_id, kind)
                if attempt == 0 and getattr(exc, "expected", None) == "armored":
                    log.warning("chat %s turned end-to-end encrypted; sealing and sending again", chat_id)
                    continue
                raise
            return SyncSentMessage.bind(edited, self)
        raise AssertionError("unreachable")  # pragma: no cover

    def delete_message(self, chat: ChatLike, message: int | MessageBase | SentMessageBase) -> bool:
        """Deletes a message for everyone, for good.

        The bot can delete its own messages anywhere, and other people's in a group or channel
        where it has the "delete messages" right. Secret messages and service notices can't be
        deleted.

        Args:
            chat: The chat's ID, or a chat object such as a `Chat`, a `ChatRef` or
                ``message.chat``.
            message: The message: its ID, a received `SyncMessage`, or a `SyncSentMessage`.

        Returns:
            ``True``. When an attempt timed out and the retry finds the message gone, the first
            attempt worked, and the result is ``True`` too.

        Raises:
            MessageNotFoundError: There is no such message, or it is gone already.
            ForbiddenError: The bot may not delete this message.
            ChatNotFoundError: There is no such chat, or the bot is not in it.
        """
        return self._api.delete_message(_chat_id(chat), _message_id(message))

    def answer_callback(
        self, callback_id: str, text: str | None = None, *, alert: bool = False, url: str | None = None
    ) -> bool:
        """Answers a button press, which stops its spinner.

        Each press can be answered once, within 60 seconds. The answer can also show a line of
        text, a box, or a link. In a handler, `SyncCallbackQuery.answer` does the same. The bot
        remembers the answer, so it does not send its automatic empty one after the handler
        (see ``auto_answer_callbacks``).

        Args:
            callback_id: The ID of the press, ``query.id``.
            text: Up to 200 characters on one line, shown over the chat for a moment. ``None``
                shows nothing.
            alert: ``True`` shows ``text`` in a box the person has to close.
            url: An ``http`` or ``https`` address, up to 1024 characters, that the phone offers
                to open.

        Returns:
            ``True``. A retry after a timeout that finds the press answered already counts as
            done.

        Raises:
            CallbackExpiredError: More than 60 seconds have passed since the press.
            CallbackAnsweredError: The press was answered already.
            CallbackNotFoundError: There is no such press.

        Warning:
            Answers are not encrypted, even in an encrypted chat.
        """
        result = self._api.answer_callback(callback_id, text, alert=alert, url=url)
        self._note_answered(callback_id)
        return result

    def send_typing(self, chat: ChatLike, typing: bool = True) -> bool:
        """Shows "typing..." in a chat for a few seconds.

        The indicator goes away by itself, or when the bot's message arrives. The server shows
        at most one every 3 seconds per chat; a call in between does nothing and returns
        ``False``. It is not tried again after a network error: a late indicator is useless.

        Args:
            chat: The chat's ID, or a chat object such as a `Chat`, a `ChatRef` or
                ``message.chat``.
            typing: ``False`` clears the indicator early.

        Returns:
            ``True`` if the indicator was shown, ``False`` if one went out to this chat less
            than 3 seconds ago.

        Raises:
            ForbiddenError: The bot may not write in this chat.
            ChatNotFoundError: There is no such chat, or the bot is not in it.
        """
        return self._api.send_typing(_chat_id(chat), typing)

    def get_chat_keys(self, chat: ChatLike) -> ChatKeys:
        """Reads the public keys of an end-to-end encrypted chat, as the server lists them.

        They come straight from ``GET /chats/{id}/keys``: not cached, and not checked.
        `recipients` gives the checked keys a message is sealed to. The bot's own key is not in
        the list, and other bots are in it only when the room lets them read every message, as
        for the app.

        Args:
            chat: The chat's ID, or a chat object such as a `Chat`, a `ChatRef` or
                ``message.chat``.

        Returns:
            The `ChatKeys`, with one `UserKey` per member who has a key and, in a group, the
            member count.

        Raises:
            ChatIsOpenError: The chat is readable, not end-to-end encrypted.
            NoUserKeyError: The person in a private chat has no published key.
            ChatNotFoundError: There is no such chat, or the bot is not in it.
        """
        return self._api.get_chat_keys(_chat_id(chat))

    def get_chat_members(self, chat: ChatLike) -> ChatMembers:
        """Reads the member list of a group or channel, as a member in the bot's seat sees it.

        Where the member list is hidden, a bot that is not an admin sees the admins only.
        Phone numbers are never included.

        Args:
            chat: The group or channel: its ID, or a chat object such as a `Chat`, a `ChatRef`
                or ``message.chat``.

        Returns:
            The `ChatMembers`, with the members the bot may see and how many members there are
            in all.

        Raises:
            BadRequestError: The chat is a private chat.
            ChatNotFoundError: There is no such chat, or the bot is not in it.
        """
        return self._api.get_chat_members(_chat_id(chat))

    def get_chat_member(self, chat: ChatLike, user_id: str | int) -> Member:
        """Reads one member's seat in a group or channel.

        Args:
            chat: The group or channel: its ID, or a chat object such as a `Chat`, a `ChatRef`
                or ``message.chat``.
            user_id: The person's ID: ``"48213907"``, or a bot's ``"B48213907"``.

        Returns:
            The `Member`, with the person, their status and their admin rights.

        Raises:
            MemberNotFoundError: The person is not a member, or is hidden from the bot.
            BadRequestError: The chat is a private chat.
            ChatNotFoundError: There is no such chat, or the bot is not in it.
        """
        return self._api.get_chat_member(_chat_id(chat), user_id)

    def leave_chat(self, chat: ChatLike) -> bool:
        """Leaves a group or channel; its members are told.

        The bot also forgets what it had cached about the chat.

        Args:
            chat: The group or channel: its ID, or a chat object such as a `Chat`, a `ChatRef`
                or ``message.chat``.

        Returns:
            ``True``. A retry after a timeout that finds the bot gone already counts as done.

        Raises:
            BadRequestError: The chat is a private chat, which can't be left.
            ChatNotFoundError: There is no such chat, or the bot is not in it.
        """
        chat_id = _chat_id(chat)
        left = self._api.leave_chat(chat_id)
        self._forget_chat(chat_id)
        return left

    def resolve(self, username: str) -> Resolved:
        """Turns a public handle into an ID (``GET /resolve/{username}``).

        For the handle somebody writes at the bot, when no update has handed that ID over.
        People, groups and channels share one pool of names, so a handle belongs to exactly
        one of the three: `Resolved.kind` says which and `Resolved.id` is filled in either
        way. A leading ``@`` and the case do not matter.

        Nothing is unlocked by knowing the number. Sending still needs the bot to be in the
        chat, and `SyncBot.get_user_key` still needs somebody the bot may look up.

        Args:
            username: The handle, with or without the ``@``.

        Returns:
            A `Resolved`: the ID, the handle it goes by, and either the person (`ChatPeer`)
            or the chat (`ChatCard`).

        Raises:
            ValidationError: Not a handle: 3-32 characters, letters, digits and underscore.
            UsernameNotFoundError: Nobody and no chat answers to it. Also the answer for a
                group with no public name and for an account that has been closed.
            FloodWaitError: More than 60 lookups this minute, where a handle nobody holds
                counts as five.

        Example:
            Answer "chi e @anna"::

                @bot.on_text()
                def who(message: Message) -> None:
                    found = bot.resolve(message.text.split("@")[-1])
                    message.reply(f"{found.mention} ha id {found.id}")
        """
        return self._api.resolve(username)

    def get_user_key(self, user_id: str | int) -> UserKey:
        """Reads one person's public key (``GET /users/{id}/key``).

        The key is not checked here: `PublicKey.from_user_key` reads it and checks its
        fingerprint.

        Args:
            user_id: The person's ID (``"48213907"``, or a bot's ``"B48213907"``). The bot must
                be able to look them up, as for ``get_user``.

        Returns:
            The `UserKey`, with the user ID, the fingerprint and the armored public key.

        Raises:
            UserNotFoundError: There is no such person, or the bot shares no chat with them.
            NoUserKeyError: The person never published a key.
        """
        return self._api.get_user_key(user_id)

    def get_commands(self, scope: str | None = None) -> Any:
        """Reads the bot's command menus: every scope, or one.

        Args:
            scope: ``None`` for every scope, or one of ``"default"``, ``"private"``,
                ``"groups"``, ``"groupAdmins"`` and ``"chat:<id>"`` (``"chat:G48213907"``).

        Returns:
            With no ``scope``, a list of `CommandScope`, one per scope with its commands. With
            a ``scope``, the list of `Command` of that scope.
        """
        return self._api.get_commands(scope)

    def set_commands(
        self, commands: Iterable[Command | Mapping[str, Any] | tuple[str, str]], scope: str | None = None
    ) -> CommandScope:
        """Replaces the whole command menu of one scope.

        The app shows the menu when someone types ``/``. The owner can change the same lists
        in the app: whoever writes last wins. `sync_commands` builds the list from the bot's
        own handlers.

        Args:
            commands: Up to 100 commands: `Command` objects, dicts with ``command`` and
                ``description``, or ``(name, description)`` pairs. A name has 1 to 32
                characters of ``a-z``, ``0-9`` and ``_`` (a leading ``/`` is dropped, and
                capitals are made lowercase); a description has 1 to 256 characters. When a
                name comes twice, the first one is kept.
            scope: Which menu: ``"default"`` (also when ``None``), ``"private"``,
                ``"groups"``, ``"groupAdmins"`` or ``"chat:<id>"`` (``"chat:G48213907"``).

        Returns:
            The `CommandScope` as the server saved it.

        Raises:
            ChatNotFoundError: A ``chat:G…`` or ``chat:C…`` scope names a chat the bot is not in.

        Example:
            A menu for every chat, and one for a single group::

                bot.set_commands([("start", "Say hello"), ("help", "What I can do")])
                bot.set_commands([("rules", "Show the rules")], scope="chat:G48213907")
        """
        return self._api.set_commands(commands, scope)

    def delete_commands(self, scope: str | None = None) -> CommandScope:
        """Empties the command menu of one scope.

        An empty scope does not hide the wider ones: the app shows the next scope that has
        commands, ``"default"`` last.

        Args:
            scope: Which menu: ``"default"`` (also when ``None``), ``"private"``,
                ``"groups"``, ``"groupAdmins"`` or ``"chat:<id>"`` (``"chat:G48213907"``).

        Returns:
            The `CommandScope`, now with no commands.
        """
        return self._api.delete_commands(scope)

    def sync_commands(self, scope: str | None = None) -> CommandScope:
        """Publishes the commands registered with a description as the menu of one scope.

        Every `Router.command` with a ``description``, on the bot and on its included routers,
        goes into the list; when a name comes twice, the first description wins. It replaces
        the whole list of that scope, as `set_commands` does. An `on_startup` hook is a good
        place to call it.

        Args:
            scope: Which menu: ``"default"`` (also when ``None``), ``"private"``,
                ``"groups"``, ``"groupAdmins"`` or ``"chat:<id>"`` (``"chat:G48213907"``).

        Returns:
            The `CommandScope` as the server saved it.

        Raises:
            ConfigurationError: No command was registered with a description.

        Example:
            Publish the menu at every start::

                @bot.command("weather", description="Forecast for a town")
                def weather(message, command): ...


                @bot.on_startup
                def ready(bot):
                    bot.sync_commands()
        """
        declared = self.declared_commands()
        if not declared:
            raise ConfigurationError("no command was registered with a description")
        return self._api.set_commands(declared, scope)

    def get_webhook(self) -> WebhookInfo:
        """Reads the webhook, if one is set, and how its deliveries are going.

        Works while the owner has paused the bot.

        Returns:
            The `WebhookInfo`, with the address, whether it has a secret, how many updates are
            waiting, and the failures and last error of the deliveries.
        """
        return self._api.get_webhook()

    def set_webhook(self, url: str, secret: str | None = None) -> WebhookInfo:
        """Sets a webhook: from now on the server POSTs every update to ``url``.

        Setting it ends any poll in progress, and the server refuses polls while it is set.
        You rarely need this: `create_webhook_app` with a ``url`` registers the webhook every
        time the bot starts.

        Args:
            url: The address: ``https``, up to 1024 characters, with no user name, password or
                ``#fragment``, pointing to a public IP address.
            secret: 1 to 256 characters of ``A-Z a-z 0-9 _ -``, sent back in the
                ``X-Hidesay-Bot-Api-Secret-Token`` header of every delivery. Always set one:
                without it, anyone who finds the address can send fake updates.

        Returns:
            The `WebhookInfo` of the new webhook.

        Raises:
            BadWebhookError: The server refused the address, for example because it points to
                a private IP address.
        """
        return self._api.set_webhook(url, secret)

    def delete_webhook(self) -> WebhookInfo:
        """Removes the webhook, so the bot can poll again.

        The updates that queued meanwhile are not lost: the next poll gets them. Works while
        the owner has paused the bot.

        Returns:
            The `WebhookInfo`, now with no ``url``; ``pending`` counts the waiting updates.
        """
        return self._api.delete_webhook()

    def publish_key(self, key: BotKey | None = None) -> PublishedKey:
        """Publishes the bot's public key, which makes its private chats end-to-end encrypted.

        It sends the public part of the key (``PUT /key``) with a fingerprint computed from the
        key itself, so the two always match: a wrong one would make phones seal to a
        key the bot does not have. A v6 key is refused before anything is sent, because phones
        can't use it. Afterwards `me` is read again and shows the new mode. Publishing does not
        change the key the bot uses: to switch to a new key, use `rotate_key`.

        Args:
            key: The key to publish. ``None`` publishes the bot's own key.

        Returns:
            The `PublishedKey`, with the fingerprint and the bot's new mode.

        Raises:
            ConfigurationError: There is no key to publish.
            InvalidKeyError: The key is a v6 key.

        Warning:
            Publishing is one-way: there is no API to remove the key. Only the bot's owner can
            make the bot readable again, from the console in the app.
        """
        key = key or self.key
        if key is None:
            raise ConfigurationError("no key to publish: pass one or set pgp_private_key")
        if key.public_key.version != 4:
            raise InvalidKeyError("only v4 keys can be published: phones on PGPainless 1.7 can't use v6 keys")
        result = self._api.set_key(key.export_public(), key.fingerprint)
        self.me = self._api.get_me()
        self._modes.drop()
        self._chats.drop()
        log.info("published key %s: the bot is protected", key.fingerprint)
        return result

    def rotate_key(self, new_key: BotKey) -> PublishedKey:
        """Publishes ``new_key`` and makes it the bot's key.

        From then on, phones seal to the new key. Messages sealed to the old one may still be
        on their way, or waiting on the server, for up to 72 hours: the old key stays loaded,
        and is still tried for decryption as long as this process runs. When you restart the
        bot, pass the old key in ``pgp_previous_keys`` for at least 72 hours.

        Args:
            new_key: The new private key. `BotKey.generate` makes one of the kind phones can
                use.

        Returns:
            The `PublishedKey`, with the new fingerprint.

        Raises:
            InvalidKeyError: ``new_key`` is a v6 key, which phones can't use.

        Example:
            Make a new key, save it, and switch to it::

                new_key = BotKey.generate("Weather", username="weatherbot", passphrase=passphrase)
                new_key.save("bot-key-2026-11.asc", passphrase=None)
                bot.rotate_key(new_key)
        """
        result = self.publish_key(new_key)
        if self.key is not None and self.key.fingerprint != new_key.fingerprint:
            self._previous_keys.insert(0, self.key)
        self.key = new_key
        return result

    def revoke_token(self) -> bool:
        """Burns the bot's token, for good.

        From then on every call with this token fails, the bot's own included, until the owner
        makes a new token in the console; the owner is told at once. Use it when the token has
        leaked. It works while the owner has paused the bot.

        Returns:
            ``True``. A retry after a timeout that finds the token burnt already counts as
            done.

        Warning:
            There is no undo: every call made with the token fails from then on, with
            `AuthenticationError`, and a polling bot stops.
        """
        return self._api.revoke_token()

    def validate_web_bot_data(self, init_data: str, *, max_age: float | None = WEB_BOT_DATA_MAX_AGE) -> WebBotData:
        """Checks the ``initData`` a page of this bot's Web-Bot sent, and reads it.

        The bot's ID and the server's Web-Bot key are fetched the first time and kept, so
        after that the check runs without any network. See
        `~hidesay_bot.validate_web_bot_data` for what is checked.

        Args:
            init_data: The string as the page sent it (``window.Hidesay.WebBot.initData``).
            max_age: The oldest data accepted, in seconds since it was signed, or ``None`` to
                accept any age.

        Returns:
            What the data says, now that it is known to come from the server.

        Raises:
            WebBotDataError: The data is refused; ``reason`` says why.

        Example:
            In a route of any web framework::

                try:
                    data = bot.validate_web_bot_data(request.headers["x-init-data"])
                except WebBotDataError:
                    return Response(status_code=401)
                cart = carts[data.user.id]
        """
        me = self.get_me()
        if self._web_bot_key is None:
            self._web_bot_key = self._api.get_web_bot_key()
        return validate_web_bot_data(init_data, bot_id=me.id, public_key=self._web_bot_key, max_age=max_age)

    def download_media(self, media: Media | str, *, max_bytes: int = MEDIA_MAX_BYTES) -> bytes:
        """Downloads the file behind a `Media`, decrypted when it came from an encrypted chat.

        The file comes from the ``/media/<id>`` address of the API server; the token is not
        sent, because that address does not use it. An attachment of an encrypted chat is
        decrypted with the bot's key. In a private chat with the bot this always works. In an
        encrypted group the phones seal attachments only to the keys of the group, which have
        the bot among them only when the room lets it read every message (`Seat.reads_all`);
        otherwise it raises `NotARecipientError`. When the message gave the file's SHA-256
        (`Media.sha256`), the file is checked against it before it is handed over.

        Args:
            media: An item of ``message.media``. A ``/media/<id>`` path or a bare ID works too,
                but is downloaded as it is, never decrypted: pass the `Media` to get it
                decrypted.
            max_bytes: The largest file accepted, in bytes, 64 MiB by default. It also limits
                the decrypted size.

        Returns:
            The file's bytes, decrypted when needed.

        Raises:
            NotARecipientError: The file was not sealed to any of the bot's keys.
            DecryptionError: The bot has no private key, or the file can't be decrypted: it is
                damaged, it opens to more than ``max_bytes``, or it is compressed. Phones never
                compress attachments, and a compressed file could open to many gigabytes.
            ValidationError: The file is bigger than ``max_bytes``, or the address is not a
                media address of this server.
            MediaIntegrityError: The file's SHA-256 is not the one its message gives.
            NotFoundError: The server has no such file.

        Example:
            Save every attachment of a message::

                @bot.on_message(f.has_media)
                def save(message):
                    for item in message.media:
                        data = bot.download_media(item)
                        Path(f"{item.id}.bin").write_bytes(data)

        Note:
            The message that carried the attachment had its signature checked, but the file
            itself is not checked against a signature.
        """
        data = self._api.download_media(media, max_bytes=max_bytes)
        if isinstance(media, Media) and media.encrypted:
            opened = self._decrypt_any(data, [], max_size=max_bytes, allow_compression=False)
            data = opened.data
        return self._check_integrity(media, data)

    def trust_key(self, user_id: str | int, fingerprint: str) -> None:
        """Accepts ``fingerprint`` as the key of ``user_id`` from now on.

        With ``pin_keys=True`` the bot refuses a person's key when it is not the one pinned for
        them, with `KeyChangedError`. Once you have checked the new key with the person, out of
        band, call this to pin it: sends to them work again, and their new messages reach the
        handlers. Their messages that failed meanwhile wait in the failed list, and
        `retry_failed` runs them again. The pin is saved in the storage.

        Args:
            user_id: The person's ID: ``"48213907"``, or a bot's ``"B48213907"``.
            fingerprint: The fingerprint you checked: 40 (v4) or 64 (v6) hex digits, without
                spaces.

        Raises:
            ValidationError: ``fingerprint`` is not 40 or 64 hex digits.

        Example:
            After checking the fingerprint with the person::

                bot.trust_key(user_id, "3F1A9C0D52E7B8A46C110D93E2F47A559B10C6D2")

        Note:
            The bot opens a `SQLiteStorage` when it starts and closes it when it stops: with
            one, call this while `run` runs (from a handler, from a hook, or from another
            thread).
        """
        self.storage.set_pin(v.check_user_id(user_id), v.check_fingerprint(fingerprint))
        self._keys.drop()

    def failed_updates(self, limit: int = 100) -> list[FailedUpdate]:
        """Lists the updates whose handlers failed, oldest first.

        An update fails when a handler raises and no error handler takes the error. It is
        confirmed to the server like any other, so it does not block the queue, and kept in
        the failed list with the error. For an update from an encrypted chat, a handler's own
        exception is kept as its type and the place it was raised, without its message, which
        could quote the decrypted text. The list keeps the newest ones only: 1000 with a
        `MemoryStorage`, 10 000 per bot with a `SQLiteStorage` (their ``max_failed`` option).

        Args:
            limit: The most updates to return.

        Returns:
            The failed updates, each a `FailedUpdate` with ``update_id``, ``payload`` (the
            update as the server sent it), ``error`` and ``failed_at``.

        Example:
            Look at what failed::

                for item in bot.failed_updates():
                    print(item.update_id, item.failed_at, item.error)

        Note:
            The bot opens a `SQLiteStorage` when it starts and closes it when it stops: with
            one, call this while `run` runs (from a handler, from a hook, or from another
            thread).
        """
        return self.storage.failed(limit)

    def retry_failed(self, update_ids: Sequence[int] | None = None) -> int:
        """Puts failed updates back in the queue, to run through the handlers again.

        They run as if they had just arrived: at once when the bot is running, otherwise at
        its next start. A common pattern is to fix the bug, restart the bot, and call this.

        Args:
            update_ids: The IDs of the updates to retry. ``None`` retries all of them.

        Returns:
            How many updates were put back.

        Note:
            The bot opens a `SQLiteStorage` when it starts and closes it when it stops: with
            one, call this while `run` runs (from a handler, from a hook, or from another
            thread).
        """
        count = self.storage.requeue_failed(update_ids)
        engine, loop = self._engine, self._loop
        if count and engine is not None and self._running and loop is not None and not loop.is_closed():
            # handlers run on worker threads, the engine on the loop: hand it over
            asyncio.run_coroutine_threadsafe(engine.recover(quiet=True), loop)
        return count

    def discard_failed(self, update_ids: Sequence[int] | None = None) -> int:
        """Forgets failed updates, for good.

        Args:
            update_ids: The IDs of the updates to forget. ``None`` forgets all of them.

        Returns:
            How many updates were forgotten.

        Note:
            The bot opens a `SQLiteStorage` when it starts and closes it when it stops: with
            one, call this while `run` runs (from a handler, from a hook, or from another
            thread).
        """
        return self.storage.discard_failed(update_ids)

    # --- opening, without an event loop: SyncBot's methods run in the caller's thread ---

    def _open_blocking(self, message: MessageBase, *, quiet: bool = False) -> MessageBase:
        try:
            opened = self._open_one_blocking(message)
        except (DecryptionError, SignatureError, KeyChangedError) as exc:
            if not quiet:
                raise
            log.debug("message %s in chat %s left sealed: %s", message.id, message.chat.id, exc)
            opened = message
        quoted = opened.reply_to_message
        if quoted is not None and quoted.armored is not None:
            opened = replace(opened, reply_to_message=self._open_blocking(quoted, quiet=True))
        return opened

    def _open_one_blocking(self, message: MessageBase) -> MessageBase:
        if message.armored is None:
            return message
        candidates: list[PublicKey] = []
        reason: str | None = None
        if self.verify_signatures != "off":
            keys: ChatKeys | None = None
            try:
                keys = self._fetch_keys_blocking(message.chat.id)
            except (APIError, NetworkError) as exc:
                log.debug("chat %s: keys not fetched (%s), signature left unchecked", message.chat.id, exc)
            candidates, reason = self._sender_candidates(message, keys)
        try:
            result = self._decrypt_any(message.armored, candidates, max_size=_MESSAGE_PLAINTEXT_MAX)
        except DecryptionError:
            try:
                readable = self._mode(message.chat.id)[0] == ChatMode.OPEN
            except (APIError, NetworkError):
                readable = False
            if readable:
                return replace(message, text=message.armored, armored=None)
            raise
        if reason is not None and result.verification.status == SignatureStatus.NOT_CHECKED:
            result = replace(result, verification=Verification(SignatureStatus.NOT_CHECKED, detail=reason))
        return self._apply_decryption(message, result)

    # --- files, stickers, forwards, comments ---

    def upload_file(self, data: bytes | str | os.PathLike[str]) -> UploadedFile:
        """Uploads a file as it is, for the ``media`` of a send into a readable chat.

        `send_media` does the upload for you, sealed when the chat is encrypted: this is for
        building a send by hand with `api`. The file goes up under a neutral name and type.

        Args:
            data: The file: its bytes, or the path of a file to read.

        Returns:
            The `UploadedFile`, with the ID to send.

        Raises:
            ValidationError: The file can't be read, or is larger than `upload_max`.
            FloodWaitError: More than 30 uploads this minute.
        """
        return self._api.upload_file(_bytes_of(data, "data"), limit=self.upload_max)

    def send_media(
        self,
        chat: ChatLike,
        files: FileLike | Sequence[FileLike],
        *,
        caption: str | None = None,
        reply_to: int | MessageBase | None = None,
        markup: InlineKeyboard | Mapping[str, Any] | None = None,
        silent: bool = False,
        mentions: Iterable[str | int] | None = None,
        encrypt: bool | None = None,
        send_id: str | uuid.UUID | None = None,
    ) -> SyncSentMessage:
        """Sends a file, or an album of 2 to 10 pictures and clips, with a caption.

        Into a readable chat each file is uploaded as it is, and the server writes the message
        the phones read. Into an end-to-end encrypted chat each file is sealed first to every
        member with a usable key and to the bot (unsigned, as the phones seal files), uploaded
        sealed, and the message that describes the files is sealed and signed like a text: the
        server sees neither the files nor what they are. Every file carries its SHA-256, which
        the phones check after downloading.

        A change of the chat's mode is handled as in `send_message`: if a readable chat turned
        encrypted, the files are sealed and sent again with the same ``send_id`` (the ones
        uploaded in clear stay on the server, unused, and a warning is logged).

        Args:
            chat: The chat's ID, or a chat object such as a `Chat`, a `ChatRef` or
                ``message.chat``.
            files: One file, or a list of 2 to 10 pictures and clips for an album. Each one is an
                `InputFile`, the bytes, or the path of a file; the kind and the type are guessed
                from the name when they are not given.
            caption: The text under the files, with the rules of a text; blank means none.
            reply_to: The message to quote: its ID, or the message itself.
            markup: Buttons under the message. Never encrypted.
            silent: ``True`` posts without a notification. It only matters in channels.
            mentions: In a group, the IDs of the people the caption tags, up to 20.
            encrypt: ``None`` follows the chat. ``True`` refuses to send in clear, ``False``
                refuses to send sealed.
            send_id: A UUID that makes the send safe to repeat. One is made for you.

        Returns:
            The `SyncSentMessage` as the server stored it.

        Raises:
            ValidationError: No files or more than 10, an album with something that is not a
                picture or a clip, a file over `upload_max`, a field that breaks a rule, or the
                caption. Nothing was sent.
            EncryptionRequiredError: The chat is readable, and ``encrypt=True`` or
                ``require_encryption=True``. Nothing was sent.
            EncryptionError: The chat is encrypted and the files can't be sealed.
            ForbiddenError: The bot may not send files, or voice notes, here.
            FloodWaitError: More than 30 uploads this minute, or too many messages.

        Example:
            A picture from disk, and an album::

                bot.send_media(chat_id, "sunset.jpg", caption="Tonight")
                bot.send_media(chat_id, [InputFile.from_path(p) for p in photos])
        """
        chat_id = _chat_id(chat)
        items = input_files(files, limit=self.upload_max)
        caption = v.check_caption(caption, limit=self.text_max)
        reply_id = _message_id(reply_to) if reply_to is not None else None
        tagged = v.check_mentions(mentions) if mentions is not None else None
        send_id = send_id or uuid.uuid4()
        for attempt in range(2):
            mode, kind = self._mode(chat_id)
            try:
                if self._body_kind(mode, chat_id, encrypt) == "armored":
                    key = self._need_key(chat_id)
                    recipients = self.recipients(chat_id)
                    envelopes = []
                    for item in items:
                        sealed = self._seal_file(key, item.data, recipients)
                        uploaded = self._api.upload_file(sealed, limit=self.upload_max)
                        envelopes.append(item.envelope(uploaded.id))
                    armored = key.seal(wire.media_body(envelopes, caption), recipients)
                    sent = self._api.send_message(
                        chat_id,
                        armored=armored,
                        reply_to=reply_id,
                        markup=markup,
                        silent=silent or None,
                        send_id=send_id,
                        mentions=tagged,
                        chat_kind=kind,
                    )
                else:
                    media: list[MediaIn] = []
                    for item in items:
                        uploaded = self._api.upload_file(item.data, limit=self.upload_max)
                        media.append(item.media_in(uploaded.id))
                    sent = self._api.send_message(
                        chat_id,
                        caption,
                        media=media,
                        reply_to=reply_id,
                        markup=markup,
                        silent=silent or None,
                        send_id=send_id,
                        mentions=tagged,
                        chat_kind=kind,
                    )
            except (WrongBodyKindError, ChatIsOpenError) as exc:
                self._mode_changed(chat_id, kind)
                if attempt == 0 and getattr(exc, "expected", None) == "armored":
                    log.warning("chat %s turned end-to-end encrypted; sealing the files and sending again", chat_id)
                    continue
                raise
            return SyncSentMessage.bind(sent, self)
        raise AssertionError("unreachable")  # pragma: no cover

    def send_sticker(
        self,
        chat: ChatLike,
        sticker: StickerLike,
        *,
        media_id: str | None = None,
        reply_to: int | MessageBase | None = None,
        markup: InlineKeyboard | Mapping[str, Any] | None = None,
        silent: bool = False,
        encrypt: bool | None = None,
        send_id: str | uuid.UUID | None = None,
    ) -> SyncSentMessage:
        """Sends a sticker, sealed when the chat is end-to-end encrypted.

        A sticker of a public pack, or one the bot was shown: a `Sticker` from a message
        carries its file ID, which proves it. Into a readable chat the server writes the
        message; into an encrypted one the bot asks the server what the sticker is
        (`get_sticker`) and seals the message itself. A sticker has no text.

        Args:
            chat: The chat's ID, or a chat object such as a `Chat`, a `ChatRef` or
                ``message.chat``.
            sticker: The sticker: its ID, a `Sticker` (``message.sticker``), or a dict with
                ``id`` and ``mediaId``.
            media_id: The sticker's file ID, when ``sticker`` is a bare ID of a pack that is not
                public.
            reply_to: The message to quote: its ID, or the message itself.
            markup: Buttons under the sticker. Never encrypted.
            silent: ``True`` posts without a notification. It only matters in channels.
            encrypt: ``None`` follows the chat. ``True`` refuses to send in clear, ``False``
                refuses to send sealed.
            send_id: A UUID that makes the send safe to repeat. One is made for you.

        Returns:
            The `SyncSentMessage` as the server stored it.

        Raises:
            ValidationError: Not a sticker ID.
            StickerNotFoundError: No such sticker, or one of a pack the bot can't use.
            EncryptionRequiredError: The chat is readable, and ``encrypt=True`` or
                ``require_encryption=True``.
            EncryptionError: The chat is encrypted and the message can't be sealed.

        Example:
            Answer a sticker with the same sticker::

                @bot.on_message(f.has_sticker)
                def mirror(message):
                    bot.send_sticker(message.chat, message.sticker, reply_to=message)
        """
        chat_id = _chat_id(chat)
        ref = _sticker_ref(sticker, media_id)
        reply_id = _message_id(reply_to) if reply_to is not None else None
        send_id = send_id or uuid.uuid4()
        for attempt in range(2):
            mode, kind = self._mode(chat_id)
            try:
                if self._body_kind(mode, chat_id, encrypt) == "armored":
                    found = self._api.get_sticker(ref["id"], ref.get("mediaId"))
                    armored = self.seal(chat_id, wire.sticker_body(found))
                    sent = self._api.send_message(
                        chat_id,
                        armored=armored,
                        reply_to=reply_id,
                        markup=markup,
                        silent=silent or None,
                        send_id=send_id,
                        chat_kind=kind,
                    )
                else:
                    sent = self._api.send_message(
                        chat_id,
                        sticker=ref,
                        reply_to=reply_id,
                        markup=markup,
                        silent=silent or None,
                        send_id=send_id,
                        chat_kind=kind,
                    )
            except (WrongBodyKindError, ChatIsOpenError) as exc:
                self._mode_changed(chat_id, kind)
                if attempt == 0 and getattr(exc, "expected", None) == "armored":
                    log.warning("chat %s turned end-to-end encrypted; sealing the sticker and sending again", chat_id)
                    continue
                raise
            return SyncSentMessage.bind(sent, self)
        raise AssertionError("unreachable")  # pragma: no cover

    def get_sticker(self, sticker_id: int, media_id: str | None = None) -> Sticker:
        """Reads a sticker the bot may send (``GET /stickers/{id}``).

        Args:
            sticker_id: The sticker's ID.
            media_id: Its file ID, as a message carried it, for a pack that is not public.

        Returns:
            The `Sticker`.

        Raises:
            StickerNotFoundError: No such sticker, or one of a pack the bot can't use.
        """
        return self._api.get_sticker(sticker_id, media_id)

    def forward_message(
        self,
        chat: ChatLike,
        from_chat: ChatLike,
        message: int | MessageBase,
        *,
        hide_sender: bool = False,
        silent: bool = False,
        send_id: str | uuid.UUID | None = None,
    ) -> SyncSentMessage:
        """Sends a message the bot was handed on into another chat it is in.

        Between two readable chats the server does it (``POST /messages/forward``): "forwarded
        from" goes over it, and the files are copied for the bot. When either chat is end-to-end
        encrypted the bot does it itself, the way a phone does: it opens the message, downloads
        and opens its files, seals them again to the new chat's keys, uploads them, and seals
        the message with "forwarded from" over it. A forward of a forward keeps the first
        origin. The bot can't know whether the person who wrote it lets a forward link to their
        profile, so its own forwards show the person's name without the link, unless the message
        is the bot's own.

        A readable chat takes from the bot only what the server writes for it, which has no
        "forwarded from": a message of an encrypted chat goes into a readable one only as a
        copy (``hide_sender=True``), the words and the files without the link card.

        A chat that protects its content does not let its messages out, and comments are not
        forwarded.

        Args:
            chat: Where to: the chat's ID, or a chat object such as ``message.chat``.
            from_chat: The chat the message is in.
            message: The message: its ID, or the message itself (a received `SyncMessage` saves
                fetching and opening it again).
            hide_sender: ``True`` sends a plain copy, without "forwarded from".
            silent: ``True`` posts without a notification. It only matters in channels.
            send_id: A UUID that makes the forward safe to repeat. One is made for you.

        Returns:
            The `SyncSentMessage` the forward became.

        Raises:
            ValidationError: The message is a comment, its chat protects its content, or it
                would go from an encrypted chat into a readable one without
                ``hide_sender=True``.
            EncryptionRequiredError: The target is readable, and ``require_encryption=True``.
            EncryptionError: The target is encrypted and the message can't be sealed to it.
            NotARecipientError: A file of the message was not sealed to the bot.
            MessageNotFoundError: No such message, or one the bot was never handed.
            ForbiddenError: The bot may not write in the target.

        Example:
            Pass every message with a file on to an archive channel::

                @bot.on_message(f.has_media)
                def archive(message):
                    message.forward_to(ARCHIVE_ID)
        """
        target = _chat_id(chat)
        source = _chat_id(from_chat)
        message_id = _message_id(message)
        send_id = send_id or uuid.uuid4()
        source_mode, _ = self._mode(source)
        target_mode, target_kind = self._mode(target)
        # the forward below writes in clear: the same promise as a send
        self._body_kind(target_mode, target, None)
        if source_mode == ChatMode.OPEN and target_mode == ChatMode.OPEN:
            try:
                sent = self._api.forward_message(
                    target,
                    source,
                    message_id,
                    hide_sender=hide_sender or None,
                    silent=silent or None,
                    send_id=send_id,
                    chat_kind=target_kind,
                )
                return SyncSentMessage.bind(sent, self)
            except ChatIsProtectedError:
                # the message itself is sealed, or one of the chats just changed mode
                self._mode_changed(source, _kind_of(source))
                self._mode_changed(target, target_kind)
        original = self._forwarded_message(source, message, message_id)
        raw = wire.body_of(original)
        if not hide_sender and not raw.startswith(wire.FORWARD):
            sender = original.from_user
            linkable = sender is not None and self._is_me(sender.id)
            raw = wire.forward_body(wire.origin_of(original.chat, original, linkable=linkable), raw)
        for attempt in range(2):
            mode, kind = self._mode(target)
            try:
                if self._body_kind(mode, target, None) == "armored":
                    key = self._need_key(target)
                    recipients = self.recipients(target)
                    moved: dict[str, dict[str, Any]] = {}
                    for env in wire.files_in(raw):
                        media = wire.media_of(env)
                        if media is None or media.id.lower() in moved:
                            continue
                        data = self.download_media(media, max_bytes=self.upload_max)
                        sealed = self._seal_file(key, data, recipients)
                        uploaded = self._api.upload_file(sealed, limit=self.upload_max)
                        moved[media.id.lower()] = {"media": uploaded.id}
                    armored = key.seal(wire.remap_files(raw, moved), recipients)
                    sent = self._api.send_message(
                        target, armored=armored, silent=silent or None, send_id=send_id, chat_kind=kind
                    )
                else:
                    if not hide_sender:
                        raise ValidationError(
                            f"chat {target} is readable and the message comes sealed: it can go there only as a "
                            "copy, hide_sender=True",
                            field="hide_sender",
                        )
                    sent = self._copy(target, original, silent=silent, send_id=send_id, chat_kind=kind)
            except (WrongBodyKindError, ChatIsOpenError) as exc:
                self._mode_changed(target, kind)
                if attempt == 0 and getattr(exc, "expected", None) == "armored":
                    log.warning("chat %s turned end-to-end encrypted; sealing the forward and sending again", target)
                    continue
                raise
            return SyncSentMessage.bind(sent, self)
        raise AssertionError("unreachable")  # pragma: no cover

    def _forwarded_message(self, source: str, message: int | MessageBase, message_id: int) -> MessageBase:
        """The message to forward, opened, after the checks the server makes for its own forwards."""
        if (
            isinstance(message, MessageBase)
            and message.chat.id == source
            and (message.plaintext is not None or message.armored is None)
        ):
            original = message
        else:
            original = self._open_blocking(self._api.get_message(source, message_id))
        if original.thread_root is not None:
            raise ValidationError("comments are not forwarded", field="message")
        if original.armored is not None and original.plaintext is None:
            raise DecryptionError(f"message {message_id} of chat {source} could not be opened to forward it")
        if _kind_of(source) != ChatKind.PRIVATE and (self.get_chat(source)).protect_content:
            raise ValidationError(
                f"chat {source} protects its content: its messages can't be forwarded", field="from_chat"
            )
        return original

    def _copy(
        self, target: str, original: MessageBase, *, silent: bool, send_id: str | uuid.UUID, chat_kind: ChatKind
    ) -> SentMessageBase:
        """A message as a readable chat takes it from the bot: the words, the files, a sticker."""
        if original.sticker is not None:
            if original.sticker.id is None:
                raise ValidationError("this sticker carries no ID, so it can't be sent on", field="message")
            ref = _sticker_ref(original.sticker)
            return self._api.send_message(
                target, sticker=ref, silent=silent or None, send_id=send_id, chat_kind=chat_kind
            )
        if original.media:
            media = []
            for item in original.media:
                data = self.download_media(item, max_bytes=self.upload_max)
                uploaded = self._api.upload_file(data, limit=self.upload_max)
                media.append(_media_in(item, uploaded.id, data))
            return self._api.send_message(
                target, original.text, media=media, silent=silent or None, send_id=send_id, chat_kind=chat_kind
            )
        if not original.text:
            raise ValidationError("the message has nothing a readable chat can take", field="message")
        return self._api.send_message(
            target, original.text, silent=silent or None, send_id=send_id, chat_kind=chat_kind
        )

    def send_comment(
        self,
        chat: ChatLike,
        post: int | MessageBase,
        text: str,
        *,
        reply_to: int | MessageBase | None = None,
        as_author: bool = False,
        mentions: Iterable[str | int] | None = None,
        encrypt: bool | None = None,
    ) -> SyncSentMessage:
        """Comments under a channel post, sealed when the channel is end-to-end encrypted.

        The rules of a person's comment hold: the channel's comments are on and this post's
        are not closed, and ``reply_to`` points at a comment under the same post. A bot that
        runs the channel speaks as the channel unless the channel lets its admins sign and
        ``as_author`` is ``True``. `SyncMessage.reply` on a comment does this for you.

        The server keeps no ``sendId`` for comments, so a comment is never sent twice by a
        retry: after a network error it raises instead.

        Args:
            chat: The channel: its ID, or a chat object such as ``message.chat``.
            post: The post: its ID, or the message itself.
            text: The comment, with the rules of `send_message`.
            reply_to: A comment under the same post to answer: its ID, or the message itself.
            as_author: ``True`` signs the comment with the bot's own name, where the channel
                lets its admins sign.
            mentions: The IDs of the people the comment tags, up to 20.
            encrypt: ``None`` follows the channel. ``True`` refuses to send in clear, ``False``
                refuses to send sealed.

        Returns:
            The comment, as the server stored it.

        Raises:
            ValidationError: Not a channel, or the text breaks a rule.
            CommentsClosedError: The channel takes no comments, or this post doesn't.
            EncryptionRequiredError: The channel is readable, and ``encrypt=True`` or
                ``require_encryption=True``.
            EncryptionError: The channel is encrypted and the comment can't be sealed.
        """
        chat_id = _chat_id(chat)
        post_id = _message_id(post)
        text = v.check_text(text, limit=self.text_max)
        reply_id = _message_id(reply_to) if reply_to is not None else None
        mode, _ = self._mode(chat_id)
        body = self._body(mode, chat_id, text, encrypt)
        try:
            sent = self._api.send_comment(
                chat_id, post_id, reply_to=reply_id, as_author=as_author or None, mentions=mentions, **body
            )
        except (WrongBodyKindError, ChatIsOpenError):
            # not sent again on its own: a comment has no sendId to make a repeat safe
            self._mode_changed(chat_id, ChatKind.CHANNEL)
            raise
        return SyncSentMessage.bind(sent, self)

    def get_comments(
        self, chat: ChatLike, post: int | MessageBase, *, before: int | None = None, limit: int | None = None
    ) -> Comments:
        """Reads the comments the bot was handed under a channel post, newest first, opened.

        In an encrypted channel each comment is decrypted and its signature checked, as an
        update's would be; one that can't be opened comes back as it is, with ``text`` left
        ``None``.

        Args:
            chat: The channel: its ID, or a chat object such as ``message.chat``.
            post: The post: its ID, or the message itself.
            before: The oldest comment ID you hold, for the next page back.
            limit: How many, 1 to 100. ``None`` leaves it to the server, which gives 50.

        Returns:
            The `Comments`, with whether the post takes comments right now and each comment as
            a `SyncMessage` that can be answered.

        Raises:
            ValidationError: Not a channel, or ``before`` or ``limit`` out of range.
            MessageNotFoundError: No such post, or one the bot was never handed.
        """
        page = self._api.get_comments(_chat_id(chat), _message_id(post), before=before, limit=limit)
        opened = [self._bind_message(self._open_blocking(m, quiet=True)) for m in page.messages]
        return replace(page, messages=tuple(opened))

    def get_history(self, chat: ChatLike, *, before: int | None = None, limit: int | None = None) -> list[SyncMessage]:
        """Reads what the bot was handed in a chat, newest first, opened.

        In a private chat that is every message of it; in a group or channel the bot's own
        messages and the ones the room delivered to it, and nothing else: what the room did not
        let it read stays out, even after the room lets it read everything. In an encrypted
        chat each message is decrypted and its signature checked, as an update's would be; one
        that can't be opened comes back as it is, with ``text`` left ``None``.

        Args:
            chat: The chat's ID, or a chat object such as ``message.chat``.
            before: The oldest ID you hold, for the next page back. ``None`` for the newest.
            limit: How many, 1 to 100. ``None`` leaves it to the server, which gives 50.

        Returns:
            The messages, each a `SyncMessage` that can be answered.

        Raises:
            ValidationError: ``before`` or ``limit`` out of range.
            ChatNotFoundError: There is no such chat, or the bot is not in it.

        Example:
            What the bot was told in a chat, oldest first::

                for message in reversed(bot.get_history(chat_id, limit=100)):
                    print(message.date, message.text)
        """
        messages = self._api.get_history(_chat_id(chat), before=before, limit=limit)
        return [self._bind_message(self._open_blocking(m, quiet=True)) for m in messages]

    def get_message(self, chat: ChatLike, message_id: int) -> SyncMessage:
        """Reads one message the bot was handed, opened.

        Args:
            chat: The chat's ID, or a chat object such as ``message.chat``.
            message_id: The message.

        Returns:
            The `SyncMessage`, decrypted when it came sealed.

        Raises:
            MessageNotFoundError: No such message, or one the bot was never handed.
            DecryptionError: The message is sealed and can't be opened.
        """
        message: SyncMessage = self._bind_message(
            self._open_blocking(self._api.get_message(_chat_id(chat), message_id))
        )
        return message

    # --- reactions, pins, read marks ---

    def delete_messages(self, chat: ChatLike, messages: Iterable[int | MessageBase | SentMessageBase]) -> list[int]:
        """Deletes up to 100 messages of one chat at once, for everyone, for good.

        Each with the rules of `delete_message`. If one is not the bot's to delete, nothing is
        deleted. Messages the bot was never handed are left out.

        Args:
            chat: The chat's ID, or a chat object such as ``message.chat``.
            messages: The messages: IDs, or the messages themselves.

        Returns:
            The IDs that went, the comments taken with their posts included.

        Raises:
            ValidationError: No messages, or more than 100.
            ForbiddenError: One of them is not the bot's to delete. Nothing was deleted.
        """
        return self._api.delete_messages(_chat_id(chat), [_message_id(m) for m in messages])

    def set_reaction(
        self, chat: ChatLike, message: int | MessageBase | SentMessageBase, emoji: str
    ) -> MessageReactions:
        """Puts the bot's reaction on a message, in place of the one it had there.

        The rules of a person's reaction hold: in a group or channel reactions must be on, the
        emoji one the chat takes, and the bot allowed to react. `SyncMessage.react` does the same.

        Args:
            chat: The chat's ID, or a chat object such as ``message.chat``.
            message: The message: its ID, or the message itself.
            emoji: The emoji, or a custom emoji as the app writes it.

        Returns:
            Every reaction on the message now.

        Raises:
            ValidationError: ``emoji`` is empty or longer than 48 characters.
            ForbiddenError: Reactions are off, the chat does not take this emoji, or the bot
                may not react.
        """
        return self._api.set_reaction(_chat_id(chat), _message_id(message), emoji)

    def remove_reaction(self, chat: ChatLike, message: int | MessageBase | SentMessageBase) -> bool:
        """Takes the bot's reaction off a message.

        Args:
            chat: The chat's ID, or a chat object such as ``message.chat``.
            message: The message: its ID, or the message itself.

        Returns:
            ``True`` when the bot had a reaction there, ``False`` when it had none.
        """
        return self._api.remove_reaction(_chat_id(chat), _message_id(message))

    def pin_message(self, chat: ChatLike, message: int | MessageBase | SentMessageBase) -> list[int]:
        """Pins a message for everybody in the chat.

        In a group or channel it needs the *pin messages* right, or the members' permission to
        pin; in a private chat the person sees a notice.

        Args:
            chat: The chat's ID, or a chat object such as ``message.chat``.
            message: The message: its ID, or the message itself.

        Returns:
            The IDs of the messages pinned in the chat now.

        Raises:
            ForbiddenError: The bot may not pin messages here.
        """
        return self._api.pin_message(_chat_id(chat), _message_id(message))

    def unpin_message(self, chat: ChatLike, message: int | MessageBase | SentMessageBase) -> list[int]:
        """Unpins a message.

        Args:
            chat: The chat's ID, or a chat object such as ``message.chat``.
            message: The message: its ID, or the message itself.

        Returns:
            The IDs of the messages still pinned.

        Raises:
            ForbiddenError: The bot may not unpin messages here.
        """
        return self._api.unpin_message(_chat_id(chat), _message_id(message))

    def unpin_all(self, chat: ChatLike) -> list[int]:
        """Unpins every message of a group or channel.

        Args:
            chat: The group or channel: its ID, or a chat object such as ``message.chat``.

        Returns:
            The IDs still pinned: none.

        Raises:
            ValidationError: A private chat.
            ForbiddenError: The bot may not unpin messages here.
        """
        return self._api.unpin_all(_chat_id(chat))

    def read_chat(self, chat: ChatLike, up_to: int | MessageBase | None = None) -> int | None:
        """Marks a chat read: the ticks people see.

        In a group or channel only a bot the room lets read every message may do it.

        Args:
            chat: The chat's ID, or a chat object such as ``message.chat``.
            up_to: The newest message read: its ID, or the message itself. ``None`` marks
                everything.

        Returns:
            The ID of the last message marked read, or ``None`` when the server did not say.

        Raises:
            ReadsAllNeededError: A group or channel that does not let the bot read every
                message.
        """
        return self._api.read_chat(_chat_id(chat), _message_id(up_to) if up_to is not None else None)

    # --- chats and people ---

    def get_chats(self) -> list[Chat]:
        """Lists the groups and channels the bot is in, with its seat in each.

        Private chats are not listed. The bot keeps what it learns, as `get_chat` does.

        Returns:
            Each group and channel, as `get_chat` describes it.

        Example:
            Where the bot may not read everything::

                for chat in bot.get_chats():
                    if not chat.reads_all:
                        print(chat.title)
        """
        chats = self._api.get_chats()
        for chat in chats:
            self._note_chat(chat)
        return chats

    def get_user(self, user: str | int | Any) -> Profile:
        """Reads somebody's public profile (``GET /users/{id}``).

        Only for somebody the bot may look up: a private chat with them, a room whose member list
        shows them to the bot, or a room where their words reached it. No phone number, no presence.

        Args:
            user: The person: an ID (``"48213907"``, or a bot's ``"B48213907"``) or a `User`.

        Returns:
            The `Profile`.

        Raises:
            UserNotFoundError: No such person, or nobody the bot may look up.
        """
        return self._api.get_user(_user_id(user))

    # --- running a group or channel ---

    def update_chat(
        self,
        chat: ChatLike,
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
    ) -> Chat:
        """Changes the settings of a group or channel; a field left out stays as it is.

        Each field needs the right the app asks for it (see `Client.update_chat`). Whether
        the bot reads every message is not one of them: the room decides that in the app.

        Args:
            chat: The group or channel: its ID, or a chat object such as ``message.chat``.
            title: The new name, 1 to 64 characters.
            description: The new description, up to 500 characters; ``None`` clears it.
            permissions: What members may do, laid over what they may do now, by name:
                ``{"send_media": False}``. Groups only.
            slow_mode_seconds: Seconds between two messages of a member, 0 to 3600. Groups
                only.
            members_hidden: ``True`` shows the member list to the admins only.
            protect_content: ``True`` stops messages from being forwarded, copied or saved.
            reactions_enabled: ``False`` turns reactions off.
            allowed_reactions: The only emoji people may react with, up to 40 of at most 16
                characters each; ``None`` allows any.
            auto_delete_seconds: After how many seconds new messages delete themselves; ``0``
                turns it off.
            signatures: ``True`` shows who wrote each post. Channels only.
            signature_profiles: ``True`` links a signature to the profile. Channels only.
            admins_hidden: ``True`` keeps the admins from the subscribers. Channels only.

        Returns:
            The chat with the new settings.

        Raises:
            ValidationError: Nothing to change, or a value the server would refuse.
            ForbiddenError: The bot lacks the right one of the fields needs.
        """
        updated = self._api.update_chat(
            _chat_id(chat),
            title=title,
            description=description,
            permissions=permissions,
            slow_mode_seconds=slow_mode_seconds,
            members_hidden=members_hidden,
            protect_content=protect_content,
            reactions_enabled=reactions_enabled,
            allowed_reactions=allowed_reactions,
            auto_delete_seconds=auto_delete_seconds,
            signatures=signatures,
            signature_profiles=signature_profiles,
            admins_hidden=admins_hidden,
        )
        self._note_chat(updated)
        return updated

    def update_member(
        self,
        chat: ChatLike,
        user: str | int | Any,
        *,
        role: str | None = None,
        admin_rights: Iterable[str] | None = None,
        restrictions: Iterable[str] | None = None,
        restricted_until: datetime | int | None = KEEP,
        custom_title: str | None = KEEP,
        anonymous: bool | None = None,
    ) -> Member:
        """Promotes, demotes, restricts or gives a title to a member of a group or channel.

        The app's rules hold to the letter (see `Client.update_member`): nobody hands out
        a right they don't hold, the owner is nobody's to change, and a bot doesn't change its
        own seat.

        Args:
            chat: The group or channel: its ID, or a chat object such as ``message.chat``.
            user: The member: an ID, a `User`, or a `Member`.
            role: ``"admin"`` promotes, ``"member"`` demotes.
            admin_rights: The rights of the badge, by name: ``["delete_messages", "ban_users"]``.
            restrictions: The permissions taken away from this member, by name:
                ``["send_messages"]``; an empty list gives them all back.
            restricted_until: When the restriction ends: an aware `~datetime.datetime` or unix
                milliseconds; ``None`` for good. Only with ``restrictions``.
            custom_title: An admin's title, up to 16 characters; ``None`` removes it.
            anonymous: ``True`` lets an admin post without their name.

        Returns:
            The member's seat now.

        Raises:
            ValidationError: Nothing to change, or a value the server would refuse.
            ForbiddenError: The bot lacks the right, or the member is out of its reach.

        Example:
            Mute somebody for an hour::

                until = datetime.now(UTC) + timedelta(hours=1)
                bot.update_member(chat, user_id, restrictions=["send_messages"], restricted_until=until)
        """
        return self._api.update_member(
            _chat_id(chat),
            _user_id(user),
            role=role,
            admin_rights=admin_rights,
            restrictions=restrictions,
            restricted_until=restricted_until,
            custom_title=custom_title,
            anonymous=anonymous,
        )

    def remove_member(
        self, chat: ChatLike, user: str | int | Any, *, ban: bool = False, banned_until: datetime | int | None = None
    ) -> bool:
        """Throws somebody out of a group or channel, and bans them if asked.

        It needs the *ban users* right; never the owner, and an admin only by the owner.

        Args:
            chat: The group or channel: its ID, or a chat object such as ``message.chat``.
            user: The member: an ID, a `User`, or a `Member`.
            ban: ``True`` bans them as well.
            banned_until: When the ban ends: an aware `~datetime.datetime` or unix milliseconds;
                ``None`` for good. Only with ``ban=True``.

        Returns:
            ``True`` when they were banned as well, ``False`` when only removed.

        Raises:
            ForbiddenError: The bot lacks the right, or the member is the owner or an admin.
            MemberNotFoundError: Not a member, or hidden from the bot.
        """
        return self._api.remove_member(_chat_id(chat), _user_id(user), ban=ban, banned_until=banned_until)

    def purge_member(self, chat: ChatLike, user: str | int | Any, *, up_to: int | MessageBase | None = None) -> bool:
        """Deletes everything one member wrote in a group or channel.

        It needs the *delete messages* right.

        Args:
            chat: The group or channel: its ID, or a chat object such as ``message.chat``.
            user: The member: an ID, a `User`, or a `Member`.
            up_to: Only up to this message: its ID, or the message itself. ``None`` deletes all
                of it.

        Returns:
            ``True`` once it is done.

        Raises:
            ForbiddenError: The bot lacks the right.
        """
        last = _message_id(up_to) if up_to is not None else None
        return self._api.purge_member(_chat_id(chat), _user_id(user), up_to=last)

    def get_bans(self, chat: ChatLike) -> list[Ban]:
        """Lists who is banned from a group or channel. It needs the *ban users* right.

        Args:
            chat: The group or channel: its ID, or a chat object such as ``message.chat``.

        Returns:
            The bans.

        Raises:
            ForbiddenError: The bot lacks the right.
        """
        return self._api.get_bans(_chat_id(chat))

    def unban(self, chat: ChatLike, user: str | int | Any) -> bool:
        """Lifts a ban: the person can come back through a link. It needs the *ban users* right.

        Args:
            chat: The group or channel: its ID, or a chat object such as ``message.chat``.
            user: The person: an ID, a `User`, or a `Ban`.

        Returns:
            ``True`` when there was a ban, ``False`` when there was none.

        Raises:
            ForbiddenError: The bot lacks the right.
        """
        return self._api.unban(_chat_id(chat), _user_id(user))

    def get_invites(self, chat: ChatLike) -> list[Invite]:
        """Lists the invite links of a group or channel.

        Args:
            chat: The group or channel: its ID, or a chat object such as ``message.chat``.

        Returns:
            The links.

        Raises:
            ForbiddenError: The bot may not manage links here.
        """
        return self._api.get_invites(_chat_id(chat))

    def create_invite(
        self,
        chat: ChatLike,
        *,
        label: str | None = None,
        expires_at: datetime | int | None = None,
        max_uses: int | None = None,
        needs_approval: bool | None = None,
    ) -> Invite:
        """Makes an invite link. Not repeated after a network error, which could make two.

        Args:
            chat: The group or channel: its ID, or a chat object such as ``message.chat``.
            label: A name for the link, up to 48 characters.
            expires_at: When it stops working: an aware `~datetime.datetime` or unix
                milliseconds. ``None`` for never.
            max_uses: How many people may use it, 1 to 100000. ``None`` for no limit.
            needs_approval: ``True`` makes people ask first; the bots that may let them in get
                a ``join_request``.

        Returns:
            The new link.

        Raises:
            ForbiddenError: The bot may not make links here.
            ChatIsPublicError: The chat is public: its handle is its link.
        """
        return self._api.create_invite(
            _chat_id(chat), label=label, expires_at=expires_at, max_uses=max_uses, needs_approval=needs_approval
        )

    def revoke_invite(self, chat: ChatLike, code: str | Invite) -> bool:
        """Revokes an invite link. It needs the *invite links* right.

        Args:
            chat: The group or channel: its ID, or a chat object such as ``message.chat``.
            code: The link: its code, or the `Invite`.

        Returns:
            ``True`` once it is revoked.

        Raises:
            InviteNotFoundError: No such link in this chat.
        """
        return self._api.revoke_invite(_chat_id(chat), code.code if isinstance(code, Invite) else code)

    def get_join_requests(self, chat: ChatLike) -> list[JoinRequest]:
        """Lists who asked to come into a group or channel. It needs the *add members* right.

        Args:
            chat: The group or channel: its ID, or a chat object such as ``message.chat``.

        Returns:
            The requests waiting.

        Raises:
            ForbiddenError: The bot lacks the right.
        """
        return self._api.get_join_requests(_chat_id(chat))

    def decide_join_request(self, chat: ChatLike, user: str | int | Any, approve: bool) -> bool:
        """Lets somebody in who asked, or turns them away. It needs the *add members* right.

        Only somebody who asked: it is never a way round a person's "don't add me to groups".

        Args:
            chat: The group or channel: its ID, or a chat object such as ``request.chat``.
            user: Who asked: an ID, a `User`, or the `JoinRequest` itself.
            approve: ``True`` lets them in, ``False`` turns them away.

        Returns:
            ``approve``, as the server confirms it.

        Raises:
            JoinRequestNotFoundError: They have no request waiting.
            ChatFullError: The chat can't take anybody else. The request stays, to
                be decided when there is room.
        """
        return self._api.decide_join_request(_chat_id(chat), _user_id(user), approve)

    def set_comments(
        self, chat: ChatLike, enabled: bool, *, signing: bool | None = None, outside: bool | None = None
    ) -> Chat:
        """Turns a channel's comments on or off. It needs the *manage comments* right.

        Args:
            chat: The channel: its ID, or a chat object such as ``message.chat``.
            enabled: ``True`` turns the comments on.
            signing: ``True`` lets the admins comment under their own name. Left out, the
                server takes it as ``False``.
            outside: ``True`` lets people who aren't subscribed comment on a public readable
                channel. Left out, it stays as it is.

        Returns:
            The channel with the new settings.
        """
        updated = self._api.set_comments(_chat_id(chat), enabled, signing=signing, outside=outside)
        self._note_chat(updated)
        return updated

    def set_post_comments(self, chat: ChatLike, post: int | MessageBase, comments_off: bool) -> bool:
        """Closes or opens again the comments of one post. It needs the *manage comments* right.

        Args:
            chat: The channel: its ID, or a chat object such as ``message.chat``.
            post: The post: its ID, or the message itself.
            comments_off: ``True`` closes them, ``False`` opens them.

        Returns:
            Whether the post's comments are closed now.
        """
        return self._api.set_post_comments(_chat_id(chat), _message_id(post), comments_off)

    def get_channel_stats(self, chat: ChatLike) -> ChannelStats:
        """Reads a channel's numbers: subscribers coming and going, views per post. Admins only.

        Args:
            chat: The channel: its ID, or a chat object such as ``message.chat``.

        Returns:
            The `ChannelStats`.

        Raises:
            ForbiddenError: The bot is not one of the channel's admins.
        """
        return self._api.get_channel_stats(_chat_id(chat))

    def set_chat_photo(self, chat: ChatLike, photo: bytes | str | os.PathLike[str]) -> Picture:
        """Changes the picture of a group or channel: a PNG, JPEG or WebP, or a short clip.

        Whoever may change the chat's info may. The picture it replaces stays in the chat's
        album.

        Args:
            chat: The group or channel: its ID, or a chat object such as ``message.chat``.
            photo: The picture: its bytes, or the path of a file.

        Returns:
            Where the new picture is, and what it turned out to be.

        Raises:
            ValidationError: The file can't be read, or is too large.
            ForbiddenError: The bot may not change the chat's info.
        """
        return self._api.set_chat_photo(_chat_id(chat), _bytes_of(photo, "photo"), limit=self.upload_max)

    def delete_chat_photo(self, chat: ChatLike) -> bool:
        """Removes the picture of a group or channel; the one before it, if any, comes back.

        Args:
            chat: The group or channel: its ID, or a chat object such as ``message.chat``.

        Returns:
            ``True`` when there was a picture to remove.
        """
        return self._api.delete_chat_photo(_chat_id(chat))

    def set_chat_banner(self, chat: ChatLike, banner: bytes | str | os.PathLike[str]) -> Picture:
        """Changes the banner of a group or channel, like `set_chat_photo`.

        Args:
            chat: The group or channel: its ID, or a chat object such as ``message.chat``.
            banner: The banner: its bytes, or the path of a file.

        Returns:
            Where the new banner is, and what it turned out to be.

        Raises:
            ValidationError: The file can't be read, or is too large.
            ForbiddenError: The bot may not change the chat's info.
        """
        return self._api.set_chat_banner(_chat_id(chat), _bytes_of(banner, "banner"), limit=self.upload_max)

    def delete_chat_banner(self, chat: ChatLike) -> bool:
        """Removes the banner of a group or channel, like `delete_chat_photo`.

        Args:
            chat: The group or channel: its ID, or a chat object such as ``message.chat``.

        Returns:
            ``True`` when there was a banner to remove.
        """
        return self._api.delete_chat_banner(_chat_id(chat))


def _sync_step(mw: Any, nxt: Callable[[Any, dict[str, Any]], Any], event: Any, data: dict[str, Any]) -> Any:
    return mw(nxt, event, data)


def _basic_logging() -> None:
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

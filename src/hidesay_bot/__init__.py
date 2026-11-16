"""hidesay-bot: bots for Hidesay, in readable and in end-to-end encrypted chats.

hidesay-bot is a Python library for the Hidesay Bot API. It covers every operation of the API,
async and blocking, and it handles the OpenPGP encryption: when a bot has a published key,
messages reach its handlers already decrypted, and its replies are encrypted and signed for
the people who should read them. The handler code is the same either way.

Install it with ``pip install hidesay-bot`` (Python 3.11 or newer). Two extras are optional:
``hidesay-bot[webhook]`` adds uvicorn to serve a webhook, ``hidesay-bot[http2]`` adds HTTP/2.

A bot that answers every message, with its token in ``HIDESAY_BOT_TOKEN``::

    from hidesay_bot import Bot

    bot = Bot()  # reads the token from HIDESAY_BOT_TOKEN


    @bot.on_message()
    async def echo(message):
        await message.reply(message.text or "...")


    bot.run()  # polls until Ctrl+C

The main names, all importable from ``hidesay_bot``:

Bots

- `~hidesay_bot.Bot`: a bot whose handlers are ``async def`` functions.
- `~hidesay_bot.SyncBot`: the same bot for plain functions, run in a pool of threads.
- `~hidesay_bot.Router`: a group of handlers, to split a bot into modules.
- `~hidesay_bot.filters`: which updates a handler takes (commands, private chats, text...).

The Bot API

- `~hidesay_bot.AsyncClient` and `~hidesay_bot.Client`: one method per operation, async or blocking.
- `~hidesay_bot.RetryPolicy`: how hard a call tries again after a failure.
- `~hidesay_bot.RateLimiter`: paces the calls the way the server counts them.

What handlers get, and what they send

- `~hidesay_bot.Message`: a message, already decrypted; ``message.reply(...)`` answers it.
- `~hidesay_bot.CallbackQuery`: a press on one of the bot's buttons.
- `~hidesay_bot.Update`: anything that happened in a chat, as the server delivered it.
- `~hidesay_bot.SentMessage`: a message the bot sent, to edit or delete later.
- `~hidesay_bot.InlineKeyboard`, `~hidesay_bot.Button`: the buttons under a message.

Keys, storage, webhooks and errors

- `~hidesay_bot.BotKey`: the bot's private key; `~hidesay_bot.PublicKey`: somebody's public key.
- `~hidesay_bot.SQLiteStorage`: a queue of updates on disk that survives restarts.
- `~hidesay_bot.WebhookApp`: the ASGI app the server posts updates to.
- `~hidesay_bot.Secret`: a string that stays out of logs and ``repr``.
- `~hidesay_bot.validate_web_bot_data`: checks who opened the bot's Web-Bot, from the
  ``initData`` its page sends.
- `~hidesay_bot.HidesayBotError`: the base of every error; `~hidesay_bot.APIError` is the server's
  refusal, with one subclass per error code.

The ``hidesay-bot`` command does the chores that need no running bot: ``hidesay-bot keygen``,
``hidesay-bot whoami``, ``hidesay-bot publish-key`` and more (``hidesay-bot --help``).

The full documentation, guides and API reference, is in the ``docs/`` folder of the source,
built with Sphinx: ``pip install -e . --group docs``, then
``sphinx-build -b html docs docs/_build/html``. The API reference is made from these
docstrings, so ``help()`` shows the same text: ``help(hidesay_bot.Bot)``.

Security: the token travels only in the ``Authorization`` header, and never shows in
``repr``, exceptions or logs. Keep it and the bot's private key out of your code: read them
from ``HIDESAY_BOT_TOKEN`` and ``HIDESAY_BOT_KEY``, or their ``_FILE`` forms. In an encrypted
chat, a message that cannot be encrypted is not sent, and never sent in clear instead. Buttons
are never encrypted. If the token leaks, ``await bot.revoke_token()`` burns it at once.
``SECURITY.md`` explains what the library protects and how to report a problem.
"""

import logging as _logging

from . import filters
from ._http import RetryPolicy
from ._secrets import Secret
from ._version import __version__
from .bot import Bot, SyncBot
from .client import AsyncClient, Client
from .crypto import BotKey, Decrypted, PublicKey, format_fingerprint
from .errors import (
    APIError,
    AuthenticationError,
    BadMarkupError,
    BadRequestError,
    BadWebhookError,
    BlockedByUserError,
    BotPausedError,
    CallbackAnsweredError,
    CallbackExpiredError,
    CallbackNotFoundError,
    ChatIsOpenError,
    ChatNotFoundError,
    ConfigurationError,
    ConflictError,
    CryptoError,
    DecryptionError,
    EncryptionError,
    EncryptionRequiredError,
    EndpointNotFoundError,
    FloodWaitError,
    ForbiddenError,
    HidesayBotError,
    InvalidKeyError,
    KeyChangedError,
    MemberNotFoundError,
    MessageNotFoundError,
    NetworkError,
    NotARecipientError,
    NotFoundError,
    NoUserKeyError,
    PassphraseError,
    PayloadError,
    PayloadTooLargeError,
    PollingConflictError,
    PollingError,
    RateLimitError,
    RequestTimeoutError,
    SendIdConflictError,
    ServerError,
    SignatureError,
    SlowModeError,
    StorageError,
    UnexpectedResponseError,
    UserGoneError,
    UsernameNotFoundError,
    UserNotFoundError,
    ValidationError,
    WebBotDataError,
    WebhookActiveError,
    WebhookError,
    WrongBodyKindError,
)
from .filters import ParsedCommand
from .keyboard import Button, InlineKeyboard, KeyboardBuilder
from .models import (
    KEEP,
    AdminRights,
    BotInfo,
    BotStatus,
    CallbackQuery,
    CallbackQueryBase,
    Chat,
    ChatCard,
    ChatKeys,
    ChatKind,
    ChatMembers,
    ChatMemberUpdate,
    ChatMode,
    ChatPeer,
    ChatRef,
    Command,
    CommandScope,
    DeletedMessages,
    HandleKind,
    Limits,
    Media,
    Member,
    Message,
    MessageBase,
    PublishedKey,
    Resolved,
    Seat,
    SeatStatus,
    SentMessage,
    SentMessageBase,
    SignatureStatus,
    SyncCallbackQuery,
    SyncMessage,
    SyncSentMessage,
    Update,
    UpdateKind,
    User,
    UserKey,
    Verification,
    WebhookInfo,
    WebhookSummary,
)
from .ratelimit import RateLimiter
from .router import Router, SkipHandler
from .storage import FailedUpdate, MemoryStorage, SQLiteStorage, Storage
from .webbot import WebBotData, WebBotUser, parse_web_bot_data, validate_web_bot_data
from .webhook import WebhookApp, generate_secret

__all__ = [
    "KEEP",
    "APIError",
    "AdminRights",
    "AsyncClient",
    "AuthenticationError",
    "BadMarkupError",
    "BadRequestError",
    "BadWebhookError",
    "BlockedByUserError",
    "Bot",
    "BotInfo",
    "BotKey",
    "BotPausedError",
    "BotStatus",
    "Button",
    "CallbackAnsweredError",
    "CallbackExpiredError",
    "CallbackNotFoundError",
    "CallbackQuery",
    "CallbackQueryBase",
    "Chat",
    "ChatCard",
    "ChatIsOpenError",
    "ChatKeys",
    "ChatKind",
    "ChatMemberUpdate",
    "ChatMembers",
    "ChatMode",
    "ChatNotFoundError",
    "ChatPeer",
    "ChatRef",
    "Client",
    "Command",
    "CommandScope",
    "ConfigurationError",
    "ConflictError",
    "CryptoError",
    "Decrypted",
    "DecryptionError",
    "DeletedMessages",
    "HandleKind",
    "EncryptionError",
    "EncryptionRequiredError",
    "EndpointNotFoundError",
    "FailedUpdate",
    "FloodWaitError",
    "ForbiddenError",
    "InlineKeyboard",
    "InvalidKeyError",
    "KeyChangedError",
    "KeyboardBuilder",
    "Limits",
    "Media",
    "Member",
    "MemberNotFoundError",
    "MemoryStorage",
    "Message",
    "MessageBase",
    "MessageNotFoundError",
    "NetworkError",
    "NoUserKeyError",
    "NotARecipientError",
    "NotFoundError",
    "HidesayBotError",
    "ParsedCommand",
    "PassphraseError",
    "PayloadError",
    "PayloadTooLargeError",
    "PollingConflictError",
    "PollingError",
    "PublicKey",
    "PublishedKey",
    "Resolved",
    "RateLimitError",
    "RateLimiter",
    "RequestTimeoutError",
    "RetryPolicy",
    "Router",
    "SQLiteStorage",
    "Seat",
    "SeatStatus",
    "Secret",
    "SendIdConflictError",
    "SentMessage",
    "SentMessageBase",
    "ServerError",
    "SignatureError",
    "SignatureStatus",
    "SkipHandler",
    "SlowModeError",
    "Storage",
    "StorageError",
    "SyncBot",
    "SyncCallbackQuery",
    "SyncMessage",
    "SyncSentMessage",
    "UnexpectedResponseError",
    "Update",
    "UpdateKind",
    "User",
    "UserGoneError",
    "UserKey",
    "UserNotFoundError",
    "UsernameNotFoundError",
    "ValidationError",
    "Verification",
    "WebBotData",
    "WebBotDataError",
    "WebBotUser",
    "WebhookActiveError",
    "WebhookApp",
    "WebhookError",
    "WebhookInfo",
    "WebhookSummary",
    "WrongBodyKindError",
    "__version__",
    "filters",
    "format_fingerprint",
    "generate_secret",
    "parse_web_bot_data",
    "validate_web_bot_data",
]

# a library never prints on its own: the application decides where logs go
_logging.getLogger("hidesay_bot").addHandler(_logging.NullHandler())

"""The low-level clients: one method per operation of the Bot API, nothing hidden.

`~hidesay_bot.AsyncClient` and `~hidesay_bot.Client` have the same methods with the same arguments:
one awaits, the other blocks. Each method checks its arguments against the server's rules,
paces the calls, retries when that is safe, and returns the answer as a typed object.

Neither client encrypts anything: they send ``text`` or ``armored`` as you give them.
`~hidesay_bot.Bot` and `~hidesay_bot.SyncBot` build on them and do the OpenPGP part.

Example:
    The token comes from ``HIDESAY_BOT_TOKEN``::

        from hidesay_bot import AsyncClient

        async with AsyncClient() as api:
            me = await api.get_me()
            await api.send_message("48213907", "hello")
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import uuid
from asyncio import get_running_loop
from collections.abc import AsyncIterator, Callable, Iterable, Iterator, Mapping
from datetime import datetime
from types import TracebackType
from typing import Any, Self, TypeVar, overload
from urllib.parse import urlsplit

import httpx2

from . import _spec
from ._http import (
    DEFAULT_API_URL,
    ENV_API_URL,
    ENV_TOKEN,
    MAX_RESPONSE_BYTES,
    AnswerTooLargeError,
    Attempts,
    Call,
    RetryPolicy,
    describe_transport_error,
    mask_token,
    normalize_api_url,
    parse_token,
    read_envelope,
)
from ._http import user_agent as default_user_agent
from ._secrets import Secret, read_env, resolve
from ._spec import MediaLike, StickerLike
from ._validate import UPLOAD_MAX
from .errors import (
    APIError,
    ConfigurationError,
    HidesayBotError,
    NetworkError,
    NotFoundError,
    RateLimitError,
    RequestTimeoutError,
    ServerError,
    ValidationError,
)
from .keyboard import InlineKeyboard
from .models import (
    KEEP,
    Ban,
    BotInfo,
    ChannelStats,
    Chat,
    ChatKeys,
    ChatKind,
    ChatMembers,
    Command,
    CommandScope,
    Comments,
    Invite,
    JoinRequest,
    Media,
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
from .ratelimit import RateLimiter

__all__ = ["AsyncClient", "Client", "RetryPolicy"]

log = logging.getLogger("hidesay_bot.client")

T = TypeVar("T")

DEFAULT_TIMEOUT = httpx2.Timeout(30.0, connect=10.0, pool=10.0)
MEDIA_MAX_BYTES = 64 * 1024 * 1024
# fullmatch it: $ would let a trailing newline through to httpx
_MEDIA_PATH_RE = re.compile(r"/media/([0-9A-Fa-f-]{36})")


class _Base:
    """Configuration both clients share. Not used on its own."""

    def __init__(
        self,
        token: str | Secret | Callable[[], str] | None = None,
        *,
        api_url: str | None = None,
        timeout: float | httpx2.Timeout | None = None,
        retry: RetryPolicy | None = None,
        rate_limiter: RateLimiter | bool = True,
        allow_insecure_http: bool = False,
        user_agent: str | None = None,
    ) -> None:
        token = resolve(token, "token") if token is not None else read_env(ENV_TOKEN)
        if not token:
            raise ConfigurationError(f"no bot token: pass token=... or set {ENV_TOKEN} (or {ENV_TOKEN}_FILE)")
        self.bot_id = parse_token(token)
        self.__token = token.strip()
        self.api_url = normalize_api_url(
            api_url or os.environ.get(ENV_API_URL) or DEFAULT_API_URL,
            allow_insecure_http=allow_insecure_http,
        )
        if timeout is None:
            self.timeout = DEFAULT_TIMEOUT
        elif isinstance(timeout, httpx2.Timeout):
            self.timeout = timeout
        else:
            self.timeout = httpx2.Timeout(float(timeout), connect=min(10.0, float(timeout)))
        self.retry = retry or RetryPolicy()
        if rate_limiter is True:
            self.rate_limiter: RateLimiter | None = RateLimiter()
        elif rate_limiter is False:
            self.rate_limiter = None
        else:
            self.rate_limiter = rate_limiter
        self._user_agent = user_agent or default_user_agent()

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(bot_id={self.bot_id}, api_url={self.api_url!r}, token={mask_token(self.__token)!r})"
        )

    def __reduce__(self) -> Any:
        # a pickle of the client would carry the token wherever the pickle goes
        raise TypeError(f"{type(self).__name__} cannot be pickled: it holds the bot token")

    @property
    def token_hint(self) -> str:
        """The token with everything hidden but the bot ID and the last four characters.

        It looks like ``pgpbot_412.****3f2a``, the way the console shows a token, and is safe
        to print or log. The full token is never in ``repr()``, in error messages or in logs.
        """
        return mask_token(self.__token)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.__token}",
            "User-Agent": self._user_agent,
            "Accept": "application/json",
        }

    def _timeout_for(self, call: Call[Any]) -> httpx2.Timeout:
        if call.read_timeout is None and call.write_timeout is None:
            return self.timeout
        read, write = self.timeout.read, self.timeout.write
        return httpx2.Timeout(
            connect=self.timeout.connect,
            read=max(call.read_timeout, read or 0.0) if call.read_timeout is not None else read,
            write=max(call.write_timeout, write or 0.0) if call.write_timeout is not None else write,
            pool=self.timeout.pool,
        )

    def _request(self, http: httpx2.Client | httpx2.AsyncClient, call: Call[Any]) -> httpx2.Request:
        return http.build_request(
            call.method,
            call.url(self.api_url),
            params=call.params,
            json=call.body if call.has_body else None,
            files=call.files,
            headers=self._headers(),
            timeout=self._timeout_for(call),
        )

    def _media_url(self, media: Media | str) -> str:
        """`/media/<id>` on the API host, and nothing else.

        The path comes from an update; a forged one must not turn the bot into
        something that fetches arbitrary URLs.
        """
        if isinstance(media, Media):
            ref = media.url or f"/media/{media.id}"
        elif isinstance(media, str):
            ref = media
        else:
            raise ValidationError("media must be a Media or a /media/<id> path", field="media")
        if re.fullmatch(r"[0-9A-Fa-f-]{36}", ref):
            ref = f"/media/{ref}"
        if "://" in ref:
            parts = urlsplit(ref)
            base = urlsplit(self.api_url)
            if (parts.scheme, parts.netloc) != (base.scheme, base.netloc):
                raise ValidationError("media URL is not on the API server", field="media")
            ref = parts.path
        if not _MEDIA_PATH_RE.fullmatch(ref):
            raise ValidationError(f"not a media path: {ref!r}", field="media")
        return f"{self.api_url}{ref}"

    def _log_retry(self, call: Call[Any], error: BaseException, wait: float, attempts: Attempts) -> None:
        if isinstance(error, RateLimitError):
            log.info("%s %s: %s, waiting %.1fs", call.method, call.path, error.code, wait)
        else:
            log.warning(
                "%s %s failed (%s), retry %d in %.1fs",
                call.method,
                call.path,
                error if isinstance(error, HidesayBotError) else describe_transport_error(error),
                attempts.tries,
                wait,
            )

    def _penalize(self, call: Call[Any], error: RateLimitError) -> None:
        if self.rate_limiter is None or error.retry_after is None:
            return
        if error.code == "rate_limited":
            self.rate_limiter.penalize(error.retry_after, calls=True)
        elif error.code == "flood_wait":
            self.rate_limiter.penalize(error.retry_after, chat_id=call.chat_id, edit=call.edit)


def _transport_error(call: Call[Any], exc: httpx2.HTTPError) -> NetworkError:
    where = f"{call.method} {call.path}"
    if isinstance(exc, httpx2.TimeoutException):
        return RequestTimeoutError(f"{where}: no answer in time ({describe_transport_error(exc)})")
    return NetworkError(f"{where}: {describe_transport_error(exc)}")


def _too_big(call: Call[Any]) -> NetworkError:
    return AnswerTooLargeError(f"{call.method} {call.path}: answer larger than {MAX_RESPONSE_BYTES} bytes, dropped")


def _media_error(path: str, exc: httpx2.HTTPError) -> NetworkError:
    if isinstance(exc, httpx2.TimeoutException):
        return RequestTimeoutError(f"GET {path}: no answer in time ({describe_transport_error(exc)})")
    return NetworkError(f"GET {path}: {describe_transport_error(exc)}")


def _media_status_error(status: int, path: str) -> APIError:
    if status == 404:
        return NotFoundError("no such media", status=404, code="media_not_found", method="GET", path=path)
    kind = ServerError if status >= 500 else APIError
    return kind(f"media download answered HTTP {status}", status=status, code=f"http_{status}", method="GET", path=path)


# --- async ---


class AsyncClient(_Base):
    """The Bot API, one awaitable method per operation.

    Each method checks its arguments against the server's rules before anything is sent,
    waits when the client-side `RateLimiter` says so, and returns the answer as a typed
    object. A call that is safe to repeat is tried again after a network error or a ``5xx``
    answer, and ``429`` answers are waited out, as the `RetryPolicy` allows. Every API call can
    raise the errors listed in :ref:`common-errors`; each method lists the errors specific to
    it.

    The client never encrypts or decrypts: ``text`` and ``armored`` go out as you give them,
    and the attachments of an encrypted chat come back as OpenPGP ciphertext. `Bot` does the
    OpenPGP part on top of this client, and gives you its own client as ``bot.api``.

    Use it as an async context manager, or call `aclose` when you are done. A client belongs
    to one event loop: used from a new one (two ``asyncio.run()`` calls in one script, say),
    it opens a new connection pool there.

    The token travels only in the ``Authorization`` header. It is never in ``repr()``, in
    error messages or in logs (`token_hint` shows the part that is safe to show), and a client
    cannot be pickled.

    Attributes:
        bot_id (int): The number in the token. It is not the bot's ID, which `get_me` gives
            (``"B48213907"``), and a token made before IDs became strings carries the old number.
        api_url (str): The server's address, without ``/bot/v1`` and the trailing slash.
        timeout (httpx2.Timeout): The timeouts of every request, made from ``timeout``.
        retry (RetryPolicy): The retry rules in use.
        rate_limiter (RateLimiter | None): The limiter that paces the calls, or ``None`` when
            pacing is off.

    Example:
        Who the bot is, then a message into a readable chat::

            from hidesay_bot import AsyncClient

            async with AsyncClient() as api:          # token from HIDESAY_BOT_TOKEN
                me = await api.get_me()
                await api.send_message("48213907", f"Hello from @{me.username}")
    """

    def __init__(
        self,
        token: str | Secret | Callable[[], str] | None = None,
        *,
        api_url: str | None = None,
        timeout: float | httpx2.Timeout | None = None,
        retry: RetryPolicy | None = None,
        rate_limiter: RateLimiter | bool = True,
        allow_insecure_http: bool = False,
        user_agent: str | None = None,
        http_client: httpx2.AsyncClient | None = None,
        transport: httpx2.AsyncBaseTransport | None = None,
        max_connections: int = 20,
    ) -> None:
        """Sets up the client. Nothing is sent to the server until the first call.

        The token and the server address are checked here, so a wrong setup fails at once.

        Args:
            token: The bot's token, ``pgpbot_<id>.<43 characters>``: a string, a `Secret`, or a
                function that returns one (called once, here). ``None`` reads
                ``HIDESAY_BOT_TOKEN``, or the file named by ``HIDESAY_BOT_TOKEN_FILE``; when
                both are set, the file wins.
            api_url: The API server, like ``https://api.hidesay.com``. A trailing ``/bot/v1``
                is removed. ``None`` reads ``HIDESAY_API``, and without it uses
                ``https://api.hidesay.com``.
            timeout: How long one request may take, in seconds, or an ``httpx2.Timeout``.
                ``None`` means 30 seconds, 10 of them at most to connect; a number ``n`` means
                ``n`` seconds, at most 10 of them to connect. A long poll may read for 15
                seconds more than it asks the server to wait, and `download_media` has limits
                of its own.
            retry: When and how often to try again. ``None`` means ``RetryPolicy()``: 4 tries
                in all for a call that is safe to repeat, and up to 5 ``429`` answers waited
                out, each for at most 90 seconds.
            rate_limiter: The client-side pacing. ``True`` makes a new `RateLimiter` with the
                default numbers, ``False`` turns pacing off, and a `RateLimiter` of your own is
                used as it is (one limiter can pace several clients). The client does not read
                the server's own numbers: for a server that uses others, call
                ``api.rate_limiter.update_limits(me.limits)`` with the `BotInfo` of `get_me`.
            allow_insecure_http: Allows an ``http://`` ``api_url`` on a host other than
                ``localhost``, ``127.0.0.1`` or ``::1``. Only for a test server on your own
                network: over plain HTTP the token travels in clear.
            user_agent: The ``User-Agent`` header. ``None`` means
                ``hidesay-bot/<version> (Python <version>)``.
            http_client: An ``httpx2.AsyncClient`` of your own, to share its connection pool.
                The client never closes it, and raises `ConfigurationError` on a call made
                after you closed it. ``transport`` and ``max_connections`` are then ignored.
            transport: An ``httpx2`` transport for the client's own pool: a proxy setup, or a
                mock transport in tests.
            max_connections: How many connections the client's own pool may open.

        Raises:
            ConfigurationError: No token was given or found, the token is not a bot token, the
                token file cannot be read, or ``api_url`` is not acceptable: not ``http`` or
                ``https``, plain ``http`` to another host without ``allow_insecure_http``, or
                with a user name, a password, a query or a fragment. The message never
                contains the token.
        """
        super().__init__(
            token,
            api_url=api_url,
            timeout=timeout,
            retry=retry,
            rate_limiter=rate_limiter,
            allow_insecure_http=allow_insecure_http,
            user_agent=user_agent,
        )
        self._http: httpx2.AsyncClient | None = http_client
        self._owns_http = http_client is None
        self._transport = transport
        self._max_connections = max_connections
        self._loop: asyncio.AbstractEventLoop | None = None

    def _client(self) -> httpx2.AsyncClient:
        # made on first use, inside the loop that will use it
        loop = get_running_loop()
        if self._owns_http and self._http is not None and self._loop is not loop:
            # made under another loop, say an earlier asyncio.run() in the same script:
            # its connections belong to a loop that is gone. Start a new pool
            self._http = None
        if self._http is None or self._http.is_closed:
            if not self._owns_http:
                raise ConfigurationError("the http_client passed to AsyncClient was closed")
            self._http = httpx2.AsyncClient(
                timeout=self.timeout,
                limits=httpx2.Limits(max_connections=self._max_connections, max_keepalive_connections=10),
                follow_redirects=False,
                transport=self._transport,
            )
            self._loop = loop
        return self._http

    async def aclose(self) -> None:
        """Closes the client's connection pool.

        An ``http_client`` you passed in is left open: closing it is up to you. The client can
        still be used afterwards, since the next call opens a new pool, and calling this twice
        does no harm.
        """
        if self._http is not None and self._owns_http:
            await self._http.aclose()
            self._http = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.aclose()

    # --- the engine room ---

    async def _once(self, call: Call[Any]) -> Any:
        http = self._client()
        try:
            response = await http.send(self._request(http, call), stream=True)
        except httpx2.HTTPError as exc:
            raise _transport_error(call, exc) from exc
        try:
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise _too_big(call)
                chunks.append(chunk)
        except httpx2.HTTPError as exc:
            raise _transport_error(call, exc) from exc
        finally:
            await response.aclose()
        return read_envelope(call, response.status_code, response.headers, b"".join(chunks))

    async def execute(self, call: Call[T]) -> T:
        """Sends a prepared call with the rate limit and retry rules. Mostly for internal use.

        The public methods check their arguments, describe the operation as a ``Call`` (made by
        the internal ``hidesay_bot._spec`` module) and hand it to this method. Before each try it
        waits for the rate limiter. A ``429`` answer is waited out as long as the server asks,
        up to ``retry.rate_limit_retries`` times; one that asks for more than
        ``retry.max_rate_limit_wait`` seconds is raised at once. A network error or a ``5xx``
        answer is tried again after a growing random wait, up to ``retry.attempts`` tries in
        all, but only for a call that is safe to repeat. When an earlier answer was lost, an
        error that means the first try went through counts as success, ``message_not_found``
        on a delete for example.

        Args:
            call: The prepared call.

        Returns:
            The call's result, read from the server's answer.

        Raises:
            APIError: The server refused the call, or still failed when the retries ran out.
            NetworkError: No answer came, after the retries.
            PayloadError: The answer lacks something this version needs.
            ConfigurationError: The ``http_client`` you passed in was closed.
        """
        attempts = Attempts(self.retry)
        while True:
            attempts.tries += 1
            if self.rate_limiter is not None:
                await self.rate_limiter.acquire_async(
                    send=call.send, edit=call.edit, chat_id=call.chat_id, chat_kind=call.chat_kind
                )
            try:
                return call.parse(await self._once(call))
            except APIError as exc:
                if attempts.settles(call, exc):
                    return call.done_value  # type: ignore[no-any-return]
                if isinstance(exc, RateLimitError):
                    self._penalize(call, exc)
                wait = attempts.wait_for(call, exc)
                if wait is None:
                    raise
                self._log_retry(call, exc, wait, attempts)
            except NetworkError as exc:
                wait = attempts.wait_for(call, exc)
                if wait is None:
                    raise
                self._log_retry(call, exc, wait, attempts)
            await asyncio.sleep(wait)

    # --- Me ---

    async def get_me(self) -> BotInfo:
        """Fetches who the bot is, its mode, its status and the limits this server enforces.

        Calls ``GET /me``. It works while the bot is paused, so a program can find out why
        nothing else does. Retried after a network error or a ``5xx`` answer.

        Returns:
            The bot as the server holds it. ``limits`` types the limits this version knows, and
            `Limits.get` reads any other by its JSON name; ``queued`` counts the updates
            waiting.

        Example:
            Read the server's limits instead of hard-coding them::

                me = await api.get_me()
                print(f"@{me.username}", me.mode, me.status, me.queued)
                print(me.limits.sends_per_group_per_minute)
                print(me.limits.get("botsPerChat"))     # a limit this version does not name
        """
        return await self.execute(_spec.get_me())

    # --- Updates ---

    async def get_updates(
        self,
        offset: int | None = None,
        limit: int | None = None,
        timeout: int | None = None,  # noqa: ASYNC109 - the API's own parameter: how long the server waits
    ) -> list[Update]:
        """Fetches the updates waiting for the bot, and confirms the ones already handled.

        Calls ``GET /updates`` (long polling). Before sending, it checks that ``offset`` is an
        integer of at least 0, ``limit`` is between 1 and 100 and ``timeout`` between 0 and
        50. With a ``timeout``, the request may read for ``timeout + 15`` seconds, so the client
        does not hang up before the server answers. Not retried after a network error or a
        ``5xx`` answer: the polling loop decides what to do.

        Only one poll per bot runs at a time: a second call ends the first one with an empty
        list. Polling is refused while a webhook is set. `Bot.run` does all of this for you.

        Args:
            offset: Confirms every update with a lower ``update_id``: the server deletes them
                for good. Pass the highest ``update_id`` you handled, plus one. ``None``
                confirms nothing and gets everything still in the queue.
            limit: At most this many updates, 1 to 100. ``None`` leaves it to the server,
                which sends up to 100.
            timeout: Seconds the server may wait for an update to arrive, 0 to 50. ``None``
                or ``0`` answers at once.

        Returns:
            The updates, oldest first. Empty when nothing arrived before the timeout.

        Raises:
            ValidationError: ``offset``, ``limit`` or ``timeout`` is out of range.
            WebhookActiveError: A webhook is set. Delete it before polling.

        Example:
            Handle each update first, then confirm it with the next call::

                offset = None
                while True:
                    for update in await api.get_updates(offset, timeout=50):
                        await handle(update)
                        offset = update.update_id + 1

        Warning:
            Confirming is final: the server deletes the confirmed updates at the start of the
            request. Never pass an ``offset`` beyond what you have handled, or saved somewhere
            safe.
        """
        updates: list[Update] = await self.execute(_spec.get_updates(offset, limit, timeout))
        return updates

    async def iter_updates(
        self,
        *,
        offset: int | None = None,
        limit: int = 100,
        timeout: int = 50,  # noqa: ASYNC109 - see get_updates
    ) -> AsyncIterator[Update]:
        """Polls forever, yielding the updates one by one.

        Calls `get_updates` in a loop, with the same checks. A batch is confirmed only when the
        next one is asked for, that is after your loop body ran for every update in it. If you
        break out of the loop, or an exception leaves it, nothing of the current batch is
        confirmed: the next poll gets the whole batch again, the updates you already handled
        included. Updates come at least once, and none is lost.

        An error of `get_updates` ends the loop, a network error included, since polls are not
        retried. Catch it and start a new loop to go on.

        Args:
            offset: The ``offset`` of the first poll. ``None`` starts with everything still in
                the queue.
            limit: At most this many updates per poll, 1 to 100.
            timeout: Seconds each poll may wait for an update to arrive, 0 to 50.

        Yields:
            Each update, oldest first.

        Raises:
            ValidationError: ``offset``, ``limit`` or ``timeout`` is out of range. It is raised
                when the loop starts, not when the method is called.
            WebhookActiveError: A webhook is set.

        Example:
            Answer every message of a readable chat::

                async for update in api.iter_updates():
                    if update.message is not None:
                        await api.send_message(update.message.chat.id, "pong")
        """
        while True:
            batch = await self.get_updates(offset, limit, timeout)
            for update in batch:
                yield update
                offset = update.update_id + 1

    async def get_webhook(self) -> WebhookInfo:
        """Fetches the webhook, if one is set, and how its deliveries are going.

        Calls ``GET /webhook``. It works while the bot is paused. Retried after a network error
        or a ``5xx`` answer.

        Returns:
            The webhook state. ``url`` is ``None`` when no webhook is set, ``pending`` counts
            the updates waiting, and ``failures``, ``last_error`` and ``next_attempt_at`` tell
            how the deliveries fail, when they do.

        Example:
            See whether the deliveries are failing::

                info = await api.get_webhook()
                if info.failures:
                    print(f"{info.failures} failures in a row, last: {info.last_error}")
        """
        return await self.execute(_spec.get_webhook())

    async def set_webhook(self, url: str, secret: str | None = None) -> WebhookInfo:
        """Sets a webhook: from now on, every update is POSTed to ``url``.

        Calls ``POST /webhook``. Before sending, it checks that ``url`` is an absolute ``http``
        or ``https`` URL of at most 1024 characters, with a host and without a user name, a
        password or a fragment, and that ``secret`` is 1 to 256 characters of ``A-Z``,
        ``a-z``, ``0-9``, ``_`` and ``-``. The server itself refuses plain ``http`` and
        addresses that are not public. Retried after a network error or a ``5xx`` answer.

        Setting a webhook ends any poll in progress, and `get_updates` is refused while it is
        set. `Bot.create_webhook_app` can register the webhook for you.

        Args:
            url: The address the server delivers the updates to.
            secret: Sent back on every delivery in the ``X-Hidesay-Bot-Api-Secret-Token``
                header, so your endpoint can refuse anything without it. ``None`` sets no
                secret.

        Returns:
            The webhook as the server saved it.

        Raises:
            ValidationError: ``url`` or ``secret`` breaks one of the rules above.
            BadWebhookError: The server refused the URL, for example because it resolves to
                a private address.

        Example:
            A webhook with a secret::

                from hidesay_bot import generate_secret

                secret = generate_secret()
                await api.set_webhook("https://bot.example.org/hidesay", secret)

        Warning:
            Always set a secret: without one, anyone who finds the URL can send fake updates.
        """
        return await self.execute(_spec.set_webhook(url, secret))

    async def delete_webhook(self) -> WebhookInfo:
        """Removes the webhook: the bot goes back to polling.

        Calls ``DELETE /webhook``. The updates that queued while the webhook was set stay, for
        the next poll. It works while the bot is paused. Retried after a network error or a
        ``5xx`` answer.

        Returns:
            The webhook state after the change. ``url`` is ``None``, and ``pending`` counts the
            updates waiting.

        Example:
            Switch from a webhook to polling::

                info = await api.delete_webhook()
                print(info.pending, "updates waiting for the next poll")
        """
        return await self.execute(_spec.delete_webhook())

    # --- Key ---

    async def set_key(self, public_key: str, fingerprint: str) -> PublishedKey:
        """Publishes the bot's OpenPGP public key: the bot becomes protected.

        Calls ``PUT /key``. From then on the bot's private chats are end-to-end encrypted:
        people's phones seal every message to this key, and the bot must send ``armored`` into
        those chats. Calling it again with another key is a rotation; every phone learns the
        new fingerprint at once. Retried after a network error or a ``5xx`` answer.

        Before sending, it checks that ``public_key`` is an armored public key block of at most
        65536 characters, and refuses anything that holds a private key, so a private key never
        leaves the process. The fingerprint travels as its own field, so it has to match the key:
        `Bot.publish_key` computes it from the key itself, and refuses v6 keys, which the
        phones cannot use.

        Args:
            public_key: The armored public key block, as `BotKey.export_public` writes it.
            fingerprint: The key's fingerprint, 40 (v4) or 64 (v6) hex digits with no spaces
                in between. It is sent in upper case.

        Returns:
            The fingerprint the server saved, and the bot's mode, now ``protected``.

        Raises:
            ValidationError: ``public_key`` is not an armored public key block, is too long or
                holds a private key, or ``fingerprint`` is not 40 or 64 hex digits.

        Example:
            Publish the public half of the key the bot decrypts with::

                key = BotKey.from_file("bot-key.asc", passphrase)
                await api.set_key(key.export_public(), key.fingerprint)

        Warning:
            There is no API to remove the key: only the bot's owner can make the bot open
            again, from the console. A wrong fingerprint makes the phones seal messages to a key
            the bot does not have.
        """
        return await self.execute(_spec.set_key(public_key, fingerprint))

    # --- Commands ---

    @overload
    async def get_commands(self, scope: None = None) -> list[CommandScope]: ...
    @overload
    async def get_commands(self, scope: str) -> list[Command]: ...
    async def get_commands(self, scope: str | None = None) -> list[CommandScope] | list[Command]:
        """Fetches the command menus: every scope, or one.

        Calls ``GET /commands``. Before sending, it checks that ``scope`` is one the server
        knows. Retried after a network error or a ``5xx`` answer.

        Args:
            scope: ``None`` for every scope with its list. Or one scope: ``"default"``,
                ``"private"``, ``"groups"``, ``"groupAdmins"``, or ``"chat:<id>"`` (``"chat:G48213907"``) for one chat
                the bot is in.

        Returns:
            Every scope with its commands, as a list of `CommandScope`, when ``scope`` is
            ``None``. Otherwise that scope's commands, as a list of `Command`.

        Raises:
            ValidationError: ``scope`` is not one of the above.

        Example:
            All the menus, then one::

                for scope in await api.get_commands():
                    print(scope.scope, [c.command for c in scope.commands])

                private = await api.get_commands("private")
        """
        result: list[CommandScope] | list[Command] = await self.execute(_spec.get_commands(scope))
        return result

    async def set_commands(
        self, commands: Iterable[Command | Mapping[str, Any] | tuple[str, str]], scope: str | None = None
    ) -> CommandScope:
        """Replaces the whole command list of one scope.

        Calls ``PUT /commands``. Before sending, each command is cleaned up the way the server
        does it: the name is trimmed, loses a leading ``/`` and is put in lower case, and must
        then be 1 to 32 letters, digits and underscores; in the description every run of white
        space becomes one space, and it must then be 1 to 256 characters, with no control
        characters. A name given twice keeps its first description, as on the server. Retried
        after a network error or a ``5xx`` answer.

        Scopes go from narrow to wide: in a private chat ``private``, then ``default``; in a
        group ``chat:<id>``, then ``groupAdmins`` for admins, then ``groups``, then
        ``default``. An empty list does not hide a wider scope. The owner can edit the same
        lists: whoever writes last wins.

        Args:
            commands: Up to 100 commands. Each is a `Command`, a dict with ``"command"`` and
                ``"description"``, or a ``(name, description)`` pair.
            scope: Which list: ``"default"``, ``"private"``, ``"groups"``, ``"groupAdmins"``
                or ``"chat:<id>"`` (``"chat:G48213907"``). ``None`` means ``"default"``.

        Returns:
            The scope and its commands, as the server saved them.

        Raises:
            ValidationError: More than 100 commands, a command that breaks the rules above, or
                an unknown ``scope``.
            ChatNotFoundError: A ``"chat:<id>"`` (``"chat:G48213907"``) scope names a chat the bot is not in.

        Example:
            A menu for everyone, and one for the admins of groups::

                await api.set_commands([("start", "Say hello"), ("help", "What I can do")])
                await api.set_commands([("ban", "Ban the replied user")], scope="groupAdmins")
        """
        return await self.execute(_spec.set_commands(commands, scope))

    async def delete_commands(self, scope: str | None = None) -> CommandScope:
        """Empties the command list of one scope.

        Calls ``DELETE /commands``. Before sending, it checks that ``scope`` is one the server
        knows. An empty list does not hide a wider scope. Retried after a network error or a
        ``5xx`` answer.

        Args:
            scope: Which list: ``"default"``, ``"private"``, ``"groups"``, ``"groupAdmins"``
                or ``"chat:<id>"`` (``"chat:G48213907"``). ``None`` means ``"default"``.

        Returns:
            The scope, now with no commands.

        Raises:
            ValidationError: ``scope`` is not one of the above.
        """
        return await self.execute(_spec.delete_commands(scope))

    # --- Messages ---

    async def send_message(
        self,
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
    ) -> SentMessageBase:
        """Sends a message: ``text`` into a readable chat, ``armored`` into an encrypted one.

        Calls ``POST /messages``. Give exactly one of ``text`` and ``armored``; `get_chat` tells
        which one a chat wants (its ``mode``). The client does not encrypt: in an encrypted
        chat, seal the message to every key of `get_chat_keys` and to the bot's own key, and
        sign it. `BotKey.seal` adds the bot's key and the signature for you.

        Into a readable chat it also sends files (``media``, uploaded first with `upload_file`,
        ``text`` being the caption) or a ``sticker``; the server writes the envelope the phones
        read. In an encrypted chat files and stickers travel inside the armor: `Bot.send_media`
        and `Bot.send_sticker` do it.

        Before sending, it checks what the server would refuse:

        - the chat is an ID like ``"G48213907"``, ``"C48213907"`` or a person's
          ``"48213907"``, and ``reply_to`` a positive integer;
        - ``text`` is not blank, has at most 4096 UTF-16 code units, and no control characters
          but newline and tab (carriage returns become newlines first); as a caption it may be
          blank, and is then left out;
        - ``armored`` holds ``-----BEGIN PGP MESSAGE-----`` and has at most 131072 characters;
        - ``media`` holds 1 to 10 files described as the server's envelope wants them, and an
          album of more than one only pictures and videos;
        - a ``sticker`` comes alone, without text;
        - ``markup`` follows the rules of `InlineKeyboard`;
        - ``mentions`` are at most 20 user IDs;
        - ``send_id``, when given, is a UUID.

        The client paces the sends: by default one a second into a private chat, 20 a minute
        into a group or channel, 30 a second in all.

        Retried after a network error or a ``5xx`` answer. The ``sendId`` makes this safe: a
        repeat hands back the message already written instead of writing it twice.

        Args:
            chat_id: A chat the bot is in.
            text: The message, for a readable chat.
            armored: An ASCII-armored OpenPGP message, for an encrypted chat.
            reply_to: The ID of a message of the same chat to quote. The server drops a wrong
                ID without an error.
            markup: Buttons under the message: an `InlineKeyboard`, or its JSON form
                ``{"inlineKeyboard": [...]}``. Buttons are never encrypted.
            silent: Channels only: ``True`` posts without a notification. ``None`` leaves it
                to the server, which notifies.
            send_id: A UUID that names this send, as a string or a `uuid.UUID`. ``None`` makes
                a new one. Pass your own to repeat the send safely later, after a crash for
                example.
            media: Readable chats: the files to send, one or an album of 2 to 10 pictures and
                clips, each a `MediaIn` or a dict in the API's form, with the ID `upload_file`
                gave. ``text`` is the caption.
            sticker: Readable chats: a sticker, as its ID, a `Sticker` or a dict with ``id``
                and ``mediaId``. The file ID proves the bot was shown a sticker of a pack that
                is not public.
            mentions: Groups: the IDs of the people the words tag (``@[Anna](u48213907)``), up
                to 20, so they are told even with the group muted, as for a person's tag.
                Somebody not in the group is dropped.
            chat_kind: The `ChatKind` of the chat, for the rate limiter only: groups and
                channels are paced per minute. ``None`` paces it like a private chat.

        Returns:
            The message as the server stored it, with its ``id`` and ``date``.

        Raises:
            ValidationError: An argument breaks one of the rules above, or both or neither of
                ``text`` and ``armored`` were given.
            WrongBodyKindError: ``text`` into an encrypted chat, or ``armored`` into a readable
                one, or files or a sticker into an encrypted one. ``expected`` says which one
                the chat wants.
            BadMediaError: A file is not one this bot uploaded, or the server refused how it is
                described.
            StickerNotFoundError: No such sticker, or one the bot may not send.
            BadMarkupError: The server refused the keyboard.
            ForbiddenError: The bot may not write in this chat, or send files or voice notes in
                this group.
            BlockedByUserError: The person blocked the bot.
            UserGoneError: The person closed their account.
            ChatNotFoundError: No such chat, or the bot is not in it.
            SendIdConflictError: This ``send_id`` was already used in another chat.
            FloodWaitError: Too many messages into this chat or in all, and the server asked
                for a longer wait, or more waits, than the `RetryPolicy` allows.
            SlowModeError: The group's slow mode, with the same limits on waiting.

        Examples:
            Into a readable chat::

                sent = await api.send_message("48213907", "Backup finished.")
                print(sent.id, sent.date)

            A picture, uploaded first::

                up = await api.upload_file(jpeg)
                media = MediaIn(id=up.id, kind="image", mime="image/jpeg", width=1280, height=720)
                await api.send_message(chat_id, "The view from here", media=[media])

            Into an encrypted group, sealed by hand::

                keys = await api.get_chat_keys(chat_id)
                recipients = [PublicKey.from_user_key(k) for k in keys.keys]
                armored = bot_key.seal("Backup finished.", recipients)
                await api.send_message(chat_id, armored=armored, chat_kind=ChatKind.GROUP)
        """
        return await self.execute(
            _spec.send_message(
                chat_id,
                text,
                armored=armored,
                reply_to=reply_to,
                markup=markup,
                silent=silent,
                send_id=send_id,
                media=media,
                sticker=sticker,
                mentions=mentions,
                chat_kind=chat_kind,
            )
        )

    async def edit_message(
        self,
        chat_id: str | int,
        message_id: int,
        text: str | None = None,
        *,
        armored: str | None = None,
        markup: InlineKeyboard | Mapping[str, Any] | None = KEEP,
        chat_kind: ChatKind | None = None,
    ) -> SentMessageBase:
        """Changes the text, the buttons, or both, of one of the bot's own messages.

        Calls ``PATCH /messages/{messageId}``. The bot edits its own ordinary messages, at any
        age, and people see the edit live. In a channel where it may edit posts it edits
        anybody's post too, the words only: buttons are only ever the sender's. On a file or an
        album of a readable chat, ``text`` is the new caption and the files stay. The new body
        follows the rules of `send_message`: ``text`` in a readable chat, ``armored`` in an
        encrypted one. Before
        sending, it checks the IDs, the body and the keyboard, and that there is something to
        change. Edits are paced with the same numbers as sends, counted on their own; the 30 a
        second in all is shared with sends. Retried after a network error or a ``5xx`` answer:
        the same edit twice gives the same result.

        Args:
            chat_id: The chat the message is in.
            message_id: The message to change.
            text: The new text, for a readable chat.
            armored: The new armored OpenPGP message, for an encrypted chat.
            markup: `KEEP` (the default) keeps the buttons, ``None`` or an empty keyboard
                removes them, and a keyboard replaces them.
            chat_kind: The `ChatKind` of the chat, for the rate limiter only, as in
                `send_message`.

        Returns:
            The message after the edit.

        Raises:
            ValidationError: An argument breaks one of the rules, both ``text`` and
                ``armored`` were given, or there is nothing to change: no ``text``, no
                ``armored``, and ``markup`` left at `KEEP`.
            WrongBodyKindError: ``text`` into an encrypted chat, or ``armored`` into a readable
                one. ``expected`` says which one the chat wants.
            BadMarkupError: The server refused the keyboard.
            ForbiddenError: Not the bot's message (nor a channel post it may edit), not an
                ordinary one (a service notice), or new buttons on somebody else's post.
            BlockedByUserError: In a private chat, the person blocked the bot.
            UserGoneError: In a private chat, the person closed their account.
            ChatNotFoundError: No such chat, or the bot is not in it.
            MessageNotFoundError: No such message.

        Example:
            New text, then the buttons removed and the text kept::

                await api.edit_message(chat_id, sent.id, "Backup finished: 14 GB.")
                await api.edit_message(chat_id, sent.id, markup=None)
        """
        return await self.execute(
            _spec.edit_message(chat_id, message_id, text, armored=armored, markup=markup, chat_kind=chat_kind)
        )

    async def delete_message(self, chat_id: str | int, message_id: int) -> bool:
        """Deletes a message for everyone, for good.

        Calls ``DELETE /messages/{messageId}``, with ``chatId`` in the query. The bot deletes
        any message of a private chat, as either person can there; in a group or channel its
        own, anybody's where it has the *delete messages* right, and anybody's comment with the
        *manage comments* right. A post takes its comments with it. Only messages the bot was
        handed; secret messages and service notices cannot be deleted. Before sending, it
        checks the chat and the message ID. `delete_messages` deletes up to 100 at once.

        Retried after a network error or a ``5xx`` answer. If an earlier answer was lost and
        the retry gets ``message_not_found``, the first try did delete the message, and the
        method returns ``True``. When no answer was lost, a missing message raises
        `MessageNotFoundError`.

        Args:
            chat_id: The chat the message is in.
            message_id: The message to delete.

        Returns:
            ``True`` once the message is deleted. ``False`` in a rare race on the server, when
            the message disappeared between its lookup and the delete.

        Raises:
            ForbiddenError: The bot may not delete this message.
            ChatNotFoundError: No such chat, or the bot is not in it.
            MessageNotFoundError: No such message, or it is already gone.
        """
        return await self.execute(_spec.delete_message(chat_id, message_id))

    # --- Buttons ---

    async def answer_callback(
        self, callback_id: str, text: str | None = None, *, alert: bool = False, url: str | None = None
    ) -> bool:
        """Answers a button press: stops the spinner on the person's phone.

        Calls ``POST /callbacks/{callbackId}/answer``. Answer within 60 seconds of the press;
        each press can be answered once. The message itself changes only if you edit it
        (`edit_message`). Before sending, it checks ``callback_id``, ``text`` and ``url``.

        Retried after a network error or a ``5xx`` answer. If an earlier answer was lost and
        the retry gets ``callback_answered``, the first try worked, and the method returns
        ``True``.

        Args:
            callback_id: The ID of the press, the ``id`` of a `CallbackQuery`.
            text: A short line shown over the chat, or in a box with ``alert``. Up to 200
                characters on one line, with no control characters. Spaces at the ends are
                trimmed, and an empty text is left out.
            alert: Shows ``text`` in a box the person has to close, instead of a short line.
            url: An address the phone shows and offers to open: an absolute ``http`` or
                ``https`` URL of up to 1024 characters, without a user name or password. An
                empty string is left out.

        Returns:
            ``True`` once the answer reached the phone that pressed.

        Raises:
            ValidationError: ``callback_id`` is not the ID of a press, or ``text`` or ``url``
                breaks one of the rules above.
            CallbackNotFoundError: No such press for this bot.
            CallbackAnsweredError: The press was answered already.
            CallbackExpiredError: More than 60 seconds went by since the press.

        Example:
            Just stop the spinner, say a word, or show a box::

                await api.answer_callback(query.id)
                await api.answer_callback(query.id, "Saved")
                await api.answer_callback(query.id, "Sold out", alert=True)

        Warning:
            Answers are not encrypted, even in an encrypted chat.
        """
        return await self.execute(_spec.answer_callback(callback_id, text, alert=alert, url=url))

    # --- Chats ---

    async def get_chat(self, chat_id: str | int) -> Chat:
        """Fetches a chat the bot is in: its kind, its mode and, in a group, the bot's seat.

        Calls ``GET /chats/{chatId}``. ``mode`` tells which body to send: ``text`` in an
        ``open`` chat, ``armored`` in a ``protected`` one. A private chat also carries the
        other person and whether they blocked the bot. Before sending, it checks the shape of
        ``chat_id``. Retried after a network error or a ``5xx`` answer.

        Args:
            chat_id: The chat.

        Returns:
            The chat as the server describes it.

        Raises:
            ChatNotFoundError: No such chat, or the bot is not in it.

        Example:
            Find out which body a chat wants::

                chat = await api.get_chat(chat_id)
                body = "armored" if chat.protected else "text"
                print(chat.kind, chat.title, body)
        """
        return await self.execute(_spec.get_chat(chat_id))

    async def get_chat_keys(self, chat_id: str | int) -> ChatKeys:
        """Fetches the public keys a message into an encrypted chat must be sealed to.

        Calls ``GET /chats/{chatId}/keys``. The answer holds the keys of the people in the
        chat, IDs and keys only, no names. The bot's own key is not in it: add it yourself
        (`BotKey.seal` does). Other bots that only receive what is addressed to them are left
        out, as they are for the app. Before sending, it checks the shape of ``chat_id``.
        Retried after a network error or a ``5xx`` answer.

        Args:
            chat_id: An encrypted chat the bot is in.

        Returns:
            The keys, and in a group ``member_count``, which is larger than the number of keys
            when some members have none.

        Raises:
            ChatIsOpenError: The chat is readable: send ``text``, no keys are needed.
            ChatNotFoundError: No such chat, or the bot is not in it.
            NoUserKeyError: A private chat whose person has no key.

        Example:
            Read the keys, checking each one against the fingerprint the server lists::

                keys = await api.get_chat_keys(chat_id)
                recipients = [PublicKey.from_user_key(k) for k in keys.keys]

        Note:
            The keys come from the server, and a server that lies could hand out a key of its
            own. Key pinning (``pin_keys=True`` on `Bot`) notices a key that changes later.
        """
        return await self.execute(_spec.get_chat_keys(chat_id))

    async def get_chat_members(self, chat_id: str | int) -> ChatMembers:
        """Fetches the members of a group or channel, as a member in the bot's seat sees them.

        Calls ``GET /chats/{chatId}/members``. Where the member list is hidden, a bot that is
        not an admin sees the admins only, and a channel that keeps its admins to itself
        leaves them out. Names and handles only, never phone numbers. Before sending, it checks
        the shape of ``chat_id``. Retried after a network error or a ``5xx`` answer.

        Args:
            chat_id: A group or channel the bot is in.

        Returns:
            The members with their seats, and ``member_count``, which counts the list as
            returned.

        Raises:
            BadRequestError: The chat is private, so it has no member list.
            ChatNotFoundError: No such chat, or the bot is not in it.

        Example:
            The handles of the admins::

                roster = await api.get_chat_members(group_id)
                admins = [m.user.username for m in roster.members if m.is_admin]
        """
        return await self.execute(_spec.get_chat_members(chat_id))

    async def get_chat_member(self, chat_id: str | int, user_id: str | int) -> Member:
        """Fetches one member's seat in a group or channel.

        Calls ``GET /chats/{chatId}/members/{userId}``. Before sending, it checks the shape of
        both IDs. Retried after a network error or a ``5xx`` answer.

        Args:
            chat_id: A group or channel the bot is in.
            user_id: The person.

        Returns:
            The person and their seat (status, admin rights, title).

        Raises:
            BadRequestError: The chat is private, so it has no members.
            ChatNotFoundError: No such chat, or the bot is not in it.
            MemberNotFoundError: The person is not a member, or a hidden member list keeps them
                from the bot.

        Example:
            Check a right before relying on it::

                member = await api.get_chat_member(group_id, user_id)
                if member.can is not None and member.can.ban_users:
                    ...
        """
        return await self.execute(_spec.get_chat_member(chat_id, user_id))

    async def leave_chat(self, chat_id: str | int) -> bool:
        """Leaves a group or channel.

        Calls ``POST /chats/{chatId}/leave``. The chat is told, the way it would be about a
        person. A bot cannot leave a private chat; the person can block it. Before sending, it
        checks the shape of ``chat_id``.

        Retried after a network error or a ``5xx`` answer. If an earlier answer was lost and
        the retry finds no chat, the first try worked, and the method returns ``True``.

        Args:
            chat_id: The group or channel.

        Returns:
            ``True`` once the bot has left.

        Raises:
            BadRequestError: The chat is private and cannot be left.
            ChatNotFoundError: No such chat, or the bot is not in it.
        """
        return await self.execute(_spec.leave_chat(chat_id))

    async def send_typing(self, chat_id: str | int, typing: bool = True) -> bool:
        """Shows "typing..." in a chat for a few seconds, or clears it.

        Calls ``POST /chats/{chatId}/typing``. The indicator clears on its own after a few
        seconds, or when a message lands. One goes out per chat every 3 seconds at most; calls
        in between show nothing and return ``False``. Before sending, it checks the shape of
        ``chat_id``. Not retried after a network error or a ``5xx`` answer: a late indicator is
        useless.

        Args:
            chat_id: A chat the bot may write in.
            typing: ``False`` clears the indicator early.

        Returns:
            ``True`` if the indicator went out, ``False`` if one went out to this chat less
            than 3 seconds ago.

        Raises:
            ForbiddenError: The bot may not write in this chat: it is restricted, or only a
                subscriber of a channel.
            ChatNotFoundError: No such chat, or the bot is not in it.

        Example:
            Show it while the bot works::

                await api.send_typing(chat_id)
                report = await build_report()
                await api.send_message(chat_id, report)
        """
        return await self.execute(_spec.send_typing(chat_id, typing))

    async def resolve(self, username: str) -> Resolved:
        """Turns a public handle into an ID.

        Calls ``GET /resolve/{username}``. For the handle somebody types at the bot - "paga
        @anna", "segui @notizie" - when no message has handed that ID over. People, groups and
        channels share one pool of names, so a handle belongs to exactly one of the three and
        `Resolved.kind` says which; `Resolved.id` is filled in either way. A leading ``@`` is
        stripped and the case does not matter. Before sending, it checks the handle against the
        server's own rule. Retried after a network error or a ``5xx`` answer.

        It finds anybody with a public handle, which is no more than the short link already
        shows a stranger with a browser. The ID on its own unlocks nothing: sending still needs
        the bot to be in the chat, and `AsyncClient.get_user_key` still needs somebody the bot may look up.

        Args:
            username: The handle, with or without the ``@``.

        Returns:
            What the handle turned out to be.

        Raises:
            ValidationError: Not a handle: 3-32 characters, letters, digits and underscore.
            UsernameNotFoundError: Nobody and no chat answers to it. Also the answer for a
                group with no public name and for a closed account.
            FloodWaitError: More than 60 lookups this minute, where a handle nobody holds
                counts as five. ``retry_after`` says how long to wait.

        Example:
            Pay whoever was named::

                found = await api.resolve(words.removeprefix("paga @"))
                if found.is_user:
                    await api.send_message(chat_id, f"{found.mention} ha id {found.id}")
        """
        return await self.execute(_spec.resolve(username))

    async def get_user_key(self, user_id: str | int) -> UserKey:
        """Fetches one person's public key.

        Calls ``GET /users/{userId}/key``. It works only for someone the bot may look up,
        as for ``get_user``: anyone else is `UserNotFoundError`, whether or not they exist. Before sending, it
        checks the shape of ``user_id``. Retried after a network error or a ``5xx`` answer.

        Args:
            user_id: The person.

        Returns:
            The person's ID, the key's fingerprint as the server lists it, and the armored
            public key.

        Raises:
            UserNotFoundError: No such person, or nobody the bot may look up.
            NoUserKeyError: The person never published a key.

        Example:
            Read the key, and show its fingerprint the way people compare it::

                user_key = await api.get_user_key("48213907")
                anna = PublicKey.from_user_key(user_key)
                print(format_fingerprint(anna.fingerprint))
        """
        return await self.execute(_spec.get_user_key(user_id))

    # --- Token ---

    async def revoke_token(self) -> bool:
        """Burns this token, for good.

        Calls ``POST /token/revoke``. Nothing works again, this client included, until the
        owner makes a new token in the console; the owner is told at once. It works while the
        bot is paused.

        Retried after a network error or a ``5xx`` answer. If an earlier answer was lost and
        the retry gets ``unauthorized``, the first try worked, and the method returns ``True``.

        Returns:
            ``True`` once the token is revoked.

        Example:
            For a program that finds out its token leaked::

                if token_was_leaked:
                    await api.revoke_token()

        Warning:
            This cannot be undone. Every later call with this token raises
            `AuthenticationError`.
        """
        return await self.execute(_spec.revoke_token())

    async def get_web_bot_key(self) -> str:
        """Fetches the key that checks the ``initData`` of a Web-Bot page.

        Calls ``GET /webbot/key``. The key is public and the same for every bot, so it can be
        fetched once and kept in the bot's configuration. Retried after a network error or a
        ``5xx`` answer.

        Returns:
            The server's Ed25519 public key: its 32 bytes in base64url, the form
            `~hidesay_bot.validate_web_bot_data` takes.

        Example:
            Fetch it once, then check every request offline::

                key = await api.get_web_bot_key()
                me = await api.get_me()
                data = validate_web_bot_data(init_data, bot_id=me.id, public_key=key)
        """
        return await self.execute(_spec.get_web_bot_key())

    # --- more on messages ---

    async def delete_messages(self, chat_id: str | int, message_ids: Iterable[int]) -> list[int]:
        """Deletes up to 100 messages of one chat at once, for everyone, for good.

        Calls ``POST /messages/delete``, with the rules of `delete_message` for each message:
        anything in a private chat; in a group or channel the bot's own, anybody's with the
        *delete messages* right, anybody's comment with the *manage comments* right. A post
        takes its comments with it. IDs that are not of this chat, or that the bot was never
        handed, are left out. If one message is not the bot's to delete, nothing is deleted.
        Before sending, it checks the chat and that there are 1 to 100 positive IDs. Retried
        after a network error or a ``5xx`` answer: a repeat leaves out what is already gone.

        Args:
            chat_id: The chat the messages are in.
            message_ids: The messages, 1 to 100 IDs.

        Returns:
            The IDs that went, the comments taken with their posts included.

        Raises:
            ValidationError: No IDs, more than 100, or one that is not a positive integer.
            ForbiddenError: One of the messages is not the bot's to delete. Nothing was deleted.
            ChatNotFoundError: No such chat, or the bot is not in it.

        Example:
            Clear what a flood left behind::

                gone = await api.delete_messages("G48213907", [90213, 90214, 90215])
        """
        return await self.execute(_spec.delete_messages(chat_id, message_ids))

    async def forward_message(
        self,
        chat_id: str | int,
        from_chat_id: str | int,
        message_id: int,
        *,
        hide_sender: bool | None = None,
        silent: bool | None = None,
        send_id: str | uuid.UUID | None = None,
        chat_kind: ChatKind | None = None,
    ) -> SentMessageBase:
        """Sends a message the bot was handed on into another chat it is in.

        Calls ``POST /messages/forward``. The forward carries "forwarded from" over it: the
        person, or the channel and its post. A forward of a forward keeps the first origin.
        Files go along: the server copies them for the bot, so the forward outlives the
        original. A chat that protects its content does not let its messages out, and
        comments are not forwarded.

        The server forwards only from a readable chat into a readable chat. A message from, or
        into, an end-to-end encrypted chat answers `ChatIsProtectedError`: the bot has to open
        it and seal it again itself, which is what `Bot.forward_message` does.

        Like a send it carries a ``sendId``, so it is retried after a network error or a
        ``5xx`` answer without forwarding twice, and it is paced like a send into the target.

        Args:
            chat_id: Where to forward it.
            from_chat_id: The chat the message is in.
            message_id: The message.
            hide_sender: ``True`` sends a plain copy, without "forwarded from".
            silent: Channels only: ``True`` posts without a notification.
            send_id: A UUID that names this forward. ``None`` makes a new one.
            chat_kind: The `ChatKind` of the target, for the rate limiter only. ``None`` reads
                it from the ID.

        Returns:
            The forward, as the server stored it.

        Raises:
            ChatIsProtectedError: One of the two chats is end-to-end encrypted, or the message
                is.
            ForbiddenError: The bot may not write in the target, or the source protects its
                content.
            BlockedByUserError: The person on the other side of the target blocked the bot.
            ChatNotFoundError: One of the chats does not exist, or the bot is not in it.
            MessageNotFoundError: No such message, or one the bot was never handed.
            BadRequestError: The message is a comment.
        """
        return await self.execute(
            _spec.forward_message(
                chat_id,
                from_chat_id,
                message_id,
                hide_sender=hide_sender,
                silent=silent,
                send_id=send_id,
                chat_kind=chat_kind,
            )
        )

    async def set_reaction(self, chat_id: str | int, message_id: int, emoji: str) -> MessageReactions:
        """Puts the bot's reaction on a message, in place of the one it had there.

        Calls ``PUT /messages/{messageId}/reaction``. One reaction per message, as for everybody.
        A custom emoji goes as the app writes it. The rules of a person's reaction hold: in a
        private chat nobody blocked anybody; in a group or channel reactions are on, the emoji
        is one the chat takes (`Chat.allowed_reactions`) and the bot may react. Only messages
        the bot was handed. Before sending, it checks that ``emoji`` is 1 to 48 characters once
        trimmed. Retried after a network error or a ``5xx`` answer: the same reaction twice is
        one reaction.

        Args:
            chat_id: The chat the message is in.
            message_id: The message.
            emoji: The emoji.

        Returns:
            Every reaction on the message now, the bot's marked ``mine``.

        Raises:
            ValidationError: ``emoji`` is empty or too long.
            ForbiddenError: Reactions are off, the chat does not take this emoji, or the bot
                may not react.
            BlockedByUserError: In a private chat, the person blocked the bot.
            MessageNotFoundError: No such message, or one the bot was never handed.

        Example:
            Say "seen" without writing::

                await api.set_reaction(chat_id, message_id, "👀")
        """
        return await self.execute(_spec.set_reaction(chat_id, message_id, emoji))

    async def remove_reaction(self, chat_id: str | int, message_id: int) -> bool:
        """Takes the bot's reaction off a message.

        Calls ``DELETE /messages/{messageId}/reaction``, with ``chatId`` in the query. Retried
        after a network error or a ``5xx`` answer.

        Args:
            chat_id: The chat the message is in.
            message_id: The message.

        Returns:
            ``True`` when the bot had a reaction there, ``False`` when it had none.

        Raises:
            BlockedByUserError: In a private chat, the person blocked the bot.
            MessageNotFoundError: No such message, or one the bot was never handed.
        """
        return await self.execute(_spec.remove_reaction(chat_id, message_id))

    # --- files and stickers ---

    async def upload_file(self, data: bytes, *, limit: int = UPLOAD_MAX) -> UploadedFile:
        """Uploads a file for a message.

        Calls ``POST /attachments``, as ``multipart/form-data`` with the bytes in a field called
        ``file``. The server takes up to 50 MB, 30 uploads a minute. The file goes up as it is,
        under a neutral name and type: say what it is when you send it.

        Into a readable chat, upload the file as it is and pass its ID in the ``media`` of
        `send_message`. Into an encrypted chat, upload it already sealed to the chat's keys and
        the bot's own, and put its ID in the envelope inside the ``armored`` body.
        `Bot.send_media` does either. Retried after a network error or a ``5xx`` answer; a lost
        answer leaves an extra copy on the server that no message points at.

        Args:
            data: The file's bytes.
            limit: The largest file accepted, in bytes. 50 MiB by default, the server's limit
                (`Limits.upload_bytes_max`).

        Returns:
            The file's ID, its address and the size the server stored.

        Raises:
            ValidationError: ``data`` is not bytes, or is larger than ``limit``.
            PayloadTooLargeError: The server's limit is lower.
            FloodWaitError: More than 30 uploads this minute.
            ServerError: ``try_again``: the server is short of disk right now.
        """
        return await self.execute(_spec.upload_file(data, limit=limit))

    async def get_sticker(self, sticker_id: int, media_id: str | None = None) -> Sticker:
        """Fetches a sticker the bot may send.

        Calls ``GET /stickers/{stickerId}``. A sticker of a public pack, or one the bot was
        shown: pass the ``media_id`` a message carried for it as proof. For a readable chat,
        `send_message` needs only the ID; for an encrypted chat this is what goes in the
        envelope. Retried after a network error or a ``5xx`` answer.

        Args:
            sticker_id: The sticker's ID.
            media_id: The sticker's file ID, as a message carried it, for a pack that is not
                public.

        Returns:
            The sticker.

        Raises:
            StickerNotFoundError: No such sticker, or one of a pack the bot can't use.
        """
        return await self.execute(_spec.get_sticker(sticker_id, media_id))

    # --- chats and history ---

    async def get_chats(self) -> list[Chat]:
        """Lists the groups and channels the bot is in.

        Calls ``GET /chats``. Each one as `get_chat` describes it, the bot's seat included.
        Private chats are not listed: they are the people who wrote to the bot. Retried after
        a network error or a ``5xx`` answer.

        Returns:
            The groups and channels.

        Example:
            Where the bot may not read everything::

                for chat in await api.get_chats():
                    if not chat.reads_all:
                        print(chat.title, "only sends me what is meant for me")
        """
        chats: list[Chat] = await self.execute(_spec.get_chats())
        return chats

    async def get_history(
        self, chat_id: str | int, *, before: int | None = None, limit: int | None = None
    ) -> list[MessageBase]:
        """Fetches what the bot was handed in a chat, newest first.

        Calls ``GET /chats/{chatId}/messages``. In a private chat that is every message of it.
        In a group or channel it is the bot's own messages and the ones the room delivered to
        it, and nothing else: what it was not let read stays out, even after the room lets it
        read everything. Comments are not listed here, see `get_comments`. The messages come
        as the server keeps them: in an encrypted chat ``armored``, which `Bot.get_history`
        decrypts. Retried after a network error or a ``5xx`` answer.

        Args:
            chat_id: The chat.
            before: The oldest ID you hold, for the next page back. ``None`` for the newest.
            limit: How many, 1 to 100. ``None`` leaves it to the server, which gives 50.

        Returns:
            The messages, newest first. Empty when there are none.

        Raises:
            ValidationError: ``before`` or ``limit`` out of range.
            ChatNotFoundError: No such chat, or the bot is not in it.

        Example:
            Every page, back to the start::

                page = await api.get_history(chat_id, limit=100)
                while page:
                    store(page)
                    page = await api.get_history(chat_id, before=page[-1].id, limit=100)
        """
        messages: list[MessageBase] = await self.execute(_spec.get_history(chat_id, before=before, limit=limit))
        return messages

    async def get_message(self, chat_id: str | int, message_id: int) -> MessageBase:
        """Fetches one message the bot was handed.

        Calls ``GET /chats/{chatId}/messages/{messageId}``. The rule of `get_history` holds: a
        message the bot was not handed answers like one that doesn't exist. Retried after a
        network error or a ``5xx`` answer.

        Args:
            chat_id: The chat.
            message_id: The message.

        Returns:
            The message, as the server keeps it.

        Raises:
            ChatNotFoundError: No such chat, or the bot is not in it.
            MessageNotFoundError: No such message, or one the bot was never handed.
        """
        return await self.execute(_spec.get_message(chat_id, message_id))

    async def read_chat(self, chat_id: str | int, up_to: int | None = None) -> int | None:
        """Marks a chat read: the ticks people see.

        Calls ``POST /chats/{chatId}/read``. In a group or channel only a bot the room lets read
        every message may do it: one that gets only what is meant for it would show up as
        having read people it never saw. Retried after a network error or a ``5xx`` answer.

        Args:
            chat_id: The chat.
            up_to: The newest message read. ``None`` marks everything.

        Returns:
            The ID of the last message marked read, or ``None`` when the server did not say.

        Raises:
            ReadsAllNeededError: A group or channel that does not let the bot read every
                message.
            ChatNotFoundError: No such chat, or the bot is not in it.
        """
        return await self.execute(_spec.read_chat(chat_id, up_to))

    async def pin_message(self, chat_id: str | int, message_id: int) -> list[int]:
        """Pins a message for everybody in the chat.

        Calls ``PUT /chats/{chatId}/pins/{messageId}``. In a group or channel it needs the *pin
        messages* right, or the members' permission to pin. In a private chat it is pinned for
        the person too, and the chat says so with a notice; up to 100. Only messages the bot
        was handed, not notices. Retried after a network error or a ``5xx`` answer.

        Args:
            chat_id: The chat.
            message_id: The message.

        Returns:
            The IDs of the messages pinned in the chat now.

        Raises:
            ForbiddenError: The bot may not pin messages here.
            BlockedByUserError: In a private chat, the person blocked the bot.
            MessageNotFoundError: No such message, or one the bot was never handed.
        """
        pinned: list[int] = await self.execute(_spec.pin_message(chat_id, message_id))
        return pinned

    async def unpin_message(self, chat_id: str | int, message_id: int) -> list[int]:
        """Unpins a message.

        Calls ``DELETE /chats/{chatId}/pins/{messageId}``, with the rights of `pin_message`.
        Retried after a network error or a ``5xx`` answer.

        Args:
            chat_id: The chat.
            message_id: The message.

        Returns:
            The IDs of the messages still pinned in the chat.

        Raises:
            ForbiddenError: The bot may not unpin messages here.
            MessageNotFoundError: No such message, or one the bot was never handed.
        """
        pinned: list[int] = await self.execute(_spec.unpin_message(chat_id, message_id))
        return pinned

    async def unpin_all(self, chat_id: str | int) -> list[int]:
        """Unpins every message of a group or channel.

        Calls ``DELETE /chats/{chatId}/pins``, with the rights of `pin_message`. Not for a
        private chat: the person's own pins there are not the bot's to clear. Retried after a
        network error or a ``5xx`` answer.

        Args:
            chat_id: The group or channel.

        Returns:
            The IDs still pinned: none.

        Raises:
            ValidationError: ``chat_id`` is a private chat.
            ForbiddenError: The bot may not unpin messages here.
        """
        pinned: list[int] = await self.execute(_spec.unpin_all(chat_id))
        return pinned

    async def get_comments(
        self, chat_id: str | int, post_id: int, *, before: int | None = None, limit: int | None = None
    ) -> Comments:
        """Fetches the comments the bot was handed under a channel post, newest first.

        Calls ``GET /chats/{chatId}/posts/{postId}/comments``. The rule of `get_history` holds.
        Retried after a network error or a ``5xx`` answer.

        Args:
            chat_id: The channel.
            post_id: The post, one the bot was handed.
            before: The oldest comment ID you hold, for the next page back.
            limit: How many, 1 to 100. ``None`` leaves it to the server, which gives 50.

        Returns:
            The comments, and whether the post takes comments right now.

        Raises:
            ValidationError: ``chat_id`` is not a channel, or ``before`` or ``limit`` is out of
                range.
            MessageNotFoundError: No such post, or one the bot was never handed.
        """
        return await self.execute(_spec.get_comments(chat_id, post_id, before=before, limit=limit))

    async def send_comment(
        self,
        chat_id: str | int,
        post_id: int,
        text: str | None = None,
        *,
        armored: str | None = None,
        reply_to: int | None = None,
        as_author: bool | None = None,
        mentions: Iterable[str | int] | None = None,
    ) -> SentMessageBase:
        """Comments under a channel post.

        Calls ``POST /chats/{chatId}/posts/{postId}/comments``, with the rules of a person's
        comment: the channel's comments are on and this post's are not closed, a member whose
        voice was taken away stays quiet, and ``reply_to`` points at a comment of the same
        post or is dropped. ``text`` in a readable channel, ``armored`` in an encrypted one,
        sealed to the channel's keys. A bot that runs the channel speaks as the channel unless
        the channel lets its admins sign and ``as_author`` is ``True``.

        The server keeps no ``sendId`` for comments, so a comment is not repeated after a
        network error: a lost answer could otherwise write it twice. It is paced like a send
        into the channel.

        Args:
            chat_id: The channel.
            post_id: The post, one the bot was handed.
            text: The comment, for a readable channel.
            armored: The sealed comment, for an encrypted channel.
            reply_to: A comment of the same post to answer.
            as_author: ``True`` signs the comment with the bot's own name, where the channel
                allows it.
            mentions: The IDs of the people the words tag, up to 20, so they are told even with
                the comments muted.

        Returns:
            The comment, as the server stored it.

        Raises:
            ValidationError: ``chat_id`` is not a channel, a body breaks a rule, or both or
                neither of ``text`` and ``armored`` were given.
            WrongBodyKindError: ``text`` into an encrypted channel, or ``armored`` into a
                readable one.
            CommentsClosedError: The channel takes no comments, or this post doesn't.
            ForbiddenError: The bot may not comment here.
            MessageNotFoundError: No such post, or one the bot was never handed.
        """
        return await self.execute(
            _spec.send_comment(
                chat_id, post_id, text, armored=armored, reply_to=reply_to, as_author=as_author, mentions=mentions
            )
        )

    async def get_user(self, user_id: str | int) -> Profile:
        """Fetches somebody's public profile.

        Calls ``GET /users/{userId}``. Only for somebody the bot may look up: a private chat
        with them, a room whose member list shows them to the bot, or a room where their words
        reached it. The same gate as their key. No phone number, no presence. Somebody who blocked the bot keeps
        their pictures to themselves. Retried after a network error or a ``5xx`` answer.

        Args:
            user_id: The person, or a bot.

        Returns:
            The profile.

        Raises:
            UserNotFoundError: No such person, or nobody the bot may look up.
        """
        return await self.execute(_spec.get_user(user_id))

    # --- running a group or channel ---

    async def update_chat(
        self,
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
    ) -> Chat:
        """Changes the settings of a group or channel.

        Calls ``PATCH /chats/{chatId}``. Each field under the right the app asks for it:
        ``title`` and ``description`` for whoever may change the chat's info (an admin with
        *change info*, or a member the group lets); ``permissions``, ``slow_mode_seconds``,
        ``members_hidden``, ``protect_content``, the reactions and a channel's signatures for
        an admin with *change info*; ``auto_delete_seconds`` for one with *delete messages*;
        ``admins_hidden`` for one who may appoint admins. What only the owner decides
        (history, the public link, the feed) is not here. Whether a bot reads every message
        is not here either: the room decides that in the app. Leave a field out to keep it.
        Retried after a network error or a ``5xx`` answer.

        Args:
            chat_id: The group or channel.
            title: The new name, 1 to 64 characters.
            description: The new description, up to 500 characters; ``None`` clears it.
            permissions: What members may do, laid over what they may do now: a name set to
                ``False`` is taken away, ``True`` is given, the rest stays. Names as in
                `MemberPermissions`, or in the API's spelling (``"sendMedia"``). Groups only.
            slow_mode_seconds: Seconds between two messages of a member, 0 to 3600; ``0``
                turns slow mode off. Groups only.
            members_hidden: ``True`` shows the member list to the admins only.
            protect_content: ``True`` stops messages from being forwarded, copied or saved.
            reactions_enabled: ``False`` turns reactions off.
            allowed_reactions: The only emoji people may react with, up to 40 of at most 16
                characters each; ``None`` or an empty list allows any.
            auto_delete_seconds: After how many seconds new messages delete themselves, up to
                31 days; ``0`` turns it off.
            signatures: ``True`` shows who wrote each post. Channels only.
            signature_profiles: ``True`` links a signature to the profile. Channels only.
            admins_hidden: ``True`` keeps the admins from the subscribers. Channels only.

        Returns:
            The chat with the new settings.

        Raises:
            ValidationError: Nothing to change, a value out of range, an unknown permission,
                a group's setting on a channel or the other way round, or a private chat.
            ForbiddenError: The bot lacks the right one of the fields needs.

        Example:
            Slow the group down and keep files out::

                await api.update_chat("G48213907", slow_mode_seconds=30, permissions={"send_media": False})
        """
        return await self.execute(
            _spec.update_chat(
                chat_id,
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
        )

    async def update_member(
        self,
        chat_id: str | int,
        user_id: str | int,
        *,
        role: str | None = None,
        admin_rights: Iterable[str] | None = None,
        restrictions: Iterable[str] | None = None,
        restricted_until: datetime | int | None = KEEP,
        custom_title: str | None = KEEP,
        anonymous: bool | None = None,
    ) -> Member:
        """Promotes, demotes, restricts or gives a title to a member of a group or channel.

        Calls ``PATCH /chats/{chatId}/members/{userId}``, with the app's rules to the letter.
        ``role`` and ``admin_rights`` need the *manage admins* right, and nobody hands out a
        right they don't hold; an admin is touched only by the owner or by whoever appointed
        them. ``restrictions`` need *ban users* and mean nothing on an admin. ``custom_title``
        and ``anonymous`` need *manage admins*. The owner is nobody's to change, and a bot
        doesn't change its own seat. Retried after a network error or a ``5xx`` answer.

        Args:
            chat_id: The group or channel.
            user_id: The member.
            role: ``"admin"`` promotes, ``"member"`` demotes.
            admin_rights: The rights of the badge, names as in `AdminRights` or in the API's
                spelling. With ``role="admin"`` they are the new admin's rights (the server's
                defaults when left out); alone, they replace an admin's rights.
            restrictions: The permissions taken away from this member, names as in
                `MemberPermissions`; an empty list gives them all back.
            restricted_until: When the restriction ends: an aware `~datetime.datetime` or unix
                milliseconds; ``None`` for good. Only with ``restrictions``.
            custom_title: An admin's title, up to 16 characters; ``None`` removes it.
            anonymous: ``True`` lets an admin post without their name.

        Returns:
            The member's seat now.

        Raises:
            ValidationError: Nothing to change, an unknown role, right or permission, a title
                too long, ``restricted_until`` without ``restrictions``, or a private chat.
            ForbiddenError: The bot lacks the right, the member is the owner or an admin
                appointed by somebody else, or it is the bot's own seat.
            MemberNotFoundError: Not a member, or hidden from the bot.

        Example:
            Mute somebody for an hour::

                until = datetime.now(UTC) + timedelta(hours=1)
                await api.update_member(group_id, user_id, restrictions=["send_messages"], restricted_until=until)
        """
        return await self.execute(
            _spec.update_member(
                chat_id,
                user_id,
                role=role,
                admin_rights=admin_rights,
                restrictions=restrictions,
                restricted_until=restricted_until,
                custom_title=custom_title,
                anonymous=anonymous,
            )
        )

    async def remove_member(
        self, chat_id: str | int, user_id: str | int, *, ban: bool = False, banned_until: datetime | int | None = None
    ) -> bool:
        """Throws somebody out of a group or channel, and bans them if asked.

        Calls ``DELETE /chats/{chatId}/members/{userId}``. It needs the *ban users* right; never
        the owner, and an admin only by the owner, which a bot never is. A ban keeps them from
        coming back through any link, until ``banned_until`` or for good. In a group the room
        sees the usual notice. Retried after a network error or a ``5xx`` answer; when the
        retry finds them gone, the first try worked.

        Args:
            chat_id: The group or channel.
            user_id: The member.
            ban: ``True`` bans them as well.
            banned_until: When the ban ends: an aware `~datetime.datetime` or unix milliseconds.
                ``None`` for good. Only with ``ban=True``.

        Returns:
            ``True`` when they were banned as well, ``False`` when only removed.

        Raises:
            ValidationError: ``banned_until`` without ``ban=True``, or a private chat.
            ForbiddenError: The bot lacks the right, or the member is the owner or an admin.
            MemberNotFoundError: Not a member, or hidden from the bot.
        """
        return await self.execute(_spec.remove_member(chat_id, user_id, ban=ban, banned_until=banned_until))

    async def purge_member(self, chat_id: str | int, user_id: str | int, *, up_to: int | None = None) -> bool:
        """Deletes everything one member wrote in a group or channel.

        Calls ``DELETE /chats/{chatId}/members/{userId}/messages``. It needs the *delete
        messages* right, as in the app. Retried after a network error or a ``5xx`` answer.

        Args:
            chat_id: The group or channel.
            user_id: The member.
            up_to: Only up to this message ID. ``None`` deletes all of it.

        Returns:
            ``True`` once it is done.

        Raises:
            ValidationError: A private chat.
            ForbiddenError: The bot lacks the right.
            MemberNotFoundError: No such person.
        """
        return await self.execute(_spec.purge_member(chat_id, user_id, up_to=up_to))

    async def get_bans(self, chat_id: str | int) -> list[Ban]:
        """Lists who is banned from a group or channel.

        Calls ``GET /chats/{chatId}/bans``. It needs the *ban users* right. Retried after a
        network error or a ``5xx`` answer.

        Args:
            chat_id: The group or channel.

        Returns:
            The bans.

        Raises:
            ValidationError: A private chat.
            ForbiddenError: The bot lacks the right.
        """
        bans: list[Ban] = await self.execute(_spec.get_bans(chat_id))
        return bans

    async def unban(self, chat_id: str | int, user_id: str | int) -> bool:
        """Lifts a ban: the person can come back through a link.

        Calls ``DELETE /chats/{chatId}/bans/{userId}``. It needs the *ban users* right. Nobody
        is put back in. Retried after a network error or a ``5xx`` answer.

        Args:
            chat_id: The group or channel.
            user_id: The person.

        Returns:
            ``True`` when there was a ban, ``False`` when there was none.

        Raises:
            ValidationError: A private chat.
            ForbiddenError: The bot lacks the right.
        """
        return await self.execute(_spec.unban(chat_id, user_id))

    async def get_invites(self, chat_id: str | int) -> list[Invite]:
        """Lists the invite links of a group or channel.

        Calls ``GET /chats/{chatId}/invites``. An admin with the *invite links* right may, and
        so may a member the group lets add people. Retried after a network error or a ``5xx``
        answer.

        Args:
            chat_id: The group or channel.

        Returns:
            The links.

        Raises:
            ValidationError: A private chat.
            ForbiddenError: The bot may not manage links here.
        """
        invites: list[Invite] = await self.execute(_spec.get_invites(chat_id))
        return invites

    async def create_invite(
        self,
        chat_id: str | int,
        *,
        label: str | None = None,
        expires_at: datetime | int | None = None,
        max_uses: int | None = None,
        needs_approval: bool | None = None,
    ) -> Invite:
        """Makes an invite link.

        Calls ``POST /chats/{chatId}/invites``, with the right of `get_invites`. A public chat
        already has its link, its handle. With ``needs_approval`` whoever uses the link asks
        first, and the bots that may let people in get a ``join_request``. Not repeated after a
        network error: a lost answer would otherwise make two links.

        Args:
            chat_id: The group or channel.
            label: A name for the link, up to 48 characters.
            expires_at: When it stops working: an aware `~datetime.datetime` or unix
                milliseconds. ``None`` for never.
            max_uses: How many people may use it, 1 to 100000. ``None`` for no limit.
            needs_approval: ``True`` makes people ask before they come in.

        Returns:
            The new link.

        Raises:
            ValidationError: A value out of range, or a private chat.
            ForbiddenError: The bot may not make links here.
            ChatIsPublicError: The chat is public: its handle is its link.
        """
        return await self.execute(
            _spec.create_invite(
                chat_id, label=label, expires_at=expires_at, max_uses=max_uses, needs_approval=needs_approval
            )
        )

    async def revoke_invite(self, chat_id: str | int, code: str) -> bool:
        """Revokes an invite link.

        Calls ``DELETE /chats/{chatId}/invites/{code}``. It needs the *invite links* right.
        Retried after a network error or a ``5xx`` answer; when the retry finds the link gone,
        the first try worked.

        Args:
            chat_id: The group or channel.
            code: The link's code, `Invite.code`.

        Returns:
            ``True`` once it is revoked.

        Raises:
            ValidationError: A private chat, or not a code.
            ForbiddenError: The bot lacks the right.
            InviteNotFoundError: No such link in this chat.
        """
        return await self.execute(_spec.revoke_invite(chat_id, code))

    async def get_join_requests(self, chat_id: str | int) -> list[JoinRequest]:
        """Lists who asked to come into a group or channel.

        Calls ``GET /chats/{chatId}/requests``. It needs the *add members* right. Retried after
        a network error or a ``5xx`` answer.

        Args:
            chat_id: The group or channel.

        Returns:
            The requests waiting.

        Raises:
            ValidationError: A private chat.
            ForbiddenError: The bot lacks the right.
        """
        requests: list[JoinRequest] = await self.execute(_spec.get_join_requests(chat_id))
        return requests

    async def decide_join_request(self, chat_id: str | int, user_id: str | int, approve: bool) -> bool:
        """Lets somebody in who asked, or turns them away.

        Calls ``POST /chats/{chatId}/requests/{userId}``. It needs the *add members* right, and
        only works for somebody who asked: it is never a way round a person's "don't add me
        to groups". Retried after a network error or a ``5xx`` answer; when the retry finds the
        request gone, the first try worked.

        Args:
            chat_id: The group or channel.
            user_id: The person who asked.
            approve: ``True`` lets them in, ``False`` turns them away.

        Returns:
            ``approve``, as the server confirms it.

        Raises:
            ValidationError: A private chat, or ``approve`` is not a bool.
            ForbiddenError: The bot lacks the right.
            JoinRequestNotFoundError: They have no request waiting.
            ChatFullError: The chat can't take anybody else. The request stays, to
                be decided when there is room.
        """
        return await self.execute(_spec.decide_join_request(chat_id, user_id, approve))

    async def set_comments(
        self, chat_id: str | int, enabled: bool, *, signing: bool | None = None, outside: bool | None = None
    ) -> Chat:
        """Turns a channel's comments on or off.

        Calls ``PATCH /chats/{chatId}/comments``. It needs the *manage comments* right. Retried
        after a network error or a ``5xx`` answer.

        Args:
            chat_id: The channel.
            enabled: ``True`` turns the comments on.
            signing: ``True`` lets the admins comment under their own name. Left out, the server
                takes it as ``False``.
            outside: ``True`` lets people who aren't subscribed comment on a public readable
                channel. Left out, it stays as it is.

        Returns:
            The channel with the new settings.

        Raises:
            ValidationError: Not a channel.
            ForbiddenError: The bot lacks the right.
        """
        return await self.execute(_spec.set_comments(chat_id, enabled, signing=signing, outside=outside))

    async def set_post_comments(self, chat_id: str | int, post_id: int, comments_off: bool) -> bool:
        """Closes or opens again the comments of one post, whatever the channel says.

        Calls ``PATCH /chats/{chatId}/posts/{postId}``. It needs the *manage comments* right.
        Retried after a network error or a ``5xx`` answer.

        Args:
            chat_id: The channel.
            post_id: The post, one the bot was handed.
            comments_off: ``True`` closes them, ``False`` opens them.

        Returns:
            Whether the post's comments are closed now.

        Raises:
            ValidationError: Not a channel.
            ForbiddenError: The bot lacks the right.
            MessageNotFoundError: No such post, or one the bot was never handed.
        """
        return await self.execute(_spec.set_post_comments(chat_id, post_id, comments_off))

    async def get_channel_stats(self, chat_id: str | int) -> ChannelStats:
        """Fetches a channel's numbers: subscribers coming and going, views per post.

        Calls ``GET /chats/{chatId}/stats``. For its admins. No words: the best posts come by
        ID. Retried after a network error or a ``5xx`` answer.

        Args:
            chat_id: The channel.

        Returns:
            The numbers.

        Raises:
            ValidationError: Not a channel.
            ForbiddenError: The bot is not one of the channel's admins.
        """
        return await self.execute(_spec.get_channel_stats(chat_id))

    async def set_chat_photo(self, chat_id: str | int, data: bytes, *, limit: int = UPLOAD_MAX) -> Picture:
        """Changes the picture of a group or channel.

        Calls ``PUT /chats/{chatId}/photo``, as ``multipart/form-data``. A PNG, JPEG or WebP, or
        a short MP4 or WebM clip, judged by its bytes. Whoever may change the chat's info may:
        an admin with *change info*, or a member the group lets. The picture it replaces stays
        in the chat's album. Not repeated after a network error: a lost answer would put the
        picture up twice.

        Args:
            chat_id: The group or channel.
            data: The picture's bytes.
            limit: The largest file accepted before anything is sent, in bytes. The server has
                a lower limit of its own for pictures.

        Returns:
            Where the new picture is, and what it turned out to be.

        Raises:
            ValidationError: Not bytes, too large, or a private chat.
            ForbiddenError: The bot may not change the chat's info.
            PayloadTooLargeError: The picture is over the server's limit.
            BadRequestError: Not a picture or a clip (status ``415``).
        """
        return await self.execute(_spec.set_chat_photo(chat_id, data, limit=limit))

    async def delete_chat_photo(self, chat_id: str | int) -> bool:
        """Removes the picture of a group or channel.

        Calls ``DELETE /chats/{chatId}/photo``, with the right of `set_chat_photo`. The current
        picture leaves the album and the one before it, if any, is worn again. Not repeated
        after a network error: a second delete would take that one off too.

        Args:
            chat_id: The group or channel.

        Returns:
            ``True`` when there was a picture to remove.

        Raises:
            ValidationError: A private chat.
            ForbiddenError: The bot may not change the chat's info.
        """
        return await self.execute(_spec.delete_chat_photo(chat_id))

    async def set_chat_banner(self, chat_id: str | int, data: bytes, *, limit: int = UPLOAD_MAX) -> Picture:
        """Changes the banner of a group or channel, like `set_chat_photo`.

        Calls ``PUT /chats/{chatId}/banner``. Not repeated after a network error.

        Args:
            chat_id: The group or channel.
            data: The banner's bytes: a PNG, JPEG or WebP, or a short MP4 or WebM clip.
            limit: The largest file accepted before anything is sent, in bytes.

        Returns:
            Where the new banner is, and what it turned out to be.

        Raises:
            ValidationError: Not bytes, too large, or a private chat.
            ForbiddenError: The bot may not change the chat's info.
            PayloadTooLargeError: The banner is over the server's limit.
            BadRequestError: Not a picture or a clip (status ``415``).
        """
        return await self.execute(_spec.set_chat_banner(chat_id, data, limit=limit))

    async def delete_chat_banner(self, chat_id: str | int) -> bool:
        """Removes the banner of a group or channel, like `delete_chat_photo`.

        Calls ``DELETE /chats/{chatId}/banner``. Not repeated after a network error.

        Args:
            chat_id: The group or channel.

        Returns:
            ``True`` when there was a banner to remove.

        Raises:
            ValidationError: A private chat.
            ForbiddenError: The bot may not change the chat's info.
        """
        return await self.execute(_spec.delete_chat_banner(chat_id))

    # --- not an operation of the spec, but where the media URLs point ---

    async def download_media(self, media: Media | str, *, max_bytes: int = MEDIA_MAX_BYTES) -> bytes:
        """Downloads the file behind a `Media`, exactly as the server stores it.

        Fetches ``GET /media/{id}`` on the API server, the address a `Media` carries in
        ``url``. This is not an operation of the Bot API document, and it skips the rate
        limiter and the retries: an error is raised at once. The token is not sent, since the
        server serves media without it. Only ``/media/<id>`` addresses on this client's API
        server are fetched, so a crafted URL in an update cannot send the client anywhere
        else. The download has timeouts of its own: 10 seconds to connect, and 120 seconds to
        wait for each piece of data.

        Args:
            media: A `Media`, its ``/media/<id>`` path, its full URL on the API server, or the
                bare ID.
            max_bytes: The largest file to accept, 64 MiB by default. It is checked against the
                size the server declares, and again while downloading.

        Returns:
            The file's bytes. For an attachment of an encrypted chat they are OpenPGP
            ciphertext, which `Bot.download_media` decrypts.

        Raises:
            ValidationError: ``media`` is not a media file of this server, or the file is
                larger than ``max_bytes``.
            NotFoundError: No such file. ``code`` is ``"media_not_found"``.
            ServerError: The server answered with a ``5xx`` status.
            APIError: The server answered with a status other than 200. ``code`` is
                ``"http_<status>"``.
            NetworkError: No answer came. `RequestTimeoutError` when it took too long.
            ConfigurationError: The ``http_client`` you passed in was closed.

        Example:
            The first attachment of a message::

                data = await api.download_media(message.media[0])
        """
        url = self._media_url(media)
        http = self._client()
        try:
            async with http.stream(
                "GET", url, headers={"User-Agent": self._user_agent}, timeout=httpx2.Timeout(120.0, connect=10.0)
            ) as response:
                if response.status_code != 200:
                    raise _media_status_error(response.status_code, urlsplit(url).path)
                declared = response.headers.get("content-length")
                if declared and declared.isascii() and declared.isdigit() and int(declared) > max_bytes:
                    raise ValidationError(f"the file is {declared} bytes, more than max_bytes={max_bytes}")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValidationError(f"the file is larger than max_bytes={max_bytes}")
                    chunks.append(chunk)
                return b"".join(chunks)
        except httpx2.HTTPError as exc:
            raise _media_error(urlsplit(url).path, exc) from exc


# --- sync ---


class Client(_Base):
    """The Bot API, one blocking method per operation.

    It has the same methods as `AsyncClient`, with the same arguments: call them without
    ``await``. Each method checks its arguments against the server's rules before anything is
    sent, waits when the client-side `RateLimiter` says so, and returns the answer as a typed
    object. A call that is safe to repeat is tried again after a network error or a ``5xx``
    answer, and ``429`` answers are waited out, as the `RetryPolicy` allows. Every API call can
    raise the errors listed in :ref:`common-errors`; each method lists the errors specific to
    it.

    Each call blocks the calling thread until it is done, including the waits for the rate
    limiter and between retries. In ``async`` code, use `AsyncClient` instead.

    The client never encrypts or decrypts: ``text`` and ``armored`` go out as you give them,
    and the attachments of an encrypted chat come back as OpenPGP ciphertext. `SyncBot` does
    the OpenPGP part on top of this client, and gives you its own client as ``bot.api``.

    One client can be shared by many threads: its connection pool and its rate limiter are
    safe to use from several threads at once. Use it as a context manager (``with``), or call
    `close` when you are done. Unlike an `AsyncClient`, a closed client does not open a new
    pool: make a new client instead.

    The token travels only in the ``Authorization`` header. It is never in ``repr()``, in
    error messages or in logs (`token_hint` shows the part that is safe to show), and a client
    cannot be pickled.

    Attributes:
        bot_id (int): The number in the token. It is not the bot's ID, which `get_me` gives
            (``"B48213907"``), and a token made before IDs became strings carries the old number.
        api_url (str): The server's address, without ``/bot/v1`` and the trailing slash.
        timeout (httpx2.Timeout): The timeouts of every request, made from ``timeout``.
        retry (RetryPolicy): The retry rules in use.
        rate_limiter (RateLimiter | None): The limiter that paces the calls, or ``None`` when
            pacing is off.

    Example:
        Who the bot is, then a message into a readable chat::

            from hidesay_bot import Client

            with Client() as api:                     # token from HIDESAY_BOT_TOKEN
                me = api.get_me()
                api.send_message("48213907", f"Hello from @{me.username}")
    """

    def __init__(
        self,
        token: str | Secret | Callable[[], str] | None = None,
        *,
        api_url: str | None = None,
        timeout: float | httpx2.Timeout | None = None,
        retry: RetryPolicy | None = None,
        rate_limiter: RateLimiter | bool = True,
        allow_insecure_http: bool = False,
        user_agent: str | None = None,
        http_client: httpx2.Client | None = None,
        transport: httpx2.BaseTransport | None = None,
        max_connections: int = 20,
    ) -> None:
        """Sets up the client. Nothing is sent to the server until the first call.

        The token and the server address are checked here, so a wrong setup fails at once.
        The connection pool is made here too, unless you pass an ``http_client``.

        Args:
            token: The bot's token, ``pgpbot_<id>.<43 characters>``: a string, a `Secret`, or a
                function that returns one (called once, here). ``None`` reads
                ``HIDESAY_BOT_TOKEN``, or the file named by ``HIDESAY_BOT_TOKEN_FILE``; when
                both are set, the file wins.
            api_url: The API server, like ``https://api.hidesay.com``. A trailing ``/bot/v1``
                is removed. ``None`` reads ``HIDESAY_API``, and without it uses
                ``https://api.hidesay.com``.
            timeout: How long one request may take, in seconds, or an ``httpx2.Timeout``.
                ``None`` means 30 seconds, 10 of them at most to connect; a number ``n`` means
                ``n`` seconds, at most 10 of them to connect. A long poll may read for 15
                seconds more than it asks the server to wait, and `download_media` has limits
                of its own.
            retry: When and how often to try again. ``None`` means ``RetryPolicy()``: 4 tries
                in all for a call that is safe to repeat, and up to 5 ``429`` answers waited
                out, each for at most 90 seconds.
            rate_limiter: The client-side pacing. ``True`` makes a new `RateLimiter` with the
                default numbers, ``False`` turns pacing off, and a `RateLimiter` of your own is
                used as it is (one limiter can pace several clients). The client does not read
                the server's own numbers: for a server that uses others, call
                ``api.rate_limiter.update_limits(me.limits)`` with the `BotInfo` of `get_me`.
            allow_insecure_http: Allows an ``http://`` ``api_url`` on a host other than
                ``localhost``, ``127.0.0.1`` or ``::1``. Only for a test server on your own
                network: over plain HTTP the token travels in clear.
            user_agent: The ``User-Agent`` header. ``None`` means
                ``hidesay-bot/<version> (Python <version>)``.
            http_client: An ``httpx2.Client`` of your own, to share its connection pool. The
                client never closes it, and raises `ConfigurationError` on a call made after
                you closed it. ``transport`` and ``max_connections`` are then ignored.
            transport: An ``httpx2`` transport for the client's own pool: a proxy setup, or a
                mock transport in tests.
            max_connections: How many connections the client's own pool may open.

        Raises:
            ConfigurationError: No token was given or found, the token is not a bot token, the
                token file cannot be read, or ``api_url`` is not acceptable: not ``http`` or
                ``https``, plain ``http`` to another host without ``allow_insecure_http``, or
                with a user name, a password, a query or a fragment. The message never
                contains the token.
        """
        super().__init__(
            token,
            api_url=api_url,
            timeout=timeout,
            retry=retry,
            rate_limiter=rate_limiter,
            allow_insecure_http=allow_insecure_http,
            user_agent=user_agent,
        )
        self._owns_http = http_client is None
        self._http = http_client or httpx2.Client(
            timeout=self.timeout,
            limits=httpx2.Limits(max_connections=max_connections, max_keepalive_connections=10),
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        """Closes the client's connection pool, for good.

        An ``http_client`` you passed in is left open: closing it is up to you, and the client
        goes on working with it until you do. Otherwise the client cannot be used afterwards:
        every later call raises `ConfigurationError`, so make a new client to go on. Calling
        this twice does no harm.
        """
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()

    # --- the engine room ---

    def _once(self, call: Call[Any]) -> Any:
        if self._http.is_closed:
            raise ConfigurationError("this Client was closed")
        try:
            response = self._http.send(self._request(self._http, call), stream=True)
        except httpx2.HTTPError as exc:
            raise _transport_error(call, exc) from exc
        try:
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise _too_big(call)
                chunks.append(chunk)
        except httpx2.HTTPError as exc:
            raise _transport_error(call, exc) from exc
        finally:
            response.close()
        return read_envelope(call, response.status_code, response.headers, b"".join(chunks))

    def execute(self, call: Call[T]) -> T:
        """Sends a prepared call with the rate limit and retry rules. Mostly for internal use.

        The public methods check their arguments, describe the operation as a ``Call`` (made by
        the internal ``hidesay_bot._spec`` module) and hand it to this method. Before each try it
        waits for the rate limiter. A ``429`` answer is waited out as long as the server asks,
        up to ``retry.rate_limit_retries`` times; one that asks for more than
        ``retry.max_rate_limit_wait`` seconds is raised at once. A network error or a ``5xx``
        answer is tried again after a growing random wait, up to ``retry.attempts`` tries in
        all, but only for a call that is safe to repeat. When an earlier answer was lost, an
        error that means the first try went through counts as success, ``message_not_found``
        on a delete for example. Every wait blocks the calling thread.

        Args:
            call: The prepared call.

        Returns:
            The call's result, read from the server's answer.

        Raises:
            APIError: The server refused the call, or still failed when the retries ran out.
            NetworkError: No answer came, after the retries.
            PayloadError: The answer lacks something this version needs.
            ConfigurationError: The client was closed, or the ``http_client`` you passed in
                was.
        """
        attempts = Attempts(self.retry)
        while True:
            attempts.tries += 1
            if self.rate_limiter is not None:
                self.rate_limiter.acquire(
                    send=call.send, edit=call.edit, chat_id=call.chat_id, chat_kind=call.chat_kind
                )
            try:
                return call.parse(self._once(call))
            except APIError as exc:
                if attempts.settles(call, exc):
                    return call.done_value  # type: ignore[no-any-return]
                if isinstance(exc, RateLimitError):
                    self._penalize(call, exc)
                wait = attempts.wait_for(call, exc)
                if wait is None:
                    raise
                self._log_retry(call, exc, wait, attempts)
            except NetworkError as exc:
                wait = attempts.wait_for(call, exc)
                if wait is None:
                    raise
                self._log_retry(call, exc, wait, attempts)
            time.sleep(wait)

    # --- Me ---

    def get_me(self) -> BotInfo:
        """Fetches who the bot is, its mode, its status and the limits this server enforces.

        Calls ``GET /me``. It works while the bot is paused, so a program can find out why
        nothing else does. Retried after a network error or a ``5xx`` answer.

        Returns:
            The bot as the server holds it. ``limits`` types the limits this version knows, and
            `Limits.get` reads any other by its JSON name; ``queued`` counts the updates
            waiting.

        Example:
            Read the server's limits instead of hard-coding them::

                me = api.get_me()
                print(f"@{me.username}", me.mode, me.status, me.queued)
                print(me.limits.sends_per_group_per_minute)
                print(me.limits.get("botsPerChat"))     # a limit this version does not name
        """
        return self.execute(_spec.get_me())

    # --- Updates ---

    def get_updates(
        self, offset: int | None = None, limit: int | None = None, timeout: int | None = None
    ) -> list[Update]:
        """Fetches the updates waiting for the bot, and confirms the ones already handled.

        Calls ``GET /updates`` (long polling). Before sending, it checks that ``offset`` is an
        integer of at least 0, ``limit`` is between 1 and 100 and ``timeout`` between 0 and
        50. With a ``timeout``, the call blocks until an update arrives or the time is up, and
        the request may read for ``timeout + 15`` seconds, so the client does not hang up
        before the server answers. Not retried after a network error or a ``5xx`` answer: the
        polling loop decides what to do.

        Only one poll per bot runs at a time: a second call ends the first one with an empty
        list. Polling is refused while a webhook is set. `SyncBot.run` does all of this for
        you.

        Args:
            offset: Confirms every update with a lower ``update_id``: the server deletes them
                for good. Pass the highest ``update_id`` you handled, plus one. ``None``
                confirms nothing and gets everything still in the queue.
            limit: At most this many updates, 1 to 100. ``None`` leaves it to the server,
                which sends up to 100.
            timeout: Seconds the server may wait for an update to arrive, 0 to 50. ``None``
                or ``0`` answers at once.

        Returns:
            The updates, oldest first. Empty when nothing arrived before the timeout.

        Raises:
            ValidationError: ``offset``, ``limit`` or ``timeout`` is out of range.
            WebhookActiveError: A webhook is set. Delete it before polling.

        Example:
            Handle each update first, then confirm it with the next call::

                offset = None
                while True:
                    for update in api.get_updates(offset, timeout=50):
                        handle(update)
                        offset = update.update_id + 1

        Warning:
            Confirming is final: the server deletes the confirmed updates at the start of the
            request. Never pass an ``offset`` beyond what you have handled, or saved somewhere
            safe.
        """
        updates: list[Update] = self.execute(_spec.get_updates(offset, limit, timeout))
        return updates

    def iter_updates(self, *, offset: int | None = None, limit: int = 100, timeout: int = 50) -> Iterator[Update]:
        """Polls forever, yielding the updates one by one.

        Calls `get_updates` in a loop, with the same checks. A batch is confirmed only when the
        next one is asked for, that is after your loop body ran for every update in it. If you
        break out of the loop, or an exception leaves it, nothing of the current batch is
        confirmed: the next poll gets the whole batch again, the updates you already handled
        included. Updates come at least once, and none is lost.

        An error of `get_updates` ends the loop, a network error included, since polls are not
        retried. Catch it and start a new loop to go on.

        Args:
            offset: The ``offset`` of the first poll. ``None`` starts with everything still in
                the queue.
            limit: At most this many updates per poll, 1 to 100.
            timeout: Seconds each poll may wait for an update to arrive, 0 to 50.

        Yields:
            Each update, oldest first.

        Raises:
            ValidationError: ``offset``, ``limit`` or ``timeout`` is out of range. It is raised
                when the loop starts, not when the method is called.
            WebhookActiveError: A webhook is set.

        Example:
            Answer every message of a readable chat::

                for update in api.iter_updates():
                    if update.message is not None:
                        api.send_message(update.message.chat.id, "pong")
        """
        while True:
            batch = self.get_updates(offset, limit, timeout)
            for update in batch:
                yield update
                offset = update.update_id + 1

    def get_webhook(self) -> WebhookInfo:
        """Fetches the webhook, if one is set, and how its deliveries are going.

        Calls ``GET /webhook``. It works while the bot is paused. Retried after a network error
        or a ``5xx`` answer.

        Returns:
            The webhook state. ``url`` is ``None`` when no webhook is set, ``pending`` counts
            the updates waiting, and ``failures``, ``last_error`` and ``next_attempt_at`` tell
            how the deliveries fail, when they do.

        Example:
            See whether the deliveries are failing::

                info = api.get_webhook()
                if info.failures:
                    print(f"{info.failures} failures in a row, last: {info.last_error}")
        """
        return self.execute(_spec.get_webhook())

    def set_webhook(self, url: str, secret: str | None = None) -> WebhookInfo:
        """Sets a webhook: from now on, every update is POSTed to ``url``.

        Calls ``POST /webhook``. Before sending, it checks that ``url`` is an absolute ``http``
        or ``https`` URL of at most 1024 characters, with a host and without a user name, a
        password or a fragment, and that ``secret`` is 1 to 256 characters of ``A-Z``,
        ``a-z``, ``0-9``, ``_`` and ``-``. The server itself refuses plain ``http`` and
        addresses that are not public. Retried after a network error or a ``5xx`` answer.

        Setting a webhook ends any poll in progress, and `get_updates` is refused while it is
        set. `SyncBot.create_webhook_app` can register the webhook for you.

        Args:
            url: The address the server delivers the updates to.
            secret: Sent back on every delivery in the ``X-Hidesay-Bot-Api-Secret-Token``
                header, so your endpoint can refuse anything without it. ``None`` sets no
                secret.

        Returns:
            The webhook as the server saved it.

        Raises:
            ValidationError: ``url`` or ``secret`` breaks one of the rules above.
            BadWebhookError: The server refused the URL, for example because it resolves to
                a private address.

        Example:
            A webhook with a secret::

                from hidesay_bot import generate_secret

                secret = generate_secret()
                api.set_webhook("https://bot.example.org/hidesay", secret)

        Warning:
            Always set a secret: without one, anyone who finds the URL can send fake updates.
        """
        return self.execute(_spec.set_webhook(url, secret))

    def delete_webhook(self) -> WebhookInfo:
        """Removes the webhook: the bot goes back to polling.

        Calls ``DELETE /webhook``. The updates that queued while the webhook was set stay, for
        the next poll. It works while the bot is paused. Retried after a network error or a
        ``5xx`` answer.

        Returns:
            The webhook state after the change. ``url`` is ``None``, and ``pending`` counts the
            updates waiting.

        Example:
            Switch from a webhook to polling::

                info = api.delete_webhook()
                print(info.pending, "updates waiting for the next poll")
        """
        return self.execute(_spec.delete_webhook())

    # --- Key ---

    def set_key(self, public_key: str, fingerprint: str) -> PublishedKey:
        """Publishes the bot's OpenPGP public key: the bot becomes protected.

        Calls ``PUT /key``. From then on the bot's private chats are end-to-end encrypted:
        people's phones seal every message to this key, and the bot must send ``armored`` into
        those chats. Calling it again with another key is a rotation; every phone learns the
        new fingerprint at once. Retried after a network error or a ``5xx`` answer.

        Before sending, it checks that ``public_key`` is an armored public key block of at most
        65536 characters, and refuses anything that holds a private key, so a private key never
        leaves the process. The fingerprint travels as its own field, so it has to match the key:
        `Bot.publish_key`, which `SyncBot` has too, computes it from the key itself, and
        refuses v6 keys, which the phones cannot use.

        Args:
            public_key: The armored public key block, as `BotKey.export_public` writes it.
            fingerprint: The key's fingerprint, 40 (v4) or 64 (v6) hex digits with no spaces
                in between. It is sent in upper case.

        Returns:
            The fingerprint the server saved, and the bot's mode, now ``protected``.

        Raises:
            ValidationError: ``public_key`` is not an armored public key block, is too long or
                holds a private key, or ``fingerprint`` is not 40 or 64 hex digits.

        Example:
            Publish the public half of the key the bot decrypts with::

                key = BotKey.from_file("bot-key.asc", passphrase)
                api.set_key(key.export_public(), key.fingerprint)

        Warning:
            There is no API to remove the key: only the bot's owner can make the bot open
            again, from the console. A wrong fingerprint makes the phones seal messages to a key
            the bot does not have.
        """
        return self.execute(_spec.set_key(public_key, fingerprint))

    # --- Commands ---

    @overload
    def get_commands(self, scope: None = None) -> list[CommandScope]: ...
    @overload
    def get_commands(self, scope: str) -> list[Command]: ...
    def get_commands(self, scope: str | None = None) -> list[CommandScope] | list[Command]:
        """Fetches the command menus: every scope, or one.

        Calls ``GET /commands``. Before sending, it checks that ``scope`` is one the server
        knows. Retried after a network error or a ``5xx`` answer.

        Args:
            scope: ``None`` for every scope with its list. Or one scope: ``"default"``,
                ``"private"``, ``"groups"``, ``"groupAdmins"``, or ``"chat:<id>"`` (``"chat:G48213907"``) for one chat
                the bot is in.

        Returns:
            Every scope with its commands, as a list of `CommandScope`, when ``scope`` is
            ``None``. Otherwise that scope's commands, as a list of `Command`.

        Raises:
            ValidationError: ``scope`` is not one of the above.

        Example:
            All the menus, then one::

                for scope in api.get_commands():
                    print(scope.scope, [c.command for c in scope.commands])

                private = api.get_commands("private")
        """
        result: list[CommandScope] | list[Command] = self.execute(_spec.get_commands(scope))
        return result

    def set_commands(
        self, commands: Iterable[Command | Mapping[str, Any] | tuple[str, str]], scope: str | None = None
    ) -> CommandScope:
        """Replaces the whole command list of one scope.

        Calls ``PUT /commands``. Before sending, each command is cleaned up the way the server
        does it: the name is trimmed, loses a leading ``/`` and is put in lower case, and must
        then be 1 to 32 letters, digits and underscores; in the description every run of white
        space becomes one space, and it must then be 1 to 256 characters, with no control
        characters. A name given twice keeps its first description, as on the server. Retried
        after a network error or a ``5xx`` answer.

        Scopes go from narrow to wide: in a private chat ``private``, then ``default``; in a
        group ``chat:<id>``, then ``groupAdmins`` for admins, then ``groups``, then
        ``default``. An empty list does not hide a wider scope. The owner can edit the same
        lists: whoever writes last wins.

        Args:
            commands: Up to 100 commands. Each is a `Command`, a dict with ``"command"`` and
                ``"description"``, or a ``(name, description)`` pair.
            scope: Which list: ``"default"``, ``"private"``, ``"groups"``, ``"groupAdmins"``
                or ``"chat:<id>"`` (``"chat:G48213907"``). ``None`` means ``"default"``.

        Returns:
            The scope and its commands, as the server saved them.

        Raises:
            ValidationError: More than 100 commands, a command that breaks the rules above, or
                an unknown ``scope``.
            ChatNotFoundError: A ``"chat:<id>"`` (``"chat:G48213907"``) scope names a chat the bot is not in.

        Example:
            A menu for everyone, and one for the admins of groups::

                api.set_commands([("start", "Say hello"), ("help", "What I can do")])
                api.set_commands([("ban", "Ban the replied user")], scope="groupAdmins")
        """
        return self.execute(_spec.set_commands(commands, scope))

    def delete_commands(self, scope: str | None = None) -> CommandScope:
        """Empties the command list of one scope.

        Calls ``DELETE /commands``. Before sending, it checks that ``scope`` is one the server
        knows. An empty list does not hide a wider scope. Retried after a network error or a
        ``5xx`` answer.

        Args:
            scope: Which list: ``"default"``, ``"private"``, ``"groups"``, ``"groupAdmins"``
                or ``"chat:<id>"`` (``"chat:G48213907"``). ``None`` means ``"default"``.

        Returns:
            The scope, now with no commands.

        Raises:
            ValidationError: ``scope`` is not one of the above.
        """
        return self.execute(_spec.delete_commands(scope))

    # --- Messages ---

    def send_message(
        self,
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
    ) -> SentMessageBase:
        """Sends a message: ``text`` into a readable chat, ``armored`` into an encrypted one.

        Calls ``POST /messages``. Give exactly one of ``text`` and ``armored``; `get_chat` tells
        which one a chat wants (its ``mode``). The client does not encrypt: in an encrypted
        chat, seal the message to every key of `get_chat_keys` and to the bot's own key, and
        sign it. `BotKey.seal` adds the bot's key and the signature for you.

        Into a readable chat it also sends files (``media``, uploaded first with `upload_file`,
        ``text`` being the caption) or a ``sticker``; the server writes the envelope the phones
        read. In an encrypted chat files and stickers travel inside the armor:
        `SyncBot.send_media` and `SyncBot.send_sticker` do it.

        Before sending, it checks what the server would refuse:

        - the chat is an ID like ``"G48213907"``, ``"C48213907"`` or a person's
          ``"48213907"``, and ``reply_to`` a positive integer;
        - ``text`` is not blank, has at most 4096 UTF-16 code units, and no control characters
          but newline and tab (carriage returns become newlines first); as a caption it may be
          blank, and is then left out;
        - ``armored`` holds ``-----BEGIN PGP MESSAGE-----`` and has at most 131072 characters;
        - ``media`` holds 1 to 10 files described as the server's envelope wants them, and an
          album of more than one only pictures and videos;
        - a ``sticker`` comes alone, without text;
        - ``markup`` follows the rules of `InlineKeyboard`;
        - ``mentions`` are at most 20 user IDs;
        - ``send_id``, when given, is a UUID.

        The client paces the sends: by default one a second into a private chat, 20 a minute
        into a group or channel, 30 a second in all. A send that must wait its turn blocks the
        thread until then.

        Retried after a network error or a ``5xx`` answer. The ``sendId`` makes this safe: a
        repeat hands back the message already written instead of writing it twice.

        Args:
            chat_id: A chat the bot is in.
            text: The message, for a readable chat.
            armored: An ASCII-armored OpenPGP message, for an encrypted chat.
            reply_to: The ID of a message of the same chat to quote. The server drops a wrong
                ID without an error.
            markup: Buttons under the message: an `InlineKeyboard`, or its JSON form
                ``{"inlineKeyboard": [...]}``. Buttons are never encrypted.
            silent: Channels only: ``True`` posts without a notification. ``None`` leaves it
                to the server, which notifies.
            send_id: A UUID that names this send, as a string or a `uuid.UUID`. ``None`` makes
                a new one. Pass your own to repeat the send safely later, after a crash for
                example.
            media: Readable chats: the files to send, one or an album of 2 to 10 pictures and
                clips, each a `MediaIn` or a dict in the API's form, with the ID `upload_file`
                gave. ``text`` is the caption.
            sticker: Readable chats: a sticker, as its ID, a `Sticker` or a dict with ``id``
                and ``mediaId``. The file ID proves the bot was shown a sticker of a pack that
                is not public.
            mentions: Groups: the IDs of the people the words tag (``@[Anna](u48213907)``), up
                to 20, so they are told even with the group muted, as for a person's tag.
                Somebody not in the group is dropped.
            chat_kind: The `ChatKind` of the chat, for the rate limiter only: groups and
                channels are paced per minute. ``None`` paces it like a private chat.

        Returns:
            The message as the server stored it, with its ``id`` and ``date``.

        Raises:
            ValidationError: An argument breaks one of the rules above, or both or neither of
                ``text`` and ``armored`` were given.
            WrongBodyKindError: ``text`` into an encrypted chat, or ``armored`` into a readable
                one, or files or a sticker into an encrypted one. ``expected`` says which one
                the chat wants.
            BadMediaError: A file is not one this bot uploaded, or the server refused how it is
                described.
            StickerNotFoundError: No such sticker, or one the bot may not send.
            BadMarkupError: The server refused the keyboard.
            ForbiddenError: The bot may not write in this chat, or send files or voice notes in
                this group.
            BlockedByUserError: The person blocked the bot.
            UserGoneError: The person closed their account.
            ChatNotFoundError: No such chat, or the bot is not in it.
            SendIdConflictError: This ``send_id`` was already used in another chat.
            FloodWaitError: Too many messages into this chat or in all, and the server asked
                for a longer wait, or more waits, than the `RetryPolicy` allows.
            SlowModeError: The group's slow mode, with the same limits on waiting.

        Examples:
            Into a readable chat::

                sent = api.send_message("48213907", "Backup finished.")
                print(sent.id, sent.date)

            A picture, uploaded first::

                up = api.upload_file(jpeg)
                media = MediaIn(id=up.id, kind="image", mime="image/jpeg", width=1280, height=720)
                api.send_message(chat_id, "The view from here", media=[media])

            Into an encrypted group, sealed by hand::

                keys = api.get_chat_keys(chat_id)
                recipients = [PublicKey.from_user_key(k) for k in keys.keys]
                armored = bot_key.seal("Backup finished.", recipients)
                api.send_message(chat_id, armored=armored, chat_kind=ChatKind.GROUP)
        """
        return self.execute(
            _spec.send_message(
                chat_id,
                text,
                armored=armored,
                reply_to=reply_to,
                markup=markup,
                silent=silent,
                send_id=send_id,
                media=media,
                sticker=sticker,
                mentions=mentions,
                chat_kind=chat_kind,
            )
        )

    def edit_message(
        self,
        chat_id: str | int,
        message_id: int,
        text: str | None = None,
        *,
        armored: str | None = None,
        markup: InlineKeyboard | Mapping[str, Any] | None = KEEP,
        chat_kind: ChatKind | None = None,
    ) -> SentMessageBase:
        """Changes the text, the buttons, or both, of one of the bot's own messages.

        Calls ``PATCH /messages/{messageId}``. The bot edits its own ordinary messages, at any
        age, and people see the edit live. In a channel where it may edit posts it edits
        anybody's post too, the words only: buttons are only ever the sender's. On a file or an
        album of a readable chat, ``text`` is the new caption and the files stay. The new body
        follows the rules of `send_message`: ``text`` in a readable chat, ``armored`` in an
        encrypted one. Before
        sending, it checks the IDs, the body and the keyboard, and that there is something to
        change. Edits are paced with the same numbers as sends, counted on their own; the 30 a
        second in all is shared with sends. Retried after a network error or a ``5xx`` answer:
        the same edit twice gives the same result.

        Args:
            chat_id: The chat the message is in.
            message_id: The message to change.
            text: The new text, for a readable chat.
            armored: The new armored OpenPGP message, for an encrypted chat.
            markup: `KEEP` (the default) keeps the buttons, ``None`` or an empty keyboard
                removes them, and a keyboard replaces them.
            chat_kind: The `ChatKind` of the chat, for the rate limiter only, as in
                `send_message`.

        Returns:
            The message after the edit.

        Raises:
            ValidationError: An argument breaks one of the rules, both ``text`` and
                ``armored`` were given, or there is nothing to change: no ``text``, no
                ``armored``, and ``markup`` left at `KEEP`.
            WrongBodyKindError: ``text`` into an encrypted chat, or ``armored`` into a readable
                one. ``expected`` says which one the chat wants.
            BadMarkupError: The server refused the keyboard.
            ForbiddenError: Not the bot's message (nor a channel post it may edit), not an
                ordinary one (a service notice), or new buttons on somebody else's post.
            BlockedByUserError: In a private chat, the person blocked the bot.
            UserGoneError: In a private chat, the person closed their account.
            ChatNotFoundError: No such chat, or the bot is not in it.
            MessageNotFoundError: No such message.

        Example:
            New text, then the buttons removed and the text kept::

                api.edit_message(chat_id, sent.id, "Backup finished: 14 GB.")
                api.edit_message(chat_id, sent.id, markup=None)
        """
        return self.execute(
            _spec.edit_message(chat_id, message_id, text, armored=armored, markup=markup, chat_kind=chat_kind)
        )

    def delete_message(self, chat_id: str | int, message_id: int) -> bool:
        """Deletes a message for everyone, for good.

        Calls ``DELETE /messages/{messageId}``, with ``chatId`` in the query. The bot deletes
        any message of a private chat, as either person can there; in a group or channel its
        own, anybody's where it has the *delete messages* right, and anybody's comment with the
        *manage comments* right. A post takes its comments with it. Only messages the bot was
        handed; secret messages and service notices cannot be deleted. Before sending, it
        checks the chat and the message ID. `delete_messages` deletes up to 100 at once.

        Retried after a network error or a ``5xx`` answer. If an earlier answer was lost and
        the retry gets ``message_not_found``, the first try did delete the message, and the
        method returns ``True``. When no answer was lost, a missing message raises
        `MessageNotFoundError`.

        Args:
            chat_id: The chat the message is in.
            message_id: The message to delete.

        Returns:
            ``True`` once the message is deleted. ``False`` in a rare race on the server, when
            the message disappeared between its lookup and the delete.

        Raises:
            ForbiddenError: The bot may not delete this message.
            ChatNotFoundError: No such chat, or the bot is not in it.
            MessageNotFoundError: No such message, or it is already gone.
        """
        return self.execute(_spec.delete_message(chat_id, message_id))

    # --- Buttons ---

    def answer_callback(
        self, callback_id: str, text: str | None = None, *, alert: bool = False, url: str | None = None
    ) -> bool:
        """Answers a button press: stops the spinner on the person's phone.

        Calls ``POST /callbacks/{callbackId}/answer``. Answer within 60 seconds of the press;
        each press can be answered once. The message itself changes only if you edit it
        (`edit_message`). Before sending, it checks ``callback_id``, ``text`` and ``url``.

        Retried after a network error or a ``5xx`` answer. If an earlier answer was lost and
        the retry gets ``callback_answered``, the first try worked, and the method returns
        ``True``.

        Args:
            callback_id: The ID of the press, the ``id`` of a `CallbackQuery`.
            text: A short line shown over the chat, or in a box with ``alert``. Up to 200
                characters on one line, with no control characters. Spaces at the ends are
                trimmed, and an empty text is left out.
            alert: Shows ``text`` in a box the person has to close, instead of a short line.
            url: An address the phone shows and offers to open: an absolute ``http`` or
                ``https`` URL of up to 1024 characters, without a user name or password. An
                empty string is left out.

        Returns:
            ``True`` once the answer reached the phone that pressed.

        Raises:
            ValidationError: ``callback_id`` is not the ID of a press, or ``text`` or ``url``
                breaks one of the rules above.
            CallbackNotFoundError: No such press for this bot.
            CallbackAnsweredError: The press was answered already.
            CallbackExpiredError: More than 60 seconds went by since the press.

        Example:
            Just stop the spinner, say a word, or show a box::

                api.answer_callback(query.id)
                api.answer_callback(query.id, "Saved")
                api.answer_callback(query.id, "Sold out", alert=True)

        Warning:
            Answers are not encrypted, even in an encrypted chat.
        """
        return self.execute(_spec.answer_callback(callback_id, text, alert=alert, url=url))

    # --- Chats ---

    def get_chat(self, chat_id: str | int) -> Chat:
        """Fetches a chat the bot is in: its kind, its mode and, in a group, the bot's seat.

        Calls ``GET /chats/{chatId}``. ``mode`` tells which body to send: ``text`` in an
        ``open`` chat, ``armored`` in a ``protected`` one. A private chat also carries the
        other person and whether they blocked the bot. Before sending, it checks the shape of
        ``chat_id``. Retried after a network error or a ``5xx`` answer.

        Args:
            chat_id: The chat.

        Returns:
            The chat as the server describes it.

        Raises:
            ChatNotFoundError: No such chat, or the bot is not in it.

        Example:
            Find out which body a chat wants::

                chat = api.get_chat(chat_id)
                body = "armored" if chat.protected else "text"
                print(chat.kind, chat.title, body)
        """
        return self.execute(_spec.get_chat(chat_id))

    def get_chat_keys(self, chat_id: str | int) -> ChatKeys:
        """Fetches the public keys a message into an encrypted chat must be sealed to.

        Calls ``GET /chats/{chatId}/keys``. The answer holds the keys of the people in the
        chat, IDs and keys only, no names. The bot's own key is not in it: add it yourself
        (`BotKey.seal` does). Other bots that only receive what is addressed to them are left
        out, as they are for the app. Before sending, it checks the shape of ``chat_id``.
        Retried after a network error or a ``5xx`` answer.

        Args:
            chat_id: An encrypted chat the bot is in.

        Returns:
            The keys, and in a group ``member_count``, which is larger than the number of keys
            when some members have none.

        Raises:
            ChatIsOpenError: The chat is readable: send ``text``, no keys are needed.
            ChatNotFoundError: No such chat, or the bot is not in it.
            NoUserKeyError: A private chat whose person has no key.

        Example:
            Read the keys, checking each one against the fingerprint the server lists::

                keys = api.get_chat_keys(chat_id)
                recipients = [PublicKey.from_user_key(k) for k in keys.keys]

        Note:
            The keys come from the server, and a server that lies could hand out a key of its
            own. Key pinning (``pin_keys=True`` on `SyncBot`) notices a key that changes later.
        """
        return self.execute(_spec.get_chat_keys(chat_id))

    def get_chat_members(self, chat_id: str | int) -> ChatMembers:
        """Fetches the members of a group or channel, as a member in the bot's seat sees them.

        Calls ``GET /chats/{chatId}/members``. Where the member list is hidden, a bot that is
        not an admin sees the admins only, and a channel that keeps its admins to itself
        leaves them out. Names and handles only, never phone numbers. Before sending, it checks
        the shape of ``chat_id``. Retried after a network error or a ``5xx`` answer.

        Args:
            chat_id: A group or channel the bot is in.

        Returns:
            The members with their seats, and ``member_count``, which counts the list as
            returned.

        Raises:
            BadRequestError: The chat is private, so it has no member list.
            ChatNotFoundError: No such chat, or the bot is not in it.

        Example:
            The handles of the admins::

                roster = api.get_chat_members(group_id)
                admins = [m.user.username for m in roster.members if m.is_admin]
        """
        return self.execute(_spec.get_chat_members(chat_id))

    def get_chat_member(self, chat_id: str | int, user_id: str | int) -> Member:
        """Fetches one member's seat in a group or channel.

        Calls ``GET /chats/{chatId}/members/{userId}``. Before sending, it checks the shape of
        both IDs. Retried after a network error or a ``5xx`` answer.

        Args:
            chat_id: A group or channel the bot is in.
            user_id: The person.

        Returns:
            The person and their seat (status, admin rights, title).

        Raises:
            BadRequestError: The chat is private, so it has no members.
            ChatNotFoundError: No such chat, or the bot is not in it.
            MemberNotFoundError: The person is not a member, or a hidden member list keeps them
                from the bot.

        Example:
            Check a right before relying on it::

                member = api.get_chat_member(group_id, user_id)
                if member.can is not None and member.can.ban_users:
                    ...
        """
        return self.execute(_spec.get_chat_member(chat_id, user_id))

    def leave_chat(self, chat_id: str | int) -> bool:
        """Leaves a group or channel.

        Calls ``POST /chats/{chatId}/leave``. The chat is told, the way it would be about a
        person. A bot cannot leave a private chat; the person can block it. Before sending, it
        checks the shape of ``chat_id``.

        Retried after a network error or a ``5xx`` answer. If an earlier answer was lost and
        the retry finds no chat, the first try worked, and the method returns ``True``.

        Args:
            chat_id: The group or channel.

        Returns:
            ``True`` once the bot has left.

        Raises:
            BadRequestError: The chat is private and cannot be left.
            ChatNotFoundError: No such chat, or the bot is not in it.
        """
        return self.execute(_spec.leave_chat(chat_id))

    def send_typing(self, chat_id: str | int, typing: bool = True) -> bool:
        """Shows "typing..." in a chat for a few seconds, or clears it.

        Calls ``POST /chats/{chatId}/typing``. The indicator clears on its own after a few
        seconds, or when a message lands. One goes out per chat every 3 seconds at most; calls
        in between show nothing and return ``False``. Before sending, it checks the shape of
        ``chat_id``. Not retried after a network error or a ``5xx`` answer: a late indicator is
        useless.

        Args:
            chat_id: A chat the bot may write in.
            typing: ``False`` clears the indicator early.

        Returns:
            ``True`` if the indicator went out, ``False`` if one went out to this chat less
            than 3 seconds ago.

        Raises:
            ForbiddenError: The bot may not write in this chat: it is restricted, or only a
                subscriber of a channel.
            ChatNotFoundError: No such chat, or the bot is not in it.

        Example:
            Show it while the bot works::

                api.send_typing(chat_id)
                report = build_report()
                api.send_message(chat_id, report)
        """
        return self.execute(_spec.send_typing(chat_id, typing))

    def resolve(self, username: str) -> Resolved:
        """Turns a public handle into an ID.

        Calls ``GET /resolve/{username}``. For the handle somebody types at the bot - "paga
        @anna", "segui @notizie" - when no message has handed that ID over. People, groups and
        channels share one pool of names, so a handle belongs to exactly one of the three and
        `Resolved.kind` says which; `Resolved.id` is filled in either way. A leading ``@`` is
        stripped and the case does not matter. Before sending, it checks the handle against the
        server's own rule. Retried after a network error or a ``5xx`` answer.

        It finds anybody with a public handle, which is no more than the short link already
        shows a stranger with a browser. The ID on its own unlocks nothing: sending still needs
        the bot to be in the chat, and `Client.get_user_key` still needs somebody the bot may look up.

        Args:
            username: The handle, with or without the ``@``.

        Returns:
            What the handle turned out to be.

        Raises:
            ValidationError: Not a handle: 3-32 characters, letters, digits and underscore.
            UsernameNotFoundError: Nobody and no chat answers to it. Also the answer for a
                group with no public name and for a closed account.
            FloodWaitError: More than 60 lookups this minute, where a handle nobody holds
                counts as five. ``retry_after`` says how long to wait.

        Example:
            Pay whoever was named::

                found = api.resolve(words.removeprefix("paga @"))
                if found.is_user:
                    api.send_message(chat_id, f"{found.mention} ha id {found.id}")
        """
        return self.execute(_spec.resolve(username))

    def get_user_key(self, user_id: str | int) -> UserKey:
        """Fetches one person's public key.

        Calls ``GET /users/{userId}/key``. It works only for someone the bot may look up,
        as for ``get_user``: anyone else is `UserNotFoundError`, whether or not they exist. Before sending, it
        checks the shape of ``user_id``. Retried after a network error or a ``5xx`` answer.

        Args:
            user_id: The person.

        Returns:
            The person's ID, the key's fingerprint as the server lists it, and the armored
            public key.

        Raises:
            UserNotFoundError: No such person, or nobody the bot may look up.
            NoUserKeyError: The person never published a key.

        Example:
            Read the key, and show its fingerprint the way people compare it::

                user_key = api.get_user_key("48213907")
                anna = PublicKey.from_user_key(user_key)
                print(format_fingerprint(anna.fingerprint))
        """
        return self.execute(_spec.get_user_key(user_id))

    # --- Token ---

    def revoke_token(self) -> bool:
        """Burns this token, for good.

        Calls ``POST /token/revoke``. Nothing works again, this client included, until the
        owner makes a new token in the console; the owner is told at once. It works while the
        bot is paused.

        Retried after a network error or a ``5xx`` answer. If an earlier answer was lost and
        the retry gets ``unauthorized``, the first try worked, and the method returns ``True``.

        Returns:
            ``True`` once the token is revoked.

        Example:
            For a program that finds out its token leaked::

                if token_was_leaked:
                    api.revoke_token()

        Warning:
            This cannot be undone. Every later call with this token raises
            `AuthenticationError`.
        """
        return self.execute(_spec.revoke_token())

    def get_web_bot_key(self) -> str:
        """Fetches the key that checks the ``initData`` of a Web-Bot page.

        Calls ``GET /webbot/key``. The key is public and the same for every bot, so it can be
        fetched once and kept in the bot's configuration. Retried after a network error or a
        ``5xx`` answer.

        Returns:
            The server's Ed25519 public key: its 32 bytes in base64url, the form
            `~hidesay_bot.validate_web_bot_data` takes.

        Example:
            Fetch it once, then check every request offline::

                key = api.get_web_bot_key()
                me = api.get_me()
                data = validate_web_bot_data(init_data, bot_id=me.id, public_key=key)
        """
        return self.execute(_spec.get_web_bot_key())

    # --- more on messages ---

    def delete_messages(self, chat_id: str | int, message_ids: Iterable[int]) -> list[int]:
        """Deletes up to 100 messages of one chat at once, for everyone, for good.

        Calls ``POST /messages/delete``, with the rules of `delete_message` for each message:
        anything in a private chat; in a group or channel the bot's own, anybody's with the
        *delete messages* right, anybody's comment with the *manage comments* right. A post
        takes its comments with it. IDs that are not of this chat, or that the bot was never
        handed, are left out. If one message is not the bot's to delete, nothing is deleted.
        Before sending, it checks the chat and that there are 1 to 100 positive IDs. Retried
        after a network error or a ``5xx`` answer: a repeat leaves out what is already gone.

        Args:
            chat_id: The chat the messages are in.
            message_ids: The messages, 1 to 100 IDs.

        Returns:
            The IDs that went, the comments taken with their posts included.

        Raises:
            ValidationError: No IDs, more than 100, or one that is not a positive integer.
            ForbiddenError: One of the messages is not the bot's to delete. Nothing was deleted.
            ChatNotFoundError: No such chat, or the bot is not in it.

        Example:
            Clear what a flood left behind::

                gone = api.delete_messages("G48213907", [90213, 90214, 90215])
        """
        return self.execute(_spec.delete_messages(chat_id, message_ids))

    def forward_message(
        self,
        chat_id: str | int,
        from_chat_id: str | int,
        message_id: int,
        *,
        hide_sender: bool | None = None,
        silent: bool | None = None,
        send_id: str | uuid.UUID | None = None,
        chat_kind: ChatKind | None = None,
    ) -> SentMessageBase:
        """Sends a message the bot was handed on into another chat it is in.

        Calls ``POST /messages/forward``. The forward carries "forwarded from" over it: the
        person, or the channel and its post. A forward of a forward keeps the first origin.
        Files go along: the server copies them for the bot, so the forward outlives the
        original. A chat that protects its content does not let its messages out, and
        comments are not forwarded.

        The server forwards only from a readable chat into a readable chat. A message from, or
        into, an end-to-end encrypted chat answers `ChatIsProtectedError`: the bot has to open
        it and seal it again itself, which is what `SyncBot.forward_message` does.

        Like a send it carries a ``sendId``, so it is retried after a network error or a
        ``5xx`` answer without forwarding twice, and it is paced like a send into the target.

        Args:
            chat_id: Where to forward it.
            from_chat_id: The chat the message is in.
            message_id: The message.
            hide_sender: ``True`` sends a plain copy, without "forwarded from".
            silent: Channels only: ``True`` posts without a notification.
            send_id: A UUID that names this forward. ``None`` makes a new one.
            chat_kind: The `ChatKind` of the target, for the rate limiter only. ``None`` reads
                it from the ID.

        Returns:
            The forward, as the server stored it.

        Raises:
            ChatIsProtectedError: One of the two chats is end-to-end encrypted, or the message
                is.
            ForbiddenError: The bot may not write in the target, or the source protects its
                content.
            BlockedByUserError: The person on the other side of the target blocked the bot.
            ChatNotFoundError: One of the chats does not exist, or the bot is not in it.
            MessageNotFoundError: No such message, or one the bot was never handed.
            BadRequestError: The message is a comment.
        """
        return self.execute(
            _spec.forward_message(
                chat_id,
                from_chat_id,
                message_id,
                hide_sender=hide_sender,
                silent=silent,
                send_id=send_id,
                chat_kind=chat_kind,
            )
        )

    def set_reaction(self, chat_id: str | int, message_id: int, emoji: str) -> MessageReactions:
        """Puts the bot's reaction on a message, in place of the one it had there.

        Calls ``PUT /messages/{messageId}/reaction``. One reaction per message, as for everybody.
        A custom emoji goes as the app writes it. The rules of a person's reaction hold: in a
        private chat nobody blocked anybody; in a group or channel reactions are on, the emoji
        is one the chat takes (`Chat.allowed_reactions`) and the bot may react. Only messages
        the bot was handed. Before sending, it checks that ``emoji`` is 1 to 48 characters once
        trimmed. Retried after a network error or a ``5xx`` answer: the same reaction twice is
        one reaction.

        Args:
            chat_id: The chat the message is in.
            message_id: The message.
            emoji: The emoji.

        Returns:
            Every reaction on the message now, the bot's marked ``mine``.

        Raises:
            ValidationError: ``emoji`` is empty or too long.
            ForbiddenError: Reactions are off, the chat does not take this emoji, or the bot
                may not react.
            BlockedByUserError: In a private chat, the person blocked the bot.
            MessageNotFoundError: No such message, or one the bot was never handed.

        Example:
            Say "seen" without writing::

                api.set_reaction(chat_id, message_id, "👀")
        """
        return self.execute(_spec.set_reaction(chat_id, message_id, emoji))

    def remove_reaction(self, chat_id: str | int, message_id: int) -> bool:
        """Takes the bot's reaction off a message.

        Calls ``DELETE /messages/{messageId}/reaction``, with ``chatId`` in the query. Retried
        after a network error or a ``5xx`` answer.

        Args:
            chat_id: The chat the message is in.
            message_id: The message.

        Returns:
            ``True`` when the bot had a reaction there, ``False`` when it had none.

        Raises:
            BlockedByUserError: In a private chat, the person blocked the bot.
            MessageNotFoundError: No such message, or one the bot was never handed.
        """
        return self.execute(_spec.remove_reaction(chat_id, message_id))

    # --- files and stickers ---

    def upload_file(self, data: bytes, *, limit: int = UPLOAD_MAX) -> UploadedFile:
        """Uploads a file for a message.

        Calls ``POST /attachments``, as ``multipart/form-data`` with the bytes in a field called
        ``file``. The server takes up to 50 MB, 30 uploads a minute. The file goes up as it is,
        under a neutral name and type: say what it is when you send it.

        Into a readable chat, upload the file as it is and pass its ID in the ``media`` of
        `send_message`. Into an encrypted chat, upload it already sealed to the chat's keys and
        the bot's own, and put its ID in the envelope inside the ``armored`` body.
        `SyncBot.send_media` does either. Retried after a network error or a ``5xx`` answer; a lost
        answer leaves an extra copy on the server that no message points at.

        Args:
            data: The file's bytes.
            limit: The largest file accepted, in bytes. 50 MiB by default, the server's limit
                (`Limits.upload_bytes_max`).

        Returns:
            The file's ID, its address and the size the server stored.

        Raises:
            ValidationError: ``data`` is not bytes, or is larger than ``limit``.
            PayloadTooLargeError: The server's limit is lower.
            FloodWaitError: More than 30 uploads this minute.
            ServerError: ``try_again``: the server is short of disk right now.
        """
        return self.execute(_spec.upload_file(data, limit=limit))

    def get_sticker(self, sticker_id: int, media_id: str | None = None) -> Sticker:
        """Fetches a sticker the bot may send.

        Calls ``GET /stickers/{stickerId}``. A sticker of a public pack, or one the bot was
        shown: pass the ``media_id`` a message carried for it as proof. For a readable chat,
        `send_message` needs only the ID; for an encrypted chat this is what goes in the
        envelope. Retried after a network error or a ``5xx`` answer.

        Args:
            sticker_id: The sticker's ID.
            media_id: The sticker's file ID, as a message carried it, for a pack that is not
                public.

        Returns:
            The sticker.

        Raises:
            StickerNotFoundError: No such sticker, or one of a pack the bot can't use.
        """
        return self.execute(_spec.get_sticker(sticker_id, media_id))

    # --- chats and history ---

    def get_chats(self) -> list[Chat]:
        """Lists the groups and channels the bot is in.

        Calls ``GET /chats``. Each one as `get_chat` describes it, the bot's seat included.
        Private chats are not listed: they are the people who wrote to the bot. Retried after
        a network error or a ``5xx`` answer.

        Returns:
            The groups and channels.

        Example:
            Where the bot may not read everything::

                for chat in api.get_chats():
                    if not chat.reads_all:
                        print(chat.title, "only sends me what is meant for me")
        """
        chats: list[Chat] = self.execute(_spec.get_chats())
        return chats

    def get_history(
        self, chat_id: str | int, *, before: int | None = None, limit: int | None = None
    ) -> list[MessageBase]:
        """Fetches what the bot was handed in a chat, newest first.

        Calls ``GET /chats/{chatId}/messages``. In a private chat that is every message of it.
        In a group or channel it is the bot's own messages and the ones the room delivered to
        it, and nothing else: what it was not let read stays out, even after the room lets it
        read everything. Comments are not listed here, see `get_comments`. The messages come
        as the server keeps them: in an encrypted chat ``armored``, which `SyncBot.get_history`
        decrypts. Retried after a network error or a ``5xx`` answer.

        Args:
            chat_id: The chat.
            before: The oldest ID you hold, for the next page back. ``None`` for the newest.
            limit: How many, 1 to 100. ``None`` leaves it to the server, which gives 50.

        Returns:
            The messages, newest first. Empty when there are none.

        Raises:
            ValidationError: ``before`` or ``limit`` out of range.
            ChatNotFoundError: No such chat, or the bot is not in it.

        Example:
            Every page, back to the start::

                page = api.get_history(chat_id, limit=100)
                while page:
                    store(page)
                    page = api.get_history(chat_id, before=page[-1].id, limit=100)
        """
        messages: list[MessageBase] = self.execute(_spec.get_history(chat_id, before=before, limit=limit))
        return messages

    def get_message(self, chat_id: str | int, message_id: int) -> MessageBase:
        """Fetches one message the bot was handed.

        Calls ``GET /chats/{chatId}/messages/{messageId}``. The rule of `get_history` holds: a
        message the bot was not handed answers like one that doesn't exist. Retried after a
        network error or a ``5xx`` answer.

        Args:
            chat_id: The chat.
            message_id: The message.

        Returns:
            The message, as the server keeps it.

        Raises:
            ChatNotFoundError: No such chat, or the bot is not in it.
            MessageNotFoundError: No such message, or one the bot was never handed.
        """
        return self.execute(_spec.get_message(chat_id, message_id))

    def read_chat(self, chat_id: str | int, up_to: int | None = None) -> int | None:
        """Marks a chat read: the ticks people see.

        Calls ``POST /chats/{chatId}/read``. In a group or channel only a bot the room lets read
        every message may do it: one that gets only what is meant for it would show up as
        having read people it never saw. Retried after a network error or a ``5xx`` answer.

        Args:
            chat_id: The chat.
            up_to: The newest message read. ``None`` marks everything.

        Returns:
            The ID of the last message marked read, or ``None`` when the server did not say.

        Raises:
            ReadsAllNeededError: A group or channel that does not let the bot read every
                message.
            ChatNotFoundError: No such chat, or the bot is not in it.
        """
        return self.execute(_spec.read_chat(chat_id, up_to))

    def pin_message(self, chat_id: str | int, message_id: int) -> list[int]:
        """Pins a message for everybody in the chat.

        Calls ``PUT /chats/{chatId}/pins/{messageId}``. In a group or channel it needs the *pin
        messages* right, or the members' permission to pin. In a private chat it is pinned for
        the person too, and the chat says so with a notice; up to 100. Only messages the bot
        was handed, not notices. Retried after a network error or a ``5xx`` answer.

        Args:
            chat_id: The chat.
            message_id: The message.

        Returns:
            The IDs of the messages pinned in the chat now.

        Raises:
            ForbiddenError: The bot may not pin messages here.
            BlockedByUserError: In a private chat, the person blocked the bot.
            MessageNotFoundError: No such message, or one the bot was never handed.
        """
        pinned: list[int] = self.execute(_spec.pin_message(chat_id, message_id))
        return pinned

    def unpin_message(self, chat_id: str | int, message_id: int) -> list[int]:
        """Unpins a message.

        Calls ``DELETE /chats/{chatId}/pins/{messageId}``, with the rights of `pin_message`.
        Retried after a network error or a ``5xx`` answer.

        Args:
            chat_id: The chat.
            message_id: The message.

        Returns:
            The IDs of the messages still pinned in the chat.

        Raises:
            ForbiddenError: The bot may not unpin messages here.
            MessageNotFoundError: No such message, or one the bot was never handed.
        """
        pinned: list[int] = self.execute(_spec.unpin_message(chat_id, message_id))
        return pinned

    def unpin_all(self, chat_id: str | int) -> list[int]:
        """Unpins every message of a group or channel.

        Calls ``DELETE /chats/{chatId}/pins``, with the rights of `pin_message`. Not for a
        private chat: the person's own pins there are not the bot's to clear. Retried after a
        network error or a ``5xx`` answer.

        Args:
            chat_id: The group or channel.

        Returns:
            The IDs still pinned: none.

        Raises:
            ValidationError: ``chat_id`` is a private chat.
            ForbiddenError: The bot may not unpin messages here.
        """
        pinned: list[int] = self.execute(_spec.unpin_all(chat_id))
        return pinned

    def get_comments(
        self, chat_id: str | int, post_id: int, *, before: int | None = None, limit: int | None = None
    ) -> Comments:
        """Fetches the comments the bot was handed under a channel post, newest first.

        Calls ``GET /chats/{chatId}/posts/{postId}/comments``. The rule of `get_history` holds.
        Retried after a network error or a ``5xx`` answer.

        Args:
            chat_id: The channel.
            post_id: The post, one the bot was handed.
            before: The oldest comment ID you hold, for the next page back.
            limit: How many, 1 to 100. ``None`` leaves it to the server, which gives 50.

        Returns:
            The comments, and whether the post takes comments right now.

        Raises:
            ValidationError: ``chat_id`` is not a channel, or ``before`` or ``limit`` is out of
                range.
            MessageNotFoundError: No such post, or one the bot was never handed.
        """
        return self.execute(_spec.get_comments(chat_id, post_id, before=before, limit=limit))

    def send_comment(
        self,
        chat_id: str | int,
        post_id: int,
        text: str | None = None,
        *,
        armored: str | None = None,
        reply_to: int | None = None,
        as_author: bool | None = None,
        mentions: Iterable[str | int] | None = None,
    ) -> SentMessageBase:
        """Comments under a channel post.

        Calls ``POST /chats/{chatId}/posts/{postId}/comments``, with the rules of a person's
        comment: the channel's comments are on and this post's are not closed, a member whose
        voice was taken away stays quiet, and ``reply_to`` points at a comment of the same
        post or is dropped. ``text`` in a readable channel, ``armored`` in an encrypted one,
        sealed to the channel's keys. A bot that runs the channel speaks as the channel unless
        the channel lets its admins sign and ``as_author`` is ``True``.

        The server keeps no ``sendId`` for comments, so a comment is not repeated after a
        network error: a lost answer could otherwise write it twice. It is paced like a send
        into the channel.

        Args:
            chat_id: The channel.
            post_id: The post, one the bot was handed.
            text: The comment, for a readable channel.
            armored: The sealed comment, for an encrypted channel.
            reply_to: A comment of the same post to answer.
            as_author: ``True`` signs the comment with the bot's own name, where the channel
                allows it.
            mentions: The IDs of the people the words tag, up to 20, so they are told even with
                the comments muted.

        Returns:
            The comment, as the server stored it.

        Raises:
            ValidationError: ``chat_id`` is not a channel, a body breaks a rule, or both or
                neither of ``text`` and ``armored`` were given.
            WrongBodyKindError: ``text`` into an encrypted channel, or ``armored`` into a
                readable one.
            CommentsClosedError: The channel takes no comments, or this post doesn't.
            ForbiddenError: The bot may not comment here.
            MessageNotFoundError: No such post, or one the bot was never handed.
        """
        return self.execute(
            _spec.send_comment(
                chat_id, post_id, text, armored=armored, reply_to=reply_to, as_author=as_author, mentions=mentions
            )
        )

    def get_user(self, user_id: str | int) -> Profile:
        """Fetches somebody's public profile.

        Calls ``GET /users/{userId}``. Only for somebody the bot may look up: a private chat
        with them, a room whose member list shows them to the bot, or a room where their words
        reached it. The same gate as their key. No phone number, no presence. Somebody who blocked the bot keeps
        their pictures to themselves. Retried after a network error or a ``5xx`` answer.

        Args:
            user_id: The person, or a bot.

        Returns:
            The profile.

        Raises:
            UserNotFoundError: No such person, or nobody the bot may look up.
        """
        return self.execute(_spec.get_user(user_id))

    # --- running a group or channel ---

    def update_chat(
        self,
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
    ) -> Chat:
        """Changes the settings of a group or channel.

        Calls ``PATCH /chats/{chatId}``. Each field under the right the app asks for it:
        ``title`` and ``description`` for whoever may change the chat's info (an admin with
        *change info*, or a member the group lets); ``permissions``, ``slow_mode_seconds``,
        ``members_hidden``, ``protect_content``, the reactions and a channel's signatures for
        an admin with *change info*; ``auto_delete_seconds`` for one with *delete messages*;
        ``admins_hidden`` for one who may appoint admins. What only the owner decides
        (history, the public link, the feed) is not here. Whether a bot reads every message
        is not here either: the room decides that in the app. Leave a field out to keep it.
        Retried after a network error or a ``5xx`` answer.

        Args:
            chat_id: The group or channel.
            title: The new name, 1 to 64 characters.
            description: The new description, up to 500 characters; ``None`` clears it.
            permissions: What members may do, laid over what they may do now: a name set to
                ``False`` is taken away, ``True`` is given, the rest stays. Names as in
                `MemberPermissions`, or in the API's spelling (``"sendMedia"``). Groups only.
            slow_mode_seconds: Seconds between two messages of a member, 0 to 3600; ``0``
                turns slow mode off. Groups only.
            members_hidden: ``True`` shows the member list to the admins only.
            protect_content: ``True`` stops messages from being forwarded, copied or saved.
            reactions_enabled: ``False`` turns reactions off.
            allowed_reactions: The only emoji people may react with, up to 40 of at most 16
                characters each; ``None`` or an empty list allows any.
            auto_delete_seconds: After how many seconds new messages delete themselves, up to
                31 days; ``0`` turns it off.
            signatures: ``True`` shows who wrote each post. Channels only.
            signature_profiles: ``True`` links a signature to the profile. Channels only.
            admins_hidden: ``True`` keeps the admins from the subscribers. Channels only.

        Returns:
            The chat with the new settings.

        Raises:
            ValidationError: Nothing to change, a value out of range, an unknown permission,
                a group's setting on a channel or the other way round, or a private chat.
            ForbiddenError: The bot lacks the right one of the fields needs.

        Example:
            Slow the group down and keep files out::

                api.update_chat("G48213907", slow_mode_seconds=30, permissions={"send_media": False})
        """
        return self.execute(
            _spec.update_chat(
                chat_id,
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
        )

    def update_member(
        self,
        chat_id: str | int,
        user_id: str | int,
        *,
        role: str | None = None,
        admin_rights: Iterable[str] | None = None,
        restrictions: Iterable[str] | None = None,
        restricted_until: datetime | int | None = KEEP,
        custom_title: str | None = KEEP,
        anonymous: bool | None = None,
    ) -> Member:
        """Promotes, demotes, restricts or gives a title to a member of a group or channel.

        Calls ``PATCH /chats/{chatId}/members/{userId}``, with the app's rules to the letter.
        ``role`` and ``admin_rights`` need the *manage admins* right, and nobody hands out a
        right they don't hold; an admin is touched only by the owner or by whoever appointed
        them. ``restrictions`` need *ban users* and mean nothing on an admin. ``custom_title``
        and ``anonymous`` need *manage admins*. The owner is nobody's to change, and a bot
        doesn't change its own seat. Retried after a network error or a ``5xx`` answer.

        Args:
            chat_id: The group or channel.
            user_id: The member.
            role: ``"admin"`` promotes, ``"member"`` demotes.
            admin_rights: The rights of the badge, names as in `AdminRights` or in the API's
                spelling. With ``role="admin"`` they are the new admin's rights (the server's
                defaults when left out); alone, they replace an admin's rights.
            restrictions: The permissions taken away from this member, names as in
                `MemberPermissions`; an empty list gives them all back.
            restricted_until: When the restriction ends: an aware `~datetime.datetime` or unix
                milliseconds; ``None`` for good. Only with ``restrictions``.
            custom_title: An admin's title, up to 16 characters; ``None`` removes it.
            anonymous: ``True`` lets an admin post without their name.

        Returns:
            The member's seat now.

        Raises:
            ValidationError: Nothing to change, an unknown role, right or permission, a title
                too long, ``restricted_until`` without ``restrictions``, or a private chat.
            ForbiddenError: The bot lacks the right, the member is the owner or an admin
                appointed by somebody else, or it is the bot's own seat.
            MemberNotFoundError: Not a member, or hidden from the bot.

        Example:
            Mute somebody for an hour::

                until = datetime.now(UTC) + timedelta(hours=1)
                api.update_member(group_id, user_id, restrictions=["send_messages"], restricted_until=until)
        """
        return self.execute(
            _spec.update_member(
                chat_id,
                user_id,
                role=role,
                admin_rights=admin_rights,
                restrictions=restrictions,
                restricted_until=restricted_until,
                custom_title=custom_title,
                anonymous=anonymous,
            )
        )

    def remove_member(
        self, chat_id: str | int, user_id: str | int, *, ban: bool = False, banned_until: datetime | int | None = None
    ) -> bool:
        """Throws somebody out of a group or channel, and bans them if asked.

        Calls ``DELETE /chats/{chatId}/members/{userId}``. It needs the *ban users* right; never
        the owner, and an admin only by the owner, which a bot never is. A ban keeps them from
        coming back through any link, until ``banned_until`` or for good. In a group the room
        sees the usual notice. Retried after a network error or a ``5xx`` answer; when the
        retry finds them gone, the first try worked.

        Args:
            chat_id: The group or channel.
            user_id: The member.
            ban: ``True`` bans them as well.
            banned_until: When the ban ends: an aware `~datetime.datetime` or unix milliseconds.
                ``None`` for good. Only with ``ban=True``.

        Returns:
            ``True`` when they were banned as well, ``False`` when only removed.

        Raises:
            ValidationError: ``banned_until`` without ``ban=True``, or a private chat.
            ForbiddenError: The bot lacks the right, or the member is the owner or an admin.
            MemberNotFoundError: Not a member, or hidden from the bot.
        """
        return self.execute(_spec.remove_member(chat_id, user_id, ban=ban, banned_until=banned_until))

    def purge_member(self, chat_id: str | int, user_id: str | int, *, up_to: int | None = None) -> bool:
        """Deletes everything one member wrote in a group or channel.

        Calls ``DELETE /chats/{chatId}/members/{userId}/messages``. It needs the *delete
        messages* right, as in the app. Retried after a network error or a ``5xx`` answer.

        Args:
            chat_id: The group or channel.
            user_id: The member.
            up_to: Only up to this message ID. ``None`` deletes all of it.

        Returns:
            ``True`` once it is done.

        Raises:
            ValidationError: A private chat.
            ForbiddenError: The bot lacks the right.
            MemberNotFoundError: No such person.
        """
        return self.execute(_spec.purge_member(chat_id, user_id, up_to=up_to))

    def get_bans(self, chat_id: str | int) -> list[Ban]:
        """Lists who is banned from a group or channel.

        Calls ``GET /chats/{chatId}/bans``. It needs the *ban users* right. Retried after a
        network error or a ``5xx`` answer.

        Args:
            chat_id: The group or channel.

        Returns:
            The bans.

        Raises:
            ValidationError: A private chat.
            ForbiddenError: The bot lacks the right.
        """
        bans: list[Ban] = self.execute(_spec.get_bans(chat_id))
        return bans

    def unban(self, chat_id: str | int, user_id: str | int) -> bool:
        """Lifts a ban: the person can come back through a link.

        Calls ``DELETE /chats/{chatId}/bans/{userId}``. It needs the *ban users* right. Nobody
        is put back in. Retried after a network error or a ``5xx`` answer.

        Args:
            chat_id: The group or channel.
            user_id: The person.

        Returns:
            ``True`` when there was a ban, ``False`` when there was none.

        Raises:
            ValidationError: A private chat.
            ForbiddenError: The bot lacks the right.
        """
        return self.execute(_spec.unban(chat_id, user_id))

    def get_invites(self, chat_id: str | int) -> list[Invite]:
        """Lists the invite links of a group or channel.

        Calls ``GET /chats/{chatId}/invites``. An admin with the *invite links* right may, and
        so may a member the group lets add people. Retried after a network error or a ``5xx``
        answer.

        Args:
            chat_id: The group or channel.

        Returns:
            The links.

        Raises:
            ValidationError: A private chat.
            ForbiddenError: The bot may not manage links here.
        """
        invites: list[Invite] = self.execute(_spec.get_invites(chat_id))
        return invites

    def create_invite(
        self,
        chat_id: str | int,
        *,
        label: str | None = None,
        expires_at: datetime | int | None = None,
        max_uses: int | None = None,
        needs_approval: bool | None = None,
    ) -> Invite:
        """Makes an invite link.

        Calls ``POST /chats/{chatId}/invites``, with the right of `get_invites`. A public chat
        already has its link, its handle. With ``needs_approval`` whoever uses the link asks
        first, and the bots that may let people in get a ``join_request``. Not repeated after a
        network error: a lost answer would otherwise make two links.

        Args:
            chat_id: The group or channel.
            label: A name for the link, up to 48 characters.
            expires_at: When it stops working: an aware `~datetime.datetime` or unix
                milliseconds. ``None`` for never.
            max_uses: How many people may use it, 1 to 100000. ``None`` for no limit.
            needs_approval: ``True`` makes people ask before they come in.

        Returns:
            The new link.

        Raises:
            ValidationError: A value out of range, or a private chat.
            ForbiddenError: The bot may not make links here.
            ChatIsPublicError: The chat is public: its handle is its link.
        """
        return self.execute(
            _spec.create_invite(
                chat_id, label=label, expires_at=expires_at, max_uses=max_uses, needs_approval=needs_approval
            )
        )

    def revoke_invite(self, chat_id: str | int, code: str) -> bool:
        """Revokes an invite link.

        Calls ``DELETE /chats/{chatId}/invites/{code}``. It needs the *invite links* right.
        Retried after a network error or a ``5xx`` answer; when the retry finds the link gone,
        the first try worked.

        Args:
            chat_id: The group or channel.
            code: The link's code, `Invite.code`.

        Returns:
            ``True`` once it is revoked.

        Raises:
            ValidationError: A private chat, or not a code.
            ForbiddenError: The bot lacks the right.
            InviteNotFoundError: No such link in this chat.
        """
        return self.execute(_spec.revoke_invite(chat_id, code))

    def get_join_requests(self, chat_id: str | int) -> list[JoinRequest]:
        """Lists who asked to come into a group or channel.

        Calls ``GET /chats/{chatId}/requests``. It needs the *add members* right. Retried after
        a network error or a ``5xx`` answer.

        Args:
            chat_id: The group or channel.

        Returns:
            The requests waiting.

        Raises:
            ValidationError: A private chat.
            ForbiddenError: The bot lacks the right.
        """
        requests: list[JoinRequest] = self.execute(_spec.get_join_requests(chat_id))
        return requests

    def decide_join_request(self, chat_id: str | int, user_id: str | int, approve: bool) -> bool:
        """Lets somebody in who asked, or turns them away.

        Calls ``POST /chats/{chatId}/requests/{userId}``. It needs the *add members* right, and
        only works for somebody who asked: it is never a way round a person's "don't add me
        to groups". Retried after a network error or a ``5xx`` answer; when the retry finds the
        request gone, the first try worked.

        Args:
            chat_id: The group or channel.
            user_id: The person who asked.
            approve: ``True`` lets them in, ``False`` turns them away.

        Returns:
            ``approve``, as the server confirms it.

        Raises:
            ValidationError: A private chat, or ``approve`` is not a bool.
            ForbiddenError: The bot lacks the right.
            JoinRequestNotFoundError: They have no request waiting.
            ChatFullError: The chat can't take anybody else. The request stays, to
                be decided when there is room.
        """
        return self.execute(_spec.decide_join_request(chat_id, user_id, approve))

    def set_comments(
        self, chat_id: str | int, enabled: bool, *, signing: bool | None = None, outside: bool | None = None
    ) -> Chat:
        """Turns a channel's comments on or off.

        Calls ``PATCH /chats/{chatId}/comments``. It needs the *manage comments* right. Retried
        after a network error or a ``5xx`` answer.

        Args:
            chat_id: The channel.
            enabled: ``True`` turns the comments on.
            signing: ``True`` lets the admins comment under their own name. Left out, the server
                takes it as ``False``.
            outside: ``True`` lets people who aren't subscribed comment on a public readable
                channel. Left out, it stays as it is.

        Returns:
            The channel with the new settings.

        Raises:
            ValidationError: Not a channel.
            ForbiddenError: The bot lacks the right.
        """
        return self.execute(_spec.set_comments(chat_id, enabled, signing=signing, outside=outside))

    def set_post_comments(self, chat_id: str | int, post_id: int, comments_off: bool) -> bool:
        """Closes or opens again the comments of one post, whatever the channel says.

        Calls ``PATCH /chats/{chatId}/posts/{postId}``. It needs the *manage comments* right.
        Retried after a network error or a ``5xx`` answer.

        Args:
            chat_id: The channel.
            post_id: The post, one the bot was handed.
            comments_off: ``True`` closes them, ``False`` opens them.

        Returns:
            Whether the post's comments are closed now.

        Raises:
            ValidationError: Not a channel.
            ForbiddenError: The bot lacks the right.
            MessageNotFoundError: No such post, or one the bot was never handed.
        """
        return self.execute(_spec.set_post_comments(chat_id, post_id, comments_off))

    def get_channel_stats(self, chat_id: str | int) -> ChannelStats:
        """Fetches a channel's numbers: subscribers coming and going, views per post.

        Calls ``GET /chats/{chatId}/stats``. For its admins. No words: the best posts come by
        ID. Retried after a network error or a ``5xx`` answer.

        Args:
            chat_id: The channel.

        Returns:
            The numbers.

        Raises:
            ValidationError: Not a channel.
            ForbiddenError: The bot is not one of the channel's admins.
        """
        return self.execute(_spec.get_channel_stats(chat_id))

    def set_chat_photo(self, chat_id: str | int, data: bytes, *, limit: int = UPLOAD_MAX) -> Picture:
        """Changes the picture of a group or channel.

        Calls ``PUT /chats/{chatId}/photo``, as ``multipart/form-data``. A PNG, JPEG or WebP, or
        a short MP4 or WebM clip, judged by its bytes. Whoever may change the chat's info may:
        an admin with *change info*, or a member the group lets. The picture it replaces stays
        in the chat's album. Not repeated after a network error: a lost answer would put the
        picture up twice.

        Args:
            chat_id: The group or channel.
            data: The picture's bytes.
            limit: The largest file accepted before anything is sent, in bytes. The server has
                a lower limit of its own for pictures.

        Returns:
            Where the new picture is, and what it turned out to be.

        Raises:
            ValidationError: Not bytes, too large, or a private chat.
            ForbiddenError: The bot may not change the chat's info.
            PayloadTooLargeError: The picture is over the server's limit.
            BadRequestError: Not a picture or a clip (status ``415``).
        """
        return self.execute(_spec.set_chat_photo(chat_id, data, limit=limit))

    def delete_chat_photo(self, chat_id: str | int) -> bool:
        """Removes the picture of a group or channel.

        Calls ``DELETE /chats/{chatId}/photo``, with the right of `set_chat_photo`. The current
        picture leaves the album and the one before it, if any, is worn again. Not repeated
        after a network error: a second delete would take that one off too.

        Args:
            chat_id: The group or channel.

        Returns:
            ``True`` when there was a picture to remove.

        Raises:
            ValidationError: A private chat.
            ForbiddenError: The bot may not change the chat's info.
        """
        return self.execute(_spec.delete_chat_photo(chat_id))

    def set_chat_banner(self, chat_id: str | int, data: bytes, *, limit: int = UPLOAD_MAX) -> Picture:
        """Changes the banner of a group or channel, like `set_chat_photo`.

        Calls ``PUT /chats/{chatId}/banner``. Not repeated after a network error.

        Args:
            chat_id: The group or channel.
            data: The banner's bytes: a PNG, JPEG or WebP, or a short MP4 or WebM clip.
            limit: The largest file accepted before anything is sent, in bytes.

        Returns:
            Where the new banner is, and what it turned out to be.

        Raises:
            ValidationError: Not bytes, too large, or a private chat.
            ForbiddenError: The bot may not change the chat's info.
            PayloadTooLargeError: The banner is over the server's limit.
            BadRequestError: Not a picture or a clip (status ``415``).
        """
        return self.execute(_spec.set_chat_banner(chat_id, data, limit=limit))

    def delete_chat_banner(self, chat_id: str | int) -> bool:
        """Removes the banner of a group or channel, like `delete_chat_photo`.

        Calls ``DELETE /chats/{chatId}/banner``. Not repeated after a network error.

        Args:
            chat_id: The group or channel.

        Returns:
            ``True`` when there was a banner to remove.

        Raises:
            ValidationError: A private chat.
            ForbiddenError: The bot may not change the chat's info.
        """
        return self.execute(_spec.delete_chat_banner(chat_id))

    # --- media ---

    def download_media(self, media: Media | str, *, max_bytes: int = MEDIA_MAX_BYTES) -> bytes:
        """Downloads the file behind a `Media`, exactly as the server stores it.

        Fetches ``GET /media/{id}`` on the API server, the address a `Media` carries in
        ``url``. This is not an operation of the Bot API document, and it skips the rate
        limiter and the retries: an error is raised at once. The token is not sent, since the
        server serves media without it. Only ``/media/<id>`` addresses on this client's API
        server are fetched, so a crafted URL in an update cannot send the client anywhere
        else. The download has timeouts of its own: 10 seconds to connect, and 120 seconds to
        wait for each piece of data.

        Args:
            media: A `Media`, its ``/media/<id>`` path, its full URL on the API server, or the
                bare ID.
            max_bytes: The largest file to accept, 64 MiB by default. It is checked against the
                size the server declares, and again while downloading.

        Returns:
            The file's bytes. For an attachment of an encrypted chat they are OpenPGP
            ciphertext, which `Bot.download_media` decrypts, and so does the same method of
            `SyncBot`.

        Raises:
            ValidationError: ``media`` is not a media file of this server, or the file is
                larger than ``max_bytes``.
            NotFoundError: No such file. ``code`` is ``"media_not_found"``.
            ServerError: The server answered with a ``5xx`` status.
            APIError: The server answered with a status other than 200. ``code`` is
                ``"http_<status>"``.
            NetworkError: No answer came. `RequestTimeoutError` when it took too long.
            ConfigurationError: The client was closed, or the ``http_client`` you passed in
                was.

        Example:
            The first attachment of a message::

                data = api.download_media(message.media[0])
        """
        url = self._media_url(media)
        if self._http.is_closed:
            raise ConfigurationError("this Client was closed")
        try:
            with self._http.stream(
                "GET", url, headers={"User-Agent": self._user_agent}, timeout=httpx2.Timeout(120.0, connect=10.0)
            ) as response:
                if response.status_code != 200:
                    raise _media_status_error(response.status_code, urlsplit(url).path)
                declared = response.headers.get("content-length")
                if declared and declared.isascii() and declared.isdigit() and int(declared) > max_bytes:
                    raise ValidationError(f"the file is {declared} bytes, more than max_bytes={max_bytes}")
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValidationError(f"the file is larger than max_bytes={max_bytes}")
                    chunks.append(chunk)
                return b"".join(chunks)
        except httpx2.HTTPError as exc:
            raise _media_error(urlsplit(url).path, exc) from exc

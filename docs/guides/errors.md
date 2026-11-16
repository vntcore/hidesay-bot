# Errors

Every error the library raises is a `HidesayBotError`. Below it, errors are grouped by **where**
the problem is, so you can catch as much or as little as you need.

```text
HidesayBotError
├── ConfigurationError        the bot or client is set up wrong (no token, bad URL...)
├── ValidationError           a request that the server would refuse; it was not sent
├── PayloadError              the server sent JSON this version cannot read
├── APIError                  the server answered with an error
│   ├── BadRequestError           400 (WrongBodyKindError, BadMarkupError, BadWebhookError, ChatIsOpenError,
│   │                                  BadMediaError, ChatIsProtectedError)
│   ├── AuthenticationError       401
│   ├── ForbiddenError            403 (BotPausedError, BlockedByUserError, UserGoneError,
│   │                                  ReadsAllNeededError, CommentsClosedError)
│   ├── NotFoundError             404 (ChatNotFoundError, MessageNotFoundError, MemberNotFoundError,
│   │                                  UserNotFoundError, UsernameNotFoundError, NoUserKeyError,
│   │                                  CallbackNotFoundError, StickerNotFoundError, InviteNotFoundError,
│   │                                  JoinRequestNotFoundError, EndpointNotFoundError)
│   ├── ConflictError             409 (SendIdConflictError, WebhookActiveError, CallbackAnsweredError,
│   │                                  ChatIsPublicError, ChatFullError)
│   ├── CallbackExpiredError      410
│   ├── PayloadTooLargeError      413
│   ├── RateLimitError            429 (FloodWaitError, SlowModeError)
│   ├── ServerError               5xx
│   └── UnexpectedResponseError   not the API's JSON at all (a proxy page, a cut answer)
├── NetworkError              no answer: DNS, refused connection, TLS...
│   └── RequestTimeoutError
├── CryptoError               OpenPGP
│   ├── InvalidKeyError
│   ├── PassphraseError
│   ├── EncryptionError           the message was NOT sent
│   │   └── EncryptionRequiredError
│   ├── DecryptionError
│   │   └── NotARecipientError
│   ├── MediaIntegrityError       a downloaded file is not the one its message describes
│   ├── SignatureError
│   └── KeyChangedError
├── WebhookError
├── WebBotDataError           validate_web_bot_data refused the initData of a Web-Bot page
├── PollingError
└── StorageError
    └── PollingConflictError
```

The full list, with the server's error codes, is in the [API reference](../api/errors.rst).

## What an `APIError` tells you

```python
from hidesay_bot import APIError

try:
    await bot.send_message(chat_id, "hi")
except APIError as exc:
    print(exc.status)        # 403
    print(exc.code)          # "blocked_by_user"
    print(exc.message)       # the server's explanation
    print(exc.retry_after)   # seconds to wait, for 429 (else None)
    print(exc.expected)      # "text" or "armored", for wrong_body_kind
    print(exc.method, exc.path)   # "POST", "/messages"
```

`str(exc)` is readable and never contains the token.

## What the library retries for you

You don't need to wrap every call in a retry loop. The client already:

- retries on network errors and `5xx` answers, with a growing random wait, **when the call is
  safe to repeat**: reads, idempotent calls, and the sends that carry a `sendId`
  (`send_message`, `forward_message`). By default it tries 4 times in all;
- waits out `429` answers for as long as the server asks (up to 90 seconds, 5 times per call);
- treats some errors after an uncertain attempt as success. For example, if a delete timed out
  and the retry says `message_not_found`, the first attempt did delete it.

It never retries other `4xx` errors: they would fail the same way again.

Eight calls are not retried after a network error or a `5xx`, because a second try could do
the job twice or would come too late: `get_updates` (polling has its own loop), `send_typing`
(a late "typing" is useless), `send_comment` (comments carry no `sendId`), `create_invite` (it
would make two links), `set_chat_photo` and `set_chat_banner` (the picture would go up twice),
`delete_chat_photo` and `delete_chat_banner` (a second delete takes off the picture before it
too). When one of them fails that way, the first try may still have gone through.

When the retries run out, you get the last error. See [Configuration](../getting-started/configuration.md#retries)
to change the numbers.

## Handling errors in a bot

Inside handlers, let errors go up to an error handler, instead of catching them everywhere:

```python
from hidesay_bot import BlockedByUserError, ForbiddenError


@bot.on_error(BlockedByUserError)
async def blocked(error, update):
    await db.mark_inactive(update.chat.id)          # stop writing to them


@bot.on_error(ForbiddenError)
async def no_rights(error, update):
    log.info("no rights in chat %s: %s", update.chat.id, error.code)


@bot.on_error()
async def anything_else(error, update):
    log.exception("update %s failed", update.update_id, exc_info=error)
    raise error                                     # keep it in the failed list
```

An error handler that returns normally marks the update as done. An error that nobody handles
marks the update as failed and keeps it for later ([Storage](storage.md#failed-updates)). Either
way the bot goes on with the next updates.

Catch an error inside the handler only when you have something useful to do there:

```python
@bot.command("notify")
async def notify(message, command):
    for user_id in subscribers:
        try:
            await bot.send_message(user_id, command.args)
        except (BlockedByUserError, UserGoneError):
            subscribers.discard(user_id)
```

## Errors that need your attention

| Error | What it means | What to do |
|---|---|---|
| `AuthenticationError` | the token is wrong, revoked or replaced | get a new token from the console |
| `BotPausedError` | the owner paused the bot | nothing until it is resumed; polling waits for it |
| `PollingError` | the bot polls while a webhook is set (`get_updates` on a client raises `WebhookActiveError`) | `run(drop_webhook=True)` or delete the webhook |
| `EndpointNotFoundError` | the server has no such path | `api_url` points to the wrong server: use the API address the console shows |
| `EncryptionError` | the message could not be encrypted, and was not sent | see [OpenPGP](openpgp.md#common-problems) |
| `PollingConflictError` | another process runs this bot with the same database | run one process |
| `PayloadError` | the server sent something this version cannot read | update hidesay-bot |

## Errors you can usually ignore

| Error | Why |
|---|---|
| `MessageNotFoundError` on delete | the message is already gone |
| `CallbackAnsweredError`, `CallbackExpiredError` | the press was already answered, or it is too late |
| `BlockedByUserError`, `UserGoneError` | the person does not want messages, or left Hidesay |

## `ValidationError`: refused before sending

The library checks your request against the same rules as the server (text length, button
rules, IDs, URLs...). A `ValidationError` means nothing was sent; `exc.field` names the bad
field.

```python
try:
    await message.reply("x" * 5000)
except ValidationError as exc:
    print(exc.field, exc)        # text  text is 5000 characters long (UTF-16), the limit is 4096
```

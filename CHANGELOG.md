# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the version
numbers follow [Semantic Versioning](https://semver.org/), with the rules in
`docs/project/versioning.md`.

## [Unreleased]

For Bot API 2.0.0, where IDs stopped being plain numbers. 0.2.0 can't read what that server
sends: upgrade both together.

### Added

- `validate_web_bot_data` checks the `initData` a bot's Web-Bot page sends to its server: the
  Hidesay server's Ed25519 signature, the bot it was signed for and its age. What comes back is
  a `WebBotData`, whose user has the same ID the bot sees in its updates. `parse_web_bot_data`
  reads the string without checking it, and `WebBotDataError.reason` says why one was refused.
  No new dependency: `cryptography` does the check when it is installed, plain Python when it
  isn't. See the new Web-Bots guide.
- `get_web_bot_key()` on both clients (`GET /webbot/key`), and `validate_web_bot_data()` on
  `Bot` and `SyncBot`, which fetches the bot's ID and the key once and keeps them.
- Two new update kinds come through as they are, in `raw`: `web_bot_write_access` and
  `web_bot_contact`.

### Changed

- IDs are strings. A person's is eight digits (`"48213907"`); a bot's, a group's and a
  channel's have a letter in front (`"B48213907"`, `"G48213907"`, `"C48213907"`). That goes
  for `User.id`, `ChatRef.id`, `Chat.id`, `BotInfo.id`, `UserKey.user_id`, `Resolved.id` and
  every method that takes a chat or a user. A person's ID can also be passed as an `int`; a
  group's or a channel's has to be the string, letter included. Old short numbers are refused
  before anything is sent (`ValidationError`).
- The private chat with somebody has that person's ID, so `send_message(user.id, ...)`
  writes to them.
- `chat:<id>` command scopes name a group or a channel: `chat:G48213907`.
- `f.user()` and `f.chat()` take the new IDs. A string shaped like an ID is only that ID, never
  a handle: anybody can pick `48213907` as a handle, and a filter like `f.user(ADMIN_ID)` must
  not let them through. Write `"@48213907"` for a handle made of digits. An empty string matches
  nobody (before, it matched everybody without a handle).
- The examples read `ADMIN_ID` and the chat arguments as strings.

### Upgrading

- Anything you saved with the old numbers (subscribers, admins, to-do lists keyed by chat)
  points at IDs that no longer exist. Take the new ones from the next updates, or ask people
  for theirs: it is on their profile in the app.
- With `pin_keys=True`, pins saved by 0.2.0 are under the old numbers, so each person's key is
  pinned again the first time the bot sees it after the upgrade. If continuity matters to you,
  check the fingerprints you care about another way before you trust them again.
- A token keeps working after the change, with the old number in it. A token made after it
  carries the bot's new number, and `SQLiteStorage` keeps each bot's data under the number in
  its token: with a new token the file starts empty for that bot.

## [0.2.0] - 2026-11-15

First release as hidesay-bot. Versions 0.1.0 and 0.1.1 came out under the app's old name, as
PGPCbot: this is the same library, and what changes for code written against it is under
"Changed".

### Added

- `AsyncClient` and `Client`: all 22 operations of Bot API 1.0.0 (`openapi.json`), plus media
  downloads, with typed models, client-side checks that match the server's, retries with
  backoff and a stable `sendId`, waiting on `429`, and a client-side rate limiter.
- `Bot` and `SyncBot`: handlers for messages, commands, buttons, edited and deleted messages,
  and membership changes; filters, routers, middleware, error handlers, automatic answers to
  button presses; startup and shutdown hooks; `request_stop()` from a handler or another thread.
- `resolve()`, for `GET /resolve/{username}`: the handle somebody writes at a bot, turned into
  an id. One pool of names covers people, groups and channels, so the answer says which it was
  - `Resolved`, with `HandleKind`, a `ChatPeer` for a person or a `ChatCard` for a public group
  or channel. A leading `@` and the case are taken off before the request goes out, and
  `UsernameNotFoundError` (404 `username_not_found`) is what a free handle, a group with no
  public name and a closed account all answer.
- OpenPGP: making, loading, protecting and rotating the bot's key; automatic encryption and
  signing in protected chats; decryption and signature checks on incoming messages, including
  the compressed messages phones send; reading the app's internal message format; downloading
  and decrypting attachments; pinning keys the first time they are seen.
- Long polling and an ASGI webhook (FastAPI, Starlette, uvicorn) with "at least once"
  delivery, a durable `SQLiteStorage` queue, and failed updates kept so they can be retried.
- The `hidesay-bot` command line: `keygen`, `fingerprint`, `whoami`, `publish-key`, `webhook`.
- The documentation website at <https://vntcore.github.io/hidesay-bot/>, built from the
  docstrings and the pages in `docs/`.

### Changed

- The package is `hidesay-bot` on PyPI, it is imported as `hidesay_bot` and the command is
  `hidesay-bot`.
- The environment variables are `HIDESAY_BOT_TOKEN`, `HIDESAY_API`, `HIDESAY_BOT_KEY` and the
  rest of the family, in place of `PGPCHAT_*`.
- The default server is `https://api.hidesay.com`.
- Webhook deliveries carry the secret in `X-Hidesay-Bot-Api-Secret-Token`, and a refused one is
  answered with `WWW-Authenticate: Hidesay-Secret-Token`.
- `PGPCbotError` is `HidesayBotError`, and the loggers are `hidesay_bot.*`.

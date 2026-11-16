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
- Files, albums and stickers in every chat. On the clients, `send_message` takes `media` and
  `sticker`, `upload_file` uploads (`POST /attachments`) and `get_sticker` reads a sticker.
  `Bot.send_media` and `send_sticker` do the whole job: in an end-to-end encrypted chat each
  file is sealed before it leaves, the way the phones seal theirs, and the message that
  describes the files is sealed and signed, so the server sees neither. `InputFile` describes a
  file to send: bytes or a path, a name, a type, the size of a picture, the length of a clip, a
  thumbnail. `InputFile.from_path` reads one from disk; `content_type`, `content_kind` and
  `sha256` say what goes out, `check` holds it to the server's rules, and `media_in` and
  `envelope` describe it once uploaded, for a readable chat and for an encrypted one.
  `upload_max` on both bots is the largest file the server takes.
- `forward_message` and `Message.forward_to`. Between readable chats the server forwards; from
  or into an end-to-end encrypted chat the bot opens the message and seals it again for the
  target, files included. An encrypted message goes into a readable chat only as a copy
  (`hide_sender=True`), and a chat that protects its content isn't forwarded from.
- Reactions, pins and read marks: `set_reaction`, `remove_reaction`, `pin_message`,
  `unpin_message`, `unpin_all`, `read_chat`, and on a message `react`, `remove_reaction`, `pin`
  and `unpin`. `delete_messages` deletes up to 100 at once.
- `get_history`, `get_message` and `get_chats`; the bots open the messages they get back the
  way they open an update. `get_user` reads a person's profile.
- Channel comments: `get_comments` and `send_comment`. `Message.reply` on a comment answers
  under the same post.
- Running a group or channel with an admin badge: `update_chat`, `update_member`,
  `remove_member`, `purge_member`, `get_bans`, `unban`, `get_invites`, `create_invite`,
  `revoke_invite`, `get_join_requests`, `decide_join_request`, `set_comments`,
  `set_post_comments`, `get_channel_stats`, `set_chat_photo`, `delete_chat_photo`,
  `set_chat_banner` and `delete_chat_banner`. Where `None` means something, `update_chat` and
  `update_member` take `KEEP` as an edit does for its buttons: `description=None` clears the
  description, and leaving it out keeps it.
- New update kinds, each with its decorator: `comment` and `edited_comment` (`on_comment`,
  `on_edited_comment`), `message_reaction` (`on_reaction`, with a `ReactionUpdate`, whose
  `added` and `removed` say whether the reaction was put or taken back, and `counts` how many
  of each emoji the message has), `chat_member` for somebody else's seat (`on_member_update`),
  `join_request` (`on_join_request`, with a `JoinRequest`) and `chat_updated`
  (`on_chat_updated`, with a `ChatUpdated`).
- Filters: `f.has_sticker`, `f.forwarded`, `f.comment`, `f.reaction(...)` and `f.changed(...)`.
  For filters of your own, `FilterContext` has `bot_usernames`, `bot_id` and `handles`.
- Fields: `Message.reply_to_message`, `thread_root`, `markup`, `sticker`, `forward` and `link`,
  and `Message.is_comment`; `Media.name`, `size`, `thumb` and `sha256`; `Seat.permissions` and
  `reads_all`; the room's settings on `Chat`; `AdminRights.manage_dividers`;
  `BotInfo.last_name`, `usernames` and `handles` (every handle, the main one first); three more
  `Limits`. New models: `Sticker`, `Forward`, `ForwardChat`, `LinkCard`, `MemberPermissions`,
  `MediaIn`, `UploadedFile`, `MessageReactions` (with `counts`), `Reaction`, `ReactionCount`,
  `Profile`, `Invite`, `Ban`, `Picture`, `Comments`, `ChannelStats`, `StatsWindow`,
  `GrowthPoint` and `PostViews`.
- An exception for each new error code: `BadMediaError`, `ChatIsProtectedError`,
  `ReadsAllNeededError`, `CommentsClosedError`, `StickerNotFoundError`, `InviteNotFoundError`,
  `JoinRequestNotFoundError`, `ChatIsPublicError` and `ChatFullError`.
- `MediaIntegrityError`: a downloaded file whose SHA-256 isn't the one its sealed message
  promised is refused.
- `hidesay-bot whoami` lists the other handles the bot answers to.
- A guide to groups and channels: what a bot reads there, asking to read everything, running a
  room.
- A Web-Bot reference page in the docs, generated from the server's spec.

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
- Privacy mode is gone. Each group or channel decides whether a bot reads every message
  (`Seat.reads_all`, `Chat.reads_all`); it is off when the bot is added, the bot can't change
  it, and a `my_chat_member` update says when it changes (`ChatMemberUpdate.reads_all_changed`).
  When the bot is added, `ChatMemberUpdate.chat` is the whole room.
- `f.mentioned` also matches the bot's name picked from the list (`@[Weather](uB48213907)`),
  and both it and `f.command` know every handle of the bot.
- `Media.encrypted` comes from the server's `plain` flag.
- `examples/05_group_bot.py` asks the group to let it read every message, and says why.
- What the server tells a bot follows what it was handed: `Message.reply_to` is `None` when
  the quoted message never reached the bot (like `reply_to_message`), `Chat.pinned_message_ids`
  lists only pins it could open, and `deleted_messages` names only messages it had.
  `get_user` and `get_user_key` work for people the bot may look up: a private chat, a member
  list that shows them, or words of theirs that reached it.

### Removed

- `BotInfo.privacy_mode`, and its line in `hidesay-bot whoami`.

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
- Code that read `me.privacy_mode` reads `chat.reads_all` for the room it cares about: there is
  no switch for the whole bot any more. Every room starts with it off, also where the bot read
  everything before the change, so a bot that needs every message has to ask again. The best
  moment is the `my_chat_member` update that says it was added.

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

# API coverage

Bot API 2.0.0 has 57 operations and one outgoing webhook, and every one of them has a method
here, on both clients. The table says which, and where the SDK's own rules differ from the
document.

- **SDK method**: the same name on `AsyncClient`, `Client`, `Bot` and `SyncBot`, except three
  the bots do their own way: `get_updates` (they poll with `run`), `set_key` (`publish_key`) and
  `get_web_bot_key` (`validate_web_bot_data`). The bots add encryption, caches and chat objects
  on top (see [Bot](api/bot.rst)).
- `tests/test_parity.py::test_every_operation_of_the_spec_is_a_method_of_both_clients` fails if
  an operation of the spec has no method, and the request shape of every operation is checked
  against `openapi.json` itself. That test, not this page, is what keeps the list honest.

| Endpoint | HTTP method | operationId | SDK method | Models | Documentation |
|---|---|---|---|---|---|
| `/me` | GET | `getMe` | `get_me` | `BotInfo`, `Limits`, `WebhookSummary` | {py:meth}`client <hidesay_bot.AsyncClient.get_me>` |
| `/updates` | GET | `getUpdates` | `get_updates`, `iter_updates`; `Bot.run`, `start_polling` | `Update` and the event models | {py:meth}`client <hidesay_bot.AsyncClient.get_updates>`, [long polling](guides/long-polling.md) |
| `/webhook` | GET | `getWebhook` | `get_webhook` | `WebhookInfo` | {py:meth}`client <hidesay_bot.AsyncClient.get_webhook>` |
| `/webhook` | POST | `setWebhook` | `set_webhook`; `create_webhook_app(url=...)` | `WebhookInfo` | {py:meth}`client <hidesay_bot.AsyncClient.set_webhook>`, [webhooks](guides/webhook.md) |
| `/webhook` | DELETE | `deleteWebhook` | `delete_webhook`; `run(drop_webhook=True)` | `WebhookInfo` | {py:meth}`client <hidesay_bot.AsyncClient.delete_webhook>` |
| `/key` | PUT | `setKey` | `set_key`; `Bot.publish_key`, `rotate_key`, `publish_key=True` | `PublishedKey` | {py:meth}`client <hidesay_bot.AsyncClient.set_key>`, [OpenPGP](guides/openpgp.md) |
| `/commands` | GET | `getCommands` | `get_commands` | `CommandScope`, `Command` | {py:meth}`client <hidesay_bot.AsyncClient.get_commands>` |
| `/commands` | PUT | `setCommands` | `set_commands`; `Bot.sync_commands` | `CommandScope` | {py:meth}`client <hidesay_bot.AsyncClient.set_commands>`, [commands](guides/commands-and-buttons.md) |
| `/commands` | DELETE | `deleteCommands` | `delete_commands` | `CommandScope` | {py:meth}`client <hidesay_bot.AsyncClient.delete_commands>` |
| `/messages` | POST | `sendMessage` | `send_message`; `Message.reply`, `answer`, `CallbackQuery.send`, `Bot.send_media`, `send_sticker`, and `forward_message` from or into an encrypted chat | `SentMessageBase`, `InlineKeyboard`, `MediaIn`, `InputFile` | {py:meth}`client <hidesay_bot.AsyncClient.send_message>`, [messages](guides/messages.md), [files](guides/messages.md#files) |
| `/messages/{messageId}` | PATCH | `editMessage` | `edit_message`; `SentMessage.edit`, `CallbackQuery.edit_message` | `SentMessageBase` | {py:meth}`client <hidesay_bot.AsyncClient.edit_message>` |
| `/messages/{messageId}` | DELETE | `deleteMessage` | `delete_message`; `Message.delete`, `SentMessage.delete` | `bool` | {py:meth}`client <hidesay_bot.AsyncClient.delete_message>` |
| `/callbacks/{callbackId}/answer` | POST | `answerCallback` | `answer_callback`; `CallbackQuery.answer`, automatic answers | `bool` | {py:meth}`client <hidesay_bot.AsyncClient.answer_callback>`, [buttons](guides/commands-and-buttons.md#button-presses) |
| `/chats/{chatId}` | GET | `getChat` | `get_chat` | `Chat`, `ChatPeer`, `Seat`, `AdminRights`, `MemberPermissions` | {py:meth}`client <hidesay_bot.AsyncClient.get_chat>` |
| `/chats/{chatId}` | PATCH | `updateChat` | `update_chat` | `Chat`, `MemberPermissions` | {py:meth}`client <hidesay_bot.AsyncClient.update_chat>`, [running a room](guides/groups-and-channels.md#running-the-room) |
| `/chats/{chatId}/keys` | GET | `getChatKeys` | `get_chat_keys`; `Bot.recipients`, `seal` | `ChatKeys`, `UserKey` | {py:meth}`client <hidesay_bot.AsyncClient.get_chat_keys>` |
| `/chats/{chatId}/members` | GET | `getChatMembers` | `get_chat_members` | `ChatMembers`, `Member` | {py:meth}`client <hidesay_bot.AsyncClient.get_chat_members>` |
| `/chats/{chatId}/members/{userId}` | GET | `getChatMember` | `get_chat_member` | `Member` | {py:meth}`client <hidesay_bot.AsyncClient.get_chat_member>` |
| `/chats/{chatId}/members/{userId}` | PATCH | `updateMember` | `update_member` | `Member`, `AdminRights` | {py:meth}`client <hidesay_bot.AsyncClient.update_member>` |
| `/chats/{chatId}/members/{userId}` | DELETE | `removeMember` | `remove_member` | `bool` | {py:meth}`client <hidesay_bot.AsyncClient.remove_member>` |
| `/chats/{chatId}/leave` | POST | `leaveChat` | `leave_chat` | `bool` | {py:meth}`client <hidesay_bot.AsyncClient.leave_chat>` |
| `/chats/{chatId}/typing` | POST | `sendTyping` | `send_typing`; `Message.typing` | `bool` | {py:meth}`client <hidesay_bot.AsyncClient.send_typing>` |
| `/messages/delete` | POST | `deleteMessages` | `delete_messages` | `list[int]` | {py:meth}`client <hidesay_bot.AsyncClient.delete_messages>` |
| `/messages/forward` | POST | `forwardMessage` | `forward_message`; `Message.forward_to`. The bots call it between two readable chats only: from or into an encrypted one they open the message, download its files and upload them again (sealed, for an encrypted target), and send a new message (`POST /messages`) | `SentMessageBase`, `Forward` | {py:meth}`client <hidesay_bot.AsyncClient.forward_message>`, [forwarding](guides/messages.md#forwarding) |
| `/messages/{messageId}/reaction` | PUT | `setReaction` | `set_reaction`; `Message.react` | `MessageReactions`, `Reaction` | {py:meth}`client <hidesay_bot.AsyncClient.set_reaction>`, [reactions](guides/messages.md#reactions-pins-and-read-marks) |
| `/messages/{messageId}/reaction` | DELETE | `removeReaction` | `remove_reaction`; `Message.remove_reaction` | `bool` | {py:meth}`client <hidesay_bot.AsyncClient.remove_reaction>` |
| `/attachments` | POST | `uploadFile` | `upload_file`; `Bot.send_media`, and `forward_message` for the files of a forward the bot makes itself | `UploadedFile` | {py:meth}`client <hidesay_bot.AsyncClient.upload_file>`, [files](guides/messages.md#files) |
| `/stickers/{stickerId}` | GET | `getSticker` | `get_sticker`; `Bot.send_sticker` | `Sticker` | {py:meth}`client <hidesay_bot.AsyncClient.get_sticker>`, [stickers](guides/messages.md#stickers) |
| `/chats` | GET | `getChats` | `get_chats` | `Chat` | {py:meth}`client <hidesay_bot.AsyncClient.get_chats>` |
| `/chats/{chatId}/messages` | GET | `getHistory` | `get_history` | `MessageBase`; an opened `Message` on the bots | {py:meth}`client <hidesay_bot.AsyncClient.get_history>`, [history](guides/messages.md#history) |
| `/chats/{chatId}/messages/{messageId}` | GET | `getMessage` | `get_message` | `MessageBase`; an opened `Message` on the bots | {py:meth}`client <hidesay_bot.AsyncClient.get_message>` |
| `/chats/{chatId}/read` | POST | `readChat` | `read_chat` | `int` | {py:meth}`client <hidesay_bot.AsyncClient.read_chat>` |
| `/chats/{chatId}/pins` | DELETE | `unpinAll` | `unpin_all` | `list[int]` | {py:meth}`client <hidesay_bot.AsyncClient.unpin_all>` |
| `/chats/{chatId}/pins/{messageId}` | PUT | `pinMessage` | `pin_message`; `Message.pin`, `SentMessage.pin` | `list[int]` | {py:meth}`client <hidesay_bot.AsyncClient.pin_message>` |
| `/chats/{chatId}/pins/{messageId}` | DELETE | `unpinMessage` | `unpin_message`; `Message.unpin`, `SentMessage.unpin` | `list[int]` | {py:meth}`client <hidesay_bot.AsyncClient.unpin_message>` |
| `/chats/{chatId}/posts/{postId}/comments` | GET | `getComments` | `get_comments` | `Comments` | {py:meth}`client <hidesay_bot.AsyncClient.get_comments>`, [comments](guides/messages.md#comments) |
| `/chats/{chatId}/posts/{postId}/comments` | POST | `sendComment` | `send_comment`; `Message.reply` on a comment | `SentMessageBase` | {py:meth}`client <hidesay_bot.AsyncClient.send_comment>`, [comments](guides/messages.md#comments) |
| `/users/{userId}` | GET | `getUser` | `get_user` | `Profile` | {py:meth}`client <hidesay_bot.AsyncClient.get_user>` |
| `/chats/{chatId}/photo` | PUT | `setChatPhoto` | `set_chat_photo` | `Picture` | {py:meth}`client <hidesay_bot.AsyncClient.set_chat_photo>` |
| `/chats/{chatId}/photo` | DELETE | `deleteChatPhoto` | `delete_chat_photo` | `bool` | {py:meth}`client <hidesay_bot.AsyncClient.delete_chat_photo>` |
| `/chats/{chatId}/banner` | PUT | `setChatBanner` | `set_chat_banner` | `Picture` | {py:meth}`client <hidesay_bot.AsyncClient.set_chat_banner>` |
| `/chats/{chatId}/banner` | DELETE | `deleteChatBanner` | `delete_chat_banner` | `bool` | {py:meth}`client <hidesay_bot.AsyncClient.delete_chat_banner>` |
| `/chats/{chatId}/members/{userId}/messages` | DELETE | `purgeMember` | `purge_member` | `bool` | {py:meth}`client <hidesay_bot.AsyncClient.purge_member>` |
| `/chats/{chatId}/bans` | GET | `getBans` | `get_bans` | `Ban` | {py:meth}`client <hidesay_bot.AsyncClient.get_bans>` |
| `/chats/{chatId}/bans/{userId}` | DELETE | `unban` | `unban` | `bool` | {py:meth}`client <hidesay_bot.AsyncClient.unban>` |
| `/chats/{chatId}/invites` | GET | `getInvites` | `get_invites` | `Invite` | {py:meth}`client <hidesay_bot.AsyncClient.get_invites>` |
| `/chats/{chatId}/invites` | POST | `createInvite` | `create_invite` | `Invite` | {py:meth}`client <hidesay_bot.AsyncClient.create_invite>` |
| `/chats/{chatId}/invites/{code}` | DELETE | `revokeInvite` | `revoke_invite` | `bool` | {py:meth}`client <hidesay_bot.AsyncClient.revoke_invite>` |
| `/chats/{chatId}/requests` | GET | `getJoinRequests` | `get_join_requests` | `JoinRequest` | {py:meth}`client <hidesay_bot.AsyncClient.get_join_requests>` |
| `/chats/{chatId}/requests/{userId}` | POST | `decideJoinRequest` | `decide_join_request` | `bool` | {py:meth}`client <hidesay_bot.AsyncClient.decide_join_request>` |
| `/chats/{chatId}/comments` | PATCH | `setComments` | `set_comments` | `Chat` | {py:meth}`client <hidesay_bot.AsyncClient.set_comments>` |
| `/chats/{chatId}/posts/{postId}` | PATCH | `setPostComments` | `set_post_comments` | `bool` | {py:meth}`client <hidesay_bot.AsyncClient.set_post_comments>` |
| `/chats/{chatId}/stats` | GET | `getChannelStats` | `get_channel_stats` | `ChannelStats`, `StatsWindow`, `GrowthPoint`, `PostViews` | {py:meth}`client <hidesay_bot.AsyncClient.get_channel_stats>` |
| `/resolve/{username}` | GET | `resolve` | `resolve` | `Resolved`, `HandleKind`, `ChatCard`, `ChatPeer` | {py:meth}`client <hidesay_bot.AsyncClient.resolve>`, {py:meth}`bot <hidesay_bot.Bot.resolve>` |
| `/users/{userId}/key` | GET | `getUserKey` | `get_user_key` | `UserKey` | {py:meth}`client <hidesay_bot.AsyncClient.get_user_key>` |
| `/token/revoke` | POST | `revokeToken` | `revoke_token` | `bool` | {py:meth}`client <hidesay_bot.AsyncClient.revoke_token>` |
| `/webbot/key` | GET | `getWebBotKey` | `get_web_bot_key`; `Bot.validate_web_bot_data` | `str` | {py:meth}`client <hidesay_bot.AsyncClient.get_web_bot_key>`, [Web-Bots](guides/web-bot.md#the-key) |
| webhook `update` (server → bot) | POST | — | `create_webhook_app`, `WebhookApp.handle` | `Update` | [webhooks](guides/webhook.md), [reference](api/webhook.rst) |
| `/media/{id}` (not in the spec) | GET | — | `download_media`; `Bot.forward_message`, for the files of a forward it makes itself | `Media` | {py:meth}`client <hidesay_bot.AsyncClient.download_media>` |

The media download is not an operation of the Bot API document: the server gives `media[].url`
in messages and serves `GET /media/{id}` without authentication. It is listed because bots need
it.

## Error codes

All 37 codes of the spec's `ErrorCode` enum have their own exception class
([Exceptions](api/errors.rst)). `tests/test_errors.py` checks the table against
`openapi.json` both ways, so a code added to the spec fails the tests until it is mapped, and
`tests/test_rooms.py` checks that the operations' descriptions name each of the nine that came
with the room operations.

## Where the spec and the server differ

hidesay-bot follows what the server actually does where the document is silent or different.
The list below was checked against a running server in October 2026, and again for Bot API
2.0.0 in November 2026:

| Topic | The spec says | The server does | What hidesay-bot does |
|---|---|---|---|
| Text length | `maxLength: 4096` | counts UTF-16 code units, the way JavaScript's `.length` does | counts UTF-16 units |
| Control characters | not stated | refuses all but `\n` and `\t`, `\r` included | turns `\r\n` and `\r` into `\n`, refuses the rest |
| Empty text | `minLength: 1` | trims with JavaScript's `trim()`, which also drops U+FEFF | the same set of spaces |
| `PUT /key` fingerprint | must be the key's | takes it from the request body | the SDK computes it from the key itself, so the two always match |
| `rate_limited` | wait `retryAfter` seconds | the body always says 60; the `Retry-After` header has the real time | trusts the header |
| `GET /me` limits | an example with a few names | more names (`markupBytes`, `commandsPerScope`, `botsPerChat`, `uploadBytesMax`, `historyLimitMax`, ...) | types the known ones, keeps all in `raw` |
| `media[].duration` | no unit | milliseconds | `duration_ms` |
| Readable chats always use `text` | yes | a body that merely contains `-----BEGIN PGP MESSAGE-----` is delivered as `armored`, even in readable chats, and in readable groups it reaches bots named by the sender's phone or that the room lets read every message | an `armored` body it can't open in a readable chat reaches the handlers as text |
| Second press on a message | "the same press" | a press on a *different* button of the same message is a new callback: a press is identified by user, message and data | nothing to change |
| `DELETE /messages/{id}` | `deleted` is always true | `{deleted: false}` when the message is gone between the lookup and the delete | returns what the server says |
| `GET /chats/{id}` private `user` | `UserRef` (with `isBot`) plus `deleted`, `hasKey` | no `isBot` | `ChatPeer.is_bot` defaults to false |
| `POST /webhook` | https and a public address only | a server set up for local development can also accept http and private addresses | leaves the scheme to the server (the integration tests rely on it) |
| Command description | `maxLength: 256` | accepts up to 1000 raw characters, and applies 256 after folding whitespace | folds whitespace the same way, then checks 256 |
| `GET /media/{id}` | not in the spec | public, no token | downloads without the token |
| Attachments in encrypted groups | not stated | the phone seals them to the group's key list only, not to the bots it addresses; a bot is on that list only when the room lets it read every message | documented; `NotARecipientError` explains it |
| `PATCH /chats/{id}/members/{userId}` `customTitle` | up to 64, shown cut to 16 | the same | refuses more than 16, so nothing is cut without the bot knowing |

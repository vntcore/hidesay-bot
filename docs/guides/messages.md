# Messages

This guide shows how a bot reads the messages it gets, and how it sends, edits and deletes its
own.

## What a message looks like

A handler for messages gets a `Message`:

```python
@bot.on_message()
async def show(message):
    print(message.id)                    # 1834, unique inside the chat
    print(message.chat.id)               # "48213907", or "G48213907" in a group
    print(message.chat.kind)             # ChatKind.PRIVATE, GROUP or CHANNEL
    print(message.from_user.username)    # "anna", or None if she has no handle
    print(message.text)                  # what she wrote, or None
    print(message.date)                  # a datetime in UTC
```

| Field | Type | Meaning |
|---|---|---|
| `id` | `int` | the message ID, unique inside its chat |
| `chat` | `ChatRef` | `id`, `kind`, and `title` / `username` for groups and channels |
| `from_user` | `User` or `None` | who wrote it. `None` for a channel post written in the channel's name |
| `date` | `datetime` | when it was sent, in UTC |
| `reply_to` | `int` or `None` | the ID of the message it answers; `None` also when that message never reached the bot |
| `kind` | `str` | the kind of message, as the server says: `"user"` for anything a person typed or attached |
| `text` | `str` or `None` | the text, already decrypted in an encrypted chat |
| `media` | tuple of `Media` | pictures, clips and files attached |
| `signature` | `str` or `None` | for a signed channel post with no `from_user`: the name it shows |
| `encrypted` | `bool` | `True` if it came end-to-end encrypted |
| `armored` | `str` or `None` | the original OpenPGP message, in an encrypted chat |
| `plaintext` | `str` or `None` | the exact decrypted body (the app's internal format), in an encrypted chat |
| `verification` | `Verification` or `None` | the result of the signature check, in an encrypted chat |
| `reply_to_message` | `Message` or `None` | the message it answers, when the bot was handed that one too |
| `thread_root` | `int` or `None` | for a comment, the channel post it hangs under |
| `sticker` | `Sticker` or `None` | the sticker, for a message that is one |
| `forward` | `Forward` or `None` | who a forwarded message came from |
| `link` | `LinkCard` or `None` | the card the sender's phone made for the first link |
| `markup` | `InlineKeyboard` or `None` | buttons, only ever on a bot's message (a quoted one) |
| `raw` | `dict` | the JSON the server sent, untouched |

Some fields are shortcuts: `message.chat_id` is `message.chat.id`, `message.is_command` is true
when the text starts with `/`, and `message.is_comment` when the message is a comment.

### Text

In an encrypted chat you get the exact text the person wrote; in a readable one, what the
server stored. For a file or an album, `message.text` is the caption.

### Stickers and calls

For a sticker or a call notice `message.text` is `None`; `message.sticker` describes a sticker.
In an encrypted chat, `message.plaintext` still holds the app's internal format if you need it.

### Media

`message.media` lists what came with the message:

```python
from pathlib import Path
from hidesay_bot import filters as f


@bot.on_message(f.has_media)
async def save_photo(message):
    for item in message.media:
        print(item.kind, item.mime, item.width, item.height, item.duration_ms)
        data = await bot.download_media(item)       # bytes, decrypted if needed
        Path(f"{item.id}.bin").write_bytes(data)
```

`download_media` refuses files bigger than `max_bytes` (64 MiB by default). In an encrypted
chat the file is decrypted for you, and when the message gives its SHA-256 (`item.sha256`) the
file is checked against it. In an encrypted **group**, phones seal attachments only to the
group's key list, which has the bot in it only when the room lets it read every message: a bot
that gets only what is meant for it can read the words of a command but not its attachments,
and the download fails with `NotARecipientError`.

A message can also be a sticker (`message.sticker`), a forward (`message.forward` says who it
came from), carry a link card (`message.link`), and quote another message
(`message.reply_to_message`, when the bot was handed that one too). In an encrypted chat the
bot reads all of it from inside the message, so handlers see the same fields either way.

## Sending

### Replying

```python
await message.reply("Got it")                    # quotes the message
await message.reply("Got it", quote=False)       # same chat, no quote
await message.answer("Got it")                   # the same as quote=False
```

### Sending into any chat

```python
await bot.send_message("48213907", "Last night's backup finished fine.")
await bot.send_message(message.chat, "Hello")    # a chat object works too
await bot.send_message("48213907", "Done", reply_to=1834)
```

The bot can only write into chats it is in. A private chat with a person exists once they
started it, or once they let the bot write first from its Web-Bot: the page calls
`Hidesay.WebBot.requestWriteAccess()`, and when they say yes the bot gets a
`web_bot_write_access` update (see {doc}`web-bot`).

### Encryption is automatic

You never choose between "text" and "encrypted" yourself. Before sending, the library checks
the chat's mode:

- **readable chat**: the text is sent as it is;
- **encrypted chat**: the text is encrypted for every member and for the bot, signed with the
  bot's key, and sent as an OpenPGP message.

If the chat is encrypted and the message cannot be encrypted (no keys, no private key, a bad
key in a private chat), nothing is sent and you get an `EncryptionError`. The library never
falls back to plain text. The [OpenPGP guide](openpgp.md) explains the details.

You can state what you expect for one message:

```python
await message.reply("Your code is 4471", encrypt=True)    # EncryptionRequiredError in a readable chat
await message.reply("Public notice", encrypt=False)       # ValidationError in an encrypted chat
```

### Limits

| What | Limit |
|---|---|
| text | 1 to 4096 characters, counted in UTF-16 units like JavaScript does: most emoji count 2 |
| characters | no control characters except new lines and tabs. `\r\n` is turned into `\n` for you |
| encrypted message | 131072 characters of armored text |

The library checks these before sending and raises `ValidationError` with the field name.
The numbers come from the server (`GET /me`), so they follow the server you are on.

### Silent posts

```python
await bot.send_message(channel_id, "Maintenance tonight", silent=True)
```

`silent` only matters in channels: the post goes out without a notification.

### Retries never write a message twice

Every send gets a `sendId`, a UUID made once and reused for every attempt. If an answer gets
lost on the network and the library tries again, the server recognises the `sendId` and gives
back the message it already wrote. You can pass your own:

```python
import uuid

send_id = uuid.uuid4()          # save it with your job, before sending
await bot.send_message("48213907", "Invoice 2026-118 is ready", send_id=send_id)
```

If your own program crashes and runs the job again with the same `send_id`, the person still
gets one message. A `sendId` belongs to one chat: using it in another chat raises
`SendIdConflictError`.

## Showing "typing..."

```python
await message.typing()              # or: await bot.send_typing(chat_id)
result = await slow_search(message.text)
await message.reply(result)
```

The indicator goes away after a few seconds or when the message arrives. The server sends at
most one every 3 seconds per chat; extra calls return `False` and do nothing.

## Files

`send_media` sends one file, or an album of 2 to 10 pictures and clips, with a caption:

```python
from hidesay_bot import InputFile

await bot.send_media(chat_id, "sunset.jpg", caption="Tonight")      # a path
await bot.send_media(chat_id, report_bytes)                         # bytes, sent as a file
await bot.send_media(
    chat_id,
    [InputFile.from_path("a.jpg", width=1280, height=720), InputFile.from_path("b.mp4", duration_ms=8000)],
    caption="Saturday",
)
```

The kind and the type are guessed from the name: a `.jpg` is a picture, a `.mp4` a clip,
anything else a file (music included). A voice note is never guessed: pass
`InputFile(data=..., kind="audio")`. Width, height and length are not read from the bytes; give
them when you know them, and the phones draw the right box before they download anything.

In a readable chat each file goes up as it is and the server writes the message. In an
encrypted chat each file is sealed first to every member and to the bot, uploaded sealed, and
the message that describes the files is sealed and signed like a text: the server sees neither
the files nor their names. Every file carries its SHA-256, which the phones check. Files are at
most 50 MB each (`bot.upload_max`), 30 uploads a minute; in a group they need the members'
permission to send files, voice notes the one for voice notes.

## Stickers

```python
await bot.send_sticker(chat_id, 4417)                     # a sticker of a public pack
await bot.send_sticker(chat_id, message.sticker)          # one the bot was shown
```

A sticker of a pack that is not public needs its file ID as proof that the bot was shown it;
`message.sticker` carries it. `bot.get_sticker(id)` says what a sticker is.

## Forwarding

```python
await message.forward_to(archive_id)                       # with "forwarded from" over it
await message.forward_to(archive_id, hide_sender=True)     # a plain copy
await bot.forward_message(archive_id, chat_id, message_id)
```

Between two readable chats the server does it, and copies the files for the bot. When either
chat is encrypted, the bot does it itself, as a phone would: it opens the message and its
files, seals them again for the other chat, and sends them with "forwarded from" over them. A
forward of a forward keeps the first origin. The bot can't know whether the person who wrote
the message lets a forward link to their profile, so its forwards show their name without the
link (the bot's own messages keep it).

A readable chat takes from the bot only what the server writes for it, which has no "forwarded
from": a message of an encrypted chat goes into a readable one only as a copy
(`hide_sender=True`). Comments are not forwarded, and a chat that protects its content keeps
its messages in.

## Reactions, pins and read marks

```python
await message.react("👍")              # in place of the bot's earlier reaction, if any
await message.remove_reaction()
await message.pin()                    # for everybody in the chat
await sent.unpin()
await bot.unpin_all(group_id)
await bot.read_chat(chat_id)           # the ticks people see
```

The rules of a person hold: in a group or channel reactions must be on and the emoji one the
chat takes, pinning needs the right to pin, and marking a group or channel read needs the room
to let the bot read every message (`ReadsAllNeededError` otherwise): a bot that only gets what
is meant for it would show up as having read people it never saw.

`@bot.on_reaction()` hears about the reactions people put on the messages the bot was handed
([Events](events.md)).

## History

```python
for message in await bot.get_history(chat_id, limit=100):    # newest first
    print(message.date, message.from_user, message.text)

older = await bot.get_history(chat_id, before=oldest_id)
one = await bot.get_message(chat_id, message_id)
```

The history holds what the bot was handed, and nothing else: in a private chat every message,
in a group or channel the bot's own messages and the ones the room delivered to it. What the
room did not let it read stays out, even after the room lets it read everything. In an
encrypted chat each message comes back decrypted, with its signature checked; one that can't be
opened comes back as it is, with `text` left `None`.

## Comments

In a channel with comments, a comment reaches the bot by the same rule as a message (one under
its post, one that answers its comment or names it, or any of them when the channel lets it
read everything), at `@bot.on_comment()`. Answering it writes a comment under the same post:

```python
@bot.on_comment()
async def thanks(comment):
    await comment.reply("Thanks for the comment!")


await bot.send_comment(channel_id, post_id, "Fixed, thanks.")
page = await bot.get_comments(channel_id, post_id)      # page.open, page.messages
```

A bot that runs the channel comments as the channel, unless the channel lets its admins sign
and you pass `as_author=True`. A comment carries no buttons and no `send_id`: the server keeps
none for comments, so one is never sent twice by a retry.

## Editing

`reply` and `send_message` return a `SentMessage`, which you can edit or delete later:

```python
sent = await message.reply("Working on it...")
await asyncio.sleep(2)
await sent.edit("Done: 14 files checked.")
```

Or by ID:

```python
await bot.edit_message(chat_id, message_id, "New text")
```

- The bot can edit its own ordinary messages, at any age, and in a channel where it may edit
  posts anybody's post (the words only). On a file the text is the caption.
- In an encrypted chat the new text is encrypted like a send.
- Leave `markup` out to keep the buttons; `markup=None` removes them.
- Edits have their own rate limits: one per second in a private chat, twenty per minute in a
  group.

## Deleting

```python
await message.delete()                  # a message someone else sent
await sent.delete()                     # one of the bot's own
await bot.delete_message(chat_id, message_id)
```

The bot can delete any message of a private chat, as either person can there; in a group or
channel its own, other people's where it has the "delete messages" right, and any comment with
the "manage comments" right. A post takes its comments with it. `bot.delete_messages(chat_id,
ids)` deletes up to 100 at once, and deletes nothing if one of them is not the bot's to delete.
A message that is already gone raises `MessageNotFoundError`. After a timeout the library
treats that error as "done", so a retry is safe.

## Edited and deleted messages

```python
@bot.on_edited_message()
async def edited(message):
    print("now it says:", message.text)


@bot.on_deleted_messages()
async def deleted(event):
    print("gone from", event.chat.id, ":", event.message_ids)
```

## Sending without handlers

For a script that only sends, use the client directly. It does not encrypt anything, so it
fits readable chats:

```python
from hidesay_bot import Client

with Client() as api:
    api.send_message("48213907", "Backup finished.")
```

For encrypted chats outside a handler, use the bot's methods, which encrypt:

```python
import asyncio
from hidesay_bot import Bot

async def main():
    async with Bot(pgp_private_key="bot-key.asc", pgp_passphrase=...) as bot:
        await bot.send_message("48213907", "Backup finished.")

asyncio.run(main())
```

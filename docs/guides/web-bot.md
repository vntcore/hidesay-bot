# Web-Bots

A Web-Bot is a web page of yours that the Hidesay app opens inside the bot's chat, from a
button next to the text box or from a link like `https://hidesay.me/yourbot/app`. It runs in
the phone's own browser engine, with a frame around it the page can drive: a main button,
popups, haptics, storage, biometrics, position, the QR scanner and more, all from
`window.Hidesay.WebBot`.

You set it up in the app: open **@bot**, pick the bot, then **Web-Bot**. There goes the address
of the page, the terms people accept before they open it the first time, and the logo and
colours of its loading screen.

This page is about the part that runs on your server: knowing who is using the page, and the
two updates the page can bring the bot. Everything the page itself can do, with its limits and
the questions the app asks the person, is in the {doc}`../openapi-reference/web-bots`.

## Who is on the page

When the app opens the page it hands it `initData`, a string signed by the Hidesay server that
says who opened it, from where and when. The page can read it right away as
`Hidesay.WebBot.initDataUnsafe`, which is fine for showing a name. For anything that matters
(their orders, their account, a payment), send the string itself to your server and check it
there:

```javascript
// on the page
const answer = await fetch("/api/orders", {
  headers: { Authorization: "WebBot " + Hidesay.WebBot.initData },
});
```

```python
# on your server, here with FastAPI
from fastapi import FastAPI, Header, HTTPException

from hidesay_bot import WebBotData, WebBotDataError, validate_web_bot_data

BOT_ID = "B48213907"  # await bot.get_me() gives it: me.id
WEB_BOT_KEY = "..."  # the server's Web-Bot key, see below

app = FastAPI()


def who_is_asking(authorization: str) -> WebBotData:
    init_data = authorization.removeprefix("WebBot ")
    try:
        return validate_web_bot_data(init_data, bot_id=BOT_ID, public_key=WEB_BOT_KEY, max_age=3600)
    except WebBotDataError:
        raise HTTPException(status_code=401) from None  # believe nothing


@app.get("/api/orders")
async def orders(authorization: str = Header()):
    data = who_is_asking(authorization)
    return await load_orders(data.user.id)
```

`validate_web_bot_data` checks three things and returns a `WebBotData` only when all three
hold:

- **the signature**: the data was signed by the Hidesay server and not changed since, not one
  character;
- **the bot**: it was signed for your bot. A string a page got from another bot never passes,
  whatever it says inside;
- **the age**: it was signed less than `max_age` seconds ago, one day unless you say otherwise
  (`WEB_BOT_DATA_MAX_AGE`); `max_age=None` takes any age. Somebody who keeps the page open
  longer gets fresh data by closing it and opening it again.

When one of them fails it raises `WebBotDataError`, and `error.reason` says which:
`"malformed"` (the string isn't `initData` at all), `"signature"`, `"wrong_bot"` or
`"expired"`. Sending the same string again fails the same way.

`data.user.id` is the same ID your bot sees in its updates (`message.from_user.id`), so the
person on the page and the person in the chat are easy to match:

```python
data = who_is_asking(header)
print(data.user.full_name, data.user.language_code, data.chat_type, data.start_param)
```

With a `Bot` running in the same program it is one call, and the bot finds its own ID and the
key by itself, once:

```python
data = await bot.validate_web_bot_data(init_data, max_age=3600)
```

On a `SyncBot` it is the same call, without `await`.

`parse_web_bot_data(init_data)` reads the string without checking anything. It is for a log
line, or to show a name before the check has run: never act on what it returns.

## The key

The key that checks the signature is public, and the same for every bot: only the Hidesay
server holds the private half. `await api.get_web_bot_key()` fetches it (`GET /webbot/key`);
fetch it once and keep it in your configuration. It is 32 bytes, written in base64url.

There is no secret in checking: `validate_web_bot_data` needs no token and makes no network
call. With the `cryptography` package installed it uses it; without, the check runs in plain
Python and takes a few milliseconds.

## What is in initData

The string is form-encoded, like a URL query:

| Field | What it is |
| --- | --- |
| `user` | who opened the page, as JSON: `id`, `firstName`, `lastName`, `username`, `languageCode`, `photoUrl`, `bannerUrl`, `allowsWriteToPm` |
| `chatType` | `private` from the chat with the bot, `link` from a link anywhere else |
| `startParam` | the word of the link that opened it, when there was one: `https://hidesay.me/yourbot/app?startapp=word` |
| `authDate` | when the server signed it, in Unix seconds |
| `botId` | the bot it was signed for |
| `signature` | Ed25519, base64url without padding |

What is signed is `<botId>:WebBotData`, then every other field as `key=value`, sorted by key,
one per line (`hidesay_bot.webbot.check_string` builds it). A newer server may add fields:
they are signed like the rest, and `data.fields` has them.

The start word is 1 to 64 letters, digits, `_` and `-`; a link with anything else in it opens
the page without one. A field of `user` with nothing in it is left out, and the pictures are
left out too when the person has blocked the bot.

`allowsWriteToPm` is `false` when the page was opened from a link by somebody who has no chat
with the bot. The page can ask with `Hidesay.WebBot.requestWriteAccess()`; once they say yes,
the bot can send them messages and reminders in its chat.

## When the person says yes

Two things the page can ask reach the bot as updates, after the app has asked the person:

- `requestWriteAccess()`: on yes, the private chat is made and the bot gets a
  `web_bot_write_access` update, once per person. From then on it can write to them first.
- `requestContact()`: the person shares their phone number, the one verified on their
  account, and the bot gets a `web_bot_contact` update with it. They are asked every time.

This version of the library has no event of its own for these two: they arrive in
`@bot.on_update()` handlers with their JSON in `update.raw`. The example is under
[Updates from the bot's Web-Bot](events.md#updates-from-the-bots-web-bot).

The page also hears about it (`requestContact` hands it the number as `responseUnsafe`), but
only the update is known to come from the server: trust that one.

## Never trust the page alone

- `initDataUnsafe`, and anything else the page says about the user, could have been made up by
  whoever is running it. Only what came back from `validate_web_bot_data` is known.
- Check the data on every request that does something for somebody, or once to start a session
  of your own, with its own expiry.
- The page runs under your domain like any other. The app only talks to the address you set in
  @bot (scheme, host and port), so a page you link to elsewhere doesn't get the frame or the
  data.
- Biometrics on the page (`BiometricManager`) happen on the phone, and the prompt also takes
  the phone's PIN. The Hidesay server sees none of it: to rely on it from your server, keep a
  secret of yours in the biometric token and check it, as the
  {doc}`../openapi-reference/web-bots` explains.

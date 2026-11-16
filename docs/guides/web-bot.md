# Web-Bots

A Web-Bot is a web page of yours that the Hidesay app opens inside the bot's chat, from a
button next to the text box or from a link like `https://hidesay.me/yourbot/app`. It runs in
the phone's own browser engine, with a frame around it the page can drive: a main button,
popups, haptics, storage, the QR scanner and more, all from `window.Hidesay.WebBot`.

You set it up in the app: open **@bot**, pick the bot, then **Web-Bot**. There goes the address
of the page, the terms people accept before they open it the first time, and the logo and
colours of its loading screen.

This page is about the one part that runs on your server: knowing who is using the page.

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
# on your server
from hidesay_bot import WebBotDataError, validate_web_bot_data

BOT_ID = "B48213907"  # await bot.get_me() gives it: me.id
WEB_BOT_KEY = "..."  # the server's Web-Bot key, see below


def who_is_asking(header: str):
    init_data = header.removeprefix("WebBot ")
    try:
        return validate_web_bot_data(init_data, bot_id=BOT_ID, public_key=WEB_BOT_KEY, max_age=3600)
    except WebBotDataError:
        return None  # answer 401, believe nothing
```

`validate_web_bot_data` checks three things and returns a `WebBotData` only when all three
hold:

- **the signature**: the data was signed by the Hidesay server and not changed since, not one
  character;
- **the bot**: it was signed for your bot. A string a page got from another bot never passes,
  whatever it says inside;
- **the age**: it was signed less than `max_age` seconds ago, one day unless you say
  otherwise. Somebody who keeps the page open longer gets fresh data by closing it and opening
  it again.

`data.user.id` is the same ID your bot sees in its updates (`message.from_user.id`), so the
person on the page and the person in the chat are easy to match.

```python
data = who_is_asking(request.headers["authorization"])
if data is None:
    return Response(status_code=401)
print(data.user.full_name, data.user.language_code, data.chat_type, data.start_param)
```

With a `Bot` running in the same program it is one call, and the bot finds its own ID and
the key by itself, once:

```python
data = await bot.validate_web_bot_data(init_data, max_age=3600)
```

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
| `startParam` | the word after `?startapp=` in the link that opened it, when there was one |
| `authDate` | when the server signed it, in Unix seconds |
| `botId` | the bot it was signed for |
| `signature` | Ed25519, base64url without padding |

What is signed is `<botId>:WebBotData`, then every other field as `key=value`, sorted by key,
one per line (`hidesay_bot.webbot.check_string` builds it). A newer server may add fields:
they are signed like the rest, and `data.fields` has them.

`allowsWriteToPm` is `false` when the page was opened from a link by somebody who never wrote
to the bot. The page can ask with `Hidesay.WebBot.requestWriteAccess()`; once they say yes,
the bot can send them messages and reminders in its chat.

## Never trust the page alone

- `initDataUnsafe`, and anything else the page says about the user, could have been made up by
  whoever is running it. Only what came back from `validate_web_bot_data` is known.
- Check the data on every request that does something for somebody, or once to start a session
  of your own, with its own expiry.
- The page runs under your domain like any other. The app only talks to the address you set in
  @bot (scheme, host and port), so a page you link to elsewhere doesn't get the frame or the
  data.

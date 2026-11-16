"""An in-memory Hidesay Bot API for the tests.

It answers like the real server where the SDK depends on it - the envelope,
the codes, which body a chat takes, sendId replays, offsets that confirm,
callbacks that can be answered once - and keeps every request so a test can
look at exactly what went over the wire. It serves both the async and the
blocking client from the same state.

Faults are scripted with :meth:`FakeAPI.fail`: an error envelope, a dropped
connection before the request is handled, or a lost answer after it was.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2

BOT_ID = 10_000_412
TOKEN = f"pgpbot_{BOT_ID}." + "A" * 43


# Ids the way the server writes them since November 2026: a person's eight digits bare,
# a bot's, a group's and a channel's with a letter in front. Tests keep their short numbers
# (chat 7, group 900, anna 55) and these turn them into what goes over the wire.
def person(n: int) -> str:
    return str(10_000_000 + n)


def group(n: int) -> str:
    return f"G{10_000_000 + n}"


def channel(n: int) -> str:
    return f"C{10_000_000 + n}"


_KINDS = {"group": group, "channel": channel}

BOT = f"B{BOT_ID}"
ANNA = person(55)
BRUNO = person(56)
# what add_chat(7) and add_chat(900, "group") make: the private chat most tests talk in, and the group
CHAT = person(7)
GROUP = group(900)
BASE = "https://api.test"
MEDIA_ID = "0f3c5a3e-1111-2222-3333-444455556666"


def ok(result: Any, status: int = 200) -> httpx2.Response:
    return httpx2.Response(status, json={"ok": True, "result": result})


def err(status: int, code: str, message: str = "", **extra: Any) -> httpx2.Response:
    headers = {"Retry-After": str(extra["retryAfter"])} if extra.get("retryAfter") else {}
    body = {"ok": False, "error": {"status": status, "code": code, "message": message or code, **extra}}
    return httpx2.Response(status, json=body, headers=headers)


@dataclass
class Fault:
    method: str
    path: re.Pattern[str]
    times: int
    response: Callable[[], httpx2.Response] | None = None
    network: bool = False
    after: bool = False


@dataclass
class FakeChat:
    id: str
    kind: str = "private"
    title: str | None = None
    username: str | None = None
    history_visible: bool = False
    peer: dict[str, Any] | None = None
    keys: list[dict[str, Any]] = field(default_factory=list)
    members: list[dict[str, Any]] = field(default_factory=list)
    blocked: bool = False
    gone: bool = False
    me: dict[str, Any] = field(default_factory=lambda: {"status": "member", "rights": 255, "restrictedUntil": None})
    # the rest of what GET /chats/{id} says about a group or channel, as the server names it
    settings: dict[str, Any] = field(default_factory=dict)

    def ref(self) -> dict[str, Any]:
        if self.kind == "private":
            return {"id": self.id, "kind": "private"}
        return {"id": self.id, "kind": self.kind, "title": self.title, "username": self.username}


class FakeAPI(httpx2.BaseTransport, httpx2.AsyncBaseTransport):
    """The state of a fake server and the transport that talks to it."""

    def __init__(self, *, token: str = TOKEN) -> None:
        self.token = token
        self.lock = threading.RLock()
        self.me: dict[str, Any] = {
            "id": BOT,
            "username": "testbot",
            "name": "Test",
            "about": None,
            "description": None,
            "mode": "open",
            "fingerprint": None,
            "publicKey": None,
            "usernames": [],
            "allowGroups": True,
            "status": "active",
            "queued": 0,
            "limits": {"textMax": 4096, "pollTimeoutMaxS": 50, "sendsPerSecond": 30, "apiCallsPerMinute": 600},
        }
        self.chats: dict[str, FakeChat] = {}
        self.queue: list[dict[str, Any]] = []
        self.next_update_id = 1
        self.messages: dict[int, dict[str, Any]] = {}
        self.next_message_id = 1000
        self.send_ids: dict[str, int] = {}
        self.callbacks: dict[str, dict[str, Any]] = {}
        self.webhook: dict[str, Any] = {"url": None, "secret": None, "failures": 0, "lastError": None}
        self.commands: dict[str, list[dict[str, str]]] = {}
        self.user_keys: dict[str, dict[str, Any]] = {}
        self.handles: dict[str, dict[str, Any]] = {}
        self.media: dict[str, bytes] = {}
        self.requests: list[dict[str, Any]] = []
        self.faults: list[Fault] = []
        self.revoked = False
        self.web_bot_key = "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo"
        self.poll_wait = 0.05
        self._arrived = threading.Event()
        # what the bot was handed, per chat, as GET /chats/{id}/messages hands it back
        self.handed: dict[str, dict[int, dict[str, Any]]] = {}
        self.stickers: dict[int, dict[str, Any]] = {}
        self.reactions: dict[int, dict[str, str]] = {}
        self.pins: dict[str, list[int]] = {}
        self.profiles: dict[str, dict[str, Any]] = {}
        self.invites: dict[str, list[dict[str, Any]]] = {}
        self.bans: dict[str, list[dict[str, Any]]] = {}
        self.join_requests: dict[str, list[dict[str, Any]]] = {}
        self.read_marks: dict[str, int] = {}
        self.stats: dict[str, Any] = {}

    # --- scripting ---

    def key(self, chat_id: int | str, kind: str | None = None) -> str:
        """The wire id of a chat a test names by its short number (7, 900), or the id itself."""
        if isinstance(chat_id, str):
            return chat_id
        with self.lock:
            for chat in self.chats.values():
                if chat.id.lstrip("GC") == person(chat_id) and kind in (None, chat.kind):
                    return chat.id
        return _KINDS.get(kind or "private", person)(chat_id)

    def add_chat(self, chat_id: int | str, kind: str = "private", **fields: Any) -> FakeChat:
        chat = FakeChat(chat_id if isinstance(chat_id, str) else _KINDS.get(kind, person)(chat_id), kind, **fields)
        if kind == "private" and chat.peer is None:
            chat.peer = {
                "id": ANNA,
                "username": "anna",
                "firstName": "Anna",
                "lastName": None,
                "deleted": False,
                "hasKey": True,
            }
        with self.lock:
            self.chats[chat.id] = chat
        return chat

    def add_handle(
        self, username: str, *, user: dict[str, Any] | None = None, chat: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Puts a handle in the pool a lookup searches, held by a person or by a chat."""
        held = user or chat or {}
        result = {
            "kind": "user" if user else str(held.get("kind", "group")),
            "id": held["id"],
            "username": held.get("username", username),
            "user": user,
            "chat": chat,
        }
        with self.lock:
            self.handles[username.lower()] = result
        return result

    def push(self, kind: str, **payload: Any) -> int:
        with self.lock:
            uid = self.next_update_id
            self.next_update_id += 1
            self.queue.append({"updateId": uid, "kind": kind, **payload})
            self._arrived.set()
        return uid

    def push_message(
        self,
        chat_id: int | str,
        text: str | None = None,
        *,
        armored: str | None = None,
        from_id: int | str | None = 55,
        message_id: int | None = None,
        edited: bool = False,
        media: list[dict[str, Any]] | None = None,
        reply_to: int | None = None,
    ) -> int:
        chat = self.chats[self.key(chat_id)]
        sender = person(from_id) if isinstance(from_id, int) else from_id
        message: dict[str, Any] = {
            "id": message_id or self._new_message_id(),
            "chat": chat.ref(),
            "from": None
            if from_id is None
            else {"id": sender, "username": f"user{from_id}", "firstName": "User", "lastName": None, "isBot": False},
            "date": 1791000000000,
            "replyTo": reply_to,
            "kind": "user",
        }
        if armored is not None:
            message["armored"] = armored
        else:
            message["text"] = text
            message["media"] = media or []
        with self.lock:
            self.handed.setdefault(chat.id, {})[message["id"]] = message
        return self.push("edited_message" if edited else "message", message=message)

    def hand(self, chat_id: int | str, message: dict[str, Any]) -> dict[str, Any]:
        """Files a message as handed to the bot, without an update: history, forwards."""
        with self.lock:
            self.handed.setdefault(self.key(chat_id), {})[message["id"]] = message
        return message

    def push_callback(
        self, chat_id: int | str, message_id: int, data: str, *, from_id: int | str = 55, expires_in: float = 60
    ) -> str:
        callback_id = uuid.uuid4().hex[:22]
        expires = int((time.time() + expires_in) * 1000)
        with self.lock:
            self.callbacks[callback_id] = {"answered": False, "expiresAt": expires, "answer": None}
        self.push(
            "callback_query",
            callback={
                "id": callback_id,
                "from": {
                    "id": person(from_id) if isinstance(from_id, int) else from_id,
                    "username": "anna",
                    "firstName": "Anna",
                    "lastName": None,
                    "isBot": False,
                },
                "chat": self.chats[self.key(chat_id)].ref(),
                "messageId": message_id,
                "data": data,
                "expiresAt": expires,
            },
        )
        return callback_id

    def fail(
        self,
        method: str,
        path: str,
        *,
        status: int = 500,
        code: str = "internal",
        times: int = 1,
        network: bool = False,
        after: bool = False,
        **extra: Any,
    ) -> None:
        """The next ``times`` matching requests fail.

        ``network``: the connection drops before the server sees the request.
        ``after``: the server handles it, then the answer is lost on the way back.
        Otherwise the server answers with an error envelope.
        """
        response = None if network or after else (lambda: err(status, code, **extra))
        with self.lock:
            self.faults.append(Fault(method.upper(), re.compile(path), times, response, network, after))

    def calls(self, method: str | None = None, path: str | None = None) -> list[dict[str, Any]]:
        with self.lock:
            return [
                r
                for r in self.requests
                if (method is None or r["method"] == method.upper()) and (path is None or re.fullmatch(path, r["path"]))
            ]

    # --- transport ---

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        request.read()
        fault = self._fault_for(request)
        if fault is not None and fault.network:
            raise httpx2.ConnectError("connection refused", request=request)
        response = fault.response() if fault is not None and fault.response else self._route(request)
        if fault is not None and fault.after:
            raise httpx2.ReadTimeout("answer lost", request=request)
        return response

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        await request.aread()
        path = urlsplit(str(request.url)).path
        if path.endswith("/updates"):
            # a long poll: wait a little for something to arrive, like the real one does
            query = parse_qs(urlsplit(str(request.url)).query)
            timeout = int(query.get("timeout", ["0"])[0])
            if timeout and not self._pending(query):
                self._arrived.clear()
                deadline = time.monotonic() + min(timeout, self.poll_wait)
                # a threading.Event: updates may be pushed from a handler thread (SyncBot)
                while time.monotonic() < deadline and not self._arrived.is_set():  # noqa: ASYNC110
                    await asyncio.sleep(0.005)
        return self.handle_request(request)

    def _pending(self, query: dict[str, list[str]]) -> bool:
        offset = int(query.get("offset", ["0"])[0])
        with self.lock:
            return any(u["updateId"] >= offset for u in self.queue)

    def _fault_for(self, request: httpx2.Request) -> Fault | None:
        path = urlsplit(str(request.url)).path
        with self.lock:
            for fault in self.faults:
                if fault.times > 0 and fault.method == request.method and fault.path.search(path):
                    fault.times -= 1
                    return fault
        return None

    def _new_message_id(self) -> int:
        with self.lock:
            self.next_message_id += 1
            return self.next_message_id

    # --- the server ---

    def _route(self, request: httpx2.Request) -> httpx2.Response:
        url = urlsplit(str(request.url))
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        body: Any = None
        files: dict[str, bytes] = {}
        content_type = request.headers.get("content-type", "")
        if content_type.startswith("multipart/form-data"):
            files = multipart(request.content, content_type)
        elif request.content:
            try:
                body = json.loads(request.content)
            except ValueError:
                return err(400, "bad_request", "body is not valid json")
        with self.lock:
            self.requests.append(
                {
                    "method": request.method,
                    "path": url.path,
                    "query": query,
                    "json": body,
                    "files": files,
                    "raw": request.content if files else None,
                    "headers": dict(request.headers),
                }
            )
        if url.path.startswith("/media/"):
            data = self.media.get(url.path.rsplit("/", 1)[-1])
            if data is None:
                return httpx2.Response(404, json={"error": {"status": 404, "code": "media_media_not_found2"}})
            return httpx2.Response(200, content=data, headers={"content-type": "application/octet-stream"})
        if not url.path.startswith("/bot/v1/"):
            return err(404, "not_found", "no such method")
        auth = request.headers.get("authorization", "")
        if self.revoked or auth != f"Bearer {self.token}":
            return err(401, "unauthorized", "missing, revoked or invalid bot token")
        path = url.path[len("/bot/v1") :]
        method = request.method
        paused_ok = {("GET", "/me"), ("GET", "/webhook"), ("DELETE", "/webhook"), ("POST", "/token/revoke")}
        if self.me["status"] != "active" and (method, path) not in paused_ok:
            return err(403, "bot_paused", "this bot is paused by its owner")
        with self.lock:
            if files or (method, path) == ("POST", "/attachments"):
                return self._files(method, path, files)
            routed = self._newer(method, path, query, body if isinstance(body, dict) else {})
            if routed is not None:
                return routed
            return self._dispatch(method, path, query, body if isinstance(body, dict) else {})

    def _dispatch(self, method: str, path: str, query: dict[str, str], body: dict[str, Any]) -> httpx2.Response:
        if (method, path) == ("GET", "/me"):
            me = dict(self.me)
            me.pop("publicKey", None)
            me["webhook"] = {
                "url": self.webhook["url"],
                "hasSecret": self.webhook["secret"] is not None,
                "failures": 0,
                "lastError": None,
            }
            me["queued"] = len(self.queue)
            return ok(me)
        if (method, path) == ("GET", "/updates"):
            if self.webhook["url"]:
                return err(409, "webhook_active", "a webhook is set; delete it before polling")
            offset = int(query.get("offset", 0))
            limit = int(query.get("limit", 100))
            if offset > 0:
                self.queue = [u for u in self.queue if u["updateId"] >= offset]
            return ok([u for u in self.queue if u["updateId"] >= offset][:limit])
        if path == "/webhook":
            if method == "GET":
                return ok(
                    {
                        "url": self.webhook["url"],
                        "hasSecret": self.webhook["secret"] is not None,
                        "pending": len(self.queue),
                        "failures": 0,
                        "lastError": None,
                        "lastOkAt": None,
                        "nextAttemptAt": None,
                    }
                )
            if method == "POST":
                if not str(body.get("url", "")).startswith("https://"):
                    return err(400, "bad_webhook", "webhook url must use https")
                self.webhook.update(url=body["url"], secret=body.get("secret"))
                return ok({"url": body["url"], "hasSecret": body.get("secret") is not None, "pending": len(self.queue)})
            self.webhook.update(url=None, secret=None)
            return ok({"url": None, "pending": len(self.queue)})
        if (method, path) == ("PUT", "/key"):
            if "BEGIN PGP PUBLIC KEY BLOCK" not in str(body.get("publicKey")):
                return err(400, "bad_request", "publicKey: not an armored PGP public key")
            self.me.update(mode="protected", fingerprint=body["fingerprint"].upper(), publicKey=body["publicKey"])
            return ok({"fingerprint": body["fingerprint"].upper(), "mode": "protected"})
        if path == "/commands":
            scope = query.get("scope") or body.get("scope") or "default"
            if method == "GET":
                if "scope" in query:
                    return ok(self.commands.get(scope, []))
                return ok([{"scope": s, "commands": c} for s, c in self.commands.items()])
            if method == "PUT":
                self.commands[scope] = list(body.get("commands", []))
                return ok({"scope": scope, "commands": self.commands[scope]})
            self.commands.pop(scope, None)
            return ok({"scope": scope, "commands": []})
        if (method, path) == ("POST", "/messages"):
            return self._send(body)
        m = re.fullmatch(r"/messages/(\d+)", path)
        if m:
            return (
                self._edit(int(m.group(1)), body) if method == "PATCH" else self._delete(int(m.group(1)), query, body)
            )
        m = re.fullmatch(r"/callbacks/([^/]+)/answer", path)
        if m:
            callback = self.callbacks.get(m.group(1))
            if callback is None:
                return err(404, "callback_not_found", "no such callback for this bot")
            if callback["answered"]:
                return err(409, "callback_answered", "this callback was already answered")
            if callback["expiresAt"] <= time.time() * 1000:
                return err(410, "callback_expired", "this callback is too old to answer")
            callback.update(answered=True, answer=body)
            return ok({"answered": True})
        m = re.fullmatch(r"/chats/([^/]+)(/.*)?", path)
        if m:
            return self._chat(method, m.group(1), m.group(2) or "", body)
        m = re.fullmatch(r"/resolve/([^/]+)", path)
        if m:
            name = m.group(1).lower()
            if not re.fullmatch(r"[a-z0-9_]{3,32}", name):
                return err(400, "bad_request", "username: 3-32 characters, letters, digits and underscore")
            found = self.handles.get(name)
            return ok(found) if found else err(404, "username_not_found", "nobody and no chat answers to that name")
        m = re.fullmatch(r"/users/([^/]+)/key", path)
        if m:
            key = self.user_keys.get(m.group(1))
            return ok(key) if key else err(404, "user_not_found", "no such user, or no chat in common")
        if (method, path) == ("POST", "/token/revoke"):
            self.revoked = True
            return ok({"revoked": True})
        if (method, path) == ("GET", "/webbot/key"):
            return ok({"key": self.web_bot_key, "algorithm": "Ed25519"})
        return err(404, "not_found", "no such method")

    def _mode_of(self, chat: FakeChat) -> str:
        if chat.kind == "private":
            return str(self.me["mode"])
        return "open" if chat.history_visible else "protected"

    def _body(self, chat: FakeChat, body: dict[str, Any], *, optional: bool) -> httpx2.Response | str | None:
        expected = "armored" if self._mode_of(chat) == "protected" else "text"
        text, armored = body.get("text"), body.get("armored")
        if text is None and armored is None:
            return None if optional else err(400, "bad_request", f"{expected} is required", expected=expected)
        if expected == "text" and text is None:
            return err(400, "wrong_body_kind", "this chat is readable: send text, not armored", expected="text")
        if expected == "armored" and armored is None:
            return err(
                400, "wrong_body_kind", "this chat is end-to-end encrypted: send armored, not text", expected="armored"
            )
        return str(text if expected == "text" else armored)

    def _view(self, row: dict[str, Any]) -> dict[str, Any]:
        chat = self.chats[row["chatId"]]
        view = {
            "id": row["id"],
            "chat": chat.ref(),
            "date": 1791000000000,
            "replyTo": row.get("replyTo"),
            "markup": row.get("markup"),
        }
        view["armored" if row["body"].startswith("-----BEGIN PGP MESSAGE-----") else "text"] = row["body"]
        return view

    def _send(self, body: dict[str, Any]) -> httpx2.Response:
        chat = self.chats.get(str(body.get("chatId")))
        if chat is None or chat.gone:
            return err(404, "chat_not_found", "no such chat, or the bot is not in it")
        send_id = body.get("sendId")
        if send_id and send_id in self.send_ids:
            row = self.messages[self.send_ids[send_id]]
            if row["chatId"] != chat.id:
                return err(409, "conflict", "sendId was used in another chat")
            return ok(self._view(row), 200)
        if body.get("media") is not None or body.get("sticker") is not None:
            text = self._wrap(chat, body)
        else:
            text = self._body(chat, body, optional=False)
        if isinstance(text, httpx2.Response):
            return text
        if chat.kind == "private" and chat.blocked:
            return err(403, "blocked_by_user", "this user blocked the bot")
        row = {
            "id": self._new_message_id(),
            "chatId": chat.id,
            "body": text,
            "replyTo": body.get("replyTo"),
            "markup": body.get("markup"),
            "mentions": body.get("mentions"),
        }
        self.messages[row["id"]] = row
        self.handed.setdefault(chat.id, {})[row["id"]] = self._as_message(row)
        if send_id:
            self.send_ids[send_id] = row["id"]
        return ok(self._view(row), 201)

    def _wrap(self, chat: FakeChat, body: dict[str, Any]) -> httpx2.Response | str:
        """What the server writes for files and stickers sent into a readable chat."""
        if self._mode_of(chat) == "protected":
            return err(
                400,
                "wrong_body_kind",
                "this chat is end-to-end encrypted: seal the files and the envelope yourself and send armored",
                expected="armored",
            )
        if body.get("sticker") is not None:
            sticker = self.stickers.get(body["sticker"].get("id"))
            if sticker is None:
                return err(404, "sticker_not_found", "no such sticker, or it comes from a pack the bot can't use")
            return MARK_STICKER + json.dumps(
                {
                    "id": sticker["id"],
                    "packId": sticker["packId"],
                    "mediaId": sticker["mediaId"],
                    "emoji": sticker["emoji"],
                }
            )
        items = []
        for item in body["media"]:
            if str(item.get("id", "")).lower() not in self.media:
                return err(
                    400, "bad_media", "media: every id has to be a file this bot uploaded with POST /attachments"
                )
            items.append(
                {
                    "media": item["id"].lower(),
                    "kind": item["kind"],
                    "mime": item.get("mime") or "application/octet-stream",
                    "name": item.get("name"),
                    "size": item.get("size") or 0,
                    "w": item.get("width"),
                    "h": item.get("height"),
                    "dur": item.get("duration"),
                    "plain": True,
                    "thumb": item.get("thumb"),
                    "sha256": item.get("sha256"),
                }
            )
        cap = body.get("text")
        if len(items) == 1:
            return MARK_MEDIA + json.dumps({**items[0], "cap": cap})
        return MARK_ALBUM + json.dumps({"items": items, "cap": cap})

    def _as_message(self, row: dict[str, Any]) -> dict[str, Any]:
        """One of the bot's own messages, as the history hands it back."""
        message: dict[str, Any] = {
            "id": row["id"],
            "chat": self.chats[row["chatId"]].ref(),
            "from": {"id": BOT, "username": "testbot", "firstName": "Test", "lastName": None, "isBot": True},
            "date": 1791000000000,
            "replyTo": row.get("replyTo"),
            "threadRoot": row.get("threadRoot"),
            "kind": "user",
            "markup": row.get("markup"),
        }
        raw = row["body"]
        if raw.startswith("-----BEGIN PGP MESSAGE-----"):
            message["armored"] = raw
        else:
            message.update(read_body(raw))
        return message

    def _files(self, method: str, path: str, files: dict[str, bytes]) -> httpx2.Response:
        """The multipart routes: an attachment, a chat's picture or banner."""
        data = files.get("file")
        if data is None:
            return err(400, "bad_request", "send the file as multipart/form-data, in a field called file")
        if (method, path) == ("POST", "/attachments"):
            media_id = str(uuid.uuid4())
            self.media[media_id] = data
            return ok({"id": media_id, "url": f"/media/{media_id}", "size": len(data)}, 201)
        m = re.fullmatch(r"/chats/([^/]+)/(photo|banner)", path)
        if m and method == "PUT":
            chat = self.chats.get(m.group(1))
            if chat is None or chat.kind == "private":
                return err(404, "chat_not_found", "no such chat, or the bot is not in it")
            if not data.startswith((b"\x89PNG", b"\xff\xd8\xff")):
                return err(415, "bad_request", "png, jpeg, webp, or an mp4/webm clip")
            media_id = str(uuid.uuid4())
            self.media[media_id] = data
            chat.settings[f"{m.group(2) if m.group(2) == 'banner' else 'photo'}Url"] = f"/media/{media_id}"
            mime = "image/png" if data.startswith(b"\x89PNG") else "image/jpeg"
            return ok({"url": f"/media/{media_id}", "mime": mime}, 201)
        return err(404, "not_found", "no such method")

    def _newer(self, method: str, path: str, query: dict[str, str], body: dict[str, Any]) -> httpx2.Response | None:
        """The operations of Bot API 2.0 past the first ones. None for a path that isn't one of them."""
        if (method, path) == ("POST", "/messages/delete"):
            chat_id = str(body.get("chatId"))
            seen = self.handed.get(chat_id, {})
            gone = [i for i in body.get("ids", []) if i in seen or i in self.messages]
            for i in gone:
                seen.pop(i, None)
                self.messages.pop(i, None)
            return ok({"deleted": gone})
        if (method, path) == ("POST", "/messages/forward"):
            return self._forward(body)
        m = re.fullmatch(r"/messages/(\d+)/reaction", path)
        if m:
            chat_id = str(body.get("chatId") or query.get("chatId"))
            message_id = int(m.group(1))
            if message_id not in self.handed.get(chat_id, {}):
                return err(404, "message_not_found", "no such message in this chat")
            marks = self.reactions.setdefault(message_id, {})
            if method == "PUT":
                marks[BOT] = body["emoji"]
                return ok(
                    {"messageId": message_id, "reactions": [{"emoji": e, "mine": u == BOT} for u, e in marks.items()]}
                )
            return ok({"removed": marks.pop(BOT, None) is not None})
        m = re.fullmatch(r"/stickers/(\d+)", path)
        if m:
            sticker = self.stickers.get(int(m.group(1)))
            if sticker is None or (not sticker.get("public", True) and query.get("mediaId") != sticker["mediaId"]):
                return err(404, "sticker_not_found", "no such sticker, or it comes from a pack the bot can't use")
            return ok({k: v for k, v in sticker.items() if k != "public"} | {"url": f"/media/{sticker['mediaId']}"})
        if (method, path) == ("GET", "/chats"):
            return ok({"chats": [self._chat_view(c) for c in self.chats.values() if c.kind != "private"]})
        m = re.fullmatch(r"/users/([^/]+)", path)
        if m and method == "GET":
            profile = self.profiles.get(m.group(1))
            return ok(profile) if profile else err(404, "user_not_found", "no such user, or no chat in common")
        m = re.fullmatch(r"/chats/([^/]+)(/.+)?", path)
        if m:
            chat = self.chats.get(m.group(1))
            if chat is None or chat.gone:
                return err(404, "chat_not_found", "no such chat, or the bot is not in it")
            return self._room(method, chat, m.group(2) or "", query, body)
        return None

    def _chat_view(self, chat: FakeChat) -> dict[str, Any]:
        view: dict[str, Any] = {**chat.ref(), "mode": self._mode_of(chat)}
        if chat.kind == "private":
            view.update(user=chat.peer, blocked=chat.blocked)
            return view
        view.update(
            description=None,
            memberCount=len(chat.members) or 3,
            historyVisible=chat.history_visible,
            slowModeSeconds=0,
            pinnedMessageIds=self.pins.get(chat.id, []),
            me=chat.me,
        )
        view.update(chat.settings)
        return view

    def _forward(self, body: dict[str, Any]) -> httpx2.Response:
        source = self.chats.get(str(body.get("fromChatId")))
        target = self.chats.get(str(body.get("chatId")))
        if source is None or target is None:
            return err(404, "chat_not_found", "no such chat, or the bot is not in it")
        message = self.handed.get(source.id, {}).get(body.get("messageId"))
        if message is None:
            return err(404, "message_not_found", "no such message in this chat")
        if "armored" in message or self._mode_of(source) == "protected" or self._mode_of(target) == "protected":
            return err(400, "chat_is_protected", "an end-to-end encrypted message is forwarded on the bot's side")
        send_id = body.get("sendId")
        if send_id and send_id in self.send_ids:
            return ok(self._view(self.messages[self.send_ids[send_id]]), 200)
        row = {"id": self._new_message_id(), "chatId": target.id, "body": message.get("text") or "", "forward": body}
        self.messages[row["id"]] = row
        if send_id:
            self.send_ids[send_id] = row["id"]
        return ok(self._view(row), 201)

    def _room(
        self, method: str, chat: FakeChat, rest: str, query: dict[str, str], body: dict[str, Any]
    ) -> httpx2.Response | None:
        seen = self.handed.setdefault(chat.id, {})
        if rest == "" and method == "GET":
            return ok(self._chat_view(chat))
        if rest == "" and method == "PATCH":
            chat.settings.update({k: v for k, v in body.items() if k not in ("title", "permissions")})
            if "title" in body:
                chat.title = body["title"]
            if "permissions" in body:
                chat.settings["permissions"] = {**chat.settings.get("permissions", {}), **body["permissions"]}
            return ok(self._chat_view(chat))
        if rest == "/messages" and method == "GET":
            before = int(query.get("before", 0))
            limit = int(query.get("limit", 50))
            listed = sorted(
                (m for i, m in seen.items() if m.get("threadRoot") is None and (not before or i < before)),
                key=lambda m: -m["id"],
            )
            return ok({"messages": listed[:limit]})
        m = re.fullmatch(r"/messages/(\d+)", rest)
        if m and method == "GET":
            found = seen.get(int(m.group(1)))
            return ok(found) if found else err(404, "message_not_found", "no such message in this chat")
        if rest == "/read" and method == "POST":
            if chat.kind != "private" and not chat.me.get("readsAll"):
                return err(403, "reads_all_needed", "only a bot the room lets read every message can mark it read")
            self.read_marks[chat.id] = body.get("upTo") or max(seen, default=0)
            return ok({"lastReadId": self.read_marks[chat.id]})
        if rest == "/pins" and method == "DELETE":
            self.pins[chat.id] = []
            return ok({"pinnedMessageIds": []})
        m = re.fullmatch(r"/pins/(\d+)", rest)
        if m:
            message_id = int(m.group(1))
            if message_id not in seen:
                return err(404, "message_not_found", "no such message in this chat")
            pins = self.pins.setdefault(chat.id, [])
            if method == "PUT" and message_id not in pins:
                pins.append(message_id)
            elif method == "DELETE" and message_id in pins:
                pins.remove(message_id)
            return ok({"pinnedMessageIds": pins})
        m = re.fullmatch(r"/posts/(\d+)/comments", rest)
        if m:
            post = int(m.group(1))
            if post not in seen:
                return err(404, "message_not_found", "no such post in this channel")
            if method == "GET":
                comments = sorted((c for c in seen.values() if c.get("threadRoot") == post), key=lambda c: -c["id"])
                return ok({"open": not chat.settings.get("commentsClosed", False), "messages": comments})
            if chat.settings.get("commentsClosed"):
                return err(403, "comments_closed", "comments are closed here")
            text = self._body(chat, body, optional=False)
            if isinstance(text, httpx2.Response):
                return text
            row = {
                "id": self._new_message_id(),
                "chatId": chat.id,
                "body": text,
                "replyTo": body.get("replyTo"),
                "threadRoot": post,
            }
            self.messages[row["id"]] = row
            seen[row["id"]] = self._as_message(row)
            view = self._view(row)
            view["threadRoot"] = post
            return ok(view, 201)
        m = re.fullmatch(r"/posts/(\d+)", rest)
        if m and method == "PATCH":
            return ok({"messageId": int(m.group(1)), "commentsOff": body["commentsOff"]})
        if rest == "/comments" and method == "PATCH":
            chat.settings.update(commentsEnabled=body["enabled"], commentSigning=body.get("signing", False))
            return ok(self._chat_view(chat))
        if rest in ("/photo", "/banner") and method == "DELETE":
            key = "photoUrl" if rest == "/photo" else "bannerUrl"
            return ok({"removed": chat.settings.pop(key, None) is not None})
        m = re.fullmatch(r"/members/([^/]+)/messages", rest)
        if m and method == "DELETE":
            return ok({"purged": True})
        m = re.fullmatch(r"/members/([^/]+)", rest)
        if m and method in ("PATCH", "DELETE"):
            member = next((x for x in chat.members if x["user"]["id"] == m.group(1)), None)
            if member is None:
                return err(404, "member_not_found", "not a member of this chat")
            if method == "PATCH":
                if body.get("role") == "admin":
                    member.update(status="admin", can=dict.fromkeys(body.get("adminRights", []), True))
                if "restrictions" in body:
                    member.update(status="restricted", restrictedUntil=body.get("restrictedUntil"))
                if "customTitle" in body:
                    member["customTitle"] = body["customTitle"]
                return ok(member)
            chat.members.remove(member)
            banned = bool(body.get("ban") or query.get("ban") == "true")
            if banned:
                self.bans.setdefault(chat.id, []).append(
                    {"user": member["user"], "bannedUntil": body.get("bannedUntil"), "bannedAt": 1791000000000}
                )
            return ok({"removed": True, "banned": banned})
        if rest == "/bans" and method == "GET":
            return ok({"bans": self.bans.get(chat.id, [])})
        m = re.fullmatch(r"/bans/([^/]+)", rest)
        if m and method == "DELETE":
            bans = self.bans.get(chat.id, [])
            kept = [b for b in bans if b["user"]["id"] != m.group(1)]
            self.bans[chat.id] = kept
            return ok({"lifted": len(kept) != len(bans)})
        if rest == "/invites":
            invites = self.invites.setdefault(chat.id, [])
            if method == "GET":
                return ok({"invites": invites})
            if chat.username:
                return err(409, "chat_is_public", "this chat is public: its name is the link")
            code = f"code{len(invites) + 1}"
            invite = {
                "code": code,
                "url": f"https://hidesay.test/join/{code}",
                "label": body.get("label"),
                "expiresAt": body.get("expiresAt"),
                "maxUses": body.get("maxUses"),
                "uses": 0,
                "needsApproval": bool(body.get("needsApproval")),
                "createdAt": 1791000000000,
            }
            invites.append(invite)
            return ok(invite, 201)
        m = re.fullmatch(r"/invites/([^/]+)", rest)
        if m and method == "DELETE":
            invites = self.invites.get(chat.id, [])
            found = next((i for i in invites if i["code"] == m.group(1)), None)
            if found is None:
                return err(404, "invite_not_found", "no such link in this chat")
            invites.remove(found)
            return ok({"revoked": True})
        if rest == "/requests" and method == "GET":
            return ok({"requests": self.join_requests.get(chat.id, [])})
        m = re.fullmatch(r"/requests/([^/]+)", rest)
        if m and method == "POST":
            waiting = self.join_requests.get(chat.id, [])
            found = next((r for r in waiting if r["user"]["id"] == m.group(1)), None)
            if found is None:
                return err(404, "request_not_found", "no request from this user")
            waiting.remove(found)
            return ok({"approved": body["approve"]})
        if rest == "/stats" and method == "GET":
            return ok(self.stats)
        return None

    def _edit(self, message_id: int, body: dict[str, Any]) -> httpx2.Response:
        chat = self.chats.get(str(body.get("chatId")))
        if chat is None:
            return err(404, "chat_not_found", "no such chat, or the bot is not in it")
        row = self.messages.get(message_id)
        if row is None or row["chatId"] != chat.id:
            return err(404, "message_not_found", "no such message in this chat")
        text = self._body(chat, body, optional=True)
        if isinstance(text, httpx2.Response):
            return text
        if text is None and "markup" not in body:
            return err(400, "bad_request", "nothing to change")
        if text is not None:
            row["body"] = text
        if "markup" in body:
            row["markup"] = body["markup"]
        return ok(self._view(row))

    def _delete(self, message_id: int, query: dict[str, str], body: dict[str, Any]) -> httpx2.Response:
        chat_id = str(query.get("chatId") or body.get("chatId") or "")
        if chat_id not in self.chats:
            return err(404, "chat_not_found", "no such chat, or the bot is not in it")
        row = self.messages.pop(message_id, None)
        if row is None:
            return err(404, "message_not_found", "no such message in this chat")
        return ok({"deleted": True})

    def _chat(self, method: str, chat_id: str, rest: str, body: dict[str, Any]) -> httpx2.Response:
        chat = self.chats.get(chat_id)
        if chat is None or chat.gone:
            return err(404, "chat_not_found", "no such chat, or the bot is not in it")
        mode = self._mode_of(chat)
        if rest == "" and method == "GET":
            view: dict[str, Any] = {**chat.ref(), "mode": mode}
            if chat.kind == "private":
                view.update(user=chat.peer, blocked=chat.blocked)
            else:
                view.update(
                    description=None,
                    memberCount=len(chat.members) or 3,
                    historyVisible=chat.history_visible,
                    slowModeSeconds=0,
                    me=chat.me,
                )
            return ok(view)
        if rest == "/keys":
            if mode == "open":
                return err(400, "chat_is_open", "this chat is readable: send text, no keys are needed")
            if chat.kind == "private" and not chat.keys:
                return err(404, "no_user_key", "this user has not published a key")
            result: dict[str, Any] = {"keys": chat.keys}
            if chat.kind != "private":
                result["memberCount"] = len(chat.keys) + 2
            return ok(result)
        if rest.startswith("/members"):
            if chat.kind == "private":
                return err(400, "bad_request", "a private chat has no members")
            m = re.fullmatch(r"/members/([^/]+)", rest)
            if m:
                member = next((x for x in chat.members if x["user"]["id"] == m.group(1)), None)
                return ok(member) if member else err(404, "member_not_found", "not a member of this chat")
            return ok({"members": chat.members, "memberCount": len(chat.members)})
        if rest == "/leave":
            if chat.kind == "private":
                return err(400, "bad_request", "a bot cannot leave a private chat")
            chat.gone = True
            return ok({"left": True})
        if rest == "/typing":
            return ok({"sent": True})
        return err(404, "not_found", "no such method")


MARK_MEDIA = "\x01pgpchat-media-v1\x01"
MARK_ALBUM = "\x01pgpchat-album-v1\x01"
MARK_STICKER = "\x01pgpchat-sticker-v1\x01"


def multipart(content: bytes, content_type: str) -> dict[str, bytes]:
    """The fields of a multipart/form-data body, by name."""
    boundary = content_type.split("boundary=", 1)[1].strip('"').encode()
    fields: dict[str, bytes] = {}
    for part in content.split(b"--" + boundary):
        head, sep, data = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        m = re.search(rb'name="([^"]+)"', head)
        if m:
            fields[m.group(1).decode()] = data.removesuffix(b"\r\n")
    return fields


def read_body(raw: str) -> dict[str, Any]:
    """What the server makes of a readable body: the words, the files, the sticker."""
    out: dict[str, Any] = {"text": raw, "media": [], "sticker": None, "forward": None, "link": None}
    for mark in (MARK_MEDIA, MARK_ALBUM):
        if raw.startswith(mark):
            env = json.loads(raw[len(mark) :])
            items = env["items"] if mark == MARK_ALBUM else [env]
            out["text"] = env.get("cap")
            out["media"] = [
                {
                    "id": i["media"],
                    "kind": i["kind"],
                    "mime": i.get("mime"),
                    "url": f"/media/{i['media']}",
                    "width": i.get("w"),
                    "height": i.get("h"),
                    "duration": i.get("dur"),
                    "name": i.get("name"),
                    "size": i.get("size"),
                    "plain": i.get("plain") is True,
                    "thumb": i.get("thumb"),
                    "sha256": i.get("sha256"),
                }
                for i in items
            ]
    if raw.startswith(MARK_STICKER):
        env = json.loads(raw[len(MARK_STICKER) :])
        out["text"] = None
        out["sticker"] = {**env, "url": f"/media/{env['mediaId']}"}
    return out

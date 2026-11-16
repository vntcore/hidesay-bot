"""Client side copies of the server's rules.

Each check here mirrors one in the server's code, so a request that passes
it is not refused for that reason. Lengths are counted in UTF-16 code units
because that is what JavaScript's `.length` counts: an emoji outside the
BMP is one character to Python and two to the server.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any
from urllib.parse import SplitResult, urlsplit

from .errors import ValidationError

TEXT_MAX = 4096
ARMORED_MAX = 128 * 1024
CALLBACK_ANSWER_MAX = 200
URL_MAX = 1024
PUBLIC_KEY_MAX = 64 * 1024
COMMANDS_PER_SCOPE = 100
COMMAND_DESCRIPTION_MAX = 256
POLL_LIMIT_MAX = 100
POLL_TIMEOUT_MAX = 50
UPLOAD_MAX = 50 * 1024 * 1024
MEDIA_PER_MESSAGE = 10
MENTIONS_MAX = 20
DELETE_BATCH_MAX = 100
EMOJI_MAX = 48
HISTORY_LIMIT_MAX = 100
CHAT_TITLE_MAX = 64
CHAT_DESCRIPTION_MAX = 500
CUSTOM_TITLE_MAX = 16
INVITE_LABEL_MAX = 48
INVITE_USES_MAX = 100_000
SLOW_MODE_MAX = 3600
AUTO_DELETE_MAX = 31 * 24 * 3600
ALLOWED_REACTIONS_MAX = 40
# the spec sets no length for a room's reactions, the server takes 16
ALLOWED_REACTION_MAX = 16
THUMB_MAX = 12_000
MEDIA_NAME_MAX = 255
MEDIA_KINDS = ("image", "video", "audio", "file")
# what one side of a picture or a clip may measure, and how long a clip may run
MEDIA_SIDE_MAX = 20_000
MEDIA_DURATION_MAX = 24 * 3600 * 1000

# the badge's rights and a member's permissions: python name -> the API's name
ADMIN_RIGHTS = {
    "delete_messages": "deleteMessages",
    "ban_users": "banUsers",
    "add_members": "addMembers",
    "pin_messages": "pinMessages",
    "change_info": "changeInfo",
    "manage_admins": "manageAdmins",
    "invite_links": "inviteLinks",
    "post_messages": "postMessages",
    "edit_messages": "editMessages",
    "manage_comments": "manageComments",
    "manage_dividers": "manageDividers",
}
PERMISSIONS = {
    "send_messages": "sendMessages",
    "send_media": "sendMedia",
    "send_voice": "sendVoice",
    "add_members": "addMembers",
    "pin_messages": "pinMessages",
    "change_info": "changeInfo",
    "react": "react",
    "add_dividers": "addDividers",
}

ARMOR_MESSAGE = "-----BEGIN PGP MESSAGE-----"
ARMOR_PUBLIC_BEGIN = "-----BEGIN PGP PUBLIC KEY BLOCK-----"
ARMOR_PUBLIC_END = "-----END PGP PUBLIC KEY BLOCK-----"

# \p{Cc}: C0, DEL and C1. Messages may keep \t and \n, nothing else may.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_CONTROL_IN_TEXT = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
# what String.prototype.trim() removes, which is not quite str.strip(): JS also
# drops U+FEFF, python drops a few C0 separators JS keeps
JS_SPACE = "".join(
    map(
        chr,
        (
            0x20,
            0x09,
            0x0A,
            0x0B,
            0x0C,
            0x0D,
            0xA0,
            0x1680,
            *range(0x2000, 0x200B),
            0x2028,
            0x2029,
            0x202F,
            0x205F,
            0x3000,
            0xFEFF,
        ),
    )
)
_JS_SPACE_RUN = re.compile("[" + re.escape(JS_SPACE) + "]+")

# use these with fullmatch: python's $ also matches before a final newline and
# \d takes any unicode digit, where the server's javascript does neither
COMMAND_RE = re.compile(r"[a-z0-9_]{1,32}")
SCOPE_RE = re.compile(r"default|private|groups|groupAdmins|chat:[GC][1-9][0-9]{7}")
SECRET_RE = re.compile(r"[A-Za-z0-9_-]{1,256}")
HANDLE_RE = re.compile(r"[A-Za-z0-9_]{3,32}")
FINGERPRINT_RE = re.compile(r"[0-9A-Fa-f]{40}|[0-9A-Fa-f]{64}")
# what new URL() strips from both ends before it parses
_URL_EDGES = "".join(map(chr, range(0x21)))


def utf16_len(text: str) -> int:
    # surrogatepass: a lone surrogate (say, from a surrogateescape'd file name) is one unit to JS too
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def js_trim(text: str) -> str:
    return text.strip(JS_SPACE)


def has_control(text: str) -> bool:
    return _CONTROL.search(text) is not None


def normalize_newlines(text: str) -> str:
    # the server refuses \r outright, and a bot that reads a file written on
    # windows would otherwise get a 400 it can't make sense of
    return text.replace("\r\n", "\n").replace("\r", "\n")


def check_id(value: object, field: str) -> int:
    # bool is an int subclass: chat_id=True must not become chat 1
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationError(f"{field} must be a positive integer, got {value!r}", field=field)
    return value


# people, bots, groups and channels: eight digits, and a letter for all but people
# eight digits that do not start with 0: 10000000-99999999, the pool the server draws from
CHAT_ID_RE = re.compile(r"[GC]?[1-9][0-9]{7}")
USER_ID_RE = re.compile(r"B?[1-9][0-9]{7}")
ID_MIN = 10_000_000
ID_MAX = 99_999_999


def _public_id(value: object, field: str, shape: re.Pattern[str], example: str) -> str:
    # a bare number is a person's id, which is the one kind that has no letter
    if isinstance(value, int) and not isinstance(value, bool) and ID_MIN <= value <= ID_MAX:
        return str(value)
    if isinstance(value, str):
        text = value.strip().upper()
        if shape.fullmatch(text):
            return text
    raise ValidationError(f"{field} must be an id like {example}, got {value!r}", field=field)


def check_chat_id(value: object, field: str = "chat_id") -> str:
    return _public_id(value, field, CHAT_ID_RE, "'G48213907', 'C48213907' or '48213907'")


def check_user_id(value: object, field: str = "user_id") -> str:
    return _public_id(value, field, USER_ID_RE, "'48213907' or 'B48213907'")


def check_text(text: object, *, field: str = "text", limit: int = TEXT_MAX) -> str:
    if not isinstance(text, str):
        raise ValidationError(f"{field} must be a string, got {type(text).__name__}", field=field)
    text = normalize_newlines(text)
    if not js_trim(text):
        raise ValidationError(f"{field} must not be empty", field=field)
    if utf16_len(text) > limit:
        raise ValidationError(
            f"{field} is {utf16_len(text)} characters long (UTF-16), the limit is {limit}", field=field
        )
    found = _CONTROL_IN_TEXT.search(text)
    if found:
        raise ValidationError(f"{field} contains the control character U+{ord(found.group()):04X}", field=field)
    return text


def check_armored(armored: object, *, field: str = "armored", limit: int = ARMORED_MAX) -> str:
    if not isinstance(armored, str) or ARMOR_MESSAGE not in armored:
        raise ValidationError(f"{field} must be an ASCII-armored PGP MESSAGE", field=field)
    if len(armored) > limit:
        raise ValidationError(f"{field} is {len(armored)} bytes, the limit is {limit}", field=field)
    return armored


def check_send_id(value: object) -> str:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, str):
        try:
            return str(uuid.UUID(value))
        except ValueError:
            pass
    raise ValidationError(f"send_id must be a UUID, got {value!r}", field="send_id")


def _split_http_url(url: object, *, field: str, limit: int) -> SplitResult:
    if not isinstance(url, str) or not url:
        raise ValidationError(f"{field} must be a non-empty string", field=field)
    if utf16_len(url) > limit:
        raise ValidationError(f"{field} is longer than {limit} characters", field=field)
    # the server parses with new URL(), which forgives more than urlsplit: it trims
    # spaces and controls at the ends, drops tabs and newlines, and for http(s)
    # takes any run of / or \ after the colon as the start of the host
    # ("https:example.org" is fine). Refuse only what it refuses for sure
    cleaned = url.strip(_URL_EDGES).replace("\t", "").replace("\n", "").replace("\r", "")
    scheme, colon, rest = cleaned.partition(":")
    if not colon or scheme.lower() not in ("http", "https"):
        raise ValidationError(f"{field} must be an absolute http or https URL", field=field)
    try:
        parts = urlsplit(f"{scheme.lower()}://{rest.replace(chr(92), '/').lstrip('/')}")
        port = parts.port  # raises on a junk port
    except ValueError as exc:
        raise ValidationError(f"{field} is not a valid URL: {exc}", field=field) from None
    del port
    if not parts.hostname:
        raise ValidationError(f"{field} must be an absolute http or https URL", field=field)
    if parts.username or parts.password:
        raise ValidationError(f"{field} must not carry credentials", field=field)
    return parts


def check_http_url(url: object, *, field: str, limit: int = URL_MAX) -> str:
    """An absolute http(s) URL with no user or password in it, as `new URL()` on the server sees it."""
    _split_http_url(url, field=field, limit=limit)
    assert isinstance(url, str)
    return url


def check_webhook_url(url: object) -> str:
    if _split_http_url(url, field="url", limit=URL_MAX).fragment:
        raise ValidationError("url must not carry a fragment", field="url")
    assert isinstance(url, str)
    # https is the server's call: a server set up for local development can allow http,
    # and anything else is refused with bad_webhook
    return url


def check_secret(secret: object) -> str:
    if not isinstance(secret, str) or not SECRET_RE.fullmatch(secret):
        raise ValidationError("secret must be 1-256 characters of A-Z, a-z, 0-9, _ and -", field="secret")
    return secret


def check_scope(scope: object) -> str:
    if scope is None or scope == "":
        return "default"
    if isinstance(scope, str) and SCOPE_RE.fullmatch(scope):
        return scope
    raise ValidationError(
        f"unknown scope {scope!r}: use default, private, groups, groupAdmins or chat:<id> (chat:G48213907)",
        field="scope",
    )


def clean_command(command: object) -> str:
    raw = command if isinstance(command, str) else ""
    name = js_trim(raw).lstrip("/").lower()
    if not COMMAND_RE.fullmatch(name):
        raise ValidationError(
            f"invalid command {raw!r}: 1-32 lowercase letters, digits and underscores",
            field="commands",
        )
    return name


def clean_command_description(description: object, command: str) -> str:
    # the server folds every run of whitespace into one space before it
    # measures, so do the same or the length check disagrees with it
    text = js_trim(_JS_SPACE_RUN.sub(" ", description if isinstance(description, str) else ""))
    if not 1 <= utf16_len(text) <= COMMAND_DESCRIPTION_MAX:
        raise ValidationError(
            f"description of /{command} must be 1-{COMMAND_DESCRIPTION_MAX} characters",
            field="commands",
        )
    if has_control(text):
        raise ValidationError(f"description of /{command} contains control characters", field="commands")
    return text


def check_username(value: object) -> str:
    # people write handles with the @ on, because that is how the app shows
    # them, and the path wants it off
    raw = value if isinstance(value, str) else ""
    name = js_trim(raw).lstrip("@")
    if not HANDLE_RE.fullmatch(name):
        raise ValidationError(
            f"invalid username {raw!r}: 3-32 characters, letters, digits and underscore",
            field="username",
        )
    return name


def check_fingerprint(value: object) -> str:
    if not isinstance(value, str) or not FINGERPRINT_RE.fullmatch(value.strip()):
        raise ValidationError("fingerprint must be 40 (v4) or 64 (v6) hex characters", field="fingerprint")
    return value.strip().upper()


def check_public_key(value: object) -> str:
    if not isinstance(value, str) or ARMOR_PUBLIC_BEGIN not in value or ARMOR_PUBLIC_END not in value:
        raise ValidationError("public_key must be an ASCII-armored PGP PUBLIC KEY BLOCK", field="public_key")
    if len(value) > PUBLIC_KEY_MAX:
        raise ValidationError(f"public_key is longer than {PUBLIC_KEY_MAX} characters", field="public_key")
    if "PRIVATE KEY" in value:
        # never, under any circumstance, let a private block leave the process
        raise ValidationError("that looks like a private key: only the public key is published", field="public_key")
    return value


def check_poll(offset: object, limit: object, timeout: object) -> dict[str, int]:
    params: dict[str, int] = {}
    if offset is not None:
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValidationError("offset must be an integer >= 0", field="offset")
        params["offset"] = offset
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= POLL_LIMIT_MAX:
            raise ValidationError(f"limit must be between 1 and {POLL_LIMIT_MAX}", field="limit")
        params["limit"] = limit
    if timeout is not None:
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 0 <= timeout <= POLL_TIMEOUT_MAX:
            raise ValidationError(f"timeout must be between 0 and {POLL_TIMEOUT_MAX} seconds", field="timeout")
        params["timeout"] = timeout
    return params


def check_callback_answer(text: object, url: object) -> tuple[str | None, str | None]:
    clean: str | None = None
    if text is not None:
        if not isinstance(text, str):
            raise ValidationError("text must be a string", field="text")
        if utf16_len(text) > CALLBACK_ANSWER_MAX:
            raise ValidationError(f"text is longer than {CALLBACK_ANSWER_MAX} characters", field="text")
        clean = js_trim(text) or None
        # here even a newline is refused: the answer is one line flashed over the chat
        if clean and has_control(clean):
            raise ValidationError("text must be a single line with no control characters", field="text")
    # the server reads an empty url as none at all
    link = check_http_url(url, field="url") if url is not None and url != "" else None
    return clean, link


# --- files, stickers, reactions ---

UUID_RE = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")
# javascript's \w is ascii only
MIME_RE = re.compile(r"[A-Za-z0-9_.+-]+/[A-Za-z0-9_.+-]+")
SHA256_RE = re.compile(r"[0-9A-Fa-f]{64}")
BASE64_RE = re.compile(r"[A-Za-z0-9+/=]+")


def check_media_id(value: object, field: str = "media") -> str:
    if not isinstance(value, str) or not UUID_RE.fullmatch(value.strip()):
        raise ValidationError(f"{field} must be the id of an uploaded file (a UUID), got {value!r}", field=field)
    return value.strip().lower()


def check_caption(text: object, *, limit: int = TEXT_MAX) -> str | None:
    """The text under a file: the rules of a text, but it may be blank, and then there is none."""
    if text is None:
        return None
    if not isinstance(text, str):
        raise ValidationError(f"caption must be a string, got {type(text).__name__}", field="caption")
    text = normalize_newlines(text)
    if not js_trim(text):
        return None
    if utf16_len(text) > limit:
        raise ValidationError(
            f"caption is {utf16_len(text)} characters long (UTF-16), the limit is {limit}", field="caption"
        )
    found = _CONTROL_IN_TEXT.search(text)
    if found:
        raise ValidationError(f"caption contains the control character U+{ord(found.group()):04X}", field="caption")
    return text


def _whole(value: object, field: str, top: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= top:
        raise ValidationError(f"{field} must be a positive integer up to {top}, got {value!r}", field=field)
    return value


def check_media_side(value: object, field: str) -> int:
    return _whole(value, field, MEDIA_SIDE_MAX)


def check_media_name(value: object, field: str = "name") -> str:
    if not isinstance(value, str) or not value or len(value) > MEDIA_NAME_MAX:
        raise ValidationError(f"{field} must be a file name of 1-{MEDIA_NAME_MAX} characters", field=field)
    if has_control(value) or "/" in value or "\\" in value:
        raise ValidationError(f"{field} must not hold control characters or slashes", field=field)
    return value


def check_mime(value: object, field: str = "mime") -> str:
    if not isinstance(value, str) or len(value) > 128 or not MIME_RE.fullmatch(value):
        raise ValidationError(f"{field} must be a MIME type like image/jpeg, got {value!r}", field=field)
    return value


def check_media_kind(value: object, field: str = "kind") -> str:
    if value not in MEDIA_KINDS:
        raise ValidationError(f"{field} must be one of {', '.join(MEDIA_KINDS)}, got {value!r}", field=field)
    assert isinstance(value, str)
    return value


def check_thumb(value: object, field: str = "thumb") -> str:
    if not isinstance(value, str) or not value or len(value) > THUMB_MAX or not BASE64_RE.fullmatch(value):
        raise ValidationError(f"{field} must be a base64 jpeg of at most {THUMB_MAX} characters", field=field)
    return value


def check_sha256(value: object, field: str = "sha256") -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ValidationError(f"{field} must be 64 hex characters", field=field)
    return value.lower()


def check_media_in(item: Mapping[str, Any], where: str) -> dict[str, Any]:
    """One file of the ``media`` of a send, in the API's names, as the server's envelope writer checks it."""
    extra = set(item) - {"id", "kind", "mime", "name", "size", "width", "height", "duration", "thumb", "sha256"}
    if extra:
        raise ValidationError(f"{where}: unknown field {min(extra)!r}", field="media")
    out: dict[str, Any] = {
        "id": check_media_id(item.get("id"), f"{where}.id"),
        "kind": check_media_kind(item.get("kind"), f"{where}.kind"),
    }
    checks: dict[str, Any] = {
        "mime": lambda v: check_mime(v, f"{where}.mime"),
        "name": lambda v: check_media_name(v, f"{where}.name"),
        "size": lambda v: _whole(v, f"{where}.size", 2**53 - 1),
        "width": lambda v: _whole(v, f"{where}.width", MEDIA_SIDE_MAX),
        "height": lambda v: _whole(v, f"{where}.height", MEDIA_SIDE_MAX),
        "duration": lambda v: _whole(v, f"{where}.duration", MEDIA_DURATION_MAX),
        "thumb": lambda v: check_thumb(v, f"{where}.thumb"),
        "sha256": lambda v: check_sha256(v, f"{where}.sha256"),
    }
    for key, check in checks.items():
        if item.get(key) is not None:
            out[key] = check(item[key])
    return out


def check_media_list(items: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    listed = list(items)
    if not 1 <= len(listed) <= MEDIA_PER_MESSAGE:
        raise ValidationError(f"media holds 1 to {MEDIA_PER_MESSAGE} files", field="media")
    try:
        out = [check_media_in(item, f"media[{i}]") for i, item in enumerate(listed)]
    except ValidationError as exc:
        # the message names the file and the field, the parameter at fault is media
        raise ValidationError(str(exc), field="media") from None
    if len(out) > 1 and any(m["kind"] not in ("image", "video") for m in out):
        raise ValidationError("an album holds pictures and videos only: send other files one by one", field="media")
    return out


def check_upload(data: object, *, limit: int = UPLOAD_MAX) -> bytes:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ValidationError(f"a file is bytes, got {type(data).__name__}", field="data")
    payload = bytes(data)
    if len(payload) > limit:
        raise ValidationError(f"the file is {len(payload)} bytes, the limit is {limit}", field="data")
    return payload


def check_sticker_id(value: object) -> int:
    return check_id(value, "sticker")


def check_emoji(value: object) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"emoji must be a string, got {type(value).__name__}", field="emoji")
    emoji = js_trim(value)
    if not emoji or utf16_len(emoji) > EMOJI_MAX:
        raise ValidationError(f"emoji must be 1-{EMOJI_MAX} characters", field="emoji")
    return emoji


def check_mentions(values: Iterable[object]) -> list[str]:
    ids = [check_user_id(v, "mentions") for v in values]
    if len(ids) > MENTIONS_MAX:
        raise ValidationError(f"at most {MENTIONS_MAX} mentions", field="mentions")
    return ids


def check_message_ids(values: Iterable[object], field: str = "message_ids") -> list[int]:
    ids = [check_id(v, field) for v in values]
    if not 1 <= len(ids) <= DELETE_BATCH_MAX:
        raise ValidationError(f"{field} holds 1 to {DELETE_BATCH_MAX} ids", field=field)
    return ids


def check_page(before: object, limit: object) -> dict[str, int]:
    params: dict[str, int] = {}
    if before is not None:
        if isinstance(before, bool) or not isinstance(before, int) or before < 0:
            raise ValidationError("before must be a message id, or 0", field="before")
        params["before"] = before
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= HISTORY_LIMIT_MAX:
            raise ValidationError(f"limit must be between 1 and {HISTORY_LIMIT_MAX}", field="limit")
        params["limit"] = limit
    return params


# --- running a room ---


def check_names(values: Iterable[object], table: Mapping[str, str], field: str) -> list[str]:
    """Rights or permissions by name, in python's spelling or the API's, out in the API's."""
    if isinstance(values, (str, bytes)):
        raise ValidationError(f"{field} is a list of names, not one string", field=field)
    known = set(table.values())
    out: list[str] = []
    for value in values:
        name = table.get(value, value) if isinstance(value, str) else value
        if name not in known:
            raise ValidationError(
                f"{field}: unknown name {value!r}, use one of {', '.join(sorted(table))}", field=field
            )
        assert isinstance(name, str)
        if name not in out:
            out.append(name)
    return out


def check_flags(values: Mapping[str, object], table: Mapping[str, str], field: str) -> dict[str, bool]:
    out: dict[str, bool] = {}
    for key, value in values.items():
        [name] = check_names([key], table, field)
        if not isinstance(value, bool):
            raise ValidationError(f"{field}.{key} must be True or False", field=field)
        out[name] = value
    if not out:
        raise ValidationError(f"{field} names nothing to change", field=field)
    return out


def unix_ms(value: object, field: str) -> int:
    """A moment as the API writes it: unix milliseconds, from a datetime or from the number itself."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValidationError(f"{field} must be an aware datetime (with a timezone)", field=field)
        return round(value.timestamp() * 1000)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationError(f"{field} must be a datetime or unix milliseconds, got {value!r}", field=field)
    return value


def check_seconds(value: object, field: str, top: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= top:
        raise ValidationError(f"{field} must be between 0 and {top} seconds", field=field)
    return value


def check_chat_title(value: object) -> str:
    if not isinstance(value, str) or not 1 <= utf16_len(js_trim(value)) <= CHAT_TITLE_MAX:
        raise ValidationError(f"title must be 1-{CHAT_TITLE_MAX} characters", field="title")
    return js_trim(value)


def check_chat_description(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or utf16_len(js_trim(value)) > CHAT_DESCRIPTION_MAX:
        raise ValidationError(f"description must be at most {CHAT_DESCRIPTION_MAX} characters", field="description")
    return js_trim(value)


def check_custom_title(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or utf16_len(value) > CUSTOM_TITLE_MAX or has_control(value):
        raise ValidationError(
            f"custom_title must be at most {CUSTOM_TITLE_MAX} characters, on one line", field="custom_title"
        )
    return value


def check_invite_code(value: object) -> str:
    # a path segment: "." and ".." would be read as steps of the path
    if not isinstance(value, str) or not value or len(value) > 64 or value in (".", "..") or has_control(value):
        raise ValidationError(f"code must be the code of an invite link, got {value!r}", field="code")
    return value

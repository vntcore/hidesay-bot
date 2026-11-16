"""validate_web_bot_data: the one check standing between a page and the bot's trust."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from typing import Any
from urllib.parse import urlencode

import pytest

from hidesay_bot import (
    ConfigurationError,
    WebBotDataError,
    parse_web_bot_data,
    validate_web_bot_data,
)
from hidesay_bot import _ed25519 as ed
from hidesay_bot.webbot import check_string

from .conftest import fixture_text

# RFC 8032, section 7.1, tests 1 to 3: public key, message, signature
RFC_VECTORS = [
    (
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        "",
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
    ),
    (
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        "72",
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
    ),
    (
        "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
        "af82",
        "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a",
    ),
]

SEED = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
OTHER_SEED = bytes(range(32))
BOT = "B48213907"
NOW = 1_800_000_000


# --- a signer, for the tests only: the library never signs ---


def _encode(point: tuple[int, int, int, int]) -> bytes:
    inverse = pow(point[2], ed._P - 2, ed._P)
    x = point[0] * inverse % ed._P
    y = point[1] * inverse % ed._P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _sign(seed: bytes, message: bytes) -> tuple[bytes, bytes]:
    digest = hashlib.sha512(seed).digest()
    secret = int.from_bytes(digest[:32], "little")
    secret &= (1 << 254) - 8
    secret |= 1 << 254
    public = _encode(ed._multiply(secret, ed._BASE))
    r = int.from_bytes(hashlib.sha512(digest[32:] + message).digest(), "little") % ed._L
    big_r = _encode(ed._multiply(r, ed._BASE))
    k = int.from_bytes(hashlib.sha512(big_r + public + message).digest(), "little") % ed._L
    return public, big_r + ((r + k * secret) % ed._L).to_bytes(32, "little")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _user(**extra: Any) -> str:
    return json.dumps({"id": "48213907", "firstName": "Anna", "username": "anna", **extra}, separators=(",", ":"))


def _fields(**extra: str) -> dict[str, str]:
    return {"user": _user(), "chatType": "private", "authDate": str(NOW - 60), "botId": BOT, **extra}


def _init_data(fields: dict[str, str], *, seed: bytes = SEED, signed_for: str = BOT) -> tuple[str, str]:
    public, signature = _sign(seed, check_string(signed_for, fields).encode())
    return urlencode({**fields, "signature": _b64(signature)}), _b64(public)


# --- Ed25519 ---


def test_the_test_signer_is_rfc_8032() -> None:
    public, signature = _sign(SEED, b"")
    assert public.hex() == RFC_VECTORS[0][0]
    assert signature.hex() == RFC_VECTORS[0][2]


@pytest.mark.parametrize(("public", "message", "signature"), RFC_VECTORS)
def test_rfc_8032_vectors_verify(public: str, message: str, signature: str) -> None:
    assert ed.verify(bytes.fromhex(public), bytes.fromhex(signature), bytes.fromhex(message), pure=True)


@pytest.mark.parametrize(("public", "message", "signature"), RFC_VECTORS)
def test_one_flipped_bit_anywhere_fails(public: str, message: str, signature: str) -> None:
    key, msg, sig = bytes.fromhex(public), bytes.fromhex(message), bytearray(bytes.fromhex(signature))
    assert not ed.verify(key, bytes(sig), msg + b"!", pure=True)
    for at in (0, 31, 32, 63):
        flipped = bytearray(sig)
        flipped[at] ^= 0x01
        assert not ed.verify(key, bytes(flipped), msg, pure=True)
    other = bytearray(key)
    other[0] ^= 0x01
    assert not ed.verify(bytes(other), bytes(sig), msg, pure=True)


def test_a_signature_with_s_past_the_order_is_refused() -> None:
    public, message, signature = (bytes.fromhex(x) for x in RFC_VECTORS[0])
    s = int.from_bytes(signature[32:], "little")
    # s + L passes the curve equation; RFC 8032 refuses it so signatures stay unique
    malleable = signature[:32] + (s + ed._L).to_bytes(32, "little")
    assert not ed.verify(public, malleable, message, pure=True)


def test_malformed_keys_and_signatures_are_false_not_errors() -> None:
    public, message, signature = (bytes.fromhex(x) for x in RFC_VECTORS[0])
    assert not ed.verify(public[:31], signature, message, pure=True)
    assert not ed.verify(public, signature[:63], message, pure=True)
    # y = p is not a field element, and this y has no x on the curve
    assert not ed.verify((2**255 - 19).to_bytes(32, "little"), signature, message, pure=True)
    assert not ed.verify((2).to_bytes(32, "little"), signature, message, pure=True)


def test_the_fast_path_agrees_when_cryptography_is_there() -> None:
    pytest.importorskip("cryptography")
    for public, message, signature in RFC_VECTORS:
        key, msg, sig = bytes.fromhex(public), bytes.fromhex(message), bytes.fromhex(signature)
        assert ed.verify(key, sig, msg) is True
        assert ed.verify(key, sig, msg + b"x") is False


# --- initData ---


def test_good_data_reads_back_whole() -> None:
    init_data, key = _init_data(_fields(startParam="ref_42"))
    data = validate_web_bot_data(init_data, bot_id=BOT, public_key=key, now=NOW)
    assert data.user.id == "48213907"
    assert data.user.first_name == "Anna"
    assert data.user.username == "anna"
    assert data.user.allows_write_to_pm is False
    assert data.bot_id == BOT
    assert data.chat_type == "private"
    assert data.start_param == "ref_42"
    assert data.auth_date.timestamp() == NOW - 60
    assert data.fields["startParam"] == "ref_42"
    assert "signature" in data.fields


def test_the_key_can_be_raw_bytes_or_padded_or_plain_base64() -> None:
    init_data, key = _init_data(_fields())
    raw = base64.urlsafe_b64decode(key + "=")
    for form in (raw, key, key + "=", base64.b64encode(raw).decode()):
        assert validate_web_bot_data(init_data, bot_id=BOT, public_key=form, now=NOW).bot_id == BOT


def test_names_with_spaces_and_accents_survive_the_encoding() -> None:
    fields = _fields(user=_user(firstName="Zoë Anne", lastName="D'Amico"))
    init_data, key = _init_data(fields)
    assert "+" in init_data
    data = validate_web_bot_data(init_data, bot_id=BOT, public_key=key, now=NOW)
    assert data.user.full_name == "Zoë Anne D'Amico"


def test_any_change_to_a_signed_field_breaks_it() -> None:
    init_data, key = _init_data(_fields())
    for before, after in (("chatType=private", "chatType=link"), ("%22anna%22", "%22mallory%22")):
        changed = init_data.replace(before, after)
        assert changed != init_data
        with pytest.raises(WebBotDataError) as caught:
            validate_web_bot_data(changed, bot_id=BOT, public_key=key, now=NOW)
        assert caught.value.reason == "signature"


def test_a_field_added_after_signing_breaks_it() -> None:
    init_data, key = _init_data(_fields())
    with pytest.raises(WebBotDataError) as caught:
        validate_web_bot_data(init_data + "&startParam=admin", bot_id=BOT, public_key=key, now=NOW)
    assert caught.value.reason == "signature"


def test_data_signed_for_another_bot_never_passes() -> None:
    # signed for B2 in every way: the prefix and the field
    init_data, key = _init_data(_fields(botId="B2"), signed_for="B2")
    with pytest.raises(WebBotDataError) as caught:
        validate_web_bot_data(init_data, bot_id=BOT, public_key=key, now=NOW)
    assert caught.value.reason == "signature"
    assert validate_web_bot_data(init_data, bot_id="B2", public_key=key, now=NOW).bot_id == "B2"


def test_a_field_naming_another_bot_is_caught_too() -> None:
    init_data, key = _init_data(_fields(botId="B2"), signed_for=BOT)
    with pytest.raises(WebBotDataError) as caught:
        validate_web_bot_data(init_data, bot_id=BOT, public_key=key, now=NOW)
    assert caught.value.reason == "wrong_bot"


def test_another_key_is_another_signer() -> None:
    init_data, _ = _init_data(_fields())
    _, stranger = _init_data(_fields(), seed=OTHER_SEED)
    with pytest.raises(WebBotDataError) as caught:
        validate_web_bot_data(init_data, bot_id=BOT, public_key=stranger, now=NOW)
    assert caught.value.reason == "signature"


def test_old_data_is_refused_unless_any_age_is_asked_for() -> None:
    init_data, key = _init_data(_fields(authDate=str(NOW - 90_000)))
    with pytest.raises(WebBotDataError) as caught:
        validate_web_bot_data(init_data, bot_id=BOT, public_key=key, now=NOW)
    assert caught.value.reason == "expired"
    assert validate_web_bot_data(init_data, bot_id=BOT, public_key=key, now=NOW, max_age=None)
    with pytest.raises(WebBotDataError):
        validate_web_bot_data(_init_data(_fields())[0], bot_id=BOT, public_key=key, now=NOW, max_age=30)


@pytest.mark.parametrize(
    "init_data",
    [
        "",
        "user=x",
        "no-equals-sign",
        "a=1&a=2&signature=x",
        "x" * 20_000,
        "user=%7B%7D&authDate=1&botId=B1&signature=@@@",
        "user=%7B%7D&authDate=1&botId=B1&signature=" + "A" * 10,
    ],
)
def test_strings_that_are_not_init_data_are_malformed(init_data: str) -> None:
    _, key = _init_data(_fields())
    with pytest.raises(WebBotDataError) as caught:
        validate_web_bot_data(init_data, bot_id=BOT, public_key=key, now=NOW)
    assert caught.value.reason in {"malformed", "signature"}


def test_a_duplicated_key_is_refused_even_when_one_copy_was_signed() -> None:
    init_data, key = _init_data(_fields())
    with pytest.raises(WebBotDataError) as caught:
        validate_web_bot_data(init_data + "&chatType=link", bot_id=BOT, public_key=key, now=NOW)
    assert caught.value.reason == "malformed"


def test_signed_data_without_a_usable_user_is_still_refused() -> None:
    init_data, key = _init_data({"user": json.dumps({"firstName": "No id"}), "authDate": str(NOW), "botId": BOT})
    with pytest.raises(WebBotDataError) as caught:
        validate_web_bot_data(init_data, bot_id=BOT, public_key=key, now=NOW)
    assert caught.value.reason == "malformed"


@pytest.mark.parametrize("key", ["not base64 !!", _b64(b"short"), _b64(bytes(33))])
def test_a_key_that_is_not_one_is_a_setup_error(key: str) -> None:
    init_data, _ = _init_data(_fields())
    with pytest.raises(ConfigurationError):
        validate_web_bot_data(init_data, bot_id=BOT, public_key=key, now=NOW)


def test_wrong_types_are_type_errors() -> None:
    init_data, key = _init_data(_fields())
    with pytest.raises(TypeError):
        validate_web_bot_data(init_data.encode(), bot_id=BOT, public_key=key)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        validate_web_bot_data(init_data, bot_id="", public_key=key)
    with pytest.raises(TypeError):
        validate_web_bot_data(init_data, bot_id=BOT, public_key=42)  # type: ignore[arg-type]


def test_parse_reads_without_checking() -> None:
    fields = _fields(user=_user(allowsWriteToPm=True, photoUrl="https://x/p.jpg"))
    data = parse_web_bot_data(urlencode({**fields, "signature": "made-up"}))
    assert data.user.allows_write_to_pm is True
    assert data.user.photo_url == "https://x/p.jpg"
    # only a real true counts
    loose = parse_web_bot_data(urlencode(_fields(user=_user(allowsWriteToPm="yes"))))
    assert loose.user.allows_write_to_pm is False


def test_the_fields_cannot_be_changed_afterwards() -> None:
    init_data, key = _init_data(_fields())
    data = validate_web_bot_data(init_data, bot_id=BOT, public_key=key, now=NOW)
    with pytest.raises(TypeError):
        data.fields["botId"] = "B2"  # type: ignore[index]


def test_the_check_string_is_the_documented_one() -> None:
    assert check_string("B9", {"b": "2", "a": "1", "signature": "zzz"}) == "B9:WebBotData\na=1\nb=2"


# --- through the bot api ---


async def test_the_client_fetches_the_key(api: Any, fake: Any) -> None:
    assert await api.get_web_bot_key() == fake.web_bot_key
    request = fake.requests[-1]
    assert (request["method"], request["path"]) == ("GET", "/bot/v1/webbot/key")


def test_the_blocking_client_fetches_it_too(sync_api: Any, fake: Any) -> None:
    assert sync_api.get_web_bot_key() == fake.web_bot_key


async def test_the_bot_checks_with_its_own_id_and_asks_for_the_key_once(make_bot: Any, fake: Any) -> None:
    from tests.fakeapi import BOT

    public, _ = _sign(SEED, b"")
    fake.web_bot_key = _b64(public)
    bot = make_bot()
    init_data, _ = _init_data(_fields(botId=BOT, authDate=str(int(time.time()) - 5)), signed_for=BOT)
    data = await bot.validate_web_bot_data(init_data)
    assert data.bot_id == BOT
    assert data.user.first_name == "Anna"
    await bot.validate_web_bot_data(init_data)
    asked = [r for r in fake.requests if r["path"] == "/bot/v1/webbot/key"]
    assert len(asked) == 1, "the key is kept after the first time"
    # data for another bot, however well signed, is refused
    other, _ = _init_data(_fields(botId="B2", authDate=str(int(time.time()))), signed_for="B2")
    with pytest.raises(WebBotDataError):
        await bot.validate_web_bot_data(other)


def test_the_sync_bot_does_the_same(make_sync_bot: Any, fake: Any) -> None:
    from tests.fakeapi import BOT

    public, _ = _sign(SEED, b"")
    fake.web_bot_key = _b64(public)
    bot = make_sync_bot()
    init_data, _ = _init_data(_fields(botId=BOT, authDate=str(int(time.time()))), signed_for=BOT)
    assert bot.validate_web_bot_data(init_data).user.id == "48213907"


def test_data_signed_by_the_server_itself_passes() -> None:
    # made by SERVER/src/webbot/signing.js with a throwaway key: if the two sides ever
    # disagree on the check string, this is where it shows
    sample = json.loads(fixture_text("webbot_initdata.json"))
    data = validate_web_bot_data(
        sample["initData"], bot_id=sample["botId"], public_key=sample["publicKey"], max_age=None
    )
    assert data.user.full_name == "Zoë Anne D'Amico"
    assert data.user.allows_write_to_pm is True
    assert data.start_param == "ref_42"
    assert data.chat_type == "link"
    assert data.auth_date.timestamp() == sample["authDate"]
    with pytest.raises(WebBotDataError):
        validate_web_bot_data(sample["initData"], bot_id="B41205737", public_key=sample["publicKey"], max_age=None)

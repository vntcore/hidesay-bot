"""Ed25519 signature checks, for the data a Web-Bot page is opened with.

Only verification: the library never signs anything with Ed25519, and checking
holds no secret, so there is nothing here to leak through timing. With the
``cryptography`` package installed it does the work; without it the check runs
in Python, after the reference code of RFC 8032 section 6. That takes a few
milliseconds per signature, which is fine for data checked once per page load.
"""

from __future__ import annotations

import hashlib

try:  # optional, and much faster when it's there
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    _FAST = True
except ImportError:  # pragma: no cover - depends on the environment
    _FAST = False

__all__ = ["verify"]

_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)

# points in extended coordinates (X, Y, Z, T): x = X/Z, y = Y/Z, xy = T/Z
_Point = tuple[int, int, int, int]
_NEUTRAL: _Point = (0, 1, 1, 0)


def _add(a: _Point, b: _Point) -> _Point:
    x1, y1, z1, t1 = a
    x2, y2, z2, t2 = b
    k = (y1 - x1) * (y2 - x2) % _P
    m = (y1 + x1) * (y2 + x2) % _P
    c = 2 * t1 * t2 * _D % _P
    dd = 2 * z1 * z2 % _P
    e, f, g, h = m - k, dd - c, dd + c, m + k
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _multiply(scalar: int, point: _Point) -> _Point:
    result = _NEUTRAL
    while scalar > 0:
        if scalar & 1:
            result = _add(result, point)
        point = _add(point, point)
        scalar >>= 1
    return result


def _same(a: _Point, b: _Point) -> bool:
    # x1/z1 == x2/z2 and y1/z1 == y2/z2, without dividing
    return (a[0] * b[2] - b[0] * a[2]) % _P == 0 and (a[1] * b[2] - b[1] * a[2]) % _P == 0


def _x_of(y: int, sign: int) -> int | None:
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P != 0:
        return None
    if x & 1 != sign:
        x = _P - x
    return x


def _decode(raw: bytes) -> _Point | None:
    if len(raw) != 32:
        return None
    y = int.from_bytes(raw, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _x_of(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _P)


_BASE_Y = 4 * pow(5, _P - 2, _P) % _P
_BASE_X = _x_of(_BASE_Y, 0)
assert _BASE_X is not None
_BASE: _Point = (_BASE_X, _BASE_Y, 1, _BASE_X * _BASE_Y % _P)


def _verify_here(public_key: bytes, signature: bytes, message: bytes) -> bool:
    a = _decode(public_key)
    if a is None:
        return False
    r_raw = signature[:32]
    r = _decode(r_raw)
    if r is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    # a second, larger s would verify too; RFC 8032 says refuse it
    if s >= _L:
        return False
    h = int.from_bytes(hashlib.sha512(r_raw + public_key + message).digest(), "little") % _L
    return _same(_multiply(s, _BASE), _add(r, _multiply(h, a)))


def verify(public_key: bytes, signature: bytes, message: bytes, *, pure: bool = False) -> bool:
    """Whether ``signature`` is ``public_key``'s Ed25519 signature of ``message``.

    Args:
        public_key: The 32 raw bytes of the key.
        signature: The 64 raw bytes of the signature.
        message: What was signed.
        pure: Check in Python even when ``cryptography`` is installed; for the tests.

    Returns:
        ``True`` only for a valid signature. Keys and signatures of the wrong length, points
        that are not on the curve and anything else malformed are ``False``, never an error.
    """
    if len(public_key) != 32 or len(signature) != 64:
        return False
    if _FAST and not pure:
        try:
            Ed25519PublicKey.from_public_bytes(public_key).verify(signature, message)
        except (InvalidSignature, ValueError):
            return False
        return True
    return _verify_here(public_key, signature, message)

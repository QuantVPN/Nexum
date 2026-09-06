"""Password hashing with the standard library (scrypt), no extra dependencies."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

_N = 2**14
_R = 8
_P = 1
_DKLEN = 64


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)
    return "scrypt${}${}${}${}".format(
        _N,
        base64.b64encode(salt).decode(),
        base64.b64encode(digest).decode(),
        f"{_R}:{_P}",
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n_str, salt_b64, digest_b64, rp = encoded.split("$")
        if scheme != "scrypt":
            return False
        r_str, p_str = rp.split(":")
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.scrypt(
            password.encode(),
            salt=salt,
            n=int(n_str),
            r=int(r_str),
            p=int(p_str),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)

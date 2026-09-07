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


def generate_token() -> tuple[str, str]:
    """A random URL-safe token and the SHA-256 hex digest that is stored."""
    import secrets

    raw = secrets.token_urlsafe(32)
    return raw, hash_token(raw)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


MIN_PASSWORD_LENGTH = 8


def validate_password(password: str) -> None:
    from nexum.errors import ValidationError

    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValidationError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    if password.strip() != password:
        raise ValidationError("Password cannot start or end with spaces")
    if password.lower() in {"password", "password1", "12345678", "qwerty12"}:
        raise ValidationError("That password is too common")

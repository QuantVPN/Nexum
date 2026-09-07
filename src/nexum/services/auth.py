"""Sign-in, password changes and password resets."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexum.errors import AuthenticationError, TooManyRequestsError, ValidationError
from nexum.models import PasswordResetToken, User
from nexum.models.identity import new_session_salt
from nexum.models.types import utcnow
from nexum.services import audit, mailer
from nexum.services.company import get_company
from nexum.services.people import get_user_by_email
from nexum.services.ratelimit import login_limiter
from nexum.services.security import (
    generate_token,
    hash_password,
    hash_token,
    validate_password,
    verify_password,
)

RESET_TOKEN_TTL = timedelta(hours=2)


def limiter_key(client_ip: str | None, email: str) -> str:
    return f"{client_ip or 'unknown'}|{email.lower().strip()}"


def authenticate(session: Session, email: str, password: str, *, client_ip: str | None) -> User:
    """Verify credentials with rate limiting; raises AuthenticationError / TooManyRequestsError."""
    key = limiter_key(client_ip, email)
    if login_limiter.is_blocked(key):
        wait = login_limiter.retry_after(key)
        raise TooManyRequestsError(
            f"Too many failed sign-in attempts. Try again in {wait // 60 + 1} min."
        )
    user = get_user_by_email(session, email)
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        login_limiter.hit(key)
        raise AuthenticationError("Invalid email or password")
    login_limiter.reset(key)
    user.last_login_at = utcnow()
    return user


def change_password(session: Session, user: User, current_password: str, new_password: str) -> None:
    if not verify_password(current_password, user.password_hash):
        raise AuthenticationError("Current password is incorrect")
    if current_password == new_password:
        raise ValidationError("Choose a password you have not used before")
    validate_password(new_password)
    user.password_hash = hash_password(new_password)
    user.session_salt = new_session_salt()  # signs every other session out
    audit.record(session, "user.password_changed", "user", user.id, actor=user)


def create_reset_token(session: Session, user: User, *, created_by: User | None = None) -> str:
    """Invalidate older tokens and mint a new one; returns the raw token (shown once)."""
    for old in session.scalars(
        select(PasswordResetToken).where(
            PasswordResetToken.user_id == user.id, PasswordResetToken.used_at.is_(None)
        )
    ):
        old.used_at = utcnow()
    raw, digest = generate_token()
    session.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=digest,
            expires_at=utcnow() + RESET_TOKEN_TTL,
            created_by_user_id=created_by.id if created_by else None,
        )
    )
    session.flush()
    audit.record(
        session,
        "user.reset_token_created",
        "user",
        user.id,
        actor=created_by,
        actor_label=created_by.email if created_by else "self-service",
    )
    return raw


def request_reset(session: Session, email: str, *, reset_url_base: str) -> bool:
    """Self-service 'forgot password': e-mails a link when the address matches an active
    login and e-mail is configured. Always returns quietly so addresses cannot be probed."""
    user = get_user_by_email(session, email)
    if user is None or not user.is_active:
        return False
    settings = get_company(session)
    if not settings.email_configured:
        return False
    raw = create_reset_token(session, user)
    link = f"{reset_url_base.rstrip('/')}/reset/{raw}"
    return mailer.try_send(
        settings,
        user.email,
        f"[{settings.name}] Reset your password",
        f"Hi {user.full_name},\n\nUse this link within 2 hours to choose a new password:\n"
        f"{link}\n\n"
        "If you did not ask for this, you can ignore the message.",
    )


def find_valid_token(session: Session, raw: str) -> PasswordResetToken | None:
    token = session.scalar(
        select(PasswordResetToken).where(PasswordResetToken.token_hash == hash_token(raw))
    )
    if token is None or not token.is_valid or not token.user.is_active:
        return None
    return token


def reset_password(session: Session, raw: str, new_password: str) -> User:
    token = find_valid_token(session, raw)
    if token is None:
        raise AuthenticationError("This reset link is invalid or has expired")
    validate_password(new_password)
    user = token.user
    user.password_hash = hash_password(new_password)
    user.session_salt = new_session_salt()
    token.used_at = utcnow()
    audit.record(session, "user.password_reset", "user", user.id, actor=user)
    return user

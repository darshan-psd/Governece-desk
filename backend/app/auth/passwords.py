"""Password hashing (argon2id) and the password policy."""
from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.errors import BadRequest

# argon2-cffi defaults follow RFC 9106's second recommended profile (64 MiB, t=3, p=4).
_hasher = PasswordHasher()
# Verified against when the account does not exist, so the response time does not reveal it.
_DUMMY_HASH = _hasher.hash("governance-desk-timing-equaliser")
MIN_LENGTH = 12
MAX_LENGTH = 256
# A short deny-list of the most common passwords that satisfy the length rule.
COMMON = frozenset({
    "password1234", "123456789012", "qwertyuiopas", "passwordpassword", "letmein12345", "welcome12345",
    "administrator", "iloveyou1234", "governance123", "changeme1234", "password@123", "admin@123456",
})


def validate_password(password: object, email: str | None = None, name: str | None = None) -> str:
    if not isinstance(password, str) or not (MIN_LENGTH <= len(password) <= MAX_LENGTH):
        raise BadRequest(f"Choose a password of {MIN_LENGTH} to {MAX_LENGTH} characters.")
    lowered = password.lower()
    if lowered in COMMON or len(set(password)) < 5:
        raise BadRequest("This password is too easy to guess. Choose a longer, less common password.")
    for personal in (email.split("@")[0] if email else None, name):
        if personal and len(personal) >= 4 and personal.lower() in lowered:
            raise BadRequest("The password must not contain your name or email address.")
    return password


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    """Constant-work verification; also runs (against a dummy hash) when there is no account."""
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return False

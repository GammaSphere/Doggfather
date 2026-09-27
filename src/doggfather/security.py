"""Small, dependency-free security primitives.

- Passwords: scrypt from the standard library, parameters stored with the hash.
- Tokens: 256-bit random, stored only as SHA-256 digests.
- Signed values: HMAC-SHA256 with the instance secret, for cookies we must
  trust on the way back in (flash messages, voter devices).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_LEN = 32


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_LEN)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        # Burn comparable time so "no such user" and "wrong password" look alike.
        hashlib.scrypt(b"x", salt=b"0" * 16, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_LEN)
        return False
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    candidate = hashlib.scrypt(password.encode("utf-8"), salt=_unb64(salt), n=int(n), r=int(r), p=int(p), dklen=len(_unb64(digest)))
    return hmac.compare_digest(candidate, _unb64(digest))


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def constant_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def sign(secret: str, value: str, purpose: str) -> str:
    """Return ``value.signature``; the purpose stops cross-use of signatures."""
    mac = hmac.new(secret.encode(), f"{purpose}:{value}".encode(), hashlib.sha256).digest()
    return f"{value}.{_b64(mac)}"


def unsign(secret: str, signed: str | None, purpose: str) -> str | None:
    if not signed or "." not in signed:
        return None
    value, _, _mac = signed.rpartition(".")
    expected = sign(secret, value, purpose)
    return value if hmac.compare_digest(expected.encode(), signed.encode()) else None


def keyed_hash(secret: str, value: str, purpose: str) -> str:
    """Stable pseudonym for values we must compare but should not store raw
    (voter IP addresses, device fingerprints)."""
    return hmac.new(secret.encode(), f"{purpose}:{value}".encode(), hashlib.sha256).hexdigest()[:32]

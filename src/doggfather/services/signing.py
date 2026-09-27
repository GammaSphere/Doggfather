"""Ed25519 signing for certificates and judge records.

The platform key lives in ``<data>/keys/ed25519.pem`` (created on first use,
mode 0600). The public half is published at
``/.well-known/doggfather/signing-key.pem``, so anyone can verify a record
offline with any Ed25519 implementation:

    message   = canonical JSON of the payload (UTF-8, sorted keys, no spaces)
    signature = base64url(Ed25519.sign(private_key, message))

``key_id`` is the first 16 hex characters of SHA-256 over the raw 32-byte
public key, so a rotated key is detectable.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

KEY_FILE = "ed25519.pem"
_lock = threading.Lock()
_cache: dict[Path, "Signer"] = {}


def canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def key_id_for(public_key: Ed25519PublicKey) -> str:
    raw = public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()[:16]


class Signer:
    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self._private = private_key
        self.public_key = private_key.public_key()
        self.key_id = key_id_for(self.public_key)

    @property
    def public_pem(self) -> str:
        return self.public_key.public_bytes(serialization.Encoding.PEM,
                                            serialization.PublicFormat.SubjectPublicKeyInfo).decode("ascii")

    def sign(self, payload: dict[str, Any]) -> str:
        return _b64(self._private.sign(canonical(payload)))


def load_signer(keys_dir: Path) -> Signer:
    path = keys_dir / KEY_FILE
    with _lock:
        if path in _cache:
            return _cache[path]
        if path.exists():
            key = serialization.load_pem_private_key(path.read_bytes(), password=None)
            if not isinstance(key, Ed25519PrivateKey):
                raise ValueError(f"{path} is not an Ed25519 key")
        else:
            keys_dir.mkdir(parents=True, exist_ok=True)
            key = Ed25519PrivateKey.generate()
            path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
            try:
                path.chmod(0o600)
            except OSError:  # pragma: no cover - Windows
                pass
        signer = Signer(key)
        _cache[path] = signer
        return signer


def load_public_key(pem: str | bytes) -> Ed25519PublicKey:
    key = serialization.load_pem_public_key(pem.encode() if isinstance(pem, str) else pem)
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("not an Ed25519 public key")
    return key


def verify(public_key: Ed25519PublicKey, payload: dict[str, Any], signature: str) -> bool:
    try:
        public_key.verify(_unb64(signature), canonical(payload))
    except (InvalidSignature, ValueError):
        return False
    return True

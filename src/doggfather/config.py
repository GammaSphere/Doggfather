"""Runtime settings, read once from the environment.

Every knob has a safe default so `docker compose up` needs no configuration.
Tests build settings directly with `load_settings(data_dir=..., ...)`.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, fields
from pathlib import Path

ENV_PREFIX = "DOGFOOD_"


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    secret_key: str
    base_url: str = "http://localhost:8080"
    host: str = "0.0.0.0"
    port: int = 8080
    # Demo mode seeds well-known passwords and the fixed checker sessions
    # (session=org_7f2a and friends). Never enable it on a public deployment.
    demo: bool = False
    fixtures_path: Path | None = None
    cookie_secure: bool = False
    session_days: int = 14
    max_upload_bytes: int = 2 * 1024 * 1024
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    mail_from: str = "Doggfather <noreply@doggfather.local>"
    # When set, webhook targets that resolve to loopback or private ranges
    # are refused. Off by default because self-hosters often run the
    # receiving bot on the same box.
    webhook_block_private: bool = False
    webhook_worker: bool = True

    @property
    def database_path(self) -> Path:
        return self.data_dir / "doggfather.db"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def keys_dir(self) -> Path:
        return self.data_dir / "keys"


def _coerce(kind: type, raw: str):
    if kind is bool:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if kind is int:
        return int(raw)
    if kind is Path:
        return Path(raw)
    return raw


_TYPES = {
    "data_dir": Path,
    "secret_key": str,
    "base_url": str,
    "host": str,
    "port": int,
    "demo": bool,
    "fixtures_path": Path,
    "cookie_secure": bool,
    "session_days": int,
    "max_upload_bytes": int,
    "smtp_host": str,
    "smtp_port": int,
    "smtp_user": str,
    "smtp_password": str,
    "mail_from": str,
    "webhook_block_private": bool,
    "webhook_worker": bool,
}


def _persistent_secret(data_dir: Path) -> str:
    """Generate the signing secret once and keep it with the data."""
    path = data_dir / "secret.key"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    data_dir.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_hex(32)
    path.write_text(secret, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover - Windows without POSIX perms
        pass
    return secret


def load_settings(**overrides) -> Settings:
    values: dict = {}
    for f in fields(Settings):
        raw = os.environ.get(ENV_PREFIX + f.name.upper())
        if raw not in (None, ""):
            values[f.name] = _coerce(_TYPES[f.name], raw)
    values.update({k: v for k, v in overrides.items() if v is not None})

    data_dir = Path(values.get("data_dir", "data")).resolve()
    values["data_dir"] = data_dir
    if not values.get("secret_key"):
        values["secret_key"] = _persistent_secret(data_dir)
    if values.get("fixtures_path") is not None:
        values["fixtures_path"] = Path(values["fixtures_path"])
    values["base_url"] = str(values.get("base_url", Settings.base_url)).rstrip("/")
    return Settings(**values)

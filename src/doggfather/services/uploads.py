"""Image uploads: thumbnails and project galleries.

The file type comes from the leading bytes, not from the name or the
Content-Type the browser sends; SVG is refused outright (it can carry
script). Stored names are 128-bit random hex, so paths cannot be guessed
or traversed, and files are served with ``nosniff`` and a locked-down CSP.
"""

from __future__ import annotations

import re
import secrets
from pathlib import Path

from starlette.datastructures import UploadFile

from ..config import Settings
from ..errors import ValidationFailed

NAME_RE = re.compile(r"^[0-9a-f]{32}\.(png|jpg|gif|webp)$")
MEDIA_TYPES = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}


def sniff_image(data: bytes) -> str | None:
    """Return the canonical extension for a supported image, else None."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def save_image(settings: Settings, upload: UploadFile, field: str = "image") -> str:
    data = upload.file.read(settings.max_upload_bytes + 1)
    if not data:
        raise ValidationFailed(fields={field: "That file is empty."})
    if len(data) > settings.max_upload_bytes:
        limit_mb = settings.max_upload_bytes / (1024 * 1024)
        raise ValidationFailed(fields={field: f"Images must be {limit_mb:g} MB or smaller."})
    ext = sniff_image(data)
    if ext is None:
        raise ValidationFailed(fields={field: "Upload a PNG, JPEG, GIF or WebP image."})
    name = f"{secrets.token_hex(16)}.{ext}"
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    (settings.uploads_dir / name).write_bytes(data)
    return name


def upload_path(settings: Settings, name: str) -> Path | None:
    if not NAME_RE.match(name):
        return None
    path = settings.uploads_dir / name
    return path if path.is_file() else None


def delete_image(settings: Settings, name: str | None) -> None:
    if name and NAME_RE.match(name):
        (settings.uploads_dir / name).unlink(missing_ok=True)

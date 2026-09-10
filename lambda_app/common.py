"""Shared validation and response helpers for the Lambda conversion boundary."""

from __future__ import annotations

import base64
import json
import re
from pathlib import PurePosixPath
from typing import Any

MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_FILES_PER_JOB = 1
MAX_JOBS_PER_IP_PER_DAY = 5
MAX_JOBS_PER_MONTH = 100
MAX_API_REQUESTS_PER_IP_PER_MINUTE = 60
MAX_API_REQUESTS_PER_DAY = 3_000
MAX_API_REQUESTS_PER_MONTH = 20_000
RESULT_TTL_SECONDS = 24 * 60 * 60
WORKER_LOCK_KEY = "control#worker-lock"
WORKER_LOCK_TTL_SECONDS = 11 * 60
ALLOWED_FORMATS = {"md", "json"}
ALLOWED_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".odt", ".ods", ".odp", ".epub", ".pages", ".html", ".htm",
    ".md", ".markdown", ".adoc", ".asciidoc", ".tex", ".latex",
    ".csv", ".tsv", ".png", ".jpg", ".jpeg", ".tif", ".tiff",
    ".bmp", ".webp", ".eml", ".msg", ".vtt", ".boxnote", ".json",
    ".dclg", ".dclx", ".xml", ".xbrl", ".jats",
}


def safe_filename(filename: str) -> str | None:
    """Return a canonical filename or reject a path, control byte, or long name."""
    if not filename or len(filename) > 200 or any(ord(character) < 32 for character in filename):
        return None
    normalized = filename.replace("\\", "/")
    if PurePosixPath(normalized).name != filename or ".." in filename:
        return None
    if PurePosixPath(filename.lower()).suffix not in ALLOWED_EXTENSIONS:
        return None
    return filename


def signature_matches(extension: str, data: bytes) -> bool:
    """Apply cheap signature checks for types with a stable file header."""
    signatures = {
        ".pdf": (b"%PDF-",),
        ".png": (b"\x89PNG\r\n\x1a\n",),
        ".jpg": (b"\xff\xd8\xff",),
        ".jpeg": (b"\xff\xd8\xff",),
        ".tif": (b"II*\x00", b"MM\x00*"),
        ".tiff": (b"II*\x00", b"MM\x00*"),
        ".bmp": (b"BM",),
        ".webp": (b"RIFF",),
        ".doc": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
        ".xls": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
        ".ppt": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
        ".msg": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
    }
    zip_extensions = {".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp", ".epub", ".pages", ".dclx"}
    if extension in zip_extensions:
        return data.startswith(b"PK\x03\x04")
    if extension == ".webp":
        return data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    expected = signatures.get(extension)
    return expected is None or any(data.startswith(value) for value in expected)


def parse_json_body(event: dict[str, Any]) -> dict[str, Any] | None:
    """Decode a Lambda Function URL JSON body without accepting malformed input."""
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        try:
            body = base64.b64decode(body).decode("utf-8")
        except (UnicodeDecodeError, ValueError):
            return None
    try:
        value = json.loads(body)
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def response(status_code: int, payload: dict[str, Any], origin: str | None = None) -> dict[str, Any]:
    """Create a private JSON Function URL response with optional strict CORS."""
    headers = {
        "content-type": "application/json; charset=utf-8",
        "cache-control": "no-store",
        "x-content-type-options": "nosniff",
    }
    if origin:
        headers["access-control-allow-origin"] = origin
        headers["vary"] = "Origin"
    return {"statusCode": status_code, "headers": headers, "body": json.dumps(payload)}


def is_site_origin(event: dict[str, Any], site_origin: str) -> bool:
    """Reject browser requests from unexpected Origins; non-browser calls are also rejected."""
    headers = {str(key).lower(): str(value) for key, value in event.get("headers", {}).items()}
    return headers.get("origin") == site_origin


def job_id_from_path(path: str) -> str | None:
    match = re.fullmatch(r"/jobs/([0-9a-f-]{36})(?:/submit)?", path)
    return match.group(1) if match else None

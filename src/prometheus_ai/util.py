"""Shared helpers: time, ids, hashing and bounded text."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any

_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def new_id(prefix: str = "inc") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_json(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return sha256_text(canonical)


def truncate(value: str, limit: int = 4000) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + f"\n...[truncated {len(value) - limit} chars]"


def is_safe_slug(value: str) -> bool:
    """Kubernetes DNS-1123 label safety, used before anything reaches kubectl."""
    return bool(_SAFE_ID.match(value))


def redact_mapping(value: Any) -> Any:
    """Best-effort recursive redaction used before persisting tool input."""
    from prometheus_ai.llm.redact import redact

    if isinstance(value, str):
        return redact(value).text
    if isinstance(value, dict):
        return {k: redact_mapping(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_mapping(v) for v in value]
    return value

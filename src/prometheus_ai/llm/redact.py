"""Redaction, applied before any model call and before persistence.

This is a data-handling control, not a safety control: the policy engine does
not depend on it. It reduces the chance that logs, events or annotations leak
credentials to a hosted model or into the audit log.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

REDACTED = "[REDACTED:{kind}]"

# Order matters: broad patterns run after specific ones so the count is useful.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "private_key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----.*?"
            r"-----END (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}")),
    (
        "credential_assignment",
        re.compile(
            r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
            r"client[_-]?secret|authorization)\b\s*[:=]\s*"
            r"[\"']?([^\s\"',;}\]]{4,})[\"']?"
        ),
    ),
    (
        "connection_string",
        re.compile(r"(?i)\b(postgres(?:ql)?|mysql|redis|mongodb)://[^\s\"']+"),
    ),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
]

_INJECTION_MARKERS = re.compile(
    r"(?i)(ignore (all |any )?(previous|prior|above) instructions|"
    r"disregard (the )?(system|previous) prompt|you are now|"
    r"delete (the )?namespace|reveal (the )?secret|exfiltrate|"
    r"kubectl\s+delete|rm\s+-rf|curl\s+https?://)"
)


@dataclass(frozen=True)
class RedactionResult:
    text: str
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def redact(text: str) -> RedactionResult:
    if not text:
        return RedactionResult(text="")
    counts: dict[str, int] = {}
    result = text
    for kind, pattern in _PATTERNS:
        result, n = pattern.subn(REDACTED.format(kind=kind), result)
        if n:
            counts[kind] = counts.get(kind, 0) + n
    return RedactionResult(text=result, counts=counts)


def redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value).text
    if isinstance(value, dict):
        return {
            _redact_key(k): redact_value(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact_value(v) for v in value]
    return value


def _redact_key(key: str) -> str:
    return key


def injection_signals(text: str) -> list[str]:
    """Return matched injection-like fragments.

    This is telemetry for the failure-mode study. It is never used as a gate:
    the policy engine assumes every tool output is hostile.
    """
    return [m.group(0) for m in _INJECTION_MARKERS.finditer(text)]

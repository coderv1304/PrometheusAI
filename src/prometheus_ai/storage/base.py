"""Repository interfaces and an in-memory implementation.

The in-memory store is the default for tests and the single-process demo. It is
explicitly not crash-safe; use Postgres for the resume guarantees in the TRD.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

from prometheus_ai.models import AuditEvent, Incident, IncidentStatus, Postmortem


class Repository(Protocol):
    async def save_incident(self, incident: Incident) -> Incident: ...

    async def get_incident(self, incident_id: str) -> Incident | None: ...

    async def list_incidents(
        self, *, limit: int = 50, status: IncidentStatus | None = None
    ) -> list[Incident]: ...

    async def find_active_by_fingerprint(self, fingerprint: str) -> Incident | None: ...

    async def add_audit(self, event: AuditEvent) -> None: ...

    async def list_audit(self, incident_id: str) -> list[AuditEvent]: ...

    async def save_postmortem(self, postmortem: Postmortem) -> None: ...

    async def get_postmortem(self, incident_id: str) -> Postmortem | None: ...

    async def close(self) -> None: ...


ACTIVE_STATUSES = {
    IncidentStatus.RECEIVED,
    IncidentStatus.INVESTIGATING,
    IncidentStatus.AWAITING_APPROVAL,
    IncidentStatus.APPROVED,
    IncidentStatus.EXECUTING,
    IncidentStatus.VERIFYING,
}


class MemoryRepository:
    def __init__(self) -> None:
        self._incidents: dict[str, Incident] = {}
        self._audit: dict[str, list[AuditEvent]] = {}
        self._postmortems: dict[str, Postmortem] = {}
        self._lock = asyncio.Lock()

    async def save_incident(self, incident: Incident) -> Incident:
        async with self._lock:
            self._incidents[incident.id] = incident
        return incident

    async def get_incident(self, incident_id: str) -> Incident | None:
        return self._incidents.get(incident_id)

    async def list_incidents(
        self, *, limit: int = 50, status: IncidentStatus | None = None
    ) -> list[Incident]:
        items = [
            i for i in self._incidents.values() if status is None or i.status == status
        ]
        items.sort(key=lambda i: i.started_at, reverse=True)
        return items[:limit]

    async def find_active_by_fingerprint(self, fingerprint: str) -> Incident | None:
        for incident in self._incidents.values():
            if incident.fingerprint == fingerprint and incident.status in ACTIVE_STATUSES:
                return incident
        return None

    async def add_audit(self, event: AuditEvent) -> None:
        async with self._lock:
            self._audit.setdefault(event.incident_id, []).append(event)

    async def list_audit(self, incident_id: str) -> list[AuditEvent]:
        return list(self._audit.get(incident_id, []))

    async def save_postmortem(self, postmortem: Postmortem) -> None:
        self._postmortems[postmortem.incident_id] = postmortem

    async def get_postmortem(self, incident_id: str) -> Postmortem | None:
        return self._postmortems.get(incident_id)

    async def close(self) -> None:
        return None

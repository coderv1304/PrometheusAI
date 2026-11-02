"""Alertmanager intake: normalise, deduplicate, rate-limit and dispatch."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from prometheus_ai.agent.graph import GraphRunner
from prometheus_ai.config import Settings
from prometheus_ai.models import Alert, AuditEvent, Incident, IncidentStatus
from prometheus_ai.storage.base import Repository
from prometheus_ai.util import new_id, sha256_text, truncate, utcnow

_ACTIVE = {
    IncidentStatus.RECEIVED,
    IncidentStatus.INVESTIGATING,
    IncidentStatus.AWAITING_APPROVAL,
    IncidentStatus.APPROVED,
    IncidentStatus.EXECUTING,
    IncidentStatus.VERIFYING,
}


def normalize(payload: dict, settings: Settings) -> list[Alert]:
    alerts: list[Alert] = []
    for raw in payload.get("alerts", [])[:50]:
        labels = {str(k): str(v)[:250] for k, v in (raw.get("labels") or {}).items()}
        annotations = {
            str(k): str(v)[:2000] for k, v in (raw.get("annotations") or {}).items()
        }
        alertname = labels.get("alertname", "UnknownAlert")
        namespace = labels.get("namespace", settings.demo_namespace)
        service = (
            labels.get("service")
            or labels.get("app")
            or labels.get("deployment")
            or labels.get("pod", "").rsplit("-", 2)[0]
            or "unknown"
        )
        fingerprint = raw.get("fingerprint") or sha256_text(
            f"{alertname}:{namespace}:{service}:{labels.get('severity', 'warning')}"
        )[:16]
        alerts.append(
            Alert(
                fingerprint=str(fingerprint)[:64],
                alertname=alertname,
                status=str(raw.get("status", "firing")),
                severity=(
                    labels.get("severity")
                    if labels.get("severity") in {"critical", "warning", "info"}
                    else "warning"
                ),
                namespace=namespace,
                service=service,
                summary=truncate(annotations.get("summary", ""), 500),
                description=truncate(annotations.get("description", ""), 2000),
                labels=labels,
                annotations=annotations,
            )
        )
    return alerts


@dataclass
class IngestResult:
    created: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    queued: int = 0


class InvestigationManager:
    """Bounded background execution with an explicit queue limit."""

    def __init__(self, runner: GraphRunner, repo: Repository, *, concurrency: int = 3) -> None:
        self.runner = runner
        self.repo = repo
        self._sem = asyncio.Semaphore(concurrency)
        self._tasks: dict[str, asyncio.Task] = {}
        self.max_queue = 100

    async def start(self, incident: Incident) -> bool:
        if len(self._tasks) >= self.max_queue:
            return False
        if incident.id in self._tasks and not self._tasks[incident.id].done():
            return False
        task = asyncio.create_task(self._run(incident), name=f"investigate:{incident.id}")
        self._tasks[incident.id] = task
        return True

    async def _run(self, incident: Incident) -> None:
        async with self._sem:
            try:
                await self.runner.start(incident)
            except Exception as exc:  # noqa: BLE001 - never lose an incident silently
                current = await self.repo.get_incident(incident.id)
                if current is not None:
                    await self.repo.save_incident(
                        current.model_copy(
                            update={
                                "status": IncidentStatus.FAILED,
                                "error": f"{type(exc).__name__}: {exc}",
                                "updated_at": utcnow(),
                            }
                        )
                    )
                await self.repo.add_audit(
                    AuditEvent(
                        id=new_id("aud"),
                        incident_id=incident.id,
                        node="runner",
                        actor="system",
                        action="investigation_failed",
                        decision="failed",
                        detail=str(exc),
                    )
                )

    async def wait_all(self) -> None:
        if self._tasks:
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)

    async def shutdown(self) -> None:
        for task in self._tasks.values():
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()


class AlertService:
    def __init__(
        self,
        settings: Settings,
        repo: Repository,
        manager: InvestigationManager,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.manager = manager

    async def ingest(self, payload: dict) -> IngestResult:
        result = IngestResult()
        for alert in normalize(payload, self.settings):
            if alert.status == "resolved":
                await self._resolve(alert, result)
                continue

            existing = await self.repo.find_active_by_fingerprint(alert.fingerprint)
            if existing is not None:
                result.duplicates.append(existing.id)
                await self.repo.add_audit(
                    AuditEvent(
                        id=new_id("aud"),
                        incident_id=existing.id,
                        node="receiver",
                        actor="system",
                        action="duplicate_alert_suppressed",
                        decision="dedupe",
                        detail=f"fingerprint={alert.fingerprint}",
                    )
                )
                continue

            if alert.namespace not in self.settings.allowed_namespaces:
                result.rejected.append(alert.fingerprint)
                continue

            incident = Incident(
                id=new_id("inc"),
                fingerprint=alert.fingerprint,
                alertname=alert.alertname,
                namespace=alert.namespace,
                service=alert.service,
                severity=alert.severity,
                status=IncidentStatus.RECEIVED,
                summary=alert.summary or alert.description or alert.alertname,
                labels=alert.labels,
                model_backend="",
            )
            await self.repo.save_incident(incident)
            await self.repo.add_audit(
                AuditEvent(
                    id=new_id("aud"),
                    incident_id=incident.id,
                    node="receiver",
                    actor="system",
                    action="incident_created",
                    decision="accepted",
                    input={"fingerprint": alert.fingerprint, "labels": alert.labels},
                )
            )
            if await self.manager.start(incident):
                result.created.append(incident.id)
                result.queued += 1
            else:
                result.rejected.append(alert.fingerprint)
        return result

    async def _resolve(self, alert: Alert, result: IngestResult) -> None:
        existing = await self.repo.find_active_by_fingerprint(alert.fingerprint)
        if existing is None:
            return
        await self.repo.save_incident(
            existing.model_copy(
                update={
                    "status": IncidentStatus.RESOLVED,
                    "resolved_at": utcnow(),
                    "updated_at": utcnow(),
                }
            )
        )
        await self.repo.add_audit(
            AuditEvent(
                id=new_id("aud"),
                incident_id=existing.id,
                node="receiver",
                actor="system",
                action="alert_resolved",
                decision="resolved",
                detail="Alertmanager reported the alert resolved",
            )
        )
        result.resolved.append(existing.id)

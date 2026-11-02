"""Postgres-backed repository.

Incidents are stored as JSONB (the domain object is the schema); audit events
are append-only rows. pgvector stores embeddings for runbook and postmortem
similarity search.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from prometheus_ai.models import AuditEvent, Incident, IncidentStatus, Postmortem
from prometheus_ai.storage.base import ACTIVE_STATUSES

if TYPE_CHECKING:  # pragma: no cover
    from psycopg_pool import AsyncConnectionPool


SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS incidents (
    id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    payload JSONB NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS incidents_fingerprint_idx ON incidents (fingerprint);
CREATE INDEX IF NOT EXISTS incidents_started_idx ON incidents (started_at DESC);

CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    ts TIMESTAMPTZ NOT NULL,
    node TEXT NOT NULL,
    actor TEXT NOT NULL,
    tool TEXT,
    action TEXT NOT NULL DEFAULT '',
    input JSONB NOT NULL DEFAULT '{}'::jsonb,
    output_hash TEXT NOT NULL DEFAULT '',
    decision TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS audit_incident_idx ON audit_events (incident_id, ts);

-- Audit rows are append-only: the repository never updates or deletes them,
-- and the database refuses to let anyone else do so either. An incident
-- cannot be deleted while its audit trail exists (the cascade is blocked).
CREATE OR REPLACE FUNCTION prometheus_ai_audit_append_only()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only';
END;
$$;

DROP TRIGGER IF EXISTS audit_events_append_only ON audit_events;
CREATE TRIGGER audit_events_append_only
BEFORE UPDATE OR DELETE ON audit_events
FOR EACH ROW EXECUTE FUNCTION prometheus_ai_audit_append_only();

CREATE TABLE IF NOT EXISTS postmortems (
    incident_id TEXT PRIMARY KEY REFERENCES incidents(id) ON DELETE CASCADE,
    markdown TEXT NOT NULL,
    root_cause_category TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    embedding vector(768)
);
ALTER TABLE postmortems ADD COLUMN IF NOT EXISTS embedding vector(768);

CREATE TABLE IF NOT EXISTS runbook_chunks (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    heading TEXT NOT NULL,
    text TEXT NOT NULL,
    embedding vector(768)
);
"""


class PostgresRepository:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._pool: AsyncConnectionPool | None = None

    async def connect(self) -> None:
        from psycopg_pool import AsyncConnectionPool

        self._pool = AsyncConnectionPool(self.dsn, open=False, min_size=1, max_size=5)
        await self._pool.open()
        async with self._pool.connection() as conn:
            await conn.execute(SCHEMA_SQL)
            await conn.commit()

    @property
    def pool(self) -> AsyncConnectionPool:
        if self._pool is None:
            raise RuntimeError("PostgresRepository.connect() was not awaited")
        return self._pool

    async def save_incident(self, incident: Incident) -> Incident:
        from psycopg.types.json import Jsonb

        payload = incident.model_dump(mode="json")
        async with self.pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO incidents (id, fingerprint, status, payload, started_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    fingerprint = EXCLUDED.fingerprint,
                    status = EXCLUDED.status,
                    payload = EXCLUDED.payload,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    incident.id,
                    incident.fingerprint,
                    str(incident.status),
                    Jsonb(payload),
                    incident.started_at,
                    incident.updated_at,
                ),
            )
            await conn.commit()
        return incident

    async def get_incident(self, incident_id: str) -> Incident | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT payload FROM incidents WHERE id = %s", (incident_id,)
            )
            row = await cur.fetchone()
        return Incident.model_validate(row[0]) if row else None

    async def list_incidents(
        self, *, limit: int = 50, status: IncidentStatus | None = None
    ) -> list[Incident]:
        async with self.pool.connection() as conn:
            if status is None:
                cur = await conn.execute(
                    "SELECT payload FROM incidents ORDER BY started_at DESC LIMIT %s", (limit,)
                )
            else:
                cur = await conn.execute(
                    "SELECT payload FROM incidents WHERE status = %s "
                    "ORDER BY started_at DESC LIMIT %s",
                    (str(status), limit),
                )
            rows = await cur.fetchall()
        return [Incident.model_validate(row[0]) for row in rows]

    async def find_active_by_fingerprint(self, fingerprint: str) -> Incident | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT payload FROM incidents WHERE fingerprint = %s AND status = ANY(%s) "
                "ORDER BY started_at DESC LIMIT 1",
                (fingerprint, [str(s) for s in ACTIVE_STATUSES]),
            )
            row = await cur.fetchone()
        return Incident.model_validate(row[0]) if row else None

    async def add_audit(self, event: AuditEvent) -> None:
        from psycopg.types.json import Jsonb

        async with self.pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO audit_events
                (id, incident_id, ts, node, actor, tool, action, input, output_hash, decision, detail)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    event.id,
                    event.incident_id,
                    event.ts,
                    event.node,
                    event.actor,
                    event.tool,
                    event.action,
                    Jsonb(event.input),
                    event.output_hash,
                    event.decision,
                    event.detail,
                ),
            )
            await conn.commit()

    async def list_audit(self, incident_id: str) -> list[AuditEvent]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id, incident_id, ts, node, actor, tool, action, input, "
                "output_hash, decision, detail FROM audit_events "
                "WHERE incident_id = %s ORDER BY ts, id",
                (incident_id,),
            )
            rows = await cur.fetchall()
        return [
            AuditEvent(
                id=r[0],
                incident_id=r[1],
                ts=r[2],
                node=r[3],
                actor=r[4],
                tool=r[5],
                action=r[6],
                input=r[7],
                output_hash=r[8],
                decision=r[9],
                detail=r[10],
            )
            for r in rows
        ]

    async def save_postmortem(self, postmortem: Postmortem) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO postmortems (incident_id, markdown, root_cause_category, created_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (incident_id) DO UPDATE SET
                    markdown = EXCLUDED.markdown,
                    root_cause_category = EXCLUDED.root_cause_category
                """,
                (
                    postmortem.incident_id,
                    postmortem.markdown,
                    str(postmortem.root_cause_category),
                    postmortem.created_at,
                ),
            )
            await conn.commit()

    async def get_postmortem(self, incident_id: str) -> Postmortem | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT incident_id, markdown, root_cause_category, created_at "
                "FROM postmortems WHERE incident_id = %s",
                (incident_id,),
            )
            row = await cur.fetchone()
        if not row:
            return None
        return Postmortem(
            incident_id=row[0],
            markdown=row[1],
            root_cause_category=row[2],
            created_at=row[3],
        )

    async def search_similar_postmortems(self, embedding: list[float], k: int = 3) -> list[dict[str, Any]]:
        """Requires pgvector. Kept small and explicit; used by incident memory."""
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT incident_id, markdown, embedding <=> %s::vector AS distance "
                "FROM postmortems WHERE embedding IS NOT NULL ORDER BY distance LIMIT %s",
                (json.dumps(embedding), k),
            )
            rows = await cur.fetchall()
        return [{"incident_id": r[0], "markdown": r[1], "distance": r[2]} for r in rows]

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()

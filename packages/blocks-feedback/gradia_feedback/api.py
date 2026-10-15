"""Bounded local reference service. Credentials are configured by its host."""
import base64
import hashlib
import hmac
import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from .contracts import Acknowledgment, BlockSettings, FeedbackPage, FeedbackRecord, Principal, Submission, Triage, TriageAcknowledgment
from .store import Store, record
from .telemetry import AttemptStore, AttemptTelemetry

VERSION = "0.1.0"
MAX_BODY = 400000


class BlockError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


def fail(status, code, message):
    raise BlockError(status, code, message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class BodyLimit:
    """Limit actual streamed bytes, including requests without Content-Length."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            size += len(message.get("body", b""))
            if size > MAX_BODY:
                scope["gradia.error_code"] = "payload_too_large"
                return await JSONResponse({"error": {"code": "payload_too_large", "message": "Request exceeds 400000 bytes"}}, status_code=413)(scope, receive, send)
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        delivered = False
        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()
        await self.app(scope, bounded_receive, send)


def create_app(database_path, api_keys: dict[str, Principal], settings: BlockSettings | None = None):
    """Each key is server-owned authority, never accepted from a tenant header."""
    if not api_keys or any(len(key) < 24 for key in api_keys):
        raise ValueError("Configure at least one bearer key of 24+ characters")
    principals = [(key, Principal.model_validate(value)) for key, value in api_keys.items()]
    settings = settings or BlockSettings()
    store = Store(database_path)
    config_digest = hashlib.sha256(canonical(settings.model_dump(mode="json")).encode()).hexdigest()
    app = FastAPI(title="Gradia Feedback reference block", version=VERSION)
    app.add_middleware(BodyLimit)
    app.state.store = store
    attempts = AttemptStore({actor.tenant_id for _, actor in principals})
    app.state.attempts = attempts
    app.add_middleware(AttemptTelemetry, store=attempts, config_digest=config_digest, version=VERSION)

    @app.exception_handler(BlockError)
    async def block_error(request, exc):
        request.scope["gradia.error_code"] = exc.code
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        request.scope["gradia.error_code"] = "invalid_request"
        # Avoid echoing potentially sensitive invalid input in framework error details.
        return JSONResponse({"error": {"code": "invalid_request", "message": "Request does not match the versioned contract"}}, status_code=422)

    @app.exception_handler(sqlite3.Error)
    async def database_error(request, exc):
        request.scope["gradia.error_code"] = "persistence_unavailable"
        return JSONResponse({"error": {"code": "persistence_unavailable", "message": "Persistence was not acknowledged; reconcile using the same idempotency key"}}, status_code=503)

    def principal(request: Request, authorization: Annotated[str | None, Header()] = None):
        token = authorization[7:] if authorization and authorization.startswith("Bearer ") else ""
        found = None
        for key, candidate in principals:
            if hmac.compare_digest(token.encode("utf-8"), key.encode("utf-8")):
                found = candidate
        if found is None:
            fail(401, "unauthorized", "A configured bearer credential is required")
        request.scope["gradia.principal"] = found
        if not settings.features.enabled:
            fail(403, "feature_disabled", "Feedback is disabled")
        return found

    def operator(actor: Principal = Depends(principal)):
        if actor.role != "operator":
            fail(403, "forbidden", "Operator authority is required")
        return actor

    def fetch(conn, tenant, identity):
        row = conn.execute("SELECT * FROM feedback WHERE tenant=? AND id=?", (tenant, identity)).fetchone()
        if row is None:
            fail(404, "not_found", "Feedback not found")
        return row

    def event(conn, actor, identity, revision, operation, started, correlation_id):
        payload = {"schema_version": "1", "event_id": str(uuid.uuid4()), "block_id": "gradia.feedback", "block_version": VERSION, "edition": VERSION, "correlation_id": correlation_id, "actor_type": "configured_principal", "human_involvement": "unknown", "privacy": "metadata_only", "configuration_digest": config_digest, "tenant_id": actor.tenant_id, "actor_id": actor.actor_id, "feedback_id": identity, "revision": revision, "operation": operation, "result": "persisted", "acceptance": "not_evaluated", "billability": "not_evaluated", "cost": {"status": "unknown", "amount": None, "currency": None}, "coverage": "committed_mutations_only", "duration_ms": round((time.monotonic() - started) * 1000, 3), "occurred_at": datetime.now(timezone.utc).isoformat()}
        conn.execute("INSERT INTO events(tenant,feedback_id,payload) VALUES(?,?,?)", (actor.tenant_id, identity, canonical(payload)))

    @app.get("/v1/feedback/diagnostics/attempts")
    def attempt_diagnostics(actor: Principal = Depends(operator)):
        return attempts.snapshot(actor.tenant_id)

    @app.get("/v1/feedback/config")
    def get_config(actor: Principal = Depends(principal)):
        return {"block_id": "gradia.feedback", "version": VERSION, "configuration_digest": config_digest, **settings.model_dump(mode="json")}

    @app.post("/v1/feedback", response_model=Acknowledgment, responses={201: {"model": Acknowledgment}})
    def submit(body: Submission, request: Request, idempotency_key: Annotated[str, Header(min_length=1, max_length=128)], actor: Principal = Depends(principal)):
        started = time.monotonic()
        if len(body.message) > settings.config.max_message_chars or body.category not in settings.config.categories:
            fail(422, "invalid_submission", "Message length or category violates configuration")
        audio = None
        if body.audio:
            if not settings.features.audio:
                fail(403, "feature_disabled", "Audio is disabled")
            try:
                audio = base64.b64decode(body.audio.data_base64, validate=True)
            except ValueError:
                fail(422, "invalid_audio", "Audio must use valid base64")
            if not audio or len(audio) > settings.config.max_audio_bytes:
                fail(413, "audio_too_large", "Audio exceeds the configured byte limit")
            signatures = {"audio/webm": audio.startswith(b"\x1aE\xdf\xa3"), "audio/ogg": audio.startswith(b"OggS"), "audio/wav": audio.startswith(b"RIFF") and audio[8:12] == b"WAVE"}
            if not signatures[body.audio.mime_type]:
                fail(422, "invalid_audio", "Audio signature does not match its MIME type")
        digest = hashlib.sha256(canonical(body.model_dump(mode="json")).encode()).hexdigest()
        with store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            replay = conn.execute("SELECT digest,response FROM idempotency WHERE tenant=? AND actor=? AND key=?", (actor.tenant_id, actor.actor_id, idempotency_key)).fetchone()
            if replay:
                if replay["digest"] != digest:
                    fail(409, "idempotency_conflict", "This key belongs to a different submission")
                response = json.loads(replay["response"])
                response["replayed"] = True
                code = 200
            else:
                identity, now = str(uuid.uuid4()), datetime.now(timezone.utc).isoformat()
                conn.execute("INSERT INTO feedback VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (identity, actor.tenant_id, actor.actor_id, 1, "new", body.category, body.message, canonical(body.context.model_dump(mode="json")), None, audio, body.audio.mime_type if audio else None, hashlib.sha256(audio).hexdigest() if audio else None, now, now))
                event(conn, actor, identity, 1, "feedback.submitted", started, request.scope["gradia.correlation_id"])
                response = {"status": "acknowledged", "feedback": record(fetch(conn, actor.tenant_id, identity)), "replayed": False}
                conn.execute("INSERT INTO idempotency VALUES(?,?,?,?,?)", (actor.tenant_id, actor.actor_id, idempotency_key, digest, canonical(response)))
                code = 201
        # The context manager commits before any acknowledgment is constructed.
        return JSONResponse(response, status_code=code)

    @app.get("/v1/feedback", response_model=FeedbackPage)
    def listing(limit: int = Query(default=25, ge=1, le=100), cursor: str = Query(default="", max_length=36), actor: Principal = Depends(operator)):
        with store.connection() as conn:
            rows = conn.execute("SELECT * FROM feedback WHERE tenant=? AND id>? ORDER BY id LIMIT ?", (actor.tenant_id, cursor, limit + 1)).fetchall()
            return {"items": [record(row) for row in rows[:limit]], "next_cursor": rows[limit-1]["id"] if len(rows) > limit else None}

    @app.get("/v1/feedback/{identity}", response_model=FeedbackRecord)
    def get_one(identity: str, actor: Principal = Depends(operator)):
        with store.connection() as conn:
            return record(fetch(conn, actor.tenant_id, identity))

    @app.patch("/v1/feedback/{identity}", response_model=TriageAcknowledgment)
    def triage(identity: str, body: Triage, request: Request, actor: Principal = Depends(operator)):
        started = time.monotonic()
        if not settings.features.triage:
            fail(403, "feature_disabled", "Triage is disabled")
        changes = body.model_dump(exclude_unset=True)
        changes.pop("expected_revision")
        if not changes or any(changes.get(field, "valid") is None for field in ("status", "category")):
            fail(422, "invalid_transition", "Provide at least one non-null triage field")
        if body.category is not None and body.category not in settings.config.categories:
            fail(422, "invalid_transition", "Unknown category")
        with store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = fetch(conn, actor.tenant_id, identity)
            if row["revision"] != body.expected_revision:
                fail(409, "revision_conflict", "Read the latest revision before changing feedback")
            fields = {field: changes.get(field, row[field]) for field in ("status", "category", "assignee")}
            revision = row["revision"] + 1
            conn.execute("UPDATE feedback SET status=?,category=?,assignee=?,revision=?,updated_at=? WHERE tenant=? AND id=?", (*fields.values(), revision, datetime.now(timezone.utc).isoformat(), actor.tenant_id, identity))
            event(conn, actor, identity, revision, "feedback.triaged", started, request.scope["gradia.correlation_id"])
            result = record(fetch(conn, actor.tenant_id, identity))
        return {"status": "acknowledged", "feedback": result}

    @app.get("/v1/feedback/{identity}/events")
    def events(identity: str, limit: int = Query(default=100, ge=1, le=100), after: int = Query(default=0, ge=0), actor: Principal = Depends(operator)):
        with store.connection() as conn:
            fetch(conn, actor.tenant_id, identity)
            rows = conn.execute("SELECT sequence,payload FROM events WHERE tenant=? AND feedback_id=? AND sequence>? ORDER BY sequence LIMIT ?", (actor.tenant_id, identity, after, limit)).fetchall()
            return {"items": [{"sequence": row["sequence"], **json.loads(row["payload"])} for row in rows], "next_after": rows[-1]["sequence"] if rows else after}

    @app.get("/v1/feedback/{identity}/audio")
    def get_audio(identity: str, actor: Principal = Depends(operator)):
        if not settings.features.audio:
            fail(403, "feature_disabled", "Audio is disabled")
        with store.connection() as conn:
            row = fetch(conn, actor.tenant_id, identity)
            if row["audio"] is None:
                fail(404, "not_found", "No audio attached")
            return Response(row["audio"], media_type=row["audio_mime"], headers={"Content-Disposition": "attachment; filename=feedback-audio", "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})

    return app

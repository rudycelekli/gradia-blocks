"""Bounded volatile HTTP evidence. No request bodies, URLs or credentials."""
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone


class AttemptStore:
    def __init__(self, tenants, capacity=100):
        self.capacity = capacity
        self._rings = {tenant: deque(maxlen=capacity) for tenant in {*tenants, None}}
        self._dropped = {tenant: 0 for tenant in self._rings}
        self._failures = {tenant: 0 for tenant in self._rings}
        self._lock = threading.Lock()

    def record(self, tenant, event):
        with self._lock:
            if len(self._rings[tenant]) == self.capacity:
                self._dropped[tenant] += 1
            self._rings[tenant].append(event)

    def note_failure(self, tenant):
        with self._lock:
            self._failures[tenant] += 1

    def snapshot(self, tenant):
        with self._lock:
            return {"items": list(self._rings[tenant]), "coverage": "volatile_http_attempts", "capacity": self.capacity,
                    "evicted": self._dropped[tenant], "collection_failures": self._failures[tenant],
                    "durability": "process_memory_only", "unauthenticated_attempts": "not_exposed_to_tenants"}


class AttemptTelemetry:
    def __init__(self, app, store, config_digest, version):
        self.app, self.store, self.config_digest, self.version = app, store, config_digest, version

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started, response_status = time.monotonic(), 500
        correlation_id = str(uuid.uuid4())
        scope["gradia.correlation_id"] = correlation_id
        async def observe_send(message):
            nonlocal response_status
            if message["type"] == "http.response.start":
                response_status = message["status"]
            await send(message)
        try:
            await self.app(scope, receive, observe_send)
        finally:
            actor = scope.get("gradia.principal")
            tenant = actor.tenant_id if actor else None
            route = scope.get("route")
            event = {"event_id": str(uuid.uuid4()), "correlation_id": correlation_id,
                     "block_id": "gradia.feedback", "edition": self.version, "configuration_digest": self.config_digest,
                     "tenant_id": tenant, "actor_id": actor.actor_id if actor else None,
                     "actor_type": "configured_principal" if actor else "unauthenticated", "human_involvement": "unknown",
                     "operation": getattr(route, "path", "unmatched"), "method": scope["method"],
                     "result": "http_response" if response_status < 400 else "http_refusal",
                     "http_status": response_status, "refusal_code": scope.get("gradia.error_code"),
                     "occurred_at": datetime.now(timezone.utc).isoformat(),
                     "duration_ms": round((time.monotonic() - started) * 1000, 3),
                     "cost": {"status": "unknown", "amount": None, "currency": None},
                     "privacy": "metadata_only", "coverage": "volatile_http_attempts"}
            try:
                self.store.record(tenant, event)
            except Exception:
                # Never rewrite an already committed mutation acknowledgment. The gap is observable.
                self.store.note_failure(tenant)

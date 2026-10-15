import base64
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from gradia_feedback import BlockSettings, Principal, create_app
from gradia_feedback.contracts import Features, RuntimeConfig

KEY_A = "a" * 32
KEY_B = "b" * 32
KEY_S = "s" * 32


def client(tmp_path, settings=None):
    return TestClient(create_app(tmp_path / "feedback.sqlite", {
        KEY_A: Principal(tenant_id="tenant-a", actor_id="alice", role="operator"),
        KEY_B: Principal(tenant_id="tenant-b", actor_id="bob", role="operator"),
        KEY_S: Principal(tenant_id="tenant-a", actor_id="sender", role="submitter"),
    }, settings))


def headers(key=KEY_A, idem="case-1"):
    return {"Authorization": f"Bearer {key}", "Idempotency-Key": idem}


def submit(c, **kwargs):
    return c.post("/v1/feedback", headers=headers(**kwargs), json={"message": "Improve keyboard navigation", "context": {"app_id": "reference"}})


def test_persistence_ack_replay_revision_and_events(tmp_path):
    c = client(tmp_path)
    first = submit(c)
    assert first.status_code == 201
    body = first.json()
    assert body["status"] == "acknowledged" and not body["replayed"]
    identity = body["feedback"]["id"]
    # A second app instance proves acknowledgment survives closing its writer.
    reopened = client(tmp_path)
    assert reopened.get(f"/v1/feedback/{identity}", headers=headers()).json()["message"] == "Improve keyboard navigation"
    replay = submit(reopened)
    assert replay.status_code == 200 and replay.json()["replayed"]
    assert replay.json()["feedback"]["id"] == identity
    changed = reopened.patch(f"/v1/feedback/{identity}", headers=headers(), json={"expected_revision": 1, "status": "in_review", "assignee": "reviewer"})
    assert changed.status_code == 200 and changed.json()["feedback"]["revision"] == 2
    assert reopened.patch(f"/v1/feedback/{identity}", headers=headers(), json={"expected_revision": 1, "status": "resolved"}).status_code == 409
    event_items = reopened.get(f"/v1/feedback/{identity}/events", headers=headers()).json()["items"]
    assert [item["operation"] for item in event_items] == ["feedback.submitted", "feedback.triaged"]
    assert event_items[0]["cost"]["status"] == "unknown"
    assert event_items[0]["correlation_id"]
    assert event_items[0]["correlation_id"] == c.app.state.attempts.snapshot("tenant-a")["items"][0]["correlation_id"]
    assert event_items[0]["privacy"] == "metadata_only"
    assert event_items[0]["human_involvement"] == "unknown"
    assert event_items[0]["coverage"] == "committed_mutations_only"
    # Stale revision refusal did not fabricate a successful mutation event.
    assert len(event_items) == 2
    assert "Improve keyboard" not in str(event_items)
    assert len(reopened.get("/v1/feedback", headers=headers()).json()["items"]) == 1


def test_tenant_and_role_boundaries(tmp_path):
    c = client(tmp_path)
    identity = submit(c, key=KEY_S).json()["feedback"]["id"]
    assert c.get("/v1/feedback", headers=headers(KEY_S)).status_code == 403
    assert c.get(f"/v1/feedback/{identity}", headers=headers(KEY_B)).status_code == 404
    assert c.patch(f"/v1/feedback/{identity}", headers=headers(KEY_B), json={"expected_revision": 1, "status": "resolved"}).status_code == 404
    assert c.get(f"/v1/feedback/{identity}/events", headers=headers(KEY_B)).status_code == 404
    assert c.get("/v1/feedback", headers=headers(KEY_B)).json()["items"] == []
    assert c.get("/v1/feedback/config").status_code == 401
    assert c.post("/v1/feedback", headers={**headers(), "X-Tenant-Id": "tenant-b"}, json={"message": "x", "tenant_id": "tenant-b"}).status_code == 422
    assert submit(c, key=KEY_B).status_code == 201  # Same key scoped to principal tenant/actor.


def test_conflicting_idempotency_and_concurrent_retry(tmp_path):
    c = client(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: submit(c), range(4)))
    assert sorted(r.status_code for r in responses) == [200, 200, 200, 201]
    assert len({r.json()["feedback"]["id"] for r in responses}) == 1
    assert c.post("/v1/feedback", headers=headers(), json={"message": "Different content"}).status_code == 409
    identity = responses[0].json()["feedback"]["id"]
    assert len(c.get(f"/v1/feedback/{identity}/events", headers=headers()).json()["items"]) == 1


def test_audio_gated_bounded_authorized_and_retrievable(tmp_path):
    audio = b"OggS" + b"reference-fixture"  # Signature fixture, not decoded media validation.
    payload = {"message": "Voice note", "audio": {"mime_type": "audio/ogg", "data_base64": base64.b64encode(audio).decode()}}
    c = client(tmp_path)
    assert c.post("/v1/feedback", headers=headers(), json=payload).status_code == 403
    c = client(tmp_path, BlockSettings(features=Features(audio=True)))
    submitted = c.post("/v1/feedback", headers=headers(), json=payload)
    assert submitted.status_code == 201
    identity = submitted.json()["feedback"]["id"]
    assert submitted.json()["feedback"]["audio"]["size_bytes"] == len(audio)
    assert c.get(f"/v1/feedback/{identity}/audio", headers=headers()).content == audio
    assert c.get(f"/v1/feedback/{identity}/audio", headers=headers(KEY_B)).status_code == 404
    assert c.get(f"/v1/feedback/{identity}/audio", headers=headers(KEY_S)).status_code == 403
    payload["audio"]["data_base64"] = "!!!"
    assert c.post("/v1/feedback", headers=headers(idem="bad"), json=payload).status_code == 422
    payload["audio"]["data_base64"] = base64.b64encode(b"not-ogg").decode()
    assert c.post("/v1/feedback", headers=headers(idem="bad"), json=payload).status_code == 422
    payload["audio"]["data_base64"] = base64.b64encode(audio).decode()
    limited = client(tmp_path, BlockSettings(features=Features(audio=True), config=RuntimeConfig(max_audio_bytes=4)))
    assert limited.post("/v1/feedback", headers=headers(idem="too-big"), json=payload).status_code == 413


def test_disabled_features_and_contract_bounds(tmp_path):
    c = client(tmp_path, BlockSettings(features=Features(enabled=False)))
    assert submit(c).status_code == 403
    c = client(tmp_path, BlockSettings(config=RuntimeConfig(max_message_chars=5)))
    assert submit(c).status_code == 422
    assert c.post("/v1/feedback", headers=headers(), json={"message": " ", "category": "general"}).status_code == 422
    assert c.post("/v1/feedback", headers=headers(), json={"message": "ok", "category": "fake"}).status_code == 422
    assert c.post("/v1/feedback", headers=headers(), json={"message": "ok", "context": {"page_url": "javascript:alert(1)"}}).status_code == 422
    assert c.post("/v1/feedback", headers=headers(), content=b"x" * 400001).status_code == 413
    assert c.get("/v1/feedback?limit=101", headers=headers()).status_code == 422
    c = client(tmp_path)
    identity = submit(c).json()["feedback"]["id"]
    no_triage = client(tmp_path, BlockSettings(features=Features(triage=False)))
    assert no_triage.patch(f"/v1/feedback/{identity}", headers=headers(), json={"expected_revision": 1, "status": "resolved"}).status_code == 403


def test_atomic_rollback_when_audit_fails(tmp_path):
    c = client(tmp_path)
    with sqlite3.connect(tmp_path / "feedback.sqlite") as conn:
        conn.execute("CREATE TRIGGER reject_audit BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT, 'fixture'); END")
    result = submit(c)
    assert result.status_code == 503 and result.json()["error"]["code"] == "persistence_unavailable"
    with sqlite3.connect(tmp_path / "feedback.sqlite") as conn:
        assert conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM idempotency").fetchone()[0] == 0
        conn.execute("DROP TRIGGER reject_audit")
    assert submit(c).status_code == 201


def test_no_default_credentials(tmp_path):
    with pytest.raises(ValueError, match="bearer"):
        create_app(tmp_path / "feedback.sqlite", {})


def test_timeout_after_commit_can_reconcile_same_key(tmp_path):
    c = client(tmp_path)
    # Lose the first HTTP response after server commit; retry the identical intent.
    submit(c)
    retry = submit(client(tmp_path))
    assert retry.status_code == 200 and retry.json()["replayed"]
    assert len(c.get("/v1/feedback", headers=headers()).json()["items"]) == 1


def test_revision_race_pagination_and_telemetry_coverage(tmp_path):
    c = client(tmp_path)
    identity = submit(c).json()["feedback"]["id"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda status: c.patch(f"/v1/feedback/{identity}", headers=headers(), json={"expected_revision": 1, "status": status}), ["in_review", "dismissed"]))
    assert sorted(result.status_code for result in results) == [200, 409]
    submit(c, idem="second")
    page = c.get("/v1/feedback?limit=1", headers=headers()).json()
    assert len(page["items"]) == 1 and page["next_cursor"]
    next_page = c.get(f"/v1/feedback?limit=1&cursor={page['next_cursor']}", headers=headers()).json()
    assert len(next_page["items"]) == 1 and next_page["items"][0]["id"] != page["items"][0]["id"]
    events = c.get(f"/v1/feedback/{identity}/events?limit=1", headers=headers()).json()
    later = c.get(f"/v1/feedback/{identity}/events?after={events['next_after']}", headers=headers()).json()
    assert len(later["items"]) == 1
    assert later["items"][0]["revision"] == 2
    assert later["items"][0]["acceptance"] == "not_evaluated"
    assert later["items"][0]["coverage"] == "committed_mutations_only"


def test_invalid_requests_do_not_echo_private_input(tmp_path):
    c = client(tmp_path)
    response = c.post("/v1/feedback", headers=headers(), json={"message": "private-canary", "unexpected": "secret-canary"})
    assert response.status_code == 422
    assert "canary" not in response.text
    schema = c.get("/openapi.json").json()
    assert "Submission" in schema["components"]["schemas"]
    assert "Acknowledgment" in schema["components"]["schemas"]
    assert KEY_A not in str(schema)


def test_http_attempt_spans_are_bounded_private_and_refusal_aware(tmp_path):
    c = client(tmp_path)
    c.get("/v1/feedback/config")
    submit(c)
    c.post("/v1/feedback", headers=headers(), json={"message": "conflicting-private-canary"})
    c.post("/v1/feedback", headers=headers(idem="invalid"), json={"message": "private-canary", "unexpected": "secret-canary"})
    spans = c.get("/v1/feedback/diagnostics/attempts", headers=headers()).json()
    assert [e["http_status"] for e in spans["items"]] == [201, 409, 422]
    assert spans["items"][1]["refusal_code"] == "idempotency_conflict"
    assert spans["items"][2]["refusal_code"] == "invalid_request"
    assert "canary" not in str(spans) and KEY_A not in str(spans)
    assert all(e["tenant_id"] == "tenant-a" for e in spans["items"])
    assert c.get("/v1/feedback/diagnostics/attempts", headers=headers(KEY_B)).json()["items"] == []
    assert c.get("/v1/feedback/diagnostics/attempts", headers=headers(KEY_S)).status_code == 403
    # Unauthenticated attempts are kept separately, never attributed using user claims.
    assert c.app.state.attempts.snapshot(None)["items"][0]["http_status"] == 401
    for _ in range(102):
        c.get("/v1/feedback/config", headers=headers())
    bounded = c.app.state.attempts.snapshot("tenant-a")
    assert len(bounded["items"]) == 100 and bounded["evicted"] > 0


def test_telemetry_gap_never_rewrites_committed_ack(tmp_path, monkeypatch):
    c = client(tmp_path)
    def unavailable(*args):
        raise RuntimeError("fixture sink failure")
    monkeypatch.setattr(c.app.state.attempts, "record", unavailable)
    first = submit(c)
    assert first.status_code == 201 and first.json()["status"] == "acknowledged"
    assert c.app.state.attempts.snapshot("tenant-a")["collection_failures"] == 1
    identity = first.json()["feedback"]["id"]
    with sqlite3.connect(tmp_path / "feedback.sqlite") as conn:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE feedback_id=?", (identity,)).fetchone()[0] == 1


def test_attempts_capture_database_and_disabled_refusals(tmp_path):
    c = client(tmp_path)
    with sqlite3.connect(tmp_path / "feedback.sqlite") as conn:
        conn.execute("CREATE TRIGGER reject_audit BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT, 'fixture'); END")
    assert submit(c).status_code == 503
    attempt = c.app.state.attempts.snapshot("tenant-a")["items"][-1]
    assert attempt["refusal_code"] == "persistence_unavailable" and attempt["result"] == "http_refusal"
    disabled = client(tmp_path, BlockSettings(features=Features(enabled=False)))
    assert submit(disabled).status_code == 403
    assert disabled.app.state.attempts.snapshot("tenant-a")["items"][-1]["refusal_code"] == "feature_disabled"


def test_sqlite_data_file_is_private_without_changing_existing_permissions(tmp_path):
    import os
    path = tmp_path / "feedback.sqlite"
    client(tmp_path)
    assert path.stat().st_mode & 0o077 == 0
    if os.name == "posix":
        path.chmod(0o644)
        with pytest.raises(ValueError, match="chmod 600"):
            client(tmp_path)
        assert path.stat().st_mode & 0o777 == 0o644

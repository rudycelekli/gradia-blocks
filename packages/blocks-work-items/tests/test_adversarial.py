"""Independent contract probes: failures must preserve authority and durable state."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from gradia_work_items import Principal, Settings, create_app


OP = "operator-a-" + "a" * 24
OTHER = "operator-b-" + "b" * 24
SUB = "submitter-a-" + "s" * 24
SUB2 = "submitter-a2-" + "t" * 24
BRIDGE = "bridge-a-" + "c" * 24
BRIDGE2 = "bridge-a2-" + "d" * 24
SECRET = "sensitive-account-92818@example.invalid"
PRIVATE_ACTOR = "sensitive-account-92818"


def auth(token=OP, key="test-create"):
    return {"Authorization": "Bearer " + token, "Idempotency-Key": key}


def client(tmp_path, settings=None):
    app = create_app(tmp_path / "items.sqlite", {
        OP: Principal(tenant_id="a", actor_id="operator", role="operator"),
        OTHER: Principal(tenant_id="b", actor_id="operator", role="operator"),
        SUB: Principal(tenant_id="a", actor_id="sender", role="submitter"),
        SUB2: Principal(tenant_id="a", actor_id="second", role="submitter"),
        BRIDGE: Principal(tenant_id="a", actor_id="pipeline", role="bridge"),
        BRIDGE2: Principal(tenant_id="a", actor_id="other-pipeline", role="bridge"),
    }, settings)
    return TestClient(app, raise_server_exceptions=False)


def post(c, token=OP, key="test-create", **values):
    return c.post("/v1/work-items", headers=auth(token, key), json={"title": "Investigate issue", **values})


def item_id(response):
    assert response.status_code in (200, 201), response.text
    return response.json()["item"]["id"]


def source(revision=1):
    return {"kind": "feedback", "id": "12345678-1234-1234-1234-123456789abc", "revision": revision, "sha256": "a" * 64}


def test_foreign_tenant_never_reads_or_mutates_known_identity(tmp_path):
    c = client(tmp_path)
    ident = item_id(post(c))
    for suffix in ("", "/events"):
        assert c.get(f"/v1/work-items/{ident}{suffix}", headers=auth(OTHER)).status_code == 404
    assert c.patch(f"/v1/work-items/{ident}", headers=auth(OTHER), json={"expected_revision": 1, "status": "open"}).status_code == 404
    assert c.get("/v1/work-items", headers=auth(OTHER)).json()["items"] == []
    assert c.get(f"/v1/work-items/{ident}", headers=auth()).json()["revision"] == 1


def test_submitters_and_bridges_only_read_their_own_items(tmp_path):
    c = client(tmp_path)
    sender = item_id(post(c, SUB))
    pipeline = item_id(post(c, BRIDGE, "bridge", source=source()))
    for token, forbidden in ((SUB2, sender), (BRIDGE, sender), (SUB, pipeline), (BRIDGE2, pipeline)):
        assert c.get(f"/v1/work-items/{forbidden}", headers=auth(token)).status_code == 404
        assert forbidden not in {v["id"] for v in c.get("/v1/work-items", headers=auth(token)).json()["items"]}
    for token in (SUB, BRIDGE):
        assert c.patch(f"/v1/work-items/{sender}", headers=auth(token), json={"expected_revision": 1, "status": "open"}).status_code == 403
        assert c.get(f"/v1/work-items/{sender}/events", headers=auth(token)).status_code == 403


def test_server_owned_tenant_and_source_authority_cannot_be_injected(tmp_path):
    c = client(tmp_path)
    for token in (SUB, OP):
        assert post(c, token, source=source()).status_code == 403
    assert post(c, tenant_id="b").status_code == 422
    response = c.post("/v1/work-items", headers={**auth(), "X-Tenant-Id": "b"}, json={"title": "Valid"})
    ident = item_id(response)
    assert c.get(f"/v1/work-items/{ident}", headers=auth(OTHER)).status_code == 404
    for path in ("/v1/work-items", "/v1/work-items/config", "/v1/work-items/session"):
        assert c.get(path).status_code == 401


def test_idempotency_replays_across_restart_and_refuses_changed_body(tmp_path):
    c = client(tmp_path)
    ident = item_id(post(c, description=SECRET))
    reopened = client(tmp_path)
    replay = post(reopened, description=SECRET)
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    assert replay.json()["item"]["id"] == ident
    assert post(reopened, description="different").status_code == 409
    assert len(reopened.get(f"/v1/work-items/{ident}/events", headers=auth()).json()["items"]) == 1


def test_concurrent_idempotency_and_cas_cannot_duplicate_or_overwrite(tmp_path):
    c = client(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: post(c), range(4)))
    assert sorted(r.status_code for r in responses) == [200, 200, 200, 201]
    assert len({item_id(r) for r in responses}) == 1
    ident = item_id(responses[0])
    with ThreadPoolExecutor(max_workers=2) as pool:
        updates = list(pool.map(lambda name: c.patch(f"/v1/work-items/{ident}", headers=auth(), json={"expected_revision": 1, "assignee": name}), ("first", "second")))
    assert sorted(r.status_code for r in updates) == [200, 409]
    stored = c.get(f"/v1/work-items/{ident}", headers=auth()).json()
    assert stored["revision"] == 2 and stored["assignee"] in ("first", "second")
    assert len(c.get(f"/v1/work-items/{ident}/events", headers=auth()).json()["items"]) == 2


def test_source_binding_survives_restart_and_new_idempotency_key(tmp_path):
    c = client(tmp_path)
    ident = item_id(post(c, BRIDGE, "first", source=source()))
    reopened = client(tmp_path)
    replay = post(reopened, BRIDGE, "other-key", source=source())
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    assert item_id(replay) == ident
    assert post(reopened, BRIDGE, "changed-title", source=source(), title="Altered").status_code == 409
    assert post(reopened, BRIDGE, "changed-revision", source=source(2)).status_code == 409
    # A separately configured pipeline is intentionally a different binding authority.
    assert item_id(post(reopened, BRIDGE2, "separate", source=source())) != ident


def test_known_bad_transition_and_stale_revision_leave_no_mutation(tmp_path):
    c = client(tmp_path)
    ident = item_id(post(c))
    for patch in ({"expected_revision": 1, "status": "nonexistent"}, {"expected_revision": 1, "status": "done"}, {"expected_revision": 1, "status": "proposed"}, {"expected_revision": True, "status": "open"}, {"expected_revision": "1", "status": "open"}, {"expected_revision": 1}):
        assert c.patch(f"/v1/work-items/{ident}", headers=auth(), json=patch).status_code in (409, 422)
    assert c.get(f"/v1/work-items/{ident}", headers=auth()).json()["revision"] == 1
    assert len(c.get(f"/v1/work-items/{ident}/events", headers=auth()).json()["items"]) == 1


def test_disabled_writes_preserve_reads_and_db_config_is_immutable(tmp_path):
    c = client(tmp_path)
    ident = item_id(post(c))
    disabled = client(tmp_path, Settings(enabled=False))
    assert post(disabled, key="disabled").status_code == 403
    assert disabled.patch(f"/v1/work-items/{ident}", headers=auth(), json={"expected_revision": 1, "status": "open"}).status_code == 403
    assert disabled.get(f"/v1/work-items/{ident}", headers=auth()).status_code == 200
    with pytest.raises(ValueError):
        client(tmp_path, Settings(initial_status="open"))


def test_invalid_json_duplicate_keys_deep_nesting_and_body_limit_are_safe(tmp_path):
    c = client(tmp_path)
    for payload in ('{"title":"first","title":"second"}', '{"title":' + "[" * 1500 + "0" + "]" * 1500 + "}", '{"title":"' + SECRET + '"'):
        r = c.post("/v1/work-items", headers={**auth(), "Content-Type": "application/json"}, content=payload)
        assert r.status_code in (400, 413, 422), r.text
        assert SECRET not in r.text
    assert c.post("/v1/work-items", headers=auth(), content=b"x" * (1024 * 1024)).status_code == 413
    assert c.get("/v1/work-items", headers=auth()).json()["items"] == []


def test_empty_and_unicode_credentials_are_safe_refusals(tmp_path):
    c = client(tmp_path)
    for value in ("", "Basic bogus", "Bearer unknown", "Bearer \N{SNOWMAN}"):
        # HTTPX headers must be explicitly UTF-8 encoded for the final fixture.
        r = c.get("/v1/work-items", headers={b"Authorization": value.encode("utf-8")})
        assert r.status_code == 401


def test_sensitive_content_is_absent_from_events_and_diagnostics(tmp_path):
    c = client(tmp_path)
    ident = item_id(post(c, title=SECRET, description=SECRET))
    assert c.patch(f"/v1/work-items/{ident}", headers=auth(), json={"expected_revision": 1, "assignee": PRIVATE_ACTOR}).status_code == 200
    evidence = c.get(f"/v1/work-items/{ident}/events", headers=auth()).json()
    assert SECRET not in json.dumps(evidence) and PRIVATE_ACTOR not in json.dumps(evidence)
    assert evidence["items"] and all(v["cost"]["status"] == "unknown" for v in evidence["items"])
    attempts = c.get("/v1/work-items/diagnostics/attempts", headers=auth())
    assert attempts.status_code == 200 and SECRET not in attempts.text and PRIVATE_ACTOR not in attempts.text
    assert c.get("/v1/work-items/diagnostics/attempts", headers=auth(SUB)).status_code == 403
    other = c.get("/v1/work-items/diagnostics/attempts", headers=auth(OTHER))
    assert ident not in other.text


def test_audit_failure_rolls_back_ack_and_idempotency_reservation(tmp_path):
    c = client(tmp_path)
    with sqlite3.connect(tmp_path / "items.sqlite") as db:
        db.execute("CREATE TRIGGER reject_audit BEFORE INSERT ON work_item_events BEGIN SELECT RAISE(ABORT, 'fixture'); END")
    assert post(c).status_code == 503
    assert c.get("/v1/work-items", headers=auth()).json()["items"] == []
    with sqlite3.connect(tmp_path / "items.sqlite") as db:
        db.execute("DROP TRIGGER reject_audit")
    assert post(c).status_code == 201


def test_non_workitems_database_is_not_silently_adopted(tmp_path):
    dbpath = tmp_path / "items.sqlite"
    with sqlite3.connect(dbpath) as db:
        db.execute("CREATE TABLE unrelated(secret TEXT)")
        db.execute("INSERT INTO unrelated VALUES('retained')")
    dbpath.chmod(0o600)
    with pytest.raises(ValueError):
        client(tmp_path)
    with sqlite3.connect(dbpath) as db:
        assert db.execute("SELECT secret FROM unrelated").fetchone()[0] == "retained"


def test_same_named_foreign_table_cannot_acquire_gradia_schema_marker(tmp_path):
    dbpath = tmp_path / "items.sqlite"
    with sqlite3.connect(dbpath) as db:
        db.execute("CREATE TABLE work_items(secret TEXT)")
        db.execute("INSERT INTO work_items VALUES('retained')")
    dbpath.chmod(0o600)
    with pytest.raises(ValueError):
        client(tmp_path)
    with sqlite3.connect(dbpath) as db:
        assert db.execute("SELECT secret FROM work_items").fetchone()[0] == "retained"
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='work_item_metadata'").fetchall()


def test_marked_database_with_replaced_table_is_refused_at_startup(tmp_path):
    client(tmp_path)
    with sqlite3.connect(tmp_path / "items.sqlite") as db:
        db.execute("DROP TABLE work_item_keys")
        db.execute("CREATE TABLE work_item_keys(secret TEXT)")
    with pytest.raises(ValueError):
        client(tmp_path)


def test_custom_graph_and_assignment_flags_are_enforced(tmp_path):
    custom = Settings(states=("queued", "review", "accepted"), initial_status="queued", transitions=(
        {"from_state": "queued", "to_state": "review"},
        {"from_state": "review", "to_state": "accepted"},
    ), assignments=False)
    c = client(tmp_path, custom)
    ident = item_id(post(c))
    assert c.get(f"/v1/work-items/{ident}", headers=auth()).json()["status"] == "queued"
    path = f"/v1/work-items/{ident}"
    assert c.patch(path, headers=auth(), json={"expected_revision": 1, "assignee": "someone"}).status_code == 403
    assert c.patch(path, headers=auth(), json={"expected_revision": 1, "status": "accepted"}).status_code == 422
    assert c.patch(path, headers=auth(), json={"expected_revision": 1, "status": "review"}).status_code == 200
    # Changing a runtime feature flag preserves existing records and graph edition.
    paused = custom.model_copy(update={"transitions_enabled": False})
    reopened = client(tmp_path, paused)
    assert reopened.patch(path, headers=auth(), json={"expected_revision": 2, "status": "accepted"}).status_code == 403
    assert reopened.get(path, headers=auth()).json()["revision"] == 2


def test_source_lookup_uses_same_tenant_and_pipeline_authority(tmp_path):
    c = client(tmp_path)
    ident = item_id(post(c, BRIDGE, source=source()))
    path = "/v1/work-items/by-source/feedback/" + source()["id"]
    assert c.get(path, headers=auth(BRIDGE)).json()["id"] == ident
    assert c.get(path, headers=auth(BRIDGE2)).status_code == 404
    assert c.get(path, headers=auth(OP)).status_code == 403


def test_pagination_is_bounded_and_has_no_cross_actor_records(tmp_path):
    c = client(tmp_path)
    for n in range(3):
        item_id(post(c, SUB, str(n)))
    item_id(post(c, SUB2, "other"))
    page = c.get("/v1/work-items?limit=2", headers=auth(SUB)).json()
    assert len(page["items"]) == 2 and page["next_cursor"]
    nextpage = c.get("/v1/work-items", params={"limit": 2, "cursor": page["next_cursor"]}, headers=auth(SUB)).json()
    ids = [v["id"] for v in page["items"] + nextpage["items"]]
    assert len(ids) == len(set(ids)) == 3
    for query in ("limit=0", "limit=101", "cursor=" + "z" * 10000):
        assert c.get("/v1/work-items?" + query, headers=auth()).status_code in (400, 422)

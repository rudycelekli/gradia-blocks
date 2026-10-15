"""Independent HTTP authorization, parser and metadata boundary probes."""
import json

from fastapi.testclient import TestClient
import httpx
import pytest

from gradia_outbox import Destination, Principal
from gradia_outbox.api import create_app

TOKEN = "operator-" + "a" * 32
SUBTOKEN = "submitter-" + "b" * 32
OTHER = "other-" + "c" * 32
SECRET = "customer-sensitive-value-571@example.invalid"


def client(tmp_path):
    app = create_app(tmp_path / "api.sqlite", {
        TOKEN: Principal("a", "operator", "operator"),
        SUBTOKEN: Principal("a", "submitter"),
        OTHER: Principal("b", "operator", "operator"),
    }, {"primary": Destination("a", "https://receiver.invalid/hooks", "fixture-signing-key-" + "k" * 32, "receiver")})
    calls = []
    def receiver(request):
        calls.append(request)
        return httpx.Response(503, text=SECRET)
    app.state.engine.sender = httpx.Client(transport=httpx.MockTransport(receiver))
    return TestClient(app, raise_server_exceptions=False), calls


def auth(token=TOKEN, **extra):
    return {"Authorization": "Bearer " + token, "Idempotency-Key": "test-key", **extra}


def enqueue(c, **extra):
    return c.post("/v1/outbox", headers=auth(), json={"destination_id": "primary", "event_type": "feedback.received", "payload": {"secret": SECRET}, **extra})


@pytest.mark.parametrize("header,value", [
    ("If-Gradia-Principal", "0" * 64), ("If-Gradia-Configuration", "0" * 64),
    ("If-Gradia-Block-Version", "future-version"),
])
def test_stale_authority_conditions_refuse_before_mutation_or_send(tmp_path, header, value):
    c, calls = client(tmp_path)
    created = enqueue(c)
    assert created.status_code == 201, created.text
    item = created.json()["item"]
    response = c.post(f"/v1/outbox/{item['id']}/deliver", headers=auth(**{header: value}), json={"expected_revision": 1})
    assert response.status_code == 409
    assert not calls
    assert c.get(f"/v1/outbox/{item['id']}", headers=auth()).json()["revision"] == 1


def test_submitter_cannot_deliver_reconcile_recover_or_read_events(tmp_path):
    c, calls = client(tmp_path)
    created = c.post("/v1/outbox", headers=auth(SUBTOKEN), json={"destination_id": "primary", "event_type": "event", "payload": {}})
    assert created.status_code == 201, created.text
    identity = created.json()["item"]["id"]
    for action in ("deliver", "reconcile", "mark-uncertain"):
        response = c.post(f"/v1/outbox/{identity}/{action}", headers=auth(SUBTOKEN), json={"expected_revision": 1})
        assert response.status_code == 403
    assert c.get(f"/v1/outbox/{identity}/events", headers=auth(SUBTOKEN)).status_code == 403
    assert c.get("/v1/outbox/diagnostics/attempts", headers=auth(SUBTOKEN)).status_code == 403
    assert not calls


def test_foreign_tenant_known_identity_and_destination_cannot_be_accessed(tmp_path):
    c, calls = client(tmp_path)
    identity = enqueue(c).json()["item"]["id"]
    assert c.get(f"/v1/outbox/{identity}", headers=auth(OTHER)).status_code == 404
    assert c.get(f"/v1/outbox/{identity}/events", headers=auth(OTHER)).status_code == 404
    assert c.get("/v1/outbox", headers=auth(OTHER)).json()["items"] == []
    assert c.get("/v1/outbox/destinations", headers=auth(OTHER)).json()["items"] == []
    assert c.post(f"/v1/outbox/{identity}/deliver", headers=auth(OTHER), json={"expected_revision": 1}).status_code == 404
    assert not calls


@pytest.mark.parametrize("header", ["authorization", "if-gradia-principal", "if-gradia-configuration", "if-gradia-block-version", "idempotency-key"])
def test_duplicate_authority_or_key_header_refused(tmp_path, header):
    c, calls = client(tmp_path)
    headers = [(k, v) for k, v in auth().items()]
    value = {"authorization": "Bearer " + TOKEN, "if-gradia-principal": "0" * 64,
             "if-gradia-configuration": "0" * 64, "if-gradia-block-version": "0.1.0", "idempotency-key": "test-key"}[header]
    if header not in ("authorization", "idempotency-key"):
        headers.append((header, value))
    headers.append((header, value))
    response = c.post("/v1/outbox", headers=headers, json={"destination_id": "primary", "event_type": "event", "payload": {}})
    assert response.status_code == 400, response.text
    assert c.get("/v1/outbox", headers=auth()).json()["items"] == []
    assert not calls


@pytest.mark.parametrize("body", [
    b'{"destination_id":"primary","destination_id":"other","event_type":"event","payload":{}}',
    b'{"destination_id":"primary","event_type":"event","payload":{"v":NaN}}',
    b'{"destination_id":"primary","event_type":"event","payload":{"v":Infinity}}',
    b'{"destination_id":"primary","event_type":"event","payload":' + b"[" * 100 + b"0" + b"]" * 100 + b"}",
    b'{"destination_id":"primary","event_type":"event","payload":{"v":1e999}}',
    b"\xff", b"x" * 50000,
])
def test_malformed_requests_are_sanitized_and_never_enqueued(tmp_path, body):
    c, calls = client(tmp_path)
    response = c.post("/v1/outbox", headers=auth(), content=body)
    assert response.status_code in (413, 422), response.text
    assert SECRET not in response.text
    assert c.get("/v1/outbox", headers=auth()).json()["items"] == []
    assert not calls


def test_payload_extra_authority_and_raw_transport_error_absent_from_evidence(tmp_path):
    c, calls = client(tmp_path)
    assert enqueue(c, url="https://attacker.invalid/").status_code == 422
    identity = enqueue(c).json()["item"]["id"]
    response = c.post(f"/v1/outbox/{identity}/deliver", headers=auth(), json={"expected_revision": 1})
    assert response.status_code == 200 and response.json()["status"] == "uncertain"
    for path in (f"/v1/outbox/{identity}", f"/v1/outbox/{identity}/events", "/v1/outbox/diagnostics/attempts"):
        evidence = c.get(path, headers=auth())
        assert evidence.status_code == 200
        assert SECRET not in evidence.text and TOKEN not in evidence.text
        assert evidence.headers["X-Gradia-Cost-Status"] == "unknown"
    assert len(calls) == 1


@pytest.mark.parametrize("revision", [True, "1", 0, -1, 1.5])
def test_invalid_revisions_never_send(tmp_path, revision):
    c, calls = client(tmp_path)
    identity = enqueue(c).json()["item"]["id"]
    response = c.post(f"/v1/outbox/{identity}/deliver", headers=auth(), json={"expected_revision": revision})
    assert response.status_code == 422
    assert not calls

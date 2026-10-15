"""Independent failure probes: outbound authority never follows untrusted payloads."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import json
import os
import sqlite3
import threading

import httpx
import pytest

from gradia_outbox import Destination, Engine, OutboxError, Principal, canonical_json

KEY = "test-only-signing-key-" + "k" * 32
SECRET = "private-customer-content-98172@example.invalid"
OP = Principal("tenant-a", "operator", "operator")
SUB = Principal("tenant-a", "submitter")
OTHER = Principal("tenant-b", "operator", "operator")


def destination(**changes):
    return Destination(**{"tenant_id": "tenant-a", "url": "https://receiver.invalid/hooks", "signing_key": KEY, "receiver_id": "receiver-a", **changes})


def engine(tmp_path, handler=None, **changes):
    client = httpx.Client(transport=httpx.MockTransport(handler)) if handler else None
    return Engine(tmp_path / "outbox.sqlite", {"primary": destination(**changes)}, sender=client)


def enqueue(e, who=SUB, key="same", payload=None):
    return e.enqueue(who, "primary", "feedback.received", {"message": SECRET} if payload is None else payload, key)["item"]


def ack(request, *, changes=None, signed=True, code=200, raw=None, extra_headers=None):
    body = {"schema": "gradia.webhook.ack.v1", "delivery_id": request.headers["X-Gradia-Delivery-Id"],
            "payload_sha256": request.headers["X-Gradia-Payload-Sha256"], "receiver_id": "receiver-a", "accepted": True}
    body.update(changes or {})
    content = canonical_json(body) if raw is None else raw
    material = b"gradia.webhook.ack.v1." + request.headers["X-Gradia-Timestamp"].encode() + b"." + request.headers["X-Gradia-Attempt-Id"].encode() + b"." + canonical_json(body)
    headers = {"Content-Type": "application/json"}
    if signed:
        headers["X-Gradia-Ack-Signature"] = "sha256=" + hmac.new(KEY.encode(), material, hashlib.sha256).hexdigest()
    headers.update(extra_headers or {})
    return httpx.Response(code, headers=headers, stream=httpx.ByteStream(content))


def fails(code, call):
    with pytest.raises(OutboxError) as failure:
        call()
    assert failure.value.status_code == code


def test_concurrent_enqueue_reserves_one_identity_and_atomic_event(tmp_path):
    e = engine(tmp_path)
    with ThreadPoolExecutor(max_workers=6) as pool:
        records = list(pool.map(lambda _: enqueue(e), range(6)))
    assert len({v["id"] for v in records}) == 1
    assert len(e.list(OP)) == 1
    assert len(e.events(OP, records[0]["id"])) == 1
    fails(409, lambda: enqueue(e, payload={"message": "changed"}))


def test_concurrent_delivery_claims_only_one_network_send(tmp_path):
    entered, release = threading.Event(), threading.Event()
    sent = []
    def receiver(request):
        sent.append(request)
        entered.set()
        assert release.wait(5)
        return ack(request)
    e = engine(tmp_path, receiver)
    item = enqueue(e)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(e.deliver, OP, item["id"], 1)
        assert entered.wait(5)
        fails(409, lambda: e.deliver(OP, item["id"], 1))
        release.set()
        assert first.result()["status"] == "delivered"
    assert len(sent) == 1


def test_post_commit_response_loss_only_reconciles_without_second_post(tmp_path):
    requests, accepted = [], {}
    def receiver(request):
        requests.append(request)
        if request.method == "POST":
            accepted[request.headers["X-Gradia-Delivery-Id"]] = request.content
            raise httpx.ReadError(SECRET, request=request)
        assert request.url.path.endswith("/receipts/" + request.headers["X-Gradia-Delivery-Id"])
        assert request.headers["X-Gradia-Delivery-Id"] in accepted
        return ack(request)
    e = engine(tmp_path, receiver)
    item = enqueue(e)
    uncertain = e.deliver(OP, item["id"], 1)
    assert uncertain["status"] == "uncertain"
    e = engine(tmp_path, receiver)
    fails(409, lambda: e.deliver(OP, item["id"], uncertain["revision"]))
    recovered = e.reconcile(OP, item["id"], uncertain["revision"])
    assert recovered["status"] == "delivered"
    assert [r.method for r in requests] == ["POST", "GET"]
    assert SECRET not in json.dumps(e.events(OP, item["id"]))


@pytest.mark.parametrize("changes", [
    {"accepted": 1}, {"accepted": "true"}, {"accepted": False},
    {"receiver_id": "other"}, {"delivery_id": "other"}, {"payload_sha256": "0" * 64},
    {"schema": "other"}, {"unexpected": True},
])
def test_even_signed_unbound_or_mistyped_ack_is_uncertain(tmp_path, changes):
    e = engine(tmp_path, lambda r: ack(r, changes=changes))
    item = enqueue(e)
    assert e.deliver(OP, item["id"], 1)["status"] == "uncertain"


@pytest.mark.parametrize("raw", [
    b'{"accepted":true,"accepted":true}', b'{"accepted":NaN}',
    b"[" * 1500 + b"0" + b"]" * 1500, b"x" * 17000, b"not-json", b"\xff",
])
def test_malformed_bounded_receiver_body_never_acknowledges(tmp_path, raw):
    e = engine(tmp_path, lambda r: ack(r, raw=raw))
    item = enqueue(e)
    assert e.deliver(OP, item["id"], 1)["status"] == "uncertain"


def test_unsigned_ack_and_redirect_are_not_followed(tmp_path):
    requests = []
    def receiver(request):
        requests.append(request)
        return ack(request, signed=False, code=302, extra_headers={"Location": "https://attacker.invalid/"})
    e = engine(tmp_path, receiver)
    item = enqueue(e)
    assert e.deliver(OP, item["id"], 1)["status"] == "uncertain"
    assert len(requests) == 1 and requests[0].url.host == "receiver.invalid"
    e.sender = httpx.Client(transport=httpx.MockTransport(lambda r: ack(r, signed=False)))
    assert e.reconcile(OP, item["id"], 3)["status"] == "uncertain"


def test_receipt_signature_bound_to_attempt_not_replayed_from_other_delivery(tmp_path):
    old = []
    def receiver(request):
        response = ack(request)
        old.append(response.headers["X-Gradia-Ack-Signature"])
        return response
    e = engine(tmp_path, receiver)
    first = enqueue(e)
    assert e.deliver(OP, first["id"], 1)["status"] == "delivered"
    second = enqueue(e, key="second")
    e.sender = httpx.Client(transport=httpx.MockTransport(lambda r: ack(r, extra_headers={"X-Gradia-Ack-Signature": old[0]})))
    assert e.deliver(OP, second["id"], 1)["status"] == "uncertain"


@pytest.mark.parametrize("changes", [
    {"url": "https://other.invalid/hooks"}, {"receiver_id": "other"},
    {"signing_key": "replacement-signing-key-" + "r" * 32}, {"tenant_id": "tenant-b"},
])
def test_restart_cannot_silently_redirect_pending_customer_payload(tmp_path, changes):
    e = engine(tmp_path)
    enqueue(e)
    with pytest.raises(ValueError):
        engine(tmp_path, **changes)


@pytest.mark.parametrize("url", [
    "http://receiver.invalid/hook", "http://localhost/hook", "ftp://receiver.invalid/hook",
    "https://user:password@receiver.invalid/hook", "https://receiver.invalid/hook?token=secret",
    "https://receiver.invalid/hook#fragment", "https://receiver.invalid/\r\ninjected",
])
def test_destination_authority_refuses_unsafe_or_ambiguous_urls(url):
    with pytest.raises(ValueError):
        destination(url=url)


def test_untrusted_payload_cannot_choose_outbound_destination(tmp_path):
    requests = []
    def receiver(request):
        requests.append(request)
        return ack(request)
    e = engine(tmp_path, receiver)
    item = enqueue(e, payload={"url": "https://attacker.invalid/", "receiver_id": "attacker", "signing_key": "attacker"})
    assert e.deliver(OP, item["id"], 1)["status"] == "delivered"
    assert requests[0].url == httpx.URL("https://receiver.invalid/hooks")
    envelope = json.loads(requests[0].content)
    assert envelope["payload_sha256"] == hashlib.sha256(canonical_json(envelope["payload"])).hexdigest()
    material = requests[0].headers["X-Gradia-Timestamp"].encode() + b"." + requests[0].content
    assert requests[0].headers["X-Gradia-Signature"] == "sha256=" + hmac.new(KEY.encode(), material, hashlib.sha256).hexdigest()


def test_tenant_actor_and_role_boundaries_hold_for_known_id(tmp_path):
    e = engine(tmp_path)
    item = enqueue(e)
    for who in (OTHER, Principal("tenant-a", "unrelated")):
        fails(404, lambda: e.get(who, item["id"]))
        assert e.list(who) == []
    for who in (SUB, OTHER):
        fails(403 if who == SUB else 404, lambda: e.deliver(who, item["id"], 1))
    fails(403, lambda: e.events(SUB, item["id"]))
    fails(404, lambda: enqueue(e, OTHER))
    assert e.get(OP, item["id"])["status"] == "queued"


@pytest.mark.parametrize("sql", [
    "CREATE TRIGGER tamper AFTER UPDATE ON outbox_items BEGIN UPDATE outbox_items SET status='delivered'; END",
    "CREATE VIEW foreign_view AS SELECT * FROM outbox_items",
    "CREATE TABLE foreign_table(id TEXT)",
])
def test_restart_refuses_foreign_trigger_view_or_table(tmp_path, sql):
    engine(tmp_path)
    with sqlite3.connect(tmp_path / "outbox.sqlite") as conn:
        conn.execute(sql)
    with pytest.raises(ValueError):
        engine(tmp_path)


def test_audit_insert_failure_rolls_back_claim_before_any_network_send(tmp_path):
    requests = []
    e = engine(tmp_path, lambda r: requests.append(r) or ack(r))
    item = enqueue(e)
    with sqlite3.connect(tmp_path / "outbox.sqlite") as conn:
        conn.execute("CREATE TRIGGER reject_audit BEFORE INSERT ON outbox_events BEGIN SELECT RAISE(ABORT,'fixture'); END")
    with pytest.raises(sqlite3.Error):
        e.deliver(OP, item["id"], 1)
    assert not requests
    assert e.get(OP, item["id"])["status"] == "queued"


def test_recovery_of_crashed_claim_never_resends(tmp_path):
    sent = []
    class Crash(BaseException):
        pass
    def receiver(request):
        sent.append(request)
        raise Crash()
    e = engine(tmp_path, receiver)
    item = enqueue(e)
    with pytest.raises(Crash):
        e.deliver(OP, item["id"], 1)
    e = engine(tmp_path, lambda r: ack(r, code=404))
    current = e.get(OP, item["id"])
    assert current["status"] == "in_flight"
    fails(409, lambda: e.deliver(OP, item["id"], current["revision"]))
    marked = e.mark_uncertain(OP, item["id"], current["revision"])
    assert e.reconcile(OP, item["id"], marked["revision"])["status"] == "uncertain"
    assert len(sent) == 1


def test_private_file_symlink_hardlink_and_world_readable_refused(tmp_path):
    path = tmp_path / "outbox.sqlite"
    target = tmp_path / "target.sqlite"
    target.touch(mode=0o600)
    path.symlink_to(target)
    with pytest.raises((OSError, ValueError)):
        engine(tmp_path)
    path.unlink()
    os.link(target, path)
    with pytest.raises(ValueError):
        engine(tmp_path)
    path.unlink()
    path.touch(mode=0o644)
    path.chmod(0o644)
    with pytest.raises(ValueError):
        engine(tmp_path)


@pytest.mark.parametrize("payload", [{"x": float("nan")}, {"x": float("inf")}, {"x": 9007199254740992}, {"x": b"bytes"}, {"x": "a" * 17000}])
def test_invalid_or_oversize_payload_never_reserves_key(tmp_path, payload):
    e = engine(tmp_path)
    with pytest.raises(OutboxError) as failure:
        enqueue(e, payload=payload)
    assert failure.value.status_code in (413, 422)
    assert e.list(OP) == []
    assert enqueue(e)["revision"] == 1


def test_metadata_does_not_disclose_payload_or_signing_key(tmp_path):
    e = engine(tmp_path, lambda r: ack(r))
    item = enqueue(e)
    result = e.deliver(OP, item["id"], 1)
    metadata = json.dumps({"create": item, "delivery": result, "list": e.list(OP), "events": e.events(OP, item["id"])})
    assert SECRET not in metadata and KEY not in metadata
    assert all(event["cost"]["status"] == "unknown" and event["acceptance"] == "not_evaluated" for event in e.events(OP, item["id"]))


def test_revision_exhaustion_is_refused_before_irreversible_send(tmp_path):
    requests = []
    e = engine(tmp_path, lambda r: requests.append(r) or ack(r))
    item = enqueue(e)
    almost_maximum = 9007199254740990
    with sqlite3.connect(tmp_path / "outbox.sqlite") as conn:
        conn.execute("UPDATE outbox_items SET revision=? WHERE id=?", (almost_maximum, item["id"]))
    fails(409, lambda: e.deliver(OP, item["id"], almost_maximum))
    assert not requests
    assert e.get(OP, item["id"])["status"] == "queued"


def test_enqueued_payload_is_frozen_and_key_reordering_replays_same_identity(tmp_path):
    captured = []
    def receiver(request):
        captured.append(json.loads(request.content))
        return ack(request)
    e = engine(tmp_path, receiver)
    payload = {"nested": {"b": 2, "a": 1}, "message": "original"}
    item = enqueue(e, payload=payload)
    payload["message"] = "mutated"
    payload["nested"]["a"] = 999
    replay = enqueue(e, payload={"message": "original", "nested": {"a": 1, "b": 2}})
    assert replay["id"] == item["id"]
    assert e.deliver(OP, item["id"], 1)["status"] == "delivered"
    assert captured[0]["payload"] == {"message": "original", "nested": {"a": 1, "b": 2}}


def test_operator_recovery_while_sender_active_cannot_overwrite_newer_state(tmp_path):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def receiver(request):
        calls.append(request)
        entered.set()
        assert release.wait(5)
        return ack(request)
    e = engine(tmp_path, receiver)
    item = enqueue(e)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(e.deliver, OP, item["id"], 1)
        assert entered.wait(5)
        marked = e.mark_uncertain(OP, item["id"], 2)
        assert marked["status"] == "uncertain" and marked["revision"] == 3
        release.set()
        fails(409, pending.result)
    assert e.get(OP, item["id"])["status"] == "uncertain"
    fails(409, lambda: e.deliver(OP, item["id"], 3))
    assert len(calls) == 1

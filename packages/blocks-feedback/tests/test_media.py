"""Signature fixtures test boundaries, not full media decoding or malware scanning."""
import base64
import hashlib
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from gradia_feedback import BlockSettings, Principal, create_app
from gradia_feedback.contracts import AttachmentStorageSettings, Features, RuntimeConfig

KEY_A, KEY_B, KEY_S = "a" * 32, "b" * 32, "s" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + b"x" * 17
JPEG = b"\xff\xd8\xff\xe0fixture\xff\xd9"
WEBP = b"RIFF" + b"\x00" * 4 + b"WEBPVP8 " + b"fixture"
WEBM = b"\x1aE\xdf\xa3\x00\x00\x00webmfixture"
MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00fixture"


def headers(key=KEY_A, idem="media-1"):
    return {"Authorization": "Bearer " + key, "Idempotency-Key": idem}


def attachment(data=PNG, kind="screenshot", mime="image/png"):
    return {"kind": kind, "mime_type": mime, "data_base64": base64.b64encode(data).decode()}


def payload(*items):
    return {"message": "Private feedback", "attachments": list(items) or [attachment()]}


def client(tmp_path, backend="sqlite", settings=None):
    storage = AttachmentStorageSettings(backend=backend, directory=str(tmp_path / "media") if backend == "filesystem" else None)
    return TestClient(create_app(tmp_path / "feedback.sqlite", {
        KEY_A: Principal(tenant_id="a", role="operator"),
        KEY_B: Principal(tenant_id="b", role="operator"),
        KEY_S: Principal(tenant_id="a", role="submitter"),
    }, settings or BlockSettings(features=Features(photo=True, screenshot=True, video=True)), attachment_storage=storage))


@pytest.mark.parametrize("backend", ["sqlite", "filesystem"])
def test_media_durable_replay_authorization_and_metadata(tmp_path, backend):
    c = client(tmp_path, backend)
    items = [attachment(PNG), attachment(JPEG, "photo", "image/jpeg"), attachment(WEBM, "video", "video/webm")]
    body = payload(*items)
    result = c.post("/v1/feedback", headers=headers(KEY_S), json=body)
    assert result.status_code == 201
    record = result.json()["feedback"]
    assert len(record["attachments"]) == 3
    identity = record["id"]
    # Fresh app instance demonstrates persisted attachment metadata + bytes.
    reopened = client(tmp_path, backend)
    loaded = reopened.get(f"/v1/feedback/{identity}", headers=headers()).json()
    assert loaded == record
    source_by_hash = {hashlib.sha256(data).hexdigest(): data for data in (PNG, JPEG, WEBM)}
    for item in loaded["attachments"]:
        path = f"/v1/feedback/{identity}/attachments/{item['id']}"
        download = reopened.get(path, headers=headers())
        assert download.status_code == 200
        assert download.content == source_by_hash[item["sha256"]]
        assert download.headers["cache-control"] == "no-store"
        assert download.headers["x-content-type-options"] == "nosniff"
        assert "attachment;" in download.headers["content-disposition"]
        assert reopened.get(path, headers=headers(KEY_B)).status_code == 404
        assert reopened.get(path, headers=headers(KEY_S)).status_code == 403
        assert reopened.get(path).status_code == 401
    replay = reopened.post("/v1/feedback", headers=headers(KEY_S), json=body)
    assert replay.status_code == 200 and replay.json()["replayed"]
    assert replay.json()["feedback"] == record
    body["attachments"][0] = attachment(WEBP, "photo", "image/webp")
    assert reopened.post("/v1/feedback", headers=headers(KEY_S), json=body).status_code == 409
    assert str(tmp_path) not in json.dumps(reopened.get("/v1/feedback/config", headers=headers()).json())
    events = reopened.get(f"/v1/feedback/{identity}/events", headers=headers()).json()["items"]
    assert len(events) == 1 and "data_base64" not in json.dumps(events)
    assert "Private feedback" not in json.dumps(events)
    if backend == "filesystem":
        assert len(list((tmp_path / "media").iterdir())) == 3
        assert all(p.stat().st_mode & 0o077 == 0 for p in (tmp_path / "media").iterdir())


@pytest.mark.parametrize("data,kind,mime", [(PNG,"screenshot","image/png"), (JPEG,"photo","image/jpeg"), (WEBP,"photo","image/webp"), (WEBM,"video","video/webm"), (MP4,"video","video/mp4")])
def test_all_media_signature_families(tmp_path, data, kind, mime):
    c = client(tmp_path)
    assert c.post("/v1/feedback", headers=headers(), json=payload(attachment(data, kind, mime))).status_code == 201


def test_gates_limits_signatures_and_unknown_fields(tmp_path):
    c = client(tmp_path, settings=BlockSettings())
    assert c.post("/v1/feedback", headers=headers(), json=payload()).status_code == 403
    c = client(tmp_path, settings=BlockSettings(features=Features(screenshot=True), config=RuntimeConfig(max_attachment_bytes=40, max_total_attachment_bytes=60, max_attachments=2)))
    assert c.post("/v1/feedback", headers=headers(), json=payload(attachment(PNG+b"x"*10))).status_code == 413
    assert c.post("/v1/feedback", headers=headers(), json=payload(attachment(), attachment())).status_code == 413
    assert c.post("/v1/feedback", headers=headers(), json=payload(attachment(), attachment(), attachment())).status_code == 413
    assert c.post("/v1/feedback", headers=headers(), json=payload(attachment(b"<svg onload=evil>"))).status_code == 422
    assert c.post("/v1/feedback", headers=headers(), json=payload(attachment(PNG,"screenshot","video/mp4"))).status_code == 422
    bad = attachment(); bad["data_base64"] = "!not-base64"
    assert c.post("/v1/feedback", headers=headers(), json=payload(bad)).status_code == 422
    bad = attachment(); bad["path"] = "../../secrets"
    assert c.post("/v1/feedback", headers=headers(), json=payload(bad)).status_code == 422
    assert c.post("/v1/feedback", headers=headers(), json=payload(attachment(PNG,"photo"))).status_code == 403


@pytest.mark.parametrize("backend", ["sqlite", "filesystem"])
def test_rollback_on_audit_failure_leaves_no_ack_or_media(tmp_path, backend):
    c = client(tmp_path, backend)
    with sqlite3.connect(tmp_path / "feedback.sqlite") as conn:
        conn.execute("CREATE TRIGGER reject_audit BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT, 'fixture'); END")
    assert c.post("/v1/feedback", headers=headers(), json=payload()).status_code == 503
    with sqlite3.connect(tmp_path / "feedback.sqlite") as conn:
        for table in ("feedback", "attachments", "events", "idempotency"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        conn.execute("DROP TRIGGER reject_audit")
    assert c.app.state.store.orphan_files() == []
    if backend == "filesystem":
        assert list((tmp_path / "media").iterdir()) == []
    assert c.post("/v1/feedback", headers=headers(), json=payload()).status_code == 201


def test_filesystem_write_failure_and_corrupt_download_fail_closed(tmp_path, monkeypatch):
    c = client(tmp_path, "filesystem")
    import gradia_feedback.store as module
    original = module.os.fsync
    def broken(_):
        raise OSError("fixture storage failure")
    monkeypatch.setattr(module.os, "fsync", broken)
    assert c.post("/v1/feedback", headers=headers(), json=payload()).status_code == 503
    assert list((tmp_path / "media").iterdir()) == []
    monkeypatch.setattr(module.os, "fsync", original)
    item = c.post("/v1/feedback", headers=headers(), json=payload()).json()["feedback"]
    media_path = next((tmp_path / "media").iterdir())
    media_path.write_bytes(b"corrupted")
    response = c.get(f"/v1/feedback/{item['id']}/attachments/{item['attachments'][0]['id']}", headers=headers())
    assert response.status_code == 503 and str(tmp_path) not in response.text


def test_filesystem_private_paths_and_db_path_tampering(tmp_path):
    external = tmp_path / "outside"; external.mkdir(mode=0o700)
    symlink = tmp_path / "link"; symlink.symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        create_app(tmp_path / "db", {KEY_A: Principal(tenant_id="a")}, attachment_storage={"backend":"filesystem", "directory":str(symlink)})
    with pytest.raises(ValueError, match="absolute"):
        create_app(tmp_path / "db", {KEY_A: Principal(tenant_id="a")}, attachment_storage={"backend":"filesystem", "directory":"../../elsewhere"})
    public = tmp_path / "public"; public.mkdir(mode=0o755)
    with pytest.raises(ValueError, match="private"):
        create_app(tmp_path / "db", {KEY_A: Principal(tenant_id="a")}, attachment_storage={"backend":"filesystem", "directory":str(public)})
    c = client(tmp_path, "filesystem")
    item = c.post("/v1/feedback", headers=headers(), json=payload()).json()["feedback"]
    with sqlite3.connect(tmp_path / "feedback.sqlite") as conn:
        conn.execute("UPDATE attachments SET storage_key='../../outside'")
    assert c.get(f"/v1/feedback/{item['id']}/attachments/{item['attachments'][0]['id']}", headers=headers()).status_code == 503


def test_filesystem_concurrent_idempotency_no_duplicates_or_crash_orphan_deletion(tmp_path):
    c = client(tmp_path, "filesystem")
    with ThreadPoolExecutor(max_workers=4) as executor:
        responses = list(executor.map(lambda _: c.post("/v1/feedback", headers=headers(), json=payload()), range(4)))
    assert sorted(r.status_code for r in responses) == [200,200,200,201]
    assert len(list((tmp_path / "media").iterdir())) == 1
    orphan = tmp_path / "media" / ("a" * 32)
    orphan.write_bytes(b"crash-orphan"); orphan.chmod(0o600)
    reopened = client(tmp_path, "filesystem")
    assert reopened.app.state.store.orphan_files() == [orphan.name]
    assert orphan.exists()  # Inspection never races staging writers with automatic deletion.


def test_legacy_idempotency_upgrade_and_disabled_download(tmp_path):
    c = client(tmp_path)
    first = c.post("/v1/feedback", headers=headers(), json={"message":"legacy"})
    # v0.1 stored digest omitted attachments; this models the exact old canonical intent.
    legacy = {"message":"legacy", "category":"general", "context":{"page_url":None,"app_id":None},"audio":None}
    digest = hashlib.sha256(json.dumps(legacy,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()
    with sqlite3.connect(tmp_path / "feedback.sqlite") as conn:
        assert conn.execute("SELECT digest FROM idempotency").fetchone()[0] == digest
    assert c.post("/v1/feedback", headers=headers(), json={"message":"legacy"}).status_code == 200
    item = c.post("/v1/feedback", headers=headers(idem="attached"), json=payload()).json()["feedback"]
    disabled = client(tmp_path, settings=BlockSettings())
    assert disabled.get(f"/v1/feedback/{item['id']}/attachments/{item['attachments'][0]['id']}",headers=headers()).status_code == 403


def test_storage_identity_cannot_silently_detach_acknowledged_media(tmp_path):
    c = client(tmp_path, "filesystem")
    item = c.post("/v1/feedback",headers=headers(),json=payload()).json()["feedback"]
    with pytest.raises(ValueError, match="explicitly migrate"):
        client(tmp_path, "sqlite")
    another = tmp_path / "elsewhere"; another.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="explicitly migrate"):
        create_app(tmp_path / "feedback.sqlite",{KEY_A:Principal(tenant_id="a",role="operator")},attachment_storage={"backend":"filesystem","directory":str(another)})
    reopened = client(tmp_path,"filesystem")
    assert reopened.get(f"/v1/feedback/{item['id']}/attachments/{item['attachments'][0]['id']}",headers=headers()).content == PNG
    with sqlite3.connect(tmp_path / "feedback.sqlite") as conn:
        assert conn.execute("SELECT value FROM block_metadata WHERE key='schema_version'").fetchone()[0] == "1"
        assert conn.execute("SELECT value FROM block_metadata WHERE key='media_schema_version'").fetchone()[0] == "2"


def test_filesystem_rejects_symlink_and_fifo_media_without_reading(tmp_path):
    c = client(tmp_path,"filesystem")
    item = c.post("/v1/feedback",headers=headers(),json=payload()).json()["feedback"]
    path = f"/v1/feedback/{item['id']}/attachments/{item['attachments'][0]['id']}"
    stored = next((tmp_path / "media").iterdir())
    stored.unlink()
    secret = tmp_path / "private"; secret.write_bytes(PNG); secret.chmod(0o600)
    stored.symlink_to(secret)
    assert c.get(path,headers=headers()).status_code == 503
    stored.unlink()
    os.mkfifo(stored,mode=0o600)
    assert c.get(path,headers=headers()).status_code == 503  # O_NONBLOCK + regular-file check.

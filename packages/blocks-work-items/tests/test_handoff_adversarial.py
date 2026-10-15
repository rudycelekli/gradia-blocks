"""Independent hostile-response and conditional-authority handoff probes."""
import json
import httpx
import pytest
from fastapi.testclient import TestClient
from gradia_work_items import Principal, create_app
from gradia_feedback import BlockSettings, Principal as FeedbackPrincipal, create_app as feedback_app
from gradia_feedback.contracts import Content
from test_handoff import environment, proxy, bridge, headers, SOURCE, TARGET, OPERATOR


def replace_target(target, mutation):
    def handler(request):
        response = target.request(request.method, request.url.path, headers=dict(request.headers), content=request.content)
        content = response.content
        extra = dict(response.headers)
        if request.method == "POST":
            content, extra = mutation(response, extra)
            extra.pop("content-length", None)
        return httpx.Response(response.status_code, headers=extra, stream=httpx.ByteStream(content))
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("field,value", [("revision", 0), ("revision", -1), ("id", "not-an-item-id")])
def test_invalid_ack_record_is_uncertain_and_explicit_retry_recovers(environment, field, value):
    source, target, feedback = environment
    def alter(response, extra):
        data = response.json()
        data["item"][field] = value
        return json.dumps(data).encode(), extra
    with proxy(source) as sc, replace_target(target, alter) as tc:
        result = bridge(sc, tc).run(feedback["id"], 1)
    assert result["receipt"]["result"] == "uncertain", result
    assert result["item"] is None
    with proxy(source) as sc, proxy(target) as tc:
        recovered = bridge(sc, tc).run(feedback["id"], 1)
    assert recovered["receipt"]["result"] == "recovered"
    assert len(target.get("/v1/work-items", headers=headers(OPERATOR)).json()["items"]) == 1


@pytest.mark.parametrize("mode", ["invalid-replayed", "noninitial-revision", "unexpected-assignee"])
def test_success_ack_requires_complete_initial_proposal_contract(environment, mode):
    source, target, feedback = environment
    def alter(response, extra):
        data = response.json()
        if mode == "invalid-replayed":
            data["replayed"] = "yes"
        elif mode == "noninitial-revision":
            data["item"]["revision"] = 2
        else:
            data["item"]["assignee"] = "unexpected-owner"
        return json.dumps(data).encode(), extra
    with proxy(source) as sc, replace_target(target, alter) as tc:
        result = bridge(sc, tc).run(feedback["id"], 1)
    assert result["receipt"]["result"] == "uncertain" and result["item"] is None, result


def test_compressed_ack_is_not_decoded_and_retried_implicitly(environment):
    source, target, feedback = environment
    def compressed(response, extra):
        extra["content-encoding"] = "gzip"
        return b"deliberately-not-decoded", extra
    with proxy(source) as sc, replace_target(target, compressed) as tc:
        result = bridge(sc, tc).run(feedback["id"], 1)
    assert result["receipt"]["result"] == "uncertain"
    assert result["receipt"]["reason"] == "unsupported_response_encoding"
    assert len(target.get("/v1/work-items", headers=headers(OPERATOR)).json()["items"]) == 1


@pytest.mark.parametrize("kind", ["duplicate", "oversized", "principal"])
def test_invalid_post_response_never_reports_proposed(environment, kind):
    source, target, feedback = environment
    def hostile(response, extra):
        if kind == "duplicate":
            return b'{"status":"acknowledged","status":"acknowledged"}', extra
        if kind == "oversized":
            return b"x" * 65537, extra
        extra["x-gradia-principal-sha256"] = "0" * 64
        return response.content, extra
    with proxy(source) as sc, replace_target(target, hostile) as tc:
        result = bridge(sc, tc).run(feedback["id"], 1)
    assert result["receipt"]["result"] == "uncertain" and result["item"] is None
    assert feedback["message"] not in json.dumps(result["receipt"])


def test_target_actor_rebinding_after_scope_check_is_refused_before_write(environment, tmp_path):
    source, target, feedback = environment
    rebound = TestClient(create_app(tmp_path / "rebound.sqlite", {
        TARGET: Principal(tenant_id="alpha", actor_id="another-pipeline", role="bridge")
    }))
    def handler(request):
        server = rebound if request.method == "POST" else target
        response = server.request(request.method, request.url.path, headers=dict(request.headers), content=request.content)
        return httpx.Response(response.status_code, headers=response.headers, stream=httpx.ByteStream(response.content))
    with proxy(source) as sc, httpx.Client(transport=httpx.MockTransport(handler)) as tc:
        result = bridge(sc, tc).run(feedback["id"], 1)
    # A principal mismatch makes the response untrusted; conservative uncertainty
    # is valid even though this actual server refused before committing.
    assert result["receipt"]["result"] in ("refused", "uncertain")
    assert rebound.get("/v1/work-items", headers=headers(TARGET)).json()["items"] == []


def test_source_read_failure_cannot_publish_or_leak_content(environment):
    source, target, feedback = environment
    calls = []
    def handler(request):
        calls.append(request.url.path)
        if not request.url.path.endswith("/session"):
            raise httpx.ConnectError("private failure " + feedback["message"])
        response = source.request(request.method, request.url.path, headers=dict(request.headers))
        return httpx.Response(response.status_code, headers=response.headers, stream=httpx.ByteStream(response.content))
    with httpx.Client(transport=httpx.MockTransport(handler)) as sc, proxy(target) as tc:
        result = bridge(sc, tc).run(feedback["id"], 1)
    assert result["receipt"]["result"] == "refused"
    assert feedback["message"] not in json.dumps(result["receipt"])
    assert target.get("/v1/work-items", headers=headers(OPERATOR)).json()["items"] == []


def test_source_configuration_changed_after_session_refuses_proposal(environment, tmp_path):
    source, target, feedback = environment
    replacement = TestClient(feedback_app(tmp_path / "feedback.sqlite", {
        SOURCE: FeedbackPrincipal(tenant_id="alpha", actor_id="reader", role="operator")
    }, BlockSettings(content=Content(title="Changed intake profile"))))
    def handler(request):
        server = source if request.url.path.endswith("/session") else replacement
        response = server.request(request.method, request.url.path, headers=dict(request.headers), content=request.content)
        return httpx.Response(response.status_code, headers=response.headers, stream=httpx.ByteStream(response.content))
    with httpx.Client(transport=httpx.MockTransport(handler)) as sc, proxy(target) as tc:
        result = bridge(sc, tc).run(feedback["id"], 1)
    assert result["receipt"]["result"] == "refused", result
    assert target.get("/v1/work-items", headers=headers(OPERATOR)).json()["items"] == []

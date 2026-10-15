"""Independent relay controls using real source/target APIs behind HTTP transports."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import os
import sqlite3
import threading

import httpx
import pytest
from fastapi.testclient import TestClient
from gradia_event_relay import Relay
from gradia_event_relay.contracts import canonical, sha
from gradia_work_items import Principal as WorkPrincipal, create_app as work_app
from gradia_outbox import Principal as OutPrincipal
from gradia_outbox.api import create_app as out_app

SOURCE = 'private-source-token-' + 's' * 32
TARGET = 'private-target-token-' + 't' * 32
SIGNING = 'private-signing-token-' + 'k' * 32
SECRET = 'sensitive-title-73193@example.invalid'

class Harness:
    def __init__(self, path):
        self.path = path
        self.source = TestClient(work_app(path / 'source.sqlite', {SOURCE: WorkPrincipal(tenant_id='tenant', actor_id='source-op', role='operator')}))
        self.target = TestClient(out_app(path / 'target.sqlite', {TARGET: OutPrincipal('tenant', 'relay-submit')}, {'primary': {'tenant_id': 'tenant', 'url': 'https://receiver.invalid/hooks', 'signing_key': SIGNING, 'receiver_id': 'receiver'}}))
        self.source_hook = None
        self.target_hook = None
        self.requests = []
        self.posts = []
        self.options = dict(source_origin='https://source.invalid', target_origin='https://target.invalid', source_token=SOURCE, target_token=TARGET, tenant_id='tenant', profile_id='fixture', destination_id='primary', source_client=httpx.Client(transport=httpx.MockTransport(lambda r: self.handle('source', r))), target_client=httpx.Client(transport=httpx.MockTransport(lambda r: self.handle('target', r))))
        self.relay = Relay(path / 'relay.sqlite', **self.options)
    def seed(self, key='one'):
        r = self.source.post('/v1/work-items', headers={'Authorization': 'Bearer ' + SOURCE, 'Idempotency-Key': key}, json={'title': SECRET, 'description': SECRET})
        assert r.status_code == 201
        return r.json()['item']
    def handle(self, side, request):
        self.requests.append((side, request))
        if request.method == 'POST': self.posts.append(request)
        service = self.source if side == 'source' else self.target
        r = service.request(request.method, request.url.raw_path.decode(), content=request.content, headers=dict(request.headers))
        result = httpx.Response(r.status_code, content=r.content, headers=dict(r.headers), request=request)
        hook = self.source_hook if side == 'source' else self.target_hook
        return hook(request, result) if hook else result
    def reopen(self, **changes):
        self.relay = Relay(self.path / 'relay.sqlite', **{**self.options, **changes})
        return self.relay
    def count(self):
        return len(self.target.app.state.engine.list(OutPrincipal('tenant', 'operator', 'operator')))

def response(r, body=None, **changes):
    headers = dict(r.headers)
    headers.pop('content-length', None)
    headers.update(changes)
    return httpx.Response(r.status_code, content=canonical(body) if body is not None else r.content, headers=headers, request=r.request)

def change_page(h, fn):
    def hook(request, r):
        if request.url.path.endswith('/changes'):
            body = r.json(); fn(body); return response(r, body)
        return r
    h.source_hook = hook


def test_real_services_cursor_only_after_ack_and_idle_no_resend(tmp_path):
    h = Harness(tmp_path); h.seed()
    result = h.relay.run()
    assert result['receipt']['result'] == 'enqueued' and h.count() == 1
    assert result['checkpoint']['cursor'] == 1 and len(h.posts) == 1
    assert h.reopen().run()['receipt']['result'] == 'idle'
    assert len(h.posts) == 1
    assert all(r.url.path == '/v1/outbox' for r in h.posts)
    metadata = json.dumps({'status': h.relay.status(), 'events': h.relay.events(), 'attempts': list(h.relay.attempts), 'result': result})
    assert all(secret not in metadata for secret in (SOURCE, TARGET, SIGNING, SECRET))


def test_lost_enqueue_response_replays_same_key_preserves_later_target_state(tmp_path):
    h = Harness(tmp_path); h.seed()
    def lose(request, r):
        if request.method == 'POST': raise httpx.ReadError(SECRET, request=request)
        return r
    h.target_hook = lose
    first = h.relay.run()
    assert first['receipt']['result'] == 'uncertain' and first['checkpoint']['cursor'] == 0
    item = h.target.app.state.engine.list(OutPrincipal('tenant', 'operator', 'operator'))[0]
    # A later operator-owned state must survive replay of the original enqueue ACK.
    with sqlite3.connect(tmp_path / 'target.sqlite') as db:
        db.execute("UPDATE outbox_items SET revision=3,status='uncertain' WHERE id=?", (item['id'],))
    h.target_hook = None
    second = h.reopen().run()
    assert second['receipt']['result'] == 'enqueued' and second['items'][0]['replayed'] is True
    assert second['items'][0]['outbox_item_id'] == item['id'] and h.count() == 1
    current = h.target.app.state.engine.get(OutPrincipal('tenant', 'operator', 'operator'), item['id'])
    assert current['revision'] == 3 and current['status'] == 'uncertain'
    assert h.posts[0].headers['Idempotency-Key'] == h.posts[1].headers['Idempotency-Key']


def test_checkpoint_failure_after_target_ack_recovers_without_duplicate(tmp_path):
    h = Harness(tmp_path); h.seed()
    original = h.relay.store.save
    def save(state, event):
        if state['cursor']: raise sqlite3.OperationalError(SECRET)
        return original(state, event)
    h.relay.store.save = save
    first = h.relay.run()
    assert first['receipt']['result'] == 'uncertain' and first['checkpoint']['cursor'] == 0 and h.count() == 1
    second = h.reopen().run()
    assert second['receipt']['result'] == 'enqueued' and second['items'][0]['replayed'] is True and h.count() == 1


@pytest.mark.parametrize('tamper', [
    lambda p: p.update(tenant_id='foreign'),
    lambda p: p.update(journal_id='12345678-1234-1234-1234-123456789abc'),
    lambda p: p.update(next_after=0),
    lambda p: p.update(head_sequence=0),
    lambda p: p.update(unknown=True),
    lambda p: p['items'].reverse(),
    lambda p: p['items'].append(copy.deepcopy(p['items'][0])),
    lambda p: p['items'][1]['event'].update(tenant_id='foreign'),
    lambda p: p['items'][1]['event'].update(title=SECRET),
    lambda p: p['items'][1]['event'].update(operation='send.email'),
    lambda p: p['items'][1]['event'].update(edition='0.1.0'),
    lambda p: p['items'][1]['event'].update(cost={'status': 'known', 'amount': 0, 'currency': 'USD'}),
    lambda p: p['items'][1]['event'].update(event_id=p['items'][0]['event']['event_id']),
    lambda p: p['items'].clear(),
    lambda p: p['items'].pop(),
])
def test_whole_malformed_page_refused_before_any_enqueue(tmp_path, tamper):
    h = Harness(tmp_path); h.seed(); h.seed('two'); change_page(h, tamper)
    result = h.relay.run()
    assert result['receipt']['result'] == 'refused', result
    assert h.count() == 0 and h.posts == [] and result['checkpoint'] is None


@pytest.mark.parametrize('side,field,value', [
    ('source', 'role', 'bridge'), ('source', 'version', '0.1.0'), ('source', 'tenant_id', 'foreign'),
    ('target', 'role', 'operator'), ('target', 'version', '0.2.0'), ('target', 'tenant_id', 'foreign'),
])
def test_session_never_grants_broader_or_cross_tenant_authority(tmp_path, side, field, value):
    h = Harness(tmp_path); h.seed()
    def hook(request, r):
        if request.url.path.endswith('/session'):
            body = r.json(); body[field] = value
            return response(r, body)
        return r
    setattr(h, side + '_hook', hook)
    result = h.relay.run()
    assert result['receipt']['result'] == 'refused' and h.posts == []


@pytest.mark.parametrize('side', ['source', 'target'])
@pytest.mark.parametrize('field', ['configuration_digest', 'actor_id'])
def test_bound_session_drift_is_refused_on_next_run(tmp_path, side, field):
    h = Harness(tmp_path); h.seed(); assert h.relay.run()['receipt']['result'] == 'enqueued'
    h.seed('two')
    def hook(request, r):
        if request.url.path.endswith('/session'):
            body = r.json(); body[field] = '0' * 64 if field == 'configuration_digest' else 'another-actor'
            headers = {'x-gradia-principal-sha256': sha({key: body[key] for key in ('tenant_id', 'actor_id', 'role')})}
            return response(r, body, **headers)
        return r
    setattr(h, side + '_hook', hook)
    result = h.relay.run()
    assert result['receipt']['result'] == 'refused' and len(h.posts) == 1 and result['checkpoint']['cursor'] == 1


@pytest.mark.parametrize('field,value', [('source_token', 'replacement-source-' + 'x' * 32), ('target_token', 'replacement-target-' + 'x' * 32), ('destination_id', 'another'), ('profile_id', 'another'), ('tenant_id', 'another'), ('target_origin', 'https://another.invalid')])
def test_checkpoint_config_cannot_be_silently_rebound(tmp_path, field, value):
    h = Harness(tmp_path)
    with pytest.raises(ValueError): h.reopen(**{field: value})


@pytest.mark.parametrize('body,headers', [(b'not-json', {}), (b'{"items":[],"items":[]}', {}), (b'x' * 262145, {}), (b'[]', {'content-encoding': 'gzip'}), (b'[]', {'content-type': 'text/html'})])
def test_bad_upstream_bodies_are_bounded_refusals(tmp_path, body, headers):
    h = Harness(tmp_path); h.seed()
    def hook(request, r):
        if request.url.path.endswith('/changes'):
            return httpx.Response(200, content=body, headers={**dict(r.headers), **headers}, request=request)
        return r
    h.source_hook = hook
    result = h.relay.run()
    assert result['receipt']['result'] == 'refused' and h.posts == []


@pytest.mark.parametrize('sql', ['CREATE TABLE foreign_table(x TEXT)', 'CREATE VIEW foreign_view AS SELECT * FROM relay_events', "CREATE TRIGGER foreign_trigger AFTER INSERT ON relay_events BEGIN DELETE FROM relay_events; END", "INSERT INTO relay_metadata VALUES('foreign','value')"])
def test_checkpoint_foreign_schema_or_metadata_refused(tmp_path, sql):
    h = Harness(tmp_path)
    with sqlite3.connect(tmp_path / 'relay.sqlite') as db: db.execute(sql)
    with pytest.raises(ValueError): h.reopen()


def test_concurrent_runs_cannot_race_checkpoint_or_enqueue(tmp_path):
    h = Harness(tmp_path); h.seed(); entered = threading.Event(); release = threading.Event()
    def wait(request, r):
        if request.method == 'POST':
            entered.set(); assert release.wait(5)
        return r
    h.target_hook = wait
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(h.relay.run)
        assert entered.wait(5)
        second = h.relay.run()
        assert second['receipt']['result'] == 'refused' and second['receipt']['reason'] == 'run_in_progress'
        release.set()
        assert pending.result()['receipt']['result'] == 'enqueued'
    assert h.count() == 1 and len(h.posts) == 1


@pytest.mark.parametrize('url', ['http://source.invalid', 'http://localhost', 'https://user:pass@source.invalid', 'https://source.invalid/path', 'https://source.invalid?token=x', 'https://source.invalid#x', 'https://source.invalid/\r\nheader'])
def test_unsafe_source_origins_are_never_constructed(tmp_path, url):
    h = Harness(tmp_path)
    with pytest.raises(ValueError): h.reopen(source_origin=url)


def test_injected_client_headers_cookies_auth_do_not_leak(tmp_path):
    h = Harness(tmp_path); h.seed()
    h.options['source_client'].headers.update({'X-Private-Client': SECRET})
    h.options['source_client'].cookies.set('private', SECRET)
    h.options['source_client'].auth = ('private', SECRET)
    assert h.relay.run()['receipt']['result'] == 'enqueued'
    for side, request in h.requests:
        assert 'X-Private-Client' not in request.headers and 'Cookie' not in request.headers
        assert request.headers['Authorization'] == 'Bearer ' + (SOURCE if side == 'source' else TARGET)
        if not request.url.path.endswith('/session'):
            assert all(name in request.headers for name in ('If-Gradia-Principal', 'If-Gradia-Configuration', 'If-Gradia-Block-Version'))


def test_checkpoint_anchor_rejects_source_database_rollback(tmp_path):
    h = Harness(tmp_path); h.seed(); h.seed('two')
    assert h.relay.run()['checkpoint']['cursor'] == 2
    with sqlite3.connect(tmp_path / 'source.sqlite') as db: db.execute('DELETE FROM work_item_events WHERE sequence=2')
    result = h.relay.run()
    assert result['receipt']['result'] == 'refused' and result['checkpoint']['cursor'] == 2 and len(h.posts) == 2


@pytest.mark.parametrize('tamper', [
    lambda p: p.update(replayed=1),
    lambda p: p.update(replayed='true'),
    lambda p: p.update(unknown=True),
    lambda p: p['item'].update(revision=2),
    lambda p: p['item'].update(status='delivered'),
    lambda p: p['item'].update(attempt_id='12345678-1234-1234-1234-123456789abc'),
    lambda p: p['item'].update(destination_id='foreign'),
    lambda p: p['item'].update(event_type='work_item.updated'),
    lambda p: p['item'].update(payload_sha256='0' * 64),
    lambda p: p['item'].update(updated_at='2020-01-01T00:00:00+00:00'),
])
def test_target_commit_with_invalid_ack_remains_uncertain_and_cursor_unadvanced(tmp_path, tamper):
    h = Harness(tmp_path); h.seed()
    def hook(request, r):
        if request.method == 'POST':
            body = r.json(); tamper(body); return response(r, body)
        return r
    h.target_hook = hook
    result = h.relay.run()
    assert result['receipt']['result'] == 'uncertain' and result['checkpoint']['cursor'] == 0 and h.count() == 1
    h.target_hook = None
    recovered = h.reopen().run()
    assert recovered['receipt']['result'] == 'enqueued' and recovered['items'][0]['replayed'] is True and h.count() == 1


@pytest.mark.parametrize('side,session', [('source', True), ('target', True), ('source', False), ('target', False)])
def test_response_principal_identity_is_required_at_each_boundary(tmp_path, side, session):
    h = Harness(tmp_path); h.seed()
    def hook(request, r):
        if request.url.path.endswith('/session') == session:
            return response(r, **{'x-gradia-principal-sha256': '0' * 64})
        return r
    setattr(h, side + '_hook', hook)
    result = h.relay.run()
    expected = 'uncertain' if side == 'target' and not session else 'refused'
    assert result['receipt']['result'] == expected
    assert result['checkpoint'] is None or result['checkpoint']['cursor'] == 0


@pytest.mark.parametrize('side', ['source', 'target'])
def test_redirect_never_forwards_bearer_to_unapproved_host(tmp_path, side):
    h = Harness(tmp_path); h.seed()
    def hook(request, r):
        return httpx.Response(307, content=b'{}', headers={'content-type': 'application/json', 'location': 'https://attacker.invalid/capture'}, request=request)
    setattr(h, side + '_hook', hook)
    result = h.relay.run()
    assert result['receipt']['result'] == 'refused'
    assert all(request.url.host in ('source.invalid', 'target.invalid') for _, request in h.requests)
    assert h.posts == []


@pytest.mark.parametrize('side', ['source', 'target'])
def test_configuration_changes_between_session_and_effect_are_conditionally_refused(tmp_path, side):
    h = Harness(tmp_path); h.seed()
    def hook(request, r):
        # Lie during session discovery; the subsequent actual API conditional header refuses.
        if request.url.path.endswith('/session'):
            body = r.json(); body['configuration_digest'] = '0' * 64
            return response(r, body)
        return r
    setattr(h, side + '_hook', hook)
    result = h.relay.run()
    assert result['receipt']['result'] == ('uncertain' if side == 'target' else 'refused')
    assert h.count() == 0
    assert result['checkpoint'] is None or result['checkpoint']['cursor'] == 0


def test_raw_source_anchor_hash_preserves_integer_duration_representation(tmp_path):
    h = Harness(tmp_path); h.seed()
    # Synthetic source stores a JSON integer duration; validation must not change anchor bytes.
    with sqlite3.connect(tmp_path / 'source.sqlite') as db:
        event = json.loads(db.execute('SELECT payload FROM work_item_events WHERE sequence=1').fetchone()[0])
        event['duration_ms'] = 0
        db.execute('UPDATE work_item_events SET payload=? WHERE sequence=1', (json.dumps(event),))
    result = h.relay.run()
    assert result['receipt']['result'] == 'enqueued'
    assert result['checkpoint']['anchor']['event_sha256'] == sha(event)
    assert h.reopen().run()['receipt']['result'] == 'idle'


@pytest.mark.parametrize('leaf', ['relay.sqlite', 'relay.sqlite.run-lock'])
@pytest.mark.parametrize('mode', ['symlink', 'hardlink', 'public'])
def test_private_checkpoint_and_run_lock_cannot_be_swapped_for_shared_file(tmp_path, leaf, mode):
    h = Harness(tmp_path)
    path = tmp_path / leaf
    path.unlink()
    target = tmp_path / ('foreign-' + leaf)
    target.touch(mode=0o600)
    if mode == 'symlink': path.symlink_to(target)
    elif mode == 'hardlink': os.link(target, path)
    else: path.touch(mode=0o644); path.chmod(0o644)
    with pytest.raises((ValueError, OSError)): h.reopen()


def test_relay_facade_authority_refusals_cannot_trigger_upstream_calls(tmp_path):
    from gradia_event_relay import Principal, create_app
    h = Harness(tmp_path); h.seed()
    credential = 'host-api-operator-' + 'z' * 32
    c = TestClient(create_app(h.relay, {credential: Principal('tenant', 'host-operator')}), raise_server_exceptions=False)
    for headers, code in [({}, 401), ({'Authorization': 'Bearer unknown'}, 401), ({'Authorization': 'Bearer ' + credential, 'If-Gradia-Configuration': '0' * 64}, 409), ({'Authorization': 'Bearer ' + credential, 'If-Gradia-Block-Version': '9.0.0'}, 409), ({'Authorization': 'Bearer ' + credential, 'If-Gradia-Principal': '0' * 64}, 409)]:
        assert c.post('/v1/event-relay/run', headers=headers, json={}).status_code == code
    assert c.post('/v1/event-relay/run', headers=[('Authorization', 'Bearer ' + credential), ('Authorization', 'Bearer unknown')], json={}).status_code == 400
    assert h.requests == []
    authorized = {'Authorization': 'Bearer ' + credential}
    for body in [b'{"limit":1,"limit":2}', b'{"limit":NaN}', b'[' * 100 + b'0' + b']' * 100, b'x' * 4097]:
        assert c.post('/v1/event-relay/run', headers=authorized, content=body).status_code in (413, 422)
    for payload in ({'limit': True}, {'limit': 0}, {'limit': 26}, {'destination_id': 'foreign'}, {'delivery_authorized': True}):
        assert c.post('/v1/event-relay/run', headers=authorized, json=payload).status_code == 422
    assert h.requests == []
    assert c.post('/v1/event-relay/run', headers=authorized, json={}).json()['receipt']['result'] == 'enqueued'
    assert all(request.url.path == '/v1/outbox' for request in h.posts)
    diagnostic = c.get('/v1/event-relay/diagnostics/attempts', headers=authorized)
    assert all(secret not in diagnostic.text for secret in (SECRET, SOURCE, TARGET, credential))


@pytest.mark.parametrize('values', [{'tenant_id': 'foreign', 'actor_id': 'host-operator'}, {'tenant_id': 'tenant', 'actor_id': 'host-operator', 'role': 'submitter'}])
def test_relay_host_facade_cannot_grant_cross_tenant_or_submitter_run_authority(tmp_path, values):
    from gradia_event_relay import create_app
    h = Harness(tmp_path)
    with pytest.raises(ValueError): create_app(h.relay, {'host-api-token-' + 'x' * 32: values})

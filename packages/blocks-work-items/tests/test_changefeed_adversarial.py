"""Independent adversarial controls for durable, tenant-scoped mutation feed."""
import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from gradia_work_items import Principal, create_app

OP = 'feed-operator-' + 'a' * 24
OTHER = 'feed-other-' + 'b' * 24
SUB = 'feed-submitter-' + 'c' * 24
BRIDGE = 'feed-bridge-' + 'd' * 24
SECRET = 'confidential-customer-case-8271@example.invalid'

def client(tmp_path):
    return TestClient(create_app(tmp_path / 'feed.sqlite', {
        OP: Principal(tenant_id='a', actor_id='operator', role='operator'),
        OTHER: Principal(tenant_id='b', actor_id='operator', role='operator'),
        SUB: Principal(tenant_id='a', actor_id='submitter'),
        BRIDGE: Principal(tenant_id='a', actor_id='bridge', role='bridge'),
    }), raise_server_exceptions=False)

def headers(token=OP): return {'Authorization': 'Bearer ' + token}

def create(c, token=OP, key='fixture'):
    r = c.post('/v1/work-items', headers={**headers(token), 'Idempotency-Key': key}, json={'title': SECRET, 'description': SECRET})
    assert r.status_code in (200, 201), r.text
    return r.json()['item']

def feed(c, token=OP, **params):
    return c.get('/v1/work-items/changes', headers=headers(token), params=params)

def cursor(page, index=-1):
    item = page['items'][index]
    digest = hashlib.sha256(json.dumps(item['event'], sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()
    return {'after_sequence': item['sequence'], 'expected_journal_id': page['journal_id'], 'after_event_id': item['event']['event_id'], 'after_event_sha256': digest}


def test_global_sequence_gaps_do_not_leak_other_tenant_or_skip_own_events(tmp_path):
    c = client(tmp_path)
    own = [create(c, key='a0')['id']]
    foreign = create(c, OTHER)['id']
    own.append(create(c, SUB, 'a1')['id'])
    first = feed(c, limit=1).json()
    second = feed(c, limit=1, **cursor(first)).json()
    assert [v['event']['item_id'] for v in first['items'] + second['items']] == own
    assert second['items'][0]['sequence'] > first['items'][0]['sequence'] + 1
    assert foreign not in json.dumps(first) + json.dumps(second)
    assert feed(c, OTHER, **cursor(first)).status_code == 409
    tail = feed(c, **cursor(second)).json()
    assert tail['items'] == [] and tail['next_after'] == second['next_after']


@pytest.mark.parametrize('token', [SUB, BRIDGE, 'unknown'])
def test_feed_never_inherits_submitter_or_bridge_authority(tmp_path, token):
    c = client(tmp_path)
    create(c)
    r = feed(c, token)
    assert r.status_code == (401 if token == 'unknown' else 403)
    assert SECRET not in r.text
    assert c.get('/v1/work-items/changes').status_code == 401


@pytest.mark.parametrize('query', ['after_sequence=1', 'after_sequence=0&after_event_id=12345678-1234-1234-1234-123456789abc', 'limit=0', 'limit=101', 'limit=1&limit=2', 'after_sequence=0&after_sequence=1', 'unknown=x', 'after_sequence=9007199254740992'])
def test_bad_and_ambiguous_cursor_never_reads_feed(tmp_path, query):
    c = client(tmp_path)
    create(c)
    r = c.get('/v1/work-items/changes?' + query, headers=headers())
    assert r.status_code in (400, 422), r.text
    assert 'items' not in r.json()


@pytest.mark.parametrize('field,value', [('expected_journal_id', '12345678-1234-1234-1234-123456789abc'), ('after_event_id', '12345678-1234-1234-1234-123456789abc'), ('after_event_sha256', '0' * 64), ('after_sequence', 99)])
def test_validly_shaped_but_unbound_anchor_is_refused(tmp_path, field, value):
    c = client(tmp_path)
    create(c)
    params = cursor(feed(c).json())
    params[field] = value
    assert feed(c, **params).status_code == 409


def test_restore_or_rewrite_of_checkpoint_anchor_is_refused(tmp_path):
    c = client(tmp_path)
    create(c)
    first = feed(c).json()
    params = cursor(first)
    with sqlite3.connect(tmp_path / 'feed.sqlite') as db:
        payload = first['items'][0]['event'].copy()
        payload['result'] = 'rewritten'
        db.execute('UPDATE work_item_events SET payload=? WHERE sequence=?', (json.dumps(payload), params['after_sequence']))
    assert feed(c, **params).status_code == 409
    with sqlite3.connect(tmp_path / 'feed.sqlite') as db:
        db.execute('DELETE FROM work_item_events WHERE sequence=?', (params['after_sequence'],))
    assert feed(c, **params).status_code == 409


def test_restart_retains_journal_and_metadata_has_no_item_content(tmp_path):
    c = client(tmp_path)
    item = create(c)
    assert c.patch('/v1/work-items/' + item['id'], headers=headers(), json={'expected_revision': 1, 'assignee': 'private-assignee'}).status_code == 200
    page = feed(c).json()
    reopened = client(tmp_path)
    assert reopened.get('/v1/work-items/session', headers=headers()).json()['journal_id'] == page['journal_id']
    assert feed(reopened, **cursor(page)).json()['items'] == []
    text = json.dumps(page)
    assert SECRET not in text and 'private-assignee' not in text and OP not in text
    assert [v['event']['revision'] for v in page['items']] == [1, 2]
    assert all(v['event']['cost']['status'] == 'unknown' for v in page['items'])


def test_failed_audit_rolls_back_mutation_and_cursor_head(tmp_path):
    c = client(tmp_path)
    item = create(c)
    before = feed(c).json()
    with sqlite3.connect(tmp_path / 'feed.sqlite') as db:
        db.execute("CREATE TRIGGER refuse_event BEFORE INSERT ON work_item_events BEGIN SELECT RAISE(ABORT,'fixture'); END")
    r = c.patch('/v1/work-items/' + item['id'], headers=headers(), json={'expected_revision': 1, 'status': 'open'})
    assert r.status_code == 503
    after = feed(c).json()
    assert after == before
    assert c.get('/v1/work-items/' + item['id'], headers=headers()).json()['revision'] == 1


def test_concurrent_mutation_cas_yields_one_event_and_no_ghost_intent(tmp_path):
    c = client(tmp_path)
    item = create(c)
    def patch(name):
        return c.patch('/v1/work-items/' + item['id'], headers=headers(), json={'expected_revision': 1, 'assignee': name})
    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = [r.status_code for r in pool.map(patch, ['one', 'two', 'three', 'four'])]
    assert sorted(statuses) == [200, 409, 409, 409]
    page = feed(c).json()
    assert len(page['items']) == 2 and [v['event']['revision'] for v in page['items']] == [1, 2]


@pytest.mark.parametrize('sql', ["CREATE TRIGGER foreign_trigger AFTER UPDATE ON work_items BEGIN UPDATE work_items SET status='done'; END", 'CREATE VIEW foreign_view AS SELECT * FROM work_item_events'])
def test_restart_does_not_adopt_foreign_executable_schema(tmp_path, sql):
    client(tmp_path)
    with sqlite3.connect(tmp_path / 'feed.sqlite') as db:
        db.execute(sql)
    with pytest.raises(ValueError):
        client(tmp_path)


def test_duplicate_authority_headers_are_rejected_before_feed_access(tmp_path):
    c = client(tmp_path)
    create(c)
    r = c.get('/v1/work-items/changes', headers=[('Authorization', 'Bearer ' + OP), ('Authorization', 'Bearer ' + OTHER)])
    assert r.status_code == 400

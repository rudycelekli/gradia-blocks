import hashlib
import json
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi.testclient import TestClient
from gradia_work_items import create_app,Principal

OP='alpha-operator-token-long-enough';BETA='beta-operator-token-long-enough'
KEYS={OP:Principal(tenant_id='alpha',role='operator'),BETA:Principal(tenant_id='beta',role='operator')}
def headers(token=OP):return {'Authorization':'Bearer '+token}
def seed(client,key='one',token=OP):
    result=client.post('/v1/work-items',headers={**headers(token),'Idempotency-Key':key},json={'title':'PRIVATE-title','description':'PRIVATE-description'})
    assert result.status_code in (200,201)
    return result.json()['item']
def anchor(page):
    row=page['items'][-1]
    return {'after_sequence':row['sequence'],'expected_journal_id':page['journal_id'],'after_event_id':row['event']['event_id'],'after_event_sha256':hashlib.sha256(json.dumps(row['event'],sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()).hexdigest()}


def test_feed_pagination_original_metadata_and_stable_restart(tmp_path):
    path=tmp_path/'items.sqlite';app=create_app(path,KEYS);client=TestClient(app)
    a=seed(client);seed(client,token=BETA);seed(client,'two')
    assert client.patch('/v1/work-items/'+a['id'],headers=headers(),json={'expected_revision':1,'status':'open'}).status_code==200
    first=client.get('/v1/work-items/changes',params={'limit':1},headers=headers()).json()
    assert first['schema']=='gradia.work-items.changefeed.v1' and first['tenant_id']=='alpha'
    assert first['head_sequence']==4 and first['next_after']==1
    assert first['items'][0]['event']['item_id']==a['id']
    assert 'PRIVATE' not in json.dumps(first)
    event=client.get('/v1/work-items/'+a['id']+'/events',headers=headers()).json()['items'][0]
    assert first['items'][0]=={'sequence':event.pop('sequence'),'event':event}
    restarted=TestClient(create_app(path,KEYS))
    assert restarted.get('/v1/work-items/session',headers=headers()).json()['journal_id']==first['journal_id']
    second=restarted.get('/v1/work-items/changes',params=anchor(first),headers=headers()).json()
    assert [r['sequence'] for r in second['items']]==[3,4]
    empty=restarted.get('/v1/work-items/changes',params=anchor(second),headers=headers()).json()
    assert empty['items']==[] and empty['next_after']==4


def test_metadata_bootstrap_preserves_legacy_events_and_item(tmp_path):
    path=tmp_path/'items.sqlite';app=create_app(path,KEYS);client=TestClient(app);item=seed(client)
    with app.state.store.connection() as conn:
        conn.execute("DELETE FROM work_item_metadata WHERE key='journal_id'")
        row=conn.execute('SELECT payload FROM work_item_events').fetchone()
        original=json.loads(row[0]);original['edition']='0.1.0';original['block_version']='0.1.0'
        conn.execute('UPDATE work_item_events SET payload=?',(json.dumps(original),))
    restarted=TestClient(create_app(path,KEYS));page=restarted.get('/v1/work-items/changes',headers=headers()).json()
    assert page['items'][0]['event']==original
    assert restarted.get('/v1/work-items/'+item['id'],headers=headers()).json()['revision']==1
    assert str(uuid.UUID(page['journal_id']))==page['journal_id']


def test_failed_event_insert_rolls_back_mutation_and_idempotency(tmp_path):
    app=create_app(tmp_path/'items.sqlite',KEYS);client=TestClient(app);item=seed(client)
    with app.state.store.connection() as conn:
        conn.execute("CREATE TRIGGER fail_event BEFORE INSERT ON work_item_events BEGIN SELECT RAISE(ABORT,'injected'); END")
    failed=client.patch('/v1/work-items/'+item['id'],headers=headers(),json={'expected_revision':1,'status':'open'})
    assert failed.status_code==503
    failed=client.post('/v1/work-items',headers={**headers(),'Idempotency-Key':'failed'},json={'title':'New'})
    assert failed.status_code==503
    assert client.get('/v1/work-items/'+item['id'],headers=headers()).json()['revision']==1
    page=client.get('/v1/work-items/changes',headers=headers()).json();assert len(page['items'])==1
    with app.state.store.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM work_item_keys').fetchone()[0]==1
        assert conn.execute('SELECT COUNT(*) FROM work_items').fetchone()[0]==1


def test_concurrent_idempotency_has_one_journal_event(tmp_path):
    client=TestClient(create_app(tmp_path/'items.sqlite',KEYS))
    with ThreadPoolExecutor(max_workers=5) as pool:items=list(pool.map(lambda _:seed(client),range(10)))
    assert len({i['id'] for i in items})==1
    assert len(client.get('/v1/work-items/changes',headers=headers()).json()['items'])==1


def test_cursor_rejects_rewound_branch_even_with_same_journal(tmp_path):
    app=create_app(tmp_path/'items.sqlite',KEYS);client=TestClient(app);seed(client);seed(client,'two')
    page=client.get('/v1/work-items/changes',headers=headers()).json();cursor=anchor(page)
    with app.state.store.connection() as conn:
        conn.execute('DELETE FROM work_item_events WHERE sequence=2')
        conn.execute("UPDATE sqlite_sequence SET seq=1 WHERE name='work_item_events'")
    seed(client,'three')
    refused=client.get('/v1/work-items/changes',params=cursor,headers=headers())
    assert refused.status_code==409 and refused.json()['error']['code']=='cursor_invalid'


@pytest.mark.parametrize('value',['', 'not-uuid', 'A'*36])
def test_invalid_stored_journal_refuses_startup(tmp_path,value):
    path=tmp_path/'items.sqlite';app=create_app(path,KEYS)
    with app.state.store.connection() as conn:conn.execute("UPDATE work_item_metadata SET value=? WHERE key='journal_id'",(value,))
    with pytest.raises(ValueError):create_app(path,KEYS)


@pytest.mark.parametrize('key',['schema','configuration_digest'])
def test_missing_required_legacy_metadata_is_not_repaired(tmp_path,key):
    path=tmp_path/'items.sqlite';app=create_app(path,KEYS)
    with app.state.store.connection() as conn:
        conn.execute("DELETE FROM work_item_metadata WHERE key='journal_id'")
        conn.execute('DELETE FROM work_item_metadata WHERE key=?',(key,))
    with pytest.raises(ValueError):create_app(path,KEYS)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT value FROM work_item_metadata WHERE key='journal_id'").fetchone() is None
        assert conn.execute('SELECT value FROM work_item_metadata WHERE key=?',(key,)).fetchone() is None


@pytest.mark.parametrize('duration',[0,1,1.0,1.125])
def test_typed_feed_preserves_integer_and_float_anchor_canonical_bytes(tmp_path,duration):
    app=create_app(tmp_path/'items.sqlite',KEYS);client=TestClient(app);seed(client)
    with app.state.store.connection() as conn:
        event=json.loads(conn.execute('SELECT payload FROM work_item_events').fetchone()[0]);event['duration_ms']=duration
        conn.execute('UPDATE work_item_events SET payload=?',(json.dumps(event),))
    page=client.get('/v1/work-items/changes',headers=headers()).json()
    assert type(page['items'][0]['event']['duration_ms']) is type(duration)
    assert page['items'][0]['event']==event
    assert client.get('/v1/work-items/changes',headers=headers(),params=anchor(page)).status_code==200

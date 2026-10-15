"""Real reference-service integration in process; TCP replay is separate."""
import hashlib
import json
from pathlib import Path
import os
import sqlite3
import pytest
from fastapi.testclient import TestClient
from gradia_event_relay import Relay,Principal,create_app
from gradia_event_relay.contracts import origin
from gradia_work_items.api import create_app as work_app
from gradia_work_items.contracts import Principal as WorkPrincipal
from gradia_outbox.api import create_app as outbox_app
from gradia_outbox.contracts import Principal as OutboxPrincipal,Destination

SOURCE='source-operator-credential-000001'
TARGET='target-submitter-credential-00001'
OPERATOR='target-operator-credential-000001'
RELAY='relay-operator-credential-0000001'
@pytest.fixture
def fixture(tmp_path):
    source=TestClient(work_app(tmp_path/'source.db',{SOURCE:WorkPrincipal(tenant_id='tenant',actor_id='source',role='operator')}))
    target=TestClient(outbox_app(tmp_path/'target.db',{TARGET:OutboxPrincipal('tenant','relay'),OPERATOR:OutboxPrincipal('tenant','operator','operator')},{'notify':Destination('tenant','http://127.0.0.1:9999/webhook','s'*32,'receiver')}))
    kwargs=dict(source_origin='http://127.0.0.1:9001',target_origin='http://127.0.0.1:9002',source_token=SOURCE,target_token=TARGET,tenant_id='tenant',profile_id='main',destination_id='notify',source_client=source,target_client=target)
    relay=Relay(tmp_path/'relay.db',**kwargs)
    yield source,target,relay,kwargs,tmp_path
    relay.close();source.close();target.close()
def seed(source):
    response=source.post('/v1/work-items',headers={'Authorization':'Bearer '+SOURCE,'Idempotency-Key':'first'},json={'title':'PRIVATE TITLE','description':'PRIVATE DESCRIPTION'})
    assert response.status_code==201,response.text
    return response.json()['item']['id']
def items(target):return target.get('/v1/outbox',headers={'Authorization':'Bearer '+OPERATOR}).json()['items']
def test_pipeline_restart_preserves_checkpoint(fixture):
    source,target,relay,kwargs,path=fixture
    identity=seed(source)
    first=relay.run(1)
    assert first['receipt']['result']=='enqueued',first
    assert first['checkpoint']['cursor']==1
    assert len(items(target))==1
    assert items(target)[0]['status']=='queued'
    assert 'PRIVATE' not in json.dumps(first)
    assert relay.run()['receipt']['result']=='idle'
    source.patch('/v1/work-items/'+identity,headers={'Authorization':'Bearer '+SOURCE},json={'expected_revision':1,'status':'open'})
    reopened=Relay(path/'relay.db',**kwargs)
    result=reopened.run()
    assert result['receipt']['result']=='enqueued',result
    assert result['checkpoint']['cursor']==2
    assert len(items(target))==2
    assert all(item['status']=='queued' for item in items(target))
    assert len(reopened.events())==6
    assert 'PRIVATE' not in json.dumps(reopened.events())
    reopened.close()
def test_empty_binds_without_advancing(fixture):
    _,_,relay,_,_=fixture
    result=relay.run()
    assert result['receipt']['result']=='idle',result
    assert result['checkpoint']['cursor']==0
    assert result['checkpoint']['anchor'] is None
@pytest.mark.parametrize('limit',[0,26,True,'1',None,1.0])
def test_limit_rejected_without_calls(fixture,limit):
    relay=fixture[2]
    assert relay.run(limit)['receipt']['result']=='refused'
    assert not relay.attempts
    assert relay.store.state() is None
def test_private_db_modes(fixture):
    _,_,relay,kwargs,path=fixture
    assert os.stat(path/'relay.db').st_mode&0o777==0o600
    os.chmod(path/'public.db',0o644) if (path/'public.db').exists() else (path/'public.db').write_text('')
    os.chmod(path/'public.db',0o644)
    with pytest.raises(ValueError):Relay(path/'public.db',**kwargs)
def test_foreign_db_refused(fixture):
    *_,kwargs,path=fixture
    foreign=path/'foreign.db'
    conn=sqlite3.connect(foreign);conn.execute('CREATE TABLE foreign_data(x)');conn.close();foreign.chmod(0o600)
    with pytest.raises(ValueError):Relay(foreign,**kwargs)
@pytest.mark.parametrize('value',['http://localhost:9000','http://example.com','https://example.com/path','https://user:pass@example.com','https://example.com?x=1','https://example.com/#x'])
def test_origin_refusals(value):
    with pytest.raises(ValueError):origin(value)
def test_relay_api_auth_fences_and_telemetry(fixture):
    source,target,relay,_,_=fixture
    api=TestClient(create_app(relay,{RELAY:Principal('tenant','operator')}))
    assert api.get('/v1/event-relay/status').status_code==401
    headers={'Authorization':'Bearer '+RELAY}
    session=api.get('/v1/event-relay/session',headers=headers)
    assert session.status_code==200
    assert api.post('/v1/event-relay/run',headers={**headers,'If-Gradia-Configuration':'0'*64},json={'limit':1}).status_code==409
    assert relay.store.state() is None
    seed(source)
    result=api.post('/v1/event-relay/run',headers=headers,json={'limit':1})
    assert result.status_code==200 and result.json()['receipt']['result']=='enqueued',result.text
    assert api.post('/v1/event-relay/run',headers=headers,json={'limit':True}).status_code==422
    assert api.post('/v1/event-relay/run',headers=headers,content=b'{"limit":1,"limit":2}').status_code==422
    assert api.get('/v1/event-relay/status?url=evil',headers=headers).status_code==400
    diagnostics=api.get('/v1/event-relay/diagnostics/attempts',headers=headers).json()
    assert diagnostics['items'] and diagnostics['upstream_items']
    assert SOURCE not in json.dumps(diagnostics) and TARGET not in json.dumps(diagnostics)
    assert 'PRIVATE' not in json.dumps(diagnostics)
    assert api.get('/v1/event-relay/events?limit=1&limit=2',headers=headers).status_code==400
    api.close()

def test_api_correlation_joins_durable_and_upstream_attempts(fixture):
    source,target,relay,_,_=fixture
    seed(source)
    api=TestClient(create_app(relay,{RELAY:Principal('tenant','operator')}))
    response=api.post('/v1/event-relay/run',headers={'Authorization':'Bearer '+RELAY},json={'limit':1})
    correlation=response.headers['x-gradia-correlation-id']
    receipt=response.json()['receipt']
    assert receipt['correlation_id']==correlation
    assert receipt['actor_id']=='operator'
    assert receipt['source_read_correlation_id']
    assert receipt['target_enqueue_correlation_id']
    assert all(event['correlation_id']==correlation and event['actor_id']=='operator' for event in relay.events())
    assert all(event['correlation_id']==correlation and event['actor_id']=='operator' and event['upstream_correlation_id'] for event in relay.attempts)
    enqueued=next(event for event in relay.events() if event['operation']=='relay.enqueued')
    assert enqueued['target_enqueue_correlation_id']==receipt['target_enqueue_correlation_id']
    assert enqueued['source_read_correlation_id']==receipt['source_read_correlation_id']
    assert 'PRIVATE' not in json.dumps(relay.events())
    api.close()
@pytest.mark.parametrize('fields',[{'correlation_id':'injected secret with spaces'},{'actor_id':'bad actor'},{'correlation_id':True},{'actor_id':{'oops':'x'}}])
def test_invalid_run_context_never_leaks_or_calls(fixture,fields):
    relay=fixture[2]
    result=relay.run(**fields)
    assert result['receipt']['result']=='refused'
    assert not relay.attempts
    assert result['receipt']['actor_id'] is None
    assert 'injected secret' not in json.dumps(result)

def test_success_response_contracts_and_openapi(fixture,monkeypatch):
    from gradia_event_relay.contracts import RelaySession,RelayStatus,RunResult
    import httpx
    source,target,relay,_,_=fixture
    api=TestClient(create_app(relay,{RELAY:Principal('tenant','operator')}))
    headers={'Authorization':'Bearer '+RELAY}
    specification=api.get('/openapi.json').json()
    for path,method,component in [('session','get','RelaySession'),('status','get','RelayStatus'),('run','post','RunResult')]:
        assert specification['paths']['/v1/event-relay/'+path][method]['responses']['200']['content']['application/json']['schema']['$ref'].endswith('/'+component)
        assert specification['components']['schemas'][component]['additionalProperties'] is False
    RelaySession.model_validate(api.get('/v1/event-relay/session',headers=headers).json())
    RelayStatus.model_validate(api.get('/v1/event-relay/status',headers=headers).json())
    original=relay.source_token
    relay.source_token='wrong-token-'+'x'*30
    refused=api.post('/v1/event-relay/run',headers=headers,json={'limit':1}).json()
    RunResult.model_validate(refused)
    assert refused['receipt']['result']=='refused'
    assert 'source_principal_sha256' not in refused['receipt']
    assert 'target_enqueue_correlation_id' not in refused['receipt']
    relay.source_token=original
    seed(source)
    request=relay._request
    def lose(side,method,*args,**kwargs):
        result=request(side,method,*args,**kwargs)
        if side=='target' and method=='POST':raise httpx.ReadError('simulated lost response')
        return result
    monkeypatch.setattr(relay,'_request',lose)
    uncertain=api.post('/v1/event-relay/run',headers=headers,json={'limit':1}).json()
    RunResult.model_validate(uncertain)
    assert uncertain['receipt']['result']=='uncertain'
    monkeypatch.setattr(relay,'_request',request)
    enqueued=api.post('/v1/event-relay/run',headers=headers,json={'limit':1}).json()
    RunResult.model_validate(enqueued)
    assert enqueued['receipt']['result']=='enqueued'
    assert enqueued['items'][0]['replayed'] is True
    RelayStatus.model_validate(api.get('/v1/event-relay/status',headers=headers).json())
    api.close()

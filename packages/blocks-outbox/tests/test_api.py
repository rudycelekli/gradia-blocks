from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
import hashlib
import json
import os
import pytest
from fastapi.testclient import TestClient
from gradia_outbox.api import create_app, canonical
from gradia_outbox.server import private_json

OP='operator-token-'+'a'*32
SUB='submitter-token-'+'b'*32
OTHER='other-actor-token-'+'c'*32
ALIEN='other-tenant-token-'+'d'*32
KEY='fixture-signing-key-'+'e'*32

@pytest.fixture
def client(tmp_path):
    app=create_app(tmp_path/'outbox.sqlite',{OP:{'tenant_id':'demo','actor_id':'operator','role':'operator'},SUB:{'tenant_id':'demo','actor_id':'app','role':'submitter'},OTHER:{'tenant_id':'demo','actor_id':'other','role':'submitter'},ALIEN:{'tenant_id':'alien','actor_id':'operator','role':'operator'}},{'sink':{'tenant_id':'demo','url':'http://127.0.0.1:1/webhook','signing_key':KEY,'receiver_id':'fixture'}})
    with TestClient(app) as c:
        yield c

def auth(token=OP,**extra):return {'Authorization':'Bearer '+token,**extra}
def enqueue(client,key='event-1',token=SUB):
    return client.post('/v1/outbox',headers=auth(token,**{'Idempotency-Key':key}),json={'destination_id':'sink','event_type':'item.proposed','payload':{'private':'Do not put this message into telemetry'}})

def test_idempotency_and_role_tenant_views(client):
    first=enqueue(client);assert first.status_code==201
    identity=first.json()['item']['id'];assert enqueue(client).json()=={'item':first.json()['item'],'replayed':True}
    assert client.get('/v1/outbox/'+identity,headers=auth(OTHER)).status_code==404
    assert client.get('/v1/outbox/'+identity,headers=auth(ALIEN)).status_code==404
    assert client.get('/v1/outbox',headers=auth(OTHER)).json()['items']==[]
    assert len(client.get('/v1/outbox',headers=auth(OP)).json()['items'])==1
    for operation in ['deliver','reconcile','mark-uncertain']:
        assert client.post('/v1/outbox/'+identity+'/'+operation,headers=auth(SUB),json={'expected_revision':1}).status_code==403
    assert client.get('/v1/outbox/'+identity+'/events',headers=auth(SUB)).status_code==403
    assert enqueue(client,token=ALIEN).status_code==404

@pytest.mark.parametrize('header,value,code', [('If-Gradia-Principal','0'*64,'principal_changed'),('If-Gradia-Configuration','0'*64,'configuration_changed'),('If-Gradia-Block-Version','999','block_version_changed')])
def test_fences_refuse_before_mutation(client,header,value,code):
    response=client.post('/v1/outbox',headers=auth(SUB,**{'Idempotency-Key':'blocked',header:value}),json={'destination_id':'sink','event_type':'item.proposed','payload':{}})
    assert response.status_code==409 and response.json()['error']['code']==code
    assert client.get('/v1/outbox',headers=auth(OP)).json()['items']==[]

def test_fingerprint_and_configuration_session(client):
    response=client.get('/v1/outbox/session',headers=auth());session=response.json();principal={k:session[k] for k in ['tenant_id','actor_id','role']}
    assert response.headers['x-gradia-principal-sha256']==hashlib.sha256(canonical(principal).encode()).hexdigest()
    assert len(session['configuration_digest'])==64 and response.headers['x-gradia-cost-status']=='unknown'
    summaries=client.get('/v1/outbox/destinations',headers=auth()).json();assert summaries['items']==[{'id':'sink','receiver_id':'fixture'}]
    assert client.get('/v1/outbox/destinations',headers=auth(ALIEN)).json()['items']==[]
    assert KEY not in json.dumps(summaries) and '127.0.0.1' not in json.dumps(summaries)

@pytest.mark.parametrize('body',[b'{"payload":{},"payload":{},"event_type":"x","destination_id":"sink"}',b'{"payload":{"x":NaN},"event_type":"x","destination_id":"sink"}',b'{"payload":'+b'['*20+b'0'+b']'*20+b',"event_type":"x","destination_id":"sink"}'])
def test_malformed_json_refuses_without_writes(client,body):
    response=client.post('/v1/outbox',headers=auth(SUB,**{'Content-Type':'application/json','Idempotency-Key':'invalid'}),content=body)
    assert response.status_code==422 and response.json()=={'error':{'code':'invalid_request'}}
    assert client.get('/v1/outbox',headers=auth()).json()['items']==[]

@pytest.mark.parametrize('field,value',[('url','http://127.0.0.1:9999/evil'),('signing_key','injected'),('receiver_id','other')])
def test_callers_cannot_override_destination(client,field,value):
    response=client.post('/v1/outbox',headers=auth(SUB,**{'Idempotency-Key':'forbidden'}),json={'destination_id':'sink','event_type':'x','payload':{},field:value})
    assert response.status_code==422

def test_byte_limit_applies_to_stream_not_content_length(client):
    body=b'{"padding":"'+b'x'*50000+b'"}'
    response=client.post('/v1/outbox',headers=auth(SUB,**{'Content-Type':'application/json','Idempotency-Key':'large','Content-Length':'2'}),content=body)
    assert response.status_code==413

def test_atomic_metadata_correlation_and_privacy(client):
    created=enqueue(client);identity=created.json()['item']['id'];events=client.get('/v1/outbox/'+identity+'/events',headers=auth()).json()['items']
    assert events[0]['correlation_id']==created.headers['x-gradia-correlation-id']
    serialized=json.dumps(events);assert 'Do not put this message' not in serialized and KEY not in serialized and SUB not in serialized
    for _ in range(105):client.get('/v1/outbox/session',headers=auth())
    recent=client.get('/v1/outbox/diagnostics/attempts',headers=auth()).json();assert len(recent['items'])==100
    assert 'Do not put this message' not in json.dumps(recent) and KEY not in json.dumps(recent)
    assert client.get('/v1/outbox/diagnostics/attempts',headers=auth(ALIEN)).json()['items']==[]

def test_duplicate_authority_and_unknown_query_refused(client):
    response=client.get('/v1/outbox/session',headers=[('Authorization','Bearer '+OP),('Authorization','Bearer '+ALIEN)])
    assert response.status_code==400
    assert client.get('/v1/outbox?limit=1&limit=2',headers=auth()).status_code==400
    assert client.get('/v1/outbox?tenant_id=alien',headers=auth()).status_code==400

def test_private_configuration_file_rules(tmp_path):
    path=tmp_path/'secret.json';path.write_text('{"x":1}');path.chmod(0o600);assert private_json(path)=={'x':1}
    path.chmod(0o644)
    with pytest.raises(ValueError):private_json(path)
    path.chmod(0o600);link=tmp_path/'link';link.symlink_to(path)
    with pytest.raises(OSError):private_json(link)
    path.write_text('{"x":1,"x":2}')
    with pytest.raises(ValueError):private_json(path)
    path.write_bytes(b' '*65537)
    with pytest.raises(ValueError):private_json(path)

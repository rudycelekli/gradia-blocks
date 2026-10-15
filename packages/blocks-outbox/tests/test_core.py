import concurrent.futures
import hashlib
import hmac
import json
import os
import sqlite3
import threading
import httpx
import pytest
from gradia_outbox import Destination, Principal, OutboxError, Engine, canonical_json

KEY='a'*32
SUB=Principal('tenant','submitter')
OP=Principal('tenant','operator','operator')

def ack(request, *, accepted=True, signed=True):
    payload=json.loads(request.content) if request.method=='POST' else None
    delivery_id=request.headers['x-gradia-delivery-id']
    body={'schema':'gradia.webhook.ack.v1','delivery_id':delivery_id,'payload_sha256':request.headers['x-gradia-payload-sha256'],'receiver_id':'receiver','accepted':accepted}
    material=b'gradia.webhook.ack.v1.'+request.headers['x-gradia-timestamp'].encode()+b'.'+request.headers['x-gradia-attempt-id'].encode()+b'.'+canonical_json(body)
    headers={'x-gradia-ack-signature':'sha256='+hmac.new(KEY.encode(),material,hashlib.sha256).hexdigest()} if signed else {}
    return httpx.Response(200,json=body,headers=headers)

def engine(tmp_path,handler=ack,path='outbox.sqlite'):
    return Engine(tmp_path/path,{'webhook':Destination('tenant','https://receiver.test/hooks',KEY,'receiver')},httpx.Client(transport=httpx.MockTransport(handler),trust_env=False))

def enqueue(e,key='one',payload=None):
    return e.enqueue(SUB,'webhook','feedback.created',{'text':'private'} if payload is None else payload,key)['item']

def test_enqueue_and_delivery(tmp_path):
    e=engine(tmp_path); item=enqueue(e)
    assert item['status']=='queued' and item['revision']==1
    result=e.deliver(OP,item['id'],1)
    assert result['status']=='delivered' and result['revision']==3
    assert [v['action'] for v in reversed(e.events(OP,item['id']))]==['enqueued','send_claimed','delivery_acknowledged']
    assert 'private' not in json.dumps(e.events(OP,item['id']))
    assert 'payload' not in result
    assert os.stat(tmp_path/'outbox.sqlite').st_mode&0o777==0o600

def test_idempotency_original_snapshot(tmp_path):
    e=engine(tmp_path); item=enqueue(e); e.deliver(OP,item['id'],1)
    replay=e.enqueue(SUB,'webhook','feedback.created',{'text':'private'},'one')
    assert replay=={'item':item,'replayed':True}
    with pytest.raises(OutboxError) as exc: enqueue(e,payload={'text':'changed'})
    assert exc.value.status_code==409

def test_signed_request_exact_envelope(tmp_path):
    calls=[]
    def receiver(req):
        calls.append(req)
        expected=hmac.new(KEY.encode(),req.headers['x-gradia-timestamp'].encode()+b'.'+req.content,hashlib.sha256).hexdigest()
        assert req.headers['x-gradia-signature']=='sha256='+expected
        assert req.headers['accept-encoding']=='identity'
        assert 'authorization' not in req.headers
        body=json.loads(req.content)
        assert body['payload_sha256']==hashlib.sha256(canonical_json(body['payload'])).hexdigest()
        return ack(req)
    e=engine(tmp_path,receiver); item=enqueue(e); assert e.deliver(OP,item['id'],1)['status']=='delivered'
    assert len(calls)==1

@pytest.mark.parametrize('failure',['timeout','badstatus','redirect','unsigned','false','integer','compressed','wrongreceiver','malformed'])
def test_uncertainty_never_retries(tmp_path,failure):
    calls=[]
    def receiver(req):
        calls.append(req)
        if failure=='timeout': raise httpx.ReadTimeout('SECRET raw payload')
        if failure=='badstatus': return httpx.Response(503,text='SECRET')
        if failure=='redirect': return httpx.Response(307,headers={'location':'https://evil.test'})
        if failure=='unsigned': return ack(req,signed=False)
        if failure=='false': return ack(req,accepted=False)
        if failure=='integer': return ack(req,accepted=1)
        if failure=='compressed': return httpx.Response(200,headers={'content-type':'application/json','content-encoding':'gzip'},content=b'invalid')
        if failure=='wrongreceiver': return httpx.Response(200,json={'receiver_id':'other'})
        return httpx.Response(200,headers={'content-type':'application/json'},content=b'{"x":1,"x":2}')
    e=engine(tmp_path,receiver); item=enqueue(e); result=e.deliver(OP,item['id'],1)
    assert result['status']=='uncertain' and result['revision']==3
    with pytest.raises(OutboxError): e.deliver(OP,item['id'],3)
    assert len(calls)==1
    assert 'SECRET' not in json.dumps(e.events(OP,item['id']))

def test_reconcile_after_restart_without_resend(tmp_path):
    seen={}; calls=[]
    def receiver(req):
        calls.append(req.method)
        if req.method=='POST':
            seen['id']=req.headers['x-gradia-delivery-id']; raise httpx.ReadError('response lost')
        assert req.url.path=='/hooks/receipts/'+seen['id']
        return ack(req)
    e=engine(tmp_path,receiver); item=enqueue(e); assert e.deliver(OP,item['id'],1)['status']=='uncertain'
    e2=engine(tmp_path,receiver); result=e2.reconcile(OP,item['id'],3)
    assert result['status']=='delivered' and result['revision']==4 and calls==['POST','GET']

def test_not_found_receipt_stays_uncertain(tmp_path):
    e=engine(tmp_path,lambda r:httpx.Response(404)); item=enqueue(e)
    e.deliver(OP,item['id'],1)
    assert e.reconcile(OP,item['id'],3)['status']=='uncertain'


def test_concurrent_claim_has_only_one_post(tmp_path):
    started=threading.Event(); release=threading.Event(); calls=[]
    def receiver(req):
        calls.append(req); started.set(); release.wait(5); return ack(req)
    e=engine(tmp_path,receiver); item=enqueue(e)
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        first=pool.submit(e.deliver,OP,item['id'],1); assert started.wait(2)
        with pytest.raises(OutboxError) as exc: e.deliver(OP,item['id'],1)
        assert exc.value.status_code==409
        release.set(); assert first.result()['status']=='delivered'
    assert len(calls)==1

def test_startup_does_not_recover_live_claim(tmp_path):
    e=engine(tmp_path); item=enqueue(e)
    with e.store.connection() as conn: conn.execute("UPDATE outbox_items SET status='in_flight',revision=2,attempt_id='claimed' WHERE id=?",(item['id'],))
    e2=engine(tmp_path); assert e2.get(OP,item['id'])['status']=='in_flight'
    assert e2.mark_uncertain(OP,item['id'],2)['status']=='uncertain'
    with pytest.raises(OutboxError): e2.deliver(OP,item['id'],3)

@pytest.mark.parametrize('change',['url','key','receiver','timeout'])
def test_configuration_pinned(tmp_path,change):
    e=engine(tmp_path); enqueue(e)
    args={'tenant_id':'tenant','url':'https://receiver.test/hooks','signing_key':KEY,'receiver_id':'receiver','timeout':3}
    args[{'url':'url','key':'signing_key','receiver':'receiver_id','timeout':'timeout'}[change]]={'url':'https://other.test/hooks','key':'b'*32,'receiver':'other','timeout':2}[change]
    with pytest.raises(ValueError): Engine(tmp_path/'outbox.sqlite',{'webhook':Destination(**args)})

def test_tenant_actor_permissions(tmp_path):
    e=engine(tmp_path); item=enqueue(e)
    for principal in (Principal('other','operator','operator'),Principal('tenant','different')):
        with pytest.raises(OutboxError) as exc: e.get(principal,item['id'])
        assert exc.value.status_code==404
        assert e.list(principal)==[]
    with pytest.raises(OutboxError): e.deliver(SUB,item['id'],1)
    with pytest.raises(OutboxError): e.enqueue(Principal('other','actor'),'webhook','event',{},'one')

@pytest.mark.parametrize('payload',[{'x':float('nan')},{1:'value'},{'x':2**64},{'x':'a'*16385},[1]])
def test_invalid_payload(tmp_path,payload):
    with pytest.raises(OutboxError): enqueue(engine(tmp_path),payload=payload)

@pytest.mark.parametrize('url',['http://localhost/hook','http://example.com','https://user:pass@example.com','https://example.com?a=1','https://example.com#x'])
def test_bad_destinations(url):
    with pytest.raises(ValueError): Destination('tenant',url,KEY,'receiver')

def test_trigger_schema_rejected(tmp_path):
    e=engine(tmp_path)
    with e.store.connection() as conn: conn.execute('CREATE TRIGGER extra AFTER INSERT ON outbox_items BEGIN SELECT 1; END')
    with pytest.raises(ValueError): engine(tmp_path)

def test_injected_client_defaults_never_become_authority(tmp_path):
    seen=[]
    def receiver(req):
        seen.append(req)
        assert 'authorization' not in req.headers and 'cookie' not in req.headers and 'x-private' not in req.headers
        return ack(req)
    client=httpx.Client(transport=httpx.MockTransport(receiver),headers={'Authorization':'Bearer SECRET','X-Private':'SECRET'},cookies={'auth':'SECRET'},auth=('secret','password'))
    e=Engine(tmp_path/'db',{'webhook':Destination('tenant','https://receiver.test/hooks',KEY,'receiver')},client)
    item=enqueue(e)
    assert e.deliver(OP,item['id'],1)['status']=='delivered' and len(seen)==1

def test_signed_ack_commitment_is_durable_metadata(tmp_path):
    e=engine(tmp_path); item=enqueue(e); e.deliver(OP,item['id'],1)
    e2=engine(tmp_path)
    receipt=e2.events(OP,item['id'])[0]
    assert receipt['acknowledgment_authentication']=='hmac-sha256'
    assert len(receipt['acknowledgment_sha256'])==64
    assert receipt['receiver_id']=='receiver'
    assert KEY not in repr(e.destinations['webhook'])

def test_existing_schema_without_identity_is_not_adopted(tmp_path):
    from gradia_outbox.store import SCHEMA
    path=tmp_path/'outbox.sqlite'
    fd=os.open(path,os.O_CREAT|os.O_RDWR,0o600);os.close(fd)
    conn=sqlite3.connect(path);conn.executescript(SCHEMA);conn.close()
    with pytest.raises(ValueError): engine(tmp_path)

@pytest.mark.parametrize('value',['payload secret',123,'x'*129])
def test_correlation_ids_cannot_embed_raw_payload(tmp_path,value):
    e=engine(tmp_path)
    with pytest.raises(OutboxError):e.enqueue(SUB,'webhook','event',{},'key',correlation_id=value)
    assert e.list(OP)==[]

def test_revision_capacity_reserved_before_network(tmp_path):
    from gradia_outbox.contracts import MAX_REVISION
    calls=[]
    e=engine(tmp_path,lambda request:(calls.append(request),ack(request))[1]);item=enqueue(e)
    with e.store.connection() as conn:
        conn.execute('UPDATE outbox_items SET revision=? WHERE id=?',(MAX_REVISION-1,item['id']))
    with pytest.raises(OutboxError) as exc:e.deliver(OP,item['id'],MAX_REVISION-1)
    assert exc.value.code=='revision_exhausted' and not calls
    assert e.get(OP,item['id'])['status']=='queued'
    with e.store.connection() as conn:
        conn.execute('UPDATE outbox_items SET revision=? WHERE id=?',(MAX_REVISION-2,item['id']))
    result=e.deliver(OP,item['id'],MAX_REVISION-2)
    assert result['revision']==MAX_REVISION and result['status']=='delivered' and len(calls)==1

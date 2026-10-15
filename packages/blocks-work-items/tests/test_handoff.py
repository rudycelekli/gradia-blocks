import hashlib
import json
from pathlib import Path
import pytest
import httpx
from fastapi.testclient import TestClient
from gradia_feedback import create_app as feedback_app,Principal as FeedbackPrincipal
from gradia_work_items import create_app,Principal,Settings
from gradia_work_items.handoff import FeedbackHandoff

SOURCE='source-operator-test-key-long-enough'
TARGET='target-bridge-test-key-long-enough'
OPERATOR='target-operator-test-key-long-enough'


def headers(token):return {'Authorization':'Bearer '+token}


@pytest.fixture
def environment(tmp_path):
    source=TestClient(feedback_app(tmp_path/'feedback.sqlite',{SOURCE:FeedbackPrincipal(tenant_id='alpha',actor_id='reader',role='operator')}))
    target=TestClient(create_app(tmp_path/'items.sqlite',{TARGET:Principal(tenant_id='alpha',actor_id='pipeline',role='bridge'),OPERATOR:Principal(tenant_id='alpha',actor_id='human',role='operator')}))
    seed=source.post('/v1/feedback',headers={**headers(SOURCE),'Idempotency-Key':'seed'},json={'message':'Make the pending approval clearer','category':'idea'}).json()['feedback']
    return source,target,seed


def proxy(client,seen=None,after=None):
    def handler(request):
        if seen is not None:seen.append((request.method,request.url.path))
        response=client.request(request.method,request.url.path,headers=dict(request.headers),content=request.content)
        if after:after(request,response)
        return httpx.Response(response.status_code,headers=response.headers,stream=httpx.ByteStream(response.content))
    return httpx.Client(transport=httpx.MockTransport(handler))


def bridge(source,target,**kwargs):
    return FeedbackHandoff(source_origin='http://127.0.0.1:3191',target_origin='http://127.0.0.1:3192',source_token=SOURCE,target_token=TARGET,tenant_id='alpha',source_client=source,target_client=target,**kwargs)


def test_real_api_contract_proposal_and_explicit_recovery(environment):
    source,target,feedback=environment
    with proxy(source) as sc,proxy(target) as tc:
        handoff=bridge(sc,tc);first=handoff.run(feedback['id'],1);second=handoff.run(feedback['id'],1)
    assert first['receipt']['result']=='proposed',first
    assert second['receipt']['result']=='recovered',second
    assert first['item']['id']==second['item']['id']
    assert first['item']['status']=='proposed'
    assert first['item']['description']==feedback['message']
    assert first['receipt']['source_read_correlation_id'] and first['receipt']['target_write_correlation_id']
    assert len(first['receipt']['source_configuration_sha256'])==64
    items=target.get('/v1/work-items',headers=headers(OPERATOR)).json()['items'];assert len(items)==1
    events=target.get('/v1/work-items/'+items[0]['id']+'/events',headers=headers(OPERATOR)).json()['items'];assert len(events)==1
    assert 'pending approval' not in json.dumps(first['receipt'])
    accepted=target.patch('/v1/work-items/'+items[0]['id'],headers=headers(OPERATOR),json={'expected_revision':1,'status':'open'})
    assert accepted.status_code==200
    with proxy(source) as sc,proxy(target) as tc:
        recovered=bridge(sc,tc).run(feedback['id'],1)
    assert recovered['item']['status']=='open'  # Reconciliation reads current item; never overwrites human triage.


def test_lost_acknowledgment_after_commit_does_not_duplicate(environment):
    source,target,feedback=environment
    def drop(request,response):
        if request.method=='POST':raise httpx.ReadTimeout('simulated lost response')
    with proxy(source) as sc,proxy(target,after=drop) as tc:
        lost=bridge(sc,tc).run(feedback['id'],1)
    assert lost['item'] is None and lost['receipt']['result']=='uncertain'
    with proxy(source) as sc,proxy(target) as tc:
        recovered=bridge(sc,tc).run(feedback['id'],1)
    assert recovered['receipt']['result']=='recovered'
    assert len(target.get('/v1/work-items',headers=headers(OPERATOR)).json()['items'])==1


def test_cross_tenant_session_refused_before_private_read_or_write(environment,tmp_path):
    source,_,feedback=environment;seen=[]
    target=TestClient(create_app(tmp_path/'foreign.sqlite',{TARGET:Principal(tenant_id='foreign',role='bridge')}))
    with proxy(source,seen) as sc,proxy(target) as tc:
        result=bridge(sc,tc).run(feedback['id'],1)
    assert result['receipt']['result']=='refused'
    assert seen==[('GET','/v1/feedback/session')]
    assert result['item'] is None


def test_changed_source_revision_refused_without_proposal(environment):
    source,target,feedback=environment
    with proxy(source) as sc,proxy(target) as tc:
        result=bridge(sc,tc).run(feedback['id'],2)
    assert result['receipt']['reason']=='source_revision_changed'
    assert target.get('/v1/work-items',headers=headers(OPERATOR)).json()['items']==[]


def test_changed_source_binding_after_session_is_conditionally_refused(environment,tmp_path):
    source,target,feedback=environment
    rebound=TestClient(feedback_app(tmp_path/'rebound.sqlite',{SOURCE:FeedbackPrincipal(tenant_id='beta',actor_id='reader',role='operator')}))
    def handler(request):
        server=source if request.url.path.endswith('/session') else rebound
        response=server.request(request.method,request.url.path,headers=dict(request.headers),content=request.content)
        return httpx.Response(response.status_code,headers=response.headers,stream=httpx.ByteStream(response.content))
    with httpx.Client(transport=httpx.MockTransport(handler)) as sc,proxy(target) as tc:
        result=bridge(sc,tc).run(feedback['id'],1)
    assert result['receipt']['result']=='refused'
    assert target.get('/v1/work-items',headers=headers(OPERATOR)).json()['items']==[]


def test_changed_target_configuration_before_write_creates_nothing(environment,tmp_path):
    source,target,feedback=environment
    # Same credential/principal, different immutable app config after config read.
    replacement=TestClient(create_app(tmp_path/'replacement.sqlite',{TARGET:Principal(tenant_id='alpha',actor_id='pipeline',role='bridge')},Settings(initial_status='done')))
    seen=[]
    def handler(request):
        server=replacement if request.method=='POST' else target
        response=server.request(request.method,request.url.path,headers=dict(request.headers),content=request.content)
        seen.append(response.status_code)
        return httpx.Response(response.status_code,headers=response.headers,stream=httpx.ByteStream(response.content))
    with proxy(source) as sc,httpx.Client(transport=httpx.MockTransport(handler)) as tc:
        result=bridge(sc,tc).run(feedback['id'],1)
    assert result['receipt']['result']=='refused'
    assert seen[-1]==409
    assert replacement.get('/v1/work-items',headers=headers(TARGET)).json()['items']==[]


def test_host_endpoint_and_credentials_validation():
    for origin in ['http://example.com','https://user:secret@example.com','https://example.com/path','https://example.com:bad','https://example.com\n']:
        with pytest.raises(ValueError):FeedbackHandoff(source_origin=origin,target_origin='http://127.0.0.1:1',source_token=SOURCE,target_token=TARGET,tenant_id='alpha')
    with pytest.raises(ValueError):FeedbackHandoff(source_origin='http://127.0.0.1:1',target_origin='http://127.0.0.1:2',source_token=SOURCE+'\n',target_token=TARGET,tenant_id='alpha')


def test_malformed_or_oversized_source_session_is_refused(environment):
    _,target,feedback=environment
    for body in [b'{"duplicate":1,"duplicate":2}',b'['*1000+b']'*1000,b'x'*65537]:
        with httpx.Client(transport=httpx.MockTransport(lambda request:httpx.Response(200,stream=httpx.ByteStream(body)))) as sc,proxy(target) as tc:
            result=bridge(sc,tc).run(feedback['id'],1)
        assert result['receipt']['result']=='refused'
        assert result['item'] is None
        assert 'duplicate' not in json.dumps(result['receipt'])


def test_wrong_proposal_profile_and_selector_refused(environment,tmp_path):
    source,_,feedback=environment
    target=TestClient(create_app(tmp_path/'done.sqlite',{TARGET:Principal(tenant_id='alpha',actor_id='pipeline',role='bridge')},Settings(initial_status='done')))
    with proxy(source) as sc,proxy(target) as tc:
        handoff=bridge(sc,tc)
        assert handoff.run(feedback['id'],1)['receipt']['reason']=='proposal_profile_required'
        assert handoff.run(feedback['id'],True)['receipt']['reason']=='invalid_selection'
        assert handoff.run('../private',1)['receipt']['reason']=='invalid_selection'
    assert target.get('/v1/work-items',headers=headers(TARGET)).json()['items']==[]

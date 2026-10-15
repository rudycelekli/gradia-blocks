import hashlib
import json
from fastapi.testclient import TestClient
from gradia_feedback import create_app,Principal


def test_authenticated_session_and_conditional_principal(tmp_path):
    token='session-test-token-longer-than-24'
    principal=Principal(tenant_id='alpha',actor_id='operator',role='operator')
    client=TestClient(create_app(tmp_path/'feedback.sqlite',{token:principal}))
    assert client.get('/v1/feedback/session').status_code==401
    headers={'Authorization':'Bearer '+token}
    session=client.get('/v1/feedback/session',headers=headers)
    body=session.json();assert body['tenant_id']=='alpha' and body['version']=='0.2.1'
    expected=hashlib.sha256(json.dumps(principal.model_dump(mode='json'),sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()).hexdigest()
    assert session.headers['x-gradia-principal-sha256']==expected
    assert client.get('/v1/feedback',headers={**headers,'If-Gradia-Principal':'0'*64}).status_code==409
    assert client.get('/v1/feedback',headers={**headers,'If-Gradia-Principal':expected}).status_code==200
    assert client.get('/v1/feedback',headers={**headers,'If-Gradia-Block-Version':'0.2.0'}).status_code==409
    assert client.get('/v1/feedback',headers={**headers,'If-Gradia-Configuration':'0'*64}).status_code==409
    assert session.headers['x-gradia-cost-status']=='unknown' and session.headers['x-gradia-correlation-id']
    assert token not in session.text

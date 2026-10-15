import json
import pytest
from fastapi.testclient import TestClient
from gradia_work_items import create_app,Principal
from gradia_work_items.server import configured_app,private_json


def test_private_config_file_and_redacted_startup_failures(tmp_path,monkeypatch):
    path=tmp_path/'credentials.json';path.write_text(json.dumps({'synthetic-secret-token-long-enough':{'tenant_id':'alpha','role':'bridge'}}));path.chmod(0o600)
    monkeypatch.setenv('GRADIA_WORK_ITEMS_CREDENTIALS_FILE',str(path));monkeypatch.setenv('GRADIA_WORK_ITEMS_DATABASE',str(tmp_path/'db.sqlite'))
    assert configured_app().state.store.path.endswith('db.sqlite')
    path.chmod(0o644)
    with pytest.raises(RuntimeError) as error:configured_app()
    assert 'synthetic-secret' not in str(error.value)
    path.chmod(0o600);path.write_text('{"duplicate":1,"duplicate":2}')
    with pytest.raises(ValueError):private_json(path)


def test_symlink_and_oversized_configuration_files_refused(tmp_path):
    path=tmp_path/'secret.json';path.write_text('{}');path.chmod(0o600)
    link=tmp_path/'link';link.symlink_to(path)
    with pytest.raises(OSError):private_json(link)
    path.write_text('x'*65537)
    with pytest.raises(ValueError):private_json(path)


def test_revision_exhaustion_does_not_mutate_or_append_event(tmp_path):
    token='synthetic-operator-key-long-enough';app=create_app(tmp_path/'db.sqlite',{token:Principal(tenant_id='alpha',role='operator')});client=TestClient(app);headers={'Authorization':'Bearer '+token}
    item=client.post('/v1/work-items',headers={**headers,'Idempotency-Key':'initial'},json={'title':'Review'}).json()['item']
    with app.state.store.connection() as conn:conn.execute('UPDATE work_items SET revision=2147483647 WHERE id=?',(item['id'],))
    result=client.patch('/v1/work-items/'+item['id'],headers=headers,json={'expected_revision':2147483647,'status':'open'})
    assert result.status_code==409 and result.json()['error']['code']=='revision_exhausted'
    assert len(client.get('/v1/work-items/'+item['id']+'/events',headers=headers).json()['items'])==1


def test_state_and_assignee_filters_remain_tenant_scoped(tmp_path):
    op='operator-test-key-longer-than-24';other='foreign-test-key-longer-than-24'
    app=create_app(tmp_path/'db.sqlite',{op:Principal(tenant_id='alpha',role='operator'),other:Principal(tenant_id='beta',role='operator')});client=TestClient(app);headers={'Authorization':'Bearer '+op}
    first=client.post('/v1/work-items',headers={**headers,'Idempotency-Key':'one'},json={'title':'One'}).json()['item']
    client.post('/v1/work-items',headers={**headers,'Idempotency-Key':'two'},json={'title':'Two'})
    client.patch('/v1/work-items/'+first['id'],headers=headers,json={'expected_revision':1,'status':'open','assignee':'reviewer'})
    matched=client.get('/v1/work-items?status=open&assignee=reviewer',headers=headers).json()['items']
    assert [item['id'] for item in matched]==[first['id']]
    assert client.get('/v1/work-items?status=open',headers={'Authorization':'Bearer '+other}).json()['items']==[]
    assert client.get('/v1/work-items?status=unknown',headers=headers).status_code==422
    assert client.get('/v1/work-items?tenant=beta',headers=headers).status_code==400
    assert client.get('/v1/work-items?status=open&status=done',headers=headers).status_code==400
    denied=client.post('/v1/work-items',headers={**headers,'Idempotency-Key':'wrong-edition','If-Gradia-Block-Version':'0.0.1'},json={'title':'Do not create'})
    assert denied.status_code==409

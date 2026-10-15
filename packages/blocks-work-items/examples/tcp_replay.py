"""Real loopback TCP replay in temporary private stores. No provider/model calls."""
from pathlib import Path
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import hashlib
import httpx
from gradia_work_items.handoff import FeedbackHandoff


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));return sock.getsockname()[1]


class DropCommittedPost(httpx.BaseTransport):
    """Real TCP request; discard response after receiving committed response headers."""
    def __init__(self):self.inner=httpx.HTTPTransport(trust_env=False)
    def handle_request(self,request):
        response=self.inner.handle_request(request)
        if request.method=='POST' and request.url.path=='/v1/work-items':
            assert response.status_code==201
            response.close()
            raise httpx.ReadTimeout('Synthetic post-commit acknowledgment loss')
        return response
    def close(self):self.inner.close()


def replay(lose_ack=False):
    source_token,target_token,operator_token=[secrets.token_hex(24) for _ in range(3)]
    source_port,target_port=free_port(),free_port()
    while target_port==source_port:target_port=free_port()
    processes=[];logs=[]
    with tempfile.TemporaryDirectory(prefix='gradia-work-items-tcp-') as temporary:
        base=Path(temporary);base.chmod(0o700)
        source_keys=base/'source.json';target_keys=base/'target.json'
        source_keys.write_text(json.dumps({source_token:{'tenant_id':'demo','actor_id':'feedback-reader','role':'operator'}}));source_keys.chmod(0o600)
        target_keys.write_text(json.dumps({target_token:{'tenant_id':'demo','actor_id':'proposal-pipeline','role':'bridge'},operator_token:{'tenant_id':'demo','actor_id':'human-triage','role':'operator'}}));target_keys.chmod(0o600)
        environment={'PATH':os.environ.get('PATH','/usr/bin:/bin'),'PYTHONPATH':os.environ.get('PYTHONPATH',''),'LANG':'C.UTF-8'}
        source_env={**environment,'GRADIA_FEEDBACK_CREDENTIALS_FILE':str(source_keys),'GRADIA_FEEDBACK_DATABASE':str(base/'feedback.sqlite'),'GRADIA_FEEDBACK_PORT':str(source_port)}
        target_env={**environment,'GRADIA_WORK_ITEMS_CREDENTIALS_FILE':str(target_keys),'GRADIA_WORK_ITEMS_DATABASE':str(base/'items.sqlite'),'GRADIA_WORK_ITEMS_PORT':str(target_port)}
        source_url=f'http://127.0.0.1:{source_port}';target_url=f'http://127.0.0.1:{target_port}'
        def start(module,env):
            stream=open(base/(module.rsplit('.',1)[0]+str(len(logs))+'.log'),'wb');logs.append(stream)
            process=subprocess.Popen([sys.executable,'-m',module],env=env,stdout=stream,stderr=stream);processes.append(process);return process
        def wait(url,token):
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                try:
                    response=httpx.get(url,headers={'Authorization':'Bearer '+token},timeout=.5,trust_env=False)
                    if response.status_code==200:return response.json()
                except httpx.HTTPError:pass
                time.sleep(.05)
            raise RuntimeError('Loopback service startup failed')
        try:
            start('gradia_feedback.server',source_env);target_process=start('gradia_work_items.server',target_env)
            wait(source_url+'/v1/feedback/session',source_token);wait(target_url+'/v1/work-items/session',target_token)
            with httpx.Client(trust_env=False,timeout=3) as client:
                feedback=client.post(source_url+'/v1/feedback',headers={'Authorization':'Bearer '+source_token,'Idempotency-Key':'tcp-seed'},json={'message':'Clarify the pending review state','category':'idea'}).json()['feedback']
                def handoff(drop=False):
                    if drop:
                        with httpx.Client(transport=DropCommittedPost(),trust_env=False) as target_client:
                            with FeedbackHandoff(source_origin=source_url,target_origin=target_url,source_token=source_token,target_token=target_token,tenant_id='demo',target_client=target_client) as runner:
                                return runner.run(feedback['id'],1)
                    with FeedbackHandoff(source_origin=source_url,target_origin=target_url,source_token=source_token,target_token=target_token,tenant_id='demo') as runner:
                        return runner.run(feedback['id'],1)
                first=handoff(lose_ack)
                assert first['receipt']['result']==('uncertain' if lose_ack else 'proposed'),first['receipt']
                persisted=client.get(target_url+'/v1/work-items',headers={'Authorization':'Bearer '+operator_token}).json()['items']
                assert len(persisted)==1
                identity=persisted[0]['id'];assert persisted[0]['status']=='proposed'
                target_process.terminate();target_process.wait(timeout=5)
                start('gradia_work_items.server',target_env);wait(target_url+'/v1/work-items/session',target_token)
                recovered=handoff();assert recovered['receipt']['result']=='recovered' and recovered['item']['id']==identity
                operator={'Authorization':'Bearer '+operator_token}
                accepted=client.patch(target_url+'/v1/work-items/'+identity,headers=operator,json={'expected_revision':1,'status':'open'})
                assert accepted.status_code==200
                started=client.patch(target_url+'/v1/work-items/'+identity,headers=operator,json={'expected_revision':2,'status':'in_progress'})
                assert started.status_code==200
                stale=client.patch(target_url+'/v1/work-items/'+identity,headers=operator,json={'expected_revision':2,'status':'done'})
                assert stale.status_code==409
                completed=client.patch(target_url+'/v1/work-items/'+identity,headers=operator,json={'expected_revision':3,'status':'done'})
                assert completed.status_code==200
                events=client.get(target_url+'/v1/work-items/'+identity+'/events',headers=operator).json()['items']
                items=client.get(target_url+'/v1/work-items',headers=operator).json()['items']
                assert len(items)==1 and len(events)==4
                assert 'Clarify the pending' not in json.dumps(events)
                return {'schema':'gradia.work-items.tcp-replay.v1','transport':'real_loopback_tcp','services':'two independent subprocesses','sourceVersion':'0.2.1','targetVersion':'0.2.0','postCommitAcknowledgmentDropped':lose_ack,'proposal':first['receipt'],'restartRecovery':recovered['receipt'],'sameItemAfterRestart':True,'persistedItems':1,'operatorTransitions':['proposed','open','in_progress','done'],'staleRevisionStatus':stale.status_code,'durableMutationEvents':len(events),'eventMetadataOnly':True,'finalRevision':completed.json()['item']['revision'],'credentialsLogged':False,'scope':'Synthetic local acceptance fixture; no customer workflow acceptance, provider calls or enterprise admission'}
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                    try:process.wait(timeout=5)
                    except subprocess.TimeoutExpired:process.kill();process.wait()
            for stream in logs:stream.close()


if __name__=='__main__':
    print(json.dumps({'happyPath':replay(),'postCommitLoss':replay(lose_ack=True)},indent=2))

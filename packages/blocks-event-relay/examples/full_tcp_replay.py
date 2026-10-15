"""Actual local HTTP chain and disposable stores. No provider/customer calls.

Feedback -> Work Items -> Event Relay -> Outbox -> signed fixture receiver.
A local forwarding proxy injects a real lost enqueue acknowledgment. Separate
receiver faults lose committed delivery acknowledgments; explicit GET reconciles.
"""
from pathlib import Path
import json
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import httpx
from gradia_work_items.handoff import FeedbackHandoff


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));return sock.getsockname()[1]


def replay(lose_ack=False):
    ports=set()
    while len(ports)<6:ports.add(free_port())
    urls=dict(zip(['feedback','items','outbox','receiver','proxy','relay'],[f'http://127.0.0.1:{p}' for p in sorted(ports)]))
    tokens={key:secrets.token_hex(24) for key in ['feedback','bridge','item-reader','submitter','sender','relay']};signing_key=secrets.token_hex(32)
    processes=[];streams=[];roots=Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix='gradia-event-relay-tcp-') as directory:
        base=Path(directory);base.chmod(0o700)
        def private(name,value):
            path=base/name;path.write_text(json.dumps(value));path.chmod(0o600);return path
        feedback_keys=private('feedback-keys.json',{tokens['feedback']:{'tenant_id':'demo','actor_id':'capture-reader','role':'operator'}})
        item_keys=private('items-keys.json',{tokens['bridge']:{'tenant_id':'demo','actor_id':'proposal-pipeline','role':'bridge'},tokens['item-reader']:{'tenant_id':'demo','actor_id':'journal-reader','role':'operator'}})
        outbox_keys=private('outbox-keys.json',{tokens['submitter']:{'tenant_id':'demo','actor_id':'event-relay','role':'submitter'},tokens['sender']:{'tenant_id':'demo','actor_id':'notification-operator','role':'operator'}})
        relay_keys=private('relay-keys.json',{tokens['relay']:{'tenant_id':'demo','actor_id':'relay-operator','role':'operator'}})
        destinations=private('destinations.json',{'fixture':{'tenant_id':'demo','url':urls['receiver']+'/webhook','receiver_id':'durable-fixture','signing_key':signing_key}})
        profile=private('relay-profile.json',{'source_origin':urls['items'],'target_origin':urls['proxy'],'source_token':tokens['item-reader'],'target_token':tokens['submitter'],'tenant_id':'demo','profile_id':'work-item-events','destination_id':'fixture'})
        receiver_cfg={'origin':urls['receiver'],'database':str(base/'receiver.sqlite'),'receiver_id':'durable-fixture','signing_key':signing_key,'lose_ack':lose_ack}
        receiver_file=private('receiver.json',receiver_cfg)
        proxy_cfg={'origin':urls['proxy'],'target_origin':urls['outbox'],'lose_ack':lose_ack}
        proxy_file=private('proxy.json',proxy_cfg)
        environment={'PATH':os.environ.get('PATH','/usr/bin:/bin'),'PYTHONPATH':os.environ.get('PYTHONPATH',''),'LANG':'C.UTF-8'}
        configs={
            'feedback':(['-m','gradia_feedback.server'],{'GRADIA_FEEDBACK_DATABASE':str(base/'feedback.sqlite'),'GRADIA_FEEDBACK_CREDENTIALS_FILE':str(feedback_keys),'GRADIA_FEEDBACK_PORT':str(urlsplit_port(urls['feedback']))}),
            'items':(['-m','gradia_work_items.server'],{'GRADIA_WORK_ITEMS_DATABASE':str(base/'items.sqlite'),'GRADIA_WORK_ITEMS_CREDENTIALS_FILE':str(item_keys),'GRADIA_WORK_ITEMS_PORT':str(urlsplit_port(urls['items']))}),
            'outbox':(['-m','gradia_outbox.server'],{'GRADIA_OUTBOX_DATABASE':str(base/'outbox.sqlite'),'GRADIA_OUTBOX_CREDENTIALS_FILE':str(outbox_keys),'GRADIA_OUTBOX_DESTINATIONS_FILE':str(destinations),'GRADIA_OUTBOX_PORT':str(urlsplit_port(urls['outbox']))}),
            'receiver':([str(roots/'blocks-outbox/examples/fixture_receiver.py')],{'GRADIA_FIXTURE_CONFIG':str(receiver_file)}),
            'proxy':([str(Path(__file__).with_name('fixture_enqueue_proxy.py'))],{'GRADIA_ENQUEUE_PROXY_CONFIG':str(proxy_file)}),
            'relay':(['-m','gradia_event_relay.server'],{'GRADIA_EVENT_RELAY_DATABASE':str(base/'relay.sqlite'),'GRADIA_EVENT_RELAY_CREDENTIALS_FILE':str(relay_keys),'GRADIA_EVENT_RELAY_PROFILE_FILE':str(profile),'GRADIA_EVENT_RELAY_PORT':str(urlsplit_port(urls['relay']))}),
        }
        active={}
        def start(name):
            args,env=configs[name];stream=open(base/(name+'-'+str(len(streams))+'.log'),'wb');streams.append(stream)
            process=subprocess.Popen([sys.executable,*args],env={**environment,**env},stdout=stream,stderr=stream);processes.append(process);active[name]=process
        def stop(name):
            process=active[name]
            if process.poll() is None:process.terminate();process.wait(timeout=5)
        def auth(name):return {'Authorization':'Bearer '+tokens[name]}
        def wait(name,path,token):
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                try:
                    response=httpx.get(urls[name]+path,headers=auth(token) if token else None,timeout=.5,trust_env=False)
                    if response.status_code==200:return response.json()
                except httpx.HTTPError:pass
                if active[name].poll() is not None:raise RuntimeError('Fixture process failed: '+name)
                time.sleep(.05)
            raise RuntimeError('Fixture startup timed out: '+name)
        def wait_all():
            for name,path,token in [('feedback','/v1/feedback/session','feedback'),('items','/v1/work-items/session','item-reader'),('outbox','/v1/outbox/session','sender'),('receiver','/health',None),('proxy','/v1/outbox/session','submitter'),('relay','/v1/event-relay/session','relay')]:wait(name,path,token)
        try:
            for name in configs:start(name)
            wait_all()
            with httpx.Client(trust_env=False,timeout=30) as client:
                report=client.post(urls['feedback']+'/v1/feedback',headers={**auth('feedback'),'Idempotency-Key':'fictional-report'},json={'message':'Fictional feedback text must not cross the notification relay','category':'idea'})
                assert report.status_code==201,report.text
                feedback=report.json()['feedback']
                with FeedbackHandoff(source_origin=urls['feedback'],target_origin=urls['items'],source_token=tokens['feedback'],target_token=tokens['bridge'],tenant_id='demo') as handoff:
                    proposal=handoff.run(feedback['id'],1)
                assert proposal['receipt']['result']=='proposed',proposal
                item=proposal['item'];identity=item['id']
                with sqlite3.connect(base/'items.sqlite') as source,sqlite3.connect(base/'prefix-backup.sqlite') as backup:source.backup(backup)
                (base/'prefix-backup.sqlite').chmod(0o600)
                first=client.post(urls['relay']+'/v1/event-relay/run',headers=auth('relay'),json={'limit':1})
                assert first.status_code==200,first.text
                first_correlation=first.headers['x-gradia-correlation-id'];first=first.json();assert first['receipt']['correlation_id']==first_correlation and first['receipt']['actor_id']=='relay-operator';assert first['receipt']['result']==('uncertain' if lose_ack else 'enqueued'),first
                first_cursor=(first.get('checkpoint') or {}).get('cursor',0)
                assert first_cursor==(0 if lose_ack else 1),first
                queued=client.get(urls['outbox']+'/v1/outbox',headers=auth('sender')).json()['items'];assert len(queued)==1
                with sqlite3.connect(base/'receiver.sqlite') as c:assert c.execute('SELECT COUNT(*) FROM receipts').fetchone()[0]==0
                updated=client.patch(urls['items']+'/v1/work-items/'+identity,headers=auth('item-reader'),json={'expected_revision':1,'status':'open'})
                assert updated.status_code==200,updated.text
                for name in configs:stop(name)
                proxy_cfg['lose_ack']=False;private('proxy.json',proxy_cfg)
                for name in configs:start(name)
                wait_all()
                recovered=client.post(urls['relay']+'/v1/event-relay/run',headers=auth('relay'),json={'limit':25})
                assert recovered.status_code==200,recovered.text
                recovered=recovered.json();assert recovered['receipt']['result']=='enqueued',recovered
                assert recovered['checkpoint']['cursor']==2,recovered
                assert recovered['receipt']['source_read_correlation_id'] and recovered['receipt']['target_enqueue_correlation_id']
                repeated=client.post(urls['relay']+'/v1/event-relay/run',headers=auth('relay'),json={'limit':25})
                assert repeated.status_code==200 and repeated.json()['receipt']['result']=='idle',repeated.text
                queued=client.get(urls['outbox']+'/v1/outbox',headers=auth('sender')).json()['items'];assert len(queued)==2 and len({q['id'] for q in queued})==2
                evidence=client.get(urls['relay']+'/v1/event-relay/events',headers=auth('relay')).json()['items']
                assert all('Fictional feedback text' not in json.dumps(e) for e in evidence)
                assert any(e.get('operation')=='relay.enqueued' and e.get('source_read_correlation_id') and e.get('target_enqueue_correlation_id') and e.get('actor_id')=='relay-operator' for e in evidence)
                # Enqueue-only authority cannot trigger delivery through the target.
                forbidden=client.post(urls['outbox']+'/v1/outbox/'+queued[0]['id']+'/deliver',headers=auth('submitter'),json={'expected_revision':1});assert forbidden.status_code==403
                outcomes=[]
                for row in queued:
                    delivery=client.post(urls['outbox']+'/v1/outbox/'+row['id']+'/deliver',headers=auth('sender'),json={'expected_revision':1})
                    assert delivery.status_code==200,delivery.text
                    outcomes.append(delivery.json());assert outcomes[-1]['status']==('uncertain' if lose_ack else 'delivered')
                stop('outbox');stop('receiver');receiver_cfg['lose_ack']=False;private('receiver.json',receiver_cfg);start('outbox');start('receiver')
                wait('outbox','/v1/outbox/session','sender');wait('receiver','/health',None)
                for row in outcomes:
                    if row['status']=='uncertain':
                        response=client.post(urls['outbox']+'/v1/outbox/'+row['id']+'/reconcile',headers=auth('sender'),json={'expected_revision':row['revision']});assert response.status_code==200,response.text
                        assert response.json()['status']=='delivered'
                with sqlite3.connect(base/'receiver.sqlite') as c:
                    effects=c.execute('SELECT COUNT(*) FROM receipts').fetchone()[0];posts=c.execute("SELECT value FROM counters WHERE name='posts'").fetchone()[0]
                    envelopes=[json.loads(bytes(r[0])) for r in c.execute('SELECT envelope FROM receipts')]
                assert effects==posts==2
                assert all('Fictional feedback text' not in json.dumps(e) for e in envelopes)
                final=client.get(urls['outbox']+'/v1/outbox',headers=auth('sender')).json()['items'];assert all(r['status']=='delivered' for r in final)
                # Actual source backup restore lacks the saved cursor anchor: refuse,
                # without reinterpreting or rewinding that checkpoint.
                stop('items')
                with sqlite3.connect(base/'prefix-backup.sqlite') as backup,sqlite3.connect(base/'items.sqlite') as source:backup.backup(source)
                start('items');wait('items','/v1/work-items/session','item-reader')
                restored=client.post(urls['relay']+'/v1/event-relay/run',headers=auth('relay'),json={'limit':25})
                assert restored.status_code==200,restored.text
                restored=restored.json();assert restored['receipt']['result']=='refused',restored
                assert restored['checkpoint']['cursor']==2,restored
                assert len(client.get(urls['outbox']+'/v1/outbox',headers=auth('sender')).json()['items'])==2
                return {'initialRelayResult':first['receipt']['result'],'initialCursor':first_cursor,'afterRestartCursor':recovered['checkpoint']['cursor'],'replayedEnqueueRecovered':any(r.get('replayed') for r in recovered['items']) if lose_ack else None,'repeatRelayResult':repeated.json()['receipt']['result'],'uniqueQueuedItems':2,'receiverPostRequests':posts,'durableReceiverRecords':effects,'finalOutboxStates':[r['status'] for r in final],'relayCannotDeliverHttpStatus':forbidden.status_code,'sourceRestoreResult':restored['receipt']['result'],'sourceRestorePreservesCursor':restored['checkpoint']['cursor']==2,'rawFeedbackTextForwarded':False,'apiAndCrossServiceCorrelationJoined':True,'cost':{'status':'unknown'},'scope':'Actual local HTTP processes, independent private stores, scripted operator actions and fictional feedback; no customer acceptance or universal exactly-once guarantee'}
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                    try:process.wait(timeout=5)
                    except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
            for stream in streams:stream.close()


def urlsplit_port(url):return int(url.rsplit(':',1)[1])

if __name__=='__main__':print(json.dumps({'schema':'gradia.event-relay.full-http-replay.v1','happyPath':replay(),'combinedAcknowledgmentLoss':replay(True)},indent=2))

"""One explicit bounded relay invocation. Enqueue permission never grants delivery."""
from collections import deque
from datetime import datetime,timezone
import hashlib
import sqlite3
import re
import time
from urllib.parse import urlencode
import uuid
import httpx
from pydantic import ValidationError
from .contracts import (VERSION,RelayError,Session,SourceSession,Page,Acknowledgment,Anchor,
    canonical,sha,parse_json,origin,token,identifier,timestamp)
from .store import Store

def now():return datetime.now(timezone.utc).isoformat()
def response_correlation(headers):
    value=headers.get('x-gradia-correlation-id')
    return value if type(value) is str and re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',value) else None

class Relay:
    def __init__(self,database_path,*,source_origin,target_origin,source_token,target_token,tenant_id,profile_id,destination_id,source_client=None,target_client=None):
        self.source_origin=origin(source_origin);self.target_origin=origin(target_origin)
        self.source_token=token(source_token);self.target_token=token(target_token)
        self.tenant_id=identifier(tenant_id);self.profile_id=identifier(profile_id);self.destination_id=identifier(destination_id)
        if len(destination_id)>100 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-' for c in destination_id):raise ValueError('Invalid destination identifier')
        self.config_digest=sha({'tenant_id':tenant_id,'profile_id':profile_id,'destination_id':destination_id,'source_origin':self.source_origin,'target_origin':self.target_origin,'source_token_sha256':hashlib.sha256(source_token.encode()).hexdigest(),'target_token_sha256':hashlib.sha256(target_token.encode()).hexdigest(),'version':VERSION})
        self.store=Store(database_path,self.config_digest)
        self.source_client=source_client or httpx.Client(trust_env=False)
        self.target_client=target_client or httpx.Client(trust_env=False)
        self._owned=[c for c,s in ((self.source_client,source_client),(self.target_client,target_client)) if s is None]
        self.attempts=deque(maxlen=100)
    def close(self):
        for client in self._owned:client.close()
    def __enter__(self):return self
    def __exit__(self,*args):self.close()
    def _request(self,side,method,path,deadline,session=None,payload=None,key=None,*,correlation_id=None,actor_id=None):
        client,base,credential=(self.source_client,self.source_origin,self.source_token) if side=='source' else (self.target_client,self.target_origin,self.target_token)
        headers={'Authorization':'Bearer '+credential,'Accept':'application/json','Accept-Encoding':'identity'}
        if session:
            headers.update({'If-Gradia-Principal':session['principal_sha256'],'If-Gradia-Configuration':session['configuration_digest'],'If-Gradia-Block-Version':'0.2.0' if side=='source' else '0.1.0'})
        if key:headers['Idempotency-Key']=key
        if payload is not None:headers['Content-Type']='application/json'
        remaining=deadline-time.monotonic()
        if remaining<=0:raise RelayError('deadline_exceeded')
        started=time.monotonic();status=None;upstream_correlation=None
        try:
            request=httpx.Request(method,base+path,headers=headers,content=canonical(payload) if payload is not None else None,extensions={'timeout':dict.fromkeys(('connect','read','write','pool'),min(3,remaining))})
            response=client.send(request,stream=True,auth=None,follow_redirects=False)
            try:
                status=response.status_code
                received=response.headers.get('x-gradia-correlation-id')
                if type(received) is str and re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',received):upstream_correlation=received
                if response.headers.get('content-encoding','identity').lower()!='identity':raise RelayError('response_encoding_refused')
                if response.headers.get('content-type','').split(';')[0].lower().strip()!='application/json':raise RelayError('response_content_type_refused')
                if session and response.headers.get('x-gradia-principal-sha256')!=session['principal_sha256']:raise RelayError('principal_response_mismatch')
                body=bytearray()
                chunks=(response.content,) if response.is_stream_consumed else response.iter_raw()
                for chunk in chunks:
                    if time.monotonic()>deadline:raise RelayError('deadline_exceeded')
                    body.extend(chunk)
                    if len(body)>262144:raise RelayError('response_limit')
                return status,parse_json(bytes(body)),response.headers
            finally:response.close()
        finally:
            self.attempts.append({'schema':'gradia.event-relay.upstream-attempt.v1','event_id':str(uuid.uuid4()),'tenant_id':self.tenant_id,'profile_id':self.profile_id,'correlation_id':correlation_id,'actor_id':actor_id,'upstream_correlation_id':upstream_correlation,'side':side,'method':method,'status':status,'occurred_at':now(),'duration_ms':round((time.monotonic()-started)*1000,3),'configuration_digest':self.config_digest,'privacy':'metadata_only','cost':{'status':'unknown','amount':None},'coverage':'last_100_upstream_attempts_process_local'})
    def _session(self,side,deadline,*,correlation_id=None,actor_id=None):
        status,data,headers=self._request(side,'GET','/v1/work-items/session' if side=='source' else '/v1/outbox/session',deadline,correlation_id=correlation_id,actor_id=actor_id)
        if status!=200:raise RelayError('session_refused')
        session=(SourceSession if side=='source' else Session).model_validate(data)
        if session.tenant_id!=self.tenant_id or session.version!=('0.2.0' if side=='source' else '0.1.0') or session.role!=('operator' if side=='source' else 'submitter'):raise RelayError('session_scope_role_or_version_refused')
        fingerprint=sha({k:getattr(session,k) for k in ('tenant_id','actor_id','role')})
        if headers.get('x-gradia-principal-sha256')!=fingerprint:raise RelayError('principal_response_mismatch')
        result={'principal_sha256':fingerprint,'configuration_digest':session.configuration_digest}
        if side=='source':result['journal_id']=session.journal_id
        return result
    def status(self):
        return {'schema':'gradia.event-relay.status.v1','version':VERSION,'tenant_id':self.tenant_id,'profile_id':self.profile_id,'configuration_digest':self.config_digest,'checkpoint':self.store.state(),'automatic_polling':False,'delivery_authorized':False}
    def events(self,limit=100):return self.store.events(limit)
    def _event(self,operation,**extra):
        return {'schema':'gradia.event-relay.event.v1','block_id':'gradia.event-relay','edition':VERSION,'event_id':str(uuid.uuid4()),'tenant_id':self.tenant_id,'profile_id':self.profile_id,'occurred_at':now(),'operation':operation,'configuration_digest':self.config_digest,'privacy':'metadata_only','cost':{'status':'unknown','amount':None,'currency':None},'acceptance':'not_evaluated','billability':'not_evaluated','human_involvement':'unknown',**extra}
    def run(self,limit=25,*,correlation_id=None,actor_id=None):
        started=time.monotonic();deadline=started+20;phase='validate';write_attempted=False;queued=[]
        receipt=self._event('relay.run',correlation_id=str(uuid.uuid4()),actor_id=None,result='refused',reason=None,automatic_retry=False,delivery_authorized=False,coverage='this_invocation_only')
        try:
            if type(limit) is not int or not 1<=limit<=25:raise RelayError('invalid_limit',422)
            if correlation_id is not None:identifier(correlation_id)
            if actor_id is not None:identifier(actor_id)
            receipt['correlation_id']=correlation_id or receipt['correlation_id'];receipt['actor_id']=actor_id
            run_context={'correlation_id':receipt['correlation_id'],'actor_id':actor_id}
            with self.store.lock():
                phase='sessions';source=self._session('source',deadline,**run_context);target=self._session('target',deadline,**run_context)
                bindings={'source':source,'target':target}
                state=self.store.state()
                if state is not None and state['bindings']!=bindings:raise RelayError('binding_changed')
                anchor=state['anchor'] if state else None;cursor=state['cursor'] if state else 0
                query={'after_sequence':cursor,'limit':limit,'expected_journal_id':source['journal_id']}
                if anchor:
                    checked=Anchor.model_validate(anchor)
                    if checked.sequence!=cursor:raise RelayError('invalid_checkpoint')
                    query.update(after_event_id=checked.event_id,after_event_sha256=checked.event_sha256)
                elif cursor!=0:raise RelayError('invalid_checkpoint')
                phase='source_page';status,data,headers=self._request('source','GET','/v1/work-items/changes?'+urlencode(query),deadline,source,**run_context)
                receipt['source_read_correlation_id']=response_correlation(headers)
                if status!=200:raise RelayError('source_cursor_refused')
                page=Page.model_validate(data)
                if page.journal_id!=source['journal_id'] or page.tenant_id!=self.tenant_id or page.head_sequence<cursor or len(page.items)>limit:raise RelayError('page_binding_mismatch')
                previous=cursor;ids=set()
                # Validate the complete page before binding or producing any target effect.
                for change in page.items:
                    event=change.event
                    if not previous<change.sequence<=page.head_sequence or event.tenant_id!=self.tenant_id or event.event_id in ids or event.edition!=event.block_version:raise RelayError('invalid_change_page')
                    if (event.operation=='item.proposed' and event.revision!=1) or (event.operation=='item.updated' and event.revision<2):raise RelayError('invalid_event_revision')
                    timestamp(event.occurred_at);ids.add(event.event_id);previous=change.sequence
                if page.next_after!=previous or (not page.items and page.head_sequence!=cursor) or (len(page.items)<limit and previous!=page.head_sequence):raise RelayError('invalid_page_cursor')
                state={'bindings':bindings,'anchor':anchor,'cursor':cursor}
                if self.store.state() is None:self.store.save(state,self._event('relay.bound',journal_id=source['journal_id'],**run_context))
                receipt.update(source_principal_sha256=source['principal_sha256'],target_principal_sha256=target['principal_sha256'],journal_id=source['journal_id'])
                for change,raw_change in zip(page.items,data['items']):
                    event=change.event;raw=raw_change['event'];event_sha=sha(raw)
                    payload={'source':{'journal_id':page.journal_id,'sequence':change.sequence,'event_id':event.event_id,'event_sha256':event_sha,'block_version':event.block_version},'work_item':{'item_id':event.item_id,'revision':event.revision,'operation':event.operation}}
                    event_type='work_item.proposed' if event.operation=='item.proposed' else 'work_item.updated'
                    key=sha({'profile_id':self.profile_id,'journal_id':page.journal_id,'event_id':event.event_id})
                    phase='target_enqueue';write_attempted=True
                    status,data,headers=self._request('target','POST','/v1/outbox',deadline,target,payload={'destination_id':self.destination_id,'event_type':event_type,'payload':payload},key=key,**run_context)
                    receipt['target_enqueue_correlation_id']=response_correlation(headers)
                    if status not in (200,201):raise RelayError('enqueue_not_acknowledged')
                    ack=Acknowledgment.model_validate(data);item=ack.item
                    if item.destination_id!=self.destination_id or item.event_type!=event_type or item.payload_sha256!=sha(payload) or item.revision!=1 or item.created_at!=item.updated_at:raise RelayError('enqueue_acknowledgment_mismatch')
                    timestamp(item.created_at);timestamp(item.updated_at)
                    if (status==200)!=ack.replayed:raise RelayError('enqueue_replay_mismatch')
                    phase='checkpoint';new_anchor={'sequence':change.sequence,'event_id':event.event_id,'event_sha256':event_sha}
                    state={'bindings':bindings,'anchor':new_anchor,'cursor':change.sequence}
                    self.store.save(state,self._event('relay.enqueued',source_anchor=new_anchor,outbox_item_id=item.id,replayed=ack.replayed,run_event_id=receipt['event_id'],source_read_correlation_id=receipt['source_read_correlation_id'],target_enqueue_correlation_id=receipt['target_enqueue_correlation_id'],**run_context))
                    write_attempted=False;queued.append({'source_sequence':change.sequence,'outbox_item_id':item.id,'replayed':ack.replayed})
                receipt['result']='enqueued' if queued else 'idle'
        except Exception as exc:
            receipt['result']='uncertain' if write_attempted else 'refused'
            receipt['reason']=exc.code if isinstance(exc,RelayError) else 'transport_contract_or_persistence_failure'
        receipt.update(phase=phase,duration_ms=round((time.monotonic()-started)*1000,3),acknowledged_count=len(queued))
        try:
            self.store.append_event(receipt)
            checkpoint=self.store.state()
        except Exception:
            receipt['result']='uncertain' if write_attempted else 'refused'
            receipt['reason']='local_persistence_uncertain';checkpoint=None
        return {'receipt':receipt,'items':queued,'checkpoint':checkpoint}

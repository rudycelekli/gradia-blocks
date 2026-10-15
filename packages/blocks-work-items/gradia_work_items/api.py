"""Independent authenticated work-item reference. No default server credentials."""
import hashlib
import hmac
import json
import sqlite3
import time
import uuid
from collections import defaultdict,deque
from datetime import datetime,timezone
from threading import Lock
from typing import Annotated
from fastapi import FastAPI,Depends,Header,Query,Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from .contracts import Principal,Settings,Submission,Mutation,Acknowledgment,Item,ChangeFeedPage
from .store import Store,record

VERSION='0.2.0'
MAX_BODY=49152


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True)


class BlockError(Exception):
    def __init__(self,status,code):self.status,self.code=status,code


def fail(status,code):raise BlockError(status,code)


class Guard:
    def __init__(self,app,attempts,mutex,config_digest):
        self.app,self.attempts,self.mutex,self.config_digest=app,attempts,mutex,config_digest

    async def __call__(self,scope,receive,send):
        if scope['type']!='http':return await self.app(scope,receive,send)
        started=time.monotonic();correlation=str(uuid.uuid4());scope['work_items.correlation']=correlation
        status=500;chunks=[];size=0
        async def observed_send(message):
            nonlocal status
            if message['type']=='http.response.start':
                status=message['status'];headers=list(message.get('headers',[]))
                headers.extend([(b'x-gradia-correlation-id',correlation.encode()),(b'x-gradia-cost-status',b'unknown')])
                principal=scope.get('work_items.principal')
                if principal:
                    fingerprint=hashlib.sha256(canonical(principal.model_dump(mode='json')).encode()).hexdigest()
                    headers.append((b'x-gradia-principal-sha256',fingerprint.encode()))
                message={**message,'headers':headers}
            await send(message)
        try:
            while True:
                message=await receive()
                if message['type']=='http.disconnect':return
                size+=len(message.get('body',b''))
                if size>MAX_BODY:
                    return await JSONResponse({'error':{'code':'payload_too_large'}},status_code=413)(scope,receive,observed_send)
                chunks.append(message.get('body',b''))
                if not message.get('more_body',False):break
            body=b''.join(chunks)
            if body and scope['method'] in ('POST','PATCH'):
                def pairs(values):
                    result={}
                    for key,value in values:
                        if key in result:raise ValueError('duplicate')
                        result[key]=value
                    return result
                try:
                    value=json.loads(body,object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError('nonfinite')))
                    stack=[(value,0)];count=0
                    while stack:
                        item,depth=stack.pop();count+=1
                        if depth>16 or count>1000:raise ValueError('limit')
                        if type(item) is dict:stack.extend((v,depth+1) for v in item.values())
                        elif type(item) is list:stack.extend((v,depth+1) for v in item)
                except (ValueError,UnicodeError,RecursionError):
                    return await JSONResponse({'error':{'code':'invalid_request'}},status_code=422)(scope,receive,observed_send)
            delivered=False
            async def bounded_receive():
                nonlocal delivered
                if not delivered:
                    delivered=True;return {'type':'http.request','body':body,'more_body':False}
                return await receive()
            await self.app(scope,bounded_receive,observed_send)
        finally:
            actor=scope.get('work_items.principal');tenant=actor.tenant_id if actor else '__unauthenticated__'
            event={'schema':'gradia.work-items.attempt.v1','event_id':str(uuid.uuid4()),'correlation_id':correlation,
                   'block_id':'gradia.work-items','edition':VERSION,'configuration_digest':self.config_digest,
                   'tenant_id':actor.tenant_id if actor else None,'actor_id':actor.actor_id if actor else None,
                   'operation':scope.get('work_items.operation','request'),'status':status,
                   'duration_ms':round((time.monotonic()-started)*1000,3),'occurred_at':datetime.now(timezone.utc).isoformat(),
                   'cost':{'status':'unknown','amount':None},'coverage':'last_100_attempts_per_tenant_process_local'}
            with self.mutex:self.attempts[tenant].append(event)


def create_app(database_path,api_keys,settings=None):
    if type(api_keys) is not dict or not api_keys or any(type(k) is not str or len(k)<24 for k in api_keys):
        raise ValueError('Configure host-owned bearer credentials of 24+ characters')
    principals=[(key,Principal.model_validate(p)) for key,p in api_keys.items()]
    settings=Settings.model_validate(settings or {})
    config_digest=hashlib.sha256(canonical(settings.model_dump(mode='json')).encode()).hexdigest()
    workflow={key:settings.model_dump(mode='json')[key] for key in ('states','initial_status','transitions')}
    workflow_digest=hashlib.sha256(canonical(workflow).encode()).hexdigest()
    store=Store(database_path,workflow_digest);attempts=defaultdict(lambda:deque(maxlen=100));mutex=Lock()
    app=FastAPI(title='Gradia Work Items reference',version=VERSION);app.state.store=store
    app.add_middleware(Guard,attempts=attempts,mutex=mutex,config_digest=config_digest)

    @app.exception_handler(BlockError)
    async def block_error(request,exc):return JSONResponse({'error':{'code':exc.code}},status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request,exc):return JSONResponse({'error':{'code':'invalid_request'}},status_code=422)

    @app.exception_handler(sqlite3.Error)
    async def database_error(request,exc):return JSONResponse({'error':{'code':'persistence_uncertain','recovery':'reconcile_before_retry'}},status_code=503)

    def actor(request:Request,authorization:Annotated[str|None,Header()]=None,if_gradia_principal:Annotated[str|None,Header(pattern=r'^[0-9a-f]{64}$')]=None,if_gradia_configuration:Annotated[str|None,Header(pattern=r'^[0-9a-f]{64}$')]=None,if_gradia_block_version:Annotated[str|None,Header(min_length=1,max_length=40)]=None):
        protected={b'authorization',b'if-gradia-principal',b'if-gradia-configuration',b'if-gradia-block-version',b'idempotency-key'}
        names=[name.lower() for name,_ in request.scope['headers']]
        if any(names.count(name)>1 for name in protected):fail(400,'duplicate_authority_header')
        if authorization is None or not authorization.startswith('Bearer '):fail(401,'unauthorized')
        token=authorization[7:];found=None
        for key,principal in principals:
            if hmac.compare_digest(token.encode(),key.encode()):found=principal
        if found is None:fail(401,'unauthorized')
        request.scope['work_items.principal']=found
        expected=if_gradia_principal
        actual=hashlib.sha256(canonical(found.model_dump(mode='json')).encode()).hexdigest()
        if expected is not None and expected!=actual:fail(409,'principal_changed')
        expected_config=if_gradia_configuration
        if expected_config is not None and expected_config!=config_digest:fail(409,'configuration_changed')
        if if_gradia_block_version is not None and if_gradia_block_version!=VERSION:fail(409,'block_version_changed')
        return found

    def operator(principal:Principal=Depends(actor)):
        if principal.role!='operator':fail(403,'forbidden')
        return principal

    def fetch(conn,principal,identity):
        row=conn.execute('SELECT * FROM work_items WHERE tenant=? AND id=?',(principal.tenant_id,identity)).fetchone()
        if row is None or (principal.role!='operator' and row['actor']!=principal.actor_id):fail(404,'not_found')
        return row

    def event(conn,principal,identity,revision,operation,request,started):
        payload={'schema':'gradia.work-items.event.v1','schema_version':'1','event_id':str(uuid.uuid4()),'block_id':'gradia.work-items','block_version':VERSION,'edition':VERSION,
                 'tenant_id':principal.tenant_id,'actor_id':principal.actor_id,'item_id':identity,'revision':revision,
                 'operation':operation,'result':'persisted','correlation_id':request.scope['work_items.correlation'],
                 'configuration_digest':config_digest,'duration_ms':round((time.monotonic()-started)*1000,3),
                 'occurred_at':datetime.now(timezone.utc).isoformat(),'privacy':'metadata_only',
                 'acceptance':'not_evaluated','billability':'not_evaluated','actor_type':'configured_principal','human_involvement':'unknown',
                 'cost':{'status':'unknown','amount':None,'currency':None},'coverage':'committed_mutations_only'}
        conn.execute('INSERT INTO work_item_events(tenant,item_id,payload) VALUES(?,?,?)',(principal.tenant_id,identity,canonical(payload)))

    @app.get('/v1/work-items/session')
    def session(request:Request,principal:Principal=Depends(actor)):
        request.scope['work_items.operation']='session.read'
        with store.connection() as conn:
            journal=conn.execute("SELECT value FROM work_item_metadata WHERE key='journal_id'").fetchone()[0]
        return {'version':VERSION,'tenant_id':principal.tenant_id,'actor_id':principal.actor_id,'role':principal.role,'configuration_digest':config_digest,'journal_id':journal}

    @app.get('/v1/work-items/config')
    def config(request:Request,principal:Principal=Depends(actor)):
        request.scope['work_items.operation']='config.read'
        return {'version':VERSION,'configuration_digest':config_digest,'settings':settings.model_dump(mode='json')}

    @app.get('/v1/work-items/diagnostics/attempts')
    def diagnostics(principal:Principal=Depends(operator)):
        with mutex:events=list(attempts[principal.tenant_id])
        return {'items':events,'coverage':'last_100_attempts_process_local','eviction':'oldest_first','persistence':False}

    @app.get('/v1/work-items/by-source/feedback/{source_id}',response_model=Item)
    def by_source(source_id:str,request:Request,principal:Principal=Depends(actor)):
        request.scope['work_items.operation']='source.lookup'
        if principal.role!='bridge':fail(403,'forbidden')
        with store.connection() as conn:
            row=conn.execute('SELECT * FROM work_items WHERE tenant=? AND actor=? AND source_kind=? AND source_id=?',(principal.tenant_id,principal.actor_id,'feedback',source_id)).fetchone()
            if row is None:fail(404,'not_found')
            return record(row)

    @app.post('/v1/work-items',response_model=Acknowledgment)
    def create(body:Submission,request:Request,idempotency_key:Annotated[str,Header(min_length=1,max_length=128)],principal:Principal=Depends(actor)):
        request.scope['work_items.operation']='item.create';started=time.monotonic()
        if not settings.enabled:fail(403,'feature_disabled')
        if body.source is not None and principal.role!='bridge':fail(403,'source_binding_forbidden')
        digest=hashlib.sha256(canonical(body.model_dump(mode='json')).encode()).hexdigest()
        with store.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            old=conn.execute('SELECT * FROM work_item_keys WHERE tenant=? AND actor=? AND key=?',(principal.tenant_id,principal.actor_id,idempotency_key)).fetchone()
            if old:
                if old['digest']!=digest:fail(409,'idempotency_conflict')
                response=json.loads(old['response']);response['replayed']=True;code=200
            else:
                bound=None
                if body.source:
                    bound=conn.execute('SELECT * FROM work_items WHERE tenant=? AND actor=? AND source_kind=? AND source_id=?',(principal.tenant_id,principal.actor_id,body.source.kind,body.source.id)).fetchone()
                if bound:
                    if bound['create_digest']!=digest:fail(409,'source_binding_conflict')
                    response=json.loads(bound['create_response']);response['replayed']=True;code=200
                else:
                    identity=str(uuid.uuid4());now=datetime.now(timezone.utc).isoformat();source=body.source.model_dump(mode='json') if body.source else None
                    item={'id':identity,'revision':1,'status':settings.initial_status,'title':body.title,'description':body.description,'assignee':None,'source':source,'created_at':now,'updated_at':now}
                    response={'status':'acknowledged','item':item,'replayed':False};code=201
                    conn.execute('INSERT INTO work_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(principal.tenant_id,principal.actor_id,identity,1,settings.initial_status,body.title,body.description,None,canonical(source) if source else None,source['kind'] if source else None,source['id'] if source else None,digest,canonical(response),now,now))
                    event(conn,principal,identity,1,'item.proposed',request,started)
                conn.execute('INSERT INTO work_item_keys VALUES(?,?,?,?,?)',(principal.tenant_id,principal.actor_id,idempotency_key,digest,canonical(response)))
        return JSONResponse(response,status_code=code)

    @app.get('/v1/work-items')
    def listing(request:Request,limit:int=Query(default=25,ge=1,le=100),cursor:str=Query(default='',max_length=36),status:str|None=Query(default=None,min_length=1,max_length=100),assignee:str|None=Query(default=None,pattern=r'^[a-zA-Z0-9][a-zA-Z0-9._-]{0,99}$'),principal:Principal=Depends(actor)):
        request.scope['work_items.operation']='item.list'
        if any(key not in ('limit','cursor','status','assignee') or len(request.query_params.getlist(key))!=1 for key in request.query_params):fail(400,'unsupported_or_duplicate_parameter')
        if status is not None and status not in settings.states:fail(422,'invalid_state')
        extra='' if principal.role=='operator' else ' AND actor=?';params=[principal.tenant_id,cursor]
        if extra:params.append(principal.actor_id)
        if status is not None:extra+=' AND status=?';params.append(status)
        if assignee is not None:extra+=' AND assignee=?';params.append(assignee)
        with store.connection() as conn:
            rows=conn.execute('SELECT * FROM work_items WHERE tenant=? AND id>?'+extra+' ORDER BY id LIMIT ?',(*params,limit+1)).fetchall()
            return {'items':[record(row) for row in rows[:limit]],'next_cursor':rows[limit-1]['id'] if len(rows)>limit else None}

    @app.get('/v1/work-items/changes',response_model=ChangeFeedPage)
    def changes(request:Request,after_sequence:int=Query(default=0,ge=0,le=9007199254740991),limit:int=Query(default=100,ge=1,le=100),expected_journal_id:str|None=Query(default=None,pattern=r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'),after_event_id:str|None=Query(default=None,pattern=r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'),after_event_sha256:str|None=Query(default=None,pattern=r'^[0-9a-f]{64}$'),principal:Principal=Depends(operator)):
        request.scope['work_items.operation']='changes.read'
        allowed=('after_sequence','limit','expected_journal_id','after_event_id','after_event_sha256')
        if any(key not in allowed or len(request.query_params.getlist(key))!=1 for key in request.query_params):fail(400,'unsupported_or_duplicate_parameter')
        if after_sequence==0 and (after_event_id is not None or after_event_sha256 is not None):fail(422,'invalid_cursor')
        if after_sequence>0 and any(value is None for value in (expected_journal_id,after_event_id,after_event_sha256)):fail(422,'invalid_cursor')
        with store.connection() as conn:
            # Explicit deferred transaction binds identity, anchor, head and rows to one snapshot.
            conn.execute('BEGIN')
            journal=conn.execute("SELECT value FROM work_item_metadata WHERE key='journal_id'").fetchone()[0]
            if expected_journal_id is not None and expected_journal_id!=journal:fail(409,'cursor_invalid')
            if after_sequence:
                anchor=conn.execute('SELECT payload FROM work_item_events WHERE tenant=? AND sequence=?',(principal.tenant_id,after_sequence)).fetchone()
                if anchor is None:fail(409,'cursor_invalid')
                event=json.loads(anchor['payload'])
                if event.get('event_id')!=after_event_id or hashlib.sha256(canonical(event).encode()).hexdigest()!=after_event_sha256:fail(409,'cursor_invalid')
            head=conn.execute('SELECT COALESCE(MAX(sequence),0) FROM work_item_events WHERE tenant=?',(principal.tenant_id,)).fetchone()[0]
            rows=conn.execute('SELECT sequence,payload FROM work_item_events WHERE tenant=? AND sequence>? ORDER BY sequence LIMIT ?',(principal.tenant_id,after_sequence,limit)).fetchall()
            return {'schema':'gradia.work-items.changefeed.v1','journal_id':journal,'tenant_id':principal.tenant_id,'head_sequence':head,'items':[{'sequence':row['sequence'],'event':json.loads(row['payload'])} for row in rows],'next_after':rows[-1]['sequence'] if rows else after_sequence}

    @app.get('/v1/work-items/{identity}',response_model=Item)
    def get_one(identity:str,request:Request,principal:Principal=Depends(actor)):
        request.scope['work_items.operation']='item.read'
        with store.connection() as conn:return record(fetch(conn,principal,identity))

    @app.patch('/v1/work-items/{identity}',response_model=Acknowledgment)
    def update(identity:str,body:Mutation,request:Request,principal:Principal=Depends(operator)):
        request.scope['work_items.operation']='item.update';started=time.monotonic()
        if not settings.enabled:fail(403,'feature_disabled')
        changes=body.model_dump(exclude_unset=True);changes.pop('expected_revision')
        if not changes:fail(422,'empty_mutation')
        if 'status' in changes and (body.status is None or body.status not in settings.states):fail(422,'invalid_state')
        if 'status' in changes and not settings.transitions_enabled:fail(403,'feature_disabled')
        if 'assignee' in changes and not settings.assignments:fail(403,'feature_disabled')
        with store.connection() as conn:
            conn.execute('BEGIN IMMEDIATE');row=fetch(conn,principal,identity)
            if row['revision']!=body.expected_revision:fail(409,'revision_conflict')
            if row['revision']>=2147483647:fail(409,'revision_exhausted')
            status=changes.get('status',row['status']);assignee=changes.get('assignee',row['assignee'])
            if status!=row['status'] and (row['status'],status) not in {(t.from_state,t.to_state) for t in settings.transitions}:fail(422,'transition_forbidden')
            if status==row['status'] and assignee==row['assignee']:fail(422,'no_change')
            revision=row['revision']+1;now=datetime.now(timezone.utc).isoformat()
            conn.execute('UPDATE work_items SET revision=?,status=?,assignee=?,updated_at=? WHERE tenant=? AND id=?',(revision,status,assignee,now,principal.tenant_id,identity))
            event(conn,principal,identity,revision,'item.updated',request,started);item=record(fetch(conn,principal,identity))
        return {'status':'acknowledged','item':item,'replayed':False}

    @app.get('/v1/work-items/{identity}/events')
    def events(identity:str,request:Request,after:int=Query(default=0,ge=0),limit:int=Query(default=100,ge=1,le=100),principal:Principal=Depends(operator)):
        request.scope['work_items.operation']='events.read'
        with store.connection() as conn:
            fetch(conn,principal,identity)
            rows=conn.execute('SELECT sequence,payload FROM work_item_events WHERE tenant=? AND item_id=? AND sequence>? ORDER BY sequence LIMIT ?',(principal.tenant_id,identity,after,limit+1)).fetchall()
            return {'items':[{'sequence':row['sequence'],**json.loads(row['payload'])} for row in rows[:limit]],'next_after':rows[limit-1]['sequence'] if len(rows)>limit else None}
    return app

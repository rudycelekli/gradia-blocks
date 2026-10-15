"""Host-authenticated relay facade; one operator request runs one bounded page."""
import hashlib
import hmac
import json
import sqlite3
import time
import uuid
from collections import defaultdict,deque
from dataclasses import asdict
from datetime import datetime,timezone
from threading import Lock
from typing import Annotated
from fastapi import FastAPI,Depends,Header,Query,Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel,ConfigDict,Field
from .contracts import VERSION,Principal,RelayError,token,sha,RelaySession,RelayStatus,RunResult
from .relay import Relay
MAX_BODY=4096
def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False)
def fail(status,code):raise RelayError(code,status)
class Guard:
    def __init__(self,app,attempts,mutex,config_digest):
        self.app,self.attempts,self.mutex,self.config_digest=app,attempts,mutex,config_digest

    async def __call__(self,scope,receive,send):
        if scope['type']!='http':return await self.app(scope,receive,send)
        started=time.monotonic();correlation=str(uuid.uuid4());scope['event_relay.correlation']=correlation
        status=500;chunks=[];size=0
        async def observed_send(message):
            nonlocal status
            if message['type']=='http.response.start':
                status=message['status'];headers=list(message.get('headers',[]))
                headers.extend([(b'x-gradia-correlation-id',correlation.encode()),(b'x-gradia-cost-status',b'unknown')])
                principal=scope.get('event_relay.principal')
                if principal:
                    fingerprint=hashlib.sha256(canonical(asdict(principal)).encode()).hexdigest()
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
            actor=scope.get('event_relay.principal');tenant=actor.tenant_id if actor else '__unauthenticated__'
            event={'schema':'gradia.event-relay.attempt.v1','event_id':str(uuid.uuid4()),'correlation_id':correlation,
                   'block_id':'gradia.event-relay','edition':VERSION,'configuration_digest':self.config_digest,
                   'tenant_id':actor.tenant_id if actor else None,'actor_id':actor.actor_id if actor else None,
                   'operation':scope.get('event_relay.operation','request'),'status':status,
                   'duration_ms':round((time.monotonic()-started)*1000,3),'occurred_at':datetime.now(timezone.utc).isoformat(),
                   'cost':{'status':'unknown','amount':None},'coverage':'last_100_attempts_per_tenant_process_local'}
            with self.mutex:self.attempts[tenant].append(event)


class Run(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    limit:int=Field(default=25,ge=1,le=25)

def create_app(relay,api_keys):
    if not isinstance(relay,Relay):raise ValueError('Host-owned Relay required')
    if type(api_keys) is not dict or not 1<=len(api_keys)<=256:raise ValueError('Bounded credentials mapping required')
    principals=[]
    for key,value in api_keys.items():
        token(key)
        if len(key)>256:raise ValueError('API credential too long')
        principal=Principal(**value) if type(value) is dict else value
        if not isinstance(principal,Principal) or principal.tenant_id!=relay.tenant_id:raise ValueError('Relay tenant operator required')
        principals.append((key,principal))
    config_digest=relay.config_digest;attempts=defaultdict(lambda:deque(maxlen=100));mutex=Lock()
    app=FastAPI(title='Gradia Event Relay reference',version=VERSION)
    app.state.relay=relay
    app.add_middleware(Guard,attempts=attempts,mutex=mutex,config_digest=config_digest)
    @app.exception_handler(RelayError)
    async def relay_error(request,exc):return JSONResponse({'error':{'code':exc.code}},status_code=exc.status_code)
    @app.exception_handler(RequestValidationError)
    async def validation_error(request,exc):return JSONResponse({'error':{'code':'invalid_request'}},status_code=422)
    @app.exception_handler(sqlite3.Error)
    async def persistence_error(request,exc):return JSONResponse({'error':{'code':'persistence_uncertain'}},status_code=503)
    def actor(request:Request,authorization:Annotated[str|None,Header()]=None,
              if_gradia_principal:Annotated[str|None,Header(pattern=r'^[0-9a-f]{64}$')]=None,
              if_gradia_configuration:Annotated[str|None,Header(pattern=r'^[0-9a-f]{64}$')]=None,
              if_gradia_block_version:Annotated[str|None,Header(max_length=40)]=None):
        if any(len(request.headers.getlist(name))>1 for name in ('authorization','if-gradia-principal','if-gradia-configuration','if-gradia-block-version')):fail(400,'duplicate_authority_header')
        if authorization is None or not authorization.startswith('Bearer ') or not authorization.isascii():fail(401,'unauthorized')
        found=None
        for key,principal in principals:
            if hmac.compare_digest(key,authorization[7:]):found=principal
        if found is None:fail(401,'unauthorized')
        request.scope['event_relay.principal']=found
        if if_gradia_principal is not None and if_gradia_principal!=sha(asdict(found)):fail(409,'principal_changed')
        if if_gradia_configuration is not None and if_gradia_configuration!=config_digest:fail(409,'configuration_changed')
        if if_gradia_block_version is not None and if_gradia_block_version!=VERSION:fail(409,'block_version_changed')
        if request.query_params and request.url.path not in ('/v1/event-relay/events',):fail(400,'unsupported_parameter')
        return found
    @app.get('/v1/event-relay/session',response_model=RelaySession,response_model_exclude_unset=True)
    def session(request:Request,principal:Principal=Depends(actor)):
        request.scope['event_relay.operation']='session.read'
        return {'version':VERSION,**asdict(principal),'configuration_digest':config_digest}
    @app.post('/v1/event-relay/run',response_model=RunResult,response_model_exclude_unset=True)
    def run(body:Run,request:Request,principal:Principal=Depends(actor)):
        request.scope['event_relay.operation']='relay.run'
        return relay.run(body.limit,correlation_id=request.scope['event_relay.correlation'],actor_id=principal.actor_id)
    @app.get('/v1/event-relay/status',response_model=RelayStatus,response_model_exclude_unset=True)
    def status(request:Request,principal:Principal=Depends(actor)):
        request.scope['event_relay.operation']='status.read'
        return relay.status()
    @app.get('/v1/event-relay/events')
    def events(request:Request,limit:int=Query(default=100,ge=1,le=100),principal:Principal=Depends(actor)):
        request.scope['event_relay.operation']='events.read'
        if any(key!='limit' or len(request.query_params.getlist(key))!=1 for key in request.query_params):fail(400,'unsupported_or_duplicate_parameter')
        return {'items':relay.events(limit),'coverage':'last_100_durable_events'}
    @app.get('/v1/event-relay/diagnostics/attempts')
    def diagnostics(request:Request,principal:Principal=Depends(actor)):
        request.scope['event_relay.operation']='attempts.read'
        with mutex:items=list(attempts[principal.tenant_id])
        return {'items':items,'upstream_items':list(relay.attempts),'coverage':'last_100_attempts_process_local','persistence':False}
    return app

"""Host-authenticated HTTP facade; no sending occurs at startup."""
import hashlib
import hmac
import json
import sqlite3
import time
import uuid
from collections import defaultdict, deque
from dataclasses import asdict
from datetime import datetime, timezone
from threading import Lock
from typing import Annotated, Any
from fastapi import FastAPI, Depends, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from .contracts import Principal, Destination, OutboxError, canonical_json
from .delivery import Engine

VERSION = '0.1.0'
MAX_BODY = 49152

def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)

def fail(status, code):
    raise OutboxError(status, code)

class Guard:
    def __init__(self,app,attempts,mutex,config_digest):
        self.app,self.attempts,self.mutex,self.config_digest=app,attempts,mutex,config_digest

    async def __call__(self,scope,receive,send):
        if scope['type']!='http':return await self.app(scope,receive,send)
        started=time.monotonic();correlation=str(uuid.uuid4());scope['outbox.correlation']=correlation
        status=500;chunks=[];size=0
        async def observed_send(message):
            nonlocal status
            if message['type']=='http.response.start':
                status=message['status'];headers=list(message.get('headers',[]))
                headers.extend([(b'x-gradia-correlation-id',correlation.encode()),(b'x-gradia-cost-status',b'unknown')])
                principal=scope.get('outbox.principal')
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
            actor=scope.get('outbox.principal');tenant=actor.tenant_id if actor else '__unauthenticated__'
            event={'schema':'gradia.outbox.attempt.v1','event_id':str(uuid.uuid4()),'correlation_id':correlation,
                   'block_id':'gradia.outbox','edition':VERSION,'configuration_digest':self.config_digest,
                   'tenant_id':actor.tenant_id if actor else None,'actor_id':actor.actor_id if actor else None,
                   'operation':scope.get('outbox.operation','request'),'status':status,
                   'duration_ms':round((time.monotonic()-started)*1000,3),'occurred_at':datetime.now(timezone.utc).isoformat(),
                   'cost':{'status':'unknown','amount':None},'coverage':'last_100_attempts_per_tenant_process_local'}
            with self.mutex:self.attempts[tenant].append(event)


class Enqueue(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    destination_id: str = Field(pattern=r'^[a-zA-Z0-9][a-zA-Z0-9._-]{0,99}$')
    event_type: str = Field(pattern=r'^[a-zA-Z0-9][a-zA-Z0-9._-]{0,99}$')
    payload: dict[str, Any]

class Revision(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    expected_revision: int = Field(ge=1, le=2147483647)

class OutboxItem(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    id: str
    revision: int
    status: str
    destination_id: str
    event_type: str
    payload_sha256: str
    created_at: str
    updated_at: str
    attempt_id: str | None

class EnqueueAcknowledgment(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    item: OutboxItem
    replayed: bool


def create_app(database_path, api_keys, destinations):
    if (type(api_keys) is not dict or not 1 <= len(api_keys) <= 256 or
        any(type(k) is not str or not 24 <= len(k) <= 256 or not k.isascii() or not k.isprintable() for k in api_keys)):
        raise ValueError('Configure bounded host-owned ASCII bearer credentials of 24+ characters')
    principals = []
    for key, value in api_keys.items():
        if type(value) is dict:
            value = Principal(**value)
        if not isinstance(value, Principal):
            raise ValueError('Configured Principal contract required')
        principals.append((key, value))
    if type(destinations) is not dict:
        raise ValueError('Configured destination mapping required')
    destinations = {key: Destination(**value) if type(value) is dict else value for key, value in destinations.items()}
    engine = Engine(database_path, destinations)
    config_digest = engine.config_digest
    attempts = defaultdict(lambda: deque(maxlen=100))
    mutex = Lock()
    app = FastAPI(title='Gradia Outbox reference', version=VERSION)
    app.state.engine = engine
    app.add_middleware(Guard, attempts=attempts, mutex=mutex, config_digest=config_digest)

    @app.exception_handler(OutboxError)
    async def block_error(request, exc):
        return JSONResponse({'error': {'code': exc.code}}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({'error': {'code': 'invalid_request'}}, status_code=422)

    @app.exception_handler(sqlite3.Error)
    async def database_error(request, exc):
        return JSONResponse({'error': {'code': 'persistence_uncertain', 'recovery': 'reconcile_before_retry'}}, status_code=503)

    def actor(request: Request, authorization: Annotated[str | None, Header()] = None,
              if_gradia_principal: Annotated[str | None, Header(pattern=r'^[0-9a-f]{64}$')] = None,
              if_gradia_configuration: Annotated[str | None, Header(pattern=r'^[0-9a-f]{64}$')] = None,
              if_gradia_block_version: Annotated[str | None, Header(max_length=40)] = None):
        for name in ('authorization', 'if-gradia-principal', 'if-gradia-configuration', 'if-gradia-block-version'):
            if len(request.headers.getlist(name)) > 1:
                fail(400, 'duplicate_authority_header')
        if authorization is None or not authorization.startswith('Bearer ') or not authorization.isascii():
            fail(401, 'unauthorized')
        found = None
        for key, principal in principals:
            if hmac.compare_digest(authorization[7:], key):
                found = principal
        if found is None:
            fail(401, 'unauthorized')
        request.scope['outbox.principal'] = found
        fingerprint = hashlib.sha256(canonical(asdict(found)).encode()).hexdigest()
        if if_gradia_principal is not None and if_gradia_principal != fingerprint:
            fail(409, 'principal_changed')
        if if_gradia_configuration is not None and if_gradia_configuration != config_digest:
            fail(409, 'configuration_changed')
        if if_gradia_block_version is not None and if_gradia_block_version != VERSION:
            fail(409, 'block_version_changed')
        return found

    def operator(principal: Principal = Depends(actor)):
        if principal.role != 'operator':
            fail(403, 'forbidden')
        return principal

    def operation(request, name):
        request.scope['outbox.operation'] = name

    @app.get('/v1/outbox/session')
    def session(request: Request, principal: Principal = Depends(actor)):
        operation(request, 'session.read')
        return {'version': VERSION, **asdict(principal), 'configuration_digest': config_digest}

    @app.get('/v1/outbox/destinations')
    def configured_destinations(request: Request, principal: Principal = Depends(actor)):
        operation(request, 'destinations.read')
        return {'items': [{'id': key, 'receiver_id': value.receiver_id} for key, value in engine.destinations.items() if value.tenant_id == principal.tenant_id], 'configuration_digest': config_digest}

    @app.get('/v1/outbox/diagnostics/attempts')
    def diagnostics(principal: Principal = Depends(operator)):
        with mutex:
            events = list(attempts[principal.tenant_id])
        return {'items': events, 'coverage': 'last_100_attempts_process_local', 'persistence': False}

    @app.post('/v1/outbox', response_model=EnqueueAcknowledgment, responses={201: {'model': EnqueueAcknowledgment}})
    def enqueue(body: Enqueue, request: Request,
                idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
                principal: Principal = Depends(actor)):
        operation(request, 'outbox.enqueue')
        if len(request.headers.getlist('idempotency-key')) != 1:
            fail(400, 'duplicate_idempotency_header')
        result = engine.enqueue(principal, body.destination_id, body.event_type, body.payload, idempotency_key, correlation_id=request.scope['outbox.correlation'])
        return JSONResponse(result, status_code=200 if result['replayed'] else 201)

    @app.get('/v1/outbox')
    def listing(request: Request, limit: int = Query(default=50, ge=1, le=100), principal: Principal = Depends(actor)):
        operation(request, 'outbox.list')
        if any(k != 'limit' or len(request.query_params.getlist(k)) != 1 for k in request.query_params):
            fail(400, 'unsupported_or_duplicate_parameter')
        return {'items': engine.list(principal, limit=limit), 'coverage': 'bounded_snapshot_no_pagination'}

    @app.get('/v1/outbox/{identity}', response_model=OutboxItem)
    def get_one(identity: str, request: Request, principal: Principal = Depends(actor)):
        operation(request, 'outbox.read')
        return engine.get(principal, identity)

    @app.get('/v1/outbox/{identity}/events')
    def events(identity: str, request: Request, principal: Principal = Depends(operator)):
        operation(request, 'events.read')
        return {'items': engine.events(principal, identity)}

    @app.post('/v1/outbox/{identity}/deliver', response_model=OutboxItem)
    def deliver(identity: str, body: Revision, request: Request, principal: Principal = Depends(operator)):
        operation(request, 'outbox.deliver')
        return engine.deliver(principal, identity, body.expected_revision, correlation_id=request.scope['outbox.correlation'])

    @app.post('/v1/outbox/{identity}/reconcile', response_model=OutboxItem)
    def reconcile(identity: str, body: Revision, request: Request, principal: Principal = Depends(operator)):
        operation(request, 'outbox.reconcile')
        return engine.reconcile(principal, identity, body.expected_revision, correlation_id=request.scope['outbox.correlation'])

    @app.post('/v1/outbox/{identity}/mark-uncertain', response_model=OutboxItem)
    def mark_uncertain(identity: str, body: Revision, request: Request, principal: Principal = Depends(operator)):
        operation(request, 'outbox.mark_uncertain')
        return engine.mark_uncertain(principal, identity, body.expected_revision, correlation_id=request.scope['outbox.correlation'])

    return app

"""Strict metadata-only relay contracts. No authorization travels in event payloads."""
from dataclasses import dataclass
from datetime import datetime
import hashlib
import ipaddress
import json
import math
import re
from urllib.parse import urlsplit
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

VERSION='0.1.0'
MAX_INT=9007199254740991
NAME=r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'
UUID=r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
SHA=r'^[0-9a-f]{64}$'
class RelayError(Exception):
    def __init__(self,code,status_code=409): self.code,self.status_code=code,status_code;super().__init__(code)
def identifier(value):
    if type(value) is not str or not re.fullmatch(NAME,value):raise ValueError('Invalid bounded identifier')
    return value

def bounded(value,depth=0):
    if depth>16:raise ValueError('JSON too deep')
    if value is None or type(value) in (str,bool):return
    if type(value) is int:
        if abs(value)>MAX_INT:raise ValueError('JSON integer exceeds range')
        return
    if type(value) is float:
        if not math.isfinite(value):raise ValueError('Nonfinite JSON')
        return
    if type(value) is list:
        for child in value:bounded(child,depth+1)
        return
    if type(value) is dict:
        for key,child in value.items():
            if type(key) is not str:raise ValueError('Invalid JSON key')
            bounded(child,depth+1)
        return
    raise ValueError('Unsupported JSON')
def canonical(value):
    bounded(value)
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode()
def sha(value):return hashlib.sha256(canonical(value)).hexdigest()
def parse_json(data):
    def pairs(values):
        result={}
        for key,value in values:
            if key in result:raise ValueError('Duplicate JSON key')
            result[key]=value
        return result
    value=json.loads(data,object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError('Nonfinite JSON')))
    bounded(value);return value

def origin(value):
    if type(value) is not str or len(value)>500 or not value.isascii() or any(ord(c)<=32 or ord(c)==127 for c in value):raise ValueError('Invalid origin')
    parsed=urlsplit(value)
    try:
        parsed.port
        if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname or parsed.path not in ('','/'):raise ValueError('Invalid origin')
        if parsed.scheme!='https':
            if parsed.scheme!='http' or not ipaddress.ip_address(parsed.hostname).is_loopback:raise ValueError('HTTPS required except literal loopback')
    except (ValueError,TypeError):raise ValueError('Invalid origin') from None
    return value.rstrip('/')
def token(value):
    if type(value) is not str or not 24<=len(value)<=4096 or not value.isascii() or any(ord(c)<33 or ord(c)>126 for c in value):raise ValueError('Private ASCII credential required')
    return value

def timestamp(value):
    try:
        if type(value) is not str or len(value)>64 or datetime.fromisoformat(value.replace('Z','+00:00')).tzinfo is None:raise ValueError()
    except (ValueError,TypeError):raise RelayError('invalid_timestamp') from None
    return value

@dataclass(frozen=True)
class Principal:
    tenant_id:str
    actor_id:str
    role:str='operator'
    def __post_init__(self):
        identifier(self.tenant_id);identifier(self.actor_id)
        if self.role!='operator':raise ValueError('Operator role required')
class Strict(BaseModel):
    model_config=ConfigDict(strict=True,extra='forbid',frozen=True)
class Session(Strict):
    version:str=Field(max_length=20)
    tenant_id:str=Field(pattern=NAME)
    actor_id:str=Field(pattern=NAME)
    role:str=Field(max_length=20)
    configuration_digest:str=Field(pattern=SHA)
class Anchor(Strict):
    sequence:int=Field(ge=1,le=MAX_INT)
    event_id:str=Field(pattern=UUID)
    event_sha256:str=Field(pattern=SHA)
class OutboxItem(Strict):
    id:str=Field(pattern=UUID)
    revision:int=Field(ge=1,le=MAX_INT)
    status:Literal['queued']
    destination_id:str=Field(pattern=NAME)
    event_type:Literal['work_item.proposed','work_item.updated']
    payload_sha256:str=Field(pattern=SHA)
    created_at:str=Field(max_length=64)
    updated_at:str=Field(max_length=64)
    attempt_id:None
class Acknowledgment(Strict):
    item:OutboxItem
    replayed:bool
class SourceSession(Session):
    journal_id:str=Field(pattern=UUID)
class Cost(Strict):
    status:Literal['unknown']
    amount:None
    currency:None
class ChangeEvent(Strict):
    schema:Literal['gradia.work-items.event.v1']
    schema_version:Literal['1']
    event_id:str=Field(pattern=UUID)
    block_id:Literal['gradia.work-items']
    block_version:Literal['0.1.0','0.2.0']
    edition:Literal['0.1.0','0.2.0']
    tenant_id:str=Field(pattern=NAME)
    actor_id:str=Field(pattern=NAME)
    item_id:str=Field(pattern=UUID)
    revision:int=Field(ge=1,le=2147483647)
    operation:Literal['item.proposed','item.updated']
    result:Literal['persisted']
    correlation_id:str=Field(pattern=UUID)
    configuration_digest:str=Field(pattern=SHA)
    duration_ms:float=Field(ge=0,allow_inf_nan=False)
    occurred_at:str=Field(max_length=64)
    privacy:Literal['metadata_only']
    acceptance:Literal['not_evaluated']
    billability:Literal['not_evaluated']
    actor_type:Literal['configured_principal']
    human_involvement:Literal['unknown']
    cost:Cost
    coverage:Literal['committed_mutations_only']
class Change(Strict):
    sequence:int=Field(ge=1,le=MAX_INT)
    event:ChangeEvent
class Page(Strict):
    schema:Literal['gradia.work-items.changefeed.v1']
    journal_id:str=Field(pattern=UUID)
    tenant_id:str=Field(pattern=NAME)
    head_sequence:int=Field(ge=0,le=MAX_INT)
    items:list[Change]=Field(max_length=25)
    next_after:int=Field(ge=0,le=MAX_INT)
class Binding(Strict):
    principal_sha256:str=Field(pattern=SHA)
    configuration_digest:str=Field(pattern=SHA)
class SourceBinding(Binding):
    journal_id:str=Field(pattern=UUID)
class Bindings(Strict):
    source:SourceBinding
    target:Binding
class Checkpoint(Strict):
    bindings:Bindings
    anchor:Anchor|None
    cursor:int=Field(ge=0,le=MAX_INT)

class RelaySession(Strict):
    version:Literal['0.1.0']
    tenant_id:str=Field(pattern=NAME)
    actor_id:str=Field(pattern=NAME)
    role:Literal['operator']
    configuration_digest:str=Field(pattern=SHA)
class RelayStatus(Strict):
    schema:Literal['gradia.event-relay.status.v1']
    version:Literal['0.1.0']
    tenant_id:str=Field(pattern=NAME)
    profile_id:str=Field(pattern=NAME)
    configuration_digest:str=Field(pattern=SHA)
    checkpoint:Checkpoint|None
    automatic_polling:Literal[False]
    delivery_authorized:Literal[False]
class RunReceipt(Strict):
    schema:Literal['gradia.event-relay.event.v1']
    block_id:Literal['gradia.event-relay']
    edition:Literal['0.1.0']
    event_id:str=Field(pattern=UUID)
    tenant_id:str=Field(pattern=NAME)
    profile_id:str=Field(pattern=NAME)
    occurred_at:str=Field(max_length=64)
    operation:Literal['relay.run']
    configuration_digest:str=Field(pattern=SHA)
    privacy:Literal['metadata_only']
    cost:Cost
    acceptance:Literal['not_evaluated']
    billability:Literal['not_evaluated']
    human_involvement:Literal['unknown']
    correlation_id:str=Field(pattern=NAME)
    actor_id:str|None=Field(pattern=NAME)
    result:Literal['enqueued','idle','refused','uncertain']
    reason:str|None=Field(max_length=100)
    automatic_retry:Literal[False]
    delivery_authorized:Literal[False]
    coverage:Literal['this_invocation_only']
    phase:Literal['validate','sessions','source_page','target_enqueue','checkpoint']
    duration_ms:float=Field(ge=0,allow_inf_nan=False)
    acknowledged_count:int=Field(ge=0,le=25)
    source_read_correlation_id:str|None=Field(default=None,pattern=UUID)
    target_enqueue_correlation_id:str|None=Field(default=None,pattern=UUID)
    source_principal_sha256:str|None=Field(default=None,pattern=SHA)
    target_principal_sha256:str|None=Field(default=None,pattern=SHA)
    journal_id:str|None=Field(default=None,pattern=UUID)
class QueuedTransfer(Strict):
    source_sequence:int=Field(ge=1,le=MAX_INT)
    outbox_item_id:str=Field(pattern=UUID)
    replayed:bool
class RunResult(Strict):
    receipt:RunReceipt
    items:list[QueuedTransfer]=Field(max_length=25)
    checkpoint:Checkpoint|None

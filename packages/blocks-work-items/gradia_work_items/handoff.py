"""Host-configured bounded Feedback → work-item proposal. No polling or automatic retries."""
import hashlib
import json
import re
import time
import uuid
from datetime import datetime,timezone
from urllib.parse import urlsplit
import httpx
from pydantic import ValidationError
from .api import canonical
from .contracts import Item,Principal,Source,Acknowledgment

MAX_RESPONSE=65536


class HandoffError(Exception):
    pass


def _base(value):
    if type(value) is not str or len(value)>500 or any(ord(c)<=32 or ord(c)==127 for c in value):raise ValueError('Invalid host endpoint')
    parsed=urlsplit(value)
    try:parsed.port
    except ValueError:raise ValueError('Invalid host port') from None
    if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('','/'):
        raise ValueError('Configure an origin without credentials, path, query or fragment')
    if parsed.scheme=='http' and parsed.hostname not in ('127.0.0.1','localhost','::1'):
        raise ValueError('Plain HTTP is limited to explicitly configured loopback origins')
    return value.rstrip('/')


class FeedbackHandoff:
    def __init__(self,*,source_origin,target_origin,source_token,target_token,tenant_id,profile_id='feedback-proposal',target_version='0.2.0',source_client=None,target_client=None):
        if type(target_version) is not str or target_version not in ('0.1.0','0.2.0'):raise ValueError('Unsupported target edition')
        self.target_version=target_version
        self.source_origin=_base(source_origin);self.target_origin=_base(target_origin)
        self.tenant=Principal(tenant_id=tenant_id).tenant_id
        if type(profile_id) is not str or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]{0,99}',profile_id):raise ValueError('Invalid profile ID')
        if any(type(v) is not str or not 24<=len(v)<=4096 or not v.isascii() or any(c.isspace() or ord(c)<33 or ord(c)>126 for c in v) for v in (source_token,target_token)):raise ValueError('Configure host-scoped ASCII credentials')
        self.source_token,self.target_token=source_token,target_token;self.profile_id=profile_id
        self.source_client=source_client or httpx.Client(trust_env=False,follow_redirects=False)
        self.target_client=target_client or httpx.Client(trust_env=False,follow_redirects=False)
        self._owned=[client for client,supplied in [(self.source_client,source_client),(self.target_client,target_client)] if supplied is None]

    def close(self):
        for client in self._owned:client.close()

    def __enter__(self):return self
    def __exit__(self,*_):self.close()

    def _request(self,side,method,path,*,fingerprint=None,configuration=None,payload=None,key=None,deadline=None):
        origin,client,token=(self.source_origin,self.source_client,self.source_token) if side=='source' else (self.target_origin,self.target_client,self.target_token)
        headers={'Authorization':'Bearer '+token,'Accept-Encoding':'identity'}
        if fingerprint:headers['If-Gradia-Principal']=fingerprint
        if configuration:headers['If-Gradia-Configuration']=configuration
        if fingerprint:headers['If-Gradia-Block-Version']='0.2.1' if side=='source' else self.target_version
        if key:headers['Idempotency-Key']=key
        remaining=deadline-time.monotonic() if deadline is not None else 3
        if remaining<=0:raise HandoffError('handoff_deadline')
        with client.stream(method,origin+path,headers=headers,json=payload,follow_redirects=False,timeout=min(3,remaining)) as response:
            if response.headers.get('content-encoding','identity')!='identity':raise HandoffError('unsupported_response_encoding')
            if fingerprint and response.headers.get('x-gradia-principal-sha256')!=fingerprint:raise HandoffError('principal_response_mismatch')
            body=bytearray()
            for chunk in response.iter_raw(chunk_size=8192):
                if deadline is not None and time.monotonic()>deadline:raise HandoffError('handoff_deadline')
                body.extend(chunk)
                if len(body)>MAX_RESPONSE:raise HandoffError('response_limit')
            def pairs(items):
                result={}
                for k,v in items:
                    if k in result:raise ValueError('duplicate')
                    result[k]=v
                return result
            try:
                data=json.loads(body.decode(),object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError('nonfinite')))
                pending=[(data,0)];count=0
                while pending:
                    value,depth=pending.pop();count+=1
                    if depth>16 or count>1000:raise ValueError('limit')
                    if type(value) is dict:pending.extend((v,depth+1) for v in value.values())
                    elif type(value) is list:pending.extend((v,depth+1) for v in value)
            except (ValueError,UnicodeError,RecursionError):raise HandoffError('invalid_upstream_json') from None
            return response.status_code,data,response.headers

    def _session(self,side,role,version,deadline):
        path='/v1/feedback/session' if side=='source' else '/v1/work-items/session'
        status,data,headers=self._request(side,'GET',path,deadline=deadline)
        if status!=200 or type(data) is not dict:raise HandoffError('session_refused')
        try:principal=Principal.model_validate({k:data[k] for k in ('tenant_id','actor_id','role')})
        except (KeyError,ValidationError):raise HandoffError('invalid_session') from None
        if principal.tenant_id!=self.tenant or principal.role!=role or data.get('version')!=version:raise HandoffError('scope_role_or_edition_mismatch')
        fingerprint=hashlib.sha256(canonical(principal.model_dump(mode='json')).encode()).hexdigest()
        if headers.get('x-gradia-principal-sha256')!=fingerprint:raise HandoffError('principal_response_mismatch')
        configuration=data.get('configuration_digest')
        if type(configuration) is not str or not re.fullmatch(r'[0-9a-f]{64}',configuration):raise HandoffError('invalid_session')
        return fingerprint,configuration

    def run(self,feedback_id,expected_revision):
        started=time.monotonic();phase='validate';write_attempted=False;item=None
        deadline=started+15
        def request(*args,**kwargs):return self._request(*args,**kwargs,deadline=deadline)
        receipt={'schema':'gradia.feedback-work-item.handoff.v1','correlation_id':str(uuid.uuid4()),'occurred_at':datetime.now(timezone.utc).isoformat(),
                 'operation':'feedback.propose_work_item','result':'refused','reason':None,'cost':{'status':'unknown','amount':None},
                 'acceptance':'not_evaluated','coverage':'this_invocation_only','automatic_retry':False,'promotion_authorized':False}
        try:
            if type(feedback_id) is not str or not re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',feedback_id) or type(expected_revision) is not int or not 1<=expected_revision<=2147483647:
                raise HandoffError('invalid_selection')
            phase='sessions';source_fp,source_config=self._session('source','operator','0.2.1',deadline);target_fp,target_config=self._session('target','bridge',self.target_version,deadline)
            receipt.update({'source_principal_sha256':source_fp,'target_principal_sha256':target_fp,'source_configuration_sha256':source_config,'target_configuration_sha256':target_config,'source_version':'0.2.1','target_version':self.target_version})
            def correlation(headers):
                value=headers.get('x-gradia-correlation-id')
                return value if type(value) is str and re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',value) else None
            phase='reconcile';status,data,response_headers=request('target','GET','/v1/work-items/by-source/feedback/'+feedback_id,fingerprint=target_fp,configuration=target_config)
            receipt['target_reconciliation_correlation_id']=correlation(response_headers)
            if status==200:
                item=Item.model_validate(data)
                if item.source is None or item.source.id!=feedback_id or item.source.revision!=expected_revision:raise HandoffError('source_already_bound_to_other_revision')
                receipt['result']='recovered'
            elif status!=404:raise HandoffError('reconciliation_refused')
            else:
                phase='target_profile';status,config,_=request('target','GET','/v1/work-items/config',fingerprint=target_fp,configuration=target_config)
                if status!=200 or type(config) is not dict or config.get('configuration_digest')!=target_config or type(config.get('settings')) is not dict or config['settings'].get('initial_status')!='proposed' or config['settings'].get('enabled') is not True:
                    raise HandoffError('proposal_profile_required')
                phase='source_read';status,source,response_headers=request('source','GET','/v1/feedback/'+feedback_id,fingerprint=source_fp,configuration=source_config)
                receipt['source_read_correlation_id']=correlation(response_headers)
                if status!=200 or type(source) is not dict:raise HandoffError('source_read_refused')
                if source.get('id')!=feedback_id or type(source.get('revision')) is not int or source['revision']!=expected_revision:raise HandoffError('source_revision_changed')
                message=source.get('message');category=source.get('category')
                if type(message) is not str or not 1<=len(message)<=10000 or type(category) is not str or len(category)>60:raise HandoffError('invalid_feedback_contract')
                snapshot={k:source[k] for k in ('id','revision','message','category')}
                source_pin=Source(id=feedback_id,revision=expected_revision,sha256=hashlib.sha256(canonical(snapshot).encode()).hexdigest())
                title=(message.strip().splitlines() or ['Feedback proposal'])[0][:180] or 'Feedback proposal'
                payload={'title':title,'description':message,'source':source_pin.model_dump(mode='json')}
                key=hashlib.sha256((self.profile_id+':'+feedback_id).encode()).hexdigest()
                phase='target_write';write_attempted=True
                status,data,response_headers=request('target','POST','/v1/work-items',fingerprint=target_fp,configuration=target_config,payload=payload,key=key)
                receipt['target_write_correlation_id']=correlation(response_headers)
                if status not in (200,201):
                    if status in (400,401,403,404,409,413,422):write_attempted=False
                    raise HandoffError('target_write_not_acknowledged')
                if type(data) is not dict or data.get('status')!='acknowledged':raise HandoffError('invalid_target_acknowledgment')
                acknowledgment=Acknowledgment.model_validate(data);item=acknowledgment.item
                if item.source!=source_pin or item.title!=title or item.description!=message or item.status!='proposed' or item.revision!=1 or item.assignee is not None:raise HandoffError('target_acknowledgment_mismatch')
                receipt['result']='proposed';receipt['source_snapshot_sha256']=source_pin.sha256
        except (HandoffError,httpx.HTTPError,ValidationError) as error:
            item=None;receipt['result']='uncertain' if write_attempted else 'refused'
            receipt['reason']=str(error) if isinstance(error,HandoffError) else 'transport_or_contract_failure'
        receipt['phase']=phase;receipt['duration_ms']=round((time.monotonic()-started)*1000,3)
        return {'item':item.model_dump(mode='json') if item else None,'receipt':receipt}

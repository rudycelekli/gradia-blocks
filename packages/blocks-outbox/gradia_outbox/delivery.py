"""Explicit at-most-one send attempt, conservative uncertainty, fixed receipt reconciliation."""
from datetime import datetime, timezone
import hashlib
import hmac
import json
import time
from types import MappingProxyType
from uuid import uuid4
import httpx
from .contracts import Destination, Principal, OutboxError, VERSION, MAX_REVISION, canonical_json, configuration_digest, identifier, parse_json
from .store import Store, record


def now(): return datetime.now(timezone.utc).isoformat()

def _error(status,code): raise OutboxError(status,code)

class Engine:
    def __init__(self,database_path,destinations,sender=None):
        if not isinstance(destinations,dict) or not 1<=len(destinations)<=100: raise ValueError('One to 100 destinations required')
        for key,value in destinations.items():
            identifier(key)
            if not isinstance(value,Destination): raise ValueError('Destination contract required')
        self.destinations=MappingProxyType(dict(destinations))
        self.configuration_digest=configuration_digest(self.destinations)
        self.config_digest=self.configuration_digest
        self.store=Store(database_path,self.configuration_digest)
        self.sender=sender

    def _operator(self,principal):
        if not isinstance(principal,Principal): _error(403,'invalid_principal')
        if principal.role!='operator': _error(403,'operator_required')

    def _principal(self,principal):
        if not isinstance(principal,Principal): _error(403,'invalid_principal')

    def _correlation(self,correlation_id):
        if correlation_id is not None:
            try: identifier(correlation_id)
            except ValueError: _error(422,'invalid_correlation_id')

    def _row(self,conn,principal,item_id):
        self._principal(principal)
        try: identifier(item_id)
        except ValueError: _error(404,'not_found')
        row=conn.execute('SELECT * FROM outbox_items WHERE tenant=? AND id=?',(principal.tenant_id,item_id)).fetchone()
        if row is None or (principal.role!='operator' and row['actor']!=principal.actor_id): _error(404,'not_found')
        return row

    def _event(self,conn,principal,row,action,**extra):
        metadata={'block_id':'gradia.outbox','edition':VERSION,'event_id':str(uuid4()),'occurred_at':now(),'privacy':'metadata_only','schema':'gradia.outbox.event.v1','block_version':VERSION,'tenant_id':principal.tenant_id,'actor_id':principal.actor_id,'item_id':row['id'],'destination_id':row['destination_id'],'event_type':row['event_type'],'revision':row['revision'],'status':row['status'],'action':action,'at':now(),'configuration_digest':self.configuration_digest,'payload_sha256':row['payload_sha256'],'attempt_id':row['attempt_id'],'cost':{'status':'unknown','amount':None,'currency':None},'acceptance':'not_evaluated','billability':'not_evaluated','human_involvement':'unknown',**extra}
        conn.execute('INSERT INTO outbox_events(tenant,item_id,payload) VALUES(?,?,?)',(principal.tenant_id,row['id'],canonical_json(metadata).decode()))

    def enqueue(self,principal,destination_id,event_type,payload,idempotency_key,*,correlation_id=None):
        self._correlation(correlation_id)
        if not isinstance(principal,Principal): _error(403,'invalid_principal')
        try: identifier(destination_id); identifier(event_type); identifier(idempotency_key)
        except ValueError: _error(422,'invalid_identifier')
        destination=self.destinations.get(destination_id)
        if destination is None or destination.tenant_id!=principal.tenant_id: _error(404,'destination_not_found')
        if type(payload) is not dict: _error(422,'payload_must_be_object')
        try: payload_bytes=canonical_json(payload)
        except (ValueError,UnicodeError,OverflowError): _error(422,'invalid_payload')
        if len(payload_bytes)>16384: _error(413,'payload_too_large')
        digest=hashlib.sha256(canonical_json({'destination_id':destination_id,'event_type':event_type,'payload':payload})).hexdigest()
        with self.store.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            previous=conn.execute('SELECT digest,response FROM outbox_keys WHERE tenant=? AND actor=? AND key=?',(principal.tenant_id,principal.actor_id,idempotency_key)).fetchone()
            if previous:
                if previous['digest']!=digest: _error(409,'idempotency_conflict')
                return {'item':json.loads(previous['response']),'replayed':True}
            item_id=str(uuid4()); created=now(); payload_sha=hashlib.sha256(payload_bytes).hexdigest()
            envelope=canonical_json({'schema':'gradia.webhook.v1','delivery_id':item_id,'tenant_id':principal.tenant_id,'event_type':event_type,'payload_sha256':payload_sha,'payload':payload,'created_at':created})
            conn.execute('INSERT INTO outbox_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(principal.tenant_id,principal.actor_id,item_id,1,'queued',destination_id,event_type,payload_sha,envelope,created,created,None))
            row=self._row(conn,principal,item_id); result=record(row)
            conn.execute('INSERT INTO outbox_keys VALUES(?,?,?,?,?)',(principal.tenant_id,principal.actor_id,idempotency_key,digest,canonical_json(result).decode()))
            self._event(conn,principal,row,'enqueued',correlation_id=correlation_id)
            return {'item':result,'replayed':False}

    def get(self,principal,item_id):
        with self.store.connection() as conn: return record(self._row(conn,principal,item_id))

    def list(self,principal,limit=50):
        self._principal(principal)
        if type(limit) is not int or not 1<=limit<=100: _error(422,'invalid_limit')
        with self.store.connection() as conn:
            if principal.role=='operator': rows=conn.execute('SELECT * FROM outbox_items WHERE tenant=? ORDER BY created_at DESC,id LIMIT ?',(principal.tenant_id,limit))
            else: rows=conn.execute('SELECT * FROM outbox_items WHERE tenant=? AND actor=? ORDER BY created_at DESC,id LIMIT ?',(principal.tenant_id,principal.actor_id,limit))
            return [record(row) for row in rows]

    def events(self,principal,item_id,limit=100):
        self._operator(principal)
        if type(limit) is not int or not 1<=limit<=100: _error(422,'invalid_limit')
        with self.store.connection() as conn:
            self._row(conn,principal,item_id)
            return [json.loads(row[0]) for row in conn.execute('SELECT payload FROM outbox_events WHERE tenant=? AND item_id=? ORDER BY sequence DESC LIMIT ?',(principal.tenant_id,item_id,limit))]

    def _revision(self,row,expected_revision):
        if type(expected_revision) is not int or not 1<=expected_revision<MAX_REVISION: _error(422,'invalid_revision')
        if row['revision']!=expected_revision: _error(409,'revision_conflict')

    def mark_uncertain(self,principal,item_id,expected_revision,*,correlation_id=None):
        """Operator asserts sender is quiescent. Does not retry or prove receiver state."""
        self._correlation(correlation_id)
        self._operator(principal)
        with self.store.connection() as conn:
            conn.execute('BEGIN IMMEDIATE'); row=self._row(conn,principal,item_id); self._revision(row,expected_revision)
            if row['status']!='in_flight': _error(409,'invalid_state')
            conn.execute("UPDATE outbox_items SET status='uncertain',revision=revision+1,updated_at=? WHERE tenant=? AND id=?",(now(),principal.tenant_id,item_id))
            row=self._row(conn,principal,item_id); self._event(conn,principal,row,'operator_marked_uncertain',correlation_id=correlation_id)
            return record(row)

    def _request(self,destination,method,url,row):
        deadline=time.monotonic()+6
        timestamp=str(int(time.time()))
        envelope=bytes(row['envelope'])
        # POST signature binds immutable envelope; receipt GET additionally binds exact path.
        material=timestamp.encode()+b'.'+envelope if method=='POST' else timestamp.encode()+b'.GET.'+url.encode()+b'.'+envelope
        signature=hmac.new(destination.signing_key.encode(),material,hashlib.sha256).hexdigest()
        headers={'Content-Type':'application/json','Accept':'application/json','Accept-Encoding':'identity','X-Gradia-Signature':'sha256='+signature,'X-Gradia-Timestamp':timestamp,'X-Gradia-Delivery-Id':row['id'],'X-Gradia-Attempt-Id':row['attempt_id'],'X-Gradia-Payload-Sha256':row['payload_sha256']}
        def execute(client):
            # A fresh Request bypasses client default headers/cookies; auth=None disables client auth.
            request=httpx.Request(method,url,content=envelope if method=='POST' else None,headers=headers,extensions={'timeout':dict.fromkeys(('connect','read','write','pool'),destination.timeout)})
            response=client.send(request,stream=True,auth=None,follow_redirects=False)
            try:
                if response.status_code==404 and method=='GET': return False
                if response.status_code not in (200,201): raise ValueError('Receiver did not acknowledge')
                if response.headers.get('content-encoding','identity').lower()!='identity': raise ValueError('Encoded acknowledgment refused')
                if response.headers.get('content-type','').split(';')[0].strip().lower()!='application/json': raise ValueError('JSON acknowledgment required')
                body=bytearray()
                chunks=(response.content,) if response.is_stream_consumed else response.iter_raw()
                for chunk in chunks:
                    if time.monotonic()>deadline: raise ValueError('Response deadline exceeded')
                    body.extend(chunk)
                    if len(body)>16384: raise ValueError('Acknowledgment too large')
                ack=parse_json(bytes(body))
                expected={'schema':'gradia.webhook.ack.v1','delivery_id':row['id'],'payload_sha256':row['payload_sha256'],'receiver_id':destination.receiver_id,'accepted':True}
                if type(ack) is not dict or set(ack)!=set(expected) or any(type(ack[k]) is not type(v) or ack[k]!=v for k,v in expected.items()): raise ValueError('Unbound acknowledgment')
                ack_material=b'gradia.webhook.ack.v1.'+timestamp.encode()+b'.'+row['attempt_id'].encode()+b'.'+canonical_json(ack)
                ack_signature='sha256='+hmac.new(destination.signing_key.encode(),ack_material,hashlib.sha256).hexdigest()
                if not hmac.compare_digest(response.headers.get('x-gradia-ack-signature',''),ack_signature): raise ValueError('Unauthenticated acknowledgment')
                return True
            finally: response.close()
        if self.sender is not None: return execute(self.sender)
        with httpx.Client(trust_env=False) as client: return execute(client)

    def deliver(self,principal,item_id,expected_revision,*,correlation_id=None):
        self._correlation(correlation_id)
        self._operator(principal)
        with self.store.connection() as conn:
            conn.execute('BEGIN IMMEDIATE'); row=self._row(conn,principal,item_id); self._revision(row,expected_revision)
            if row['status']!='queued': _error(409,'invalid_state')
            # Claim plus terminal receipt must both fit before any irreversible send.
            if row['revision']>MAX_REVISION-2: _error(409,'revision_exhausted')
            conn.execute("UPDATE outbox_items SET status='in_flight',attempt_id=?,revision=revision+1,updated_at=? WHERE tenant=? AND id=?",(str(uuid4()),now(),principal.tenant_id,item_id))
            row=dict(self._row(conn,principal,item_id)); self._event(conn,principal,row,'send_claimed',correlation_id=correlation_id)
        accepted=False
        try: accepted=self._request(self.destinations[row['destination_id']],'POST',self.destinations[row['destination_id']].url,row)
        except Exception: pass  # Untrusted response/transport details never enter receipts or logs.
        return self._finish(principal,item_id,row,accepted,'delivery_acknowledged' if accepted else 'delivery_uncertain',correlation_id)

    def _finish(self,principal,item_id,claimed,accepted,action,correlation_id=None):
        with self.store.connection() as conn:
            conn.execute('BEGIN IMMEDIATE'); row=self._row(conn,principal,item_id)
            if row['revision']!=claimed['revision'] or row['status']!=claimed['status'] or row['attempt_id']!=claimed['attempt_id']:
                _error(409,'claim_changed')
            if row['revision']>=MAX_REVISION: _error(409,'revision_exhausted')
            conn.execute('UPDATE outbox_items SET status=?,revision=revision+1,updated_at=? WHERE tenant=? AND id=?',('delivered' if accepted else 'uncertain',now(),principal.tenant_id,item_id))
            row=self._row(conn,principal,item_id)
            evidence={}
            if accepted:
                destination=self.destinations[row['destination_id']]
                acknowledgment={'schema':'gradia.webhook.ack.v1','delivery_id':row['id'],'payload_sha256':row['payload_sha256'],'receiver_id':destination.receiver_id,'accepted':True}
                evidence={'receiver_id':destination.receiver_id,'acknowledgment_sha256':hashlib.sha256(canonical_json(acknowledgment)).hexdigest(),'acknowledgment_authentication':'hmac-sha256'}
            self._event(conn,principal,row,action,correlation_id=correlation_id,**evidence)
            return record(row)

    def reconcile(self,principal,item_id,expected_revision,*,correlation_id=None):
        self._correlation(correlation_id)
        self._operator(principal)
        with self.store.connection() as conn:
            row=dict(self._row(conn,principal,item_id)); self._revision(row,expected_revision)
            if row['status']!='uncertain': _error(409,'invalid_state')
        destination=self.destinations[row['destination_id']]
        try: accepted=self._request(destination,'GET',destination.url.rstrip('/')+'/receipts/'+row['id'],row)
        except Exception: accepted=False
        return self._finish(principal,item_id,row,accepted,'receipt_acknowledged' if accepted else 'receipt_unconfirmed',correlation_id)

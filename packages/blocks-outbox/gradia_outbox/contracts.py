"""Bounded public contracts for the Gradia durable webhook outbox."""
from dataclasses import dataclass, field
import hashlib
import ipaddress
import json
import math
import re
from urllib.parse import urlsplit

VERSION = '0.1.0'
MAX_REVISION = 9007199254740991
ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$')

class OutboxError(Exception):
    def __init__(self, status_code, code):
        self.status_code, self.code = status_code, code
        super().__init__(code)

def identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError('Invalid ASCII identifier')
    return value

def _bounded(value, depth=0):
    if depth > 16: raise ValueError('JSON nesting exceeds 16')
    if value is None or type(value) in (str, bool): return
    if type(value) is int:
        if abs(value) > 9007199254740991: raise ValueError('Integer exceeds interoperable range')
        return
    if type(value) is float:
        if not math.isfinite(value): raise ValueError('Nonfinite JSON number')
        return
    if type(value) is list:
        for child in value: _bounded(child, depth + 1)
        return
    if type(value) is dict:
        for key, child in value.items():
            if type(key) is not str: raise ValueError('JSON object keys must be strings')
            _bounded(child, depth + 1)
        return
    raise ValueError('Unsupported JSON value')

def canonical_json(value):
    _bounded(value)
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')

def parse_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result: raise ValueError('Duplicate JSON key')
            result[key] = value
        return result
    try:
        value = json.loads(data, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))
        _bounded(value)
        return value
    except (RecursionError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('Invalid bounded JSON') from exc

@dataclass(frozen=True)
class Principal:
    tenant_id: str
    actor_id: str
    role: str = 'submitter'
    def __post_init__(self):
        identifier(self.tenant_id); identifier(self.actor_id)
        if self.role not in ('submitter', 'operator'): raise ValueError('Invalid role')

@dataclass(frozen=True)
class Destination:
    tenant_id: str
    url: str
    signing_key: str = field(repr=False)
    receiver_id: str
    timeout: float = 3.0
    def __post_init__(self):
        identifier(self.tenant_id); identifier(self.receiver_id)
        if not isinstance(self.signing_key, str) or len(self.signing_key) < 32 or len(self.signing_key) > 4096 or not self.signing_key.isascii() or any(ord(c)<33 or ord(c)>126 for c in self.signing_key):
            raise ValueError('Signing key must be 32-4096 printable ASCII characters')
        if type(self.timeout) not in (int,float) or not math.isfinite(self.timeout) or not 0 < self.timeout <= 3:
            raise ValueError('Timeout must be positive and at most three seconds')
        if not isinstance(self.url,str) or len(self.url)>2048 or not self.url.isascii() or any(ord(c)<=32 or ord(c)==127 for c in self.url): raise ValueError('Invalid destination URL')
        try:
            parsed=urlsplit(self.url)
            if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname: raise ValueError('Invalid destination URL')
            parsed.port
            if parsed.scheme != 'https':
                if parsed.scheme != 'http' or not ipaddress.ip_address(parsed.hostname).is_loopback: raise ValueError('HTTPS required except literal loopback')
        except (ValueError, TypeError) as exc: raise ValueError('Invalid destination URL') from exc
    def public(self):
        return {'tenant_id':self.tenant_id,'url':self.url,'receiver_id':self.receiver_id,'timeout':self.timeout,'signing_key_sha256':hashlib.sha256(self.signing_key.encode()).hexdigest()}

def configuration_digest(destinations):
    return hashlib.sha256(canonical_json({identifier(k):v.public() for k,v in destinations.items()})).hexdigest()

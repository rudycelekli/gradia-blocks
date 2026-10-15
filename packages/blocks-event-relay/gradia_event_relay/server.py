"""Explicit loopback server entrypoint. Host configuration stays outside the package."""
import json
import os
from pathlib import Path
import stat
from .api import create_app
from .contracts import Principal


def private_json(path):
    fd=os.open(Path(path),os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_mode&0o077 or info.st_size>65536:
            raise ValueError('Configuration must be a bounded private regular file')
        body=stream.read(65537)
    if len(body)>65536:raise ValueError('Configuration too large')
    def pairs(values):
        result={}
        for key,value in values:
            if key in result:raise ValueError('Duplicate configuration key')
            result[key]=value
        return result
    return json.loads(body,object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError('Nonfinite configuration')))


def configured_app():
    try:
        from .relay import Relay
        keys=private_json(os.environ['GRADIA_EVENT_RELAY_CREDENTIALS_FILE'])
        profile=private_json(os.environ['GRADIA_EVENT_RELAY_PROFILE_FILE'])
        if type(profile) is not dict or set(profile)!={'source_origin','target_origin','source_token','target_token','tenant_id','profile_id','destination_id'}:raise ValueError('Invalid profile')
        relay=Relay(os.environ['GRADIA_EVENT_RELAY_DATABASE'],**profile)
        return create_app(relay,keys)
    except (KeyError,TypeError,ValueError,OSError,RecursionError):
        raise RuntimeError('Host configuration refused; check private files, schema and identities') from None

if __name__=='__main__':
    import uvicorn
    uvicorn.run(configured_app(),host='127.0.0.1',port=int(os.environ.get('GRADIA_EVENT_RELAY_PORT','3194')))

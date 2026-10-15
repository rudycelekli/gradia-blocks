"""Explicit loopback server entrypoint. Host configuration stays outside the package."""
import json
import os
from pathlib import Path
import stat
from .api import create_app
from .contracts import Principal,Settings


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
        keys=private_json(os.environ['GRADIA_WORK_ITEMS_CREDENTIALS_FILE'])
        if type(keys) is not dict:raise ValueError('Invalid credentials')
        settings_path=os.environ.get('GRADIA_WORK_ITEMS_SETTINGS_FILE')
        settings=Settings.model_validate(private_json(settings_path)) if settings_path else Settings()
        return create_app(os.environ['GRADIA_WORK_ITEMS_DATABASE'],keys,settings)
    except (KeyError,ValueError,OSError,RecursionError):
        raise RuntimeError('Host configuration refused; check private files, schema and workflow identity') from None


if __name__=='__main__':
    import uvicorn
    uvicorn.run(configured_app(),host='127.0.0.1',port=int(os.environ.get('GRADIA_WORK_ITEMS_PORT','3192')))

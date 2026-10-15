"""Private checkpoints and durable metadata; acknowledgment advancement is atomic."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import sqlite3
import stat
from .contracts import RelayError,canonical,parse_json,Checkpoint
SCHEMA='''
CREATE TABLE relay_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE relay_events(sequence INTEGER PRIMARY KEY AUTOINCREMENT,payload TEXT NOT NULL);
'''
def signature(conn):return sorted(tuple(row) for row in conn.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name'))
class Store:
    def __init__(self,path,config):
        if str(path)==':memory:':raise ValueError('Persistent database required')
        self.path=str(Path(path))
        database_fd=self._private_fd(self.path)
        os.close(database_fd)
        with self.lock():
            expected=sqlite3.connect(':memory:')
            try:
                expected.executescript(SCHEMA);expected_signature=signature(expected)
                with self.connection() as conn:
                    conn.execute('BEGIN IMMEDIATE');current=signature(conn)
                    if current and current!=expected_signature:raise ValueError('Foreign database schema')
                    if not current:
                        for statement in SCHEMA.split(';'):
                            if statement.strip():conn.execute(statement)
                        conn.executemany('INSERT INTO relay_metadata VALUES(?,?)',[('schema','1'),('config',config),('state','null')])
                    metadata=dict(conn.execute('SELECT key,value FROM relay_metadata'))
                    if set(metadata)!={'schema','config','state'} or metadata['schema']!='1' or metadata['config']!=config:raise ValueError('Database identity changed')
                    self._state(metadata['state'])
            finally:expected.close()
    def _private_fd(self,path):
        fd=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_mode&0o077:
            os.close(fd);raise ValueError('Private regular database and lock required')
        return fd
    @contextmanager
    def lock(self):
        # Separate private lock inode: on macOS flock(database) conflicts with SQLite.
        # All conforming relay processes use this stable sidecar; trusted directory required.
        fd=self._private_fd(self.path+'.run-lock')
        try:
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise RelayError('run_in_progress',409) from None
            info=os.fstat(fd);current=os.stat(self.path+'.run-lock',follow_symlinks=False)
            if (current.st_dev,current.st_ino)!=(info.st_dev,info.st_ino):raise ValueError('Lock inode changed')
            db_fd=self._private_fd(self.path);os.close(db_fd)
            yield
        finally:os.close(fd)
    @contextmanager
    def connection(self):
        conn=sqlite3.connect(self.path,timeout=1);conn.row_factory=sqlite3.Row
        try:
            conn.execute('PRAGMA synchronous=FULL');yield conn;conn.commit()
        except BaseException:conn.rollback();raise
        finally:conn.close()
    def _state(self,value):
        state=parse_json(value)
        if state is not None:
            validated=Checkpoint.model_validate(state)
            if (validated.anchor is None and validated.cursor!=0) or (validated.anchor is not None and validated.anchor.sequence!=validated.cursor):raise ValueError('Invalid checkpoint anchor')
        return state
    def state(self):
        with self.connection() as conn:return self._state(conn.execute("SELECT value FROM relay_metadata WHERE key='state'").fetchone()[0])
    def save(self,state,event):
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute("UPDATE relay_metadata SET value=? WHERE key='state'",(canonical(state).decode(),))
            conn.execute('INSERT INTO relay_events(payload) VALUES(?)',(canonical(event).decode(),))
    def events(self,limit=100):
        if type(limit) is not int or not 1<=limit<=100:raise RelayError('invalid_limit',422)
        with self.connection() as conn:return [parse_json(row[0]) for row in conn.execute('SELECT payload FROM relay_events ORDER BY sequence DESC LIMIT ?',(limit,))]

    def append_event(self,event):
        with self.connection() as conn:
            conn.execute('INSERT INTO relay_events(payload) VALUES(?)',(canonical(event).decode(),))

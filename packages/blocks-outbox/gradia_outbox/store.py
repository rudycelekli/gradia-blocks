"""Private SQLite storage; mutation and metadata recording share transactions."""
from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import stat

SCHEMA = '''
CREATE TABLE outbox_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE outbox_items(
 tenant TEXT NOT NULL,actor TEXT NOT NULL,id TEXT NOT NULL,revision INTEGER NOT NULL,
 status TEXT NOT NULL,destination_id TEXT NOT NULL,event_type TEXT NOT NULL,payload_sha256 TEXT NOT NULL,
 envelope BLOB NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,attempt_id TEXT,
 PRIMARY KEY(tenant,id));
CREATE TABLE outbox_keys(
 tenant TEXT NOT NULL,actor TEXT NOT NULL,key TEXT NOT NULL,digest TEXT NOT NULL,response TEXT NOT NULL,
 PRIMARY KEY(tenant,actor,key));
CREATE TABLE outbox_events(
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,tenant TEXT NOT NULL,item_id TEXT NOT NULL,payload TEXT NOT NULL);
'''

def _signature(conn):
    return sorted(tuple(row) for row in conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"))

class Store:
    def __init__(self,path,configuration_digest):
        if str(path)==':memory:': raise ValueError('Persistent private SQLite path required')
        self.path=str(Path(path))
        fd=os.open(self.path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_mode&0o077:
                raise ValueError('Private regular database without hardlinks required')
        finally: os.close(fd)
        expected=sqlite3.connect(':memory:')
        try:
            expected.executescript(SCHEMA)
            signature=_signature(expected)
            with self.connection() as conn:
                conn.execute('BEGIN IMMEDIATE')
                current=_signature(conn)
                if current and current!=signature: raise ValueError('Foreign or changed schema; explicit migration required')
                if current and dict(conn.execute('SELECT key,value FROM outbox_metadata'))!={'schema':'1','configuration_digest':configuration_digest}:
                    raise ValueError('Missing or changed identity; explicit migration required')
                if not current:
                    for statement in SCHEMA.split(';'):
                        if statement.strip(): conn.execute(statement)
                for key,value in {'schema':'1','configuration_digest':configuration_digest}.items():
                    conn.execute('INSERT OR IGNORE INTO outbox_metadata VALUES(?,?)',(key,value))
                    if conn.execute('SELECT value FROM outbox_metadata WHERE key=?',(key,)).fetchone()[0]!=value:
                        raise ValueError('Configuration changed; explicit offline migration required')
        finally: expected.close()

    @contextmanager
    def connection(self):
        conn=sqlite3.connect(self.path,timeout=5)
        conn.row_factory=sqlite3.Row
        try:
            conn.execute('PRAGMA synchronous=FULL')
            conn.execute('PRAGMA foreign_keys=ON')
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback(); raise
        finally: conn.close()

def record(row):
    return {key:row[key] for key in ('id','revision','status','destination_id','event_type','payload_sha256','created_at','updated_at','attempt_id')}

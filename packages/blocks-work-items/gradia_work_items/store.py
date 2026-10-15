from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import stat

SCHEMA='''
CREATE TABLE IF NOT EXISTS work_item_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS work_items(
 tenant TEXT NOT NULL, actor TEXT NOT NULL, id TEXT NOT NULL, revision INTEGER NOT NULL,
 status TEXT NOT NULL,title TEXT NOT NULL,description TEXT NOT NULL,assignee TEXT,
 source TEXT,source_kind TEXT,source_id TEXT,create_digest TEXT NOT NULL,create_response TEXT NOT NULL,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(tenant,id),
 UNIQUE(tenant,actor,source_kind,source_id));
CREATE TABLE IF NOT EXISTS work_item_keys(
 tenant TEXT NOT NULL,actor TEXT NOT NULL,key TEXT NOT NULL,digest TEXT NOT NULL,response TEXT NOT NULL,
 PRIMARY KEY(tenant,actor,key));
CREATE TABLE IF NOT EXISTS work_item_events(
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,tenant TEXT NOT NULL,item_id TEXT NOT NULL,payload TEXT NOT NULL);
'''


class Store:
    def __init__(self,path,configuration_digest):
        if str(path)==':memory:':
            raise ValueError('Use a persistent private SQLite path')
        self.path=str(Path(path))
        fd=os.open(self.path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_mode&0o077:
                raise ValueError('Database must be a private regular file without hardlinks')
        finally:
            os.close(fd)
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            tables={row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            allowed={'work_item_metadata','work_items','work_item_keys','work_item_events','sqlite_sequence'}
            if tables and tables!=allowed:
                raise ValueError('Database belongs to another application')
            if tables:
                # Names alone cannot identify a database: reject altered columns and uniqueness.
                expected=sqlite3.connect(':memory:')
                try:
                    expected.executescript(SCHEMA)
                    def signature(database,table):
                        columns=[tuple(row)[1:] for row in database.execute('PRAGMA table_info('+table+')')]
                        indexes=[]
                        for row in database.execute('PRAGMA index_list('+table+')'):
                            cols=tuple(tuple(c)[2] for c in database.execute('SELECT seqno,cid,name FROM pragma_index_info(?)',(row[1],)))
                            indexes.append((row[2],row[3],row[4],cols))
                        return columns,sorted(indexes)
                    if any(signature(conn,table)!=signature(expected,table) for table in allowed):
                        raise ValueError('Stored schema changed; explicit migration required')
                finally:
                    expected.close()
            # Execute separately to retain the startup transaction (executescript commits implicitly).
            for statement in SCHEMA.split(';'):
                if statement.strip():conn.execute(statement)
            expected={'schema':'1','configuration_digest':configuration_digest}
            for key,value in expected.items():
                conn.execute('INSERT OR IGNORE INTO work_item_metadata VALUES(?,?)',(key,value))
                if conn.execute('SELECT value FROM work_item_metadata WHERE key=?',(key,)).fetchone()[0]!=value:
                    raise ValueError('Stored configuration changed; explicit offline migration required')

    @contextmanager
    def connection(self):
        conn=sqlite3.connect(self.path,timeout=5)
        conn.row_factory=sqlite3.Row
        try:
            conn.execute('PRAGMA foreign_keys=ON')
            conn.execute('PRAGMA synchronous=FULL')
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


def record(row):
    return {**{k:row[k] for k in ('id','revision','status','title','description','assignee','created_at','updated_at')},
            'source':json.loads(row['source']) if row['source'] else None}

"""Synthetic loopback-only receiver for protocol qualification, not a hosted adapter.

The record and acknowledgement are atomic here; a real receiver must atomically
bind its own business effect and dedup record, or preserve its own uncertainty.
"""
import hashlib
import hmac
import os
import socket
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from gradia_outbox.contracts import canonical_json, parse_json
from gradia_outbox.server import private_json


def main():
    cfg = private_json(os.environ['GRADIA_FIXTURE_CONFIG'])
    fd = os.open(cfg['database'], os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    with sqlite3.connect(cfg['database']) as connection:
        connection.execute('CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY,envelope BLOB NOT NULL,payload_sha256 TEXT NOT NULL)')
        connection.execute('CREATE TABLE IF NOT EXISTS counters(name TEXT PRIMARY KEY,value INTEGER NOT NULL)')
        connection.execute("INSERT OR IGNORE INTO counters VALUES('posts',0)")

    class Receiver(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, data):
            body = canonical_json(data)
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            if status in (200, 201) and data.get('schema') == 'gradia.webhook.ack.v1':
                material = b'gradia.webhook.ack.v1.' + self.headers['X-Gradia-Timestamp'].encode() + b'.' + self.headers['X-Gradia-Attempt-Id'].encode() + b'.' + body
                self.send_header('X-Gradia-Ack-Signature', 'sha256=' + hmac.new(cfg['signing_key'].encode(), material, hashlib.sha256).hexdigest())
            self.end_headers()
            self.wfile.write(body)

        def authorized(self, envelope, method):
            stamp = self.headers.get('X-Gradia-Timestamp', '')
            if not stamp.isdigit() or abs(time.time() - int(stamp)) > 60:
                raise ValueError('Expired fixture signature')
            value = parse_json(envelope)
            if value['delivery_id'] != self.headers.get('X-Gradia-Delivery-Id') or value['tenant_id'] != 'demo':
                raise ValueError('Identity mismatch')
            if value['payload_sha256'] != hashlib.sha256(canonical_json(value['payload'])).hexdigest():
                raise ValueError('Payload mismatch')
            if value['payload_sha256'] != self.headers.get('X-Gradia-Payload-Sha256'):
                raise ValueError('Header mismatch')
            material = stamp.encode() + b'.' + envelope if method == 'POST' else stamp.encode() + b'.GET.' + (cfg['origin'] + self.path).encode() + b'.' + envelope
            expected = 'sha256=' + hmac.new(cfg['signing_key'].encode(), material, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, self.headers.get('X-Gradia-Signature', '')):
                raise ValueError('Signature mismatch')
            return value

        def acknowledgment(self, value):
            return {'schema': 'gradia.webhook.ack.v1', 'delivery_id': value['delivery_id'], 'payload_sha256': value['payload_sha256'], 'receiver_id': cfg['receiver_id'], 'accepted': True}

        def do_POST(self):
            if self.path != '/webhook':
                return self.reply(404, {'error': 'not_found'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 32768:
                    raise ValueError('Size')
                envelope = self.rfile.read(length)
                value = self.authorized(envelope, 'POST')
                with sqlite3.connect(cfg['database']) as connection:
                    connection.execute('PRAGMA synchronous=FULL')
                    connection.execute('BEGIN IMMEDIATE')
                    previous = connection.execute('SELECT envelope FROM receipts WHERE id=?', (value['delivery_id'],)).fetchone()
                    if previous and bytes(previous[0]) != envelope:
                        raise ValueError('Conflicting duplicate')
                    connection.execute('INSERT OR IGNORE INTO receipts VALUES(?,?,?)', (value['delivery_id'], envelope, value['payload_sha256']))
                    connection.execute("UPDATE counters SET value=value+1 WHERE name='posts'")
                if cfg.get('lose_ack', False):
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                self.reply(200 if previous else 201, self.acknowledgment(value))
            except (ValueError, KeyError, TypeError):
                self.reply(400, {'error': 'fixture_refused'})

        def do_GET(self):
            if self.path == '/health':
                return self.reply(200, {'fixture': 'ready'})
            prefix = '/webhook/receipts/'
            if not self.path.startswith(prefix):
                return self.reply(404, {'error': 'not_found'})
            identity = self.path[len(prefix):]
            with sqlite3.connect(cfg['database']) as connection:
                row = connection.execute('SELECT envelope FROM receipts WHERE id=?', (identity,)).fetchone()
            if row is None:
                return self.reply(404, {'error': 'not_found'})
            try:
                value = self.authorized(bytes(row[0]), 'GET')
                if identity != value['delivery_id']:
                    raise ValueError('Path mismatch')
                self.reply(200, self.acknowledgment(value))
            except (ValueError, KeyError, TypeError):
                self.reply(403, {'error': 'fixture_refused'})

    server = ThreadingHTTPServer(('127.0.0.1', urlsplit(cfg['origin']).port), Receiver)
    server.serve_forever()


if __name__ == '__main__':
    main()

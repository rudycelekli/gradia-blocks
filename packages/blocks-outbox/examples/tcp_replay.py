"""Real independent local subprocesses; no external destination or customer data."""
from pathlib import Path
import json
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import httpx


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def replay(lose_ack=False):
    sender_port, receiver_port = free_port(), free_port()
    while sender_port == receiver_port:
        receiver_port = free_port()
    sender_origin = f'http://127.0.0.1:{sender_port}'
    receiver_origin = f'http://127.0.0.1:{receiver_port}'
    token, submit_token, key = [secrets.token_hex(24) for _ in range(3)]
    processes, streams = [], []
    with tempfile.TemporaryDirectory(prefix='gradia-outbox-tcp-') as directory:
        base = Path(directory)
        base.chmod(0o700)
        def private(name, value):
            path = base / name
            path.write_text(json.dumps(value))
            path.chmod(0o600)
            return path
        credentials = private('credentials.json', {token: {'tenant_id': 'demo', 'actor_id': 'operator', 'role': 'operator'}, submit_token: {'tenant_id': 'demo', 'actor_id': 'app', 'role': 'submitter'}})
        destinations = private('destinations.json', {'fixture': {'tenant_id': 'demo', 'url': receiver_origin + '/webhook', 'signing_key': key, 'receiver_id': 'durable-fixture'}})
        fixture_path = private('receiver.json', {'database': str(base / 'receiver.sqlite'), 'origin': receiver_origin, 'signing_key': key, 'receiver_id': 'durable-fixture', 'lose_ack': lose_ack})
        environment = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'PYTHONPATH': os.environ.get('PYTHONPATH', ''), 'LANG': 'C.UTF-8'}
        sender_env = {**environment, 'GRADIA_OUTBOX_DATABASE': str(base / 'outbox.sqlite'), 'GRADIA_OUTBOX_CREDENTIALS_FILE': str(credentials), 'GRADIA_OUTBOX_DESTINATIONS_FILE': str(destinations), 'GRADIA_OUTBOX_PORT': str(sender_port)}
        receiver_env = {**environment, 'GRADIA_FIXTURE_CONFIG': str(fixture_path)}
        def start(args, env):
            stream = open(base / ('process-' + str(len(streams)) + '.log'), 'wb')
            streams.append(stream)
            process = subprocess.Popen([sys.executable, *args], env=env, stdout=stream, stderr=stream)
            processes.append(process)
            return process
        def wait(url, headers=None):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    response = httpx.get(url, headers=headers, timeout=.5, trust_env=False)
                    if response.status_code == 200:
                        return response.json()
                except httpx.HTTPError:
                    pass
                time.sleep(.05)
            raise RuntimeError('Loopback fixture startup failed')
        operator = {'Authorization': 'Bearer ' + token}
        submitter = {'Authorization': 'Bearer ' + submit_token, 'Idempotency-Key': 'fixture-event'}
        try:
            receiver = start([str(Path(__file__).with_name('fixture_receiver.py'))], receiver_env)
            sender = start(['-m', 'gradia_outbox.server'], sender_env)
            wait(receiver_origin + '/health')
            session = wait(sender_origin + '/v1/outbox/session', operator)
            with httpx.Client(trust_env=False, timeout=8) as client:
                request = {'destination_id': 'fixture', 'event_type': 'work_item.proposed', 'payload': {'item_id': 'fictional-work-item', 'status': 'proposed'}}
                created = client.post(sender_origin + '/v1/outbox', json=request, headers=submitter)
                assert created.status_code == 201, created.text
                item = created.json()['item']
                identity = item['id']
                # A stale profile refuses before a delivery claim or network request.
                fenced = client.post(sender_origin + '/v1/outbox/' + identity + '/deliver', json={'expected_revision': 1}, headers={**operator, 'If-Gradia-Configuration': '0' * 64})
                assert fenced.status_code == 409
                denied = client.post(sender_origin + '/v1/outbox/' + identity + '/deliver', json={'expected_revision': 1}, headers=submitter)
                assert denied.status_code == 403
                delivered = client.post(sender_origin + '/v1/outbox/' + identity + '/deliver', json={'expected_revision': 1}, headers={**operator, 'If-Gradia-Configuration': session['configuration_digest']})
                assert delivered.status_code == 200, delivered.text
                first = delivered.json()
                assert first['status'] == ('uncertain' if lose_ack else 'delivered'), first
                # Both durable stores survive independent service restarts.
                for process in (sender, receiver):
                    process.terminate()
                    process.wait(timeout=5)
                start(['-m', 'gradia_outbox.server'], sender_env)
                cfg = json.loads(fixture_path.read_text()); cfg['lose_ack'] = False
                private('receiver.json', cfg)
                start([str(Path(__file__).with_name('fixture_receiver.py'))], receiver_env)
                wait(receiver_origin + '/health'); wait(sender_origin + '/v1/outbox/session', operator)
                recovered = client.get(sender_origin + '/v1/outbox/' + identity, headers=operator).json()
                assert recovered['status'] == first['status']
                if lose_ack:
                    response = client.post(sender_origin + '/v1/outbox/' + identity + '/reconcile', headers=operator, json={'expected_revision': recovered['revision']})
                    assert response.status_code == 200, response.text
                    recovered = response.json()
                assert recovered['status'] == 'delivered' and recovered['id'] == identity
                refused = client.post(sender_origin + '/v1/outbox/' + identity + '/deliver', headers=operator, json={'expected_revision': recovered['revision']})
                assert refused.status_code == 409
                replayed = client.post(sender_origin + '/v1/outbox', json=request, headers=submitter)
                assert replayed.status_code == 200 and replayed.json()['replayed'] is True and replayed.json()['item']['id'] == identity
                events = client.get(sender_origin + '/v1/outbox/' + identity + '/events', headers=operator).json()['items']
                assert all('payload' not in event and 'signing_key' not in event for event in events)
                with sqlite3.connect(base / 'receiver.sqlite') as conn:
                    effects = conn.execute('SELECT COUNT(*) FROM receipts').fetchone()[0]
                    posts = conn.execute("SELECT value FROM counters WHERE name='posts'").fetchone()[0]
                assert effects == posts == 1
                return {'initialStatus': first['status'], 'afterRestartAndExplicitReconciliation': recovered['status'], 'receiverPostRequests': posts, 'durableReceiverRecords': effects, 'sameDeliveryId': recovered['id'] == identity, 'mutationEvents': len(events), 'finalRevision': recovered['revision'], 'repeatDeliveryHttpStatus': refused.status_code, 'submitterDeliveryHttpStatus': denied.status_code, 'staleConfigurationHttpStatus': fenced.status_code, 'enqueueReplayUsesOriginalSnapshot': replayed.json()['item']['status'] == 'queued', 'signedReceiptProtocol': True, 'cost': {'status': 'unknown'}, 'scope': 'Actual local HTTP subprocesses and private SQLite stores; fictional work-item event, not customer acceptance'}
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill(); process.wait(timeout=5)
            for stream in streams:
                stream.close()


if __name__ == '__main__':
    print(json.dumps({'schema': 'gradia.outbox.tcp-replay.v1', 'happyPath': replay(), 'postCommitLoss': replay(True)}, indent=2))

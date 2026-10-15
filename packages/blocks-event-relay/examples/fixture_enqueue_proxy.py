"""Local fault proxy: forward real Outbox requests, optionally lose one enqueue ACK.
No production adapter or arbitrary URL is exposed. Configuration is host-owned.
"""
import os
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
import httpx
from gradia_event_relay.server import private_json


def main():
    cfg=private_json(os.environ['GRADIA_ENQUEUE_PROXY_CONFIG'])
    dropped=False
    class Proxy(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def forward(self):
            nonlocal dropped
            if not self.path.startswith('/v1/outbox'):
                self.send_error(404);return
            length=int(self.headers.get('Content-Length','0'))
            if not 0<=length<=49152:
                self.send_error(413);return
            body=self.rfile.read(length) if length else None
            headers={key:value for key,value in self.headers.items() if key.lower() not in ('host','content-length','connection','transfer-encoding')}
            with httpx.Client(trust_env=False,timeout=4,follow_redirects=False) as client:
                response=client.request(self.command,cfg['target_origin']+self.path,headers=headers,content=body)
            if self.command=='POST' and self.path=='/v1/outbox' and response.status_code==201 and cfg.get('lose_ack',False) and not dropped:
                dropped=True;self.close_connection=True;self.connection.shutdown(socket.SHUT_RDWR);self.connection.close();return
            self.send_response(response.status_code)
            for key,value in response.headers.items():
                if key.lower() not in ('content-length','connection','transfer-encoding','content-encoding'):self.send_header(key,value)
            self.send_header('Content-Length',str(len(response.content)));self.end_headers();self.wfile.write(response.content)
        do_GET=forward
        do_POST=forward
    ThreadingHTTPServer(('127.0.0.1',urlsplit(cfg['origin']).port),Proxy).serve_forever()

if __name__=='__main__':main()

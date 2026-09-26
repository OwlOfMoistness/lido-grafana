"""Exercise the real HTTP collector against a local, synthetic beacon API."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

ROOT=Path(__file__).resolve().parents[2]


class InventoryHTTP(unittest.TestCase):
    def test_real_collector_serves_aggregate_metrics_without_keys(self):
        self.run_collector('true')

    def test_monitoring_only_collector_needs_only_child_csvs(self):
        self.run_collector('false')

    def run_collector(self, enabled):
        funded = 'validator-1' if enabled=='true' else 'validator-3'
        pubkey='0x'+'01'*48
        requests=[]
        class Beacon(BaseHTTPRequestHandler):
            def reply(self,data):
                body=json.dumps(data).encode()
                self.send_response(200)
                self.send_header('Content-Length',str(len(body)))
                self.end_headers();self.wfile.write(body)
            def do_GET(self):
                requests.append(self.path)
                if self.path.endswith('/syncing'):
                    self.reply({'data':{'is_syncing':False,'is_optimistic':False,'el_offline':False}})
                else:
                    self.reply({'execution_optimistic':False,'data':{'canonical':True,'root':'0x'+'ab'*32,
                                'header':{'message':{'slot':'100'}}}})
            def do_POST(self):
                requests.append(self.path)
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                self.reply({'execution_optimistic':False,'data':[
                    {'status':'active_ongoing','balance':'32123456789','validator':{'pubkey':k}} for k in body['ids']]})
            def log_message(self,*args): pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Beacon)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            with tempfile.TemporaryDirectory() as directory:
                (Path(directory)/f'{funded}.csv').write_text(pubkey+'\n')
                (Path(directory)/'validator-2.csv').write_text('')
                children=[{'id':'validator-2','host':'child2.example.org','user':'monitor','local_port':20000}]
                if enabled=='false':
                    children.append({'id':'validator-3','host':'child3.example.org','user':'monitor','local_port':20002})
                env={**os.environ,'BACKEND_ADDRESS':'127.0.0.1','INVENTORY_ENABLED':'true',
                     'INVENTORY_DIRECTORY':directory,'INVENTORY_PORT':str(port),'LOCAL_VALIDATOR_ENABLED':enabled,
                     'MAIN_BEACON_API_PORT':str(server.server_port),'VC_ID':'validator-1',
                     'CHILDREN':json.dumps(children)}
                process=subprocess.Popen([sys.executable,str(ROOT/'deployment/control.py'),'inventory'],env=env,
                                         stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                try:
                    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    deadline=time.monotonic()+10
                    text=''
                    while time.monotonic()<deadline:
                        try:
                            with opener.open(f'http://127.0.0.1:{port}/metrics',timeout=1) as response:
                                text=response.read().decode()
                            if 'lido_inventory_active_balance_eth{' in text: break
                        except OSError: pass
                        time.sleep(.05)
                    self.assertIn('lido_inventory_active_balance_eth{host="'+funded+'"} 32.123456789',text)
                    if enabled=='false': self.assertNotIn('validator-1',text)
                    self.assertIn('lido_inventory_active_validators{host="validator-2"} 0',text)
                    self.assertNotIn(pubkey,text)
                    self.assertIn('/eth/v1/beacon/states/100/validators',requests)
                    self.assertEqual(requests[-1],'/eth/v1/beacon/headers/100')
                finally:
                    process.terminate();process.communicate(timeout=5)
        finally:
            server.shutdown();server.server_close()


if __name__=='__main__':unittest.main()

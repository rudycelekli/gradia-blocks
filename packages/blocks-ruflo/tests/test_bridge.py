import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import sys
import os
from gradia_bridges.adapter import Bridge,BridgeError,Principal,Registry,RufloEdition,LocalCliEdition,OperationSpec,DEFAULT_OPERATIONS,_run
from gradia_bridges.api import create_app
from fastapi.testclient import TestClient

ROOT=Path(__file__).resolve().parents[1]
REAL_ENTRY=Path(os.environ.get('GRADIA_RUFLO_TEST_ENTRY','/nonexistent/operator-configured-ruflo-entry'))
REAL_NODE=Path(os.environ.get('GRADIA_RUFLO_TEST_NODE','/nonexistent/operator-configured-node'))
class BridgeTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup); self.base=Path(self.temp.name)
  self.workspace=self.base/'tenant-a';self.workspace.mkdir();(self.base/'tenant-b').mkdir()
  self.runtime=self.base/'runtime';(self.runtime/'bin').mkdir(parents=True)
  self.entry=self.runtime/'bin'/'cli.py';self.entry.write_text('import sys\nprint("synthetic output")\n')
  (self.runtime/'package.json').write_text(json.dumps({'name':'@claude-flow/cli','version':'3.56.9'}))
  manifest=json.loads((ROOT/'provider.manifest.json').read_text());manifest['upstream']['entrySha256']=hashlib.sha256(self.entry.read_bytes()).hexdigest()
  self.manifest=self.base/'manifest.json';self.manifest.write_text(json.dumps(manifest))
  self.edition=RufloEdition(Path(sys.executable),self.entry,self.manifest)
  self.bridge=Bridge(Registry([self.edition]),{'a':self.workspace,'b':self.base/'tenant-b'},self.base/'journal.sqlite')
  self.principal=Principal('a',frozenset({'ruflo:inspect','ruflo:swarm:init'}))
 def request(self,operation='system.version',arguments=None):
  return {'edition':'3.56.9','operation':operation,'arguments':arguments or {},'expected_contract_hash':self.bridge.catalog('3.56.9')['contractSha256']}
 def test_pin_permission_and_unknown_operations_fail_closed(self):
  request=self.request();request['expected_contract_hash']='changed'
  with self.assertRaisesRegex(BridgeError,'contract_identity'):self.bridge.execute(self.principal,request)
  with self.assertRaisesRegex(BridgeError,'permission_denied'):self.bridge.execute(Principal('a',frozenset()),self.request())
  with self.assertRaisesRegex(BridgeError,'operation_not_registered'):self.bridge.execute(self.principal,self.request('agent.spawn'))
  self.entry.write_text('print("changed")')
  with self.assertRaisesRegex(BridgeError,'runtime_identity'):self.bridge.execute(self.principal,self.request())
 def test_no_arbitrary_arguments_or_workspace_overlap(self):
  with self.assertRaisesRegex(BridgeError,'invalid_arguments'):self.bridge.execute(self.principal,self.request(arguments={'cwd':'/tmp'}))
  with self.assertRaisesRegex(BridgeError,'workspace_overlap'):Bridge(Registry([self.edition]),{'a':self.workspace,'b':self.workspace},self.base/'j2')
  with self.assertRaisesRegex(BridgeError,'tenant_not_configured'):self.bridge.execute(Principal('other',frozenset()),self.request())
 def test_mutation_idempotent_replay_and_conflict_across_restart(self):
  request=self.request('swarm.init',{'topology':'hierarchical','max_agents':2})
  receipt=self.bridge.execute(self.principal,request,'mutation-one')
  restarted=Bridge(Registry([self.edition]),{'a':self.workspace},self.base/'journal.sqlite')
  self.assertEqual(receipt,restarted.execute(self.principal,request,'mutation-one'))
  self.assertNotIn('synthetic output',json.dumps(receipt));self.assertEqual(receipt['cost']['status'],'unknown')
  changed={**request,'arguments':{'topology':'ring','max_agents':2}}
  with self.assertRaisesRegex(BridgeError,'idempotency_conflict'):restarted.execute(self.principal,changed,'mutation-one')
 def test_output_and_timeout_are_bounded(self):
  out=_run([sys.executable,'-c','print("x"*100000)'],self.workspace,{},1)
  self.assertEqual(out[0],'output_limit');self.assertLessEqual(len(out[2]),65536)
  out=_run([sys.executable,'-c','import time;time.sleep(5)'],self.workspace,{},.1)
  self.assertEqual(out[0],'timeout')
 def test_api_auth_and_contract(self):
  client=TestClient(create_app(self.bridge,{'a-very-long-test-token':self.principal}))
  self.assertEqual(client.get('/v1/providers/ruflo/catalog').status_code,401)
  headers={'Authorization':'Bearer a-very-long-test-token'}
  self.assertEqual(client.get('/v1/providers/ruflo/catalog',headers=headers).status_code,200)
  response=client.post('/v1/providers/ruflo/execute',headers=headers,json=self.request())
  self.assertEqual(response.status_code,200)
  self.assertNotIn('synthetic output',response.text)
 def test_manifest_cannot_weaken_registered_mutation_authority(self):
  manifest=json.loads(self.manifest.read_text());op=manifest['operations'][2];op['effect']='inspect';op['permission']='ruflo:inspect';self.manifest.write_text(json.dumps(manifest))
  with self.assertRaisesRegex(BridgeError,'contract_authority_mismatch'):RufloEdition(Path(sys.executable),self.entry,self.manifest).contract()
 def test_new_host_registered_operation_needs_no_gateway_changes(self):
  manifest=json.loads(self.manifest.read_text());manifest['operations'].append({'id':'system.help','effect':'inspect','permission':'ruflo:inspect','arguments':{}});self.manifest.write_text(json.dumps(manifest))
  with self.assertRaisesRegex(BridgeError,'unregistered_contract'):RufloEdition(Path(sys.executable),self.entry,self.manifest).contract()
  def builder(arguments):
   if arguments: raise BridgeError('invalid_arguments')
   return ['--help']
  edition=RufloEdition(Path(sys.executable),self.entry,self.manifest,DEFAULT_OPERATIONS+(OperationSpec('system.help','ruflo:inspect','inspect',{},builder),))
  bridge=Bridge(Registry([edition]),{'a':self.workspace},self.base/'new-op.sqlite')
  client=TestClient(create_app(bridge,{'a-very-long-test-token':self.principal}))
  request={'edition':'3.56.9','operation':'system.help','arguments':{},'expected_contract_hash':bridge.catalog('3.56.9')['contractSha256']}
  response=client.post('/v1/providers/ruflo/execute',headers={'Authorization':'Bearer a-very-long-test-token'},json=request)
  self.assertEqual(response.status_code,200);self.assertEqual(response.json()['operation'],'system.help')
 def test_attempt_telemetry_is_tenant_scoped_bounded_and_private(self):
  other=Principal('b',frozenset({'ruflo:inspect'}));client=TestClient(create_app(self.bridge,{'a-very-long-test-token':self.principal,'b-very-long-test-token':other}))
  headers={'Authorization':'Bearer a-very-long-test-token'}
  for _ in range(102):client.get('/v1/providers/ruflo/catalog?privatequery=never-record-this',headers=headers)
  events=client.get('/v1/providers/ruflo/diagnostics',headers=headers).json()['events']
  self.assertEqual(len(events),100);self.assertNotIn('never-record-this',json.dumps(events))
  self.assertEqual(client.get('/v1/providers/ruflo/diagnostics',headers={'Authorization':'Bearer b-very-long-test-token'}).json()['events'],[])
  client.get('/v1/providers/ruflo/catalog');self.assertEqual(len(client.get('/v1/providers/ruflo/diagnostics',headers=headers).json()['events']),100)
 def test_runtime_failure_after_reservation_is_durable_uncertain(self):
  request=self.request('swarm.init',{'topology':'ring','max_agents':2})
  self.entry.write_text('changed')
  receipt=self.bridge.execute(self.principal,request,'interrupted')
  self.assertEqual(receipt['result'],'uncertain');self.assertFalse(receipt['retry_safe'])
  self.assertEqual(self.bridge.execute(self.principal,request,'interrupted'),receipt)
 def test_side_by_side_edition_and_provider_use_unchanged_gateway(self):
  def runtime(folder,package,version,provider,operations):
   folder=self.base/folder;(folder/'bin').mkdir(parents=True)
   entry=folder/'bin'/'cli.py';entry.write_text('print("synthetic extension")')
   (folder/'package.json').write_text(json.dumps({'name':package,'version':version}))
   manifest={'id':provider,'edition':version,'upstream':{'package':package,'version':version,'entrySha256':hashlib.sha256(entry.read_bytes()).hexdigest()},'operations':[{'id':op.id,'permission':op.permission,'effect':op.effect,'arguments':op.arguments} for op in operations]}
   path=folder/'manifest.json';path.write_text(json.dumps(manifest));return entry,path
  entry,path=runtime('new-ruflo','@claude-flow/cli','3.56.10','ruflo',DEFAULT_OPERATIONS)
  newer=RufloEdition(Path(sys.executable),entry,path,expected_version='3.56.10')
  ops=(OperationSpec('system.version','fixture:inspect','inspect',{},lambda args:['--version']),)
  entry,path=runtime('other-provider','@fixture/cli','1.0.0','fixture',ops)
  other=LocalCliEdition(Path(sys.executable),entry,path,ops,provider_id='fixture',expected_version='1.0.0',expected_package='@fixture/cli')
  bridge=Bridge(Registry([self.edition,newer,other]),{'a':self.workspace},self.base/'multi.sqlite')
  client=TestClient(create_app(bridge,{'a-very-long-test-token':Principal('a',frozenset({'ruflo:inspect','fixture:inspect'}))}))
  headers={'Authorization':'Bearer a-very-long-test-token'}
  for provider,version in [('ruflo','3.56.9'),('ruflo','3.56.10'),('fixture','1.0.0')]:
   catalog=client.get(f'/v1/providers/{provider}/catalog?edition={version}',headers=headers)
   self.assertEqual(catalog.status_code,200)
   request={'edition':version,'operation':'system.version','arguments':{},'expected_contract_hash':catalog.json()['contractSha256']}
   response=client.post(f'/v1/providers/{provider}/execute',headers=headers,json=request)
   self.assertEqual(response.status_code,200);self.assertEqual(response.json()['provider'],provider);self.assertEqual(response.json()['edition'],version)
  self.assertEqual(client.get('/v1/providers/ruflo/catalog?edition=unregistered',headers=headers).status_code,409)
 def test_host_schema_rejects_before_permissive_builder(self):
  calls=[]
  def builder(arguments):calls.append(arguments);return ['--version']
  spec=OperationSpec('bounded.read','ruflo:inspect','inspect',{'count':{'type':'integer','minimum':1,'maximum':4},'kind':{'enum':['safe']}},builder)
  edition=RufloEdition(Path(sys.executable),self.entry,self.manifest,(spec,))
  for args in [{'count':True,'kind':'safe'},{'count':5,'kind':'safe'},{'count':2,'kind':'unsafe'},{'count':2,'kind':'safe','extra':1}]:
   with self.assertRaisesRegex(BridgeError,'invalid_arguments'):edition.argv('bounded.read',args)
  self.assertEqual(calls,[])
  self.assertEqual(edition.argv('bounded.read',{'count':2,'kind':'safe'}),['--version'])
 def test_real_pinned_cli_in_private_temporary_workspace(self):
  if not REAL_ENTRY.exists() or not REAL_NODE.exists():self.skipTest('Pinned local Ruflo runtime absent')
  bridge=Bridge(Registry([RufloEdition(REAL_NODE,REAL_ENTRY,ROOT/'provider.manifest.json')]),{'a':self.workspace},self.base/'real.sqlite')
  def req(op,args):return {'edition':'3.56.9','operation':op,'arguments':args,'expected_contract_hash':bridge.catalog('3.56.9')['contractSha256']}
  results=[bridge.execute(self.principal,req('system.version',{})),bridge.execute(self.principal,req('swarm.status',{})),bridge.execute(self.principal,req('swarm.init',{'topology':'hierarchical','max_agents':2}),'real-test-init')]
  for receipt in results:self.assertEqual(receipt['result'],'completed',receipt)
  self.assertEqual(results[1]['data']['status'],'idle');self.assertNotIn('objective',results[1]['data'])
  after=bridge.execute(self.principal,req('swarm.status',{}));self.assertEqual(after['data']['status'],'ready');self.assertTrue(after['data']['hasActiveSwarm']);self.assertEqual(after['data']['topology'],'hierarchical');self.assertTrue(after['data']['id'].startswith('swarm-'))
  results.append(after)
  self.assertEqual(bridge.execute(self.principal,req('swarm.init',{'topology':'hierarchical','max_agents':2}),'real-test-init'),results[2])
  print('REAL_RUFLO_SMOKE',json.dumps(results))
if __name__=='__main__':unittest.main()

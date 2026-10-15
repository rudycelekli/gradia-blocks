import copy
import json
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest

from gradia_composition import CompositionError, build_lock, compare_graphs, digest, read_json, verify_lock

ROOT = Path(__file__).resolve().parents[1]


class LockAndCliTests(unittest.TestCase):
    def setUp(self):
        self.graph = read_json(ROOT/'examples/baseline.json')
        self.candidate = read_json(ROOT/'examples/candidate-stale-pins.json')
        self.roots = ['custom-triage']

    def invoke(self, *args):
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        return subprocess.run([sys.executable, '-m', 'gradia_composition', *args],
                              capture_output=True, text=True, env=env, timeout=5)

    def test_checked_example_lock_and_report_reproduce(self):
        self.assertEqual(build_lock(self.graph,self.roots,'demo-tenant'), read_json(ROOT/'examples/baseline.lock.json'))
        report = compare_graphs(self.graph,self.candidate,self.roots,'demo-tenant')
        self.assertEqual(report,read_json(ROOT/'examples/impact.json'))
        self.assertEqual(report['affected'],['custom-triage','feedback-triage','ruflo-bridge'])
        self.assertIsNone(report['candidateLock'])

    def test_lock_verification_refuses_tampered_lock_and_graph(self):
        lock = build_lock(self.graph,self.roots,'demo-tenant')
        self.assertEqual(verify_lock(lock,self.graph,self.roots,'demo-tenant')['status'],'REPRODUCED')
        changed = copy.deepcopy(lock); changed['promotionAuthorized'] = True
        body = {k:v for k,v in changed.items() if k!='lockSha256'}
        changed['lockSha256'] = digest(body)
        with self.assertRaisesRegex(CompositionError,'lock_identity_mismatch'):
            verify_lock(changed,self.graph,self.roots,'demo-tenant')
        graph = copy.deepcopy(self.graph);graph['nodes'][1]['artifactSha256']='f'*64
        with self.assertRaises(CompositionError):
            verify_lock(lock,graph,self.roots,'demo-tenant')

    def test_repinning_requires_consumers_and_still_has_unknown_compatibility(self):
        graph = copy.deepcopy(self.candidate)
        bridge = graph['nodes'][1]
        for node in graph['nodes']:
            for pin in node['requires']:
                if pin['id']==bridge['id']:
                    pin.update({k:bridge[k] for k in ('edition','artifactSha256','contractSha256')})
        report = compare_graphs(self.graph,graph,self.roots,'demo-tenant')
        self.assertEqual(report['blockers'],[])
        self.assertIsNotNone(report['candidateLock'])
        self.assertEqual(report['compatibility'],'UNKNOWN')
        self.assertFalse(report['promotionAuthorized'])

    def test_unwired_input_is_reported_without_execution_claim(self):
        graph=copy.deepcopy(self.graph);graph['connections']=[]
        lock=build_lock(graph,self.roots,'demo-tenant')
        self.assertEqual(len(lock['unresolvedInputs']),2)
        self.assertFalse(lock['promotionAuthorized'])

    def test_cli_lock_impact_and_diagram_are_actual_separate_processes(self):
        common=['--graph',str(ROOT/'examples/baseline.json'),'--scope','demo-tenant']
        locked=self.invoke('lock',*common,'--root','custom-triage')
        self.assertEqual(locked.returncode,0,locked.stderr)
        self.assertEqual(json.loads(locked.stdout),build_lock(self.graph,self.roots,'demo-tenant'))
        impact=self.invoke('impact',*common,'--candidate',str(ROOT/'examples/candidate-stale-pins.json'),'--root','custom-triage')
        self.assertEqual(impact.returncode,0,impact.stderr)
        self.assertTrue(json.loads(impact.stdout)['blockers'])
        diagram=self.invoke('diagram',*common)
        self.assertEqual(diagram.returncode,0,diagram.stderr)
        self.assertEqual(diagram.stdout,(ROOT/'examples/baseline.mmd').read_text())

    def test_cli_error_does_not_emit_partial_lock_or_graph(self):
        response=self.invoke('lock','--graph',str(ROOT/'examples/baseline.json'),'--scope','other-tenant','--root','custom-triage')
        self.assertEqual(response.returncode,2)
        self.assertEqual(response.stdout,'')
        error=json.loads(response.stderr)
        self.assertEqual(error['reason'],'scope_denied')
        self.assertFalse(error['promotionAuthorized'])
        self.assertNotIn('custom-triage',response.stderr)

    def test_file_reader_refuses_fifo_without_blocking(self):
        if not hasattr(os,'mkfifo'):
            self.skipTest('POSIX FIFO unavailable')
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'pipe';os.mkfifo(path)
            with self.assertRaises(CompositionError):
                read_json(path)


if __name__=='__main__':
    unittest.main()

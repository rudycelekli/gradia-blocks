import unittest
from pathlib import Path
from gradia_composition import digest, observe_plan, read_json

ROOT=Path(__file__).resolve().parents[1]


class ObservationTests(unittest.TestCase):
    def test_success_receipt_matches_artifact_without_changing_lock_identity(self):
        graph=read_json(ROOT/'examples/baseline.json')
        first=observe_plan('lock',graph=graph,expected_scope='demo-tenant',roots=['custom-triage'])
        second=observe_plan('lock',graph=graph,expected_scope='demo-tenant',roots=['custom-triage'])
        self.assertEqual(first['result'],second['result'])
        receipt=first['receipt']
        self.assertEqual(receipt['artifactSha256'],digest(first['result']))
        self.assertNotEqual(receipt['correlationId'],second['receipt']['correlationId'])
        self.assertEqual(receipt['cost'],{'status':'unknown','amount':None})
        self.assertGreaterEqual(receipt['durationMs'],0)
        self.assertNotIn('custom-triage',str(receipt))

    def test_refusal_receipt_does_not_expose_foreign_ids_or_inputs(self):
        graph=read_json(ROOT/'examples/baseline.json')
        output=observe_plan('lock',graph=graph,expected_scope='foreign',roots=['custom-triage'])
        self.assertIsNone(output['result'])
        self.assertEqual(output['receipt']['status'],'REFUSED')
        self.assertEqual(output['receipt']['reason'],'scope_denied')
        self.assertNotIn('custom-triage',str(output));self.assertNotIn('demo-tenant',str(output))

    def test_unknown_operation_never_reflected_and_diagram_hash_is_explicit(self):
        output=observe_plan('secret-query-value',graph={},expected_scope='public')
        self.assertNotIn('secret-query-value',str(output))
        graph=read_json(ROOT/'examples/baseline.json')
        diagram=observe_plan('diagram',graph=graph,expected_scope='demo-tenant')
        self.assertEqual(diagram['receipt']['status'],'PRODUCED')
        self.assertEqual(len(diagram['receipt']['artifactSha256']),64)


if __name__=='__main__':
    unittest.main()

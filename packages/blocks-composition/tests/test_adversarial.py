"""Independent known-bad controls for the offline composition reference.

These tests assess refusal, graph reachability and honest claim boundaries. They
neither execute a workflow nor certify behavior of an upstream implementation.
"""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from gradia_composition.core import (
    CompositionError, build_lock, compare_graphs, diagram, digest, read_json,
    validate_graph, verify_lock,
)

A = 'a' * 64
B = 'b' * 64
C = 'c' * 64


def node(name, *, kind='block', scope='public', requires=()):
    return {'id': name, 'kind': kind, 'edition': '1.0.0',
            'artifactSha256': A, 'contractSha256': B, 'scope': scope,
            'requires': list(requires), 'inputs': {'in': C}, 'outputs': {'out': C}}


def pin(item):
    return {key: item[key] for key in ('id', 'edition', 'artifactSha256', 'contractSha256')}


def graph(*nodes, scope='public', connections=()):
    return {'schema': 'gradia.composition.graph.v1', 'scope': scope,
            'nodes': list(nodes), 'connections': list(connections)}


def chain(scope='public'):
    base = node('source')
    adapter = node('adapter', kind='bridge', requires=[pin(base)])
    flow = node('flow', kind='workflow', scope=scope, requires=[pin(adapter)])
    return graph(base, adapter, flow, scope=scope)


class IndependentAdversarialTests(unittest.TestCase):
    def reject(self, value, scope='public'):
        with self.assertRaises(CompositionError):
            validate_graph(value, scope)

    def test_duplicate_json_keys_and_nonfinite_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'input.json'
            for text in ('{"schema":1,"schema":2}', '{"x":NaN}', '{"x":Infinity}', '{"x":-Infinity}'):
                with self.subTest(text=text):
                    path.write_text(text)
                    with self.assertRaises(CompositionError):
                        read_json(path)

    def test_json_size_and_excessive_depth_refused_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'input.json'
            for text in ('{"x":"' + 'a' * (3 * 1024 * 1024) + '"}', '[' * 2000 + '0' + ']' * 2000):
                path.write_text(text)
                with self.assertRaises(CompositionError):
                    read_json(path)

    def test_wrong_types_are_normalized_to_refusal(self):
        for value in (None, [], 'graph', True, {'schema': 'gradia.composition.graph.v1'}):
            with self.subTest(value=value):
                self.reject(value)
        for field, value in (('nodes', None), ('connections', {}), ('scope', [])):
            candidate = chain()
            candidate[field] = value
            with self.subTest(field=field):
                self.reject(candidate)

    def test_identifier_and_mermaid_injection_refused(self):
        for name in ('../escape', '/tmp/escape', 'bad\nname', 'x"]-->evil["', 'x; touch /tmp/nope', 'a' * 1000):
            with self.subTest(name=name):
                self.reject(graph(node(name)))

    def test_duplicate_nodes_and_oversized_graph_refused(self):
        self.reject(graph(node('same'), node('same')))
        self.reject(graph(*(node('n' + str(i)) for i in range(129))))

    def test_missing_dependency_and_self_cycle_refused(self):
        item = node('item')
        item['requires'] = [pin(node('missing'))]
        self.reject(graph(item))
        item['requires'] = [pin(item)]
        self.reject(graph(item))

    def test_dependency_cycle_refused(self):
        first, second = node('first'), node('second')
        first['requires'] = [pin(second)]
        second['requires'] = [pin(first)]
        self.reject(graph(first, second))

    def test_digest_and_dependency_pin_tampering_refused(self):
        for field in ('artifactSha256', 'contractSha256'):
            candidate = chain()
            candidate['nodes'][0][field] = 'wrong'
            self.reject(candidate)
            candidate = chain()
            candidate['nodes'][1]['requires'][0][field] = 'd' * 64
            self.reject(candidate)

    def test_unknown_fields_cannot_inject_compatibility_or_authority(self):
        for field, value in (('compatibility', 'PASS'), ('promotionAuthorized', True), ('execute', 'shell')):
            candidate = chain()
            candidate[field] = value
            with self.subTest(field=field):
                self.reject(candidate)

    def test_private_graph_foreign_scope_and_public_dependency_refused(self):
        private = chain('tenant-alpha')
        self.reject(private, 'public')
        self.reject(private, 'tenant-beta')
        secret = node('secret', kind='variant', scope='tenant-alpha')
        public = node('public-consumer', requires=[pin(secret)])
        self.reject(graph(secret, public, scope='tenant-alpha'), 'tenant-alpha')

    def test_public_closure_does_not_include_unselected_private_variant(self):
        private = chain('tenant-alpha')
        secret = node('private-variant', kind='variant', scope='tenant-alpha')
        private['nodes'].append(secret)
        lock = build_lock(private, ['flow'], 'tenant-alpha')
        self.assertNotIn('private-variant', json.dumps(lock))

    def test_lock_and_report_never_authorize_or_claim_behavior(self):
        source = chain()
        for value in (build_lock(source, ['flow'], 'public'), compare_graphs(source, copy.deepcopy(source), ['flow'], 'public')):
            self.assertIn('UNKNOWN', json.dumps(value))
            self.assertIs(value.get('promotionAuthorized'), False)

    def test_missing_or_too_many_roots_refused(self):
        for roots in ([], ['missing'], ['flow'] * 9):
            with self.subTest(roots=roots):
                with self.assertRaises(CompositionError):
                    build_lock(chain(), roots, 'public')

    def test_connection_missing_node_port_or_wrong_schema_refused(self):
        first, second = node('first'), node('second')
        second['requires'] = [pin(first)]
        link = {'fromNode': 'first', 'fromPort': 'out', 'toNode': 'second', 'toPort': 'in'}
        for field, value in (('fromNode', 'missing'), ('fromPort', 'missing'), ('toNode', 'missing'), ('toPort', 'missing')):
            changed = dict(link)
            changed[field] = value
            self.reject(graph(first, second, connections=[changed]))
        second['inputs']['in'] = A
        self.reject(graph(first, second, connections=[link]))

    def test_impact_propagates_transitively_and_keeps_unrelated_nodes_out(self):
        before = chain()
        before['nodes'].append(node('unrelated'))
        after = copy.deepcopy(before)
        after['nodes'][0]['artifactSha256'] = 'd' * 64
        report = compare_graphs(before, after, ['flow'], 'public')
        self.assertEqual(report['changed'], ['source'])
        self.assertEqual(report['affected'], ['adapter', 'flow', 'source'])
        self.assertEqual(report['affectedRoots'], ['flow'])
        self.assertIsNone(report['candidateLock'])
        self.assertIn('stale_dependency_pin', [item['kind'] for item in report['blockers']])
        self.assertEqual(report['candidateGraphSha256'], digest(after))

    def test_removal_follows_baseline_edges_and_blocks_removed_root(self):
        before = chain()
        after = graph(node('source'))
        report = compare_graphs(before, after, ['flow'], 'public')
        self.assertEqual(report['affected'], ['adapter', 'flow'])
        self.assertIsNone(report['candidateLock'])
        self.assertIn('removed_root', [item['kind'] for item in report['blockers']])

    def test_wiring_only_change_affects_consumer_and_its_consumers(self):
        before = chain()
        before['connections'] = [{'fromNode': 'source', 'fromPort': 'out',
                                  'toNode': 'adapter', 'toPort': 'in'}]
        after = copy.deepcopy(before)
        after['connections'] = []
        report = compare_graphs(before, after, ['flow'], 'public')
        self.assertEqual(report['changed'], ['adapter'])
        self.assertEqual(report['affected'], ['adapter', 'flow'])
        self.assertEqual(report['compatibility'], 'UNKNOWN')
        self.assertIn({'node': 'adapter', 'port': 'in'}, report['candidateLock']['unresolvedInputs'])

    def test_new_private_variant_is_an_affected_consumer_in_its_scope(self):
        before = chain('tenant-alpha')
        after = copy.deepcopy(before)
        variant = node('custom', kind='variant', scope='tenant-alpha', requires=[pin(after['nodes'][0])])
        after['nodes'].append(variant)
        after['nodes'][0]['artifactSha256'] = 'd' * 64
        report = compare_graphs(before, after, ['flow'], 'tenant-alpha')
        self.assertEqual(report['affected'], ['adapter', 'custom', 'flow', 'source'])
        with self.assertRaisesRegex(CompositionError, '^scope_denied$'):
            compare_graphs(before, after, ['flow'], 'tenant-beta')

    def test_exact_lock_and_report_hashes_detect_output_tampering(self):
        source = chain()
        lock = build_lock(source, ['flow'], 'public')
        body = {key: value for key, value in lock.items() if key != 'lockSha256'}
        self.assertEqual(lock['lockSha256'], digest(body))
        body['nodes'][0]['artifactSha256'] = 'd' * 64
        self.assertNotEqual(lock['lockSha256'], digest(body))
        report = compare_graphs(source, copy.deepcopy(source), ['flow'], 'public')
        body = {key: value for key, value in report.items() if key != 'reportSha256'}
        self.assertEqual(report['reportSha256'], digest(body))

    def test_direct_host_cycles_and_aliases_refuse_before_serialization(self):
        value = {}
        value['cycle'] = value
        with self.assertRaises(CompositionError):
            digest(value)
        child = {'x': 1}
        with self.assertRaises(CompositionError):
            digest([child, child])

    def test_symlink_and_hardlink_input_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source.json'
            source.write_text('{}')
            symbolic = Path(directory) / 'symbolic.json'
            symbolic.symlink_to(source)
            with self.assertRaises(CompositionError):
                read_json(symbolic)
            import os
            linked = Path(directory) / 'linked.json'
            os.link(source, linked)
            with self.assertRaises(CompositionError):
                read_json(linked)

    def test_connection_cannot_hide_dependency_or_have_multiple_writers(self):
        first, second = node('first'), node('second')
        link = {'fromNode': 'first', 'fromPort': 'out', 'toNode': 'second', 'toPort': 'in'}
        self.reject(graph(first, second, connections=[link]))
        second['requires'] = [pin(first)]
        self.reject(graph(first, second, connections=[link, dict(link)]))
        third = node('third')
        second['requires'].append(pin(third))
        other = dict(link, fromNode='third')
        self.reject(graph(first, second, third, connections=[link, other]))

    def test_validation_snapshot_is_not_mutated_by_caller(self):
        source = chain()
        validated = validate_graph(source, 'public')
        source['nodes'][0]['edition'] = '2.0.0'
        self.assertEqual(validated['nodes'][0]['edition'], '1.0.0')

    def test_stale_port_contract_candidate_has_no_eligible_lock(self):
        before = chain()
        before['connections'] = [{'fromNode': 'source', 'fromPort': 'out',
                                  'toNode': 'adapter', 'toPort': 'in'}]
        after = copy.deepcopy(before)
        after['nodes'][0]['outputs']['out'] = A
        report = compare_graphs(before, after, ['flow'], 'public')
        self.assertIsNone(report['candidateLock'])
        self.assertIn('port_schema_mismatch', [item['kind'] for item in report['blockers']])
        self.assertEqual(report['affected'], ['adapter', 'flow', 'source'])

    def test_lock_verification_does_not_conflate_false_with_zero(self):
        source = chain()
        from gradia_composition.observability import observe_plan
        for value in (0, 1):
            for recompute in (False, True):
                with self.subTest(value=value, recompute=recompute):
                    lock = build_lock(source, ['flow'], 'public')
                    lock['promotionAuthorized'] = value
                    if recompute:
                        lock['lockSha256'] = digest({key: item for key, item in lock.items() if key != 'lockSha256'})
                    with self.assertRaises(CompositionError):
                        verify_lock(lock, source, ['flow'], 'public')
                    envelope = observe_plan('verify-lock', graph=source, roots=['flow'], expected_scope='public', lock=lock)
                    self.assertIsNone(envelope['result'])
                    self.assertEqual(envelope['receipt']['reason'], 'lock_identity_mismatch')
                    self.assertIs(envelope['receipt']['promotionAuthorized'], False)

    def test_observation_rejects_tampered_lock_and_does_not_log_identifiers(self):
        from gradia_composition.observability import observe_plan
        source = chain('tenant-private')
        lock = build_lock(source, ['flow'], 'tenant-private')
        lock['nodes'][0]['artifactSha256'] = 'd' * 64
        observation = observe_plan('verify-lock', graph=source, roots=['flow'],
                                   expected_scope='tenant-private', lock=lock)
        self.assertIsNone(observation['result'])
        receipt = observation['receipt']
        self.assertEqual(receipt['status'], 'REFUSED')
        self.assertIsNone(receipt['artifactSha256'])
        self.assertIs(receipt['promotionAuthorized'], False)
        self.assertEqual(receipt['cost'], {'status': 'unknown', 'amount': None})
        text = json.dumps(receipt)
        for secret in ('tenant-private', 'source', 'adapter', 'flow', A, B, C):
            self.assertNotIn(secret, text)

    def test_diagram_emits_only_validated_data(self):
        emitted = diagram(chain(), 'public')
        self.assertIn('flowchart', emitted)
        self.assertNotIn('promotionAuthorized true', emitted)
        with self.assertRaises(CompositionError):
            diagram(graph(node('evil\nclick x "javascript:alert(1)"')), 'public')


if __name__ == '__main__':
    unittest.main()

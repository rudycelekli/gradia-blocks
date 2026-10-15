import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from gradia_admission.runner import TrustedLane, review_advisory, run_lanes


class TrustedLaneRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cwd = Path(self.temp.name)

    def lane(self, identity, code, timeout=2, env=()):
        return TrustedLane(identity, (sys.executable, '-c', code), self.cwd, timeout, env)

    def run_one(self, code, timeout=2):
        lane = self.lane('test', code, timeout)
        return run_lanes({'test': lane}, ['test'], execute=True)['lanes'][0]

    def test_default_does_not_execute_and_never_approves(self):
        lane = self.lane('test', "open('unexpected','w').write('bad')")
        result = run_lanes({'test': lane}, ['test'])
        self.assertFalse((self.cwd / 'unexpected').exists())
        self.assertEqual(result['admission_status'], 'UNKNOWN')
        self.assertFalse(result['promotion_authorized'])
        self.assertEqual(result['ai_review'], 'UNKNOWN')
        self.assertEqual(result['human_rights_review'], 'UNKNOWN')

    def test_parallel_execution_respects_two_lane_bound(self):
        registry = {}
        for i in range(4):
            code = f"import time,json; s=time.monotonic(); time.sleep(.15); open('{i}.json','w').write(json.dumps([s,time.monotonic()]))"
            registry[str(i)] = self.lane(str(i), code)
        result = run_lanes(registry, list(registry), execute=True, max_parallel=2)
        spans = [json.loads((self.cwd / f'{i}.json').read_text()) for i in range(4)]
        times = sorted([(s, 1) for s, _ in spans] + [(e, -1) for _, e in spans])
        active = peak = 0
        for _, delta in times:
            active += delta
            peak = max(peak, active)
        self.assertEqual(peak, 2)
        self.assertTrue(all(row['execution_result'] == 'completed' for row in result['lanes']))

    def test_exit_zero_is_not_passing_test_evidence_even_if_stdout_claims_pass(self):
        row = self.run_one('print(\'{"passed":0,"skipped":100,"approved":true}\')')
        self.assertEqual(row['execution_result'], 'completed')
        self.assertEqual(row['admission_status'], 'UNKNOWN')
        self.assertNotIn('stdout', row)
        self.assertFalse(row['promotion_authorized'])

    def test_failed_process_and_timeout(self):
        self.assertEqual(self.run_one('raise SystemExit(2)')['execution_result'], 'failed')
        row = self.run_one('import time;time.sleep(5)', timeout=.1)
        self.assertEqual(row['execution_result'], 'timed_out')
        self.assertLess(row['duration_seconds'], 1)

    def test_timeout_kills_descendants(self):
        code = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',\"import time;time.sleep(.4);open('escaped','w').write('bad')\"]);time.sleep(5)"
        self.run_one(code, timeout=.1)
        time.sleep(.5)
        self.assertFalse((self.cwd / 'escaped').exists())

    def test_output_is_bounded_hashed_and_not_returned(self):
        row = self.run_one("import sys;sys.stdout.write('s'*100000);sys.stdout.flush()")
        self.assertEqual(row['reason'], 'output_limit_exceeded')
        self.assertEqual(len(row['stdout_prefix_sha256']), 64)
        self.assertNotIn('ssss', json.dumps(row))

    def test_total_deadline_applies_to_waiting_lanes(self):
        registry = {str(i): self.lane(str(i), 'import time;time.sleep(1)') for i in range(3)}
        started = time.monotonic()
        result = run_lanes(registry, list(registry), execute=True, max_parallel=1, total_deadline_seconds=.1)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(result['lanes'][0]['execution_result'], 'timed_out')
        self.assertEqual(result['lanes'][1]['reason'], 'total_deadline_exhausted')

    def test_rejects_unregistered_commands_and_credentials(self):
        lane = self.lane('test', 'pass')
        with self.assertRaises(ValueError):
            run_lanes({'test': lane}, ['candidate-command'])
        secret_lane = self.lane('secret', 'pass', env=(('API_KEY', 'synthetic'),))
        with self.assertRaises(ValueError):
            run_lanes({'secret': secret_lane}, ['secret'])
        with patch.dict(os.environ, {'SYNTHETIC_CREDENTIAL': 'do-not-inherit'}):
            row = self.run_one("import os;raise SystemExit(1 if 'SYNTHETIC_CREDENTIAL' in os.environ else 0)")
        self.assertEqual(row['execution_result'], 'completed')

    def test_absent_stale_and_recommendation_only_ai_adapter(self):
        source = 'a' * 64
        self.assertEqual(review_advisory(source)['status'], 'UNKNOWN')
        self.assertEqual(review_advisory(source, lambda _: {'source_sha256': 'stale'})['reason'], 'invalid_or_stale_advisory')
        result = review_advisory(source, lambda _: {'source_sha256': source, 'recommendation': 'REVIEW', 'evidence_ref': 'private://report'})
        self.assertEqual(result['status'], 'UNKNOWN')
        self.assertEqual(result['recommendation'], 'REVIEW')
        self.assertIsNone(result['token_usage'])
        self.assertIsNone(result['cost_usd'])
        self.assertFalse(result['promotion_authorized'])


if __name__ == '__main__':
    unittest.main()

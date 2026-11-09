"""Automatic provider verdicts are bound to their recorded PR base."""
import json
from pathlib import Path
import unittest

import test_merge_gate as merge_tests
import test_review_run_history as history


class RetargetedAutomaticRuns(unittest.TestCase):
    setUp = history.LatestAutomaticGate.setUp
    pr = history.LatestAutomaticGate.pr
    decide = history.LatestAutomaticGate.decide
    write_config = history.LatestAutomaticGate.write_config
    evidence = history.LatestAutomaticGate.evidence
    record = history.LatestAutomaticGate.record
    reviewed = history.LatestAutomaticGate.reviewed
    write_pr = history.LatestAutomaticGate.write_pr

    def retarget(self):
        path = Path(self.pr_json)
        pr = json.loads(path.read_text())
        pr.update(baseRefName='production', baseRefOid='b' * 40)
        path.write_text(json.dumps(pr))

    def test_old_run_verdicts_cannot_become_manual_passes_after_retargeting(self):
        for status in ('RUNNING', 'PASS'):
            self.write_pr([self.record('RUNNING'), self.reviewed('Codex'), self.reviewed('Claude'),
                           self.record(status, providers={'Codex': 'PASS', 'Claude': 'PASS'})])
            self.retarget()
            with self.subTest(status=status):
                result = self.decide()
                self.assertIsNotNone(result)
                self.assertEqual(result['permissionDecision'], 'deny')

    def test_matching_run_passes_and_manual_legacy_passes_still_work(self):
        self.write_pr([self.record('RUNNING'), self.reviewed('Codex'), self.record('PASS')])
        self.assertIsNone(self.decide())
        self.write_pr([merge_tests.comment('Codex', 'PASS'), merge_tests.comment('Claude', 'PASS')])
        self.assertIsNone(self.decide())


if __name__ == '__main__':
    unittest.main()

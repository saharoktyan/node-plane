import copy
import importlib.util
import json
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('archive_update_recovery',
    Path(__file__).resolve().parents[1] / 'scripts/recover_archive_update.py')
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


class ArchiveUpdateRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.unit = 'node-plane-update-20261008-023431'
        self.job = {'id': '83c6b371-4f28-4067-8c72-ecff537081bb', 'kind': 'stack', 'status': 'blocked',
                    'result_json': json.dumps({'phase': 'core', 'resolved_ref': 'v0.4.3-alpha.52',
                        'expected_commit': 'a' * 40,
                        'unit_name': self.unit, 'error_code': 'update_verification_unavailable'})}
        self.journal = (f'Started {self.unit}.service - [systemd-run] update.sh '
                        f'--stack-job {self.job["id"]}.\n'
                        'Controller release unavailable: Unexpected or unsafe controller archive member: '
                        'scripts/lib/archive_agent_journals.py\n'
                        'Main process exited, code=exited, status=1/FAILURE\n')

    def test_only_known_pre_installation_failure_is_eligible_and_records_are_unchanged(self):
        original = copy.deepcopy(self.job)
        self.assertEqual(recovery.validate(self.job, [], self.journal, 'ActiveState=inactive'), self.unit)
        self.assertEqual(self.job, original)

    def test_active_units_or_dispatched_agents_cannot_be_replayed(self):
        for active in ('active', 'activating', 'deactivating', 'reloading'):
            with self.subTest(active=active), self.assertRaises(ValueError):
                recovery.validate(self.job, [], self.journal, 'ActiveState=' + active)
        for item in ({'status': 'running', 'child_id': None},
                     {'status': 'awaiting_executor', 'child_id': 'existing-child'}):
            with self.subTest(item=item), self.assertRaises(ValueError):
                recovery.validate(self.job, [item], self.journal, 'ActiveState=failed')

    def test_other_outcomes_units_versions_or_ambiguous_journal_are_refused(self):
        for field, value in (('phase', 'agents'), ('resolved_ref', 'v0.4.3-alpha.53'),
                             ('unit_name', 'unrelated.service'), ('expected_commit', ''),
                             ('error_code', 'different_failure')):
            job = copy.deepcopy(self.job)
            plan = json.loads(job['result_json'])
            plan[field] = value
            job['result_json'] = json.dumps(plan)
            with self.subTest(field=field), self.assertRaises(ValueError):
                recovery.validate(job, [], self.journal, 'ActiveState=failed')
        for journal in ('', self.journal.replace(self.job['id'], 'another-job'),
                        self.journal + f'Started {self.unit}.service\n',
                        self.journal + 'Preparing new release:', self.journal + 'Update complete.'):
            with self.subTest(journal=journal), self.assertRaises(ValueError):
                recovery.validate(self.job, [], journal, 'ActiveState=inactive')

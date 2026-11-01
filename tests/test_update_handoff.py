"""Regression tests for controller update handoff protocol.

Tests prepare_target_handoff and claim_target_handoff from scripts/update.sh.
Verifies JSON handoff creation, identity check against caller variables,
caller shell variable preservation across branch/job variations,
rejection of identity mismatches, and rejection of consumed handoffs.
"""
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from unittest import TestCase
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
UPDATE_SCRIPT = (ROOT / 'scripts' / 'update.sh').read_text()
PREPARE_FN = UPDATE_SCRIPT[UPDATE_SCRIPT.index('prepare_target_handoff() {'):UPDATE_SCRIPT.index('claim_target_handoff() {')]
CLAIM_FN = UPDATE_SCRIPT[UPDATE_SCRIPT.index('claim_target_handoff() {'):UPDATE_SCRIPT.index('mark_target_handoff() {')]


class UpdateHandoffTests(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.base_dir = self.root / 'base'
        self.app_dir = self.root / 'app'
        self.shared_dir = self.root / 'shared'
        self.stage_dir = self.root / 'stage'
        for directory in (self.base_dir, self.app_dir, self.shared_dir, self.stage_dir):
            directory.mkdir(parents=True)
        self.commit = '1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b'
        (self.stage_dir / 'BUILD_COMMIT').write_text(self.commit + '\n')

    def tearDown(self):
        self.temp_dir.cleanup()

    def _build_shell_harness(self, *, stage_dir=None, commit=None, branch='', job='', ref='v0.2.1',
                             timeout='45', skip_restart='1', skip_deps='0', mode='simple',
                             claim_branch=None, claim_ref=None, claim_job=None, claim_stage=None,
                             claim_twice=False):
        stage = stage_dir or self.stage_dir
        build_commit = commit or self.commit
        claim_b = branch if claim_branch is None else claim_branch
        claim_r = ref if claim_ref is None else claim_ref
        claim_j = job if claim_job is None else claim_job
        claim_s = stage if claim_stage is None else claim_stage

        second_claim_snippet = 'claim_target_handoff "$handoff"\n' if claim_twice else ''

        return f"""set -euo pipefail

simple_paths() {{
  printf '%s\\n' '{self.base_dir}' '{self.app_dir}' '{self.shared_dir}'
}}

read_env_value() {{
  case "$1" in
    NODE_PLANE_BASE_DIR) printf '%s\\n' '{self.base_dir}' ;;
    NODE_PLANE_SHARED_DIR) printf '%s\\n' '{self.shared_dir}' ;;
    *) echo "" ;;
  esac
}}

{PREPARE_FN}
{CLAIM_FN}

CONTROLLER_STAGE_DIR='{stage}'
TARGET_REF='{ref}'
CONTROLLER_COMMIT='{build_commit}'
MODE='{mode}'
TARGET_BRANCH='{branch}'
STACK_JOB='{job}'
HEALTH_TIMEOUT='{timeout}'
SKIP_RESTART='{skip_restart}'
SKIP_DEPS='{skip_deps}'
REPO_ROOT='{stage}'

handoff="$(prepare_target_handoff)"

# Apply claim-time variable overrides if testing mismatch
TARGET_BRANCH='{claim_b}'
TARGET_REF='{claim_r}'
STACK_JOB='{claim_j}'
REPO_ROOT='{claim_s}'

claim_target_handoff "$handoff"
{second_claim_snippet}
python3 -c 'import json, sys; print("CALLER_VARS:" + json.dumps(dict(zip(sys.argv[1::2], sys.argv[2::2]))))' \\
  TARGET_BRANCH "$TARGET_BRANCH" \\
  STACK_JOB "$STACK_JOB" \\
  TARGET_REF "$TARGET_REF" \\
  CONTROLLER_COMMIT "$CONTROLLER_COMMIT" \\
  HEALTH_TIMEOUT "$HEALTH_TIMEOUT" \\
  SKIP_RESTART "$SKIP_RESTART" \\
  SKIP_DEPS "$SKIP_DEPS" \\
  MODE "$MODE" \\
  HANDOFF "$handoff"
"""

    def _run_shell(self, script_text, cwd=None):
        target_cwd = str(cwd or self.stage_dir)
        return subprocess.run(
            ['bash', '-c', script_text],
            cwd=target_cwd,
            capture_output=True,
            text=True,
            env={**os.environ,
                 'NODE_PLANE_BASE_DIR': str(self.base_dir),
                 'NODE_PLANE_SHARED_DIR': str(self.shared_dir)},
            timeout=10,
        )

    def test_handoff_lifecycle_and_caller_variables_preserved(self):
        cases = [
            ('dev', str(uuid4())),
            ('main', ''),
            ('', ''),
            ('feature/test', str(uuid4())),
        ]

        for branch, job in cases:
            with self.subTest(branch=branch, job=job):
                script = self._build_shell_harness(
                    branch=branch,
                    job=job,
                    ref='v0.2.1',
                    timeout='45',
                    skip_restart='1',
                    skip_deps='0',
                    mode='simple',
                )
                result = self._run_shell(script)
                self.assertEqual(result.returncode, 0, f"Script failed: {result.stderr}")

                caller_line = [line for line in result.stdout.splitlines() if line.startswith('CALLER_VARS:')]
                self.assertEqual(len(caller_line), 1)
                caller_vars = json.loads(caller_line[0][len('CALLER_VARS:'):])

                # Identity vars must remain intact after claim
                self.assertEqual(caller_vars['TARGET_BRANCH'], branch)
                self.assertEqual(caller_vars['STACK_JOB'], job)
                self.assertEqual(caller_vars['TARGET_REF'], 'v0.2.1')
                self.assertEqual(caller_vars['CONTROLLER_COMMIT'], self.commit)
                self.assertEqual(caller_vars['HEALTH_TIMEOUT'], '45')
                self.assertEqual(caller_vars['SKIP_RESTART'], '1')
                self.assertEqual(caller_vars['SKIP_DEPS'], '0')
                self.assertEqual(caller_vars['MODE'], 'simple')

                handoff_path = Path(caller_vars['HANDOFF'])
                self.assertTrue(handoff_path.is_file())
                # Permissions must be restricted to 0600
                self.assertEqual(stat.S_IMODE(handoff_path.stat().st_mode), 0o600)

                payload = json.loads(handoff_path.read_text())
                self.assertEqual(payload['status'], 'claimed')
                self.assertIn('claimed_at', payload)
                self.assertIsInstance(payload['claimed_at'], (int, float))
                self.assertEqual(payload['protocol_version'], 1)
                self.assertEqual(payload['branch'], branch)
                self.assertEqual(payload['stack_job'], job)
                self.assertEqual(payload['ref'], 'v0.2.1')
                self.assertEqual(payload['commit'], self.commit)
                self.assertEqual(payload['mode'], 'simple')
                self.assertEqual(payload['stage'], str(self.stage_dir))
                self.assertEqual(payload['base'], str(self.base_dir))
                self.assertEqual(payload['shared'], str(self.shared_dir))

    def test_claim_rejects_ref_mismatch(self):
        script = self._build_shell_harness(
            branch='dev',
            job='job-1',
            ref='v1.0.0',
            claim_ref='v2.0.0'
        )
        result = self._run_shell(script)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Update handoff identity mismatch: ref', result.stderr)

    def test_claim_rejects_commit_mismatch(self):
        different_commit = '9f8e7d6c5b4a3f2e1d0c9b8a7f6e5d4c3b2a1f0e'
        (self.stage_dir / 'BUILD_COMMIT').write_text(different_commit + '\n')
        script = self._build_shell_harness(
            branch='dev',
            job='job-1',
            commit=self.commit  # prepare with original commit, but stage has different_commit
        )
        result = self._run_shell(script)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Update handoff identity mismatch: commit', result.stderr)

    def test_claim_rejects_branch_mismatch(self):
        script = self._build_shell_harness(
            branch='main',
            job='job-1',
            claim_branch='dev'
        )
        result = self._run_shell(script)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Update handoff identity mismatch: branch', result.stderr)

    def test_claim_rejects_job_mismatch(self):
        script = self._build_shell_harness(
            branch='dev',
            job='job-original-uuid',
            claim_job='job-different-uuid'
        )
        result = self._run_shell(script)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Update handoff identity mismatch: stack_job', result.stderr)

    def test_claim_rejects_stage_mismatch(self):
        other_stage = self.root / 'other_stage'
        other_stage.mkdir()
        (other_stage / 'BUILD_COMMIT').write_text(self.commit + '\n')

        script = self._build_shell_harness(
            branch='dev',
            job='job-1',
            claim_stage=other_stage
        )
        result = self._run_shell(script, cwd=other_stage)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Update handoff identity mismatch: stage', result.stderr)

    def test_consumed_handoff_rejects_second_claim(self):
        script = self._build_shell_harness(
            branch='dev',
            job='job-test-double',
            claim_twice=True
        )
        result = self._run_shell(script)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Update handoff is missing, consumed, or already claimed.', result.stderr)

    def test_archive_update_uses_release_channel_when_branch_setting_is_empty(self):
        source = (ROOT / 'scripts/update.sh').read_text()
        begin = source.index('fetch_code() {')
        function = source[begin:source.index('\n}\n', begin) + 3]
        for channel, branch in [('beta', 'beta'), ('dev', 'dev'), ('stable', 'main')]:
            with self.subTest(channel=channel):
                (self.root / 'RELEASE_CHANNEL').write_text(channel + '\n')
                script = '''set -euo pipefail
FROM_SOURCE=0
TARGET_BRANCH=
TARGET_REF=
set_step() { :; }
read_env_value() { :; }
prepare_controller_archive() { printf 'selected=%s\\n' "$1"; CONTROLLER_REF=v0.5.0-beta.1; }
''' + function + '\nfetch_code\n'
                result = subprocess.run(['bash', '-c', script],
                    env={**os.environ, 'REPO_ROOT': str(self.root)}, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, f'selected={branch}\n')

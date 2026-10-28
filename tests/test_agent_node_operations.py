import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SPEC = importlib.util.spec_from_file_location('node_operations', Path(__file__).resolve().parents[1] / 'runtime_assets/backend-node-operation.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class AgentNodeOperationTests(unittest.TestCase):
    def test_selective_protocol_cleanup_preserves_the_other_protocol_and_agent(self):
        for removed in ('awg', 'xray'):
            with self.subTest(removed=removed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for folder in ('xray', 'amnezia-awg/data', 'awg-clients'):
                    (root / folder).mkdir(parents=True)
                    (root / folder / 'config').write_text('owned config')
                (root / 'agent-journal').write_text('keep')
                (root / 'helper.py').write_text('keep')
                records = [
                    {'Name':'/xray', 'Id':'a'*64, 'Mounts':[{'Type':'bind','Source':str(root/'xray'),'Destination':'/etc/xray'}]},
                    {'Name':'/amnezia-awg','Id':'b'*64,'Mounts':[{'Type':'bind','Source':str(root/'amnezia-awg/data'),'Destination':'/opt/amnezia/awg'}]},
                    {'Name':'/unrelated','Id':'c'*64,'Mounts':[]},
                ]
                def command(args, lock_fd=None):
                    if args[:3] == ['docker', 'ps', '-aq']:
                        return '\n'.join(record['Id'] for record in records)
                    if args[:3] == ['docker', 'container', 'inspect']:
                        return json.dumps(records)
                    return ''
                with patch.object(MODULE, 'ROOT', root), patch.object(MODULE, 'environment',
                        return_value=[str(root/'xray/config.json'),str(root/'amnezia-awg/data/wg0.conf'),'xray','amnezia-awg']), \
                     patch.object(MODULE, 'command', side_effect=command) as commands, \
                     patch.object(MODULE.intents, 'stop_leased_runtime_units') as stop, \
                     patch.object(MODULE.subprocess, 'run', return_value=SimpleNamespace(returncode=1)):
                    MODULE.remove_protocol_runtime(17, [removed])
                stop.assert_called_once_with([removed])
                deleted = [call.args[0][-1] for call in commands.call_args_list if call.args[0][:3] == ['docker','rm','-f']]
                self.assertEqual(deleted, [('b' if removed == 'awg' else 'a') * 64])
                self.assertEqual((root/'xray').exists(), removed != 'xray')
                self.assertEqual((root/'amnezia-awg').exists(), removed != 'awg')
                self.assertEqual((root/'awg-clients').exists(), removed != 'awg')
                self.assertTrue((root/'agent-journal').exists())
                self.assertTrue((root/'helper.py').exists())

    def test_managed_ports_verify_mounts_running_state_and_actual_publications(self):
        xray = Path('/owned/xray/config.json')
        awg = Path('/owned/awg/wg0.conf')
        records = [
            {'Name':'/xray','State':{'Running':True},'Mounts':[{'Type':'bind','Source':'/owned/xray','Destination':'/etc/xray'}]},
            {'Name':'/amnezia-awg','State':{'Running':True},'Mounts':[{'Type':'bind','Source':'/owned/awg','Destination':'/opt/amnezia/awg'}],
             'NetworkSettings':{'Ports':{'443/udp':[{'HostPort':'8443'}]}}},
        ]
        with patch.object(MODULE,'environment',return_value=(str(xray),str(awg),'xray','amnezia-awg')), \
             patch.object(MODULE,'command',side_effect=['one two',json.dumps(records)]), \
             patch.object(MODULE.Path,'read_text',return_value=json.dumps({'inbounds':[
                 {'protocol':'vless','port':443},{'protocol':'socks','port':1080}]})):
            self.assertEqual(MODULE.managed_ports(), {('tcp',443),('udp',8443)})
        records[0]['Mounts'] = []
        records[1]['State']['Running'] = False
        with patch.object(MODULE,'environment',return_value=(str(xray),str(awg),'xray','amnezia-awg')), \
             patch.object(MODULE,'command',side_effect=['one two',json.dumps(records)]):
            self.assertEqual(MODULE.managed_ports(), set())

    def test_inspection_distinguishes_absent_stopped_and_running_containers(self):
        for code, output, installed, running in [(1, '', False, False), (0, 'false', True, False), (0, 'true', True, True)]:
            with self.subTest(installed=installed, running=running), \
                 patch.object(MODULE, 'environment', return_value=('missing-xray', 'missing-awg', 'xray', 'amnezia-awg')), \
                 patch.object(MODULE.shutil, 'which', return_value='/usr/bin/docker'), \
                 patch.object(MODULE.Path, 'read_text', side_effect=OSError), \
                 patch.object(MODULE, 'host_metrics', return_value={}), \
                 patch.object(MODULE.subprocess, 'run', side_effect=[SimpleNamespace(returncode=0),
                     SimpleNamespace(returncode=code, stdout=output), SimpleNamespace(returncode=code, stdout=output)]):
                facts = MODULE.inspect()
                for protocol in ('awg', 'xray'):
                    self.assertEqual(facts[protocol + '_installed'], installed)
                    self.assertEqual(facts[protocol + '_running'], running)

    def test_host_metrics_use_available_memory_and_runtime_filesystem(self):
        with patch.object(MODULE.Path,'read_text',side_effect=['MemTotal: 1000 kB\nMemAvailable: 200 kB\n', '86400.9 1000']), \
             patch.object(MODULE.os,'statvfs',return_value=SimpleNamespace(f_blocks=100,f_bavail=7,f_frsize=4096)), \
             patch.object(MODULE.os,'getloadavg',return_value=(4.5,2,1)), \
             patch.object(MODULE.os,'cpu_count',return_value=2):
            self.assertEqual(MODULE.host_metrics(),{'ram_used_percent':80.0,'disk_free_percent':7.0,
                'load1':4.5,'load5':2,'load15':1,'cpus':2,'uptime_seconds':86400,
                'ram_total_bytes':1024000,'ram_available_bytes':204800,
                'disk_total_bytes':409600,'disk_free_bytes':28672})

    def test_missing_host_measurements_are_not_reported_as_healthy_zero(self):
        with patch.object(MODULE.Path,'read_text',side_effect=OSError), \
             patch.object(MODULE.os,'statvfs',side_effect=OSError), \
             patch.object(MODULE.os,'getloadavg',side_effect=OSError):
            self.assertEqual(MODULE.host_metrics(),{})

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        policy = patch.object(MODULE, "RA_POLICY", Path(self.temp.name) / "ipv6-ra.conf")
        policy.start()
        self.addCleanup(policy.stop)
        self.path = Path(self.temp.name) / 'journal.sqlite3'
        owner = patch.object(MODULE.intents, 'verify_node_runtime_owner')
        self.owner_check = owner.start()
        self.addCleanup(owner.stop)
        self.intent = {'node_key': 'n1', 'command_id': 'job1', 'revision': 2,
            'protocols': ['awg'], 'settings': {'public_host': 'node.example', 'awg_port': 51820}}

    def execute(self, action='reinstall_clean', recover=False):
        return MODULE.execute(action, 'job1', self.intent, recover, self.path)

    def test_first_protocol_install_and_both_reinstalls_use_firewall_settings_path(self):
        for action in ('bootstrap', 'reinstall_keep', 'reinstall_clean'):
            with self.subTest(action=action), \
                    patch.object(MODULE, 'inspect', return_value={'awg_config_valid': True}), \
                    patch.object(MODULE, 'remove_protocol_runtime') as cleanup, \
                    patch.object(MODULE.intents, 'run_node_settings',
                        return_value={'payload_json': '{"applied":true}'}) as settings:
                self.assertEqual(MODULE.run(action, self.intent, 17), {'applied': True})
                settings.assert_called_once_with({'kind': 'node_settings', **self.intent}, 17)
                self.assertEqual(cleanup.call_count, int(action == 'reinstall_clean'))
                if action == 'reinstall_clean':
                    cleanup.assert_called_once_with(17, ['awg'])

    def test_runtime_cleanup_is_scoped_to_the_nodes_vpn_protocols(self):
        with patch.object(MODULE, 'remove_protocol_runtime') as cleanup:
            self.assertEqual(MODULE.run('cleanup_runtime', self.intent, 17), {'cleaned': True})
        cleanup.assert_called_once_with(17, ['awg'])

    def test_protocol_progress_survives_failure_without_replaying_command(self):
        from uuid import uuid4
        identity = str(uuid4())
        intent = dict(self.intent, command_id=identity)
        def interrupted(*args):
            MODULE.intents.installation_step('xray_init')
            raise TimeoutError()
        with patch.dict(os.environ, {'NODE_PLANE_OPERATION_PROGRESS_ID': 'inherited'}), \
             patch.object(MODULE, 'run', side_effect=interrupted) as run:
            with self.assertRaises(TimeoutError):
                MODULE.execute('bootstrap', identity, intent, False, self.path)
            observation = json.loads((self.path.parent / 'operation-progress' / (identity + '.json')).read_text())
            self.assertEqual(observation, {'operation_id': identity, 'stage': 'xray_init'})
            self.assertEqual(os.environ['NODE_PLANE_OPERATION_PROGRESS_ID'], 'inherited')
            with self.assertRaises(ValueError):
                MODULE.execute('bootstrap', identity, intent, True, self.path)
            run.assert_called_once()

    def test_retries_and_recovery_do_not_repeat_clean_reinstall(self):
        with patch.object(MODULE, 'run', return_value={'verified': True}) as run:
            result = self.execute()
            self.assertEqual(self.execute(), result)
            self.assertEqual(self.execute(recover=True), result)
            run.assert_called_once()
            with self.assertRaises(ValueError):
                self.execute('reinstall_keep')

    def test_finished_executor_can_be_retired_under_lock_without_agent_restart(self):
        with patch.dict(os.environ, {'NODE_PLANE_AGENT_INSTANCE_ID': 'old'}), patch.object(MODULE, 'run', side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):
                self.execute()
            self.assertTrue(self.execute('resolve_reinstall_clean', True)['retired'])
        with patch.dict(os.environ, {'NODE_PLANE_AGENT_INSTANCE_ID': 'new'}):
            self.assertTrue(self.execute('resolve_reinstall_clean', True)['retired'])
            with self.assertRaises(ValueError):
                self.execute()

    def test_retiring_not_started_command_fences_a_delayed_request(self):
        self.assertTrue(self.execute('resolve_reinstall_clean', True)['retired'])
        with patch.object(MODULE, 'run') as run:
            with self.assertRaises(ValueError):
                self.execute()
            run.assert_not_called()

    def test_explicit_backend_resolution_restores_helpers_without_replaying_bootstrap(self):
        from backend.node_operations import NodeOperations
        row = {'id': 'job1', 'node_key': 'n1', 'action': 'bootstrap', 'revision': 2,
               'status': 'blocked', 'intent_json': json.dumps(self.intent), 'result_json': None}
        db = MagicMock()
        conn = db.connect.return_value.__enter__.return_value
        conn.execute.return_value.fetchone.side_effect = [row, dict(row, status='superseded')]
        calls = []
        class Driver:
            def node_action(inner, job_id, action, intent, recover=False):
                calls.append((action, recover))
                return MODULE.execute(action, job_id, intent, recover, self.path)
        with patch('backend.node_operations.require_permission'), patch.object(MODULE, 'run') as run:
            result = NodeOperations(db, Driver()).resolve(object(), 'job1')
            self.assertEqual(result['status'], 'superseded')
            self.assertEqual(calls, [('resolve_bootstrap', False)])
            with self.assertRaises(ValueError):
                self.execute('bootstrap')
            run.assert_not_called()

    def test_cleanup_removes_client_cache_but_preserves_agent_journal_and_scripts(self):
        root = Path(self.temp.name)
        for folder in ('xray', 'amnezia-awg/data', 'awg-clients'):
            (root / folder).mkdir(parents=True)
            (root / folder / 'config').write_text('private config')
        (root / 'helper.py').write_text('runtime helper')
        self.path.write_text('journal')
        MODULE.RA_POLICY.write_text('administrator-owned settings\n')
        with patch.object(MODULE, 'ROOT', root), patch.object(MODULE, 'environment',
                return_value=[str(root/'xray/config.json'), str(root/'amnezia-awg/data/wg0.conf'), 'xray', 'amnezia-awg']), \
             patch.object(MODULE.subprocess, 'run') as inspect, patch.object(MODULE, 'command', return_value=''):
            inspect.return_value.returncode = 1
            MODULE.remove_protocol_runtime(0)
            self.assertEqual(MODULE.RA_POLICY.read_text(), 'administrator-owned settings\n')
            MODULE.RA_POLICY.write_text('# Managed by Node Plane: IPv6 router advertisements\n')
            MODULE.remove_protocol_runtime(0)
            self.assertFalse(MODULE.RA_POLICY.exists())
        self.assertFalse((root / 'awg-clients').exists())
        self.assertFalse((root / 'xray').exists())
        self.assertFalse((root / 'amnezia-awg').exists())
        self.assertTrue(self.path.exists())
        self.assertTrue((root / 'helper.py').exists())

    def test_cleanup_plans_current_and_previous_containers_by_mount_and_id(self):
        records = [{'Name': '/' + name, 'Id': digit * 64,
                    'Mounts': [{'Type': 'bind', 'Source': '/runtime/xray', 'Destination': '/etc/xray'}]}
                   for name, digit in [('xray', 'a'), ('xray-previous-123', 'b')]]
        records.append({'Name': '/unrelated', 'Id': 'c' * 64, 'Mounts': []})
        with patch.object(MODULE, 'command', side_effect=['a\nb\nc', json.dumps(records)]):
            self.assertEqual(MODULE.owned_protocol_containers('/runtime/xray/config.json',
                '/runtime/amnezia-awg/data/wg0.conf', 'xray', 'amnezia-awg'), ['a' * 64, 'b' * 64])

    def test_unowned_second_container_aborts_before_any_removal(self):
        root = Path(self.temp.name)
        (root / 'xray').mkdir()
        sentinel = root / 'xray/config.json'
        sentinel.write_text('keep')
        records = [{'Name': '/xray', 'Id': 'a' * 64, 'Mounts': [
            {'Type': 'bind', 'Source': str(root/'xray'), 'Destination': '/etc/xray'}]},
            {'Name': '/amnezia-awg', 'Id': 'b' * 64, 'Mounts': []}]
        with patch.object(MODULE, 'ROOT', root), patch.object(MODULE, 'environment', return_value=[
                str(sentinel), str(root/'amnezia-awg/data/wg0.conf'), 'xray', 'amnezia-awg']), \
             patch.object(MODULE, 'command', side_effect=['a\nb', json.dumps(records)]) as command:
            with self.assertRaisesRegex(ValueError, 'ownership'):
                MODULE.remove_protocol_runtime(0)
            self.assertEqual(command.call_count, 2)
        self.assertEqual(sentinel.read_text(), 'keep')

    def test_invalid_second_directory_does_not_remove_first_or_stop_containers(self):
        root = Path(self.temp.name)
        (root / 'xray').mkdir()
        sentinel = root / 'xray/config.json'
        sentinel.write_text('keep')
        with patch.object(MODULE, 'ROOT', root), patch.object(MODULE, 'environment',
                return_value=[str(sentinel), str(root/'unrelated/data/wg0.conf'), 'xray', 'amnezia-awg']), \
             patch.object(MODULE.subprocess, 'run') as inspect, patch.object(MODULE, 'command') as command:
            with self.assertRaises(ValueError):
                MODULE.remove_protocol_runtime(0)
            inspect.assert_not_called()
            command.assert_not_called()
        self.assertEqual(sentinel.read_text(), 'keep')

    def test_symlinked_clients_refuse_entire_cleanup_before_container_changes(self):
        root = Path(self.temp.name)
        external = root / 'external'
        external.mkdir()
        sentinel = external / 'private.conf'
        sentinel.write_text('keep')
        (root / 'awg-clients').symlink_to(external, target_is_directory=True)
        with patch.object(MODULE, 'ROOT', root), patch.object(MODULE, 'environment',
                return_value=[str(root/'xray/config.json'), str(root/'amnezia-awg/data/wg0.conf'), 'xray', 'amnezia-awg']), \
             patch.object(MODULE.subprocess, 'run') as inspect:
            with self.assertRaises(ValueError):
                MODULE.remove_protocol_runtime(0)
            inspect.assert_not_called()
        self.assertEqual(sentinel.read_text(), 'keep')

    def test_symlinked_runtime_root_does_not_make_external_directories_owned(self):
        root = Path(self.temp.name)
        external = root / 'external'
        (external / 'xray').mkdir(parents=True)
        sentinel = external / 'xray/config.json'
        sentinel.write_text('keep')
        alias = root / 'runtime'
        alias.symlink_to(external, target_is_directory=True)
        with patch.object(MODULE, 'ROOT', alias), patch.object(MODULE, 'environment',
                return_value=[str(alias/'xray/config.json'), str(alias/'amnezia-awg/data/wg0.conf'), 'xray', 'amnezia-awg']), \
             patch.object(MODULE.subprocess, 'run') as inspect:
            with self.assertRaises(ValueError):
                MODULE.remove_protocol_runtime(0)
            inspect.assert_not_called()
        self.assertEqual(sentinel.read_text(), 'keep')

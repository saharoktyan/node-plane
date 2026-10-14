import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import io
import os
import sys
from contextlib import redirect_stdout, redirect_stderr
from types import ModuleType
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from tests import test_backend_stack_updates as stack_fixture

spec = importlib.util.spec_from_file_location('collected_update_recovery', Path(__file__).resolve().parents[1] / 'scripts/recover_collected_update.py')
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


class CollectedUpdateRecoveryTests(TestCase):
    setUp = stack_fixture.StackUpdateTests.setUp
    service = stack_fixture.StackUpdateTests.service

    def prepare(self):
        service = self.service()
        job = service.queue(self.actor, str(uuid4()), 'stack', target_ref='v0.4.3-alpha.19', branch='dev')
        service.run_one()
        unit = 'node-plane-update-20261014-140503'
        plan = {**job['result'], 'unit_name':unit}
        self.db.connection.execute('UPDATE backend_update_jobs SET result_json=? WHERE id=?',(json.dumps(plan),job['id']))
        self.db.connection.execute("INSERT INTO schema_meta(key,value) VALUES ('updates_last_run_unit',?)",(unit,))
        self.db.connection.commit()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root/'data/updates').mkdir(parents=True)
        path = root/'data/updates'/(job['id']+'.json')
        path.write_text(json.dumps({'status':'failed','rollback_status':'succeeded'}))
        return service, job, root, path

    def execute(self, args, **kwargs):
        return subprocess.CompletedProcess(args,1,stdout='not-found\n',stderr='')

    def test_exact_collected_failed_update_is_finished_atomically_without_launch(self):
        service,job,root,_ = self.prepare()
        # argparse passes a UUID object to the real recovery function.
        result = recovery.recover(self.db,root,recovery.UUID(job['id']),self.execute)
        self.assertEqual(result['status'],'rolled_back')
        self.assertFalse(result['installation_replayed'])
        self.assertIsNone(self.db.connection.execute('SELECT job_id FROM backend_controller_update_gate').fetchone())
        self.assertEqual(service.get(self.actor,job['id'])['status'],'rolled_back')
        self.assertEqual(self.db.connection.execute('SELECT outcome FROM backend_recovery_audit').fetchone()['outcome'],'confirmed')
        service.updater.schedule_update.assert_called_once()

    def test_missing_rollback_or_present_unit_preserves_gate(self):
        service,job,root,path = self.prepare()
        path.write_text(json.dumps({'status':'failed','rollback_status':'failed'}))
        with self.assertRaises(ValueError):
            recovery.recover(self.db,root,job['id'],self.execute)
        path.write_text(json.dumps({'status':'failed','rollback_status':'succeeded'}))
        with self.assertRaises(ValueError):
            recovery.recover(self.db,root,job['id'],lambda args,**kw:subprocess.CompletedProcess(args,0,stdout='loaded\n'))
        self.assertIsNotNone(self.db.connection.execute('SELECT job_id FROM backend_controller_update_gate').fetchone())
        self.assertEqual(service.get(self.actor,job['id'])['status'],'running')

    def test_cli_runs_real_recovery_from_parsed_arguments_to_committed_result(self):
        service,job,shared,_ = self.prepare()
        app = shared/'current'
        (app/'app/backend').mkdir(parents=True)
        (app/'app/backend/updates.py').touch()
        (shared/'.env').write_text('# Disposable configuration fixture\n')
        config = ModuleType('config')
        config.__getattr__ = lambda name: os.environ['NODE_PLANE_SHARED_DIR'] if name == 'SHARED_ROOT' else (_ for _ in ()).throw(AttributeError(name))
        db = ModuleType('db')
        db.get_db = lambda: self.db
        output = io.StringIO()
        with patch.dict(sys.modules,{'config':config,'db':db}), patch.dict(os.environ,{}), patch.object(Path,'cwd',return_value=app), patch.object(sys,'argv',['recover',job['id'],'--shared-dir',str(shared)]), patch.object(recovery.subprocess,'run',side_effect=self.execute), redirect_stdout(output):
            recovery.main()
        self.assertEqual(json.loads(output.getvalue())['status'],'rolled_back')
        self.assertEqual(service.get(self.actor,job['id'])['status'],'rolled_back')
        self.assertIsNone(self.db.connection.execute('SELECT job_id FROM backend_controller_update_gate').fetchone())
        service.updater.schedule_update.assert_called_once()

    def test_cli_selects_installed_paths_before_loading_configuration_and_reports_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory)/'current'
            (app/'app/backend').mkdir(parents=True)
            (app/'app/backend/updates.py').touch()
            shared = Path(directory)/'shared'
            (shared/'data').mkdir(parents=True)
            (shared/'.env').write_text('# Disposable configuration fixture\n')
            config = ModuleType('config')
            def configuration(name):
                self.assertEqual(os.environ['NODE_PLANE_APP_DIR'],str(app))
                self.assertEqual(os.environ['NODE_PLANE_SHARED_DIR'],str(shared))
                if name == 'SHARED_ROOT':
                    return os.environ['NODE_PLANE_SHARED_DIR']
                raise AttributeError(name)
            config.__getattr__ = configuration
            db = ModuleType('db')
            database = object()
            db.get_db = lambda: database
            identity = str(uuid4())
            argv = ['recover',identity,'--shared-dir',str(shared)]
            with patch.dict(sys.modules,{'config':config,'db':db}), patch.dict(os.environ,{'NODE_PLANE_APP_DIR':'/wrong','NODE_PLANE_SHARED_DIR':'/wrong'}), patch.object(Path,'cwd',return_value=app), patch.object(sys,'argv',argv):
                with patch.object(recovery,'recover',return_value={'status':'rolled_back'}) as recover, redirect_stdout(io.StringIO()):
                    recovery.main()
                recover.assert_called_once_with(database,str(shared),recovery.UUID(identity))
                error = io.StringIO()
                with patch.object(recovery,'recover',side_effect=recovery.RecoveryRefused('The update unit is still present')), redirect_stderr(error), self.assertRaises(SystemExit) as stopped:
                    recovery.main()
                self.assertEqual(stopped.exception.code,1)
                self.assertIn('The update unit is still present',error.getvalue())

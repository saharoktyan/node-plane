import unittest
from unittest.mock import Mock, patch

import psycopg

from backend.executor import wait_for_schema


class WorkerStartupReadinessTests(unittest.TestCase):
    def event(self):
        return Mock(is_set=Mock(return_value=False), wait=Mock(return_value=False))

    def test_database_startup_recovers_without_worker_initialization_or_secret_logs(self):
        check = Mock(side_effect=[psycopg.OperationalError('SECRET_DSN'), None])
        event = self.event()
        with self.assertLogs('backend.executor', level='INFO') as logs:
            self.assertTrue(wait_for_schema(object(), event, check=check))
        self.assertEqual(check.call_count, 2)
        event.wait.assert_called_once_with(1)
        self.assertNotIn('SECRET_DSN', ' '.join(logs.output))

    def test_timeout_preserves_original_failure(self):
        failure = psycopg.OperationalError('unavailable')
        event = self.event()
        with self.assertRaises(psycopg.OperationalError) as caught, \
                patch('backend.executor.logging'):
            wait_for_schema(None, event, check=Mock(side_effect=failure),
                            clock=Mock(side_effect=[0, 1, 61]), timeout=60)
        self.assertIs(caught.exception, failure)
        event.wait.assert_called_once_with(1)

    def test_shutdown_during_readiness_wait_exits_cleanly(self):
        event = self.event()
        event.wait.return_value = True
        with patch('backend.executor.logging'):
            self.assertFalse(wait_for_schema(None, event,
                check=Mock(side_effect=psycopg.OperationalError('unavailable'))))

    def test_bad_credentials_and_schema_errors_are_not_retried(self):
        for failure in (psycopg.errors.InvalidPassword('bad credentials'), ValueError('schema mismatch')):
            with self.subTest(failure=type(failure).__name__):
                event = self.event()
                check = Mock(side_effect=failure)
                with self.assertRaises(type(failure)):
                    wait_for_schema(None, event, check=check)
                check.assert_called_once()
                event.wait.assert_not_called()

    def test_postgres_starting_up_is_retryable(self):
        event = self.event()
        with patch('backend.executor.logging'):
            self.assertTrue(wait_for_schema(None, event,
                check=Mock(side_effect=[psycopg.errors.CannotConnectNow('starting'), None])))

"""Dedicated LISTEN connection; periodic scans remain authoritative."""
import logging
import time
from db.postgres_db import WORK_CHANNEL


class Wakeup:
    def __init__(self, dsn):
        self.dsn = dsn
        self.conn = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def wait(self, timeout, stop):
        if stop.is_set():
            return
        try:
            if self.conn is None:
                import psycopg
                self.conn = psycopg.connect(self.dsn, autocommit=True, connect_timeout=3)
                self.conn.execute('LISTEN ' + WORK_CHANNEL)
            deadline = time.monotonic() + timeout
            while not stop.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                notifications = self.conn.notifies(timeout=min(1, remaining), stop_after=1)
                try:
                    if next(notifications, None) is not None:
                        return
                finally:
                    notifications.close()
        except Exception:
            self.close()
            logging.getLogger(__name__).warning('Worker wakeup unavailable; using periodic scans')
            stop.wait(timeout)

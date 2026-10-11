"""One PostgreSQL listener per API process; queues remain authoritative."""
import logging
import threading

from db.postgres_db import DELIVERY_CHANNEL


class DeliveryWakeup:
    def __init__(self, dsn):
        self.dsn = dsn
        self.condition = threading.Condition()
        self.generation = 0
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.listen, name='delivery-wakeup', daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.stop.set()
        self.notify()
        self.thread.join(timeout=5)

    def notify(self):
        with self.condition:
            self.generation += 1
            self.condition.notify_all()

    def snapshot(self):
        with self.condition:
            return self.generation

    def wait(self, generation, timeout):
        with self.condition:
            return self.condition.wait_for(
                lambda: self.generation != generation or self.stop.is_set(), timeout)

    def listen(self):
        import psycopg
        while not self.stop.is_set():
            try:
                with psycopg.connect(self.dsn, autocommit=True, connect_timeout=3) as conn:
                    conn.execute('LISTEN ' + DELIVERY_CHANNEL)
                    # Recheck durable work after the initial connection or reconnect.
                    self.notify()
                    while not self.stop.is_set():
                        for _ in conn.notifies(timeout=1):
                            self.notify()
                            if self.stop.is_set():
                                break
            except Exception:
                logging.getLogger(__name__).warning('Delivery wakeup unavailable; using periodic checks')
                self.stop.wait(5)

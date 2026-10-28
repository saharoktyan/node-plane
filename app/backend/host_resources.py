"""Rolling controller CPU sampling and validation shared by alerts and probes."""
from collections import deque
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import time

SAMPLE_SECONDS = 5
WINDOW_SECONDS = 60
CHECK_SECONDS = 30
MAX_AGE_SECONDS = 15
CPU_WARNING = 85
CPU_CRITICAL = 95
CPU_RECOVERED = 80
LOAD_TRIGGER = 2.0
LOAD_RECOVERED = 1.5
RAM_TRIGGER = 90
RAM_RECOVERED = 85
DISK_TRIGGER = 10
DISK_RECOVERED = 12
REPEAT_CHOICES = (0, 30, 60, 180)
DEFAULT_REPEAT_MINUTES = 60


def cpu_metrics(metrics):
    if not isinstance(metrics, dict):
        return None
    limits = {'cpu_used_percent': (0, 100), 'cpu_sample_window_seconds': (60, 75),
              'cpu_sample_age_seconds': (0, MAX_AGE_SECONDS)}
    for key, (low, high) in limits.items():
        value = metrics.get(key)
        if type(value) not in {int, float} or not math.isfinite(value) or not low <= value <= high:
            return None
    return {key: metrics[key] for key in limits}


class CpuSampler:
    def __init__(self):
        self.samples = deque()

    def observe(self, raw, at):
        try:
            fields = raw.splitlines()[0].split()
            if fields[0] != 'cpu' or len(fields) < 5:
                raise ValueError('invalid counters')
            values = [int(value) for value in fields[1:9]]
            if any(value < 0 for value in values):
                raise ValueError('invalid counters')
            total, idle = sum(values), values[3] + (values[4] if len(values) > 4 else 0)
        except (AttributeError, ValueError, IndexError):
            self.samples.clear()
            return
        if self.samples and (at-self.samples[-1][0] > MAX_AGE_SECONDS or total < self.samples[-1][1] or idle < self.samples[-1][2]):
            self.samples.clear()
        if self.samples and at <= self.samples[-1][0]:
            return
        self.samples.append((at, total, idle))
        while len(self.samples) > 2 and at-self.samples[1][0] >= WINDOW_SECONDS:
            self.samples.popleft()

    def metrics(self, at):
        if len(self.samples) < 2:
            return {}
        start, total_before, idle_before = self.samples[0]
        end, total, idle = self.samples[-1]
        window, age, delta, idle_delta = end-start, at-end, total-total_before, idle-idle_before
        if not 60 <= window <= 75 or not 0 <= age <= MAX_AGE_SECONDS or delta <= 0 or not 0 <= idle_delta <= delta:
            return {}
        return {'cpu_used_percent': round(100*(delta-idle_delta)/delta, 1),
                'cpu_sample_window_seconds': window, 'cpu_sample_age_seconds': age}


def monitor(db, driver, stop):
    """Independent read-only monitoring; a long provisioning job cannot block it."""
    from .alerts import AlertService
    from db.postgres_db import worker_connections
    import logging
    sampler = CpuSampler()
    try:
        boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except OSError:
        boot_id = ''
    import threading
    def scan():
        with worker_connections():
            while not stop.is_set():
                try:
                    AlertService(db, driver, stop=stop).scheduled(cpu_only=True)
                except Exception as exc:
                    logging.getLogger(__name__).warning('Host resource alert check temporarily unavailable: type=%s', type(exc).__name__)
                stop.wait(SAMPLE_SECONDS)
    scanner = threading.Thread(target=scan, name='host-alert-check', daemon=True)
    scanner.start()
    try:
        with worker_connections():
            while not stop.is_set():
                try:
                    try:
                        raw = Path('/proc/stat').read_text()
                    except OSError:
                        raw = None
                    sampler.observe(raw, time.monotonic())
                    measurement = {'at': datetime.now(timezone.utc).isoformat(), 'boot_id': boot_id,
                                   'host_metrics': sampler.metrics(time.monotonic())}
                    with db.transaction() as conn:
                        conn.execute("INSERT INTO backend_system_settings VALUES ('controller_cpu_sample',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(measurement),))
                except Exception as exc:
                    logging.getLogger(__name__).warning('CPU measurement temporarily unavailable: type=%s', type(exc).__name__)
                stop.wait(SAMPLE_SECONDS)
    finally:
        stop.set()
        scanner.join(timeout=6)

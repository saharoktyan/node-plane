from unittest import TestCase
from backend.host_resources import CpuSampler, cpu_metrics


class HostCpuTests(TestCase):
    def test_rolling_average_waits_for_a_minute_excludes_guest_and_expires(self):
        sampler = CpuSampler()
        for second in range(0,121,5):
            sampler.observe(f'cpu {second*9} 0 0 {second} 0 0 0 0 99999 99999',second)
            if second < 60:
                self.assertEqual(sampler.metrics(second),{})
        measured = sampler.metrics(120)
        self.assertEqual(measured['cpu_used_percent'],90)
        self.assertEqual(measured['cpu_sample_window_seconds'],60)
        self.assertEqual(sampler.metrics(136),{})
        self.assertLessEqual(len(sampler.samples),14)
        self.assertIsNotNone(cpu_metrics(measured))

    def test_counter_reset_missing_samples_and_long_gaps_discard_the_window(self):
        for broken in (None,'cpu invalid','cpu 0 0 0 0','cpu -1 0 0 1'):
            sampler = CpuSampler()
            for second in range(0,61,5):
                sampler.observe(f'cpu {second*9} 0 0 {second}',second)
            self.assertTrue(sampler.metrics(60))
            sampler.observe(broken,65)
            self.assertEqual(sampler.metrics(65),{})
        sampler.observe('cpu 1000 0 0 100',100)
        self.assertEqual(sampler.metrics(100),{})

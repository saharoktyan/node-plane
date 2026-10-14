//! Rolling host CPU counters. Guest time is already included in user/nice.
use std::collections::VecDeque;
use std::time::{Duration, Instant};

use serde_json::{Value, json};

pub const SAMPLE_SECONDS: u64 = 5;
const WINDOW_SECONDS: u64 = 60;
const MAX_GAP_SECONDS: u64 = 15;

#[derive(Default)]
pub struct CpuSampler {
    samples: VecDeque<(Instant, u64, u64)>,
}

fn counters(raw: &str) -> Option<(u64, u64)> {
    let mut fields = raw.lines().next()?.split_whitespace();
    if fields.next()? != "cpu" {
        return None;
    }
    let values: Vec<u64> = fields
        .take(8)
        .map(str::parse)
        .collect::<Result<_, _>>()
        .ok()?;
    if values.len() < 4 {
        return None;
    }
    let total = values
        .iter()
        .try_fold(0u64, |sum, value| sum.checked_add(*value))?;
    let idle = values[3].checked_add(*values.get(4).unwrap_or(&0))?;
    Some((total, idle))
}

impl CpuSampler {
    pub fn observe(&mut self, raw: Option<&str>, now: Instant) {
        let Some((total, idle)) = raw.and_then(counters) else {
            self.samples.clear();
            return;
        };
        if self
            .samples
            .back()
            .is_some_and(|&(at, before_total, before_idle)| {
                now.saturating_duration_since(at) > Duration::from_secs(MAX_GAP_SECONDS)
                    || total < before_total
                    || idle < before_idle
            })
        {
            self.samples.clear();
        }
        if self.samples.back().is_some_and(|&(at, _, _)| now <= at) {
            return;
        }
        self.samples.push_back((now, total, idle));
        while self.samples.len() > 2
            && now.duration_since(self.samples[1].0) >= Duration::from_secs(WINDOW_SECONDS)
        {
            self.samples.pop_front();
        }
    }

    pub fn metrics(&self, now: Instant) -> Value {
        let unavailable = || json!({});
        let (Some(&(start, before_total, before_idle)), Some(&(end, total, idle))) =
            (self.samples.front(), self.samples.back())
        else {
            return unavailable();
        };
        let age = now.saturating_duration_since(end).as_secs_f64();
        let window = end.saturating_duration_since(start).as_secs_f64();
        if age > MAX_GAP_SECONDS as f64
            || window < WINDOW_SECONDS as f64
            || window > (WINDOW_SECONDS + MAX_GAP_SECONDS) as f64
        {
            return unavailable();
        }
        let delta = total - before_total;
        let idle_delta = idle - before_idle;
        if delta == 0 || idle_delta > delta {
            return unavailable();
        }
        let percent = 100.0 * (delta - idle_delta) as f64 / delta as f64;
        json!({"cpu_used_percent": (percent * 10.0).round() / 10.0,
            "cpu_sample_window_seconds": window, "cpu_sample_age_seconds": age})
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn aggregate_excludes_guest_double_counting_and_uses_a_full_recent_window() {
        assert_eq!(
            counters("cpu 10 20 30 40 5 6 7 8 100 200\ncpu0 1 2 3 4"),
            Some((126, 45))
        );
        let start = Instant::now();
        let mut sampler = CpuSampler::default();
        for second in (0..=120).step_by(5) {
            let raw = format!("cpu {} 0 0 {} 0 0 0 0 999 999", second * 9, second);
            sampler.observe(Some(&raw), start + Duration::from_secs(second));
            if second < 60 {
                assert_eq!(
                    sampler.metrics(start + Duration::from_secs(second)),
                    json!({})
                );
            }
        }
        let now = start + Duration::from_secs(120);
        assert_eq!(sampler.metrics(now)["cpu_used_percent"], 90.0);
        assert_eq!(sampler.metrics(now)["cpu_sample_window_seconds"], 60.0);
        assert_eq!(sampler.metrics(now + Duration::from_secs(16)), json!({}));
        assert!(sampler.samples.len() <= 14);
    }
    #[test]
    fn invalid_counters_reset_and_short_spikes_do_not_become_a_minute_of_overload() {
        let start = Instant::now();
        let mut sampler = CpuSampler::default();
        for second in (0..=60).step_by(5) {
            let busy = if second == 60 { 50 } else { 0 };
            let raw = format!("cpu {busy} 0 0 {}", second * 10 - busy);
            sampler.observe(Some(&raw), start + Duration::from_secs(second));
        }
        assert!(
            sampler.metrics(start + Duration::from_secs(60))["cpu_used_percent"]
                .as_f64()
                .unwrap()
                < 10.0
        );
        sampler.observe(Some("cpu 0 0 0 0"), start + Duration::from_secs(65));
        assert_eq!(sampler.metrics(start + Duration::from_secs(65)), json!({}));
        sampler.observe(None, start + Duration::from_secs(70));
        assert!(sampler.samples.is_empty());
    }
}

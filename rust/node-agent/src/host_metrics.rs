//! Lightweight host resources and rolling CPU counters.
//! Guest time is already included in user/nice.
use std::collections::VecDeque;
use std::time::{Duration, Instant};

use serde_json::{Value, json};

pub const SAMPLE_SECONDS: u64 = 5;
const WINDOW_SECONDS: u64 = 60;
const MAX_GAP_SECONDS: u64 = 15;

/// Lightweight host readings, independent of Docker and runtime scripts.
pub fn resources() -> Value {
    parse_resources(
        std::fs::read_to_string("/proc/meminfo").ok().as_deref(),
        std::fs::read_to_string("/proc/loadavg").ok().as_deref(),
        std::fs::read_to_string("/proc/stat").ok().as_deref(),
    )
}

fn parse_resources(memory: Option<&str>, load: Option<&str>, stat: Option<&str>) -> Value {
    let mut result = json!({});
    if let Some(raw) = memory {
        let field = |name: &str| -> Option<u64> {
            let line = raw.lines().find(|line| line.starts_with(name))?;
            let mut fields = line.split_whitespace();
            fields.next()?;
            let value: u64 = fields.next()?.parse().ok()?;
            (fields.next()? == "kB").then_some(value)
        };
        if let (Some(total), Some(available)) = (field("MemTotal:"), field("MemAvailable:"))
            && total > 0
            && available <= total
            && let (Some(total_bytes), Some(available_bytes)) =
                (total.checked_mul(1024), available.checked_mul(1024))
        {
            result["ram_used_percent"] =
                json!((1000.0 * (total - available) as f64 / total as f64).round() / 10.0);
            result["ram_total_bytes"] = json!(total_bytes);
            result["ram_available_bytes"] = json!(available_bytes);
        }
    }
    if let Some(raw) = load {
        for (key, value) in ["load1", "load5", "load15"]
            .into_iter()
            .zip(raw.split_whitespace())
        {
            if let Ok(value) = value.parse::<f64>()
                && value.is_finite()
                && value >= 0.0
            {
                result[key] = json!(value);
            }
        }
    }
    if let Some(raw) = stat {
        let cpus = raw
            .lines()
            .filter(|line| {
                line.split_whitespace().next().is_some_and(|label| {
                    label.strip_prefix("cpu").is_some_and(|suffix| {
                        !suffix.is_empty() && suffix.bytes().all(|c| c.is_ascii_digit())
                    })
                })
            })
            .count();
        if cpus > 0 {
            result["cpus"] = json!(cpus);
        }
    }
    result
}

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
    fn host_resources_use_available_memory_and_online_cpu_count() {
        let value = parse_resources(
            Some("MemTotal: 1000 kB\nMemAvailable: 100 kB\n"),
            Some("4.2 3.1 2.0 1/99 12"),
            Some("cpu 1 2 3 4\ncpu0 1 2\ncpu1 1 2\nintr 12\n"),
        );
        assert_eq!(value["ram_used_percent"], 90.0);
        assert_eq!(value["ram_total_bytes"], 1024000);
        assert_eq!(value["cpus"], 2);
        assert_eq!(value["load1"], 4.2);
        assert_eq!(parse_resources(None, None, None), json!({}));
        let invalid = parse_resources(
            Some("MemTotal: 100 kB\nMemAvailable: 101 kB"),
            Some("NaN -1 nope"),
            Some("cpu 1 2 3 4\n"),
        );
        assert_eq!(invalid, json!({}));
    }
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

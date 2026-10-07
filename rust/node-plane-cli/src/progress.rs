use anyhow::{Result, ensure};
use serde::Deserialize;

pub const STAGES: [&str; 7] = [
    "configuration",
    "release",
    "python",
    "database",
    "identity",
    "services",
    "driver",
];

#[derive(Clone, Deserialize, Debug)]
pub struct Progress {
    pub version: u32,
    pub event: String,
    pub id: String,
    pub label: String,
    pub state: String,
    pub completed: usize,
    pub total: usize,
}

#[derive(Default)]
pub struct Tracker {
    pub completed: usize,
    pub current: String,
    pub detail: String,
    pub failed: bool,
}

impl Tracker {
    pub fn accept(&mut self, p: &Progress) -> Result<()> {
        ensure!(
            p.version == 1 && p.total == STAGES.len() && p.completed <= p.total,
            "Unsupported installer progress protocol."
        );
        let index = STAGES
            .iter()
            .position(|id| *id == p.id)
            .ok_or_else(|| anyhow::anyhow!("Unknown installation step."))?;
        ensure!(
            index == self.completed
                && if p.state == "done" {
                    p.completed == self.completed + 1
                } else {
                    p.completed == self.completed
                },
            "Installer progress arrived out of order."
        );
        ensure!(
            matches!(p.state.as_str(), "running" | "done" | "failed")
                && matches!(p.event.as_str(), "step" | "detail"),
            "Invalid installer progress event."
        );
        ensure!(
            p.event != "detail" || p.state == "running",
            "Installer details cannot confirm completion."
        );
        ensure!(
            if p.state == "done" {
                p.completed == index + 1
            } else {
                p.completed == index
            },
            "Installer step count does not match its phase."
        );
        if p.event == "detail" {
            self.detail = p.label.clone();
        } else {
            self.current = p.label.clone();
            self.detail.clear();
        }
        self.completed = p.completed;
        self.failed |= p.state == "failed";
        Ok(())
    }
    pub fn is_complete(&self) -> bool {
        self.completed == STAGES.len() && !self.failed
    }
}

/// Keeps line memory bounded even if a remote process prints without newlines.
#[derive(Default)]
pub struct Lines {
    pending: Vec<u8>,
    discarding: bool,
}
impl Lines {
    pub fn feed(&mut self, bytes: &[u8], mut emit: impl FnMut(String)) {
        for &b in bytes {
            if b == b'\n' {
                emit(if self.discarding {
                    "[Overlong output line omitted]".into()
                } else {
                    String::from_utf8_lossy(&self.pending)
                        .trim_end_matches('\r')
                        .to_owned()
                });
                self.pending.clear();
                self.discarding = false;
            } else if !self.discarding {
                if self.pending.len() == 32 * 1024 {
                    self.pending.clear();
                    self.discarding = true;
                } else {
                    self.pending.push(b);
                }
            }
        }
    }
    pub fn finish(&mut self, emit: impl FnOnce(String)) {
        if self.discarding {
            emit("[Overlong output line omitted]".into());
            self.discarding = false;
        } else if !self.pending.is_empty() {
            emit(String::from_utf8_lossy(&self.pending).to_string());
            self.pending.clear();
        }
    }
}

pub fn redact(line: &str, token: &str) -> String {
    let mut clean = if token.is_empty() {
        line.to_owned()
    } else {
        line.replace(token, "[BOT_TOKEN REDACTED]")
    };
    for prefix in ["postgres://", "postgresql://"] {
        while let Some(start) = clean.find(prefix) {
            let end = clean[start..]
                .find(|c: char| c.is_whitespace() || c == '"' || c == '\'')
                .map_or(clean.len(), |n| start + n);
            clean.replace_range(start..end, "[DATABASE CONNECTION REDACTED]");
        }
    }
    // Remote output is treated as text, never as terminal control sequences.
    clean
        .chars()
        .filter(|c| !c.is_control() || *c == '\t' || *c == '\n')
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    fn event(index: usize, state: &str, completed: usize) -> Progress {
        Progress {
            version: 1,
            event: "step".into(),
            id: STAGES[index].into(),
            label: STAGES[index].into(),
            state: state.into(),
            completed,
            total: 7,
        }
    }
    #[test]
    fn strict_completion() {
        let mut t = Tracker::default();
        for i in 0..7 {
            t.accept(&event(i, "running", i)).unwrap();
            assert!(!t.is_complete());
            t.accept(&event(i, "done", i + 1)).unwrap();
        }
        assert!(t.is_complete());
    }
    #[test]
    fn failure_and_missing_steps_never_succeed() {
        let mut t = Tracker::default();
        assert!(t.accept(&event(6, "done", 7)).is_err());
        assert!(t.accept(&event(1, "running", 1)).is_err());
        t.accept(&event(0, "failed", 0)).unwrap();
        assert!(!t.is_complete());
    }
    #[test]
    fn split_stream_and_eof() {
        let mut l = Lines::default();
        let mut out = vec![];
        l.feed(b"abc", |s| out.push(s));
        l.feed(b"def\r\nlast", |s| out.push(s));
        l.finish(|s| out.push(s));
        assert_eq!(out, ["abcdef", "last"]);
    }
    #[test]
    fn secrets_and_escape_sequences() {
        let line = redact("secret postgresql://u:pw@h/db\x1b[2J", "secret");
        assert!(!line.contains("pw"));
        assert!(!line.contains("secret"));
        assert!(!line.contains('\x1b'));
    }
    #[test]
    fn overlong_lines_are_not_split_through_secrets() {
        let mut lines = Lines::default();
        let mut output = vec![];
        let text = format!("{}secret\nnext\n", "x".repeat(32765));
        lines.feed(text.as_bytes(), |line| output.push(line));
        assert_eq!(output, ["[Overlong output line omitted]", "next"]);
    }
}

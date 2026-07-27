use std::str::FromStr;
use std::sync::atomic::{AtomicU8, AtomicU64, Ordering};
use std::time::{Duration, Instant};

use serde::Deserialize;

pub const PROTOCOL_VERSION: u8 = 1;
pub const MAX_LINE_BYTES: usize = 2_048;
pub const MAX_CONTEXT_AGE: Duration = Duration::from_millis(750);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u8)]
pub enum Decision {
    Unknown = 0,
    Native = 1,
    Scroll = 2,
}

impl FromStr for Decision {
    type Err = crate::config::ConfigError;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        match value.trim().to_ascii_lowercase().as_str() {
            "unknown" => Ok(Self::Unknown),
            "native" => Ok(Self::Native),
            "scroll" => Ok(Self::Scroll),
            _ => Err(crate::config::ConfigError(format!(
                "invalid context decision {value:?}"
            ))),
        }
    }
}

impl Decision {
    fn from_atomic(value: u8) -> Self {
        match value {
            1 => Self::Native,
            2 => Self::Scroll,
            _ => Self::Unknown,
        }
    }
}

#[derive(Debug, Deserialize)]
pub struct ContextMessage {
    pub v: u8,
    #[serde(rename = "type")]
    pub message_type: String,
    pub decision: String,
}

impl ContextMessage {
    pub fn parse(line: &[u8]) -> Result<Decision, String> {
        if line.len() > MAX_LINE_BYTES {
            return Err("context message is too large".to_owned());
        }
        let message: Self = serde_json::from_slice(line).map_err(|error| error.to_string())?;
        if message.v != PROTOCOL_VERSION || message.message_type != "context" {
            return Err("unsupported context protocol message".to_owned());
        }
        message
            .decision
            .parse()
            .map_err(|error: crate::config::ConfigError| error.to_string())
    }
}

/// Lock-free cache read by the button-event hot path.
pub struct ContextCache {
    started: Instant,
    decision: AtomicU8,
    updated_millis: AtomicU64,
}

impl Default for ContextCache {
    fn default() -> Self {
        Self::new()
    }
}

impl ContextCache {
    pub fn new() -> Self {
        Self {
            started: Instant::now(),
            decision: AtomicU8::new(Decision::Unknown as u8),
            updated_millis: AtomicU64::new(0),
        }
    }

    fn elapsed_millis(&self) -> u64 {
        self.started.elapsed().as_millis().min(u64::MAX as u128) as u64
    }

    pub fn update(&self, decision: Decision) {
        // Store the value before its release timestamp. A reader acquiring the
        // timestamp will then observe the corresponding decision.
        self.decision.store(decision as u8, Ordering::Relaxed);
        self.updated_millis
            .store(self.elapsed_millis().max(1), Ordering::Release);
    }

    pub fn invalidate(&self) {
        // Raw pointer motion can reach the input daemon before the desktop
        // helper has classified the new coordinate. Make that gap fail native
        // instead of reusing a scroll decision from the previous location.
        self.updated_millis.store(0, Ordering::Release);
    }

    pub fn current(&self, unknown_action: Decision) -> Decision {
        let updated = self.updated_millis.load(Ordering::Acquire);
        let age = self.elapsed_millis().saturating_sub(updated);
        if updated == 0 || age > MAX_CONTEXT_AGE.as_millis() as u64 {
            return unknown_action;
        }
        let decision = Decision::from_atomic(self.decision.load(Ordering::Relaxed));
        if decision == Decision::Unknown {
            unknown_action
        } else {
            decision
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_wrong_protocol_version() {
        let line = br#"{"v":2,"type":"context","decision":"native"}"#;
        assert!(ContextMessage::parse(line).is_err());
    }

    #[test]
    fn parses_current_protocol() {
        let line = br#"{"v":1,"type":"context","decision":"scroll","role":"document web"}"#;
        assert_eq!(ContextMessage::parse(line).unwrap(), Decision::Scroll);
    }

    #[test]
    fn missing_context_is_native_by_default() {
        let cache = ContextCache::new();
        assert_eq!(cache.current(Decision::Native), Decision::Native);
    }

    #[test]
    fn update_is_visible_without_locking() {
        let cache = ContextCache::new();
        cache.update(Decision::Scroll);
        assert_eq!(cache.current(Decision::Native), Decision::Scroll);
    }

    #[test]
    fn pointer_motion_invalidates_a_cached_scroll_decision() {
        let cache = ContextCache::new();
        cache.update(Decision::Scroll);
        cache.invalidate();
        assert_eq!(cache.current(Decision::Native), Decision::Native);
    }
}

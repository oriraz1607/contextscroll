use std::str::FromStr;
use std::sync::atomic::{AtomicU8, AtomicU64, Ordering};
use std::sync::{Condvar, Mutex};
use std::time::{Duration, Instant};

use serde::Deserialize;

pub const PROTOCOL_VERSION: u8 = 2;
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
    #[serde(default)]
    pub decision: String,
    #[serde(default)]
    pub request_id: u64,
    #[serde(default)]
    pub generation: u64,
    #[serde(default)]
    pub paused: Option<bool>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ContextUpdate {
    pub decision: Decision,
    pub request_id: u64,
    pub generation: u64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ClientUpdate {
    Context(ContextUpdate),
    Control { paused: bool },
}

impl ContextMessage {
    pub fn parse(line: &[u8]) -> Result<ClientUpdate, String> {
        if line.len() > MAX_LINE_BYTES {
            return Err("context message is too large".to_owned());
        }
        let message: Self = serde_json::from_slice(line).map_err(|error| error.to_string())?;
        if message.v != PROTOCOL_VERSION {
            return Err("unsupported context protocol version".to_owned());
        }
        if message.message_type == "control" {
            return message
                .paused
                .map(|paused| ClientUpdate::Control { paused })
                .ok_or_else(|| "control message is missing paused".to_owned());
        }
        if message.message_type != "context" {
            return Err("unsupported context protocol message".to_owned());
        }
        let decision = message
            .decision
            .parse()
            .map_err(|error: crate::config::ConfigError| error.to_string())?;
        Ok(ClientUpdate::Context(ContextUpdate {
            decision,
            request_id: message.request_id,
            generation: message.generation,
        }))
    }
}

/// Lock-free cache read by the button-event hot path.
pub struct ContextCache {
    started: Instant,
    decision: AtomicU8,
    updated_millis: AtomicU64,
    updated_generation: AtomicU64,
    required_generation: AtomicU64,
    acknowledged_request: AtomicU64,
    wait_lock: Mutex<()>,
    wait_condition: Condvar,
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
            updated_generation: AtomicU64::new(0),
            required_generation: AtomicU64::new(0),
            acknowledged_request: AtomicU64::new(0),
            wait_lock: Mutex::new(()),
            wait_condition: Condvar::new(),
        }
    }

    fn elapsed_millis(&self) -> u64 {
        self.started.elapsed().as_millis().min(u64::MAX as u128) as u64
    }

    pub fn update(&self, update: ContextUpdate) {
        let _guard = self
            .wait_lock
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        // Store the value before its release timestamp. A reader acquiring the
        // timestamp will then observe the corresponding decision.
        self.decision
            .store(update.decision as u8, Ordering::Relaxed);
        self.updated_generation
            .store(update.generation, Ordering::Relaxed);
        self.updated_millis
            .store(self.elapsed_millis().max(1), Ordering::Release);
        self.acknowledged_request
            .fetch_max(update.request_id, Ordering::Release);
        self.wait_condition.notify_all();
    }

    pub fn invalidate(&self) {
        // Raw pointer motion can reach the input daemon before the desktop
        // helper has classified the new coordinate. Make that gap fail native
        // instead of reusing a scroll decision from the previous location.
        self.updated_millis.store(0, Ordering::Release);
    }

    pub fn clear(&self) {
        let _guard = self
            .wait_lock
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        self.decision
            .store(Decision::Unknown as u8, Ordering::Relaxed);
        self.updated_millis.store(0, Ordering::Release);
        self.updated_generation.store(0, Ordering::Relaxed);
        self.acknowledged_request.store(0, Ordering::Release);
        self.wait_condition.notify_all();
    }

    pub fn require_generation(&self, generation: u64) {
        let _guard = self
            .wait_lock
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        self.required_generation
            .fetch_max(generation, Ordering::Release);
        if self.updated_generation.load(Ordering::Acquire) < generation {
            self.invalidate();
        }
    }

    fn generation_is_current(&self) -> bool {
        self.updated_generation.load(Ordering::Acquire)
            >= self.required_generation.load(Ordering::Acquire)
    }

    pub fn current(&self, unknown_action: Decision) -> Decision {
        let updated = self.updated_millis.load(Ordering::Acquire);
        let age = self.elapsed_millis().saturating_sub(updated);
        if updated == 0 || age > MAX_CONTEXT_AGE.as_millis() as u64 || !self.generation_is_current()
        {
            return unknown_action;
        }
        let decision = Decision::from_atomic(self.decision.load(Ordering::Relaxed));
        if decision == Decision::Unknown {
            unknown_action
        } else {
            decision
        }
    }

    pub fn needs_refresh(&self) -> bool {
        let updated = self.updated_millis.load(Ordering::Acquire);
        updated == 0
            || self.elapsed_millis().saturating_sub(updated) > MAX_CONTEXT_AGE.as_millis() as u64
            || !self.generation_is_current()
            || Decision::from_atomic(self.decision.load(Ordering::Relaxed)) == Decision::Unknown
    }

    pub fn wait_for_request(
        &self,
        request_id: u64,
        timeout: Duration,
        unknown_action: Decision,
    ) -> Decision {
        let guard = self
            .wait_lock
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let _wait_result = self
            .wait_condition
            .wait_timeout_while(guard, timeout, |_| {
                self.acknowledged_request.load(Ordering::Acquire) < request_id
                    || !self.generation_is_current()
            })
            .unwrap_or_else(|error| error.into_inner());
        if self.acknowledged_request.load(Ordering::Acquire) < request_id
            || !self.generation_is_current()
        {
            unknown_action
        } else {
            self.current(unknown_action)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_wrong_protocol_version() {
        let line = br#"{"v":1,"type":"context","decision":"native"}"#;
        assert!(ContextMessage::parse(line).is_err());
    }

    #[test]
    fn parses_current_protocol() {
        let line = br#"{"v":2,"type":"context","decision":"scroll","role":"document web"}"#;
        assert_eq!(
            ContextMessage::parse(line).unwrap(),
            ClientUpdate::Context(ContextUpdate {
                decision: Decision::Scroll,
                request_id: 0,
                generation: 0,
            })
        );
    }

    #[test]
    fn parses_pause_control() {
        assert_eq!(
            ContextMessage::parse(br#"{"v":2,"type":"control","paused":true}"#).unwrap(),
            ClientUpdate::Control { paused: true }
        );
    }

    #[test]
    fn rejects_control_without_boolean_pause() {
        assert!(ContextMessage::parse(br#"{"v":2,"type":"control","paused":"yes"}"#).is_err());
    }

    #[test]
    fn missing_context_is_native_by_default() {
        let cache = ContextCache::new();
        assert_eq!(cache.current(Decision::Native), Decision::Native);
    }

    #[test]
    fn update_is_visible_without_locking() {
        let cache = ContextCache::new();
        cache.update(ContextUpdate {
            decision: Decision::Scroll,
            request_id: 0,
            generation: 0,
        });
        assert_eq!(cache.current(Decision::Native), Decision::Scroll);
    }

    #[test]
    fn clearing_context_fails_safe() {
        let cache = ContextCache::new();
        cache.update(ContextUpdate {
            decision: Decision::Scroll,
            request_id: 7,
            generation: 0,
        });
        assert_eq!(cache.current(Decision::Native), Decision::Scroll);
        cache.clear();
        assert_eq!(cache.current(Decision::Native), Decision::Native);
        assert!(cache.needs_refresh());
    }

    #[test]
    fn pointer_motion_invalidates_a_cached_scroll_decision() {
        let cache = ContextCache::new();
        cache.update(ContextUpdate {
            decision: Decision::Scroll,
            request_id: 0,
            generation: 0,
        });
        cache.invalidate();
        assert_eq!(cache.current(Decision::Native), Decision::Native);
    }

    #[test]
    fn acknowledged_refresh_returns_the_new_decision() {
        let cache = ContextCache::new();
        cache.update(ContextUpdate {
            decision: Decision::Scroll,
            request_id: 7,
            generation: 0,
        });
        assert_eq!(
            cache.wait_for_request(7, Duration::from_millis(1), Decision::Native,),
            Decision::Scroll
        );
    }

    #[test]
    fn unacknowledged_heartbeat_cannot_satisfy_a_refresh() {
        let cache = ContextCache::new();
        cache.update(ContextUpdate {
            decision: Decision::Scroll,
            request_id: 0,
            generation: 0,
        });
        assert_eq!(
            cache.wait_for_request(8, Duration::from_millis(1), Decision::Native,),
            Decision::Native
        );
    }

    #[test]
    fn pre_stop_decision_cannot_satisfy_post_stop_generation() {
        let cache = ContextCache::new();
        cache.update(ContextUpdate {
            decision: Decision::Scroll,
            request_id: 10,
            generation: 0,
        });
        cache.require_generation(1);

        cache.update(ContextUpdate {
            decision: Decision::Scroll,
            request_id: 11,
            generation: 0,
        });
        assert_eq!(
            cache.wait_for_request(11, Duration::from_millis(1), Decision::Native),
            Decision::Native
        );

        cache.update(ContextUpdate {
            decision: Decision::Native,
            request_id: 12,
            generation: 1,
        });
        assert_eq!(
            cache.wait_for_request(12, Duration::from_millis(1), Decision::Native),
            Decision::Native
        );
        assert!(!cache.needs_refresh());
    }
}

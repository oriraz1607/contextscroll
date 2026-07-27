use std::error::Error;
use std::fmt::{Display, Formatter};
use std::fs;
use std::path::Path;

use crate::context::Decision;
use crate::engine::Mode;

#[derive(Clone, Debug, PartialEq)]
pub struct Settings {
    pub mode: Mode,
    pub unknown_action: Decision,
    pub deadzone_px: f64,
    pub speed_multiplier: f64,
    pub speed_exponent: f64,
    pub maximum_px_per_second: f64,
    pub pixels_per_notch: f64,
    pub maximum_drag_px: f64,
    pub tick_hz: f64,
    pub natural_scrolling: bool,
    pub socket_path: String,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            mode: Mode::Toggle,
            unknown_action: Decision::Native,
            deadzone_px: 15.0,
            speed_multiplier: 0.0112,
            speed_exponent: 2.2,
            maximum_px_per_second: 30_000.0,
            pixels_per_notch: 55.0,
            maximum_drag_px: 1_200.0,
            tick_hz: 120.0,
            natural_scrolling: false,
            socket_path: "/run/contextscroll/context.sock".to_owned(),
        }
    }
}

#[derive(Debug)]
pub struct ConfigError(pub String);

impl Display for ConfigError {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(&self.0)
    }
}

impl Error for ConfigError {}

fn parse_bool(value: &str) -> Result<bool, ConfigError> {
    match value.trim().to_ascii_lowercase().as_str() {
        "1" | "true" | "yes" | "on" => Ok(true),
        "0" | "false" | "no" | "off" => Ok(false),
        _ => Err(ConfigError(format!("invalid boolean {value:?}"))),
    }
}

fn parse_number(key: &str, value: &str) -> Result<f64, ConfigError> {
    value
        .parse::<f64>()
        .map_err(|_| ConfigError(format!("{key} is not a number")))
}

impl Settings {
    pub fn load(path: &Path) -> Result<Self, ConfigError> {
        let text = match fs::read_to_string(path) {
            Ok(text) => text,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                return Ok(Self::default());
            }
            Err(error) => {
                return Err(ConfigError(format!("{}: {error}", path.display())));
            }
        };
        let mut settings = Self::default();
        for (index, original) in text.lines().enumerate() {
            let line = original.split('#').next().unwrap_or_default().trim();
            if line.is_empty() {
                continue;
            }
            let Some((key, value)) = line.split_once('=') else {
                return Err(ConfigError(format!(
                    "{}:{}: expected KEY = value",
                    path.display(),
                    index + 1
                )));
            };
            let key = key.trim();
            let value = value.trim();
            let update = match key {
                "MODE" => {
                    settings.mode = value.parse()?;
                    Ok(())
                }
                "UNKNOWN_ACTION" => {
                    settings.unknown_action = value.parse()?;
                    if settings.unknown_action == Decision::Unknown {
                        Err(ConfigError(
                            "UNKNOWN_ACTION must be native or scroll".to_owned(),
                        ))
                    } else {
                        Ok(())
                    }
                }
                "DEADZONE_PX" => {
                    settings.deadzone_px = parse_number(key, value)?;
                    Ok(())
                }
                "SPEED_MULTIPLIER" => {
                    settings.speed_multiplier = parse_number(key, value)?;
                    Ok(())
                }
                "SPEED_EXPONENT" => {
                    settings.speed_exponent = parse_number(key, value)?;
                    Ok(())
                }
                "MAXIMUM_PX_PER_SECOND" => {
                    settings.maximum_px_per_second = parse_number(key, value)?;
                    Ok(())
                }
                "PIXELS_PER_NOTCH" => {
                    settings.pixels_per_notch = parse_number(key, value)?;
                    Ok(())
                }
                "MAXIMUM_DRAG_PX" => {
                    settings.maximum_drag_px = parse_number(key, value)?;
                    Ok(())
                }
                "TICK_HZ" => {
                    settings.tick_hz = parse_number(key, value)?;
                    Ok(())
                }
                "NATURAL_SCROLLING" => {
                    settings.natural_scrolling = parse_bool(value)?;
                    Ok(())
                }
                "SOCKET_PATH" => {
                    settings.socket_path = value.to_owned();
                    Ok(())
                }
                _ => Err(ConfigError(format!("unknown key {key}"))),
            };
            if let Err(error) = update {
                return Err(ConfigError(format!(
                    "{}:{}: {error}",
                    path.display(),
                    index + 1
                )));
            }
        }
        settings.validate()?;
        Ok(settings)
    }

    pub fn validate(&self) -> Result<(), ConfigError> {
        if !self.deadzone_px.is_finite() || self.deadzone_px < 0.0 {
            return Err(ConfigError(
                "DEADZONE_PX must be finite and non-negative".to_owned(),
            ));
        }
        for (key, value) in [
            ("SPEED_MULTIPLIER", self.speed_multiplier),
            ("SPEED_EXPONENT", self.speed_exponent),
            ("MAXIMUM_PX_PER_SECOND", self.maximum_px_per_second),
            ("PIXELS_PER_NOTCH", self.pixels_per_notch),
            ("MAXIMUM_DRAG_PX", self.maximum_drag_px),
            ("TICK_HZ", self.tick_hz),
        ] {
            if !value.is_finite() || value <= 0.0 {
                return Err(ConfigError(format!(
                    "{key} must be finite and greater than zero"
                )));
            }
        }
        if self.socket_path.is_empty() {
            return Err(ConfigError("SOCKET_PATH must not be empty".to_owned()));
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn temporary_path() -> std::path::PathBuf {
        std::env::temp_dir().join(format!(
            "contextscroll-config-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ))
    }

    #[test]
    fn defaults_are_safe_and_timer_free() {
        let settings = Settings::default();
        assert_eq!(settings.unknown_action, Decision::Native);
        assert_eq!(settings.mode, Mode::Toggle);
    }

    #[test]
    fn loads_supported_values() {
        let path = temporary_path();
        fs::write(
            &path,
            "MODE = hold\nUNKNOWN_ACTION = scroll\nTICK_HZ = 144\n\
             NATURAL_SCROLLING = true\n",
        )
        .unwrap();
        let settings = Settings::load(&path).unwrap();
        let _ = fs::remove_file(path);
        assert_eq!(settings.mode, Mode::Hold);
        assert_eq!(settings.unknown_action, Decision::Scroll);
        assert_eq!(settings.tick_hz, 144.0);
        assert!(settings.natural_scrolling);
    }

    #[test]
    fn rejects_latency_style_unknown_keys() {
        let path = temporary_path();
        fs::write(&path, "TOGGLE_HOLD_MS = 150\n").unwrap();
        let result = Settings::load(&path);
        let _ = fs::remove_file(path);
        assert!(result.is_err());
    }
}

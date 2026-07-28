use std::fmt::{Display, Formatter};
use std::str::FromStr;

use crate::config::ConfigError;
use crate::context::Decision;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Mode {
    Toggle,
    Hold,
}

impl Display for Mode {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(match self {
            Self::Toggle => "toggle",
            Self::Hold => "hold",
        })
    }
}

impl FromStr for Mode {
    type Err = ConfigError;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        match value.trim().to_ascii_lowercase().as_str() {
            "toggle" => Ok(Self::Toggle),
            "hold" => Ok(Self::Hold),
            _ => Err(ConfigError(format!("invalid mode {value:?}"))),
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Route {
    Forward,
    Consume,
    Start,
    Stop,
}

#[derive(Debug, Default)]
pub struct Interaction {
    pub scrolling: bool,
    native_middle_down: bool,
    starting_release_pending: bool,
    consumed_release: Option<u16>,
    pub dx: f64,
    pub dy: f64,
    pub visual_dx: f64,
    pub visual_dy: f64,
}

impl Interaction {
    pub fn stop(&mut self) {
        self.scrolling = false;
        self.starting_release_pending = false;
        self.dx = 0.0;
        self.dy = 0.0;
        self.visual_dx = 0.0;
        self.visual_dy = 0.0;
    }

    pub fn button(
        &mut self,
        code: u16,
        value: i32,
        middle_code: u16,
        decision: Decision,
        mode: Mode,
    ) -> Route {
        let is_press = value == 1;
        let is_release = value == 0;

        if is_release && self.consumed_release == Some(code) {
            self.consumed_release = None;
            return Route::Consume;
        }

        if mode == Mode::Toggle && self.scrolling {
            if code == middle_code && is_release && self.starting_release_pending {
                self.starting_release_pending = false;
                return Route::Consume;
            }
            if is_press {
                self.stop();
                self.consumed_release = Some(code);
                return Route::Stop;
            }
            return if code == middle_code {
                Route::Consume
            } else {
                Route::Forward
            };
        }

        if code != middle_code {
            return Route::Forward;
        }

        if is_press {
            if decision != Decision::Scroll {
                self.native_middle_down = true;
                return Route::Forward;
            }
            self.native_middle_down = false;
            self.scrolling = true;
            self.dx = 0.0;
            self.dy = 0.0;
            self.visual_dx = 0.0;
            self.visual_dy = 0.0;
            if mode == Mode::Toggle {
                self.starting_release_pending = true;
            }
            return Route::Start;
        }

        if is_release {
            if self.native_middle_down {
                self.native_middle_down = false;
                return Route::Forward;
            }
            if mode == Mode::Hold && self.scrolling {
                self.stop();
                return Route::Stop;
            }
            if self.starting_release_pending {
                self.starting_release_pending = false;
            }
            return Route::Consume;
        }
        Route::Forward
    }

    pub fn motion(&mut self, x: f64, y: f64, _mode: Mode, maximum: f64) -> bool {
        if !self.scrolling {
            return true;
        }
        self.visual_dx = (self.visual_dx + x).clamp(-maximum, maximum);
        self.visual_dy = (self.visual_dy + y).clamp(-maximum, maximum);
        self.dx = self.visual_dx;
        self.dy = self.visual_dy;
        // Keep the compositor pointer at the activation point. Forwarding
        // toggle-mode motion allowed it to enter Chromium's tab strip, where
        // the generated wheel events could switch tabs instead of scrolling
        // the page that started the interaction.
        false
    }

    pub fn finish_motion_batch(&mut self, deadzone: f64) {
        if !self.scrolling {
            return;
        }
        self.dx = self.visual_dx;
        self.dy = self.visual_dy;
        if self.visual_dy.abs() > deadzone && self.visual_dy.abs() >= self.visual_dx.abs() * 1.5 {
            self.dx = 0.0;
        } else if self.visual_dx.abs() > deadzone
            && self.visual_dx.abs() >= self.visual_dy.abs() * 1.5
        {
            self.dy = 0.0;
        }
    }
}

fn speed(offset: f64, deadzone: f64, multiplier: f64, exponent: f64, maximum: f64) -> f64 {
    if offset.abs() <= deadzone {
        return 0.0;
    }
    offset.signum() * (multiplier * offset.abs().powf(exponent)).min(maximum)
}

#[derive(Debug, Default)]
pub struct WheelAccumulator {
    vertical: f64,
    horizontal: f64,
    vertical_notch: f64,
    horizontal_notch: f64,
}

impl WheelAccumulator {
    pub fn clear(&mut self) {
        *self = Self::default();
    }

    #[allow(clippy::too_many_arguments)]
    pub fn step(
        &mut self,
        dx: f64,
        dy: f64,
        seconds: f64,
        deadzone: f64,
        multiplier: f64,
        exponent: f64,
        maximum: f64,
        pixels_per_notch: f64,
        natural: bool,
    ) -> [i32; 4] {
        let direction = if natural { 1.0 } else { -1.0 };
        let units_per_pixel = 120.0 / pixels_per_notch;
        self.vertical += direction
            * speed(dy, deadzone, multiplier, exponent, maximum)
            * units_per_pixel
            * seconds;
        self.horizontal += -direction
            * speed(dx, deadzone, multiplier, exponent, maximum)
            * units_per_pixel
            * seconds;

        let vertical_hires = self.vertical.trunc() as i32;
        let horizontal_hires = self.horizontal.trunc() as i32;
        self.vertical -= f64::from(vertical_hires);
        self.horizontal -= f64::from(horizontal_hires);
        self.vertical_notch += f64::from(vertical_hires);
        self.horizontal_notch += f64::from(horizontal_hires);

        let vertical_notch = (self.vertical_notch / 120.0).trunc() as i32;
        let horizontal_notch = (self.horizontal_notch / 120.0).trunc() as i32;
        self.vertical_notch -= f64::from(vertical_notch * 120);
        self.horizontal_notch -= f64::from(horizontal_notch * 120);
        [
            vertical_hires,
            vertical_notch,
            horizontal_hires,
            horizontal_notch,
        ]
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const MIDDLE: u16 = 0x112;
    const LEFT: u16 = 0x110;

    #[test]
    fn native_context_forwards_press_and_release_immediately() {
        let mut state = Interaction::default();
        assert_eq!(
            state.button(MIDDLE, 1, MIDDLE, Decision::Native, Mode::Toggle),
            Route::Forward
        );
        assert_eq!(
            state.button(MIDDLE, 0, MIDDLE, Decision::Unknown, Mode::Toggle),
            Route::Forward
        );
    }

    #[test]
    fn scroll_context_starts_without_a_timer() {
        let mut state = Interaction::default();
        assert_eq!(
            state.button(MIDDLE, 1, MIDDLE, Decision::Scroll, Mode::Toggle),
            Route::Start
        );
        assert!(state.scrolling);
        assert_eq!(
            state.button(MIDDLE, 0, MIDDLE, Decision::Scroll, Mode::Toggle),
            Route::Consume
        );
        assert!(state.scrolling);
    }

    #[test]
    fn stopping_click_does_not_reach_the_application() {
        let mut state = Interaction::default();
        state.button(MIDDLE, 1, MIDDLE, Decision::Scroll, Mode::Toggle);
        state.button(MIDDLE, 0, MIDDLE, Decision::Scroll, Mode::Toggle);
        assert_eq!(
            state.button(LEFT, 1, MIDDLE, Decision::Scroll, Mode::Toggle),
            Route::Stop
        );
        assert_eq!(
            state.button(LEFT, 0, MIDDLE, Decision::Scroll, Mode::Toggle),
            Route::Consume
        );
    }

    #[test]
    fn stopping_middle_release_does_not_consume_the_next_middle_click() {
        let mut state = Interaction::default();
        state.button(MIDDLE, 1, MIDDLE, Decision::Scroll, Mode::Toggle);
        state.button(MIDDLE, 0, MIDDLE, Decision::Scroll, Mode::Toggle);
        assert_eq!(
            state.button(MIDDLE, 1, MIDDLE, Decision::Scroll, Mode::Toggle),
            Route::Stop
        );
        assert_eq!(
            state.button(MIDDLE, 0, MIDDLE, Decision::Scroll, Mode::Toggle),
            Route::Consume
        );
        assert_eq!(
            state.button(MIDDLE, 1, MIDDLE, Decision::Native, Mode::Toggle),
            Route::Forward
        );
        assert_eq!(
            state.button(MIDDLE, 0, MIDDLE, Decision::Native, Mode::Toggle),
            Route::Forward
        );
    }

    #[test]
    fn toggle_scroll_accumulates_motion_without_moving_pointer() {
        let mut state = Interaction::default();
        state.button(MIDDLE, 1, MIDDLE, Decision::Scroll, Mode::Toggle);

        assert!(!state.motion(3.0, -8.0, Mode::Toggle, 1_200.0,));
        assert_eq!(state.dx, 3.0);
        assert_eq!(state.dy, -8.0);
        assert_eq!(state.visual_dx, 3.0);
        assert_eq!(state.visual_dy, -8.0);
    }

    #[test]
    fn vertical_gesture_locks_out_horizontal_jitter() {
        let mut state = Interaction::default();
        state.button(MIDDLE, 1, MIDDLE, Decision::Scroll, Mode::Toggle);

        state.motion(4.0, -30.0, Mode::Toggle, 1_200.0);
        state.finish_motion_batch(15.0);
        state.motion(-20.0, -40.0, Mode::Toggle, 1_200.0);
        state.finish_motion_batch(15.0);

        assert_eq!(state.dx, 0.0);
        assert_eq!(state.dy, -70.0);
        assert_eq!(state.visual_dx, -16.0);
        assert_eq!(state.visual_dy, -70.0);
    }

    #[test]
    fn diagonal_gesture_keeps_both_axes_available() {
        let mut state = Interaction::default();
        state.button(MIDDLE, 1, MIDDLE, Decision::Scroll, Mode::Toggle);

        state.motion(30.0, -30.0, Mode::Toggle, 1_200.0);
        state.finish_motion_batch(15.0);

        assert_eq!(state.dx, 30.0);
        assert_eq!(state.dy, -30.0);
        assert_eq!(state.visual_dx, 30.0);
        assert_eq!(state.visual_dy, -30.0);
    }

    #[test]
    fn dominant_axis_filter_yields_to_a_direction_change() {
        let mut state = Interaction::default();
        state.button(MIDDLE, 1, MIDDLE, Decision::Scroll, Mode::Toggle);

        state.motion(2.0, -60.0, Mode::Toggle, 1_200.0);
        state.finish_motion_batch(15.0);
        assert_eq!(state.dx, 0.0);
        assert_eq!(state.dy, -60.0);

        state.motion(50.0, 40.0, Mode::Toggle, 1_200.0);
        state.finish_motion_batch(15.0);
        assert_eq!(state.dx, 52.0);
        assert_eq!(state.dy, 0.0);
        assert_eq!(state.visual_dx, 52.0);
        assert_eq!(state.visual_dy, -20.0);
    }

    #[test]
    fn deadzone_produces_no_wheel_events() {
        let mut wheel = WheelAccumulator::default();
        assert_eq!(
            wheel.step(10.0, 10.0, 0.1, 15.0, 0.008, 2.2, 30_000.0, 55.0, false),
            [0; 4]
        );
    }
}

use std::collections::{HashMap, HashSet};
use std::error::Error;
use std::fs;
use std::io;
use std::os::fd::AsRawFd;
use std::os::unix::fs::PermissionsExt;
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

use contextscroll::config::Settings;
use contextscroll::context::{ContextCache, ContextMessage, MAX_LINE_BYTES};
use contextscroll::engine::{Interaction, Route, WheelAccumulator};
use evdev::uinput::VirtualDevice;
use evdev::{
    AbsoluteAxisCode, AttributeSet, Device, EventType, InputEvent, KeyCode, RelativeAxisCode,
    SynchronizationCode,
};
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::net::{UnixListener, UnixStream};
use tokio::signal::unix::{SignalKind, signal};
use tokio::task::JoinHandle;
use tokio::time::{MissedTickBehavior, interval};

type AnyError = Box<dyn Error + Send + Sync>;

const DEFAULT_CONFIG: &str = "/etc/contextscroll.conf";
const VIRTUAL_NAME_PREFIX: &str = "ContextScroll virtual: ";
const BUTTON_MIN: u16 = 0x110;
const BUTTON_MAX_EXCLUSIVE: u16 = 0x120;

#[derive(Debug)]
struct Arguments {
    config: PathBuf,
    debug: bool,
    check_config: bool,
    list_devices: bool,
}

fn parse_arguments() -> Result<Arguments, String> {
    let mut result = Arguments {
        config: PathBuf::from(DEFAULT_CONFIG),
        debug: false,
        check_config: false,
        list_devices: false,
    };
    let mut arguments = std::env::args().skip(1);
    while let Some(argument) = arguments.next() {
        match argument.as_str() {
            "--config" => {
                result.config = arguments
                    .next()
                    .map(PathBuf::from)
                    .ok_or_else(|| "--config requires a path".to_owned())?;
            }
            "--debug" => result.debug = true,
            "--check-config" => result.check_config = true,
            "--list-devices" => result.list_devices = true,
            "--version" => {
                println!("contextscroll {}", env!("CARGO_PKG_VERSION"));
                std::process::exit(0);
            }
            "--help" | "-h" => {
                println!(
                    "Usage: contextscroll [--config PATH] [--debug]\n\
                     \x20                    [--check-config] [--list-devices]"
                );
                std::process::exit(0);
            }
            _ => return Err(format!("unknown argument {argument:?}")),
        }
    }
    Ok(result)
}

fn debug(enabled: bool, message: impl std::fmt::Display) {
    if enabled {
        eprintln!("DEBUG: {message}");
    }
}

fn is_mouse(device: &Device) -> bool {
    if device
        .name()
        .is_some_and(|name| name.starts_with(VIRTUAL_NAME_PREFIX))
    {
        return false;
    }
    let keys = device.supported_keys();
    let relative = device.supported_relative_axes();
    let absolute = device.supported_absolute_axes();
    keys.is_some_and(|set| set.contains(KeyCode::BTN_MIDDLE))
        && relative.is_some_and(|set| {
            set.contains(RelativeAxisCode::REL_X) && set.contains(RelativeAxisCode::REL_Y)
        })
        && !absolute.is_some_and(|set| {
            set.contains(AbsoluteAxisCode::ABS_X)
                || set.contains(AbsoluteAxisCode::ABS_MT_POSITION_X)
        })
}

fn virtual_mouse(device: &Device) -> Result<VirtualDevice, AnyError> {
    let keys: AttributeSet<KeyCode> = device
        .supported_keys()
        .ok_or("mouse has no key capabilities")?
        .iter()
        .collect();
    let mut relative: AttributeSet<RelativeAxisCode> = device
        .supported_relative_axes()
        .ok_or("mouse has no relative-axis capabilities")?
        .iter()
        .collect();
    for axis in [
        RelativeAxisCode::REL_X,
        RelativeAxisCode::REL_Y,
        RelativeAxisCode::REL_WHEEL,
        RelativeAxisCode::REL_HWHEEL,
        RelativeAxisCode::REL_WHEEL_HI_RES,
        RelativeAxisCode::REL_HWHEEL_HI_RES,
    ] {
        relative.insert(axis);
    }

    let name = format!(
        "{VIRTUAL_NAME_PREFIX}{}",
        device.name().unwrap_or("unnamed mouse")
    );
    let mut builder = VirtualDevice::builder()
        .map_err(|error| format!("opening /dev/uinput: {error}"))?
        .name(&name)
        .input_id(device.input_id())
        .with_keys(&keys)
        .map_err(|error| format!("copying mouse buttons: {error}"))?
        .with_relative_axes(&relative)
        .map_err(|error| format!("setting relative axes: {error}"))?;
    if !device.properties().iter().collect::<Vec<_>>().is_empty() {
        builder = builder
            .with_properties(device.properties())
            .map_err(|error| format!("copying input properties: {error}"))?;
    }
    Ok(builder
        .build()
        .map_err(|error| format!("creating virtual mouse: {error}"))?)
}

fn is_button(code: u16) -> bool {
    (BUTTON_MIN..BUTTON_MAX_EXCLUSIVE).contains(&code)
}

fn relative_event(axis: RelativeAxisCode, value: i32) -> InputEvent {
    InputEvent::new(EventType::RELATIVE.0, axis.0, value)
}

fn release_event(code: u16) -> InputEvent {
    InputEvent::new(EventType::KEY.0, code, 0)
}

fn poll_input(device: &Device, timeout: Duration) -> io::Result<bool> {
    let milliseconds = timeout.as_micros().div_ceil(1_000).min(i32::MAX as u128) as i32;
    let mut descriptor = libc::pollfd {
        fd: device.as_raw_fd(),
        events: libc::POLLIN,
        revents: 0,
    };
    loop {
        // SAFETY: descriptor points to one initialized pollfd for the entire
        // call and its file descriptor remains owned by `device`.
        let result = unsafe { libc::poll(&mut descriptor, 1, milliseconds) };
        if result > 0 {
            if descriptor.revents & libc::POLLIN != 0 {
                return Ok(true);
            }
            return Err(io::Error::other(format!(
                "input device poll failed (revents={:#x})",
                descriptor.revents
            )));
        }
        if result == 0 {
            return Ok(false);
        }
        let error = io::Error::last_os_error();
        if error.kind() != io::ErrorKind::Interrupted {
            return Err(error);
        }
    }
}

fn run_mouse(
    path: PathBuf,
    mut device: Device,
    settings: Arc<Settings>,
    cache: Arc<ContextCache>,
    shutdown: Arc<AtomicBool>,
    debug_enabled: bool,
) -> Result<(), AnyError> {
    device.grab()?;
    device.set_nonblocking(true)?;
    let name = device.name().unwrap_or("unnamed mouse").to_owned();
    let mut output = virtual_mouse(&device)?;
    let mut interaction = Interaction::default();
    let mut wheel = WheelAccumulator::default();
    let mut batch = Vec::<InputEvent>::with_capacity(16);
    let mut held = HashSet::<u16>::new();
    let period = Duration::from_secs_f64(1.0 / settings.tick_hz);
    let mut previous_tick = Instant::now();
    let mut next_tick = previous_tick + period;
    let mut last_wheel_debug = previous_tick
        .checked_sub(Duration::from_secs(1))
        .unwrap_or(previous_tick);

    eprintln!("INFO: grabbed {name} at {}", path.display());
    let result: Result<(), AnyError> = (|| {
        while !shutdown.load(Ordering::Relaxed) {
            let now = Instant::now();
            let timeout = if interaction.scrolling {
                next_tick
                    .saturating_duration_since(now)
                    .min(Duration::from_millis(250))
            } else {
                Duration::from_millis(250)
            };

            if poll_input(&device, timeout)? {
                let events: Vec<_> = match device.fetch_events() {
                    Ok(events) => events.collect(),
                    Err(error) if error.kind() == io::ErrorKind::WouldBlock => Vec::new(),
                    Err(error) => return Err(error.into()),
                };
                for event in events {
                    let event_type = event.event_type();
                    let code = event.code();
                    let value = event.value();

                    if event_type == EventType::KEY && is_button(code) {
                        let decision = cache.current(settings.unknown_action);
                        let route = interaction.button(
                            code,
                            value,
                            KeyCode::BTN_MIDDLE.0,
                            decision,
                            settings.mode,
                        );
                        if code == KeyCode::BTN_MIDDLE.0 && value == 1 {
                            debug(
                                debug_enabled,
                                format!(
                                    "{}: middle press decision={decision:?} route={route:?}",
                                    path.display()
                                ),
                            );
                        }
                        match route {
                            Route::Forward => {
                                batch.push(event);
                                if value == 1 {
                                    held.insert(code);
                                } else if value == 0 {
                                    held.remove(&code);
                                }
                            }
                            Route::Consume => {}
                            Route::Start => {
                                wheel.clear();
                                previous_tick = Instant::now();
                                next_tick = previous_tick + period;
                                debug(
                                    debug_enabled,
                                    format!(
                                        "{}: autoscroll started ({decision:?})",
                                        path.display()
                                    ),
                                );
                            }
                            Route::Stop => {
                                wheel.clear();
                                previous_tick = Instant::now();
                                next_tick = previous_tick + period;
                                debug(
                                    debug_enabled,
                                    format!("{}: autoscroll stopped", path.display()),
                                );
                            }
                        }
                        continue;
                    }

                    if event_type == EventType::RELATIVE
                        && (code == RelativeAxisCode::REL_X.0 || code == RelativeAxisCode::REL_Y.0)
                    {
                        let forward = interaction.motion(
                            if code == RelativeAxisCode::REL_X.0 {
                                f64::from(value)
                            } else {
                                0.0
                            },
                            if code == RelativeAxisCode::REL_Y.0 {
                                f64::from(value)
                            } else {
                                0.0
                            },
                            settings.mode,
                            settings.maximum_drag_px,
                        );
                        if forward {
                            batch.push(event);
                        }
                        continue;
                    }

                    if event_type == EventType::SYNCHRONIZATION {
                        if code == SynchronizationCode::SYN_REPORT.0 && !batch.is_empty() {
                            output.emit(&batch)?;
                            batch.clear();
                        }
                        continue;
                    }
                    if event_type == EventType::KEY || event_type == EventType::RELATIVE {
                        batch.push(event);
                    }
                }
            }

            let now = Instant::now();
            if interaction.scrolling && now >= next_tick && batch.is_empty() {
                let seconds = now
                    .duration_since(previous_tick)
                    .min(Duration::from_millis(250))
                    .as_secs_f64();
                previous_tick = now;
                next_tick = now + period;
                let values = wheel.step(
                    interaction.dx,
                    interaction.dy,
                    seconds,
                    settings.deadzone_px,
                    settings.speed_multiplier,
                    settings.speed_exponent,
                    settings.maximum_px_per_second,
                    settings.pixels_per_notch,
                    settings.natural_scrolling,
                );
                let mut events = Vec::with_capacity(4);
                for (axis, value) in [
                    (RelativeAxisCode::REL_WHEEL_HI_RES, values[0]),
                    (RelativeAxisCode::REL_WHEEL, values[1]),
                    (RelativeAxisCode::REL_HWHEEL_HI_RES, values[2]),
                    (RelativeAxisCode::REL_HWHEEL, values[3]),
                ] {
                    if value != 0 {
                        events.push(relative_event(axis, value));
                    }
                }
                if !events.is_empty() {
                    output.emit(&events)?;
                    if debug_enabled
                        && now.duration_since(last_wheel_debug) >= Duration::from_millis(250)
                    {
                        eprintln!(
                            "DEBUG: {}: wheel dx={:.1} dy={:.1} values={values:?}",
                            path.display(),
                            interaction.dx,
                            interaction.dy,
                        );
                        last_wheel_debug = now;
                    }
                }
            } else if !interaction.scrolling {
                previous_tick = now;
                next_tick = now + period;
            }
        }
        Ok(())
    })();

    if !held.is_empty() {
        let releases: Vec<_> = held.into_iter().map(release_event).collect();
        let _ = output.emit(&releases);
    }
    eprintln!("INFO: released {}", path.display());
    result
}

fn uid_has_desktop_session(uid: u32) -> bool {
    let path = format!("/run/systemd/users/{uid}");
    let Ok(text) = fs::read_to_string(path) else {
        return false;
    };
    text.lines().any(|line| {
        line.strip_prefix("STATE=")
            .is_some_and(|state| matches!(state, "active" | "online"))
    })
}

async fn handle_context_client(
    stream: UnixStream,
    cache: Arc<ContextCache>,
    debug_enabled: bool,
) -> Result<(), AnyError> {
    let credentials = stream.peer_cred()?;
    let uid = credentials.uid();
    if !uid_has_desktop_session(uid) {
        return Err(format!("rejected context client uid={uid}").into());
    }
    debug(debug_enabled, format!("context client connected uid={uid}"));
    let mut reader = BufReader::with_capacity(MAX_LINE_BYTES + 1, stream);
    let mut line = Vec::<u8>::with_capacity(512);
    loop {
        let available = reader.fill_buf().await?;
        if available.is_empty() {
            break;
        }
        let end = available
            .iter()
            .position(|byte| *byte == b'\n')
            .map_or(available.len(), |index| index + 1);
        if line.len() + end > MAX_LINE_BYTES {
            return Err("context message exceeded size limit".into());
        }
        line.extend_from_slice(&available[..end]);
        reader.consume(end);
        if line.last() != Some(&b'\n') {
            continue;
        }
        match ContextMessage::parse(&line) {
            Ok(decision) => {
                cache.update(decision);
                debug(debug_enabled, format!("context updated to {decision:?}"));
            }
            Err(error) => return Err(error.into()),
        }
        line.clear();
    }
    Ok(())
}

async fn context_server(
    path: PathBuf,
    cache: Arc<ContextCache>,
    debug_enabled: bool,
) -> Result<(), AnyError> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    match fs::remove_file(&path) {
        Ok(()) => {}
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => return Err(error.into()),
    }
    let listener = UnixListener::bind(&path)?;
    fs::set_permissions(&path, fs::Permissions::from_mode(0o666))?;
    eprintln!("INFO: context socket ready at {}", path.display());
    loop {
        let (stream, _) = listener.accept().await?;
        let client_cache = Arc::clone(&cache);
        tokio::spawn(async move {
            if let Err(error) = handle_context_client(stream, client_cache, debug_enabled).await {
                debug(debug_enabled, format!("context client closed: {error}"));
            }
        });
    }
}

fn print_devices() {
    for (path, device) in evdev::enumerate() {
        let kind = if is_mouse(&device) {
            "mouse"
        } else {
            "ignored"
        };
        println!(
            "{}\t{}\t{}",
            path.display(),
            kind,
            device.name().unwrap_or("unnamed")
        );
    }
}

async fn run(arguments: Arguments) -> Result<(), AnyError> {
    let settings = Arc::new(Settings::load(&arguments.config)?);
    if arguments.check_config {
        println!("{settings:#?}");
        return Ok(());
    }
    if arguments.list_devices {
        print_devices();
        return Ok(());
    }

    let cache = Arc::new(ContextCache::new());
    let shutdown = Arc::new(AtomicBool::new(false));
    let socket_task = tokio::spawn(context_server(
        PathBuf::from(&settings.socket_path),
        Arc::clone(&cache),
        arguments.debug,
    ));
    let mut device_tasks = HashMap::<PathBuf, JoinHandle<()>>::new();
    let mut ignored = HashSet::<PathBuf>::new();
    let mut discovery = interval(Duration::from_secs(1));
    discovery.set_missed_tick_behavior(MissedTickBehavior::Skip);
    let mut terminate = signal(SignalKind::terminate())?;
    let mut interrupt = signal(SignalKind::interrupt())?;

    eprintln!("INFO: running in {} mode", settings.mode);
    loop {
        tokio::select! {
            _ = discovery.tick() => {
                device_tasks.retain(|_, handle| !handle.is_finished());
                for (path, device) in evdev::enumerate() {
                    if device_tasks.contains_key(&path) || ignored.contains(&path) {
                        continue;
                    }
                    if !is_mouse(&device) {
                        ignored.insert(path);
                        continue;
                    }
                    let task_settings = Arc::clone(&settings);
                    let task_cache = Arc::clone(&cache);
                    let task_shutdown = Arc::clone(&shutdown);
                    let task_path = path.clone();
                    let debug_enabled = arguments.debug;
                    let handle = tokio::task::spawn_blocking(move || {
                        if let Err(error) = run_mouse(
                            task_path.clone(),
                            device,
                            task_settings,
                            task_cache,
                            task_shutdown,
                            debug_enabled,
                        ) {
                            eprintln!("WARN: {}: {error}", task_path.display());
                        }
                    });
                    device_tasks.insert(path, handle);
                }
            }
            _ = terminate.recv() => break,
            _ = interrupt.recv() => break,
        }
    }

    shutdown.store(true, Ordering::Relaxed);
    socket_task.abort();
    for (_, task) in device_tasks {
        let _ = task.await;
    }
    let _ = fs::remove_file(&settings.socket_path);
    Ok(())
}

#[tokio::main(flavor = "current_thread")]
async fn main() {
    match parse_arguments() {
        Ok(arguments) => {
            if let Err(error) = run(arguments).await {
                eprintln!("ERROR: {error}");
                std::process::exit(2);
            }
        }
        Err(error) => {
            eprintln!("ERROR: {error}");
            std::process::exit(2);
        }
    }
}

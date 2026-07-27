# ContextScroll

Context-aware middle-button autoscrolling for Linux.

ContextScroll knows the difference between a browser tab and the page below
it. A middle click on a tab, link, button, or editable field is delivered to
the application immediately. A middle click on a document or scrollable view
starts autoscroll.

The semantic policy also covers LibreOffice Writer document bodies and
standard desktop scroll surfaces such as directory panes, lists, tables,
trees, and scroll panes. Writer links and toolbar controls remain native.
Fallbacks cover common document/PDF/image/help viewers (including Papers,
Evince, Okular, Foliate, Loupe, and Yelp) and blank folder views in Files,
Nautilus, Dolphin, Nemo, and Thunar. File icons and viewer controls remain
native.

There is no hold threshold, click timeout, or latency parameter.

On GNOME Wayland, the normal pointer changes to a compact black
four-direction autoscroll cursor with a white outline while autoscroll is
active. The replacement cursor is click-through and follows the physical
pointer until autoscroll stops.

## How it works

ContextScroll is two deliberately separate processes:

1. The tiny `contextscroll-pointer@local` GNOME Shell extension publishes
   compositor pointer coordinates and the window beneath them at no more
   than 60 Hz.
2. `contextscroll-context` runs as your desktop user. It combines those
   coordinates with AT-SPI roles to classify the accessible object beneath
   the pointer and tells the Shell bridge when to show the autoscroll cursor.
   On X11 it samples coordinates through Xlib instead.
3. `contextscroll` is a small Rust system daemon. It mirrors physical mice
   through evdev/uinput and keeps the most recent classification in a
   lock-free cache. It reports only the aggregate active/inactive state back
   to the session helper.

When the middle button is pressed, the Rust path reads two atomics and makes
the decision immediately:

| Context under pointer | Result |
| --- | --- |
| Tab, link, button, menu, slider, editable field | Native middle click |
| Web/text document, list, table, canvas, scroll pane | Autoscroll |
| Unknown, stale, helper disconnected | Native middle click |

Native targets take precedence over scrollable ancestors. For example, text
inside a link remains native even though the enclosing web document scrolls.

The helper sends context before a click happens. It never performs AT-SPI,
D-Bus, subprocess, image-recognition, or network work in the click handler.

## Desktop support

ContextScroll uses the desktop accessibility bus for semantic recognition,
the included GNOME Shell bridge for Wayland coordinates, and Xlib coordinates
in X11 sessions. It requires:

- an AT-SPI 2 accessibility bus;
- applications that expose meaningful accessibility objects;
- GNOME Shell 48 or newer on Wayland, or an X11 display with `libX11`.

GTK, Qt, Firefox, Chromium/Electron, and most standard desktop applications
can expose AT-SPI roles. An application may expose an incomplete tree,
especially if accessibility was explicitly disabled. That context is treated
as unknown and therefore remains a native middle click.

While the helper runs, it enables the session's general AT-SPI status so
applications such as Firefox publish their semantic trees. It does not enable
the screen-reader status and restores the original setting when it exits.

This project intentionally does not inspect screenshots. Semantic roles are
faster, private, and able to distinguish a real tab from text that merely
looks like one.

## Requirements

Runtime:

- Linux with evdev and uinput;
- systemd;
- Python 3;
- PyGObject with the AT-SPI 2 introspection bindings.
- `libX11` (normally already installed by XWayland).

Build:

- Rust 1.85 or newer;
- Cargo;
- internet access for the first Cargo build, or a populated Cargo cache.

Fedora:

```bash
sudo dnf install cargo rust python3-gobject at-spi2-core libX11
```

Debian/Ubuntu package names are typically:

```bash
sudo apt install cargo rustc python3-gi gir1.2-atspi-2.0 at-spi2-core libx11-6
```

## Install

First stop any other daemon that exclusively grabs the same mouse.

Then install ContextScroll:

```bash
./scripts/install.sh
```

The script builds a locked release binary before elevating, installs both
services and the GNOME pointer bridge, preserves an existing
`/etc/contextscroll.conf`, and starts the system and current-user services.

GNOME discovers a newly installed local extension at graphical-session
startup. Sign out and back in after the first installation, and after an
update that changes the GNOME extension.

To update the installed files without starting either component:

```bash
./scripts/install.sh --install-only
```

Verify the live setup:

```bash
./scripts/diagnose.sh
```

## Test

```bash
./scripts/test.sh
```

The suite tests the Rust routing state machine, speed curve, configuration and
bounded bidirectional protocol, plus the Python semantic classifier and
protocol encoder.
It does not need root or access to physical input devices.

## Use

The default interaction is toggle mode:

1. Point at the body of a web page or other scrollable content.
2. Middle-click once.
3. Move away from the origin to control speed and direction.
4. Click any mouse button to stop. The stopping click is consumed.

Point at a browser tab, link, button, menu, or editable field and middle-click
normally. Both the press and release are forwarded without waiting.

Hold mode is also available in `/etc/contextscroll.conf`:

```ini
MODE = hold
```

In hold mode the pointer remains anchored while the middle button is held.
Release the button to stop.

After changing configuration:

```bash
sudo systemctl restart contextscroll.service
```

## Configuration

The installed configuration is `/etc/contextscroll.conf`:

```ini
MODE = toggle
UNKNOWN_ACTION = native
DEADZONE_PX = 15
SPEED_MULTIPLIER = 0.0112
SPEED_EXPONENT = 2.2
MAXIMUM_PX_PER_SECOND = 30000
PIXELS_PER_NOTCH = 55
MAXIMUM_DRAG_PX = 1200
TICK_HZ = 120
NATURAL_SCROLLING = false
SOCKET_PATH = /run/contextscroll/context.sock
```

`UNKNOWN_ACTION = native` is the safe default. Changing it to `scroll` makes
unsupported applications autoscroll, but can intercept native middle-click
actions because no semantic evidence is available.

There is intentionally no click-delay or hold-duration setting.

## Inspect live context

Enable helper debug logging:

```bash
systemctl --user edit contextscroll-context.service
```

Add:

```ini
[Service]
ExecStart=
ExecStart=/usr/bin/contextscroll-context --debug
```

Then:

```bash
systemctl --user daemon-reload
systemctl --user restart contextscroll-context.service
journalctl --user -u contextscroll-context.service -f
```

For a one-time coordinate probe:

```bash
contextscroll-context --probe X Y
```

## Service control

```bash
sudo systemctl stop contextscroll.service
sudo systemctl start contextscroll.service
systemctl --user restart contextscroll-context.service
```

To remove ContextScroll:

```bash
./scripts/uninstall.sh
```

The uninstaller preserves `/etc/contextscroll.conf` and does not change any
other mouse service.

## Architecture and safety

- The Rust daemon runs as root only because `/dev/input/event*` and
  `/dev/uinput` normally require it.
- It accepts context only from a Unix-socket peer whose UID logind reports as
  an active or online desktop user.
- Protocol lines are capped at 2048 bytes before JSON parsing.
- Context expires after 750 ms. The helper sends a 200 ms heartbeat, so a
  crashed or frozen helper automatically returns the daemon to native clicks.
- The hot path uses no mutex and performs no allocation for its context
  decision.
- Physical events are drained in kernel-sized batches on blocking poll
  threads. Inactive mice wake only for input or a bounded shutdown check.
- Wayland coordinates are capped at 60 Hz, duplicate points are coalesced,
  and accessibility queries are capped at 30 Hz.
- Each virtual mouse copies the physical device's vendor/product identity and
  capabilities. Its name is prefixed with `ContextScroll virtual:` so the
  daemon can reliably ignore its own output device.
- Both systemd units drop all capabilities and apply syscall, filesystem,
  namespace, network, memory, and task limits.

See [SECURITY.md](SECURITY.md) for the trust model.

## License

MIT

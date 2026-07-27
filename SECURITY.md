# Security model

ContextScroll handles privileged input and should be reviewed accordingly.

## Trust boundaries

### Rust daemon

`contextscroll` runs as root and exclusively grabs relative mice. It can read
mouse buttons and movement, but it never opens keyboard devices. It creates a
uinput mirror for each selected mouse.

The daemon reads:

- `/etc/contextscroll.conf`;
- `/dev/input/event*`;
- `/dev/uinput`;
- `/run/systemd/users/<uid>` when authenticating a helper.

It creates `/run/contextscroll/context.sock`.

### Python helper

`contextscroll-context` runs unprivileged in the graphical session. It can
read the semantic accessibility tree that the user's applications publish.
On GNOME Wayland, the included Shell extension publishes rate-limited pointer
coordinates and under-pointer window geometry over the session bus. On X11,
the helper samples the pointer through Xlib. It does not access evdev, uinput,
screenshots, clipboard contents, network resources, or application files.
The Shell extension temporarily hides the native pointer and renders a
non-reactive autoscroll cursor when the daemon reports that autoscroll is
active. Cursor visibility uses GNOME's paired inhibit/uninhibit API, and the
replacement overlay cannot receive input. A paired seat focus inhibitor keeps
wheel events directed to the under-pointer application while the native
cursor is hidden.

Its service exposes `/tmp/.X11-unix` read-only inside otherwise-private
network and temporary-file namespaces.

The helper enables `org.a11y.Status.IsEnabled` while running so applications
such as Firefox publish AT-SPI objects. It does not enable
`ScreenReaderEnabled`; if it changed the general status, it restores it on a
clean exit unless a screen reader has since been enabled.

The helper sends only:

- decision: `native`, `scroll`, or `unknown`;
- accessible role;
- application and object names;
- pointer coordinates.

Names are used only for diagnostics and are not persisted.

The daemon sends one aggregate boolean (`active`) back to the authenticated
helper so it can show or hide the autoscroll cursor. It does not send input
events, device identities, or application data to the user session.

## Socket authentication

The socket is writable by desktop users because the system daemon and session
helper have different UIDs. The daemon retrieves `SO_PEERCRED` from every
connection and accepts reports only when logind lists that UID as `active` or
`online`.

Messages use bounded newline-delimited JSON:

- maximum line length: 2048 bytes;
- exact protocol version and message type;
- an enumerated decision value;
- a boolean active state in daemon-to-helper messages;
- malformed or oversized input disconnects the client.

## Failure behavior

Context is advisory and expires after 750 ms. If the helper is missing,
blocked, crashed, stale, or unable to recognize an application, the default
action is `native`. Recognition failure therefore disables autoscroll rather
than intercepting a middle click.

The input daemon never queries the helper synchronously. A malicious or hung
application accessibility implementation cannot delay a physical click.

## Deliberate systemd exceptions

The system service cannot use `PrivateDevices=yes` or a closed
`DevicePolicy`, because it must open real and hot-plugged evdev nodes and
`/dev/uinput`.

The daemon uses normal time-sharing scheduling with a modest negative nice
value. It deliberately does not use real-time FIFO scheduling, so a faulty
event loop cannot starve the compositor. The process itself has an empty
capability set.

## Reporting

Please avoid public issue details for a vulnerability that could expose input
or enable local privilege escalation. Contact the repository owner privately
first.

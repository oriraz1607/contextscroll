# Security model

ContextScroll handles sensitive input and should be reviewed accordingly.

## Trust boundaries

### Rust daemon

`contextscroll` runs as the dedicated, non-login `contextscroll` system
account and exclusively grabs relative mice. A udev rule grants this account a
POSIX ACL only on physical event nodes classified as mice and on
`/dev/uinput`. It can read mouse buttons and movement, but it never opens
keyboard-only devices. It creates a uinput mirror for each selected mouse.

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
replacement overlay cannot receive input. The overlay follows bounded
daemon-provided relative offsets; when autoscroll ends, the extension warps
the hidden pointer to the final visual position before revealing it. A paired
seat focus inhibitor keeps wheel events directed to the under-pointer
application while the native cursor is hidden.

Its service exposes `/tmp/.X11-unix` read-only inside otherwise-private
network and temporary-file namespaces.

The helper enables `org.a11y.Status.IsEnabled` while running so applications
such as Firefox publish AT-SPI objects. It does not enable
`ScreenReaderEnabled`; if it changed the general status, it restores it on a
clean exit unless a screen reader has since been enabled.

AT-SPI traversal and Shell-facing D-Bus calls are dispatched on the GLib main
thread. The separate classifier worker schedules those ahead-of-click lookups
but never calls libatspi or PyGObject concurrently.

The helper sends only:

- decision: `native`, `scroll`, or `unknown`;
- accessible role;
- application and object names;
- pointer coordinates;
- the monotonic ID of a refresh request being acknowledged;
- the cursor context generation that produced the decision.

Names are used only for diagnostics and are not persisted.

The daemon sends one aggregate boolean (`active`) and bounded relative cursor
offsets back to the authenticated helper so it can render the autoscroll
cursor. It can also send a monotonic refresh request ID after motion
invalidates cached context. It does not send device identities or application
data to the user session.

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
- a bounded monotonic refresh request ID;
- a bounded context generation used to reject pre-warp decisions;
- malformed or oversized input disconnects the client.

## Failure behavior

Context is advisory and expires after 750 ms. If the helper is missing,
blocked, crashed, stale, or unable to recognize an application, the default
action is `native`. Recognition failure therefore disables autoscroll rather
than intercepting a middle click.

Fresh context is consumed without waiting. After raw pointer motion invalidates
the cache, a middle-button press can request one acknowledged refresh and wait
for at most 60 ms. If the helper or an application accessibility
implementation does not answer in that bound, routing falls back to the
configured safe action (`native` by default).

## Deliberate systemd exceptions

The system service cannot use `PrivateDevices=yes` or a closed `DevicePolicy`,
because it must open real and hot-plugged evdev nodes and `/dev/uinput`.
Instead, Unix ACLs restrict the unprivileged service account to udev-classified
mouse nodes and uinput. It is not a member of the broad `input` group.

The daemon uses normal time-sharing scheduling with a modest negative nice
value. It deliberately does not use real-time FIFO scheduling, so a faulty
event loop cannot starve the compositor. The process is not root and has an
empty capability set.

## Reporting

Please avoid public issue details for a vulnerability that could expose input
or enable local privilege escalation. Contact the repository owner privately
first.

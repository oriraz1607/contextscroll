# ContextScroll security policy and threat model

ContextScroll handles system input and desktop accessibility data. Treat it as
security-sensitive software. The controls below reduce risk; they are not a
claim that the project has received an independent security audit.

## Supported versions

| Version | Security fixes |
| --- | --- |
| Latest published release | Yes |
| Older releases and source snapshots | No |

Upgrade to the latest release before reporting a problem that may already have
been fixed.

## Private vulnerability reporting

Do not open a public issue for a vulnerability that could expose input,
accessibility data, or local privileges. Use GitHub's private
**Report a vulnerability** form:

<https://github.com/oriraz1607/contextscroll/security/advisories/new>

Include the affected version, distribution and desktop version, reproduction
steps, impact, and any suggested mitigation. Reports are handled on a
best-effort basis; the project does not promise a fixed response SLA.

## Security goals

ContextScroll is designed to:

- read only physical, relative devices that udev classifies as mice;
- emit mouse buttons, relative motion, and wheel events through a virtual
  mouse;
- transparently preserve non-mouse key events from hybrid mouse nodes through
  a separate, exact-capability virtual passthrough;
- accept context from one foreground local graphical session;
- become transparent mouse pass-through when that session is missing,
  ambiguous, stale, or disconnected;
- run the system component without root or Linux capabilities;
- prevent the session helper from writing application files or using IP
  networking;
- make release artifacts traceable to a repository commit and workflow.

## Trust boundaries and sensitive capabilities

### System daemon

`contextscroll` runs as the dedicated, non-login `contextscroll` account. A
udev rule grants that UID access to `/dev/uinput` and to event nodes that udev
classifies as mice. Pure keyboard nodes do not match. The daemon independently
requires middle-button, relative X/Y capabilities and rejects touch/absolute
pointer nodes.

Some physical mice, notably Logitech HID++ devices, advertise keyboard
capabilities on the same event node as their mouse motion. An exclusive evdev
grab applies to the whole node, so dropping those events could break
programmed buttons or leave keys stuck. For such a hybrid node, ContextScroll
keeps keyboard capabilities off the virtual mouse and creates a separate
virtual device containing exactly the physical node's advertised non-mouse
key capabilities. Those key events bypass autoscroll logic and are forwarded
unchanged. This expands the daemon's observable input only to keyboard events
originating from a udev-classified mouse, not from pure keyboard devices.

The daemon reads mouse movement and buttons. It does not intentionally record
or persist them.

Access to `/dev/uinput` is nevertheless a high-power capability: a compromised
daemon could bypass the program's Rust-level restrictions and attempt to create
arbitrary virtual input devices, including a keyboard. systemd device policy,
ACLs, an empty capability set, filesystem protection, and network isolation
limit the surrounding account, but they cannot restrict individual uinput
ioctls. Eliminating this residual risk requires a substantially different input
architecture.

The daemon reads `/etc/contextscroll.conf`, the selected input nodes, uinput,
the system D-Bus socket for logind, and its runtime directory. It creates
`/run/contextscroll/context.sock`.

### Session helper

`contextscroll-context` runs as the desktop user and traverses the AT-SPI
accessibility tree. AT-SPI can expose semantic roles, names, states, actions,
and text that applications publish for visible user interfaces. This is
sensitive desktop data even though it is not raw evdev input.

The helper intentionally sends only a bounded classification, role,
application/object names, pointer coordinates, protocol counters, and pause
state to the daemon. Names are used for diagnostics and are not persisted by
ContextScroll.

The user service hides the home directory and exposes only dconf, legacy
AT-SPI discovery, and Xauthority paths read-only. Its only declared writable
path is `%t/dconf`. It has a private IP network namespace. These restrictions
do not make the session bus or AT-SPI data untrusted, and a compromised process
with the user's UID remains inside the user's desktop security domain.

### GNOME Shell extension

GNOME Shell loads the extension into the compositor process. Shell extensions
are not sandboxed plugins: a compromised extension has the authority available
to GNOME Shell and can affect the entire graphical session. The extension
publishes pointer/window geometry over the user's session bus, renders a
non-reactive cursor, and temporarily inhibits cursor visibility and focus while
autoscroll is active.

The distributed extension is installed once, system-wide and root-owned.
Users can still override extensions in their own home directory; same-UID
modification is outside the package's isolation boundary.

## Socket authorization

The socket is mode `0666` because the daemon and graphical session have
different UIDs. Filesystem writability is not authorization.

For every connection the daemon obtains the peer PID and UID with
`SO_PEERCRED`, enumerates logind sessions, and accepts it only when:

- exactly one supported graphical session is active on the machine;
- that session is local, non-remote, class `user`, and has a seat;
- its type is `wayland` or `x11`; and
- its UID matches the socket peer.

systemd user services normally belong to the per-user service manager rather
than a `session-N.scope`, so their PID cannot be reliably mapped back with
logind's `GetSessionByPID`. UID matching is intentional: processes with the
same desktop UID are already one trust domain.

Only the newest authorized connection can update daemon state. Authorization
is revalidated while connected. Supersession, an expired heartbeat,
disconnection, session switching, or ambiguous multi-seat state clears cached
context, ends active autoscroll, and enables transparent pass-through.

Authentication attempts and protocol lines are bounded. Lines are newline
delimited JSON with a maximum of 2,048 bytes, exact version/type fields,
enumerated decisions and cursor directions, booleans for control state, and
bounded counters and coordinates.

Processes running as the same desktop UID are treated as one trust domain. A
malicious same-UID process may impersonate the helper; ContextScroll does not
claim to isolate mutually hostile processes belonging to one user.

## Failure behavior

Context expires after 750 ms, while the normal helper heartbeat is 200 ms.
Unknown, stale, unauthenticated, or missing context uses the configured safe
action, `native` by default. Pointer motion invalidates cached semantic
context. A stale click may request one refresh for at most 60 ms, then falls
back safely.

Losing the authorized helper is stronger than ordinary context expiry: the
daemon stops autoscroll and mirrors supported mouse events without
classification until a helper is authenticated again.

## Installation and supply chain

Supported release bundles contain a SHA-256 file manifest and SPDX SBOM. GitHub
Actions builds the locked dependency graph, compares two builds, creates
GitHub/Sigstore provenance and SBOM attestations, and publishes complete draft
releases before immutability is applied.

Users must verify the downloaded archive with `gh attestation verify` before
extracting or executing it. The installer then verifies the bundle manifest,
copies a fixed file allowlist into a private root-owned staging directory,
verifies that snapshot again, and installs from it. The privileged phase does
not run Cargo, access the network, or execute from the user-writable checkout.

Attestation proves which repository workflow and commit produced an artifact.
It does not prove the source is vulnerability-free.

## Threat model limits

The following are not defended against:

- a malicious or already-compromised root account;
- a compromised kernel, systemd, logind, udev, GNOME Shell, or package manager;
- a malicious process running as the same desktop UID;
- physical attacks, hostile USB firmware, or kernel input-driver flaws;
- denial of service by an administrator changing device permissions or
  stopping required services;
- unsupported multi-seat or remote graphical configurations.

Dependency and static-analysis checks reduce known supply-chain risk but cannot
detect every malicious or unknown dependency behavior.

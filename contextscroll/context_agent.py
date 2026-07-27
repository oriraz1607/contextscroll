"""User-session AT-SPI context recognizer.

Recognition is deliberately performed ahead of input. Mouse events only update
the point to inspect; a worker resolves and classifies the accessibility tree,
then refreshes the daemon's cache. The root input daemon never waits for this
process while handling a button event.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import threading
import time
import warnings
from dataclasses import dataclass, field

from .classifier import Decision, SemanticNode, classify_chain
from .pointer import GnomeShellPointer, PointerUnavailable, X11Pointer
from .protocol import ContextReport, encode

log = logging.getLogger("contextscroll.context")

DEFAULT_SOCKET = "/run/contextscroll/context.sock"
HEARTBEAT_SECONDS = 0.20
MIN_QUERY_INTERVAL = 1.0 / 30.0
POINTER_SAMPLE_MILLISECONDS = 8
MAX_ANCESTORS = 32
MAX_DESCENT = 24


def _load_atspi():
    import gi

    gi.require_version("Atspi", "2.0")
    gi.require_version("Gio", "2.0")
    gi.require_version("GLib", "2.0")
    from gi.repository import Atspi, Gio, GLib

    return Atspi, GLib, Gio


class AccessibilityStatus:
    """Keep toolkit accessibility enabled while the helper is running."""

    BUS_NAME = "org.a11y.Bus"
    OBJECT_PATH = "/org/a11y/bus"
    STATUS_INTERFACE = "org.a11y.Status"
    PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"

    def __init__(self, gio, glib):
        self.gio = gio
        self.glib = glib
        self.connection = gio.bus_get_sync(gio.BusType.SESSION, None)
        self.changed = not self._get("IsEnabled")
        if self.changed:
            self._set("IsEnabled", True)
            log.info("enabled session accessibility for semantic recognition")

    def _get(self, name: str) -> bool:
        result = self.connection.call_sync(
            self.BUS_NAME,
            self.OBJECT_PATH,
            self.PROPERTIES_INTERFACE,
            "Get",
            self.glib.Variant(
                "(ss)", (self.STATUS_INTERFACE, name)
            ),
            self.glib.VariantType("(v)"),
            self.gio.DBusCallFlags.NONE,
            1_000,
            None,
        )
        return bool(result.unpack()[0])

    def _set(self, name: str, value: bool) -> None:
        self.connection.call_sync(
            self.BUS_NAME,
            self.OBJECT_PATH,
            self.PROPERTIES_INTERFACE,
            "Set",
            self.glib.Variant(
                "(ssv)",
                (
                    self.STATUS_INTERFACE,
                    name,
                    self.glib.Variant("b", value),
                ),
            ),
            None,
            self.gio.DBusCallFlags.NONE,
            1_000,
            None,
        )

    def close(self) -> None:
        if not self.changed:
            return
        try:
            if not self._get("ScreenReaderEnabled"):
                self._set("IsEnabled", False)
                log.info("restored session accessibility status")
        except Exception as error:
            log.warning("could not restore accessibility status: %s", error)
        self.changed = False


@dataclass(slots=True)
class LatestPoint:
    x: int | None = None
    y: int | None = None
    window: tuple[int, int, int, int, int, str] | None = None
    generation: int = 0
    condition: threading.Condition = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.condition = threading.Condition()

    def update(
        self,
        x: int,
        y: int,
        window_x: int = 0,
        window_y: int = 0,
        window_width: int = 0,
        window_height: int = 0,
        window_pid: int = 0,
        window_title: str = "",
    ) -> None:
        if not (-1_000_000 <= x <= 1_000_000):
            return
        if not (-1_000_000 <= y <= 1_000_000):
            return
        with self.condition:
            window = (
                window_x,
                window_y,
                window_width,
                window_height,
                window_pid,
                window_title,
            )
            if self.x == x and self.y == y and self.window == window:
                return
            self.x = x
            self.y = y
            self.window = window
            self.generation += 1
            self.condition.notify()

    def wait(
        self, previous_generation: int, timeout: float
    ):
        with self.condition:
            if self.generation == previous_generation:
                self.condition.wait(timeout)
            return self.x, self.y, self.generation, self.window


class AccessibilityTree:
    def __init__(self, atspi):
        self.atspi = atspi
        self.last_top_level = None
        self.last_descent = []

    def _states(self, accessible) -> set[str]:
        result: set[str] = set()
        try:
            state_set = accessible.get_state_set()
            for name in (
                "ACTIVE",
                "EDITABLE",
                "ENABLED",
                "FOCUSABLE",
                "SELECTABLE",
                "SENSITIVE",
                "SHOWING",
                "VISIBLE",
            ):
                if state_set.contains(getattr(self.atspi.StateType, name)):
                    result.add(name)
        except Exception:
            pass
        return result

    @staticmethod
    def _actions(accessible) -> set[str]:
        result: set[str] = set()
        try:
            action = accessible.get_action_iface()
            if action is None:
                return result
            for index in range(min(action.get_n_actions(), 32)):
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    name = action.get_action_name(index)
                if name:
                    result.add(name)
        except Exception:
            pass
        return result

    def node(self, accessible) -> SemanticNode:
        try:
            role = accessible.get_role_name() or "unknown"
        except Exception:
            role = "unknown"
        try:
            attributes = accessible.get_attributes() or {}
        except Exception:
            attributes = {}
        try:
            name = accessible.get_name() or ""
        except Exception:
            name = ""
        return SemanticNode.create(
            role,
            states=self._states(accessible),
            actions=self._actions(accessible),
            attributes=attributes,
            name=name,
        )

    def _children(self, accessible, maximum: int = 512):
        try:
            count = min(accessible.get_child_count(), maximum)
        except Exception:
            return
        for index in range(count):
            try:
                child = accessible.get_child_at_index(index)
            except Exception:
                continue
            if child is not None:
                yield child

    def _contains(self, accessible, x: int, y: int) -> bool:
        try:
            if not accessible.is_component():
                return False
            return bool(
                accessible.get_component_iface().contains(
                    x, y, self.atspi.CoordType.SCREEN
                )
            )
        except Exception:
            return False

    def _active(self, accessible) -> bool:
        try:
            return accessible.get_state_set().contains(
                self.atspi.StateType.ACTIVE
            )
        except Exception:
            return False

    def _showing(self, accessible) -> bool:
        try:
            return accessible.get_state_set().contains(
                self.atspi.StateType.SHOWING
            )
        except Exception:
            return False

    def _top_level_at(self, desktop, x: int, y: int):
        if (
            self.last_top_level is not None
            and self._active(self.last_top_level)
            and self._contains(self.last_top_level, x, y)
        ):
            return self.last_top_level
        candidates = []
        for application in self._children(desktop, maximum=256):
            for window in self._children(application, maximum=256):
                if self._contains(window, x, y):
                    candidates.append(window)
        if not candidates:
            return None
        # The active window is the best portable stacking hint. AT-SPI's MDI
        # z-order is used as a secondary hint where a toolkit provides it.
        def score(accessible):
            z_order = 0
            try:
                z_order = accessible.get_component_iface().get_mdi_z_order()
            except Exception:
                pass
            return self._active(accessible), z_order

        self.last_top_level = max(candidates, key=score)
        return self.last_top_level

    def _top_level_for_window(self, desktop, window):
        if window is None:
            return None
        _, _, width, height, pid, title = window
        if width <= 0 or height <= 0:
            return None
        candidates = []
        for application in self._children(desktop, maximum=256):
            process_id = 0
            try:
                process_id = application.get_process_id()
            except Exception:
                pass
            for candidate in self._children(application, maximum=256):
                candidate_title = ""
                try:
                    candidate_title = candidate.get_name() or ""
                except Exception:
                    pass
                title_match = (
                    title
                    and candidate_title
                    and (
                        title in candidate_title
                        or candidate_title in title
                    )
                )
                pid_match = pid > 0 and process_id == pid
                if pid_match or title_match:
                    candidates.append(candidate)
        if not candidates:
            return None
        active = [candidate for candidate in candidates if self._active(candidate)]
        self.last_top_level = (active or candidates)[0]
        return self.last_top_level

    def _map_point(self, top_level, x: int, y: int, window):
        if window is None:
            return x, y
        source_x, source_y, source_width, source_height, pid, _ = window
        if source_width <= 0 or source_height <= 0:
            return x, y
        if pid == -1:
            # Compatibility with the v1 bridge: AT-SPI coordinates on this
            # GNOME setup are monitor-local while Shell coordinates are global.
            return x - source_x, y - source_y
        try:
            target = top_level.get_component_iface().get_extents(
                self.atspi.CoordType.SCREEN
            )
        except Exception:
            return x, y
        if target.width <= 0 or target.height <= 0:
            return x, y
        mapped_x = target.x + round(
            (x - source_x) * target.width / source_width
        )
        mapped_y = target.y + round(
            (y - source_y) * target.height / source_height
        )
        return mapped_x, mapped_y

    def _child_at(self, accessible, x: int, y: int):
        try:
            component = accessible.get_component_iface()
            child = component.get_accessible_at_point(
                x, y, self.atspi.CoordType.SCREEN
            )
            if child is not None and child != accessible:
                return child
        except Exception:
            pass

        # Firefox and some other toolkits return the top-level object from
        # GetAccessibleAtPoint. Fall back to a bounded direct-child walk and
        # prefer the SHOWING child when multiple tab documents overlap.
        candidates = [
            child
            for child in self._children(accessible)
            if self._contains(child, x, y)
        ]
        if not candidates:
            return None

        def score(candidate):
            z_order = 0
            try:
                z_order = (
                    candidate.get_component_iface().get_mdi_z_order()
                )
            except Exception:
                pass
            return self._showing(candidate), self._active(candidate), z_order

        return max(candidates, key=score)

    def _deepest(self, accessible, x: int, y: int):
        current = accessible
        path = [current]
        if self.last_descent and self.last_descent[0] == accessible:
            for index in range(len(self.last_descent) - 1, 0, -1):
                candidate = self.last_descent[index]
                if self._showing(candidate) and self._contains(
                    candidate, x, y
                ):
                    current = candidate
                    path = self.last_descent[: index + 1]
                    break
        for _ in range(MAX_DESCENT):
            child = self._child_at(current, x, y)
            if child is None:
                break
            current = child
            path.append(current)
        self.last_descent = path
        return current

    def chain_at(self, x: int, y: int, window=None):
        desktop = self.atspi.get_desktop(0)
        if desktop is None:
            return [], ""
        legacy_monitor = window is not None and window[4] == -1
        if legacy_monitor:
            x, y = self._map_point(None, x, y, window)
            top_level = self._top_level_at(desktop, x, y)
        else:
            top_level = self._top_level_for_window(desktop, window)
        if top_level is not None:
            if not legacy_monitor:
                x, y = self._map_point(top_level, x, y, window)
        elif not legacy_monitor:
            top_level = self._top_level_at(desktop, x, y)
        if top_level is None:
            return [], ""
        deepest = self._deepest(top_level, x, y)

        accessibles = []
        current = deepest
        for _ in range(MAX_ANCESTORS):
            if current is None:
                break
            accessibles.append(current)
            if current == top_level:
                break
            try:
                parent = current.get_parent()
            except Exception:
                break
            if parent is None or parent == current:
                break
            current = parent

        application_name = ""
        try:
            app = deepest.get_application()
            application_name = app.get_name() or ""
        except Exception:
            pass
        return [self.node(item) for item in accessibles], application_name

    def report_at(self, x: int, y: int, window=None) -> ContextReport:
        try:
            chain, application = self.chain_at(x, y, window)
        except Exception as error:
            log.debug("AT-SPI lookup failed at %d,%d: %r", x, y, error)
            return ContextReport(Decision.UNKNOWN, x=x, y=y)
        decision = classify_chain(chain, application)
        deepest = chain[0] if chain else None
        return ContextReport(
            decision=decision,
            role=deepest.role if deepest else "",
            application=application,
            name=deepest.name if deepest else "",
            x=x,
            y=y,
        )


class ContextWorker(threading.Thread):
    def __init__(
        self,
        tree: AccessibilityTree,
        points: LatestPoint,
        socket_path: str,
        stop: threading.Event,
    ):
        super().__init__(name="contextscroll-classifier", daemon=True)
        self.tree = tree
        self.points = points
        self.socket_path = socket_path
        self.stop_event = stop
        self.last_report = ContextReport(Decision.UNKNOWN)
        self.connection: socket.socket | None = None

    def _connect(self) -> bool:
        if self.connection is not None:
            return True
        candidate = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        candidate.settimeout(1.0)
        try:
            candidate.connect(self.socket_path)
        except OSError:
            candidate.close()
            return False
        candidate.settimeout(None)
        self.connection = candidate
        log.info("connected to %s", self.socket_path)
        return True

    def _send(self) -> None:
        if not self._connect():
            return
        try:
            self.connection.sendall(encode(self.last_report))
        except OSError:
            try:
                self.connection.close()
            except OSError:
                pass
            self.connection = None

    def run(self) -> None:
        generation = -1
        last_query = 0.0
        next_send = 0.0
        while not self.stop_event.is_set():
            x, y, current_generation, window = self.points.wait(
                generation, HEARTBEAT_SECONDS
            )
            now = time.monotonic()
            changed = current_generation != generation
            if changed and (x is None or y is None):
                generation = current_generation
                changed = False
            if changed and x is not None and y is not None:
                remaining = MIN_QUERY_INTERVAL - (now - last_query)
                if remaining > 0 and self.stop_event.wait(remaining):
                    break
                # Movement may have continued while throttling. Classify the
                # newest coordinate, never the stale sample that woke us.
                x, y, current_generation, window = self.points.wait(
                    current_generation, 0
                )
                self.last_report = self.tree.report_at(x, y, window)
                last_query = time.monotonic()
                generation = current_generation
                log.debug(
                    "context %s role=%r app=%r at %d,%d",
                    self.last_report.decision,
                    self.last_report.role,
                    self.last_report.application,
                    x,
                    y,
                )
            if changed or time.monotonic() >= next_send:
                self._send()
                next_send = time.monotonic() + HEARTBEAT_SECONDS
        if self.connection is not None:
            try:
                self.connection.close()
            except OSError:
                pass


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Continuously classify the UI beneath the pointer."
    )
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--check-pointer",
        action="store_true",
        help="verify X11/XWayland coordinates and exit",
    )
    parser.add_argument(
        "--probe",
        nargs=2,
        type=int,
        metavar=("X", "Y"),
        help="classify one screen coordinate and exit",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s: %(message)s",
    )
    if args.check_pointer:
        with X11Pointer() as pointer:
            position = pointer.position()
            if position is None:
                raise RuntimeError("X11/XWayland did not return a pointer")
            print(f"{position[0]}\t{position[1]}")
        return 0
    atspi, glib, gio = _load_atspi()
    accessibility_status = AccessibilityStatus(gio, glib)
    if accessibility_status.changed:
        # Firefox responds dynamically to the IsEnabled property and needs a
        # brief moment to register its root object on the accessibility bus.
        time.sleep(0.25)
    # Accessibility calls happen in a background worker. A short D-Bus timeout
    # prevents a hung application from stalling future cache refreshes.
    atspi.set_timeout(200, 1_000)
    atspi.init()
    tree = AccessibilityTree(atspi)
    if args.probe:
        report = tree.report_at(*args.probe)
        print(
            f"{report.decision.value}\t{report.role}\t"
            f"{report.application}\t{report.name}"
        )
        atspi.exit()
        accessibility_status.close()
        return 0

    points = LatestPoint()
    stop = threading.Event()
    worker = ContextWorker(tree, points, args.socket, stop)
    worker.start()
    loop = glib.MainLoop()
    session_type = os.environ.get("XDG_SESSION_TYPE", "").lower()
    pointer = None
    pointer_source = None
    listener = None

    if session_type == "wayland":
        try:
            pointer = GnomeShellPointer(gio)
        except PointerUnavailable as error:
            stop.set()
            worker.join(timeout=2.0)
            atspi.exit()
            accessibility_status.close()
            raise RuntimeError(
                "the ContextScroll GNOME extension is required on Wayland"
            ) from error
        pointer.connect(points.update)
        points.update(*pointer.position())
        coordinate_source = "GNOME Shell coordinates"
    else:
        try:
            pointer = X11Pointer()
        except PointerUnavailable as error:
            stop.set()
            worker.join(timeout=2.0)
            atspi.exit()
            accessibility_status.close()
            raise RuntimeError(
                "a pointer source is required: " + str(error)
            ) from error

        def sample_pointer():
            position = pointer.position()
            if position is not None:
                points.update(*position)
            return not stop.is_set()

        pointer_source = glib.timeout_add(
            POINTER_SAMPLE_MILLISECONDS, sample_pointer
        )
        sample_pointer()
        coordinate_source = "X11 coordinates"

    def quit_loop(_signum, _frame):
        stop.set()
        loop.quit()

    signal.signal(signal.SIGINT, quit_loop)
    signal.signal(signal.SIGTERM, quit_loop)
    log.info(
        "sampling semantic pointer context (%s, %s, %s)",
        session_type or "unknown session",
        os.environ.get("XDG_CURRENT_DESKTOP", "unknown desktop"),
        coordinate_source,
    )
    try:
        loop.run()
    finally:
        stop.set()
        if listener is not None:
            listener.deregister("mouse:abs")
        if pointer_source is not None:
            glib.source_remove(pointer_source)
        worker.join(timeout=2.0)
        if pointer is not None:
            pointer.close()
        atspi.exit()
        accessibility_status.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

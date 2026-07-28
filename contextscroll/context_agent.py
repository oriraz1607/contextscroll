"""User-session AT-SPI context recognizer.

Recognition is deliberately performed ahead of input. Mouse events only update
the point to inspect; a worker resolves and classifies the accessibility tree,
then refreshes the daemon's cache. If pointer motion outruns that cache, the
input daemon can request one acknowledged last-moment refresh before routing
the middle-button press.
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
from dataclasses import dataclass, field, replace

from .classifier import (
    Decision,
    SemanticNode,
    classify_chain,
    is_browser_application,
    normalize,
)
from .pointer import GnomeShellPointer, PointerUnavailable, X11Pointer
from .protocol import (
    MAX_LINE_BYTES,
    ActivityReport,
    ContextReport,
    CursorReport,
    RefreshReport,
    decode_daemon,
    encode,
)

log = logging.getLogger("contextscroll.context")

DEFAULT_SOCKET = "/run/contextscroll/context.sock"
HEARTBEAT_SECONDS = 0.20
STATE_POLL_SECONDS = 0.01
MIN_QUERY_INTERVAL = 1.0 / 30.0
UNKNOWN_RETRY_INITIAL_SECONDS = 0.075
UNKNOWN_RETRY_MAX_SECONDS = 1.0
POINTER_SAMPLE_MILLISECONDS = 8
MAX_ANCESTORS = 32
MAX_DESCENT = 24
MAX_LINK_CONTAINER_ANCESTORS = 8
MAX_LINK_DESCENDANTS = 64
MAX_LINK_DESCENT = 6
MAX_LINK_CONTAINER_AREA_RATIO = 0.35
POINT_LINK_ROOT_ANCESTOR = 4
MAX_POINT_LINK_DESCENDANTS = 128
MAX_POINT_LINK_DESCENT = 8


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

    def refresh(self) -> None:
        with self.condition:
            if self.x is None or self.y is None:
                return
            self.generation += 1
            self.condition.notify()


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

    @staticmethod
    def _is_hyperlink(accessible) -> bool:
        try:
            role = normalize(accessible.get_role_name() or "")
        except Exception:
            role = ""
        if role == "link":
            return True
        try:
            attributes = {
                normalize(str(key)): normalize(str(value))
                for key, value in (accessible.get_attributes() or {}).items()
            }
        except Exception:
            return False
        if attributes.get("tag") == "a":
            return True
        return "link" in attributes.get("xml roles", "").split()

    def _has_hyperlink_descendant(self, accessible) -> bool:
        queue = [
            (child, 1)
            for child in self._children(
                accessible, maximum=MAX_LINK_DESCENDANTS
            )
        ]
        visited = 0
        while queue and visited < MAX_LINK_DESCENDANTS:
            candidate, depth = queue.pop(0)
            visited += 1
            if self._is_hyperlink(candidate):
                return True
            if depth < MAX_LINK_DESCENT:
                remaining = MAX_LINK_DESCENDANTS - visited - len(queue)
                if remaining > 0:
                    queue.extend(
                        (child, depth + 1)
                        for child in self._children(
                            candidate, maximum=remaining
                        )
                    )
        return False

    def _has_hyperlink_at_point(
        self, accessible, x: int, y: int
    ) -> bool:
        queue = [(accessible, 0)]
        visited = 0
        while queue and visited < MAX_POINT_LINK_DESCENDANTS:
            candidate, depth = queue.pop(0)
            visited += 1
            try:
                role = normalize(candidate.get_role_name() or "")
            except Exception:
                role = ""
            if role == "link" and self._contains(candidate, x, y):
                return True
            if depth < MAX_POINT_LINK_DESCENT:
                remaining = (
                    MAX_POINT_LINK_DESCENDANTS
                    - visited
                    - len(queue)
                )
                if remaining > 0:
                    queue.extend(
                        (child, depth + 1)
                        for child in self._children(
                            candidate, maximum=remaining
                        )
                    )
        return False

    def _mark_hyperlink_at_point(
        self,
        accessibles,
        nodes: list[SemanticNode],
        application: str,
        x: int,
        y: int,
    ) -> None:
        if not is_browser_application(application) or not nodes:
            return
        if classify_chain(nodes, application) != Decision.SCROLL:
            return
        if not any(
            "clickancestor" in node.actions for node in nodes[:3]
        ):
            return
        root_index = min(
            POINT_LINK_ROOT_ANCESTOR, len(accessibles) - 1
        )
        if self._has_hyperlink_at_point(
            accessibles[root_index], x, y
        ):
            nodes[0] = replace(nodes[0], hyperlink_target=True)

    def _is_compact_link_container(
        self, accessible, top_level
    ) -> bool:
        try:
            container = accessible.get_component_iface().get_extents(
                self.atspi.CoordType.SCREEN
            )
            window = top_level.get_component_iface().get_extents(
                self.atspi.CoordType.SCREEN
            )
        except Exception:
            return False
        container_area = container.width * container.height
        window_area = window.width * window.height
        return (
            container_area > 0
            and window_area > 0
            and container_area
            <= window_area * MAX_LINK_CONTAINER_AREA_RATIO
        )

    def _mark_linked_click_target(
        self,
        accessibles,
        nodes: list[SemanticNode],
        application: str,
        top_level,
    ) -> None:
        if not is_browser_application(application):
            return
        for index, (accessible, node) in enumerate(
            zip(accessibles, nodes, strict=False)
        ):
            if index >= MAX_LINK_CONTAINER_ANCESTORS:
                break
            if "click" not in node.actions:
                continue
            if not self._is_compact_link_container(
                accessible, top_level
            ):
                continue
            if self._has_hyperlink_descendant(accessible):
                nodes[index] = replace(node, hyperlink_target=True)
                return

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

    @staticmethod
    def _application_matches_window(
        application_name: str, window_title: str
    ) -> bool:
        """Recognize an application's own windows without trusting its PID.

        Chromium-family accessibility processes commonly have a different
        PID from the compositor window, especially before that window has
        received focus. Product-name tokens provide a conservative fallback
        when the page title exposed by AT-SPI is also not current yet.
        """
        application = normalize(application_name)
        title = normalize(window_title)
        if not application or not title:
            return False
        ignored = {
            "application",
            "browser",
            "desktop",
            "gtk",
            "qt",
            "web",
        }
        return any(
            len(token) >= 4
            and token not in ignored
            and token in title.split()
            for token in application.split()
        )

    def _window_size_score(self, candidate, width: int, height: int):
        try:
            extents = candidate.get_component_iface().get_extents(
                self.atspi.CoordType.SCREEN
            )
        except Exception:
            return -1_000_000_000
        if extents.width <= 0 or extents.height <= 0:
            return -1_000_000_000
        # Coordinates can be monitor-local in AT-SPI and global in Shell, but
        # the dimensions still distinguish most same-application windows.
        return -(
            abs(extents.width - width)
            + abs(extents.height - height)
        )

    def _top_level_for_window(self, desktop, window):
        if window is None:
            return None
        _, _, width, height, pid, title = window
        if width <= 0 or height <= 0:
            return None
        candidates = []
        for application in self._children(desktop, maximum=256):
            process_id = 0
            application_name = ""
            try:
                process_id = application.get_process_id()
            except Exception:
                pass
            try:
                application_name = application.get_name() or ""
            except Exception:
                pass
            application_match = self._application_matches_window(
                application_name, title
            )
            for candidate in self._children(application, maximum=256):
                candidate_title = ""
                try:
                    candidate_title = candidate.get_name() or ""
                except Exception:
                    pass
                title_match = bool(
                    title
                    and candidate_title
                    and (
                        title in candidate_title
                        or candidate_title in title
                    )
                )
                pid_match = pid > 0 and process_id == pid
                if pid_match or title_match or application_match:
                    candidates.append(
                        (
                            candidate,
                            title_match,
                            pid_match,
                            application_match,
                            self._window_size_score(
                                candidate, width, height
                            ),
                        )
                    )
        if not candidates:
            return None
        # Exact title and PID matches outrank the product-name fallback.
        # Activity is deliberately last: an unfocused window under the pointer
        # must beat a focused window elsewhere.
        self.last_top_level = max(
            candidates,
            key=lambda item: (
                item[1],
                item[2],
                item[3],
                item[4],
                self._active(item[0]),
            ),
        )[0]
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
        nodes = [self.node(item) for item in accessibles]
        self._mark_hyperlink_at_point(
            accessibles,
            nodes,
            application_name,
            x,
            y,
        )
        self._mark_linked_click_target(
            accessibles, nodes, application_name, top_level
        )
        return nodes, application_name

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


class MainThreadAccessibility:
    """Run every libatspi traversal on the GLib main thread.

    Some libatspi/PyGObject combinations corrupt their cached objects when
    they are traversed concurrently with the GLib loop. The classifier worker
    waits for traversal here; the Rust path normally consumes the precomputed
    result and requests this work only when raw motion made it stale.
    """

    def __init__(self, tree, glib, stop: threading.Event):
        self.tree = tree
        self.glib = glib
        self.stop_event = stop

    def report_at(self, x: int, y: int, window=None) -> ContextReport:
        completed = threading.Event()
        result = [ContextReport(Decision.UNKNOWN, x=x, y=y)]

        def resolve():
            try:
                if not self.stop_event.is_set():
                    result[0] = self.tree.report_at(x, y, window)
            finally:
                completed.set()
            return self.glib.SOURCE_REMOVE

        self.glib.idle_add(
            resolve,
            priority=self.glib.PRIORITY_DEFAULT,
        )
        while not completed.wait(STATE_POLL_SECONDS):
            if self.stop_event.is_set():
                return ContextReport(Decision.UNKNOWN, x=x, y=y)
        return result[0]


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
        self.receive_buffer = bytearray()
        self.active = False
        self.active_callback = None
        self.refresh_callback = None
        self.cursor_callback = None
        self.pending_request_id = 0
        self.context_generation = 0

    def set_active_callback(self, callback) -> None:
        self.active_callback = callback

    def set_refresh_callback(self, callback) -> None:
        self.refresh_callback = callback

    def set_cursor_callback(self, callback) -> None:
        self.cursor_callback = callback

    def _set_active(self, active: bool, generation: int) -> None:
        if active == self.active and generation <= self.context_generation:
            return
        if self.active_callback is not None:
            try:
                self.active_callback(active, generation)
            except Exception as error:
                log.warning("could not update autoscroll indicator: %s", error)
                return
        self.active = active
        self.context_generation = max(
            self.context_generation,
            generation,
        )

    def _disconnect(self) -> None:
        if self.connection is not None:
            try:
                self.connection.close()
            except OSError:
                pass
        self.connection = None
        self.receive_buffer.clear()
        self._set_active(False, self.context_generation)

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
        self.receive_buffer.clear()
        log.info("connected to %s", self.socket_path)
        return True

    def _send(self) -> None:
        if not self._connect():
            return
        try:
            self.connection.sendall(encode(self.last_report))
        except OSError:
            self._disconnect()

    def _receive_activity(self) -> None:
        if self.connection is None:
            return
        while True:
            try:
                data = self.connection.recv(512, socket.MSG_DONTWAIT)
            except BlockingIOError:
                break
            except OSError:
                self._disconnect()
                break
            if not data:
                self._disconnect()
                break
            self.receive_buffer.extend(data)
            while b"\n" in self.receive_buffer:
                line, _, remainder = self.receive_buffer.partition(b"\n")
                self.receive_buffer = bytearray(remainder)
                if len(line) + 1 > MAX_LINE_BYTES:
                    self._disconnect()
                    return
                try:
                    report = decode_daemon(line + b"\n")
                except ValueError as error:
                    log.warning("invalid daemon message: %s", error)
                    self._disconnect()
                    return
                if isinstance(report, ActivityReport):
                    self._set_active(report.active, report.generation)
                elif isinstance(report, RefreshReport):
                    self.pending_request_id = max(
                        self.pending_request_id,
                        report.request_id,
                    )
                    if self.refresh_callback is None:
                        self.points.refresh()
                    else:
                        self.refresh_callback()
                elif isinstance(report, CursorReport):
                    if self.cursor_callback is not None:
                        try:
                            self.cursor_callback(report.x, report.y)
                        except Exception as error:
                            log.warning(
                                "could not update autoscroll cursor: %s",
                                error,
                            )
            if len(self.receive_buffer) > MAX_LINE_BYTES:
                self._disconnect()
                break

    def run(self) -> None:
        generation = -1
        last_query = 0.0
        next_send = 0.0
        next_unknown_retry = 0.0
        unknown_retry_interval = UNKNOWN_RETRY_INITIAL_SECONDS
        while not self.stop_event.is_set():
            x, y, current_generation, window = self.points.wait(
                generation, STATE_POLL_SECONDS
            )
            self._receive_activity()
            now = time.monotonic()
            changed = current_generation != generation
            if changed and (x is None or y is None):
                generation = current_generation
                changed = False
            retry_unknown = (
                not changed
                and x is not None
                and y is not None
                and self.last_report.decision == Decision.UNKNOWN
                and now >= next_unknown_retry
            )
            queried = False
            if (
                (changed or retry_unknown)
                and x is not None
                and y is not None
            ):
                remaining = MIN_QUERY_INTERVAL - (now - last_query)
                if remaining > 0 and self.stop_event.wait(remaining):
                    break
                # Movement may have continued while throttling. Classify the
                # newest coordinate, never the stale sample that woke us.
                x, y, current_generation, window = self.points.wait(
                    current_generation, 0
                )
                report = self.tree.report_at(x, y, window)
                last_query = time.monotonic()
                (
                    _latest_x,
                    _latest_y,
                    latest_generation,
                    _latest_window,
                ) = self.points.wait(current_generation, 0)
                if latest_generation != current_generation:
                    # The accessibility result belongs to the old pointer
                    # location. The daemon has already invalidated its cache
                    # from raw motion; immediately classify the newer point
                    # instead of publishing this stale decision.
                    generation = current_generation
                    log.debug(
                        "discarded stale context generation %d; latest is %d",
                        current_generation,
                        latest_generation,
                    )
                    continue
                self.last_report = replace(
                    report,
                    request_id=self.pending_request_id,
                    generation=self.context_generation,
                )
                generation = current_generation
                queried = True
                if self.last_report.decision == Decision.UNKNOWN:
                    if retry_unknown:
                        unknown_retry_interval = min(
                            unknown_retry_interval * 2,
                            UNKNOWN_RETRY_MAX_SECONDS,
                        )
                    else:
                        unknown_retry_interval = (
                            UNKNOWN_RETRY_INITIAL_SECONDS
                        )
                    next_unknown_retry = (
                        last_query + unknown_retry_interval
                    )
                else:
                    unknown_retry_interval = UNKNOWN_RETRY_INITIAL_SECONDS
                    next_unknown_retry = 0.0
                log.debug(
                    "context %s role=%r app=%r at %d,%d",
                    self.last_report.decision,
                    self.last_report.role,
                    self.last_report.application,
                    x,
                    y,
                )
            if queried or time.monotonic() >= next_send:
                self._send()
                self._receive_activity()
                next_send = time.monotonic() + HEARTBEAT_SECONDS
        self._disconnect()


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
    # A short D-Bus timeout prevents a hung application from stalling future
    # cache refreshes. Traversal itself stays on this GLib thread.
    atspi.set_timeout(200, 1_000)
    atspi.init()
    tree = AccessibilityTree(atspi)
    if args.probe:
        report = tree.report_at(*args.probe)
        print(
            f"{report.decision.value}\t{report.role}\t"
            f"{report.application}\t{report.name}"
        )
        accessibility_status.close()
        return 0

    points = LatestPoint()
    stop = threading.Event()
    worker = ContextWorker(
        MainThreadAccessibility(tree, glib, stop),
        points,
        args.socket,
        stop,
    )
    worker.start()
    loop = glib.MainLoop()
    session_type = os.environ.get("XDG_SESSION_TYPE", "").lower()
    pointer = None
    pointer_source = None
    listener = None

    if session_type == "wayland":
        try:
            pointer = GnomeShellPointer(gio, glib)
        except PointerUnavailable as error:
            stop.set()
            worker.join(timeout=2.0)
            accessibility_status.close()
            raise RuntimeError(
                "the ContextScroll GNOME extension is required on Wayland"
            ) from error
        pointer.connect(points.update)

        def update_indicator(active, _generation):
            completed = threading.Event()

            def apply_indicator():
                try:
                    if pointer is not None:
                        pointer.set_indicator(active)
                        if not active:
                            points.update(*pointer.refresh_position())
                            points.refresh()
                finally:
                    completed.set()
                return glib.SOURCE_REMOVE

            glib.idle_add(
                apply_indicator,
                priority=glib.PRIORITY_HIGH,
            )
            while not completed.wait(STATE_POLL_SECONDS):
                if stop.is_set():
                    return

        # The Shell-facing GDBusProxy remains on the GLib thread. The socket
        # worker waits for pointer restoration before accepting the new
        # context generation.
        worker.set_active_callback(update_indicator)

        def update_cursor(x, y):
            def apply_cursor():
                if pointer is not None:
                    pointer.set_indicator_offset(x, y)
                return glib.SOURCE_REMOVE

            glib.idle_add(
                apply_cursor,
                priority=glib.PRIORITY_HIGH,
            )

        worker.set_cursor_callback(update_cursor)

        def refresh_pointer_context():
            def apply_refresh():
                if pointer is not None:
                    points.update(*pointer.position())
                    points.refresh()
                return glib.SOURCE_REMOVE

            glib.idle_add(
                apply_refresh,
                priority=glib.PRIORITY_HIGH,
            )

        worker.set_refresh_callback(refresh_pointer_context)
        points.update(*pointer.position())
        coordinate_source = "GNOME Shell coordinates"
    else:
        try:
            pointer = X11Pointer()
        except PointerUnavailable as error:
            stop.set()
            worker.join(timeout=2.0)
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

        def refresh_pointer_context():
            def apply_refresh():
                sample_pointer()
                points.refresh()
                return glib.SOURCE_REMOVE

            glib.idle_add(
                apply_refresh,
                priority=glib.PRIORITY_HIGH,
            )

        worker.set_refresh_callback(refresh_pointer_context)
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
        # Process teardown releases libatspi. Explicit atspi.exit() has caused
        # shutdown-time crashes in current libatspi/Python combinations.
        accessibility_status.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

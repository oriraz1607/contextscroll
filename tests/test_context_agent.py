import unittest
import threading
import time
from types import SimpleNamespace
from unittest import mock

from contextscroll.classifier import Decision, SemanticNode, classify_chain
from contextscroll.context_agent import (
    AccessibilityTree,
    ContextWorker,
    LatestPoint,
    MainThreadAccessibility,
)
from contextscroll.protocol import ContextReport


class FakeStateSet:
    def __init__(self, active=False):
        self.active = active

    def contains(self, _state):
        return self.active


class FakeComponent:
    def __init__(self, extents):
        self.extents = extents

    def get_extents(self, _coordinate_type):
        return self.extents

    def contains(self, x, y, _coordinate_type):
        return (
            self.extents.x <= x < self.extents.x + self.extents.width
            and self.extents.y <= y < self.extents.y + self.extents.height
        )


class FakeAccessible:
    def __init__(
        self, *, name="", process_id=0, active=False, extents=None
    ):
        self.name = name
        self.process_id = process_id
        self.state_set = FakeStateSet(active)
        self.component = FakeComponent(extents) if extents else None

    def get_name(self):
        return self.name

    def get_process_id(self):
        return self.process_id

    def get_state_set(self):
        return self.state_set

    def get_component_iface(self):
        return self.component


class FakeSemanticAccessible:
    def __init__(
        self, role, *, attributes=None, children=None, extents=None
    ):
        self.role = role
        self.attributes = attributes or {}
        self.children = children or []
        self.component = FakeComponent(extents) if extents else None

    def get_role_name(self):
        return self.role

    def get_attributes(self):
        return self.attributes

    def get_child_count(self):
        return len(self.children)

    def get_child_at_index(self, index):
        return self.children[index]

    def get_component_iface(self):
        return self.component

    def is_component(self):
        return self.component is not None


class ContextTreeTests(unittest.TestCase):
    def test_wayland_desktop_never_falls_through_to_atspi_window(self):
        class NoAccessibilityLookup:
            @staticmethod
            def get_desktop(_index):
                raise AssertionError("desktop must not query AT-SPI windows")

        tree = AccessibilityTree(NoAccessibilityLookup())
        self.assertEqual(
            tree.chain_at(
                400,
                300,
                (0, 0, 0, 0, 0, ""),
            ),
            ([], ""),
        )
        report = tree.report_at(
            400,
            300,
            (0, 0, 0, 0, 0, ""),
        )
        self.assertEqual(report.decision, Decision.UNKNOWN)

    def test_x11_coordinate_without_window_still_uses_atspi(self):
        desktop = object()

        class Accessibility:
            @staticmethod
            def get_desktop(_index):
                return desktop

        tree = AccessibilityTree(Accessibility())
        top_level = object()
        deepest = SimpleNamespace(
            get_parent=lambda: top_level,
            get_application=lambda: None,
        )
        tree._top_level_at = lambda value, _x, _y: (
            top_level if value is desktop else None
        )
        tree._deepest = lambda value, _x, _y: (
            deepest if value is top_level else None
        )
        tree.node = lambda _accessible: SemanticNode.create("frame")
        tree._mark_hyperlink_at_point = lambda *_args: None
        # None is deliberately distinct from GNOME's zero-sized compositor
        # snapshot and preserves the coordinate-only X11 fallback.
        nodes, _application = tree.chain_at(400, 300, None)
        self.assertTrue(nodes)

    def test_page_to_tab_hit_test_restarts_at_window_root(self):
        tree = AccessibilityTree(atspi=None)
        window = object()
        stale_document = object()
        tab = object()
        tree.last_descent = [window, stale_document]
        children = {
            window: tab,
            tab: None,
            stale_document: None,
        }
        tree._child_at = lambda accessible, _x, _y: children[accessible]
        tree._showing = lambda _accessible: True
        tree._contains = lambda _accessible, _x, _y: True

        self.assertIs(tree._deepest(window, 400, 20), tab)

    def test_inactive_cached_window_is_replaced(self):
        tree = AccessibilityTree(atspi=None)
        desktop = object()
        application = object()
        stale_window = object()
        active_window = object()
        tree.last_top_level = stale_window

        def children(accessible, maximum=512):
            del maximum
            if accessible is desktop:
                return iter([application])
            if accessible is application:
                return iter([stale_window, active_window])
            return iter(())

        tree._children = children
        tree._contains = lambda accessible, x, y: accessible in {
            stale_window,
            active_window,
        }
        tree._active = lambda accessible: accessible is active_window

        self.assertIs(
            tree._top_level_at(desktop, 100, 100),
            active_window,
        )

    def test_window_title_fallback_handles_different_process_ids(self):
        atspi = SimpleNamespace(
            StateType=SimpleNamespace(ACTIVE=1),
            CoordType=SimpleNamespace(SCREEN=1),
        )
        tree = AccessibilityTree(atspi)
        desktop = object()
        application = FakeAccessible(process_id=99)
        firefox = FakeAccessible(
            name="Example — Mozilla Firefox", active=True
        )

        def children(accessible, maximum=512):
            del maximum
            if accessible is desktop:
                return iter([application])
            if accessible is application:
                return iter([firefox])
            return iter(())

        tree._children = children
        result = tree._top_level_for_window(
            desktop,
            (1920, 0, 1920, 1080, 1234, "Example — Mozilla Firefox"),
        )
        self.assertIs(result, firefox)

    def test_unfocused_browser_matches_application_product_name(self):
        atspi = SimpleNamespace(
            StateType=SimpleNamespace(ACTIVE=1),
            CoordType=SimpleNamespace(SCREEN=1),
        )
        tree = AccessibilityTree(atspi)
        desktop = object()
        application = FakeAccessible(
            name="Brave Browser", process_id=99
        )
        focused_elsewhere = FakeAccessible(
            name="Old page",
            active=True,
            extents=SimpleNamespace(
                x=0, y=0, width=1280, height=720
            ),
        )
        under_pointer = FakeAccessible(
            name="Accessibility title not refreshed",
            active=False,
            extents=SimpleNamespace(
                x=0, y=0, width=1920, height=1080
            ),
        )

        def children(accessible, maximum=512):
            del maximum
            if accessible is desktop:
                return iter([application])
            if accessible is application:
                return iter([focused_elsewhere, under_pointer])
            return iter(())

        tree._children = children
        result = tree._top_level_for_window(
            desktop,
            (1920, 0, 1920, 1080, 1234, "New page — Brave"),
        )

        self.assertIs(result, under_pointer)

    def test_generic_application_name_does_not_claim_a_window(self):
        self.assertFalse(
            AccessibilityTree._application_matches_window(
                "Web Browser", "Example page"
            )
        )

    def test_window_ranking_handles_unnamed_accessibility_windows(self):
        atspi = SimpleNamespace(
            StateType=SimpleNamespace(ACTIVE=1),
            CoordType=SimpleNamespace(SCREEN=1),
        )
        tree = AccessibilityTree(atspi)
        desktop = object()
        browser_application = FakeAccessible(
            name="Brave Browser", process_id=99
        )
        unnamed_application = FakeAccessible(process_id=1234)
        browser = FakeAccessible(
            name="Example — Brave",
            extents=SimpleNamespace(
                x=0, y=0, width=1920, height=1080
            ),
        )
        unnamed = FakeAccessible(
            name="",
            extents=SimpleNamespace(
                x=0, y=0, width=800, height=600
            ),
        )

        def children(accessible, maximum=512):
            del maximum
            if accessible is desktop:
                return iter(
                    [browser_application, unnamed_application]
                )
            if accessible is browser_application:
                return iter([browser])
            if accessible is unnamed_application:
                return iter([unnamed])
            return iter(())

        tree._children = children

        self.assertIs(
            tree._top_level_for_window(
                desktop,
                (1920, 0, 1920, 1080, 1234, "Example — Brave"),
            ),
            browser,
        )

    def test_maps_compositor_point_into_atspi_window(self):
        atspi = SimpleNamespace(CoordType=SimpleNamespace(SCREEN=1))
        tree = AccessibilityTree(atspi)
        top_level = FakeAccessible(
            extents=SimpleNamespace(x=20, y=20, width=960, height=1048)
        )
        mapped = tree._map_point(
            top_level,
            2880,
            540,
            (1920, 0, 1920, 1080, 1234, "Firefox"),
        )
        self.assertEqual(mapped, (500, 544))

    def test_legacy_bridge_translates_to_monitor_local_coordinates(self):
        tree = AccessibilityTree(atspi=None)
        mapped = tree._map_point(
            None,
            2500,
            732,
            (1920, 208, 1920, 1080, -1, ""),
        )
        self.assertEqual(mapped, (580, 524))

    def test_finds_link_at_point_when_hit_test_returns_plain_text(self):
        link = FakeSemanticAccessible(
            "text",
            attributes={"xml-roles": "link"},
            extents=SimpleNamespace(
                x=300, y=400, width=120, height=24
            ),
        )
        comment = FakeSemanticAccessible(
            "section",
            children=[
                FakeSemanticAccessible("static"),
                link,
            ],
        )
        tree = AccessibilityTree(
            SimpleNamespace(CoordType=SimpleNamespace(SCREEN=1))
        )

        self.assertTrue(
            tree._has_hyperlink_at_point(comment, 350, 410)
        )
        self.assertFalse(
            tree._has_hyperlink_at_point(comment, 500, 410)
        )

    def test_youtube_comment_text_scrolls_but_actual_link_is_native(self):
        link = FakeSemanticAccessible(
            "link",
            extents=SimpleNamespace(
                x=300, y=400, width=120, height=24
            ),
        )
        comment = FakeSemanticAccessible(
            "section",
            children=[
                FakeSemanticAccessible("static"),
                link,
            ],
        )
        tree = AccessibilityTree(
            SimpleNamespace(CoordType=SimpleNamespace(SCREEN=1))
        )

        def nodes():
            return [
                SemanticNode.create(
                    "text", actions=["clickancestor"]
                ),
                SemanticNode.create(
                    "section",
                    actions=["click", "showcontextmenu"],
                ),
            ]

        plain_text_nodes = nodes()
        tree._mark_hyperlink_at_point(
            [object(), comment],
            plain_text_nodes,
            "Brave Browser",
            500,
            410,
        )
        self.assertEqual(
            classify_chain(plain_text_nodes, "Brave Browser"),
            Decision.SCROLL,
        )

        link_nodes = nodes()
        tree._mark_hyperlink_at_point(
            [object(), comment],
            link_nodes,
            "Brave Browser",
            350,
            410,
        )
        self.assertTrue(link_nodes[0].hyperlink_target)
        self.assertEqual(
            classify_chain(link_nodes, "Brave Browser"),
            Decision.NATIVE,
        )


class ContextWorkerTests(unittest.TestCase):
    def test_accessibility_lookup_runs_on_glib_thread(self):
        class FakeTree:
            def __init__(self):
                self.thread = None

            def report_at(self, x, y, window):
                self.thread = threading.get_ident()
                return ContextReport(Decision.SCROLL, x=x, y=y)

        class FakeGlib:
            PRIORITY_DEFAULT = 0
            SOURCE_REMOVE = False

            def __init__(self):
                self.callback = None

            def idle_add(self, callback, *, priority):
                self.priority = priority
                self.callback = callback
                return 1

        tree = FakeTree()
        glib = FakeGlib()
        proxy = MainThreadAccessibility(tree, glib, threading.Event())
        reports = []
        caller = threading.Thread(
            target=lambda: reports.append(proxy.report_at(10, 20))
        )
        caller.start()
        while glib.callback is None:
            time.sleep(0.001)
        main_thread = threading.get_ident()
        glib.callback()
        caller.join(timeout=1)

        self.assertFalse(caller.is_alive())
        self.assertEqual(tree.thread, main_thread)
        self.assertEqual(reports[0].decision, Decision.SCROLL)

    def test_receives_daemon_activity_transitions(self):
        class FakeConnection:
            def __init__(self):
                self.chunks = [
                    (
                        b'{"v":2,"type":"activity","active":true,'
                        b'"generation":4}\n'
                        b'{"v":2,"type":"activity","active":false,'
                        b'"generation":5}\n'
                    )
                ]

            def recv(self, _size, _flags):
                if self.chunks:
                    return self.chunks.pop(0)
                raise BlockingIOError

        worker = ContextWorker(
            tree=object(),
            points=LatestPoint(),
            socket_path="/not-used",
            stop=threading.Event(),
        )
        worker.connection = FakeConnection()
        changes = []
        worker.set_active_callback(
            lambda active, generation: changes.append(
                (active, generation)
            )
        )

        worker._receive_activity()

        self.assertEqual(changes, [(True, 4), (False, 5)])
        self.assertEqual(worker.context_generation, 5)

    def test_refresh_request_schedules_a_fresh_pointer_sample(self):
        class FakeConnection:
            def __init__(self):
                self.chunks = [
                    b'{"v":2,"type":"refresh","request_id":23}\n'
                ]

            def recv(self, _size, _flags):
                if self.chunks:
                    return self.chunks.pop(0)
                raise BlockingIOError

        worker = ContextWorker(
            tree=object(),
            points=LatestPoint(),
            socket_path="/not-used",
            stop=threading.Event(),
        )
        worker.connection = FakeConnection()
        refreshes = []
        worker.set_refresh_callback(lambda: refreshes.append(True))

        refreshed = worker._receive_activity()

        self.assertEqual(worker.pending_request_id, 23)
        self.assertEqual(refreshes, [True])
        self.assertTrue(refreshed)

    def test_explicit_refresh_bypasses_background_query_throttle(self):
        worker = ContextWorker(
            tree=object(),
            points=LatestPoint(),
            socket_path="/not-used",
            stop=threading.Event(),
        )
        worker.last_report = ContextReport(
            Decision.SCROLL,
            request_id=22,
        )
        worker.pending_request_id = 23

        self.assertEqual(worker._query_delay(10.0, 9.999), 0.0)

        worker.last_report = ContextReport(
            Decision.SCROLL,
            request_id=23,
        )
        self.assertGreater(worker._query_delay(10.0, 9.999), 0.0)

    def test_receives_visual_cursor_offsets(self):
        class FakeConnection:
            def __init__(self):
                self.chunks = [
                    b'{"v":2,"type":"cursor","x":-19,"y":27,'
                    b'"direction":5}\n'
                ]

            def recv(self, _size, _flags):
                if self.chunks:
                    return self.chunks.pop(0)
                raise BlockingIOError

        worker = ContextWorker(
            tree=object(),
            points=LatestPoint(),
            socket_path="/not-used",
            stop=threading.Event(),
        )
        worker.connection = FakeConnection()
        offsets = []
        worker.set_cursor_callback(
            lambda x, y, direction: offsets.append((x, y, direction))
        )

        worker._receive_activity()

        self.assertEqual(offsets, [(-19, 27, 5)])

    def test_sends_persistent_pause_control_before_context(self):
        class FakeConnection:
            def __init__(self):
                self.messages = []

            def sendall(self, message):
                self.messages.append(message)

        worker = ContextWorker(
            tree=object(),
            points=LatestPoint(),
            socket_path="/not-used",
            stop=threading.Event(),
        )
        worker.connection = FakeConnection()
        worker.set_paused(True)

        worker._send()

        self.assertEqual(
            worker.connection.messages[0],
            b'{"v":2,"type":"control","paused":true}\n',
        )
        self.assertIn(b'"type":"context"', worker.connection.messages[1])
        self.assertFalse(worker.control_dirty)

    def test_retries_unknown_without_pointer_movement(self):
        stop = threading.Event()

        class FakeTree:
            def __init__(self):
                self.calls = 0

            def report_at(self, x, y, window):
                self.calls += 1
                self.assertion = (x, y, window)
                if self.calls == 1:
                    return ContextReport(Decision.UNKNOWN, x=x, y=y)
                stop.set()
                return ContextReport(Decision.SCROLL, x=x, y=y)

        class StationaryPoint:
            @staticmethod
            def wait(_generation, timeout):
                time.sleep(min(timeout, 0.002))
                return 10, 20, 1, None

        tree = FakeTree()
        worker = ContextWorker(
            tree=tree,
            points=StationaryPoint(),
            socket_path="/not-used",
            stop=stop,
        )
        worker._send = lambda: None
        worker._receive_activity = lambda: None

        with (
            mock.patch(
                "contextscroll.context_agent."
                "UNKNOWN_RETRY_INITIAL_SECONDS",
                0.001,
            ),
            mock.patch(
                "contextscroll.context_agent."
                "UNKNOWN_RETRY_MAX_SECONDS",
                0.004,
            ),
        ):
            worker.run()

        self.assertEqual(tree.calls, 2)
        self.assertEqual(tree.assertion, (10, 20, None))

    def test_discards_result_when_pointer_moves_during_lookup(self):
        stop = threading.Event()

        class MovingPoint:
            def __init__(self):
                self.calls = 0

            def wait(self, _generation, _timeout):
                self.calls += 1
                if self.calls <= 2:
                    return 10, 20, 1, None
                return 11, 20, 2, None

        class FakeTree:
            def __init__(self):
                self.calls = 0

            def report_at(self, x, y, window):
                self.calls += 1
                if self.calls == 1:
                    return ContextReport(Decision.SCROLL, x=x, y=y)
                stop.set()
                return ContextReport(Decision.NATIVE, x=x, y=y)

        tree = FakeTree()
        worker = ContextWorker(
            tree=tree,
            points=MovingPoint(),
            socket_path="/not-used",
            stop=stop,
        )
        sent = []
        worker._send = lambda: sent.append(worker.last_report)
        worker._receive_activity = lambda: None

        worker.run()

        self.assertEqual(tree.calls, 2)
        self.assertEqual(worker.last_report.decision, Decision.NATIVE)
        self.assertTrue(
            all(report.decision != Decision.SCROLL for report in sent)
        )


if __name__ == "__main__":
    unittest.main()

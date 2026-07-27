import unittest
import threading
import time
from types import SimpleNamespace
from unittest import mock

from contextscroll.classifier import Decision, SemanticNode
from contextscroll.context_agent import (
    AccessibilityTree,
    ContextWorker,
    LatestPoint,
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
        atspi = SimpleNamespace(StateType=SimpleNamespace(ACTIVE=1))
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

    def test_finds_real_link_inside_clickable_browser_card(self):
        link = FakeSemanticAccessible(
            "text", attributes={"xml-roles": "link"}
        )
        card = FakeSemanticAccessible(
            "section",
            children=[
                FakeSemanticAccessible(
                    "section",
                    children=[
                        FakeSemanticAccessible(
                            "section", children=[link]
                        )
                    ],
                )
            ],
        )
        tree = AccessibilityTree(atspi=None)

        self.assertTrue(tree._has_hyperlink_descendant(card))

    def test_does_not_treat_an_image_uri_as_a_hyperlink(self):
        image = FakeSemanticAccessible("image")
        image.is_hyperlink = lambda: True
        card = FakeSemanticAccessible("section", children=[image])
        tree = AccessibilityTree(atspi=None)

        self.assertFalse(tree._has_hyperlink_descendant(card))

    def test_marks_linked_click_container_only_in_a_browser(self):
        card = FakeSemanticAccessible(
            "section",
            children=[FakeSemanticAccessible("link")],
            extents=SimpleNamespace(
                x=100, y=100, width=400, height=250
            ),
        )
        top_level = FakeSemanticAccessible(
            "frame",
            extents=SimpleNamespace(
                x=0, y=0, width=1920, height=1080
            ),
        )
        node = SemanticNode.create("section", actions=["click"])
        tree = AccessibilityTree(atspi=None)
        tree.atspi = SimpleNamespace(
            CoordType=SimpleNamespace(SCREEN=1)
        )

        browser_nodes = [node]
        tree._mark_linked_click_target(
            [card], browser_nodes, "Brave Browser", top_level
        )
        self.assertTrue(browser_nodes[0].hyperlink_target)

        desktop_nodes = [node]
        tree._mark_linked_click_target(
            [card], desktop_nodes, "Example Application", top_level
        )
        self.assertFalse(desktop_nodes[0].hyperlink_target)

    def test_does_not_scan_a_page_sized_click_container(self):
        page = FakeSemanticAccessible(
            "section",
            children=[FakeSemanticAccessible("link")],
            extents=SimpleNamespace(
                x=0, y=100, width=1920, height=900
            ),
        )
        top_level = FakeSemanticAccessible(
            "frame",
            extents=SimpleNamespace(
                x=0, y=0, width=1920, height=1080
            ),
        )
        nodes = [SemanticNode.create("section", actions=["click"])]
        tree = AccessibilityTree(
            SimpleNamespace(CoordType=SimpleNamespace(SCREEN=1))
        )

        tree._mark_linked_click_target(
            [page], nodes, "Firefox", top_level
        )

        self.assertFalse(nodes[0].hyperlink_target)

    def test_finds_link_at_point_when_hit_test_returns_plain_text(self):
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

        self.assertTrue(
            tree._has_hyperlink_at_point(comment, 350, 410)
        )
        self.assertFalse(
            tree._has_hyperlink_at_point(comment, 500, 410)
        )


class ContextWorkerTests(unittest.TestCase):
    def test_receives_daemon_activity_transitions(self):
        class FakeConnection:
            def __init__(self):
                self.chunks = [
                    (
                        b'{"v":1,"type":"activity","active":true}\n'
                        b'{"v":1,"type":"activity","active":false}\n'
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
        worker.set_active_callback(changes.append)

        worker._receive_activity()

        self.assertEqual(changes, [False, True, False])

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

import unittest
import threading
from types import SimpleNamespace

from contextscroll.context_agent import (
    AccessibilityTree,
    ContextWorker,
    LatestPoint,
)


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


if __name__ == "__main__":
    unittest.main()

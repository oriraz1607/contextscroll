"""Low-overhead pointer coordinates for semantic accessibility hit testing.

On GNOME Wayland, a tiny Shell extension publishes compositor coordinates and
focused-window geometry. The helper uses that geometry to translate into each
toolkit's AT-SPI coordinate space. X11 sessions use XQueryPointer snapshots.
"""

from __future__ import annotations

import ctypes
import ctypes.util


class PointerUnavailable(RuntimeError):
    """Raised when the desktop does not expose X11/XWayland coordinates."""


class GnomeShellPointer:
    BUS_NAME = "org.contextscroll.Pointer"
    OBJECT_PATH = "/org/contextscroll/Pointer"
    INTERFACE = "org.contextscroll.Pointer"

    def __init__(self, gio, glib=None):
        self._gio = gio
        self._glib = glib
        self._callback = None
        self._handler = None
        self._legacy = False
        self._monitors = []
        try:
            bus = gio.bus_get_sync(gio.BusType.SESSION, None)
            self._bus = bus
            self._proxy = gio.DBusProxy.new_sync(
                bus,
                gio.DBusProxyFlags.NONE,
                None,
                self.BUS_NAME,
                self.OBJECT_PATH,
                self.INTERFACE,
                None,
            )
            self._handler = self._proxy.connect(
                "g-signal", self._on_signal
            )
            self._position = self._read_position()
        except Exception as error:
            self.close()
            raise PointerUnavailable(
                "ContextScroll GNOME pointer bridge is unavailable"
            ) from error

    def _read_position(self):
        try:
            result = self._proxy.call_sync(
                "GetSnapshot",
                None,
                self._gio.DBusCallFlags.NONE,
                1_000,
                None,
            )
        except Exception:
            # GNOME Wayland keeps an extension's JavaScript module cached
            # until the graphical session restarts. Remain functional while a
            # just-upgraded bridge is still exposing its v1 x/y-only API.
            self._legacy = True
            self._monitors = self._read_monitors()
            result = self._proxy.call_sync(
                "GetPosition",
                None,
                self._gio.DBusCallFlags.NONE,
                1_000,
                None,
            )
            x, y = result.unpack()
            return self._legacy_snapshot(int(x), int(y))
        values = result.unpack()
        return tuple(int(value) for value in values[:-1]) + (values[-1],)

    def _read_monitors(self):
        try:
            proxy = self._gio.DBusProxy.new_sync(
                self._bus,
                self._gio.DBusProxyFlags.NONE,
                None,
                "org.gnome.Mutter.DisplayConfig",
                "/org/gnome/Mutter/DisplayConfig",
                "org.gnome.Mutter.DisplayConfig",
                None,
            )
            state = proxy.call_sync(
                "GetCurrentState",
                None,
                self._gio.DBusCallFlags.NONE,
                1_000,
                None,
            ).unpack()
            modes_by_connector = {}
            for monitor in state[1]:
                connector = monitor[0][0]
                for mode in monitor[1]:
                    if mode[6].get("is-current", False):
                        modes_by_connector[connector] = (mode[1], mode[2])
                        break
            result = []
            for logical in state[2]:
                x, y, scale, transform = logical[:4]
                connector = logical[5][0][0]
                width, height = modes_by_connector[connector]
                if transform in (1, 3, 5, 7):
                    width, height = height, width
                result.append(
                    (
                        int(x),
                        int(y),
                        round(width / scale),
                        round(height / scale),
                    )
                )
            return result
        except Exception:
            return []

    def _legacy_snapshot(self, x: int, y: int):
        for monitor_x, monitor_y, width, height in self._monitors:
            if (
                monitor_x <= x < monitor_x + width
                and monitor_y <= y < monitor_y + height
            ):
                # pid=-1 marks monitor geometry rather than a window frame.
                return (
                    x,
                    y,
                    monitor_x,
                    monitor_y,
                    width,
                    height,
                    -1,
                    "",
                )
        return x, y, 0, 0, 0, 0, -1, ""

    def _on_signal(self, _proxy, _sender, signal_name, parameters):
        if signal_name == "PositionChanged" and self._legacy:
            x, y = parameters.unpack()
            self._position = self._legacy_snapshot(int(x), int(y))
        elif signal_name == "ContextChanged":
            values = parameters.unpack()
            self._position = (
                tuple(int(value) for value in values[:-1]) + (values[-1],)
            )
        else:
            return
        if self._callback is not None:
            self._callback(*self._position)

    def connect(self, callback) -> None:
        self._callback = callback

    def position(self):
        return self._position

    def set_indicator(self, active: bool) -> None:
        if self._proxy is None or self._glib is None:
            return
        try:
            self._proxy.call_sync(
                "SetIndicator",
                self._glib.Variant("(b)", (bool(active),)),
                self._gio.DBusCallFlags.NONE,
                1_000,
                None,
            )
        except Exception:
            # Extension upgrades are cached by GNOME Wayland until the next
            # graphical login. Context recognition must continue meanwhile.
            return

    def close(self) -> None:
        self.set_indicator(False)
        proxy = getattr(self, "_proxy", None)
        handler = getattr(self, "_handler", None)
        if proxy is not None and handler is not None:
            proxy.disconnect(handler)
        self._handler = None
        self._proxy = None
        self._callback = None


class X11Pointer:
    def __init__(self, library=None):
        if library is None:
            name = ctypes.util.find_library("X11")
            if not name:
                raise PointerUnavailable("libX11 was not found")
            library = ctypes.CDLL(name)
        self._library = library
        self._configure_signatures()
        self._display = self._library.XOpenDisplay(None)
        if not self._display:
            raise PointerUnavailable(
                "X11/XWayland display is unavailable; check DISPLAY"
            )
        self._root = self._library.XDefaultRootWindow(self._display)
        if not self._root:
            self.close()
            raise PointerUnavailable("X11/XWayland has no root window")

    def _configure_signatures(self) -> None:
        pointer_ulong = ctypes.POINTER(ctypes.c_ulong)
        pointer_int = ctypes.POINTER(ctypes.c_int)
        self._library.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self._library.XOpenDisplay.restype = ctypes.c_void_p
        self._library.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        self._library.XDefaultRootWindow.restype = ctypes.c_ulong
        self._library.XQueryPointer.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            pointer_ulong,
            pointer_ulong,
            pointer_int,
            pointer_int,
            pointer_int,
            pointer_int,
            ctypes.POINTER(ctypes.c_uint),
        ]
        self._library.XQueryPointer.restype = ctypes.c_int
        self._library.XCloseDisplay.argtypes = [ctypes.c_void_p]
        self._library.XCloseDisplay.restype = ctypes.c_int

    def position(self) -> tuple[int, int] | None:
        if not self._display:
            return None
        root_return = ctypes.c_ulong()
        child_return = ctypes.c_ulong()
        root_x = ctypes.c_int()
        root_y = ctypes.c_int()
        window_x = ctypes.c_int()
        window_y = ctypes.c_int()
        mask = ctypes.c_uint()
        found = self._library.XQueryPointer(
            self._display,
            self._root,
            ctypes.byref(root_return),
            ctypes.byref(child_return),
            ctypes.byref(root_x),
            ctypes.byref(root_y),
            ctypes.byref(window_x),
            ctypes.byref(window_y),
            ctypes.byref(mask),
        )
        if not found:
            return None
        return root_x.value, root_y.value

    def close(self) -> None:
        display = getattr(self, "_display", None)
        if display:
            self._library.XCloseDisplay(display)
            self._display = None

    def __enter__(self):
        return self

    def __exit__(self, _exception_type, _exception, _traceback):
        self.close()

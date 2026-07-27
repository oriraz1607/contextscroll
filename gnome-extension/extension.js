import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {getPointerWatcher} from 'resource:///org/gnome/shell/ui/pointerWatcher.js';

const BUS_NAME = 'org.contextscroll.Pointer';
const OBJECT_PATH = '/org/contextscroll/Pointer';
const SAMPLE_MILLISECONDS = 16;
const INTERFACE_XML = `
<node>
  <interface name="org.contextscroll.Pointer">
    <method name="GetPosition">
      <arg type="i" name="x" direction="out"/>
      <arg type="i" name="y" direction="out"/>
    </method>
    <method name="GetSnapshot">
      <arg type="i" name="x" direction="out"/>
      <arg type="i" name="y" direction="out"/>
      <arg type="i" name="window_x" direction="out"/>
      <arg type="i" name="window_y" direction="out"/>
      <arg type="i" name="window_width" direction="out"/>
      <arg type="i" name="window_height" direction="out"/>
      <arg type="i" name="window_pid" direction="out"/>
      <arg type="s" name="window_title" direction="out"/>
    </method>
    <method name="SetIndicator">
      <arg type="b" name="active" direction="in"/>
    </method>
    <method name="SetIndicatorOffset">
      <arg type="i" name="x" direction="in"/>
      <arg type="i" name="y" direction="in"/>
    </method>
    <signal name="ContextChanged">
      <arg type="i" name="x"/>
      <arg type="i" name="y"/>
      <arg type="i" name="window_x"/>
      <arg type="i" name="window_y"/>
      <arg type="i" name="window_width"/>
      <arg type="i" name="window_height"/>
      <arg type="i" name="window_pid"/>
      <arg type="s" name="window_title"/>
    </signal>
  </interface>
</node>`;

export default class ContextScrollPointerExtension extends Extension {
    enable() {
        [this._x, this._y] = global.get_pointer();
        this._cursorTracker = global.backend.get_cursor_tracker();
        this._seat = Clutter.get_default_backend().get_default_seat();
        this._indicatorActive = false;
        this._cursorOffsetX = 0;
        this._cursorOffsetY = 0;
        this._visualX = this._x;
        this._visualY = this._y;
        this._cursorHidden = false;
        this._focusInhibited = false;
        this._cursorRevealId = 0;
        this._cursorIcon = new St.Icon({
            reactive: false,
            can_focus: false,
            track_hover: false,
            icon_size: 28,
            width: 28,
            height: 28,
            gicon: Gio.Icon.new_for_string(
                `${this.path}/autoscroll-cursor.svg`
            ),
        });
        this._cursorIcon.hide();
        // A non-reactive top-chrome actor stays above application windows
        // without participating in pointer picking.
        Main.layoutManager.addTopChrome(this._cursorIcon);
        this._dbus = Gio.DBusExportedObject.wrapJSObject(
            INTERFACE_XML,
            this
        );
        this._dbus.export(Gio.DBus.session, OBJECT_PATH);
        this._nameId = Gio.DBus.session.own_name(
            BUS_NAME,
            Gio.BusNameOwnerFlags.NONE,
            null,
            null
        );
        this._pointerWatcher = getPointerWatcher();
        this._watch = this._pointerWatcher.addWatch(
            SAMPLE_MILLISECONDS,
            (x, y) => this._update(x, y)
        );
    }

    disable() {
        this._setCursorActive(false);
        if (this._cursorRevealId) {
            GLib.source_remove(this._cursorRevealId);
            this._cursorRevealId = 0;
        }
        if (this._cursorIcon?.get_parent())
            Main.layoutManager.removeChrome(this._cursorIcon);
        this._cursorIcon?.destroy();
        this._cursorIcon = null;
        this._cursorTracker = null;
        this._seat = null;
        this._watch?.remove();
        this._watch = null;
        this._pointerWatcher = null;
        if (this._nameId) {
            Gio.DBus.session.unown_name(this._nameId);
            this._nameId = 0;
        }
        this._dbus?.unexport();
        this._dbus = null;
    }

    GetPosition() {
        return [this._x, this._y];
    }

    GetSnapshot() {
        return this._snapshot();
    }

    SetIndicator(active) {
        this._setCursorActive(active);
    }

    SetIndicatorOffset(x, y) {
        this._cursorOffsetX = x;
        this._cursorOffsetY = y;
        if (this._indicatorActive)
            this._moveCursorIcon();
    }

    _setCursorActive(active) {
        if (!this._cursorIcon || !this._cursorTracker || !this._seat)
            return;
        const wasActive = this._indicatorActive;
        this._indicatorActive = active;
        if (this._cursorRevealId) {
            GLib.source_remove(this._cursorRevealId);
            this._cursorRevealId = 0;
        }
        if (!active) {
            if (wasActive && this._seat.warp_pointer)
                this._seat.warp_pointer(this._visualX, this._visualY);
            this._x = this._visualX;
            this._y = this._visualY;
            this._cursorOffsetX = 0;
            this._cursorOffsetY = 0;
            this._cursorIcon.hide();
            if (this._focusInhibited) {
                this._seat.uninhibit_unfocus();
                this._focusInhibited = false;
            }
            if (this._cursorHidden) {
                this._cursorTracker.uninhibit_cursor_visibility();
                this._cursorHidden = false;
            }
            return;
        }
        this._cursorOffsetX = 0;
        this._cursorOffsetY = 0;
        this._moveCursorIcon();
        this._cursorIcon.opacity = 255;
        this._cursorIcon.show();
        if (!this._focusInhibited) {
            this._seat.inhibit_unfocus();
            this._focusInhibited = true;
        }
        // Let Shell map and allocate the replacement before hiding the
        // hardware cursor. If the actor cannot be mapped, leave the normal
        // cursor visible instead of producing an invisible pointer.
        this._cursorRevealId = GLib.idle_add(
            GLib.PRIORITY_HIGH_IDLE,
            () => {
                this._cursorRevealId = 0;
                if (
                    this._indicatorActive &&
                    this._cursorIcon?.mapped &&
                    !this._cursorHidden
                ) {
                    this._cursorTracker.inhibit_cursor_visibility();
                    this._cursorHidden = true;
                }
                return GLib.SOURCE_REMOVE;
            }
        );
    }

    _moveCursorIcon() {
        const halfSize = 14;
        this._visualX = Math.max(
            halfSize,
            Math.min(global.stage.width - halfSize, this._x + this._cursorOffsetX)
        );
        this._visualY = Math.max(
            halfSize,
            Math.min(global.stage.height - halfSize, this._y + this._cursorOffsetY)
        );
        this._cursorIcon?.set_position(
            this._visualX - halfSize,
            this._visualY - halfSize
        );
    }

    _windowAtPoint() {
        let actor = global.stage.get_actor_at_pos(
            Clutter.PickMode.ALL,
            this._x,
            this._y
        );
        if (actor && this._cursorIcon?.contains(actor))
            actor = null;
        while (actor) {
            if (actor.meta_window)
                return actor.meta_window;
            actor = actor.get_parent();
        }
        // A non-reactive overlay or Shell chrome may be the picked actor.
        // Fall back to the topmost visible compositor window containing the
        // point, keeping the indicator click-through.
        const windows = global.get_window_actors();
        for (let index = windows.length - 1; index >= 0; index--) {
            const windowActor = windows[index];
            const window = windowActor.meta_window;
            const rect = window.get_frame_rect();
            if (
                windowActor.visible &&
                rect.x <= this._x &&
                this._x < rect.x + rect.width &&
                rect.y <= this._y &&
                this._y < rect.y + rect.height
            )
                return window;
        }
        return null;
    }

    _snapshot() {
        // Focus is not a reliable proxy: a middle click is commonly the first
        // interaction with a window on another monitor. Stage picking gives
        // us the compositor window actually beneath the pointer without
        // querying or delaying the input event.
        const window = this._windowAtPoint();
        if (!window)
            return [this._x, this._y, 0, 0, 0, 0, 0, ''];
        const rect = window.get_frame_rect();
        return [
            this._x,
            this._y,
            rect.x,
            rect.y,
            rect.width,
            rect.height,
            window.get_pid(),
            window.get_title() ?? '',
        ];
    }

    _update(x, y) {
        if (x === this._x && y === this._y)
            return;
        this._x = x;
        this._y = y;
        if (this._cursorHidden)
            this._moveCursorIcon();
        this._dbus.emit_signal(
            'ContextChanged',
            new GLib.Variant('(iiiiiiis)', this._snapshot())
        );
    }
}

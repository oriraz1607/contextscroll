import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {getPointerWatcher} from 'resource:///org/gnome/shell/ui/pointerWatcher.js';
import * as QuickSettings from 'resource:///org/gnome/shell/ui/quickSettings.js';

const BUS_NAME = 'org.contextscroll.Pointer';
const OBJECT_PATH = '/org/contextscroll/Pointer';
const SAMPLE_MILLISECONDS = 16;
const CURSOR_SIZE = 24;
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
    <method name="SetIndicatorState">
      <arg type="i" name="x" direction="in"/>
      <arg type="i" name="y" direction="in"/>
      <arg type="u" name="direction" direction="in"/>
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
        this._cursorDirection = 0;
        this._visualX = this._x;
        this._visualY = this._y;
        this._cursorHidden = false;
        this._focusInhibited = false;
        this._unredirectInhibited = false;
        this._cursorRevealId = 0;
        this._neutralCursor = Gio.Icon.new_for_string(
            `${this.path}/autoscroll-cursor.svg`
        );
        this._directionCursor = Gio.Icon.new_for_string(
            `${this.path}/autoscroll-direction.svg`
        );
        this._cursorIcon = new St.Icon({
            reactive: false,
            can_focus: false,
            track_hover: false,
            icon_size: CURSOR_SIZE,
            width: CURSOR_SIZE,
            height: CURSOR_SIZE,
            gicon: this._neutralCursor,
        });
        this._cursorIcon.set_pivot_point(0.5, 0.5);
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
        this._settings = this.getSettings();
        this._directionSettingsId = this._settings.connect(
            'changed::direction-aware-cursor',
            () => this._updateCursorDirection()
        );
        this._pausedSettingsId = this._settings.connect(
            'changed::paused',
            () => this._updateQuickToggle()
        );
        this._quickToggle = new QuickSettings.QuickToggle({
            title: 'ContextScroll',
            iconName: 'input-mouse-symbolic',
            toggleMode: true,
        });
        this._quickToggleId = this._quickToggle.connect('clicked', () => {
            this._settings.set_boolean(
                'paused',
                !this._quickToggle.checked
            );
        });
        this._quickIndicator = new QuickSettings.SystemIndicator();
        this._quickIndicator.quickSettingsItems.push(this._quickToggle);
        Main.panel.statusArea.quickSettings.addExternalIndicator(
            this._quickIndicator
        );
        this._updateQuickToggle();
    }

    disable() {
        this._setCursorActive(false);
        this._releaseCompositing();
        if (this._cursorRevealId) {
            GLib.source_remove(this._cursorRevealId);
            this._cursorRevealId = 0;
        }
        if (this._cursorIcon?.get_parent())
            Main.layoutManager.removeChrome(this._cursorIcon);
        this._cursorIcon?.destroy();
        this._cursorIcon = null;
        this._neutralCursor = null;
        this._directionCursor = null;
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
        if (this._quickToggleId)
            this._quickToggle?.disconnect(this._quickToggleId);
        this._quickToggleId = 0;
        this._quickIndicator?.quickSettingsItems.forEach(
            item => item.destroy()
        );
        this._quickIndicator?.destroy();
        this._quickIndicator = null;
        this._quickToggle = null;
        if (this._directionSettingsId)
            this._settings?.disconnect(this._directionSettingsId);
        if (this._pausedSettingsId)
            this._settings?.disconnect(this._pausedSettingsId);
        this._directionSettingsId = 0;
        this._pausedSettingsId = 0;
        this._settings = null;
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

    SetIndicatorState(x, y, direction) {
        this._cursorOffsetX = x;
        this._cursorOffsetY = y;
        this._cursorDirection = [0, 1, 5].includes(direction)
            ? direction
            : 0;
        this._updateCursorDirection();
        if (this._indicatorActive)
            this._moveCursorIcon();
    }

    _updateQuickToggle() {
        if (!this._quickToggle || !this._settings)
            return;
        const paused = this._settings.get_boolean('paused');
        this._quickToggle.checked = !paused;
        this._quickToggle.subtitle = paused ? 'Paused' : 'Active';
    }

    _updateCursorDirection() {
        if (!this._cursorIcon || !this._settings)
            return;
        const directional = this._settings.get_boolean(
            'direction-aware-cursor'
        );
        if (!directional || this._cursorDirection === 0) {
            this._cursorIcon.gicon = this._neutralCursor;
            this._cursorIcon.set_rotation_angle(
                Clutter.RotateAxis.Z_AXIS, 0
            );
            return;
        }
        this._cursorIcon.gicon = this._directionCursor;
        this._cursorIcon.set_rotation_angle(
            Clutter.RotateAxis.Z_AXIS,
            this._cursorDirection === 1 ? 0 : 180
        );
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
            this._cursorDirection = 0;
            this._updateCursorDirection();
            this._cursorIcon.hide();
            if (this._focusInhibited) {
                this._seat.uninhibit_unfocus();
                this._focusInhibited = false;
            }
            if (this._cursorHidden) {
                this._cursorTracker.uninhibit_cursor_visibility();
                this._cursorHidden = false;
            }
            this._releaseCompositing();
            return;
        }
        this._holdCompositing();
        this._cursorOffsetX = 0;
        this._cursorOffsetY = 0;
        this._cursorDirection = 0;
        this._updateCursorDirection();
        this._moveCursorIcon();
        Main.layoutManager.uiGroup.set_child_above_sibling(
            this._cursorIcon,
            null
        );
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

    _holdCompositing() {
        if (this._unredirectInhibited)
            return;
        // Fullscreen windows may use direct scanout and bypass Shell's
        // overlay actors. Keep the compositor active only while the
        // replacement cursor is visible.
        global.compositor.disable_unredirect();
        this._unredirectInhibited = true;
    }

    _releaseCompositing() {
        if (!this._unredirectInhibited)
            return;
        global.compositor.enable_unredirect();
        this._unredirectInhibited = false;
    }

    _moveCursorIcon() {
        const halfSize = CURSOR_SIZE / 2;
        const monitor = Main.layoutManager.findMonitorForPoint(
            this._x,
            this._y
        );
        const bounds = monitor ?? {
            x: 0,
            y: 0,
            width: global.stage.width,
            height: global.stage.height,
        };
        this._visualX = Math.max(
            bounds.x + halfSize,
            Math.min(
                bounds.x + bounds.width - halfSize,
                this._x + this._cursorOffsetX
            )
        );
        this._visualY = Math.max(
            bounds.y + halfSize,
            Math.min(
                bounds.y + bounds.height - halfSize,
                this._y + this._cursorOffsetY
            )
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

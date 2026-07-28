import Adw from 'gi://Adw';
import Gdk from 'gi://Gdk';
import Gio from 'gi://Gio';
import GObject from 'gi://GObject';
import Gtk from 'gi://Gtk';

import {ExtensionPreferences} from 'resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js';

const MAX_RULES = 128;
const DECISIONS = ['Native middle-click', 'Autoscroll'];

function listFromText(text) {
    return text.split(',')
        .map(item => item.trim())
        .filter(item => item.length > 0)
        .slice(0, 32);
}

function matcherSummary(rule) {
    const parts = [];
    if (rule.application)
        parts.push(rule.application);
    if (rule.role)
        parts.push(rule.role);
    if (rule.name)
        parts.push(`“${rule.name}”`);
    return parts.join(' · ') || 'Semantic matcher';
}

class RulesEditor {
    constructor(settings, group) {
        this._settings = settings;
        this._group = group;
        this._rows = [];
        this._rules = this._readRules();
        this._rebuild();
    }

    _readRules() {
        try {
            const value = JSON.parse(this._settings.get_string('rules-json'));
            return Array.isArray(value)
                ? value.filter(rule => rule && typeof rule === 'object')
                    .slice(0, MAX_RULES)
                : [];
        } catch {
            return [];
        }
    }

    _writeRules() {
        this._settings.set_string('rules-json', JSON.stringify(this._rules));
    }

    addRule() {
        if (this._rules.length >= MAX_RULES)
            return;
        this._rules.push({
            enabled: false,
            application: '',
            role: 'document web',
            states: [],
            actions: [],
            name: '',
            decision: 'scroll',
        });
        this._writeRules();
        this._rebuild();
        const addedRow = this._rows[this._rows.length - 1];
        if (addedRow)
            addedRow.expanded = true;
    }

    _moveRule(from, to) {
        if (from === to || from < 0 || to < 0 ||
            from >= this._rules.length || to >= this._rules.length)
            return;
        const [rule] = this._rules.splice(from, 1);
        this._rules.splice(to, 0, rule);
        this._writeRules();
        this._rebuild();
    }

    _removeRule(index) {
        this._rules.splice(index, 1);
        this._writeRules();
        this._rebuild();
    }

    _rebuild() {
        for (const row of this._rows)
            this._group.remove(row);
        this._rows = [];
        this._rules.forEach((rule, index) => {
            const row = this._createRuleRow(rule, index);
            this._rows.push(row);
            this._group.add(row);
        });
    }

    _createRuleRow(rule, index) {
        const row = new Adw.ExpanderRow({
            title: matcherSummary(rule),
            subtitle: rule.decision === 'scroll'
                ? 'Autoscroll overrides can suppress native middle-click actions'
                : 'Preserves the native middle-click action',
        });
        const enabled = new Gtk.Switch({
            active: rule.enabled !== false,
            valign: Gtk.Align.CENTER,
            tooltip_text: 'Enable this rule',
        });
        enabled.connect('notify::active', widget => {
            rule.enabled = widget.active;
            this._writeRules();
        });
        row.add_suffix(enabled);

        const application = new Adw.EntryRow({
            title: 'Application contains',
            text: rule.application ?? '',
        });
        const role = new Adw.EntryRow({
            title: 'Accessible role',
            text: rule.role ?? '',
        });
        const name = new Adw.EntryRow({
            title: 'Accessible name contains',
            text: rule.name ?? '',
        });
        const states = new Adw.EntryRow({
            title: 'Required states',
            text: (rule.states ?? []).join(', '),
        });
        states.add_suffix(new Gtk.Label({
            label: 'comma separated',
            css_classes: ['dim-label'],
        }));
        const actions = new Adw.EntryRow({
            title: 'Required actions',
            text: (rule.actions ?? []).join(', '),
        });
        actions.add_suffix(new Gtk.Label({
            label: 'comma separated',
            css_classes: ['dim-label'],
        }));
        const decision = new Adw.ComboRow({
            title: 'Result',
            model: Gtk.StringList.new(DECISIONS),
            selected: rule.decision === 'native' ? 0 : 1,
        });

        const updateText = (widget, property) => {
            rule[property] = widget.text;
            row.title = matcherSummary(rule);
            this._writeRules();
        };
        application.connect('changed', widget =>
            updateText(widget, 'application'));
        role.connect('changed', widget => updateText(widget, 'role'));
        name.connect('changed', widget => updateText(widget, 'name'));
        states.connect('changed', widget => {
            rule.states = listFromText(widget.text);
            this._writeRules();
        });
        actions.connect('changed', widget => {
            rule.actions = listFromText(widget.text);
            this._writeRules();
        });
        decision.connect('notify::selected', widget => {
            rule.decision = widget.selected === 0 ? 'native' : 'scroll';
            row.subtitle = rule.decision === 'scroll'
                ? 'Autoscroll overrides can suppress native middle-click actions'
                : 'Preserves the native middle-click action';
            this._writeRules();
        });

        for (const child of [
            application, role, name, states, actions, decision,
        ])
            row.add_row(child);

        const actionsRow = new Adw.ActionRow({
            title: 'Rule order',
            subtitle: 'The first matching enabled rule wins',
        });
        const up = new Gtk.Button({
            icon_name: 'go-up-symbolic',
            tooltip_text: 'Move rule earlier',
            valign: Gtk.Align.CENTER,
            sensitive: index > 0,
        });
        up.connect('clicked', () => this._moveRule(index, index - 1));
        const down = new Gtk.Button({
            icon_name: 'go-down-symbolic',
            tooltip_text: 'Move rule later',
            valign: Gtk.Align.CENTER,
            sensitive: index + 1 < this._rules.length,
        });
        down.connect('clicked', () => this._moveRule(index, index + 1));
        const remove = new Gtk.Button({
            icon_name: 'user-trash-symbolic',
            tooltip_text: 'Delete rule',
            valign: Gtk.Align.CENTER,
            css_classes: ['destructive-action'],
        });
        remove.connect('clicked', () => this._removeRule(index));
        actionsRow.add_suffix(up);
        actionsRow.add_suffix(down);
        actionsRow.add_suffix(remove);
        row.add_row(actionsRow);

        const dragSource = new Gtk.DragSource({actions: Gdk.DragAction.MOVE});
        dragSource.connect('prepare', () => {
            const current = this._rows.indexOf(row);
            return Gdk.ContentProvider.new_for_value(String(current));
        });
        row.add_controller(dragSource);
        const dropTarget = Gtk.DropTarget.new(
            GObject.TYPE_STRING,
            Gdk.DragAction.MOVE
        );
        dropTarget.connect('drop', (_target, value) => {
            const from = Number.parseInt(value, 10);
            const to = this._rows.indexOf(row);
            if (!Number.isInteger(from))
                return false;
            this._moveRule(from, to);
            return true;
        });
        row.add_controller(dropTarget);
        return row;
    }
}

export default class ContextScrollPreferences extends ExtensionPreferences {
    fillPreferencesWindow(window) {
        const settings = this.getSettings();
        const page = new Adw.PreferencesPage({
            title: 'ContextScroll',
            icon_name: 'input-mouse-symbolic',
        });

        const general = new Adw.PreferencesGroup({
            title: 'General',
            description: 'Quick Settings and this switch control the same persistent state.',
        });
        const paused = new Adw.SwitchRow({
            title: 'Pause ContextScroll',
            subtitle: 'Forward all mouse input without starting autoscroll',
        });
        settings.bind(
            'paused',
            paused,
            'active',
            Gio.SettingsBindFlags.DEFAULT
        );
        general.add(paused);
        page.add(general);

        const cursor = new Adw.PreferencesGroup({
            title: 'Cursor',
        });
        const directional = new Adw.SwitchRow({
            title: 'Show scrolling direction',
            subtitle: 'Use a fixed-size up/down cursor during autoscroll',
        });
        settings.bind(
            'direction-aware-cursor',
            directional,
            'active',
            Gio.SettingsBindFlags.DEFAULT
        );
        cursor.add(directional);
        page.add(cursor);

        const rules = new Adw.PreferencesGroup({
            title: 'Context Rules',
            description: 'Rules override built-in recognition in the order shown. All match fields on a rule apply to the same accessible item.',
        });
        const add = new Adw.ActionRow({
            title: 'Add context rule',
            subtitle: 'New rules start disabled',
            activatable: true,
        });
        const addButton = new Gtk.Button({
            icon_name: 'list-add-symbolic',
            tooltip_text: 'Add context rule',
            valign: Gtk.Align.CENTER,
        });
        add.add_suffix(addButton);
        add.activatable_widget = addButton;
        rules.add(add);
        const editor = new RulesEditor(settings, rules);
        addButton.connect('clicked', () => editor.addRule());
        page.add(rules);

        window.add(page);
    }
}

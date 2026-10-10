// Screenwave top-bar indicator.
//
// Always present in the top bar. It mirrors the Screenwave app's state over
// D-Bus and sends it commands; the app is D-Bus activatable, so a command
// (from this menu or from a hotkey) starts it when it isn't running.
// SPDX-License-Identifier: GPL-3.0-or-later

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Graphene from 'gi://Graphene';
import St from 'gi://St';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

// Must match screenwave/ids.py.
const APP_ID = 'io.github.ferhateroglu.Screenwave';
const APP_PATH = '/io/github/ferhateroglu/Screenwave';
const STATUS_PATH = '/io/github/ferhateroglu/Screenwave/Status';
const StatusProxy = Gio.DBusProxy.makeProxyWrapper(`
<node>
  <interface name="io.github.ferhateroglu.Screenwave.Status">
    <property name="State" type="s" access="read"/>
    <property name="Zoom" type="b" access="read"/>
    <property name="Elapsed" type="d" access="read"/>
    <property name="Countdown" type="i" access="read"/>
    <property name="Monitor" type="s" access="read"/>
    <method name="Command">
      <arg name="action" type="s" direction="in"/>
      <arg name="reply" type="s" direction="out"/>
    </method>
  </interface>
</node>`);

// Each countdown number fades out within this time, so it is gone before
// the recording starts at the end of the last second.
const COUNTDOWN_FADE_MS = 800;

function formatElapsed(seconds) {
    const s = Math.floor(seconds);
    const mm = String(Math.floor(s / 60) % 60).padStart(2, '0');
    const ss = String(s % 60).padStart(2, '0');
    return s >= 3600 ? `${Math.floor(s / 3600)}:${mm}:${ss}` : `${mm}:${ss}`;
}

function callApp(iface, method, params, autoStart) {
    Gio.DBus.session.call(
        APP_ID, APP_PATH, iface, method, params, null,
        autoStart ? Gio.DBusCallFlags.NONE : Gio.DBusCallFlags.NO_AUTO_START,
        -1, null, (conn, res) => {
            try {
                conn.call_finish(res);
            } catch (e) {
                logError(e, `Screenwave ${method}`);
            }
        });
}

// Big centered 3-2-1 on the monitor being recorded.
class CountdownOverlay {
    constructor() {
        this._label = new St.Label({
            style_class: 'screenwave-countdown-label',
            x_align: Clutter.ActorAlign.CENTER,
            y_align: Clutter.ActorAlign.CENTER,
        });
        this._actor = new St.Bin({
            style_class: 'screenwave-countdown-overlay',
            child: this._label,
            pivot_point: new Graphene.Point({x: 0.5, y: 0.5}),
        });
        this._actor.reactive = false;
        Main.uiGroup.add_child(this._actor);
    }

    show(number, connector) {
        this._label.text = String(number);
        const monitor = this._monitor(connector);
        const [, w] = this._actor.get_preferred_width(-1);
        const [, h] = this._actor.get_preferred_height(w);
        this._actor.set_position(
            Math.round(monitor.x + (monitor.width - w) / 2),
            Math.round(monitor.y + (monitor.height - h) / 2));
        this._actor.remove_all_transitions();
        this._actor.opacity = 255;
        this._actor.scale_x = this._actor.scale_y = 1.0;
        this._actor.ease({
            opacity: 0,
            scale_x: 0.7,
            scale_y: 0.7,
            duration: COUNTDOWN_FADE_MS,
            mode: Clutter.AnimationMode.EASE_IN_QUAD,
        });
    }

    _monitor(connector) {
        const lm = Main.layoutManager;
        let index = -1;
        try {
            index = global.backend.get_monitor_manager().get_monitor_for_connector(connector);
        } catch (e) {
            // older shells: fall back to the primary monitor
        }
        return lm.monitors[index] ?? lm.primaryMonitor;
    }

    destroy() {
        this._actor.remove_all_transitions();
        this._actor.destroy();
    }
}

const Indicator = GObject.registerClass(
class ScreenwaveIndicator extends PanelMenu.Button {
    _init() {
        super._init(0.5, 'Screenwave');
        this._proxy = null;
        this._timerId = 0;
        this._overlay = null;
        this._shownCount = 0;

        const box = new St.BoxLayout({style_class: 'panel-status-menu-box'});
        this._stateIcon = new St.Icon({style_class: 'system-status-icon'});
        this._timeLabel = new St.Label({
            style_class: 'screenwave-time',
            y_align: Clutter.ActorAlign.CENTER,
        });
        this._zoomIcon = new St.Icon({
            icon_name: 'zoom-in-symbolic',
            style_class: 'system-status-icon screenwave-zoom',
        });
        box.add_child(this._stateIcon);
        box.add_child(this._timeLabel);
        box.add_child(this._zoomIcon);
        this.add_child(box);

        this._recordItem = this.menu.addAction('', () => this._command('record-toggle'));
        this._pauseItem = this.menu.addAction('', () => this._command('toggle'));
        this._zoomItem = new PopupMenu.PopupSwitchMenuItem('Cursor zoom', false);
        // setToggleState() in _sync emits 'toggled' too; only a real click
        // differs from the app's state (otherwise zoom would flip forever)
        this._zoomItem.connect('toggled', (_item, on) => {
            if (on !== Boolean(this._proxy?.Zoom))
                this._command('zoom');
        });
        this.menu.addMenuItem(this._zoomItem);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this.menu.addAction('Open Screenwave', () =>
            callApp('org.freedesktop.Application', 'Activate',
                new GLib.Variant('(a{sv})', [{}]), true));
        this._quitItem = this.menu.addAction('Quit Screenwave', () =>
            callApp('org.freedesktop.Application', 'ActivateAction',
                new GLib.Variant('(sava{sv})', ['quit', [], {}]), false));

        this._sync();
    }

    setProxy(proxy) {
        this._proxy = proxy;
        this._propsId = proxy.connect('g-properties-changed', () => this._sync());
        // app started or quit: the cached properties appear or vanish
        this._ownerId = proxy.connect('notify::g-name-owner', () => this._sync());
        this._sync();
    }

    _sync() {
        const running = Boolean(this._proxy?.g_name_owner);
        const state = (running && this._proxy.State) || 'idle';
        const active = state === 'recording' || state === 'paused';
        const zoom = active && Boolean(this._proxy.Zoom);
        this._elapsed = active ? this._proxy.Elapsed ?? 0 : 0;
        this._elapsedAt = GLib.get_monotonic_time();
        this._state = state;

        this._stateIcon.icon_name = state === 'paused'
            ? 'media-playback-pause-symbolic' : 'media-record-symbolic';
        for (const s of ['recording', 'paused', 'countdown'])
            this._stateIcon.remove_style_class_name(`screenwave-${s}`);
        if (state !== 'idle')
            this._stateIcon.add_style_class_name(`screenwave-${state}`);
        // while recording the zoom slot stays reserved, so the width only
        // changes on start/stop and nothing beside us shifts
        this._timeLabel.visible = state !== 'idle';
        this._zoomIcon.visible = active;
        this._zoomIcon.opacity = zoom ? 255 : 0;

        this._recordItem.label.text = {
            idle: 'Start recording',
            countdown: 'Cancel countdown',
        }[state] ?? 'Stop recording';
        this._pauseItem.label.text = state === 'paused' ? 'Resume' : 'Pause';
        this._pauseItem.setSensitive(active);
        this._zoomItem.setToggleState(zoom);
        this._zoomItem.setSensitive(active);
        this._quitItem.visible = running;

        this._syncCountdown(state);
        this._updateTime();
        this._setTicking(state === 'recording');
    }

    _syncCountdown(state) {
        const count = state === 'countdown' ? this._proxy.Countdown ?? 0 : 0;
        if (count > 0 && count !== this._shownCount) {
            this._overlay ??= new CountdownOverlay();
            this._overlay.show(count, this._proxy.Monitor ?? '');
        } else if (count <= 0) {
            this._overlay?.destroy();
            this._overlay = null;
        }
        this._shownCount = count;
    }

    _updateTime() {
        if (this._state === 'countdown') {
            this._timeLabel.text = `${this._shownCount}…`;
            return;
        }
        let seconds = this._elapsed;
        if (this._state === 'recording')
            seconds += (GLib.get_monotonic_time() - this._elapsedAt) / 1e6;
        this._timeLabel.text = formatElapsed(seconds);
    }

    _setTicking(on) {
        if (on && !this._timerId) {
            this._timerId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 500, () => {
                this._updateTime();
                return GLib.SOURCE_CONTINUE;
            });
        } else if (!on && this._timerId) {
            GLib.source_remove(this._timerId);
            this._timerId = 0;
        }
    }

    _command(action) {
        if (!this._proxy)
            return;
        // auto-starts the app when it isn't running
        this._proxy.CommandAsync(action).catch(e => logError(e, 'Screenwave command'));
    }

    destroy() {
        this._setTicking(false);
        this._overlay?.destroy();
        this._overlay = null;
        if (this._proxy) {
            this._proxy.disconnect(this._propsId);
            this._proxy.disconnect(this._ownerId);
            this._proxy = null;
        }
        super.destroy();
    }
});

export default class ScreenwaveExtension extends Extension {
    enable() {
        this._indicator = new Indicator();
        Main.panel.addToStatusArea(this.uuid, this._indicator);

        this._cancellable = new Gio.Cancellable();
        new StatusProxy(Gio.DBus.session, APP_ID, STATUS_PATH, (proxy, error) => {
            if (error) {
                if (!error.matches(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                    logError(error, 'Screenwave status proxy');
                return;
            }
            this._indicator?.setProxy(proxy);
        }, this._cancellable, Gio.DBusProxyFlags.DO_NOT_AUTO_START_AT_CONSTRUCTION);
    }

    disable() {
        this._cancellable?.cancel();
        this._cancellable = null;
        this._indicator?.destroy();
        this._indicator = null;
    }
}

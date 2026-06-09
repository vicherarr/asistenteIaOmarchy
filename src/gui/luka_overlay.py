#!/usr/bin/env python3
"""Luka overlay — GUI nativa del asistente para Omarchy/Hyprland.

Superficie *layer-shell* (como waybar/mako/walker), NO una ventana:
  - anclada abajo-centro (nunca centrada en pantalla),
  - visible en todos los workspaces (capa overlay; sigue al cambiar de escritorio),
  - en reposo = punto mínimo que no molesta ni roba el foco,
  - al activarse (voz o escritura) crece hacia arriba en un panel-burbuja,
  - **clic en la píldora** abre una caja de texto: toma el teclado SOLO mientras
    escribes (modo exclusivo) y lo suelta con Esc,
  - muestra la respuesta de Luka (en vivo al escribir; vía estado al hablar),
  - se auto-tema con la paleta del tema activo de Omarchy.

Refleja el estado real suscribiéndose al SSE `/status/events` y, al escribir,
envía el texto a `POST /transcribe/stream` mostrando la respuesta en streaming.

Se ejecuta con el PYTHON DEL SISTEMA (tiene PyGObject), no con el venv:

    /usr/bin/python src/gui/luka_overlay.py
"""
from __future__ import annotations

import ctypes

# CRÍTICO: cargar gtk4-layer-shell con RTLD_GLOBAL ANTES de que GTK conecte con
# Wayland; si no, interpone sus símbolos tarde y la ventana sale como xdg-toplevel
# normal (se quedaría atrapada en un workspace en vez de vivir en la capa overlay).
ctypes.CDLL("libgtk4-layer-shell.so.0", mode=ctypes.RTLD_GLOBAL)

import json
import os
import signal
import ssl
import threading
import tomllib
import urllib.request
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402
from gi.repository import Gtk4LayerShell as LayerShell  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[2]
THEME_COLORS = Path.home() / ".config/omarchy/current/theme/colors.toml"
COLLAPSE_SECONDS = 7  # tras terminar un turno, vuelve al punto pasado este tiempo

FALLBACK = {
    "background": "#1e1e2e", "foreground": "#cdd6f4", "accent": "#89b4fa",
    "color1": "#f38ba8", "color2": "#a6e3a1", "color3": "#f9e2af",
    "color4": "#89b4fa", "color8": "#45475a",
}

# estado -> (clase CSS, texto, ¿pulsa el punto?)
STATES = {
    "idle":      ("state-idle",      "",            False),
    "listening": ("state-listening", "Escuchando…", True),
    "thinking":  ("state-thinking",  "Pensando…",   True),
    "speaking":  ("state-speaking",  "Hablando…",   True),
    "offline":   ("state-offline",   "",            False),
}


def load_env() -> dict:
    env: dict[str, str] = {}
    path = PROJECT_ROOT / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            env[key.strip()] = val.strip().strip('"').strip("'")
    return env


def resolve_api() -> tuple[str, str]:
    env = load_env()
    port = env.get("PORT", "8765")
    scheme = "https" if env.get("SSL_CERTFILE") else "http"
    base = os.environ.get("LUKA_API_URL") or f"{scheme}://127.0.0.1:{port}"
    token = os.environ.get("LUKA_API_TOKEN") or env.get("API_TOKEN", "")
    return base.rstrip("/"), token


def load_colors() -> dict:
    colors = dict(FALLBACK)
    try:
        with open(THEME_COLORS, "rb") as fh:
            data = tomllib.load(fh)
        for key in colors:
            if key in data:
                colors[key] = data[key]
    except Exception:
        pass
    return colors


def build_css(c: dict) -> str:
    return f"""
@define-color bg       {c['background']};
@define-color fg       {c['foreground']};
@define-color accent   {c['accent']};
@define-color c_idle   {c['color8']};
@define-color c_listen {c['color2']};
@define-color c_think  {c['color3']};
@define-color c_speak  {c['color4']};
@define-color c_off    {c['color1']};

window {{ background-color: transparent; }}

.luka {{
    background-color: alpha(@bg, 0.94);
    border: 1px solid alpha(@accent, 0.35);
    border-radius: 18px;
    padding: 12px 14px;
    box-shadow: 0 10px 30px alpha(black, 0.5);
    transition: padding 160ms ease, border-radius 160ms ease,
                background-color 200ms ease;
}}
.luka.collapsed {{
    padding: 6px;
    border-radius: 999px;
    background-color: alpha(@bg, 0.55);
    border-color: alpha(@accent, 0.18);
    box-shadow: none;
}}
.luka.collapsed:hover {{ background-color: alpha(@bg, 0.8); }}
.luka.state-offline.collapsed {{ border-color: alpha(@c_off, 0.25); }}

.dot {{
    min-width: 12px; min-height: 12px;
    border-radius: 999px;
    background-color: @c_idle;
    transition: background-color 200ms ease, opacity 600ms ease;
}}
.luka.state-listening .dot {{ background-color: @c_listen; }}
.luka.state-thinking  .dot {{ background-color: @c_think; }}
.luka.state-speaking  .dot {{ background-color: @c_speak; }}
.luka.state-offline   .dot {{ background-color: @c_off; opacity: 0.55; }}
.dot.pulse-on {{ opacity: 0.30; }}

.status  {{ color: alpha(@fg, 0.7); font-size: 12px; margin-left: 8px; }}
.you     {{ color: alpha(@fg, 0.55); font-size: 12px; font-style: italic; }}
.answer  {{ color: @fg; font-size: 13.5px; }}

entry {{
    background-color: alpha(@fg, 0.08);
    color: @fg;
    border-radius: 11px;
    border: 1px solid alpha(@accent, 0.30);
    padding: 7px 11px;
    caret-color: @accent;
    margin-top: 4px;
}}
entry:focus-within {{ border-color: alpha(@accent, 0.7); }}
"""


def derive_state(status: dict) -> str:
    if not status.get("engine_connected", True):
        return "offline"
    if status.get("speaking"):
        return "speaking"
    if status.get("recording"):
        return "listening"
    if status.get("processing"):
        return "thinking"
    return "idle"


class SSEClient(threading.Thread):
    """Suscripción a /status/events en hilo aparte; marshalla al loop GTK."""

    def __init__(self, base_url, token, on_status, on_offline):
        super().__init__(daemon=True)
        self.url = f"{base_url}/status/events"
        self.token = token
        self.on_status = on_status
        self.on_offline = on_offline
        self._stop = threading.Event()
        self._ssl = ssl.create_default_context()
        self._ssl.check_hostname = False
        self._ssl.verify_mode = ssl.CERT_NONE

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            try:
                req = urllib.request.Request(self.url)
                if self.token:
                    req.add_header("X-API-Token", self.token)
                req.add_header("Accept", "text/event-stream")
                with urllib.request.urlopen(req, context=self._ssl) as resp:
                    for raw in resp:
                        if self._stop.is_set():
                            return
                        line = raw.decode("utf-8", "replace").strip()
                        if not line.startswith("data:"):
                            continue
                        try:
                            data = json.loads(line[5:].strip())
                        except json.JSONDecodeError:
                            continue
                        GLib.idle_add(self.on_status, data)
            except Exception:
                GLib.idle_add(self.on_offline)
                if self._stop.wait(2.0):
                    return


class LukaOverlay(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.asistenteia.luka")
        self.colors = load_colors()
        self.base_url, self.token = resolve_api()
        self._ssl = ssl.create_default_context()
        self._ssl.check_hostname = False
        self._ssl.verify_mode = ssl.CERT_NONE
        self._provider = None
        self.state = "offline"
        self.input_mode = False
        self.answer_text = ""
        self.user_text = ""
        self._pulse = False
        self._pulse_on = False
        self._collapsed = True
        self._collapse_id = None
        self._sse = None
        self._theme_monitor = None

    # ---- ciclo de vida -----------------------------------------------------
    def do_activate(self):
        self.win = Gtk.ApplicationWindow(application=self)
        self.win.set_default_size(1, 1)

        LayerShell.init_for_window(self.win)
        LayerShell.set_namespace(self.win, "asistenteia")
        LayerShell.set_layer(self.win, LayerShell.Layer.OVERLAY)
        LayerShell.set_keyboard_mode(self.win, LayerShell.KeyboardMode.NONE)
        LayerShell.set_anchor(self.win, LayerShell.Edge.BOTTOM, True)
        LayerShell.set_margin(self.win, LayerShell.Edge.BOTTOM, 28)
        LayerShell.set_exclusive_zone(self.win, 0)

        # --- contenido: [respuesta] / [eco usuario] / fila(punto+estado) / [entry]
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.box.add_css_class("luka")
        self.box.set_halign(Gtk.Align.CENTER)
        self.box.set_valign(Gtk.Align.END)

        self.answer = Gtk.Label(label="", xalign=0.0)
        self.answer.add_css_class("answer")
        self.answer.set_wrap(True)
        self.answer.set_max_width_chars(46)
        self.answer_rev = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_UP)
        self.answer_rev.set_transition_duration(180)
        self.answer_rev.set_child(self.answer)
        self.box.append(self.answer_rev)

        self.echo = Gtk.Label(label="", xalign=0.0)
        self.echo.add_css_class("you")
        self.echo.set_wrap(True)
        self.echo.set_max_width_chars(46)
        self.echo_rev = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_UP)
        self.echo_rev.set_child(self.echo)
        self.box.append(self.echo_rev)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        row.set_halign(Gtk.Align.CENTER)
        self.dot = Gtk.Box()
        self.dot.add_css_class("dot")
        self.dot.set_valign(Gtk.Align.CENTER)
        row.append(self.dot)
        self.status = Gtk.Label(label="")
        self.status.add_css_class("status")
        self.status_rev = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.CROSSFADE)
        self.status_rev.set_child(self.status)
        row.append(self.status_rev)
        self.box.append(row)

        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text("Escribe a Luka…")
        self.entry.set_width_chars(40)
        self.entry.set_size_request(380, -1)  # ancho mínimo cómodo (si no, se encoge al contenido)
        self.entry.set_hexpand(True)
        self.entry.connect("activate", self._on_send)
        self.entry_rev = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_UP)
        self.entry_rev.set_child(self.entry)
        self.box.append(self.entry_rev)

        # clic en la píldora -> abrir/centrar el modo escritura
        click = Gtk.GestureClick()
        click.connect("released", lambda *_: self._enter_input())
        self.box.add_controller(click)
        # Esc en el entry -> salir del modo escritura
        keyc = Gtk.EventControllerKey()
        keyc.connect("key-pressed", self._on_key)
        self.entry.add_controller(keyc)

        self.win.set_child(self.box)
        self._install_css()
        self.win.present()

        self._refresh()
        GLib.timeout_add(700, self._tick_pulse)
        self._start_theme_watch()
        self.hold()

        self._sse = SSEClient(self.base_url, self.token, self._on_status, self._on_offline)
        self._sse.start()

        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1, self._reload_theme)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, self._quit)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, self._quit)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR2,
                             lambda *_: GLib.SOURCE_CONTINUE)

    def _quit(self, *_):
        if self._sse:
            self._sse.stop()
        self.quit()
        return GLib.SOURCE_REMOVE

    # ---- estado (SSE) ------------------------------------------------------
    def _on_status(self, status: dict):
        self.state = derive_state(status)
        resp = (status.get("last_assistant_response") or "").strip()
        # Solo refleja la respuesta del backend cuando NO estamos escribiendo
        # (en modo escritura la pintamos en vivo desde el POST de streaming).
        if resp and not self.input_mode and resp != self.answer_text:
            self.answer_text = resp
            self.user_text = (status.get("last_user_transcription") or "").strip()
        if self.state in ("listening", "thinking", "speaking"):
            self._cancel_collapse()
            self._collapsed = False
        elif self.state == "idle":
            self._schedule_collapse()
        elif self.state == "offline" and not self.input_mode:
            self._collapsed = True
        self._refresh()
        return GLib.SOURCE_REMOVE

    def _on_offline(self):
        self.state = "offline"
        if not self.input_mode:
            self._collapsed = True
        self._refresh()
        return GLib.SOURCE_REMOVE

    # ---- modo escritura ----------------------------------------------------
    def _enter_input(self):
        if self.input_mode:
            self.entry.grab_focus()
            return
        self.input_mode = True
        self._cancel_collapse()
        self._collapsed = False
        # Tomar el teclado SOLO ahora (modo exclusivo, como un lanzador).
        LayerShell.set_keyboard_mode(self.win, LayerShell.KeyboardMode.EXCLUSIVE)
        self._refresh()
        GLib.idle_add(self.entry.grab_focus)

    def _exit_input(self):
        if not self.input_mode:
            return
        self.input_mode = False
        self.entry.set_text("")
        LayerShell.set_keyboard_mode(self.win, LayerShell.KeyboardMode.NONE)
        self._schedule_collapse()
        self._refresh()

    def _on_key(self, _ctrl, keyval, _code, _state):
        if keyval == Gdk.KEY_Escape:
            self._exit_input()
            return True
        return False

    def _on_send(self, _entry):
        text = self.entry.get_text().strip()
        if not text:
            return
        self.entry.set_text("")
        self.user_text = text
        self.answer_text = ""
        self._collapsed = False
        self._refresh()
        threading.Thread(target=self._stream_post, args=(text,), daemon=True).start()

    def _stream_post(self, text: str):
        try:
            body = json.dumps({"text": text}).encode()
            req = urllib.request.Request(f"{self.base_url}/transcribe/stream",
                                         data=body, method="POST")
            req.add_header("Content-Type", "application/json")
            if self.token:
                req.add_header("X-API-Token", self.token)
            with urllib.request.urlopen(req, context=self._ssl) as resp:
                for raw in resp:
                    chunk = raw.decode("utf-8", "replace")
                    GLib.idle_add(self._append_answer, chunk)
        except Exception as exc:
            GLib.idle_add(self._append_answer, f"\n[error: {exc}]")

    def _append_answer(self, chunk: str):
        self.answer_text += chunk
        self._refresh()
        return GLib.SOURCE_REMOVE

    # ---- colapso -----------------------------------------------------------
    def _cancel_collapse(self):
        if self._collapse_id:
            GLib.source_remove(self._collapse_id)
            self._collapse_id = None

    def _schedule_collapse(self):
        if self.input_mode:
            return
        self._cancel_collapse()
        self._collapse_id = GLib.timeout_add_seconds(COLLAPSE_SECONDS, self._do_collapse)

    def _do_collapse(self):
        self._collapse_id = None
        if not self.input_mode:
            self._collapsed = True
            self._refresh()
        return GLib.SOURCE_REMOVE

    # ---- pintado -----------------------------------------------------------
    def _refresh(self):
        expanded = self.input_mode or not self._collapsed
        if self.state == "offline" and not self.input_mode:
            expanded = False

        # clase de estado + colapsado
        for cls, _, _ in STATES.values():
            self.box.remove_css_class(cls)
        self.box.add_css_class(STATES[self.state][0])
        if expanded:
            self.box.remove_css_class("collapsed")
        else:
            self.box.add_css_class("collapsed")

        show_answer = expanded and bool(self.answer_text)
        show_echo = expanded and bool(self.user_text) and not self.input_mode
        status_txt = STATES[self.state][1] if expanded else ""

        self.answer.set_text(self.answer_text)
        self.answer_rev.set_reveal_child(show_answer)
        self.echo.set_text(f"› {self.user_text}" if self.user_text else "")
        self.echo_rev.set_reveal_child(show_echo)
        self.status.set_text(status_txt)
        self.status_rev.set_reveal_child(bool(status_txt))
        self.entry_rev.set_reveal_child(self.input_mode)

        self._pulse = STATES[self.state][2]
        if not self._pulse:
            self.dot.remove_css_class("pulse-on")

    def _tick_pulse(self):
        if self._pulse:
            self._pulse_on = not self._pulse_on
            if self._pulse_on:
                self.dot.add_css_class("pulse-on")
            else:
                self.dot.remove_css_class("pulse-on")
        return True

    # ---- tema --------------------------------------------------------------
    def _install_css(self):
        display = Gdk.Display.get_default()
        if self._provider is not None:
            Gtk.StyleContext.remove_provider_for_display(display, self._provider)
        self._provider = Gtk.CssProvider()
        self._provider.load_from_data(build_css(self.colors).encode())
        Gtk.StyleContext.add_provider_for_display(
            display, self._provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def _reload_theme(self, *_):
        self.colors = load_colors()
        self._install_css()
        return GLib.SOURCE_CONTINUE

    def _start_theme_watch(self):
        try:
            target = Gio.File.new_for_path(str(Path.home() / ".config/omarchy/current"))
            self._theme_monitor = target.monitor_directory(Gio.FileMonitorFlags.NONE, None)
            self._theme_monitor.connect("changed", lambda *_: self._reload_theme())
        except Exception:
            pass


def main():
    LukaOverlay().run(None)


if __name__ == "__main__":
    main()

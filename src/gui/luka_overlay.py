#!/usr/bin/env python3
"""Luka overlay — GUI nativa del asistente para Omarchy/Hyprland.

Superficie *layer-shell* (como waybar/mako/walker), NO una ventana:
  - anclada abajo-centro (nunca centrada en pantalla, nunca robando foco),
  - visible en todos los workspaces (capa overlay; sigue al cambiar de escritorio),
  - reposo = punto mínimo; al activarse crece hacia arriba en píldora,
  - se auto-tema con la paleta del tema activo de Omarchy y reacciona a cambios.

Refleja el estado real del asistente suscribiéndose al canal SSE `/status/events`.

IMPORTANTE: se ejecuta con el PYTHON DEL SISTEMA (tiene PyGObject), no con el
venv de inferencia del proyecto:

    /usr/bin/python src/gui/luka_overlay.py

Variables de entorno opcionales (para pruebas / despliegues no estándar):
    LUKA_API_URL    p.ej. https://127.0.0.1:8765  (si no, se deduce del .env)
    LUKA_API_TOKEN  token Bearer (si no, se lee API_TOKEN del .env)

Recarga de tema en caliente: además del vigilante de fichero, responde a SIGUSR1.
Para integrarlo con `omarchy theme set`, basta un hook que mande la señal:
    ~/.config/omarchy/hooks/theme-set  ->  pkill -USR1 -f luka_overlay.py
"""
from __future__ import annotations

import ctypes

# CRÍTICO: cargar gtk4-layer-shell con RTLD_GLOBAL ANTES de que GTK conecte con
# Wayland; si no, interpone sus símbolos tarde y la superficie sale como ventana
# normal (se quedaría atrapada en un workspace en vez de vivir en la capa overlay).
ctypes.CDLL("libgtk4-layer-shell.so.0", mode=ctypes.RTLD_GLOBAL)

import json
import os
import signal
import ssl
import threading
import time
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

# Paleta de respaldo (Catppuccin Mocha) si no hay tema de Omarchy.
FALLBACK = {
    "background": "#1e1e2e",
    "foreground": "#cdd6f4",
    "accent": "#89b4fa",
    "color1": "#f38ba8",  # rojo   -> desconectado
    "color2": "#a6e3a1",  # verde  -> escuchando
    "color3": "#f9e2af",  # ámbar  -> pensando
    "color4": "#89b4fa",  # azul   -> hablando
    "color8": "#45475a",  # gris   -> reposo
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
    """Lee el .env de la raíz del proyecto (parser mínimo, sin dependencias)."""
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
    """Devuelve (base_url, token) deduciendo scheme/puerto del .env."""
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
    except FileNotFoundError:
        pass
    except Exception:  # pragma: no cover - defensivo
        pass
    return colors


def build_css(colors: dict) -> str:
    return f"""
@define-color bg       {colors['background']};
@define-color fg       {colors['foreground']};
@define-color accent   {colors['accent']};
@define-color c_idle   {colors['color8']};
@define-color c_listen {colors['color2']};
@define-color c_think  {colors['color3']};
@define-color c_speak  {colors['color4']};
@define-color c_off    {colors['color1']};

window {{ background-color: transparent; }}

.luka {{
    background-color: alpha(@bg, 0.92);
    border: 1px solid alpha(@accent, 0.35);
    border-radius: 22px;
    padding: 9px 16px;
    box-shadow: 0 8px 28px alpha(black, 0.45);
    transition: padding 180ms ease, background-color 220ms ease,
                border-radius 180ms ease, border-color 220ms ease;
}}
.luka.state-idle, .luka.state-offline {{
    padding: 6px;
    border-radius: 999px;
    background-color: alpha(@bg, 0.55);
    border-color: alpha(@accent, 0.18);
    box-shadow: none;
}}
.luka.state-offline {{ border-color: alpha(@c_off, 0.25); }}

.dot {{
    min-width: 13px;
    min-height: 13px;
    border-radius: 999px;
    background-color: @c_idle;
    transition: background-color 220ms ease, opacity 650ms ease;
}}
.luka.state-listening .dot {{ background-color: @c_listen; }}
.luka.state-thinking  .dot {{ background-color: @c_think; }}
.luka.state-speaking  .dot {{ background-color: @c_speak; }}
.luka.state-offline   .dot {{ background-color: @c_off; opacity: 0.55; }}
.dot.pulse-on {{ opacity: 0.30; }}

.luka label {{
    color: @fg;
    font-size: 13px;
    font-weight: 600;
    margin-left: 9px;
}}
"""


def derive_state(status: dict) -> str:
    """Mapea el dict de /status al estado visual.

    Degrada con elegancia: si el backend aún no expone `recording`, no habrá
    estado "listening" y `processing` cae a "thinking" (sigue siendo útil).
    """
    if not status.get("engine_connected", True):
        return "offline"
    if status.get("speaking"):
        return "speaking"
    if status.get("recording"):
        return "listening"
    if status.get("processing"):
        return "thinking"
    return "idle"


def state_label(state: str, status: dict) -> str:
    if state == "thinking":
        txt = (status.get("last_user_transcription") or "").strip()
        if txt:
            return txt if len(txt) <= 60 else txt[:57] + "…"
    return STATES[state][1]


class SSEClient(threading.Thread):
    """Suscripción a /status/events en hilo aparte; marshalla al loop GTK."""

    def __init__(self, base_url: str, token: str, on_status, on_offline):
        super().__init__(daemon=True)
        self.url = f"{base_url}/status/events"
        self.token = token
        self.on_status = on_status
        self.on_offline = on_offline
        self._stop = threading.Event()
        # cert autofirmado en localhost: no verificamos.
        self._ssl_ctx = ssl.create_default_context()
        self._ssl_ctx.check_hostname = False
        self._ssl_ctx.verify_mode = ssl.CERT_NONE

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            try:
                req = urllib.request.Request(self.url)
                if self.token:
                    req.add_header("Authorization", f"Bearer {self.token}")
                req.add_header("Accept", "text/event-stream")
                with urllib.request.urlopen(req, context=self._ssl_ctx) as resp:
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
                # servidor caído / reinicio: marca offline y reintenta.
                GLib.idle_add(self.on_offline)
                if self._stop.wait(2.0):
                    return


class LukaOverlay(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.asistenteia.luka")
        self.colors = load_colors()
        self.base_url, self.token = resolve_api()
        self._provider: Gtk.CssProvider | None = None
        self._state = "offline"
        self._pulse = False
        self._pulse_on = False
        self._sse: SSEClient | None = None
        self._theme_monitor: Gio.FileMonitor | None = None

    # ---- ciclo de vida -----------------------------------------------------
    def do_activate(self):
        win = Gtk.ApplicationWindow(application=self)
        win.set_default_size(1, 1)

        LayerShell.init_for_window(win)
        LayerShell.set_namespace(win, "asistenteia")
        LayerShell.set_layer(win, LayerShell.Layer.OVERLAY)
        LayerShell.set_keyboard_mode(win, LayerShell.KeyboardMode.NONE)
        LayerShell.set_anchor(win, LayerShell.Edge.BOTTOM, True)
        LayerShell.set_margin(win, LayerShell.Edge.BOTTOM, 28)
        LayerShell.set_exclusive_zone(win, 0)

        self.pill = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.pill.add_css_class("luka")
        self.pill.set_halign(Gtk.Align.CENTER)
        self.pill.set_valign(Gtk.Align.END)

        self.dot = Gtk.Box()
        self.dot.add_css_class("dot")
        self.dot.set_valign(Gtk.Align.CENTER)
        self.pill.append(self.dot)

        self.revealer = Gtk.Revealer()
        self.revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_RIGHT)
        self.revealer.set_transition_duration(200)
        self.label = Gtk.Label(label="")
        self.revealer.set_child(self.label)
        self.pill.append(self.revealer)

        win.set_child(self.pill)
        self._install_css()
        win.present()

        self._set_state("offline", {})
        GLib.timeout_add(700, self._tick_pulse)
        self._start_theme_watch()

        # mantener viva la app aunque la ventana se oculte
        self.hold()

        self._sse = SSEClient(self.base_url, self.token,
                              self._on_status, self._on_offline)
        self._sse.start()

        # señales: SIGUSR1 = recargar tema; TERM/INT = salir limpio
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1,
                             self._reload_theme)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, self._quit)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, self._quit)
        # El overlay está siempre visible: SIGUSR2 (señal "mostrar" del toggle
        # antiguo) se neutraliza para que no termine el proceso por defecto.
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR2,
                             lambda *_: GLib.SOURCE_CONTINUE)

    def _quit(self, *_):
        if self._sse:
            self._sse.stop()
        self.quit()
        return GLib.SOURCE_REMOVE

    # ---- estado ------------------------------------------------------------
    def _on_status(self, status: dict):
        self._set_state(derive_state(status), status)
        return GLib.SOURCE_REMOVE

    def _on_offline(self):
        self._set_state("offline", {})
        return GLib.SOURCE_REMOVE

    def _set_state(self, state: str, status: dict):
        css_class, _, pulses = STATES[state]
        for cls, _, _ in STATES.values():
            self.pill.remove_css_class(cls)
        self.pill.add_css_class(css_class)
        text = state_label(state, status)
        self.label.set_text(text)
        self.revealer.set_reveal_child(bool(text))
        self._state = state
        self._pulse = pulses
        if not pulses:
            self.dot.remove_css_class("pulse-on")

    def _tick_pulse(self) -> bool:
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
        # Vigila el directorio `current` (donde el symlink `theme` se repunta al
        # cambiar de tema con `omarchy theme set`) para recargar la paleta.
        try:
            target = Gio.File.new_for_path(
                str(Path.home() / ".config/omarchy/current"))
            self._theme_monitor = target.monitor_directory(
                Gio.FileMonitorFlags.NONE, None)
            self._theme_monitor.connect(
                "changed", lambda *_: self._reload_theme())
        except Exception:
            pass  # el vigilante es un extra; SIGUSR1 sigue funcionando


def main():
    LukaOverlay().run(None)


if __name__ == "__main__":
    main()

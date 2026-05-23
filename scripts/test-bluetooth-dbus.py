#!/usr/bin/env python3
"""
Script de prueba para monitorear señales D-Bus de BlueZ relacionadas
con el dispositivo HOME SPA-133.

BlueZ no siempre expone señales directas de botón AVRCP por D-Bus
(los eventos suelen ir por evdev / input), pero este script escucha:
  - PropertiesChanged de org.bluez.Device1 (conexión, batería, etc.)
  - PropertiesChanged de org.bluez.MediaControl1 (si existe)
  - Señales del sistema media player (mpris-proxy, etc.)

Uso:
    python3 scripts/test-bluetooth-dbus.py

Requiere: dbus-python o pydbus (usa dbus-send como fallback si no hay librería)
"""

import sys
import subprocess
import signal

DEVICE_MAC = "3D:E8:0D:AF:A7:BD"
DBUS_PATH = "/org/bluez/hci0/dev_3D_E8_0D_AF_A7_BD"


def listen_dbus_monitor():
    """Usa dbus-monitor para escuchar señales del sistema relacionadas con BlueZ."""
    print("=" * 60)
    print("  TEST: Monitoreo D-Bus BlueZ para HOME SPA-133")
    print("=" * 60)
    print(f"\nDispositivo: {DEVICE_MAC}")
    print("Escuchando señales de org.bluez en el bus de sistema...")
    print("Pulsa el botón Play en el dispositivo para ver si aparece algo.")
    print("Presiona Ctrl+C para salir.\n")

    # dbus-monitor filtra solo bluez y el path del dispositivo
    cmd = [
        "dbus-monitor",
        "--system",
        f"type='signal',sender='org.bluez',path='{DBUS_PATH}',interface='org.freedesktop.DBus.Properties',member='PropertiesChanged'"
    ]

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for line in proc.stdout:
            print(line, end="")
    except KeyboardInterrupt:
        print("\n🛑 Detenido.")
        proc.terminate()
    except FileNotFoundError:
        print("❌ dbus-monitor no encontrado. Instalar paquete 'dbus'.")
        sys.exit(1)


def inspect_media_interfaces():
    """Muestra el estado actual de las interfaces media del dispositivo."""
    print("\n--- Estado actual de interfaces media ---")
    interfaces = [
        ("org.bluez.MediaControl1", "Connected"),
        ("org.bluez.MediaControl1", "Player"),
        ("org.bluez.Device1", "Connected"),
        ("org.bluez.Device1", "UUIDs"),
    ]

    for iface, prop in interfaces:
        cmd = [
            "dbus-send", "--system", "--print-reply",
            "--dest=org.bluez", DBUS_PATH,
            "org.freedesktop.DBus.Properties.Get",
            f"string:{iface}", f"string:{prop}"
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            if result.returncode == 0:
                print(f"  {iface}.{prop}: {result.stdout.strip().split()[-1] if result.stdout.strip() else 'N/A'}")
            else:
                print(f"  {iface}.{prop}: (no disponible)")
        except Exception as e:
            print(f"  {iface}.{prop}: error ({e})")


def main():
    inspect_media_interfaces()
    print()
    listen_dbus_monitor()


if __name__ == "__main__":
    main()

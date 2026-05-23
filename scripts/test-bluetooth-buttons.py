#!/usr/bin/env python3
"""
Script de prueba interactivo para capturar botones Bluetooth HOME SPA-133.

Uso:
    source venv/bin/activate
    python3 scripts/test-bluetooth-buttons.py

Detiene automáticamente tras 30 segundos o con Ctrl+C.
"""

import asyncio
import sys

# Asegurar que src/ esté en el path
sys.path.insert(0, "/home/victor/develop/asistenteia")

from src.bt_button_listener import BtButtonListener


def on_play():
    print("🎵 >>> PLAY detectado")


def on_pause():
    print("⏸️  >>> PAUSE detectado")


def on_next():
    print("⏭️  >>> NEXT detectado")


def on_previous():
    print("⏮️  >>> PREVIOUS detectado")


def on_stop():
    print("🛑 >>> STOP detectado")


def on_volume_up():
    print("🔊 >>> VOLUME UP detectado")


def on_volume_down():
    print("🔉 >>> VOLUME DOWN detectado")


def on_unknown(name, code):
    print(f"❓ >>> Botón desconocido: {name} (code={code})")


async def main():
    print("=" * 60)
    print("  TEST INTERACTIVO: Botones Bluetooth HOME SPA-133")
    print("=" * 60)
    print("\nInstrucciones:")
    print("  1. Pulsa el botón Play/Pause/Next/etc. en el HOME SPA-133")
    print("  2. Verás la acción detectada en esta terminal")
    print("  3. El test finaliza automáticamente en 30 segundos")
    print("  4. O presiona Ctrl+C para salir antes\n")

    listener = BtButtonListener()
    listener.on_play = on_play
    listener.on_pause = on_pause
    listener.on_next = on_next
    listener.on_previous = on_previous
    listener.on_stop = on_stop
    listener.on_volume_up = on_volume_up
    listener.on_volume_down = on_volume_down
    listener.on_unknown = on_unknown

    try:
        await listener.start()
        print(f"✅ Escuchando dispositivo: {listener.device_name}")
        print("   Esperando eventos...\n")

        # Esperar 30 segundos o hasta interrupción
        await asyncio.sleep(30)

    except KeyboardInterrupt:
        print("\n\n🛑 Interrumpido por el usuario.")
    except Exception as e:
        print(f"\n❌ Error: {e}")
    finally:
        await listener.stop()
        print("\n🏁 Test finalizado.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass  # Silenciar traceback de SIGINT/timeout

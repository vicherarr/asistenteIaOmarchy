#!/usr/bin/env bash
# =============================================================================
# diagnose-bt-button.sh - Diagnóstico completo del botón Bluetooth AVRCP
# =============================================================================
# Este script monitorea simultáneamente:
#   1. Señales MPRIS en el bus de sesión (donde debería llegar al dummy player)
#   2. Señales BlueZ en el bus de sistema
#   3. Eventos evdev directos (por si BlueZ sí genera eventos)
#   4. Estado de mpris-proxy y playerctld
#
# Uso:
#   ./scripts/diagnose-bt-button.sh
#   Luego pulsa el botón Play del HOME SPA-133
#   Presiona Ctrl+C para salir
# =============================================================================

echo "=========================================="
echo "  DIAGNÓSTICO: Botón Bluetooth AVRCP"
echo "=========================================="
echo ""
echo "Estado previo:"
echo "---------------"

# 1. Verificar servicio asistenteia
if systemctl --user is-active --quiet asistenteia.service; then
    echo "✅ asistenteia.service: ACTIVO"
else
    echo "❌ asistenteia.service: INACTIVO"
fi

# 2. Verificar mpris-proxy
if pgrep -x mpris-proxy > /dev/null; then
    echo "✅ mpris-proxy: CORRIENDO (PID $(pgrep -x mpris-proxy))"
else
    echo "❌ mpris-proxy: NO CORRIENDO"
fi

# 3. Verificar playerctld
if pgrep -x playerctld > /dev/null; then
    echo "✅ playerctld: CORRIENDO"
else
    echo "❌ playerctld: NO CORRIENDO"
fi

# 4. Verificar dummy player MPRIS
if dbus-send --session --print-reply --dest=org.freedesktop.DBus /org/freedesktop/DBus org.freedesktop.DBus.ListNames 2>/dev/null | grep -q "asistenteia"; then
    echo "✅ Dummy MPRIS Player: REGISTRADO en D-Bus"
else
    echo "❌ Dummy MPRIS Player: NO REGISTRADO"
fi

# 5. Verificar dispositivo evdev
if [ -e "/dev/input/event20" ]; then
    echo "✅ Dispositivo evdev: /dev/input/event20 existe"
else
    echo "❌ Dispositivo evdev: NO ENCONTRADO"
fi

echo ""
echo "Instrucciones:"
echo "--------------"
echo "1. Pulsa el botón PLAY del HOME SPA-133"
echo "2. Espera unos segundos"
echo "3. Presiona Ctrl+C para salir y ver resultados"
echo ""
echo "Escuchando... (Ctrl+C para salir)"
echo ""

# Función para limpiar al salir
cleanup() {
    echo ""
    echo "=========================================="
    echo "  Limpieza y resumen"
    echo "=========================================="
    
    # Restaurar playerctld si lo detuvimos
    if [ -n "$PLAYERCTLD_WAS_RUNNING" ]; then
        echo "Restaurando playerctld..."
        /usr/bin/playerctld &
    fi
    
    exit 0
}
trap cleanup INT TERM

# Guardar estado de playerctld
if pgrep -x playerctld > /dev/null; then
    PLAYERCTLD_WAS_RUNNING=1
    echo "🧪 Deteniendo playerctld temporalmente para que mpris-proxy hable directamente con nuestro dummy player..."
    killall playerctld 2>/dev/null
    sleep 1
else
    PLAYERCTLD_WAS_RUNNING=""
fi

# Monitorear MPRIS de nuestro dummy player en background
echo "📡 Monitoreando señales MPRIS del dummy player..."
dbus-monitor --session "type='signal',interface='org.mpris.MediaPlayer2.Player'" &
DBUS_PID=$!

# Monitorear BlueZ MediaControl1 en background
echo "📡 Monitoreando señales BlueZ MediaControl1..."
dbus-monitor --system "path='/org/bluez/hci0/dev_3D_E8_0D_AF_A7_BD',interface='org.freedesktop.DBus.Properties',member='PropertiesChanged'" &
DBUS_SYS_PID=$!

# Intentar leer evdev directamente en background (por si acaso)
if [ -e "/dev/input/event20" ]; then
    echo "📡 Monitoreando evdev /dev/input/event20..."
    ( python3 -c "
import struct
with open('/dev/input/event20', 'rb') as f:
    fmt = 'llHHi'
    size = struct.calcsize(fmt)
    while True:
        data = f.read(size)
        if data:
            _, _, t, c, v = struct.unpack(fmt, data)
            if t == 1:  # EV_KEY
                print(f'   [evdev] type=EV_KEY code={c} value={v}')
    " 2>/dev/null ) &
    EVDEV_PID=$!
fi

# Mantener el script corriendo hasta Ctrl+C
sleep 30

# Si llegamos aquí, fue por timeout
kill $DBUS_PID $DBUS_SYS_PID 2>/dev/null
kill $EVDEV_PID 2>/dev/null
wait $DBUS_PID $DBUS_SYS_PID $EVDEV_PID 2>/dev/null

cleanup

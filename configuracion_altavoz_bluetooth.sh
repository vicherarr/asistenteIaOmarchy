#!/bin/bash
# ==============================================================================
# CONFIGURACIÓN AUTOMÁTICA DE ALTAVOZ BLUETOOTH "HOME SPA-133" EN MODO HEADSET (MSBC)
# ==============================================================================
# Este script restaura la configuración completa de PipeWire, WirePlumber y el
# servicio systemd de usuario para forzar que el altavoz HOME SPA-133 se conecte
# siempre en modo Headset (manos libres) utilizando el códec MSBC (micrófono activo).
#
# Creado para: Víctor (CachyOS / Hyprland / Omarchy)
# Fecha de creación: 23 de mayo de 2026
# ==============================================================================

# Variables de configuración del dispositivo Bluetooth específico
MAC_ADDR="3D:E8:0D:AF:A7:BD"
DEVICE_NAME="HOME SPA-133"
CARD_NAME="bluez_card.3D_E8_0D_AF_A7_BD"

# Directorios de destino
WP_CONF_DIR="$HOME/.config/wireplumber/wireplumber.conf.d"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"

echo "======================================================================"
echo "Instalando configuración de Bluetooth Headset para: $DEVICE_NAME"
echo "======================================================================"

# 1. Crear directorios si no existen
mkdir -p "$WP_CONF_DIR"
mkdir -p "$SYSTEMD_USER_DIR"

# 2. Crear archivo de reglas de WirePlumber
# Esto deshabilita A2DP para este dispositivo a nivel de WirePlumber y fuerza el perfil manos libres.
echo "[1/3] Creando regla de WirePlumber en: $WP_CONF_DIR/99-home-spa-defaults.conf"
cat << 'EOF' > "$WP_CONF_DIR/99-home-spa-defaults.conf"
# Reglas de WirePlumber para el altavoz HOME SPA-133
# Configura el dispositivo para que funcione por defecto en modo Headset Head Unit (HSP/HFP, códec mSBC) al conectarse.

monitor.bluez.rules = [
  {
    matches = [
      {
        # Coincide específicamente con la tarjeta de sonido de tu altavoz "HOME SPA-133"
        device.name = "bluez_card.3D_E8_0D_AF_A7_BD"
      }
    ]
    actions = {
      update-props = {
        # Fuerza que el perfil predeterminado sea Headset Head Unit (HSP/HFP con mSBC)
        device.profile = "headset-head-unit"
        bluez5.profile = "headset-head-unit"
        
        # Desactiva por completo todos los códecs A2DP para evitar este perfil
        bluez5.codecs = [ ]
        
        # Conecta de forma automática solo los perfiles de Headset (HSP/HFP) para evitar A2DP
        bluez5.auto-connect = [ "hsp_hs" "hfp_hf" ]
        
        # Habilita únicamente los roles de Headset en este dispositivo
        bluez5.roles = [ "hsp_hs" "hfp_hf" ]
      }
    }
  }
]
EOF

# 3. Crear el script en segundo plano para interceptar y corregir la carrera de conexión
echo "[2/3] Creando script daemon en: $HOME/.config/wireplumber/bluetooth-homespa.sh"
cat << 'EOF' > "$HOME/.config/wireplumber/bluetooth-homespa.sh"
#!/bin/bash
# Script para forzar de forma activa el perfil manos libres (MSBC) al conectar.

MAC_ADDR="3D:E8:0D:AF:A7:BD"
CARD_NAME="bluez_card.${MAC_ADDR//:/_}"
SINK_NAME="bluez_output.${MAC_ADDR//:/_}.1"

force_profile() {
  if pactl list cards | grep -q "Name: $CARD_NAME"; then
    ACTIVE_PROFILE=$(pactl list cards | grep -A 35 "Name: $CARD_NAME" | grep "Active Profile" | cut -d' ' -f3)
    if [ "$ACTIVE_PROFILE" != "headset-head-unit" ]; then
      pactl set-card-profile "$CARD_NAME" headset-head-unit
    fi
    # Si el volumen está a 0% o silenciado por defecto de perfil, forzar a un 80% saludable
    if pactl list sinks | grep -q "Name: $SINK_NAME"; then
      VOLUME=$(pactl list sinks | grep -A 15 "Name: $SINK_NAME" | grep "Volume:" | head -n1 | awk '{print $5}' | tr -d '%')
      if [ -n "$VOLUME" ] && [ "$VOLUME" -eq 0 ]; then
        pactl set-sink-volume "$SINK_NAME" 80%
      fi
    fi
  fi
}

# Comprobación inicial al arrancar
force_profile

# Escuchar eventos de PipeWire y reaccionar al instante
pactl subscribe | while read -r line; do
  if echo "$line" | grep -q -E "card|sink|source"; then
    force_profile
  fi
done
EOF

# Hacer ejecutable el script interno
chmod +x "$HOME/.config/wireplumber/bluetooth-homespa.sh"

# 4. Crear el servicio systemd de usuario
echo "[3/3] Creando servicio systemd en: $SYSTEMD_USER_DIR/bluetooth-homespa.service"
cat << EOF > "$SYSTEMD_USER_DIR/bluetooth-homespa.service"
[Unit]
Description=Auto-force HOME SPA-133 Bluetooth Headset MSBC Profile
After=pipewire.service wireplumber.service pipewire-pulse.service
BindsTo=pipewire.service wireplumber.service pipewire-pulse.service

[Service]
ExecStart=$HOME/.config/wireplumber/bluetooth-homespa.sh
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

# 5. Recargar systemd de usuario y activar el servicio
echo "----------------------------------------------------------------------"
echo "Activando servicio..."
systemctl --user daemon-reload
systemctl --user enable --now bluetooth-homespa.service

echo "======================================================================"
echo "¡CONFIGURACIÓN COMPLETADA!"
echo "Tu altavoz $DEVICE_NAME ($MAC_ADDR) ahora está blindado."
echo "Siempre se conectará en modo Headset (MSBC) automáticamente."
echo "======================================================================"

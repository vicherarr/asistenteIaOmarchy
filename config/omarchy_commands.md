## COMANDOS DE OMARCHI/HYPRLAND DISPONIBLES

El asistente puede ejecutar estos comandos para controlar el sistema:

### Lanzar Aplicaciones
- `omarchy launch <app>` - Lanzar cualquier aplicación instalada
- `omarchy search <query>` - Buscar aplicaciones o archivos

### Control de Ventanas (Hyprland)
- `hyprctl dispatch exec <command>` - Ejecutar cualquier comando
- `hyprctl dispatch focuswindow <class>` - Enfocar una ventana por clase
- `hyprctl dispatch movetoworkspace <n>` - Mover ventana a workspace
- `hyprctl dispatch togglefloating` - Alternar ventana flotante

### Control de Audio (PipeWire)
- `wpctl set-mute @DEFAULT_AUDIO_SINK@ toggle` - Silenciar/activar salida
- `wpctl set-volume @DEFAULT_AUDIO_SINK@ 50%` - Ajustar volumen (0-100%)
- `wpctl set-mute @DEFAULT_AUDIO_SOURCE@ toggle` - Silenciar micrófono
- `wpctl set-default <node-id>` - Cambiar dispositivo por defecto

### Control de Música (Playerctl)
- `playerctl play-pause` - Pausar/reproducir
- `playerctl next` - Siguiente pista
- `playerctl previous` - Pista anterior
- `playerctl stop` - Detener
- `playerctl metadata title` - Obtener título actual

### Navegador
- `chromium <url>` - Abrir URL en Chromium
- `chromium` - Abrir navegador

### Notificaciones
- `notify-send "título" "mensaje"` - Enviar notificación al escritorio

### Capturas de Pantalla
- `grim` - Captura de pantalla completa
- `grim -g "$(slurp)"` - Captura de región seleccionada

### Sistema
- `systemctl --user restart pipewire` - Reiniciar PipeWire
- `bluetoothctl connect <mac>` - Conectar dispositivo Bluetooth

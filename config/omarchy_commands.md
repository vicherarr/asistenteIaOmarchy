## REFERENCIA RÁPIDA DE COMANDOS DEL SISTEMA

### Lanzar Aplicaciones
- `omarchy launch <app>` — Lanzar app (firefox, spotify, alacritty, etc.)
- `omarchy search <query>` — Buscar apps/archivos

### Ventanas (Hyprland)
- `hyprctl dispatch exec <cmd>` — Ejecutar comando
- `hyprctl dispatch focuswindow <class>` — Enfocar ventana por clase (ej: class=firefox)
- `hyprctl dispatch movetoworkspace <n>` — Mover ventana a workspace
- `hyprctl dispatch togglefloating` — Toggle flotante
- `hyprctl dispatch fullscreen` — Toggle fullscreen
- `hyprctl dispatch closewindow <class>` — Cerrar ventana por clase

### Audio (PipeWire / wpctl)
- `wpctl set-mute @DEFAULT_AUDIO_SINK@ toggle` — Mute/unmute salida
- `wpctl set-volume @DEFAULT_AUDIO_SINK@ 50%` — Volumen salida (0-100%)
- `wpctl set-mute @DEFAULT_AUDIO_SOURCE@ toggle` — Mute/unmute micrófono
- `wpctl set-default <node-id>` — Cambiar dispositivo por defecto
- `wpctl status` — Listar dispositivos y nodos

### Música (Playerctl / MPRIS)
- `playerctl play-pause` — Play/pause
- `playerctl next` — Siguiente
- `playerctl previous` — Anterior
- `playerctl stop` — Detener
- `playerctl metadata title` — Título actual
- `playerctl --list-all` — Listar reproductores activos

### Navegador
- `chromium <url>` — Abrir URL
- `chromium` — Abrir navegador vacío

### Notificaciones
- `notify-send "título" "mensaje"` — Notificación desktop

### Capturas (Wayland)
- `grim` — Pantalla completa
- `grim -g "$(slurp)"` — Región seleccionada

### Sistema
- `systemctl --user restart pipewire` — Reiniciar PipeWire
- `systemctl --user restart asistenteia.service` — Reiniciar asistente
- `bluetoothctl connect <mac>` — Conectar BT
- `bluetoothctl info <mac>` — Info dispositivo BT

### Terminal TMUX (sesión "asistenteia")
- El asistente usa tmux para persistencia. Comandos se envían vía `open_terminal_and_run_command`.
- La terminal es visible en pantalla para que el usuario supervise.

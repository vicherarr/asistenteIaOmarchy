#!/usr/bin/env bash
set -euo pipefail

echo "Iniciando AsistenteIA (servicio)..."

if ! systemctl --user is-enabled asistenteia.service &>/dev/null; then
    echo "ERROR: Servicio no instalado. Ejecutar installservice.sh primero"
    exit 1
fi

# Asegurar que Ollama esté corriendo antes de iniciar
if command -v ollama &>/dev/null; then
    if ! curl -s http://localhost:11434/api/tags &>/dev/null; then
        echo "Ollama no está corriendo. Iniciándolo..."
        systemctl --user start ollama.service 2>/dev/null || ollama serve &>/dev/null &
        echo "Esperando a que Ollama esté listo..."
        for i in $(seq 1 60); do
            if curl -s http://localhost:11434/api/tags &>/dev/null; then
                echo "Ollama listo."
                break
            fi
            if [ "$i" -eq 60 ]; then
                echo "ERROR: Ollama no respondió en 2 minutos"
                exit 1
            fi
            sleep 2
        done
    else
        echo "Ollama ya está corriendo."
    fi

    # Verificar modelo LLM
    if ! ollama list 2>/dev/null | grep -q "gemma4"; then
        echo "Modelo gemma4:e2b no encontrado. Descargándolo (puede tardar varios minutos)..."
        ollama pull gemma4:e2b
    else
        echo "Modelo gemma4:e2b disponible."
    fi

    # Precargar modelo en memoria (en background, no bloquea el inicio)
    echo "Precargando modelo gemma4:e2b en memoria..."
    curl -s http://localhost:11434/api/chat -d '{"model":"gemma4:e2b","messages":[{"role":"user","content":"hola"}],"stream":false}' &>/dev/null &
fi

systemctl --user start asistenteia.service
sleep 3

if systemctl --user is-active asistenteia.service &>/dev/null; then
    echo "AsistenteIA iniciado correctamente"
    systemctl --user status asistenteia.service --no-pager
else
    echo "ERROR: El servicio no se inició correctamente"
    echo "Ver logs: journalctl --user -u asistenteia --no-pager -n 20"
    exit 1
fi
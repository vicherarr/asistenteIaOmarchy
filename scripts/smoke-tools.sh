#!/usr/bin/env bash
# Smoke test del tool calling, un turno por proceso.
#
# El motor LiteRT segfaultea al encadenar varios turnos en el mismo proceso. No es un
# problema nuevo —el servicio lleva cayéndose así desde antes (varios SIGSEGV el 6 de
# agosto de 2026)—, pero significa que medir varios turnos seguidos es imposible dentro
# de un único proceso: el primero pasa y el segundo se lleva el script por delante.
#
# Aquí cada turno se lanza aparte, así que una caída solo cuesta ESE turno y además
# queda contada como fallo en vez de dejar la medición a medias. El precio es cargar el
# modelo en cada turno (~40s), que para una verificación puntual sale a cuenta.
#
# Para y rearranca el servicio (necesita la VRAM). El arranque va con `;` para que tu
# asistente vuelva pase lo que pase.
#
# Uso:  scripts/smoke-tools.sh [repeticiones_por_caso]   (por defecto 3)

set -u

cd "$(dirname "$0")/.." || exit 1

REPES="${1:-3}"
PY=venv/bin/python
INFORME=/tmp/asistenteia-smoke.txt

N_CASOS=$("$PY" scripts/smoke-tools.py --listar) || { echo "No pude listar los casos"; exit 1; }

: > "$INFORME"
echo "Smoke test: $N_CASOS casos x $REPES intentos, un proceso por turno." | tee -a "$INFORME"

systemctl --user stop asistenteia.service

ok=0
ko=0
for ((caso = 0; caso < N_CASOS; caso++)); do
    for ((intento = 1; intento <= REPES; intento++)); do
        "$PY" scripts/smoke-tools.py --caso "$caso" --intento "$intento/$REPES" 2>/dev/null
        estado=$?
        if [[ $estado -eq 0 ]]; then
            ok=$((ok + 1))
        else
            ko=$((ko + 1))
            # 139 = 128+11 = SIGSEGV: la caída del motor, no un fallo de tool calling.
            if [[ $estado -eq 139 ]]; then
                echo "  💥 caso $caso intento $intento/$REPES · SEGFAULT del motor LiteRT" | tee -a "$INFORME"
            fi
        fi
    done
done

systemctl --user start asistenteia.service

{
    echo
    echo "=================================================="
    echo "Intentos correctos: $ok · con problemas: $ko"
    if [[ $ko -eq 0 ]]; then
        echo "TOOL CALLING OK"
    else
        echo "TOOL CALLING CON FALLOS"
    fi
} | tee -a "$INFORME"

[[ $ko -eq 0 ]]

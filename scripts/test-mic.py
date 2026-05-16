#!/usr/bin/env python3
"""Prueba de escucha: graba audio del micrófono BT y lo envía al asistente."""

import subprocess
import sys
import tempfile
import time
from pathlib import Path

BT_SOURCE = "bluez_input.84:D3:52:8E:96:31"
ORCHESTRATOR = "http://localhost:8765/transcribe"


def grabar_audio(segundos: int = 5) -> str:
    """Graba audio del micrófono Bluetooth usando pw-record."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        wav_path = f.name

    print(f"🎤 Grabando {segundos} segundos del micrófono BT...")
    print("   Habla ahora...")

    cmd = [
        "pw-record",
        "--target", BT_SOURCE,
        "--rate", "48000",
        "--channels", "1",
        wav_path,
    ]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(segundos)
    proc.terminate()
    proc.wait(timeout=5)

    if not Path(wav_path).exists() or Path(wav_path).stat().st_size == 0:
        print(f"❌ Error grabando: archivo vacío o no creado")
        return ""

    size = Path(wav_path).stat().st_size
    print(f"✅ Audio guardado: {wav_path} ({size} bytes)")
    return wav_path


def transcribir_audio(wav_path: str) -> str:
    """Transcribe audio usando whisper-cli."""
    if not Path(wav_path).exists():
        return ""

    print("🔄 Transcribiendo audio con whisper.cpp...")

    model_path = str(Path.home() / ".cache" / "whisper" / "ggml-base.bin")
    if not Path(model_path).exists():
        print(f"   ❌ Modelo no encontrado: {model_path}")
        return ""

    try:
        result = subprocess.run(
            ["whisper-cli", "--model", model_path, "--file", wav_path,
             "--language", "es", "--no-timestamps"],
            capture_output=True, text=True, timeout=120,
        )

        # whisper-cli escribe la transcripción en stdout mezclado con logs
        lines = result.stdout.strip().splitlines()
        for line in lines:
            line = line.strip()
            if line and not line.startswith("whisper_") and not line.startswith("main:") and not line.startswith("system_info:"):
                cleaned = line.replace("[", "").replace("]", "").strip()
                if cleaned:
                    print(f"   📝 Transcripción: {cleaned}")
                    return cleaned

    except Exception as e:
        print(f"   ⚠️  Error: {e}")

    return ""


def enviar_asistente(texto: str):
    """Envía texto al asistente y muestra respuesta."""
    if not texto.strip():
        print("❌ Texto vacío")
        return

    print(f"\n📤 Enviando al asistente: \"{texto[:80]}...\"")

    try:
        import httpx
        response = httpx.post(
            ORCHESTRATOR,
            json={"text": texto},
            timeout=120,
        )
        data = response.json()

        print(f"\n🤖 Asistente responde:")
        print(f"   {data['response_text']}")
        print(f"   Comandos ejecutados: {data['commands_executed']}")
        if data.get('audio_file'):
            print(f"   Audio generado: {data['audio_file']}")

    except Exception as e:
        print(f"❌ Error enviando al asistente: {e}")


def main():
    print("=" * 50)
    print("  🎙️  PRUEBA DE ESCUCHA - AsistenteIA")
    print("=" * 50)
    print()

    if len(sys.argv) > 1:
        segundos = int(sys.argv[1])
    else:
        segundos = 5

    wav = grabar_audio(segundos)
    if not wav:
        sys.exit(1)

    texto = transcribir_audio(wav)

    if texto:
        enviar_asistente(texto)
    else:
        print()
        print("💡 Como no hay whisper.cpp, escribe lo que dijiste:")
        print("   (o presiona Enter para usar texto de prueba)")
        entrada = input("   > ").strip()

        if entrada:
            enviar_asistente(entrada)
        else:
            print("\n📝 Usando texto de prueba...")
            enviar_asistente("Hola, qué GPU tengo en mi equipo?")


if __name__ == "__main__":
    main()

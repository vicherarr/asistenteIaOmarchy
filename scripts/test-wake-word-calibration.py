#!/usr/bin/env python3
"""Script de diagnóstico para calibrar el wake word 'hey_mycroft'.

Uso:
    ./scripts/test-wake-word-calibration.py

Captura audio del micrófono y muestra scores de openwakeword en tiempo real.
Muestra una barra de progreso con el score actual, el máximo histórico,
y guarda clips cuando detecta algo por encima del threshold.
"""

import asyncio
import sys
import time
import os
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import settings

# --- CONFIGURACIÓN AJUSTABLE ---
THRESHOLD = 0.3       # Empieza bajo para ver si detecta algo
AMPLIFICATION = 4.0   # Empieza a 4x en vez de 8x
MODEL_NAME = "hey_mycroft"
CHUNK_SAMPLES = 1280
FRAME_BYTES = CHUNK_SAMPLES * 2


def _save_raw_to_wav(raw_bytes, path):
    import wave
    with wave.open(path, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(raw_bytes)


async def test_wake_word_calibration():
    print(f"🎙️  Calibración wake word: {MODEL_NAME}")
    print(f"   Threshold inicial: {THRESHOLD}")
    print(f"   Amplificación: {AMPLIFICATION}x")
    print()
    print("Di 'Hey Mycroft' varias veces...")
    print()

    try:
        from openwakeword.model import Model
        model = Model(wakeword_models=[MODEL_NAME])
        print(f"✅ Modelo '{MODEL_NAME}' cargado.")
    except Exception as e:
        print(f"❌ Error cargando modelo: {e}")
        return

    from src.audio_manager import AudioManager
    am = AudioManager()
    await am.auto_configure_bluetooth()
    source_id = am.default_source
    device = source_id if source_id else "@DEFAULT_SOURCE@"
    print(f"🎧 Dispositivo: {device}")
    print()

    pacat_cmd = ["pacat", "--record", "--rate=16000", "--channels=1", "--format=s16le", "--device", str(device)]
    proc = await asyncio.create_subprocess_exec(
        *pacat_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    frame_count = 0
    detected_clips = 0
    last_detection_time = 0
    cooldown = 2.0

    scores_above_threshold = 0
    max_score_seen = 0.0
    scores_window = []  # últimos 50 scores para media móvil

    async def read_stderr():
        while True:
            try:
                line = await proc.stderr.readline()
                if not line:
                    break
                err = line.decode().strip()
                if err and "warning" not in err.lower():
                    print(f"\n[pacat stderr] {err}")
            except Exception:
                break

    stderr_task = asyncio.create_task(read_stderr())

    try:
        while True:
            try:
                data = await proc.stdout.readexactly(FRAME_BYTES)
            except asyncio.IncompleteReadError as e:
                if len(e.partial) > 0:
                    data = e.partial
                else:
                    break
            except Exception:
                break

            if len(data) < FRAME_BYTES:
                break

            frame_count += 1

            audio_float = np.frombuffer(data, dtype=np.int16).astype(np.float32)
            boosted = np.clip(audio_float * AMPLIFICATION, -32768, 32767).astype(np.int16)

            prediction = model.predict(boosted)
            score = prediction.get(MODEL_NAME, 0.0)

            if score > max_score_seen:
                max_score_seen = score
            if score >= THRESHOLD:
                scores_above_threshold += 1

            scores_window.append(score)
            if len(scores_window) > 50:
                scores_window.pop(0)
            avg_score = sum(scores_window) / len(scores_window) if scores_window else 0.0

            # Mostrar score cada 25 frames (~2s) o cuando hay detección
            show_line = (frame_count % 25 == 0) or (score >= THRESHOLD)

            if score >= THRESHOLD:
                elapsed = time.time() - last_detection_time
                if elapsed >= cooldown:
                    last_detection_time = time.time()
                    rms = np.sqrt(np.mean(audio_float**2))
                    print(f"\n🟢 DETECTADO! score={score:.3f}  rms={rms:.0f}")
                    detected_clips += 1

                    save_path = f"/tmp/wake_word_detected_{detected_clips:02d}.wav"
                    _save_raw_to_wav(data, save_path)
                    print(f"   💾 Audio guardado: {save_path}")
                else:
                    print(f"\n🟡 Score alto={score:.3f} pero en cooldown ({elapsed:.1f}s < {cooldown}s)")

            elif show_line:
                # Barra visual del score (0.0 a 1.0)
                bar_len = 20
                filled = int(score * bar_len)
                bar = "█" * filled + "░" * (bar_len - filled)
                print(
                    f"   {bar}  score={score:.3f}  "
                    f"max={max_score_seen:.3f}  "
                    f"avg50={avg_score:.3f}  "
                    f"rms={np.sqrt(np.mean(audio_float**2)):.0f}    ",
                    end="\r"
                )

            await asyncio.sleep(0.001)

    except KeyboardInterrupt:
        pass
    finally:
        print(f"\n\n{'='*50}")
        print(f"📊 RESUMEN DE CALIBRACIÓN")
        print(f"{'='*50}")
        print(f"   Frames procesados: {frame_count}")
        print(f"   Máximo score visto: {max_score_seen:.3f}")
        print(f"   Veces por encima de threshold ({THRESHOLD}): {scores_above_threshold}")
        print(f"   Detecciones reales: {detected_clips}")
        print()
        print(f"   💡 RECOMENDACIÓN:")
        if max_score_seen < 0.1:
            print(f"   El modelo no detecta NADA. Considera usar 'hey_jarvis' en vez de 'hey_mycroft'")
            print(f"   o entrenar un modelo personalizado con picovoice/openwakeword.")
        elif max_score_seen < 0.3:
            print(f"   Scores muy bajos. Prueba con threshold=0.15 y amplificación=8x")
            print(f"   o considera cambiar de modelo.")
        elif max_score_seen < 0.5:
            print(f"   Parece detectar algo. Prueba threshold={max_score_seen * 0.7:.2f} (70% del máximo)")
        else:
            print(f"   El modelo funciona bien. Usa threshold={THRESHOLD} (o ligeramente inferior)")
        print()

        stderr_task.cancel()
        try:
            await stderr_task
        except asyncio.CancelledError:
            pass

        if proc.returncode is None:
            proc.terminate()
            await proc.wait()


if __name__ == "__main__":
    try:
        asyncio.run(test_wake_word_calibration())
    except KeyboardInterrupt:
        print("\n\n👋 Saliendo.")

"""Módulo de síntesis de voz (TTS).

Usa gTTS (Google Text-to-Speech) como motor principal.
Piper TTS se usa como fallback si está disponible localmente.
"""

import asyncio
import logging
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PIPER_VOICES_DIR = Path.home() / ".local" / "share" / "piper-voices"
DEFAULT_VOICE = "es_ES-mls_10246-low"


class TTSError(Exception):
    """Errores del motor TTS."""
    pass


class TTSEngine:
    """Motor de texto a voz. Prioriza gTTS, fallback a Piper."""

    def __init__(
        self,
        voice_name: str = DEFAULT_VOICE,
        voice_dir: Optional[Path] = None,
        prefer_local: bool = False,
    ) -> None:
        self.voice_name = voice_name
        self.voice_dir = voice_dir or PIPER_VOICES_DIR
        self.prefer_local = prefer_local
        self._default_sink: Optional[str] = None

    def _get_voice_model_path(self) -> Path:
        return self.voice_dir / f"{self.voice_name}.onnx"

    def _get_default_bluetooth_sink(self) -> Optional[str]:
        """Detecta el sink Bluetooth por defecto."""
        try:
            result = subprocess.run(
                ["wpctl", "status"],
                capture_output=True,
                text=True,
                timeout=5,
            )

            in_sinks = False
            for line in result.stdout.splitlines():
                if "Sinks:" in line:
                    in_sinks = True
                    continue
                if in_sinks and "Sources:" in line:
                    break
                if in_sinks and "bluetooth" in line.lower():
                    cleaned = line.replace("│", "").replace("├", "").replace("─", "").strip()
                    parts = cleaned.split()
                    for part in parts:
                        node_id = part.replace(".", "").replace("*", "")
                        if node_id.isdigit():
                            return node_id
        except Exception as e:
            logger.warning(f"Error detectando sink BT: {e}")

        return None

    def _set_audio_sink(self) -> None:
        sink_id = self._get_default_bluetooth_sink()
        if sink_id:
            self._default_sink = sink_id
            logger.info(f"TTS usará sink Bluetooth: {sink_id}")

    def speak(self, text: str) -> Optional[str]:
        """Sintetiza texto a voz y lo reproduce."""
        if not text.strip():
            logger.warning("Texto vacío para TTS")
            return None

        self._set_audio_sink()

        if self.prefer_local and self._get_voice_model_path().exists():
            return self._speak_piper(text)

        return self._speak_gtts(text)

    def _speak_gtts(self, text: str) -> Optional[str]:
        """Sintetiza usando gTTS (Google TTS API)."""
        try:
            from gtts import gTTS
        except ImportError:
            logger.warning("gTTS no disponible, intentando Piper")
            return self._speak_piper(text)

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp_file:
            mp3_path = tmp_file.name

        try:
            tts = gTTS(text=text, lang="es", slow=False)
            tts.save(mp3_path)

            if not Path(mp3_path).exists() or Path(mp3_path).stat().st_size == 0:
                raise TTSError("gTTS generó archivo vacío")

            logger.info(f"gTTS generó audio: {Path(mp3_path).stat().st_size} bytes")
            self._play_audio(mp3_path)
            return mp3_path

        except Exception as e:
            logger.warning(f"gTTS falló: {e}, intentando Piper")
            return self._speak_piper(text)

    def _speak_piper(self, text: str) -> Optional[str]:
        """Sintetiza usando Piper TTS local."""
        model_path = self._get_voice_model_path()
        if not model_path.exists():
            logger.warning("Piper no disponible (modelo no encontrado)")
            raise TTSError("No hay motor TTS disponible (gTTS y Piper fallaron)")

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_file:
            wav_path = tmp_file.name

        try:
            result = subprocess.run(
                ["piper", "--model", str(model_path), "--output_file", wav_path],
                input=text,
                capture_output=True,
                text=True,
                timeout=60,
            )

            if result.returncode != 0:
                raise TTSError(f"Piper falló: {result.stderr}")

            if not Path(wav_path).exists() or Path(wav_path).stat().st_size == 0:
                raise TTSError("Piper generó archivo vacío")

            self._play_audio(wav_path)
            return wav_path

        except FileNotFoundError:
            raise TTSError("piper binario no encontrado")
        except subprocess.TimeoutExpired:
            raise TTSError("Timeout generando audio TTS")

    def _play_audio(self, audio_path: str, speed: float = 1.7) -> None:
        """Reproduce audio al sink Bluetooth con velocidad ajustada."""
        ext = Path(audio_path).suffix.lower()

        try:
            if self._default_sink:
                # Usar ffmpeg para acelerar y enviar al sink BT
                with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                    sped_path = tmp.name

                subprocess.run(
                    ["ffmpeg", "-y", "-i", audio_path, "-af", f"atempo={speed}", sped_path],
                    capture_output=True, timeout=30,
                )

                cmd = ["paplay", "--device", self._default_sink, sped_path]
                subprocess.run(cmd, capture_output=True, timeout=60)

                try:
                    Path(sped_path).unlink()
                except OSError:
                    pass
            else:
                if ext == ".mp3":
                    cmd = ["ffplay", "-nodisp", "-autoexit", "-af", f"atempo={speed}", audio_path]
                else:
                    cmd = ["aplay", audio_path]

                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=60,
                )

                if result.returncode != 0:
                    logger.warning(f"Error reproduciendo: {result.stderr.decode()}")

        except FileNotFoundError:
            logger.warning(f"Reproductor no encontrado para {ext}")
        except Exception as e:
            logger.warning(f"Fallo en reproducción: {e}")

    def speak_async(self, text: str) -> asyncio.Task:
        return asyncio.create_task(self._speak_async(text))

    async def _speak_async(self, text: str) -> Optional[str]:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.speak, text)

    def cleanup_temp_files(self, max_age_seconds: int = 3600) -> int:
        cleaned = 0
        tmp_dir = Path(tempfile.gettempdir())

        for f in tmp_dir.glob("*.mp3"):
            if f.name.startswith("tmp"):
                age = time.time() - f.stat().st_mtime
                if age > max_age_seconds:
                    try:
                        f.unlink()
                        cleaned += 1
                    except OSError:
                        pass

        for f in tmp_dir.glob("*.wav"):
            if f.name.startswith("tmp"):
                age = time.time() - f.stat().st_mtime
                if age > max_age_seconds:
                    try:
                        f.unlink()
                        cleaned += 1
                    except OSError:
                        pass

        return cleaned

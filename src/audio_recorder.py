"""Módulo para la grabación de audio desde el micrófono."""

import asyncio
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class AudioRecorder:
    """Gestiona la captura de audio usando comandos del sistema (parecord)."""

    def __init__(self) -> None:
        self._process: Optional[subprocess.Popen] = None
        self._current_file: Optional[Path] = None

    def start_recording(self, source_id: Optional[str] = None) -> Path:
        """Inicia la grabación en un archivo temporal de forma rápida."""
        if self._process and self._process.poll() is None:
            logger.warning("Ya hay una grabación en curso")
            return self._current_file

        fd, path = tempfile.mkstemp(suffix=".wav", prefix="asistente_rec_")
        import os
        os.close(fd)
        
        self._current_file = Path(path)
        logger.info(f"Iniciando grabación en: {self._current_file} (Dispositivo: {source_id or 'default'})")
        
        # Construir comando parecord
        cmd = ["parecord", "--rate=16000", "--channels=1", "--file-format=wav"]
        if source_id:
            cmd.extend(["--device", source_id])
        cmd.append(str(self._current_file))

        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            logger.error("parecord no encontrado")
            raise RuntimeError("Herramienta 'parecord' no encontrada")

        return self._current_file

    def stop_recording(self) -> Optional[Path]:
        """Detiene la grabación y devuelve la ruta del archivo generado."""
        if not self._process:
            return None

        logger.info("Deteniendo grabación...")
        
        # Enviamos SIGINT para que parecord cierre el archivo correctamente
        self._process.terminate()
        try:
            self._process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._process.kill()
        
        self._process = None
        
        if self._current_file and self._current_file.exists():
            size = self._current_file.stat().st_size
            if size > 44:  # 44 bytes es el header vacío de un WAV
                logger.info(f"Grabación completada: {size} bytes")
                return self._current_file
            else:
                logger.warning("La grabación está vacía")
                self._current_file.unlink(missing_ok=True)
                return None
        
        return None

    @property
    def is_recording(self) -> bool:
        """Indica si hay una grabación activa."""
        return self._process is not None and self._process.poll() is None

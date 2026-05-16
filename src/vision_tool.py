"""Módulo de captura de pantalla para visión del asistente."""

import base64
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class VisionToolError(Exception):
    """Errores del módulo de visión."""
    pass


class VisionTool:
    """Captura la pantalla y la convierte a formato para modelos multimodales."""

    def __init__(self, output_dir: Optional[Path] = None) -> None:
        self.output_dir = output_dir or Path(tempfile.gettempdir())

    def capture_screen(self, output_path: Optional[str] = None) -> str:
        """
        Captura la pantalla completa usando grim.
        Devuelve la ruta del archivo PNG generado.
        """
        if output_path is None:
            tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False, dir=self.output_dir)
            output_path = tmp.name

        try:
            result = subprocess.run(
                ["grim", output_path],
                capture_output=True,
                timeout=10,
            )

            if result.returncode != 0:
                raise VisionToolError(f"grim falló: {result.stderr.decode()}")

            if not Path(output_path).exists() or Path(output_path).stat().st_size == 0:
                raise VisionToolError("Captura de pantalla generó archivo vacío")

            logger.info(f"Pantalla capturada: {Path(output_path).stat().st_size} bytes")
            return output_path

        except FileNotFoundError:
            raise VisionToolError("grim no encontrado. Instalar grim para capturas de pantalla")
        except subprocess.TimeoutExpired:
            raise VisionToolError("Timeout capturando pantalla")

    def capture_region(self, output_path: Optional[str] = None) -> str:
        """
        Captura una región seleccionada por el usuario usando grim + slurp.
        Devuelve la ruta del archivo PNG generado.
        """
        if output_path is None:
            tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False, dir=self.output_dir)
            output_path = tmp.name

        try:
            slurp_result = subprocess.run(
                ["slurp"],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if slurp_result.returncode != 0:
                raise VisionToolError("Selección de región cancelada")

            geometry = slurp_result.stdout.strip()

            grim_result = subprocess.run(
                ["grim", "-g", geometry, output_path],
                capture_output=True,
                timeout=10,
            )

            if grim_result.returncode != 0:
                raise VisionToolError(f"grim falló: {grim_result.stderr.decode()}")

            logger.info(f"Región capturada: {Path(output_path).stat().st_size} bytes")
            return output_path

        except FileNotFoundError:
            raise VisionToolError("slurp no encontrado. Instalar slurp para selección de región")
        except subprocess.TimeoutExpired:
            raise VisionToolError("Timeout seleccionando región")

    @staticmethod
    def image_to_base64(image_path: str) -> str:
        """Convierte una imagen a base64 para enviar a Ollama."""
        try:
            with open(image_path, "rb") as f:
                return base64.b64encode(f.read()).decode()
        except FileNotFoundError:
            raise VisionToolError(f"Imagen no encontrada: {image_path}")
        except Exception as e:
            raise VisionToolError(f"Error convirtiendo imagen a base64: {e}")

    def get_screen_for_vision(self) -> str:
        """
        Flujo completo: captura pantalla y devuelve base64.
        Método principal para usar con modelos multimodales.
        """
        screenshot_path = self.capture_screen()
        try:
            return self.image_to_base64(screenshot_path)
        finally:
            try:
                Path(screenshot_path).unlink()
            except OSError:
                pass

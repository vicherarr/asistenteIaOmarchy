"""Módulo de captura de pantalla para visión del asistente."""

import base64
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from PIL import Image

logger = logging.getLogger(__name__)

MAX_IMAGE_DIMENSION = 800


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
    def _resize_image(image_path: str, max_dim: int = MAX_IMAGE_DIMENSION) -> str:
        """Redimensiona la imagen manteniendo aspect ratio. Devuelve path del temp."""
        img = Image.open(image_path)
        if max(img.size) <= max_dim:
            img.close()
            return image_path

        ratio = max_dim / max(img.size)
        new_size = (int(img.size[0] * ratio), int(img.size[1] * ratio))
        img = img.resize(new_size, Image.Resampling.LANCZOS)

        resized_path = tempfile.mktemp(suffix=".jpg")
        img.save(resized_path, "JPEG", quality=85)
        img.close()

        original_kb = Path(image_path).stat().st_size // 1024
        resized_kb = Path(resized_path).stat().st_size // 1024
        logger.info(f"Imagen redimensionada: {original_kb}KB -> {resized_kb}KB ({img.size[0]}x{img.size[1]})")
        return resized_path

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
        Flujo completo: captura pantalla, redimensiona y devuelve base64.
        Método principal para usar con modelos multimodales.
        """
        screenshot_path = self.capture_screen()
        resized_path = screenshot_path
        try:
            resized_path = self._resize_image(screenshot_path)
            return self.image_to_base64(resized_path)
        finally:
            try:
                Path(screenshot_path).unlink()
            except OSError:
                pass
            if resized_path != screenshot_path:
                try:
                    Path(resized_path).unlink()
                except OSError:
                    pass

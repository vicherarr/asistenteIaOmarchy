"""Captura de pantalla para la visión del asistente.

La inferencia visual NO ocurre aquí: el motor LiteRT no puede llamarse de forma
reentrante desde dentro de un tool. En su lugar, analyze_screen captura la pantalla
y guarda la ruta; AssistantService hace una segunda pasada al modelo con la imagen
adjunta (ver process_transcription_stream).
"""

import asyncio
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from PIL import Image

logger = logging.getLogger(__name__)

MAX_IMAGE_DIMENSION = 800

# Ruta de la última captura pendiente de analizar. La fija analyze_screen (tool) y la
# consume AssistantService tras la primera pasada de inferencia.
_pending_vision_image: Optional[str] = None


def get_pending_vision_image() -> Optional[str]:
    """Devuelve y limpia la ruta de la captura pendiente de análisis visual."""
    global _pending_vision_image
    path = _pending_vision_image
    _pending_vision_image = None
    return path


class VisionToolError(Exception):
    """Errores del módulo de visión."""
    pass


class VisionTool:
    """Captura la pantalla y preprocesa la imagen."""

    def __init__(self, output_dir: Optional[Path] = None) -> None:
        self.output_dir = output_dir or Path(tempfile.gettempdir())

    async def capture_screen(self, output_path: Optional[str] = None) -> str:
        """Captura la pantalla completa con grim. Devuelve la ruta del PNG."""
        if output_path is None:
            tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False, dir=self.output_dir)
            output_path = tmp.name

        try:
            process = await asyncio.create_subprocess_exec(
                "grim", output_path,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)

            if process.returncode != 0:
                raise VisionToolError(f"grim falló: {stderr.decode().strip()}")

            if not Path(output_path).exists() or Path(output_path).stat().st_size == 0:
                raise VisionToolError("Captura de pantalla generó archivo vacío")

            logger.info(f"Pantalla capturada: {Path(output_path).stat().st_size} bytes")
            return output_path

        except FileNotFoundError:
            raise VisionToolError("grim no encontrado. Instalar grim para capturas de pantalla")
        except asyncio.TimeoutError:
            raise VisionToolError("Timeout capturando pantalla")

    async def capture_region(self, output_path: Optional[str] = None) -> str:
        """Captura una región elegida por el usuario con grim + slurp. Devuelve la ruta del PNG."""
        if output_path is None:
            tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False, dir=self.output_dir)
            output_path = tmp.name

        try:
            slurp_process = await asyncio.create_subprocess_exec(
                "slurp",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(slurp_process.communicate(), timeout=30)

            if slurp_process.returncode != 0:
                raise VisionToolError("Selección de región cancelada o fallida")

            geometry = stdout.decode().strip()

            grim_process = await asyncio.create_subprocess_exec(
                "grim", "-g", geometry, output_path,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(grim_process.communicate(), timeout=10)

            if grim_process.returncode != 0:
                raise VisionToolError(f"grim falló: {stderr.decode().strip()}")

            logger.info(f"Región capturada: {Path(output_path).stat().st_size} bytes")
            return output_path

        except FileNotFoundError:
            raise VisionToolError("slurp/grim no encontrado.")
        except asyncio.TimeoutError:
            raise VisionToolError("Timeout seleccionando región")

    @staticmethod
    def _resize_image(image_path: str, max_dim: int = MAX_IMAGE_DIMENSION) -> str:
        """Redimensiona la imagen manteniendo aspect ratio. Devuelve la ruta del resultado."""
        try:
            with Image.open(image_path) as img:
                if max(img.size) <= max_dim:
                    return image_path

                ratio = max_dim / max(img.size)
                new_size = (int(img.size[0] * ratio), int(img.size[1] * ratio))
                img_resized = img.resize(new_size, Image.Resampling.LANCZOS)

                with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                    resized_path = tmp.name

                img_resized.save(resized_path, "JPEG", quality=85)

                original_kb = Path(image_path).stat().st_size // 1024
                resized_kb = Path(resized_path).stat().st_size // 1024
                logger.info(
                    f"Imagen redimensionada: {original_kb}KB → {resized_kb}KB "
                    f"({img_resized.size[0]}×{img_resized.size[1]})"
                )
                return resized_path
        except Exception as e:
            logger.error(f"Error redimensionando imagen: {e}")
            return image_path


async def analyze_screen(region: str = "full") -> str:
    """
    Toma una captura de pantalla para que el asistente pueda 'ver' lo que hay en ella.
    Úsala cuando el usuario pregunte sobre su pantalla, una ventana abierta, un error
    visible o cualquier elemento visual. El contenido se analizará automáticamente
    después; no inventes lo que aparece en la imagen.

    Args:
        region: 'full' para capturar toda la pantalla; 'select' para que el usuario
                elija una región con el cursor.
    """
    global _pending_vision_image
    vision_tool = VisionTool()

    try:
        if region == "select":
            image_path = await vision_tool.capture_region()
        else:
            image_path = await vision_tool.capture_screen()

        resized_path = vision_tool._resize_image(image_path)
        if resized_path != image_path:
            Path(image_path).unlink(missing_ok=True)

        _pending_vision_image = resized_path
        return "Captura de pantalla realizada. Analizando el contenido visual."

    except VisionToolError as e:
        logger.error(f"Error de captura en analyze_screen: {e}")
        return f"No pude capturar la pantalla: {e}"
    except Exception as e:
        logger.error(f"Error inesperado en analyze_screen: {e}", exc_info=True)
        return f"Error capturando la pantalla: {e}"

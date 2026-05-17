"""Módulo de captura de pantalla para visión del asistente."""

import asyncio
import base64
import logging
import shlex
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

    async def capture_screen(self, output_path: Optional[str] = None) -> str:
        """
        Captura la pantalla completa usando grim de forma asíncrona.
        Devuelve la ruta del archivo PNG generado.
        """
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
        """
        Captura una región seleccionada por el usuario usando grim + slurp de forma asíncrona.
        Devuelve la ruta del archivo PNG generado.
        """
        if output_path is None:
            tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False, dir=self.output_dir)
            output_path = tmp.name

        try:
            # 1. Obtener geometría con slurp
            slurp_process = await asyncio.create_subprocess_exec(
                "slurp",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(slurp_process.communicate(), timeout=30)

            if slurp_process.returncode != 0:
                raise VisionToolError("Selección de región cancelada o fallida")

            geometry = stdout.decode().strip()

            # 2. Capturar con grim usando la geometría
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
        """Redimensiona la imagen manteniendo aspect ratio. Devuelve path del temp."""
        try:
            with Image.open(image_path) as img:
                if max(img.size) <= max_dim:
                    return image_path

                ratio = max_dim / max(img.size)
                new_size = (int(img.size[0] * ratio), int(img.size[1] * ratio))
                img_resized = img.resize(new_size, Image.Resampling.LANCZOS)

                # Usar un nombre de archivo único para evitar colisiones
                with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                    resized_path = tmp.name
                
                img_resized.save(resized_path, "JPEG", quality=85)
                
                original_kb = Path(image_path).stat().st_size // 1024
                resized_kb = Path(resized_path).stat().st_size // 1024
                logger.info(f"Imagen redimensionada: {original_kb}KB -> {resized_kb}KB ({img_resized.size[0]}x{img_resized.size[1]})")
                return resized_path
        except Exception as e:
            logger.error(f"Error redimensionando imagen: {e}")
            return image_path

# --- Funciones para LiteRT Tool Calling ---

def analyze_screen(region: str = "full") -> str:
    """
    Toma una captura de pantalla para que el asistente pueda 'ver' lo que hay en ella.
    Usa esto si el usuario hace preguntas sobre su pantalla, una ventana abierta, 
    un error visible o cualquier cosa visual.
    
    Args:
        region: 'full' para pantalla completa, 'select' para dejar al usuario elegir una región.
    """
    vision = VisionTool()
    
    async def _do_capture():
        if region == "select":
            path = await vision.capture_region()
        else:
            path = await vision.capture_screen()
        return path

    try:
        # Ejecutar captura asíncrona en el hilo de LiteRT
        try:
            loop = asyncio.get_running_loop()
            path = loop.run_until_complete(_do_capture())
        except RuntimeError:
            path = asyncio.run(_do_capture())
            
        # Redimensionar (síncrono es aceptable aquí)
        resized_path = vision._resize_image(path)
        
        # Eliminar el original si se creó uno nuevo
        if resized_path != path:
            Path(path).unlink(missing_ok=True)
        
        # Guardar en estado global temporal para AssistantService
        from src.utils import set_pending_image
        set_pending_image(resized_path)
        
        return f"Éxito: Captura de pantalla realizada. Analizando el contenido visual ahora mismo..."
    except Exception as e:
        logger.error(f"Error en tool analyze_screen: {e}")
        return f"Error capturando pantalla: {e}"

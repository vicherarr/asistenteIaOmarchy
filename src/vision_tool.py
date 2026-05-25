"""Captura de pantalla para la visión del asistente.

La inferencia visual NO ocurre aquí: el motor LiteRT no puede llamarse de forma
reentrante desde dentro de un tool. En su lugar, las capturas se registran con
stage_vision_capture() y AssistantService hace una segunda pasada al modelo con la
imagen adjunta (ver process_transcription_stream).

Fuentes de captura (analyze_screen):
- full          → el monitor enfocado completo (lo que el usuario ve ahora; ideal para
                  juegos/vídeo a pantalla completa).
- active_window → la ventana real del usuario (fullscreen-aware: si está en pantalla
                  completa captura el monitor entero). Excluye la GUI del propio Luka.
- window        → la ventana de una app/título concreto.
- region        → área dibujada por el usuario (slurp).

Como grim captura la salida COMPOSITADA, antes de cada captura ocultamos las ventanas
del propio asistente (alpha 0 vía hyprctl) y las restauramos después, para que la barra
de Luka no aparezca en la foto. La captura de la web (control_local_browser action='look')
no pasa por aquí: usa Playwright sobre el DOM y nunca incluye el escritorio.
"""

import asyncio
import json
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, Tuple

from PIL import Image

logger = logging.getLogger(__name__)

MAX_IMAGE_DIMENSION = 800

# Captura pendiente de analizar: (ruta_imagen, pregunta_opcional). La fija quien captura
# (analyze_screen o el tool de navegador) y la consume AssistantService tras la 1ª pasada.
_pending_vision: Optional[Tuple[str, Optional[str]]] = None


def stage_vision_capture(image_path: str, prompt_hint: Optional[str] = None) -> None:
    """Registra una captura para que AssistantService la analice en la segunda pasada."""
    global _pending_vision
    _pending_vision = (image_path, prompt_hint)


def get_pending_vision() -> Optional[Tuple[str, Optional[str]]]:
    """Devuelve y limpia la captura pendiente: (ruta, pregunta_opcional) o None."""
    global _pending_vision
    pending = _pending_vision
    _pending_vision = None
    return pending


class VisionToolError(Exception):
    """Errores del módulo de visión."""
    pass


class VisionTool:
    """Captura la pantalla (o una ventana) y preprocesa la imagen."""

    # Títulos de las GUIs del propio asistente. Al hablarle, su ventana suele estar
    # enfocada/encima, así que la excluimos de la selección Y la ocultamos durante grim.
    # Marcadores específicos (no el bare "asistenteia") para no excluir, p.ej., un
    # terminal abierto en el repositorio.
    OWN_WINDOW_MARKERS = ("asistenteia chat", "asistenteia spotlight")

    def __init__(self, output_dir: Optional[Path] = None) -> None:
        self.output_dir = output_dir or Path(tempfile.gettempdir())

    # --- helpers ---

    def _new_tmp(self, suffix: str = ".png") -> str:
        tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False, dir=self.output_dir)
        tmp.close()
        return tmp.name

    @classmethod
    def _is_own_window(cls, client: dict) -> bool:
        title = str(client.get("title", "")).lower()
        return any(marker in title for marker in cls.OWN_WINDOW_MARKERS)

    @staticmethod
    def _is_visible(client: dict) -> bool:
        size = client.get("size")
        return bool(
            client.get("mapped", True) and not client.get("hidden", False)
            and isinstance(size, list) and len(size) == 2 and size[0] > 0 and size[1] > 0
        )

    @staticmethod
    def _geometry(client: dict) -> str:
        at = client.get("at")
        size = client.get("size")
        if not (isinstance(at, list) and isinstance(size, list) and len(at) == 2 and len(size) == 2):
            raise VisionToolError("La ventana no tiene geometría válida")
        x, y, w, h = int(at[0]), int(at[1]), int(size[0]), int(size[1])
        if w <= 0 or h <= 0:
            raise VisionToolError("La ventana tiene tamaño nulo (¿minimizada?)")
        return f"{x},{y} {w}x{h}"

    @staticmethod
    async def _hyprctl_json(*args: str):
        """Ejecuta 'hyprctl <args> -j' y devuelve el JSON parseado."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "hyprctl", *args, "-j",
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=5)
            if proc.returncode != 0:
                raise VisionToolError(f"hyprctl {' '.join(args)} falló: {err.decode().strip()}")
            return json.loads(out.decode() or "null")
        except FileNotFoundError:
            raise VisionToolError("hyprctl no encontrado (¿estás en Hyprland?)")
        except asyncio.TimeoutError:
            raise VisionToolError("Timeout consultando hyprctl")
        except json.JSONDecodeError as e:
            raise VisionToolError(f"Respuesta de hyprctl no válida: {e}")

    async def _focused_monitor_name(self) -> Optional[str]:
        monitors = await self._hyprctl_json("monitors")
        if not isinstance(monitors, list):
            return None
        focused = next((m for m in monitors if m.get("focused")), None)
        return focused.get("name") if focused else None

    async def _monitor_name_by_id(self, monitor_id) -> Optional[str]:
        monitors = await self._hyprctl_json("monitors")
        if not isinstance(monitors, list):
            return None
        m = next((m for m in monitors if m.get("id") == monitor_id), None)
        return m.get("name") if m else None

    async def _grim(self, output_path: str, geometry: Optional[str] = None,
                    output_name: Optional[str] = None) -> str:
        """Ejecuta grim. geometry='x,y wxh' recorta; output_name captura ese monitor."""
        args = ["grim"]
        if output_name:
            args += ["-o", output_name]
        if geometry:
            args += ["-g", geometry]
        args.append(output_path)
        try:
            process = await asyncio.create_subprocess_exec(
                *args, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
            if process.returncode != 0:
                raise VisionToolError(f"grim falló: {stderr.decode().strip()}")
            if not Path(output_path).exists() or Path(output_path).stat().st_size == 0:
                raise VisionToolError("Captura generó archivo vacío")
            logger.info(f"Captura: {Path(output_path).stat().st_size} bytes "
                        f"(monitor={output_name or '-'}, geom={geometry or '-'})")
            return output_path
        except FileNotFoundError:
            raise VisionToolError("grim no encontrado. Instalar grim para capturas de pantalla")
        except asyncio.TimeoutError:
            raise VisionToolError("Timeout capturando pantalla")

    async def _capture(self, geometry: Optional[str] = None, output_name: Optional[str] = None,
                       output_path: Optional[str] = None) -> str:
        """Punto único de captura (grim). La GUI de Luka se excluye en la SELECCIÓN de
        ventana; no se oculta a nivel de compositor (Hyprland 0.55 no expone una vía
        fiable para ello sin riesgo de dejar la ventana colgada)."""
        return await self._grim(output_path or self._new_tmp(),
                                geometry=geometry, output_name=output_name)

    # --- fuentes de captura ---

    async def capture_screen(self, output_path: Optional[str] = None) -> str:
        """Captura el monitor enfocado completo (lo que el usuario ve ahora)."""
        name = None
        try:
            name = await self._focused_monitor_name()
        except VisionToolError as e:
            logger.warning(f"No se pudo determinar el monitor enfocado ({e}); capturo todo.")
        return await self._capture(output_name=name, output_path=output_path)

    async def capture_active_window(self, output_path: Optional[str] = None) -> str:
        """Captura la ventana real que el usuario estaba mirando (fullscreen-aware).

        Excluye la GUI del propio Luka y usa el historial de foco de Hyprland. Si la
        ventana está en pantalla completa, captura el monitor entero (= lo que se ve).
        """
        clients = await self._hyprctl_json("clients")
        if not isinstance(clients, list):
            raise VisionToolError("No se pudo listar las ventanas")
        candidates = [c for c in clients if self._is_visible(c) and not self._is_own_window(c)]
        if not candidates:
            raise VisionToolError("No hay ninguna ventana de usuario visible (solo el asistente)")
        candidates.sort(key=lambda c: c.get("focusHistoryID", 1_000_000))
        win = candidates[0]
        if win.get("fullscreen"):
            name = await self._monitor_name_by_id(win.get("monitor"))
            return await self._capture(output_name=name, output_path=output_path)
        return await self._capture(geometry=self._geometry(win), output_path=output_path)

    async def capture_window(self, match: str, output_path: Optional[str] = None) -> str:
        """Captura la ventana de una app/título que coincida con 'match' (fullscreen-aware)."""
        if not match:
            return await self.capture_active_window(output_path)
        clients = await self._hyprctl_json("clients")
        if not isinstance(clients, list):
            raise VisionToolError("No se pudo listar las ventanas")
        needle = match.lower().strip()
        targets_own = "asistenteia" in needle  # solo incluye la del asistente si la pide
        visible = [
            c for c in clients
            if self._is_visible(c) and (targets_own or not self._is_own_window(c))
            and (needle in str(c.get("class", "")).lower() or needle in str(c.get("title", "")).lower())
        ]
        if not visible:
            raise VisionToolError(f"No encontré ninguna ventana visible que coincida con '{match}'")
        visible.sort(key=lambda c: c.get("focusHistoryID", 1_000_000))
        win = visible[0]
        if win.get("fullscreen"):
            name = await self._monitor_name_by_id(win.get("monitor"))
            return await self._capture(output_name=name, output_path=output_path)
        return await self._capture(geometry=self._geometry(win), output_path=output_path)

    async def capture_region(self, output_path: Optional[str] = None) -> str:
        """Captura una región elegida por el usuario con slurp + grim."""
        try:
            slurp = await asyncio.create_subprocess_exec(
                "slurp", stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            out, _ = await asyncio.wait_for(slurp.communicate(), timeout=30)
            if slurp.returncode != 0:
                raise VisionToolError("Selección de región cancelada o fallida")
            geometry = out.decode().strip()
        except FileNotFoundError:
            raise VisionToolError("slurp no encontrado.")
        except asyncio.TimeoutError:
            raise VisionToolError("Timeout seleccionando región")
        return await self._capture(geometry=geometry, output_path=output_path)

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
                resized_path = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False).name
                img_resized.save(resized_path, "JPEG", quality=85)
                logger.info(
                    f"Imagen redimensionada a {img_resized.size[0]}×{img_resized.size[1]} "
                    f"({Path(resized_path).stat().st_size // 1024}KB)"
                )
                return resized_path
        except Exception as e:
            logger.error(f"Error redimensionando imagen: {e}")
            return image_path


async def analyze_screen(source: str = "full", target: str = "") -> str:
    """MIRA y ANALIZA la pantalla y te digo qué contiene; para LEER o DESCRIBIR algo (si solo quieren GUARDAR/COPIAR la captura usa take_screenshot; para una web usa control_local_browser action='look').

    Args:
        source: una de —
            full (monitor actual; lo que ve ahora, incluye juego o vídeo),
            active_window (solo la ventana enfocada),
            window (una app concreta; indica cuál en target),
            region (el usuario dibuja el área con el cursor).
            Por defecto full.
        target: nombre de app o título cuando source='window'.
    """
    source = (source or "full").lower().strip()
    vision = VisionTool()

    async def _do_capture() -> str:
        if source in ("region", "select"):
            return await vision.capture_region()
        if source in ("active_window", "active", "focused", "ventana_activa", "ventana"):
            return await vision.capture_active_window()
        if source in ("window", "app"):
            return await vision.capture_window(target)
        return await vision.capture_screen()  # full / screen / monitor / pantalla

    try:
        try:
            path = await _do_capture()
        except VisionToolError as e:
            # Las fuentes de ventana pueden fallar (sin foco, no encontrada). Caemos al
            # monitor enfocado para no dejar al usuario sin respuesta visual.
            if source not in ("full", "screen", "monitor", "pantalla"):
                logger.warning(f"Captura '{source}' falló ({e}); uso el monitor enfocado.")
                path = await vision.capture_screen()
            else:
                raise

        resized = VisionTool._resize_image(path)
        if resized != path:
            Path(path).unlink(missing_ok=True)

        stage_vision_capture(resized)
        return "Captura realizada. Analizando el contenido visual."

    except VisionToolError as e:
        logger.error(f"Error de captura en analyze_screen: {e}")
        return f"No pude capturar la pantalla: {e}"
    except Exception as e:
        logger.error(f"Error inesperado en analyze_screen: {e}", exc_info=True)
        return f"Error capturando la pantalla: {e}"


async def _copy_image_to_clipboard(image_path: str) -> bool:
    """Copia el PNG al portapapeles vía wl-copy. Devuelve True si tuvo éxito.

    wl-copy se demoniza (doble fork) para seguir sirviendo el contenido tras leer stdin.
    El proceso padre termina enseguida; redirigimos stdout/stderr a DEVNULL para no
    bloquearnos esperando EOF de un pipe que el demonio hijo mantiene abierto.
    """
    try:
        with open(image_path, "rb") as f:
            proc = await asyncio.create_subprocess_exec(
                "wl-copy", "--type", "image/png",
                stdin=f, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            rc = await asyncio.wait_for(proc.wait(), timeout=5)
    except FileNotFoundError:
        logger.warning("wl-copy no encontrado; no se copió al portapapeles.")
        return False
    except asyncio.TimeoutError:
        logger.warning("Timeout copiando la captura al portapapeles.")
        return False
    if rc != 0:
        logger.warning(f"wl-copy devolvió código {rc}.")
        return False
    return True


def _open_in_annotator(image_path: str) -> bool:
    """Abre la imagen en satty (visor/anotador) de forma desacoplada. Cae a xdg-open.

    No bloquea: satty queda abierto y sobrevive a este proceso. --copy-command permite
    que, si el usuario anota, re-copie la versión editada al portapapeles.
    """
    for cmd in (
        ["satty", "--filename", image_path, "--copy-command", "wl-copy"],
        ["xdg-open", image_path],
    ):
        try:
            subprocess.Popen(
                cmd, start_new_session=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return True
        except FileNotFoundError:
            continue
        except Exception as e:
            logger.warning(f"No se pudo abrir el visor con {cmd[0]}: {e}")
            continue
    logger.warning("No hay visor disponible (ni satty ni xdg-open).")
    return False


async def take_screenshot() -> str:
    """Hace una captura de pantalla y la GUARDA para el usuario: la copia al portapapeles y la abre en satty; NO la analiza (si quieren que MIRES o LEAS la pantalla usa analyze_screen).

    Úsala para "haz una captura", "haz un pantallazo", "captura/copia la pantalla".
    """
    vision = VisionTool()
    try:
        path = await vision.capture_screen()
    except VisionToolError as e:
        logger.error(f"Error de captura en take_screenshot: {e}")
        return f"No pude hacer la captura: {e}"
    except Exception as e:
        logger.error(f"Error inesperado en take_screenshot: {e}", exc_info=True)
        return f"Error haciendo la captura: {e}"

    copied = await _copy_image_to_clipboard(path)
    opened = _open_in_annotator(path)

    detalles = ["copiada al portapapeles" if copied else "no pude copiarla al portapapeles"]
    detalles.append("abierta en satty" if opened else f"guardada en {path} (no encontré visor)")
    return f"Captura de pantalla hecha: {' y '.join(detalles)}."


# MIME types de imagen aceptados del portapapeles, por orden de preferencia. PIL los
# decodifica todos; preferimos png/jpeg por ser los más universales.
_CLIPBOARD_IMAGE_TYPES = (
    "image/png", "image/jpeg", "image/jpg", "image/webp", "image/bmp", "image/tiff",
)


async def _list_clipboard_image_types() -> list[str]:
    """Devuelve los tipos MIME 'image/*' presentes en el portapapeles (Wayland)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "wl-paste", "--list-types",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=5)
    except FileNotFoundError:
        raise VisionToolError("wl-paste no encontrado (instala wl-clipboard).")
    except asyncio.TimeoutError:
        raise VisionToolError("Timeout consultando el portapapeles.")
    # wl-paste devuelve código 1 cuando el portapapeles está vacío: no es un error fatal.
    types = [t.strip().lower() for t in out.decode(errors="ignore").splitlines() if t.strip()]
    return [t for t in types if t.startswith("image/")]


async def _dump_clipboard_image(mime: str, output_path: str) -> None:
    """Vuelca los bytes de la imagen del portapapeles (tipo 'mime') a 'output_path'."""
    try:
        with open(output_path, "wb") as f:
            proc = await asyncio.create_subprocess_exec(
                "wl-paste", "--type", mime,
                stdout=f, stderr=subprocess.PIPE,
            )
            _, err = await asyncio.wait_for(proc.communicate(), timeout=10)
    except asyncio.TimeoutError:
        raise VisionToolError("Timeout leyendo la imagen del portapapeles.")
    if proc.returncode != 0:
        raise VisionToolError(f"wl-paste falló: {err.decode(errors='ignore').strip()}")
    if not Path(output_path).exists() or Path(output_path).stat().st_size == 0:
        raise VisionToolError("El portapapeles no contenía datos de imagen.")


def _normalize_clipboard_image(raw_path: str, output_dir: Path) -> str:
    """Decodifica los bytes crudos, normaliza a RGB, redimensiona y guarda como JPEG.

    Reencodamos siempre (no como _resize_image, que puede devolver el original) porque el
    volcado del portapapeles no tiene extensión fiable y puede venir en formatos variados.
    """
    with Image.open(raw_path) as img:
        img = img.convert("RGB")
        if max(img.size) > MAX_IMAGE_DIMENSION:
            ratio = MAX_IMAGE_DIMENSION / max(img.size)
            img = img.resize(
                (int(img.size[0] * ratio), int(img.size[1] * ratio)),
                Image.Resampling.LANCZOS,
            )
        out_path = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False, dir=output_dir).name
        img.save(out_path, "JPEG", quality=85)
    logger.info(f"Imagen del portapapeles preparada: {Path(out_path).stat().st_size // 1024}KB")
    return out_path


async def analyze_clipboard_image() -> str:
    """
    Analiza la IMAGEN que el usuario tiene copiada en el portapapeles.
    Úsala cuando el usuario diga "mira la imagen de mi portapapeles", "mira lo que he
    copiado", "analiza la captura que tengo copiada" o haga preguntas sobre una imagen
    que acaba de copiar (por ejemplo, una captura de pantalla en el portapapeles).
    El contenido se analiza automáticamente después; no inventes lo que aparece.

    Para TEXTO copiado usa clipboard_manager(action='paste'); esta herramienta es solo
    para imágenes.
    """
    vision = VisionTool()
    try:
        image_types = await _list_clipboard_image_types()
    except VisionToolError as e:
        logger.error(f"Error consultando el portapapeles: {e}")
        return f"No pude leer el portapapeles: {e}"

    if not image_types:
        return ("No hay ninguna imagen en el portapapeles. Copia primero una imagen "
                "(por ejemplo, una captura de pantalla) y vuelve a pedírmelo.")

    chosen = next((t for t in _CLIPBOARD_IMAGE_TYPES if t in image_types), image_types[0])
    raw_path = vision._new_tmp(suffix=".bin")
    try:
        await _dump_clipboard_image(chosen, raw_path)
        prepared = _normalize_clipboard_image(raw_path, vision.output_dir)
        stage_vision_capture(prepared)
        return "Imagen del portapapeles cargada. Analizando el contenido visual."
    except VisionToolError as e:
        logger.error(f"Error leyendo imagen del portapapeles: {e}")
        return f"No pude leer la imagen del portapapeles: {e}"
    except Exception as e:
        logger.error(f"Error inesperado en analyze_clipboard_image: {e}", exc_info=True)
        return ("No pude interpretar la imagen del portapapeles; "
                "asegúrate de tener una imagen copiada.")
    finally:
        Path(raw_path).unlink(missing_ok=True)

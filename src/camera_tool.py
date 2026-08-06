"""Los ojos del dispositivo: mirar por la cámara del ESP32 y enseñar la foto.

Los nombres van en INGLÉS y siguiendo el patrón de las demás (`analyze_screen`,
`take_screenshot`). No es cosmética: con nombres en español el modelo los
generaba mal —emitió `call:mirarara` por `mirar_camara`, comiéndose el `_cam`—,
la llamada no parseaba y acababa improvisando que no tiene ojos. Un nombre que
no encaja con el patrón del resto del catálogo es un nombre que el modelo
escribe mal.

Se apoya entero en lo que ya existe. `stage_vision_capture` y la segunda pasada
de `AssistantService` son exactamente las mismas que usa `analyze_screen` para
las capturas de pantalla, así que aquí solo hay que conseguir la imagen y
dejarla ahí: quien la describe es maquinaria que ya funcionaba.

La diferencia con la pantalla es de dónde sale el píxel. Aquí hay que pedírselo
a un dispositivo que está en otra habitación y puede no contestar, así que todo
lo que sigue asume que la foto puede no llegar.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path
from typing import Optional

from src.vision_tool import stage_vision_capture, get_pending_vision

logger = logging.getLogger(__name__)

# Última foto que mandó el dispositivo. La guarda `analyze_camera` para que
# `show_camera_photo` pueda enseñarla sin volver a pedirla: "mírala" y "enséñamela"
# son dos frases seguidas, no dos fotos distintas.
_ultima_foto: Optional[Path] = None


def ultima_foto() -> Optional[Path]:
    return _ultima_foto


async def analyze_camera(question: str = "") -> str:
    """TOMA UNA FOTO NUEVA con la cámara del altavoz y describe lo que ve AHORA; úsala SIEMPRE que pregunten qué ves, qué hay delante o quieran que mires algo del mundo real, INCLUSO SI YA MIRASTE ANTES — la escena cambia y una descripción vieja no sirve, nunca respondas de memoria (para la PANTALLA del ordenador usa analyze_screen).

    Args:
        question: qué quiere saber el usuario sobre lo que se ve, si lo concretó.
    """
    global _ultima_foto

    # Tirar una captura huérfana de un turno anterior que no llegó a analizarse.
    # `_pending_vision` es un global del módulo de visión: si un turno la deja ahí
    # y se cancela, la recoge el turno SIGUIENTE y describe la foto vieja. Es una
    # de las dos formas en que esto acababa hablando siempre de la primera foto.
    get_pending_vision()

    # Importación diferida: sin dispositivo conectado este módulo no debe
    # arrastrar la pasarela, y así la tool existe aunque el satélite no esté.
    from src.device_gateway import manager

    if not manager.connected or manager.session is None:
        return (
            "No hay ningún dispositivo con cámara conectado ahora mismo. "
            "Si quieres que mire la pantalla del ordenador, puedo hacerlo."
        )

    path = await manager.session.request_capture()
    if path is None:
        # Distinguir esto de "no hay cámara" importa: aquí el dispositivo SÍ
        # está, así que el problema es la cámara o el enlace, y el usuario puede
        # hacer algo al respecto.
        return "El dispositivo está conectado pero no me ha mandado la foto. ¿Está la cámara bien conectada?"

    _ultima_foto = path
    stage_vision_capture(str(path), question or None)
    # El texto que devuelve una tool es lo ÚNICO que el modelo conserva de ella
    # en el turno siguiente, así que aquí se decide cómo se comporta después.
    #
    # La versión anterior decía "la foto queda guardada", y con eso arreglaba que
    # supiera enseñarla... pero le enseñaba a la vez que no hacía falta hacer otra:
    # a la segunda pregunta describía la primera foto de memoria. Se dice lo justo
    # para MOSTRAR, dejando claro que para VOLVER A MIRAR hay que capturar de nuevo.
    return (
        "Foto NUEVA tomada con la cámara. Analizando lo que se ve ahora. "
        "Para enseñársela en pantalla, show_camera_photo; "
        "para volver a mirar más tarde, hay que llamar otra vez a analyze_camera."
    )


async def show_camera_photo() -> str:
    """ABRE en el monitor la foto que se acaba de tomar, para que el usuario la vea con sus ojos; solo para ENSEÑAR una foto ya tomada (enséñamela, muéstramela, quiero verla, ábrela, ponla en pantalla). NO mira ni describe ni toma nada: si preguntan qué se ve ahora, es analyze_camera.
    """
    foto = _ultima_foto
    if foto is None or not foto.exists():
        return "Todavía no he tomado ninguna foto con la cámara. Pídemelo y la hago."

    try:
        # `xdg-open` y no un visor concreto: abre lo que el usuario tenga puesto
        # por defecto, en vez de imponer uno que quizá no esté instalado.
        #
        # Se desatiende a propósito: el visor se queda abierto y esperar a que se
        # cierre bloquearía el turno de voz hasta que el usuario cerrara la
        # ventana.
        await asyncio.create_subprocess_exec(
            "xdg-open", str(foto),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        logger.warning("xdg-open no está; no se pudo abrir la foto")
        return f"No he podido abrirla, pero está guardada en {foto}"
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error abriendo la foto: {e}", exc_info=True)
        return f"No he podido abrirla: {e}"

    return "Ahí la tienes, en la pantalla."

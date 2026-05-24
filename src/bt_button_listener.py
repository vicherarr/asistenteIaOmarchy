"""
Módulo de integración de botones Bluetooth (AVRCP) para el asistente.

Permite registrar callbacks para eventos de botón del dispositivo HOME SPA-133
u otros dispositivos Bluetooth que expongan una interfaz evdev AVRCP.

Uso básico:
    from src.bt_button_listener import BtButtonListener
    
    listener = BtButtonListener()
    listener.on_play = lambda: print("Play pulsado!")
    listener.on_pause = lambda: print("Pause pulsado!")
    
    await listener.start()   # Inicia el listener en background
    # ...
    await listener.stop()    # Detiene el listener

El listener usa evdev para leer eventos de input del kernel de forma no bloqueante.
"""

import asyncio
import logging
from typing import Callable, Optional

try:
    from evdev import InputDevice, ecodes, list_devices
    EVDEV_AVAILABLE = True
except ImportError:
    EVDEV_AVAILABLE = False

logger = logging.getLogger(__name__)


class BtButtonListenerError(Exception):
    """Error específico del listener de botones Bluetooth."""
    pass


class BtButtonListener:
    """
    Escucha eventos de botón de dispositivos Bluetooth AVRCP vía evdev.

    Por defecto busca dispositivos cuyo nombre contenga "HOME SPA" o "AVRCP",
    pero se puede configurar con `device_name_filter`.
    """

    def __init__(
        self,
        device_name_filter: Optional[str] = None,
        grab_device: bool = False,
    ) -> None:
        """
        Args:
            device_name_filter: Substring para buscar en el nombre evdev.
                                Si es None, busca "HOME SPA" o "AVRCP".
            grab_device: Si True, hace grab exclusivo del dispositivo evdev
                         para evitar que el sistema consuma los eventos.
                         Útil solo si quieres interceptar y no dejar que
                         PipeWire/MPRIS actúen sobre ellos.
        """
        if not EVDEV_AVAILABLE:
            raise BtButtonListenerError(
                "python-evdev no instalado. Ejecuta: pip install evdev"
            )

        self._filter = device_name_filter or ("HOME SPA", "AVRCP")
        if isinstance(self._filter, str):
            self._filter = (self._filter,)

        self._grab = grab_device
        self._device: Optional[InputDevice] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False

        # Callbacks públicos que el usuario puede asignar
        self.on_play: Optional[Callable[[], None]] = None
        self.on_pause: Optional[Callable[[], None]] = None
        self.on_playpause: Optional[Callable[[], None]] = None
        self.on_next: Optional[Callable[[], None]] = None
        self.on_previous: Optional[Callable[[], None]] = None
        self.on_stop: Optional[Callable[[], None]] = None
        self.on_volume_up: Optional[Callable[[], None]] = None
        self.on_volume_down: Optional[Callable[[], None]] = None
        self.on_unknown: Optional[Callable[[str, int], None]] = None

    def _find_device(self) -> Optional[InputDevice]:
        """Busca el dispositivo evdev que coincida con el filtro."""
        for path in list_devices():
            try:
                dev = InputDevice(path)
                if any(f in dev.name for f in self._filter):
                    logger.info(f"Dispositivo BT encontrado: {dev.name} en {dev.path}")
                    return dev
            except Exception:
                continue
        return None

    async def start(self) -> None:
        """Inicia la escucha de eventos en una tarea de background."""
        if self._running:
            logger.warning("BtButtonListener ya está en ejecución.")
            return

        self._running = True
        self._device = self._find_device()
        if not self._device:
            logger.warning(
                f"No se encontró dispositivo Bluetooth {self._filter} al arrancar. "
                "El listener buscará y se conectará automáticamente en segundo plano cuando esté disponible."
            )

        self._task = asyncio.create_task(self._read_loop())
        logger.info("BtButtonListener iniciado.")

    async def stop(self) -> None:
        """Detiene la escucha y libera recursos."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        if self._device:
            try:
                self._device.ungrab()
            except Exception:
                pass
            self._device.close()
            self._device = None

        logger.info("BtButtonListener detenido.")

    async def _read_loop(self) -> None:
        """Loop principal de lectura de eventos evdev con auto-reconexión."""
        while self._running:
            if not self._device:
                self._device = self._find_device()
                if not self._device:
                    await asyncio.sleep(5.0)
                    continue
                else:
                    logger.info(f"Dispositivo BT re-conectado y abierto: {self._device.name} en {self._device.path}")

            try:
                if self._grab:
                    try:
                        self._device.grab()
                        logger.info("Grab exclusivo activado en dispositivo evdev.")
                    except Exception as e:
                        logger.warning(f"No se pudo hacer grab exclusivo: {e}")

                # Leer eventos de forma asíncrona usando el loop integrado de evdev
                async for event in self._device.async_read_loop():
                    if not self._running:
                        break
                    if event.type == ecodes.EV_KEY and event.value == 1:  # Solo KEY_PRESS
                        self._handle_key(event.code)
            except asyncio.CancelledError:
                logger.debug("Loop de lectura cancelado.")
                break
            except Exception as e:
                logger.error(f"Error leyendo eventos evdev: {e}")
                # Limpiar el dispositivo roto
                if self._device:
                    try:
                        self._device.ungrab()
                    except Exception:
                        pass
                    try:
                        self._device.close()
                    except Exception:
                        pass
                    self._device = None
                
                # Esperar antes de reintentar buscar el dispositivo de nuevo
                await asyncio.sleep(3.0)

    def _handle_key(self, code: int) -> None:
        """Mapea códigos de tecla a callbacks correspondientes."""
        # Mapeo de códigos de AVRCP comunes
        mapping = {
            200: ("play", self.on_play),          # KEY_PLAYCD
            201: ("pause", self.on_pause),         # KEY_PAUSECD
            164: ("playpause", self.on_playpause), # KEY_PLAYPAUSE (si existiera)
            163: ("next", self.on_next),           # KEY_NEXTSONG
            165: ("previous", self.on_previous),   # KEY_PREVIOUSSONG
            166: ("stop", self.on_stop),           # KEY_STOPCD
            114: ("volumedown", self.on_volume_down),  # KEY_VOLUMEDOWN
            115: ("volumeup", self.on_volume_up),      # KEY_VOLUMEUP
        }

        if code in mapping:
            action_name, callback = mapping[code]
            logger.info(f"Botón BT detectado: {action_name} (code={code})")
            if callback:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        asyncio.create_task(callback())
                    else:
                        callback()
                except Exception as e:
                    logger.error(f"Error en callback {action_name}: {e}")
        else:
            name = ecodes.KEY.get(code, f"CODE_{code}")
            if isinstance(name, tuple):
                name = name[0]
            logger.debug(f"Tecla desconocida: {name} (code={code})")
            if self.on_unknown:
                try:
                    if asyncio.iscoroutinefunction(self.on_unknown):
                        asyncio.create_task(self.on_unknown(name, code))
                    else:
                        self.on_unknown(name, code)
                except Exception as e:
                    logger.error(f"Error en callback on_unknown: {e}")

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def device_name(self) -> Optional[str]:
        return self._device.name if self._device else None

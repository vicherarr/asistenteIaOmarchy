"""
Dummy MPRIS Player para interceptar comandos AVRCP de dispositivos Bluetooth.

Cuando un dispositivo Bluetooth AVRCP (como HOME SPA-133) envía comandos
multimedia (Play, Pause, etc.), BlueZ los traduce a llamadas MPRIS via
mpris-proxy. Este dummy player se registra como reproductor activo para
recibir esos comandos y convertirlos en callbacks personalizables.

Requiere: dbus-next (pip install dbus-next)
Uso:
    player = MprisDummyPlayer()
    player.on_play = lambda: print("Play recibido!")
    await player.start()
    # ...
    await player.stop()

Nota: mpris-proxy debe estar ejecutándose para que BlueZ envíe los
comandos AVRCP a players MPRIS. Este módulo NO inicia mpris-proxy;
es responsabilidad del caller lanzarlo si es necesario.
"""

import asyncio
import logging
from typing import Callable, Optional

from dbus_next.aio import MessageBus
from dbus_next.service import ServiceInterface, method, dbus_property, signal, PropertyAccess
from dbus_next import Variant, BusType

logger = logging.getLogger(__name__)

BUS_NAME = "org.mpris.MediaPlayer2.asistenteia"
OBJECT_PATH = "/org/mpris/MediaPlayer2"


class MprisPlayerError(Exception):
    """Error del dummy player MPRIS."""
    pass


class _MprisRootInterface(ServiceInterface):
    """Interfaz org.mpris.MediaPlayer2 (root)."""

    def __init__(self) -> None:
        super().__init__("org.mpris.MediaPlayer2")

    @dbus_property(access=PropertyAccess.READ)
    def Identity(self) -> "s":
        return "AsistenteIA"

    @dbus_property(access=PropertyAccess.READ)
    def CanRaise(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def CanQuit(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def HasTrackList(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def DesktopEntry(self) -> "s":
        return "asistenteia"

    @method()
    def Raise(self):
        pass

    @method()
    def Quit(self):
        pass


class _MprisTrackListInterface(ServiceInterface):
    """Interfaz dummy org.mpris.MediaPlayer2.TrackList (requerida por algunos clientes)."""

    def __init__(self) -> None:
        super().__init__("org.mpris.MediaPlayer2.TrackList")

    @dbus_property(access=PropertyAccess.READ)
    def Tracks(self) -> "ao":
        return []

    @dbus_property(access=PropertyAccess.READ)
    def CanEditTracks(self) -> "b":
        return False

    @method()
    def GetTracksMetadata(self, track_ids: "ao") -> "aa{sv}":
        return []

    @method()
    def AddTrack(self, uri: "s", after_track: "o", set_as_current: "b"):
        pass

    @method()
    def RemoveTrack(self, track_id: "o"):
        pass

    @method()
    def GoTo(self, track_id: "o"):
        pass


class _MprisPlaylistsInterface(ServiceInterface):
    """Interfaz dummy org.mpris.MediaPlayer2.Playlists (requerida por algunos clientes)."""

    def __init__(self) -> None:
        super().__init__("org.mpris.MediaPlayer2.Playlists")

    @dbus_property(access=PropertyAccess.READ)
    def PlaylistCount(self) -> "u":
        return 0

    @dbus_property(access=PropertyAccess.READ)
    def Orderings(self) -> "as":
        return []

    @dbus_property(access=PropertyAccess.READ)
    def ActivePlaylist(self) -> "(b(oa{sv}))":
        return [False, ["/", {}]]

    @method()
    def ActivatePlaylist(self, playlist_id: "o"):
        pass

    @method()
    def GetPlaylists(self, index: "u", max_count: "u", order: "s", reverse: "b") -> "a(oa{sv})":
        return []


class _MprisPlayerInterface(ServiceInterface):
    """Interfaz org.mpris.MediaPlayer2.Player."""

    def __init__(self, on_play=None, on_pause=None, on_next=None, on_previous=None):
        super().__init__("org.mpris.MediaPlayer2.Player")
        self._playback_status = "Stopped"
        self._can_play = True
        self._can_pause = True
        self._can_go_next = True
        self._can_go_previous = True
        self._can_seek = False
        self._can_control = True
        self._metadata = {}
        self.on_play: Optional[Callable[[], None]] = on_play
        self.on_pause: Optional[Callable[[], None]] = on_pause
        self.on_next: Optional[Callable[[], None]] = on_next
        self.on_previous: Optional[Callable[[], None]] = on_previous

    @dbus_property()
    def PlaybackStatus(self) -> "s":
        return self._playback_status

    @PlaybackStatus.setter
    def PlaybackStatus(self, value: "s"):
        self._playback_status = value

    @dbus_property(access=PropertyAccess.READ)
    def CanPlay(self) -> "b":
        return self._can_play

    @dbus_property(access=PropertyAccess.READ)
    def CanPause(self) -> "b":
        return self._can_pause

    @dbus_property(access=PropertyAccess.READ)
    def CanGoNext(self) -> "b":
        return self._can_go_next

    @dbus_property(access=PropertyAccess.READ)
    def CanGoPrevious(self) -> "b":
        return self._can_go_previous

    @dbus_property(access=PropertyAccess.READ)
    def CanSeek(self) -> "b":
        return self._can_seek

    @dbus_property(access=PropertyAccess.READ)
    def CanControl(self) -> "b":
        return self._can_control

    @dbus_property(access=PropertyAccess.READ)
    def Metadata(self) -> "a{sv}":
        return {k: Variant("s", v) if isinstance(v, str) else v for k, v in self._metadata.items()}

    @method()
    def Play(self):
        logger.info("MPRIS: Play() recibido desde AVRCP")
        self._playback_status = "Playing"
        self.emit_properties_changed({"PlaybackStatus": "Playing"})
        if self.on_play:
            try:
                if asyncio.iscoroutinefunction(self.on_play):
                    asyncio.create_task(self.on_play())
                else:
                    self.on_play()
            except Exception as e:
                logger.error(f"Error en callback on_play: {e}")

    @method()
    def Pause(self):
        logger.info("MPRIS: Pause() recibido desde AVRCP")
        self._playback_status = "Paused"
        self.emit_properties_changed({"PlaybackStatus": "Paused"})
        if self.on_pause:
            try:
                if asyncio.iscoroutinefunction(self.on_pause):
                    asyncio.create_task(self.on_pause())
                else:
                    self.on_pause()
            except Exception as e:
                logger.error(f"Error en callback on_pause: {e}")

    @method()
    def PlayPause(self):
        logger.info("MPRIS: PlayPause() recibido desde AVRCP")
        if self._playback_status == "Playing":
            self.Pause()
        else:
            self.Play()

    @method()
    def Stop(self):
        logger.info("MPRIS: Stop() recibido desde AVRCP")
        self._playback_status = "Stopped"
        self.emit_properties_changed({"PlaybackStatus": "Stopped"})

    @method()
    def Next(self):
        logger.info("MPRIS: Next() recibido desde AVRCP")
        if self.on_next:
            try:
                if asyncio.iscoroutinefunction(self.on_next):
                    asyncio.create_task(self.on_next())
                else:
                    self.on_next()
            except Exception as e:
                logger.error(f"Error en callback on_next: {e}")

    @method()
    def Previous(self):
        logger.info("MPRIS: Previous() recibido desde AVRCP")
        if self.on_previous:
            try:
                if asyncio.iscoroutinefunction(self.on_previous):
                    asyncio.create_task(self.on_previous())
                else:
                    self.on_previous()
            except Exception as e:
                logger.error(f"Error en callback on_previous: {e}")

    @method()
    def Seek(self, offset: "x"):
        pass

    @method()
    def SetPosition(self, track_id: "o", position: "x"):
        pass

    @method()
    def OpenUri(self, uri: "s"):
        pass

    @signal()
    def Seeked(self, position: "x") -> "x":
        return position


class MprisDummyPlayer:
    """
    Dummy MPRIS Player que se registra en el bus de sesión para recibir
    comandos multimedia provenientes de dispositivos Bluetooth AVRCP.
    """

    def __init__(self) -> None:
        self._bus: Optional[MessageBus] = None
        self._root_iface: Optional[_MprisRootInterface] = None
        self._player_iface: Optional[_MprisPlayerInterface] = None
        self._tracklist_iface: Optional[_MprisTrackListInterface] = None
        self._playlists_iface: Optional[_MprisPlaylistsInterface] = None
        self._started = False

        self.on_play: Optional[Callable[[], None]] = None
        self.on_pause: Optional[Callable[[], None]] = None
        self.on_next: Optional[Callable[[], None]] = None
        self.on_previous: Optional[Callable[[], None]] = None

    async def start(self) -> None:
        """Inicia el servicio MPRIS en el bus de sesión."""
        if self._started:
            return

        try:
            self._bus = await MessageBus(bus_type=BusType.SESSION).connect()
        except Exception as e:
            raise MprisPlayerError(f"No se pudo conectar al bus de sesión D-Bus: {e}")

        self._root_iface = _MprisRootInterface()
        self._player_iface = _MprisPlayerInterface(
            on_play=self.on_play,
            on_pause=self.on_pause,
            on_next=self.on_next,
            on_previous=self.on_previous,
        )
        self._tracklist_iface = _MprisTrackListInterface()
        self._playlists_iface = _MprisPlaylistsInterface()

        self._bus.export(OBJECT_PATH, self._root_iface)
        self._bus.export(OBJECT_PATH, self._player_iface)
        self._bus.export(OBJECT_PATH, self._tracklist_iface)
        self._bus.export(OBJECT_PATH, self._playlists_iface)

        # Solicitar nombre en el bus
        from dbus_next.constants import RequestNameReply
        reply = await self._bus.request_name(BUS_NAME)
        if reply not in (RequestNameReply.PRIMARY_OWNER, RequestNameReply.ALREADY_OWNER):
            logger.warning(f"request_name devolvió {reply}, puede haber otro owner")

        self._started = True
        logger.info(f"Dummy MPRIS Player registrado: {BUS_NAME}")

    async def stop(self) -> None:
        """Detiene el servicio MPRIS y libera el nombre."""
        if not self._started or not self._bus:
            return

        try:
            await self._bus.release_name(BUS_NAME)
        except Exception:
            pass

        try:
            self._bus.disconnect()
        except Exception:
            pass

        self._started = False
        logger.info("Dummy MPRIS Player detenido")

    @property
    def is_started(self) -> bool:
        return self._started

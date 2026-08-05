"""Tests del endpoint `/device/ws` y del control de salida de audio.

Cubren lo que los tests de `DeviceSession` no pueden ver: el *upgrade* del
WebSocket y su autenticación, que en FastAPI **no** pasa por las dependencias
`Depends(verify_token)` de las rutas HTTP. Es justo la clase de detalle que se
descubre tarde, cuando el dispositivo ya está montado en la pared.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from fastapi.websockets import WebSocketDisconnect

from src import device_protocol as proto
from src.config import settings
from src.main import app

TOKEN = "token-de-prueba"


@pytest.fixture
def client(monkeypatch):
    """Cliente con un AppState de mentira y un token conocido.

    Se inyecta `app.state.app_state` a mano porque el `lifespan` real cargaría el
    modelo LiteRT entero, que no pinta nada en un test de transporte.

    Por eso el `TestClient` se usa **sin** `with`: entrar en su contexto dispara
    el `lifespan`, que crea un `AppState` de verdad y pisa este doble (ver
    `src/main.py`, `app.state.app_state = state`). Sin el contexto, cada petición
    abre su propio portal y las rutas ven el mock.
    """
    monkeypatch.setattr(settings, "API_TOKEN", TOKEN)

    state = MagicMock()
    state.stt_engine.transcribe = AsyncMock(return_value="")
    state.audio_manager.default_sink = None
    state.conversation_history = []
    state.assistant_service.audio_target = "pc"
    state.assistant_service.audio_sink = None
    state.assistant_service.wait_for_tts_complete = AsyncMock()
    monkeypatch.setattr(app.state, "app_state", state, raising=False)

    c = TestClient(app)
    c.app_state = state
    yield c


class TestAutenticacion:
    def test_sin_token_se_rechaza(self, client):
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/device/ws"):
                pass
        assert exc.value.code == 1008  # policy violation

    def test_token_incorrecto_se_rechaza(self, client):
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/device/ws?token=malo"):
                pass
        assert exc.value.code == 1008

    def test_token_por_query_se_acepta(self, client):
        with client.websocket_connect(f"/device/ws?token={TOKEN}") as ws:
            frame = proto.decode(ws.receive_bytes())
            assert frame.kind == proto.STATE
            assert frame.json()["state"] == proto.STATE_IDLE

    def test_token_por_cabecera_se_acepta(self, client):
        with client.websocket_connect("/device/ws", headers={"X-API-Token": TOKEN}) as ws:
            assert proto.decode(ws.receive_bytes()).kind == proto.STATE


class TestSesion:
    def test_el_ping_se_responde(self, client):
        with client.websocket_connect(f"/device/ws?token={TOKEN}") as ws:
            ws.receive_bytes()  # STATE inicial
            ws.send_bytes(proto.encode(proto.PING))
            assert proto.decode(ws.receive_bytes()).kind == proto.PONG

    def test_la_telemetria_se_responde_y_queda_en_el_estado(self, client):
        """La telemetría hace de latido y de diagnóstico a la vez.

        Se responde con PONG para que el dispositivo sepa que el enlace va en los
        dos sentidos, no solo de subida, y se guarda para poder mirar luego si la
        WiFi flojea o si la memoria del aparato se está erosionando.
        """
        with client.websocket_connect(f"/device/ws?token={TOKEN}") as ws:
            ws.receive_bytes()  # STATE inicial
            ws.send_bytes(proto.encode_json(
                proto.TELEMETRY, rssi=-64, uptime_s=3600, free_heap=4_500_000
            ))
            assert proto.decode(ws.receive_bytes()).kind == proto.PONG

            r = client.get("/device/status", headers={"X-API-Token": TOKEN})
            device = r.json()["device"]
            assert device["telemetry"]["rssi"] == -64
            assert device["telemetry"]["free_heap"] == 4_500_000
            # La antigüedad es lo que delata un dispositivo que dejó de latir
            # aunque el socket siga aparentando estar vivo.
            assert device["telemetry_age_s"] < 5

    def test_al_conectar_el_audio_pasa_a_both_y_al_salir_vuelve_a_pc(self, client):
        """Lo crítico: que desconectar el dispositivo no deje al asistente mudo."""
        service = client.app_state.assistant_service
        with client.websocket_connect(f"/device/ws?token={TOKEN}") as ws:
            ws.receive_bytes()
            assert service.audio_target == "both"
            assert service.audio_sink is not None
        assert service.audio_target == "pc"
        assert service.audio_sink is None


class TestControlDeSalida:
    def test_sin_dispositivo_no_se_puede_mandar_al_device(self, client):
        r = client.post("/device/audio-sink", json={"target": "device"},
                        headers={"X-API-Token": TOKEN})
        assert r.status_code == 409

    def test_target_invalido(self, client):
        r = client.post("/device/audio-sink", json={"target": "altavoz-del-vecino"},
                        headers={"X-API-Token": TOKEN})
        assert r.status_code == 400

    def test_volver_a_pc_siempre_se_permite(self, client):
        # Aunque no haya dispositivo: es la vía de escape para recuperar el audio.
        r = client.post("/device/audio-sink", json={"target": "pc"},
                        headers={"X-API-Token": TOKEN})
        assert r.status_code == 200
        assert r.json()["audio_target"] == "pc"

    def test_status_sin_dispositivo(self, client):
        r = client.get("/device/status", headers={"X-API-Token": TOKEN})
        assert r.status_code == 200
        assert r.json()["device"]["connected"] is False

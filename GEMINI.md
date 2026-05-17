# AsistenteIA - Instrucciones del Proyecto

## Arquitectura de Transición (Ollama -> LiteRT)

### Estrategia de Migración
Se está realizando una migración del motor de inferencia local de **Ollama** a **LiteRT (Google AI Edge)** usando el modelo **Gemma 4:E2B**. El objetivo es aprovechar el "Function Calling" nativo y la integración multimodal directa de este modelo.

### Mandatos Técnicos de la Migración
1.  **Eliminación de Parseo JSON:** No usar `parse_gemma_response` ni prompts que fuercen bloques ` ```json `. LiteRT debe invocar herramientas mediante docstrings de Python.
2.  **Ciclo de Vida del Modelo:** El `Engine` de LiteRT debe cargarse una única vez en el `lifespan` de FastAPI y compartirse mediante el estado de la aplicación.
3.  **Herramientas Nativas:** Todas las acciones (sistema, visión, diagnóstico) deben ser funciones de Python puras registradas en el cliente LiteRT.

## Herramientas y Capacidades (Nuevas)

| Herramienta | Descripción |
| :--- | :--- |
| `execute_system_command(command: str)` | Ejecuta comandos de la lista blanca (Hyprland, audio, apps). |
| `analyze_screen(region: str)` | Toma una captura de pantalla y la inyecta como imagen en la conversación actual. |
| `get_system_status()` | Devuelve información de hardware (CPU, RAM, Audio) bajo demanda. |
| `read_log_file(service: str)` | Permite al asistente leer logs de systemd para autodiagnóstico. |

## Estándares de Código
-   **Asincronía:** Todas las herramientas y llamadas al modelo deben ser asíncronas (`async/await`). Usar `asyncio.to_thread` para operaciones bloqueantes si es necesario.
-   **Seguridad:** Mantener y aplicar estrictamente la lista blanca de prefijos de comandos en `CommandExecutor`.
-   **TTS:** El motor de voz debe procesar el texto final generado por el modelo tras haber resuelto todas las llamadas a herramientas.

## Próximos Pasos (Hoja de Ruta)
1.  Crear `src/litert_client.py`.
2.  Refactorizar `src/command_executor.py` para exportar funciones individuales.
3.  Adaptar `src/assistant_service.py` para el nuevo flujo de conversación de LiteRT.
4.  Actualizar `src/main.py` para gestionar el `Engine`.

# Plan de Mejora: AsistenteIA

## 🧠 Comprensión Actual del Proyecto

**AsistenteIA** es un orquestador local para Linux (específicamente CachyOS/Hyprland) que funciona como un asistente de voz multimodal. Su flujo se divide en:
1. **Entrada de Audio**: Externa a la aplicación principal (scripts en Bash que graban y transcriben usando `whisper.cpp`).
2. **Orquestador (FastAPI)**: Recibe el texto transcrito en el endpoint `/transcribe`.
3. **Inyección de Contexto**: Recopila sincrónicamente datos de hardware y estado del sistema (CPU, RAM, ventanas activas).
4. **Cerebro (Ollama + Gemma)**: Evalúa el prompt del sistema + contexto + historial + solicitud del usuario y devuelve un JSON estructurado.
5. **Ejecución (Acciones)**: Dependiendo del tipo de acción (`speak`, `execute`, `vision`, `both`):
   - Ejecuta comandos de terminal validados por una lista blanca.
   - Toma capturas de pantalla y hace una segunda consulta visual al LLM si es necesario.
   - Sintetiza una respuesta verbal mediante **Kokoro TTS** o gTTS y la reproduce por PipeWire/Bluetooth.

---

## 🏗️ Análisis Arquitectónico e Implementación: Áreas de Mejora

Si bien el proyecto cumple su función de manera pragmática, presenta varios problemas de nivel profesional (escalabilidad, seguridad y concurrencia) derivados de un diseño acoplado y sincrónico.

### 1. Riesgo Crítico de Seguridad (Shell Injection)
En `src/command_executor.py`, el método `execute` utiliza `subprocess.run(command, shell=True)`. Aunque se usa una lista blanca de prefijos (`ALLOWED_PREFIXES`), esto no protege contra concatenación de comandos.
*Ejemplo:* El LLM podría generar un comando: `chromium https://google.com; rm -rf /`. Como empieza por `chromium`, pasaría la validación inicial y el shell ejecutaría ambas instrucciones.

### 2. Bloqueo del Event Loop (I/O Síncrona en FastAPI)
FastAPI es asíncrono, pero casi todos los módulos internos están haciendo llamadas bloqueantes al sistema operativo:
- `ContextInjector` usa `subprocess.run` varias veces para construir el prompt.
- `CommandExecutor` bloquea mientras ejecuta comandos.
- `AudioManager` y `VisionTool` también usan llamadas bloqueantes.
- `TTSEngine._play_audio` usa `subprocess.Popen(...).wait(timeout=120)`.
Esto significa que durante estos procesos, el servidor FastAPI queda completamente bloqueado, incapaz de responder a `/status` o `/cancel` a tiempo si hay latencia.

### 3. Estado Global y Acoplamiento Fuerte
En `src/main.py`, variables como `audio_manager`, `ollama_client`, `conversation_history` y `processing` se mantienen como variables globales mutables. 
- No hay inyección de dependencias.
- Es imposible testear `main.py` sin mockear variables globales del módulo.
- Si el usuario decide usar dos asistentes o si se corren varios hilos (workers de Uvicorn), el estado se corromperá.

### 4. Hardcoding y Falta de Configuración Centralizada
Rutas, modelos (`ministral-3:3b`), tiempos de timeout y configuraciones (puerto `8765`) están codificados directamente en los archivos en lugar de usar un gestor de configuraciones (`BaseSettings` de Pydantic o `.env`).

### 5. Gestión del Tooling (Herramientas del LLM)
La lógica del tipo de acción (`vision`) está "hardcodeada" en la función principal del enrutador (`_process_transcription`), interrumpiendo el principio de Responsabilidad Única. Debería existir un sistema dinámico de herramientas (Tool Registry) al que el agente llama.

---

## 📋 Plan de Implementación de Cambios (Ruta Profesional)

### Fase 1: Seguridad y Asincronía Base (Crítico)
**Objetivo:** Evitar inyecciones de comandos y liberar el event loop de FastAPI.
1. **Refactorizar `CommandExecutor`**: 
   - Cambiar `shell=True` a `shell=False`.
   - Utilizar `shlex.split(command)` para parsear los comandos de forma segura en listas de argumentos.
2. **Asincronía Total en subprocesos**:
   - Reemplazar todas las instancias de `subprocess.run` y `subprocess.Popen` por `asyncio.create_subprocess_exec` o delegar su ejecución a un pool de hilos (`asyncio.to_thread`) en los módulos: `context_injector.py`, `command_executor.py`, `audio_manager.py` y `vision_tool.py`.
   - Hacer que `TTSEngine` maneje la reproducción de audio de forma verdaderamente asíncrona en lugar de esperar la terminación sincrónicamente.

### Fase 2: Gestión de Estado e Inyección de Dependencias
**Objetivo:** Eliminar variables globales y adoptar las mejores prácticas de FastAPI.
1. **Clase de Estado (Session/State Manager)**:
   - Crear una clase para agrupar el `conversation_history`, el lock asíncrono de procesamiento (`asyncio.Lock`) y la tarea actual (`current_task`).
2. **Inyección de Dependencias (`Depends`)**:
   - Instanciar los clientes (`OllamaClient`, `TTSEngine`, etc.) en el `lifespan` de FastAPI y ponerlos a disposición a través de request state (`request.app.state`) o mediante dependencias de FastAPI.

### Fase 3: Desacoplamiento Arquitectónico (Core logic)
**Objetivo:** Separar la lógica HTTP de la lógica de negocio.
1. **Servicio del Asistente (`AssistantService`)**:
   - Mover la lógica de `_process_transcription` fuera de `main.py` hacia una clase dedicada que coordine todo el proceso de orquestación (LLM -> parseo -> tools -> TTS).
2. **Patrón Strategy/Registry para Herramientas**:
   - Sacar el bloque "hardcodeado" de "vision" y convertir `CommandExecutor` y `VisionTool` en herramientas modulares (`Tool`) que la respuesta de Gemma pueda invocar dinámicamente.

### Fase 4: Configuración y Robustez
**Objetivo:** Flexibilidad para despliegues.
1. **Configuración con Pydantic**:
   - Implementar un módulo `config.py` con `pydantic-settings` para cargar desde un `.env` (puerto, modelo de Ollama, timeouts).
2. **Manejo de Errores Avanzado**:
   - Estandarizar respuestas de error JSON personalizadas usando `Exception Handlers` de FastAPI en lugar de retornar strings en las funciones de contexto.

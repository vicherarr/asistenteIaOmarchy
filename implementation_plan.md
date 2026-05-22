# Implementation Plan – Optimización del pipeline de streaming y TTS

## Objetivo
Mejorar la fluidez del audio al pasar de una frase a la siguiente cuando la IA genera texto en **streaming**. Actualmente hay un retraso perceptible entre frases.

## Cuellos de botella detectados
1. **Bloqueo total en `TTSEngine.speak`** – el worker TTS espera a que la síntesis, reproducción y guardado de WAV finalicen antes de pedir la siguiente frase.
2. **Se abre un `sd.OutputStream` por cada frase** – la inicialización del dispositivo de audio introduce latencia (~50‑150 ms).
3. **Guardado de WAV en disco** después de cada frase – I/O de disco bloquea el flujo.

## Propuesta de solución
### 1. Pipeline de doble cola (síntesis → reproducción)
- **Cola de frases** → `synthesize_only` genera audio (numpy) sin reproducir ni guardar.
- **Cola de audio** → `play_audio_array` reproduce el audio usando **un único** `sd.OutputStream` persistente.
- Así mientras la frase **N** se reproduce, la frase **N+1** ya está sintetizada.

### 2. Nuevas API en `tts_engine.py`
```python
async def synthesize_only(self, text: str) -> Optional[np.ndarray]:
    """Genera el audio como `np.ndarray` sin reproducir ni guardar."""
    ...

async def play_audio_array(self, audio_np: np.ndarray) -> None:
    """Reproduce un array numpy usando un OutputStream reutilizado."""
    ...
```
- `speak` seguirá existiendo para compatibilidad, delegando a las nuevas funciones.

### 3. Modificaciones en `assistant_service.py`
- Reemplazar `_tts_worker` por **dos workers**:
  - `_synth_worker` consume frases de `queue_text`, llama a `tts.synthesize_only` y pone el resultado en `queue_audio`.
  - `_play_worker` consume arrays de `queue_audio` y llama a `tts.play_audio_array`.
- Arrancar ambos workers al inicio del streaming y cerrar limpiamente al terminar.
- Ajustar `_extract_sentences` para cortar también por **coma** cuando el buffer supera ~80 caracteres, evitando frases muy largas que retrasan la síntesis.

### 4. Limpieza de recursos
- Mantener `self._is_playing` y `self._active_stream` a nivel de motor, pero el stream se abre una sola vez al crear `_play_worker` y se cierra al finalizar.
- El guardado de WAV se elimina del camino crítico; solo se guarda opcionalmente para depuración.

## Impacto esperado
| Métrica | Antes | Después |
|---|---|---|
| Gap entre frases | 500‑1500 ms | < 100 ms (audio pre‑sintetizado) |
| Número de `OutputStream` | 1 por frase | 1 persistente |
| I/O a disco | Cada frase | Sólo para depuración |
| Uso de CPU | Picos alternados | Más uniforme (sintetiza y reproduce en paralelo) |

## Verificación
1. **Prueba manual**: iniciar una conversación y comprobar que el audio suena continuo sin silencios perceptibles.
2. **Medición de tiempo**: registrar timestamps de `synthesize_only` y de `play_audio_array` para asegurar solapamiento.
3. **Fallback**: desactivar Kokoro (eliminar `KPipeline`) y verificar que `gTTS` sigue funcionando.
4. **Cancelación**: pulsar `stop` y confirmar que ambos workers se cancelan sin dejar procesos colgados.

## Pasos de implementación
1. Añadir los dos nuevos métodos en `src/tts_engine.py`.
2. Refactorizar `_tts_worker` en `src/assistant_service.py` a dos workers y actualizar la lógica de `process_transcription_stream`.
3. Ajustar `_extract_sentences` para incluir coma y longitud máxima.
4. Actualizar pruebas unitarias (si existen) y documentación.
5. Ejecutar pruebas manuales y ajustar parámetros de buffer si es necesario.

---
**Nota:** Todos los cambios son **asíncronos** y mantienen la compatibilidad con la API existente del proyecto.

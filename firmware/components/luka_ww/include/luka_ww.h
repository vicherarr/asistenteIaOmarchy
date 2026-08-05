/* Detector de wake word: envoltorio en C sobre TFLite Micro.
 *
 * La API se mantiene deliberadamente pequeña —crear, alimentar, reiniciar,
 * destruir— porque cada función de aquí es una función `unsafe` en el lado
 * Rust. Todo lo que se pueda decidir en Rust (umbral, ventana deslizante,
 * refractario) se decide en Rust, donde se puede probar en el PC.
 *
 * Este módulo NO decide si la palabra se ha dicho: solo entrega la
 * probabilidad cruda que saca el modelo, trama a trama.
 */
#pragma once

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Características por franja que genera el frontend. Fijo: todos los modelos
 * de microWakeWord se han entrenado con 40. */
#define LUKA_WW_FEATURE_SIZE 40

/* Cada franja de características cubre 30 ms de audio y avanza 10 ms, así que
 * el detector produce una probabilidad cada 10 ms de audio (dividido por el
 * `stride` interno del modelo). */
#define LUKA_WW_WINDOW_MS 30
#define LUKA_WW_STEP_MS 10

typedef struct luka_ww luka_ww_t;

/* Crea el detector a partir de un modelo .tflite en memoria (flash o RAM).
 *
 * `model` debe seguir siendo válido y alineado a 16 bytes mientras viva el
 * detector: TFLite no copia el flatbuffer, lo lee en sitio.
 * `arena_bytes` es el tamaño del arena de tensores; si se queda corto, la
 * creación falla y hay que subirlo (el manifiesto del modelo lo indica).
 *
 * Devuelve NULL si el modelo no es válido o no hay memoria. */
luka_ww_t *luka_ww_create(const uint8_t *model, size_t arena_bytes);

void luka_ww_destroy(luka_ww_t *ww);

/* Alimenta PCM mono de 16 kHz y escribe en `probabilities` una probabilidad
 * (0-255) por cada inferencia que haya salido.
 *
 * Puede escribir 0, 1 o varias: el frontend acumula muestras hasta completar
 * una franja, y el modelo solo infiere cada `stride` franjas. Con tramas de
 * 20 ms sale una probabilidad cada 2-3 tramas, no una por trama.
 *
 * Devuelve cuántas ha escrito, o -1 si la inferencia falló. */
int luka_ww_process(luka_ww_t *ww, const int16_t *pcm, size_t samples, uint8_t *probabilities,
                    size_t max_probabilities);

/* Olvida el estado interno del modelo y del frontend.
 *
 * Hay que llamarlo al salir de reposo: si no, el detector arrastra el eco de
 * lo que sonó mientras Luka hablaba y puede autodispararse. */
void luka_ww_reset(luka_ww_t *ww);

/* Bytes de arena realmente usados. Solo para el log de arranque: sirve para
 * ajustar `arena_bytes` sin ir a ciegas. */
size_t luka_ww_arena_used(const luka_ww_t *ww);

#ifdef __cplusplus
}
#endif

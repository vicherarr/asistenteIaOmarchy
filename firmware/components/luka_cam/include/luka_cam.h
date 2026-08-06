/* Cámara GC0308, envuelta en la API más pequeña que sirve.
 *
 * Existe por la misma razón que `luka_ww`: `camera_config_t` es una estructura
 * grande y llena de enumerados del ESP-IDF, y replicarla en Rust a mano es la
 * clase de error que no se ve al compilar y aparece como una imagen en negro.
 * Aquí el lado C usa la cabecera de verdad y hacia Rust solo cruzan punteros y
 * enteros.
 *
 * El pinout lo pasa Rust, que es donde vive la fuente de verdad de la placa
 * (`luka_board`): así este archivo no repite ningún número. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Pines del interfaz DVP, tal y como los tiene `luka_board::camera`. */
typedef struct {
    int xclk;
    int pclk;
    int vsync;
    int href;
    int data[8]; /* D0..D7 */
    /* Puerto I2C **ya inicializado** por el firmware, no pines sueltos.
     *
     * El SCCB del sensor cuelga del mismo bus que los codecs y el expansor. Si
     * el driver de cámara abre su propio bus sobre esos pines, hay dos
     * periféricos manejando las mismas líneas: el ES7210 se corrompe y el
     * micrófono se queda MUDO en cuanto alguien vuelve a tocar el I2C —que es
     * lo que hace el firmware al abrir un turno, cerrando el amplificador.
     *
     * El síntoma era desconcertante: la palabra "Luka" se oía (venía del
     * pre-roll, grabado antes) y a partir de ahí solo silencio. */
    int sccb_port;
    int xclk_hz;
} luka_cam_pins_t;

/* Arranca el sensor.
 *
 * Devuelve 0 si todo fue bien, o el código de error del ESP-IDF.
 *
 * **El sensor tiene que estar ya despierto**: `power_down` a bajo, `select` a
 * alto y fuera de reset. Esas tres líneas cuelgan del TCA9555 y las mueve Rust
 * antes de llamar aquí, porque el bus I2C lo posee él. */
int luka_cam_init(const luka_cam_pins_t *pins);

/* Captura un fotograma y lo entrega ya comprimido en JPEG.
 *
 * El GC0308 **no lleva compresor JPEG** (a diferencia del OV2640): solo saca
 * RGB565/YUV422. Una VGA cruda son 614 kB, que por el enlace del dispositivo son
 * casi veinte segundos. Comprimida ronda los 50 kB. Por eso la compresión se
 * hace aquí y no es opcional.
 *
 * `out` apunta a memoria que hay que devolver con `luka_cam_release`. Devuelve 0
 * si todo fue bien. */
int luka_cam_capture_jpeg(uint8_t **out, size_t *out_len, int quality);

/* Libera lo que devolvió `luka_cam_capture_jpeg`. */
void luka_cam_release(uint8_t *buf);

/* Ancho y alto del último fotograma, para el log y para la cabecera del envío. */
void luka_cam_last_size(int *width, int *height);

#ifdef __cplusplus
}
#endif

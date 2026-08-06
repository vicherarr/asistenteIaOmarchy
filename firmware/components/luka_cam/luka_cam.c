#include "luka_cam.h"

#include "esp_camera.h"
#include "esp_log.h"

static const char *TAG = "luka_cam";

static int s_width = 0;
static int s_height = 0;

int luka_cam_init(const luka_cam_pins_t *p) {
    if (p == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    camera_config_t cfg = {
        .pin_pwdn = -1, /* Cuelgan del TCA9555, no de un GPIO: los mueve Rust */
        .pin_reset = -1,
        .pin_xclk = p->xclk,
        /* -1 = "no abras un bus, usa el que ya hay". Es lo que evita la
         * colisión con los codecs. */
        .pin_sccb_sda = -1,
        .pin_sccb_scl = -1,
        .sccb_i2c_port = p->sccb_port,

        .pin_d7 = p->data[7],
        .pin_d6 = p->data[6],
        .pin_d5 = p->data[5],
        .pin_d4 = p->data[4],
        .pin_d3 = p->data[3],
        .pin_d2 = p->data[2],
        .pin_d1 = p->data[1],
        .pin_d0 = p->data[0],
        .pin_vsync = p->vsync,
        .pin_href = p->href,
        .pin_pclk = p->pclk,

        .xclk_freq_hz = p->xclk_hz,
        .ledc_timer = LEDC_TIMER_1, /* El 0 lo usa el anillo de LEDs */
        .ledc_channel = LEDC_CHANNEL_1,

        /* RGB565 y no JPEG a propósito: el GC0308 no tiene compresor, así que
         * pedir PIXFORMAT_JPEG haría que el driver lo rechazara o lo emulara por
         * detrás sin control. Se comprime explícitamente en `capture_jpeg`. */
        .pixel_format = PIXFORMAT_RGB565,
        /* QVGA y no VGA: a 640x480 el driver rechaza cada fotograma con
         * "FB-SIZE: 599040 != 614400", justo 12 líneas de menos. Es consistente
         * —no es ruido de cableado— y viene de la ventana de salida del GC0308,
         * que no cuadra con lo que el driver espera para VGA.
         *
         * 320x240 sobra para lo que se busca: describirle una escena a un modelo
         * multimodal. Y de paso el JPEG baja a ~15 kB, que por este enlace es
         * medio segundo en vez de dos. */
        .frame_size = FRAMESIZE_QVGA,

        /* Un solo búfer y en PSRAM: no se hace vídeo, se hace una foto suelta.
         * Dos búferes solo servirían para gastar 600 kB más. */
        .fb_count = 1,
        .fb_location = CAMERA_FB_IN_PSRAM,
        .grab_mode = CAMERA_GRAB_WHEN_EMPTY,
        .jpeg_quality = 12,
    };

    esp_err_t err = esp_camera_init(&cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_camera_init falló: %s", esp_err_to_name(err));
        return err;
    }

    sensor_t *s = esp_camera_sensor_get();
    if (s != NULL) {
        ESP_LOGI(TAG, "sensor PID=0x%04x", s->id.PID);
        /* El GC0308 de estos módulos viene montado del revés en muchas placas.
         * Se deja como está: darle la vuelta a ciegas es peor que verlo torcido
         * una vez y corregirlo con conocimiento. */
    }
    ESP_LOGI(TAG, "cámara lista: VGA RGB565 en PSRAM");
    return ESP_OK;
}

int luka_cam_capture_jpeg(uint8_t **out, size_t *out_len, int quality) {
    if (out == NULL || out_len == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    *out = NULL;
    *out_len = 0;

    camera_fb_t *fb = esp_camera_fb_get();
    if (fb == NULL) {
        ESP_LOGE(TAG, "no se pudo capturar el fotograma");
        return ESP_FAIL;
    }

    s_width = (int) fb->width;
    s_height = (int) fb->height;
    ESP_LOGI(TAG, "fotograma %dx%d, %u B en crudo", s_width, s_height, (unsigned) fb->len);

    /* `frame2jpg` reserva el búfer de salida; el llamante lo libera con
     * `luka_cam_release`. Se hace aquí, con el fotograma aún prestado, para
     * poder devolverlo enseguida y no retener 600 kB de PSRAM más de lo justo. */
    bool ok = frame2jpg(fb, quality, out, out_len);
    esp_camera_fb_return(fb);

    if (!ok || *out == NULL) {
        ESP_LOGE(TAG, "la compresión JPEG falló");
        return ESP_FAIL;
    }
    ESP_LOGI(TAG, "JPEG: %u B (calidad %d)", (unsigned) *out_len, quality);
    return ESP_OK;
}

void luka_cam_release(uint8_t *buf) {
    if (buf != NULL) {
        free(buf);
    }
}

void luka_cam_last_size(int *width, int *height) {
    if (width != NULL) {
        *width = s_width;
    }
    if (height != NULL) {
        *height = s_height;
    }
}

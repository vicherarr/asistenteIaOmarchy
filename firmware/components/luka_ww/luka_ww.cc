/* Implementación del detector: frontend de espectrograma + TFLite Micro.
 *
 * La cadena es la misma que usa ESPHome en sus satélites de voz, y eso no es
 * casualidad: el modelo se entrena con unos parámetros de frontend concretos,
 * y si aquí se cambia uno solo (el número de bandas, el suavizado del ruido,
 * el desplazamiento del logaritmo) el modelo recibe unas características que
 * no se parecen a las que vio entrenando. El síntoma sería un detector que
 * nunca dispara, sin ningún error en el log. Por eso las constantes de
 * `preprocessor_settings` están replicadas literalmente y no se tocan.
 */

#include "luka_ww.h"

#include <cstdint>
#include <cstring>
#include <new>

#include "esp_heap_caps.h"
#include "esp_log.h"

#include "frontend.h"
#include "frontend_util.h"

#include "tensorflow/lite/core/c/common.h"
#include "tensorflow/lite/micro/micro_interpreter.h"
#include "tensorflow/lite/micro/micro_mutable_op_resolver.h"
#include "tensorflow/lite/micro/micro_resource_variable.h"
#include "tensorflow/lite/schema/schema_generated.h"

namespace {

const char *TAG = "luka_ww";

/* --- Parámetros del frontend. Deben coincidir con el entrenamiento. --- */
constexpr float FILTERBANK_LOWER_BAND_LIMIT = 125.0f;
constexpr float FILTERBANK_UPPER_BAND_LIMIT = 7500.0f;
constexpr int NOISE_REDUCTION_SMOOTHING_BITS = 10;
constexpr float NOISE_REDUCTION_EVEN_SMOOTHING = 0.025f;
constexpr float NOISE_REDUCTION_ODD_SMOOTHING = 0.06f;
constexpr float NOISE_REDUCTION_MIN_SIGNAL_REMAINING = 0.05f;
constexpr bool PCAN_GAIN_CONTROL_ENABLE_PCAN = true;
constexpr float PCAN_GAIN_CONTROL_STRENGTH = 0.95f;
constexpr float PCAN_GAIN_CONTROL_OFFSET = 80.0f;
constexpr int PCAN_GAIN_CONTROL_GAIN_BITS = 21;
constexpr bool LOG_SCALE_ENABLE_LOG = true;
constexpr int LOG_SCALE_SCALE_SHIFT = 6;

constexpr int SAMPLE_RATE_HZ = 16000;

/* Los modelos en streaming guardan su estado entre inferencias en "resource
 * variables", que viven en un arena aparte del de tensores. 1 kB sobra: son
 * punteros, no datos. */
constexpr size_t VARIABLE_ARENA_SIZE = 1024;
constexpr int MAX_RESOURCE_VARIABLES = 20;

/* Convierte una característica del frontend (uint16, rango ~0-670) al int8
 * cuantizado que espera el modelo.
 *
 * La constante 666 no es una broma: es 25.6 * 26.0 redondeado. El frontend
 * histórico dividía por 25.6 para dar floats de 0 a 26, y la cuantización
 * mapea ese rango a -128..127. Se hace en entero de 32 bits para no meter
 * coma flotante en la ruta de tiempo real. */
inline int8_t feature_to_int8(uint16_t value) {
  constexpr int32_t SCALE = 256;
  constexpr int32_t DIV = 666;
  int32_t v = ((static_cast<int32_t>(value) * SCALE) + (DIV / 2)) / DIV;
  v += INT8_MIN;
  if (v < INT8_MIN) v = INT8_MIN;
  if (v > INT8_MAX) v = INT8_MAX;
  return static_cast<int8_t>(v);
}

/* Reserva primero en RAM interna y, si no cabe, en PSRAM.
 *
 * El orden importa: la inferencia corre cada 10-20 ms y la PSRAM es bastante
 * más lenta. Pero un arena de ~50 kB en interna es mucho pedir cuando ya hay
 * búferes DMA y pilas, así que se acepta la PSRAM antes que no arrancar. */
uint8_t *alloc_arena(size_t bytes, bool *in_psram) {
  auto *p = static_cast<uint8_t *>(heap_caps_malloc(bytes, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
  if (p != nullptr) {
    *in_psram = false;
    return p;
  }
  p = static_cast<uint8_t *>(heap_caps_malloc(bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  *in_psram = true;
  return p;
}

}  // namespace

struct luka_ww {
  FrontendConfig frontend_config;
  FrontendState frontend_state;
  bool frontend_ready = false;

  uint8_t *tensor_arena = nullptr;
  uint8_t *variable_arena = nullptr;
  size_t arena_bytes = 0;

  tflite::MicroInterpreter *interpreter = nullptr;
  tflite::MicroMutableOpResolver<20> *resolver = nullptr;
  tflite::MicroResourceVariables *resource_variables = nullptr;
  tflite::MicroAllocator *variable_allocator = nullptr;

  /* Franjas acumuladas dentro de la entrada del modelo. El modelo no infiere
   * con cada franja: junta `stride` de ellas y entonces invoca. */
  uint8_t current_stride_step = 0;
};

namespace {

/* Los 20 operadores que puede usar un modelo de microWakeWord.
 *
 * Se registran todos aunque un modelo concreto no los use: sobra sitio y la
 * alternativa —afinar la lista por modelo— convierte cada reentrenamiento en
 * un cambio de firmware. Si falta uno, el fallo aparece al cargar el modelo
 * (AllocateTensors), no al compilar. */
bool register_ops(tflite::MicroMutableOpResolver<20> &r) {
  return r.AddCallOnce() == kTfLiteOk && r.AddVarHandle() == kTfLiteOk && r.AddReshape() == kTfLiteOk &&
         r.AddReadVariable() == kTfLiteOk && r.AddStridedSlice() == kTfLiteOk && r.AddConcatenation() == kTfLiteOk &&
         r.AddAssignVariable() == kTfLiteOk && r.AddConv2D() == kTfLiteOk && r.AddMul() == kTfLiteOk &&
         r.AddAdd() == kTfLiteOk && r.AddMean() == kTfLiteOk && r.AddFullyConnected() == kTfLiteOk &&
         r.AddLogistic() == kTfLiteOk && r.AddQuantize() == kTfLiteOk && r.AddDepthwiseConv2D() == kTfLiteOk &&
         r.AddAveragePool2D() == kTfLiteOk && r.AddMaxPool2D() == kTfLiteOk && r.AddPad() == kTfLiteOk &&
         r.AddPack() == kTfLiteOk && r.AddSplitV() == kTfLiteOk;
}

}  // namespace

extern "C" luka_ww_t *luka_ww_create(const uint8_t *model_data, size_t arena_bytes) {
  if (model_data == nullptr || arena_bytes == 0) {
    ESP_LOGE(TAG, "parámetros inválidos");
    return nullptr;
  }

  auto *ww = new (std::nothrow) luka_ww();
  if (ww == nullptr) {
    ESP_LOGE(TAG, "sin memoria para el detector");
    return nullptr;
  }

  /* --- Frontend --- */
  FrontendFillConfigWithDefaults(&ww->frontend_config);
  ww->frontend_config.window.size_ms = LUKA_WW_WINDOW_MS;
  ww->frontend_config.window.step_size_ms = LUKA_WW_STEP_MS;
  ww->frontend_config.filterbank.num_channels = LUKA_WW_FEATURE_SIZE;
  ww->frontend_config.filterbank.lower_band_limit = FILTERBANK_LOWER_BAND_LIMIT;
  ww->frontend_config.filterbank.upper_band_limit = FILTERBANK_UPPER_BAND_LIMIT;
  ww->frontend_config.noise_reduction.smoothing_bits = NOISE_REDUCTION_SMOOTHING_BITS;
  ww->frontend_config.noise_reduction.even_smoothing = NOISE_REDUCTION_EVEN_SMOOTHING;
  ww->frontend_config.noise_reduction.odd_smoothing = NOISE_REDUCTION_ODD_SMOOTHING;
  ww->frontend_config.noise_reduction.min_signal_remaining = NOISE_REDUCTION_MIN_SIGNAL_REMAINING;
  ww->frontend_config.pcan_gain_control.enable_pcan = PCAN_GAIN_CONTROL_ENABLE_PCAN;
  ww->frontend_config.pcan_gain_control.strength = PCAN_GAIN_CONTROL_STRENGTH;
  ww->frontend_config.pcan_gain_control.offset = PCAN_GAIN_CONTROL_OFFSET;
  ww->frontend_config.pcan_gain_control.gain_bits = PCAN_GAIN_CONTROL_GAIN_BITS;
  ww->frontend_config.log_scale.enable_log = LOG_SCALE_ENABLE_LOG;
  ww->frontend_config.log_scale.scale_shift = LOG_SCALE_SCALE_SHIFT;

  if (!FrontendPopulateState(&ww->frontend_config, &ww->frontend_state, SAMPLE_RATE_HZ)) {
    ESP_LOGE(TAG, "no se pudo inicializar el frontend");
    luka_ww_destroy(ww);
    return nullptr;
  }
  ww->frontend_ready = true;

  /* --- Modelo --- */
  const tflite::Model *model = tflite::GetModel(model_data);
  if (model->version() != TFLITE_SCHEMA_VERSION) {
    ESP_LOGE(TAG, "esquema del modelo no soportado (%lu, esperado %d)", (unsigned long) model->version(),
             TFLITE_SCHEMA_VERSION);
    luka_ww_destroy(ww);
    return nullptr;
  }

  ww->variable_arena = static_cast<uint8_t *>(heap_caps_malloc(VARIABLE_ARENA_SIZE, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
  if (ww->variable_arena == nullptr) {
    ESP_LOGE(TAG, "sin memoria para el arena de variables");
    luka_ww_destroy(ww);
    return nullptr;
  }
  ww->variable_allocator = tflite::MicroAllocator::Create(ww->variable_arena, VARIABLE_ARENA_SIZE);
  ww->resource_variables = tflite::MicroResourceVariables::Create(ww->variable_allocator, MAX_RESOURCE_VARIABLES);

  bool in_psram = false;
  ww->tensor_arena = alloc_arena(arena_bytes, &in_psram);
  if (ww->tensor_arena == nullptr) {
    ESP_LOGE(TAG, "sin memoria para el arena de tensores (%u B)", (unsigned) arena_bytes);
    luka_ww_destroy(ww);
    return nullptr;
  }
  ww->arena_bytes = arena_bytes;

  ww->resolver = new (std::nothrow) tflite::MicroMutableOpResolver<20>();
  if (ww->resolver == nullptr || !register_ops(*ww->resolver)) {
    ESP_LOGE(TAG, "no se pudieron registrar los operadores");
    luka_ww_destroy(ww);
    return nullptr;
  }

  ww->interpreter = new (std::nothrow)
      tflite::MicroInterpreter(model, *ww->resolver, ww->tensor_arena, ww->arena_bytes, ww->resource_variables);
  if (ww->interpreter == nullptr || ww->interpreter->AllocateTensors() != kTfLiteOk) {
    /* Casi siempre es el arena corto. Se dice explícitamente porque el error
     * de TFLite por sí solo no lo deja claro. */
    ESP_LOGE(TAG, "AllocateTensors falló: el arena de %u B probablemente se queda corto", (unsigned) arena_bytes);
    luka_ww_destroy(ww);
    return nullptr;
  }

  TfLiteTensor *input = ww->interpreter->input(0);
  TfLiteTensor *output = ww->interpreter->output(0);
  if (input->type != kTfLiteInt8 || output->type != kTfLiteUInt8) {
    ESP_LOGE(TAG, "el modelo no está cuantizado como se espera (entrada int8, salida uint8)");
    luka_ww_destroy(ww);
    return nullptr;
  }
  if (input->dims->size < 3 || input->dims->data[2] != LUKA_WW_FEATURE_SIZE) {
    ESP_LOGE(TAG, "el modelo no espera %d características por franja", LUKA_WW_FEATURE_SIZE);
    luka_ww_destroy(ww);
    return nullptr;
  }

  ESP_LOGI(TAG, "detector listo: stride=%d, arena=%u/%u B (%s)", input->dims->data[1],
           (unsigned) ww->interpreter->arena_used_bytes(), (unsigned) arena_bytes, in_psram ? "PSRAM" : "interna");
  return ww;
}

extern "C" void luka_ww_destroy(luka_ww_t *ww) {
  if (ww == nullptr) return;
  delete ww->interpreter;
  delete ww->resolver;
  if (ww->tensor_arena != nullptr) heap_caps_free(ww->tensor_arena);
  if (ww->variable_arena != nullptr) heap_caps_free(ww->variable_arena);
  if (ww->frontend_ready) FrontendFreeStateContents(&ww->frontend_state);
  delete ww;
}

extern "C" void luka_ww_reset(luka_ww_t *ww) {
  if (ww == nullptr) return;
  FrontendReset(&ww->frontend_state);
  ww->current_stride_step = 0;
  if (ww->interpreter != nullptr) {
    /* Pone a cero las variables de estado del modelo en streaming; sin esto
     * el modelo sigue "oyendo" lo que pasó antes del reinicio. */
    ww->interpreter->Reset();
  }
}

extern "C" int luka_ww_process(luka_ww_t *ww, const int16_t *pcm, size_t samples, uint8_t *probabilities,
                               size_t max_probabilities) {
  if (ww == nullptr || ww->interpreter == nullptr || pcm == nullptr || probabilities == nullptr) {
    return -1;
  }

  size_t written = 0;
  size_t remaining = samples;
  const int16_t *cursor = pcm;

  TfLiteTensor *input = ww->interpreter->input(0);
  const uint8_t stride = static_cast<uint8_t>(input->dims->data[1]);

  while (remaining > 0) {
    size_t consumed = 0;
    FrontendOutput out = FrontendProcessSamples(&ww->frontend_state, cursor, remaining, &consumed);

    /* Puede no consumir nada si aún no hay muestras para una ventana entera;
     * en ese caso el frontend se las queda y se sale del bucle. */
    if (consumed == 0) break;
    cursor += consumed;
    remaining -= consumed;

    if (out.size == 0) continue;

    int8_t *slot = tflite::GetTensorData<int8_t>(input) + LUKA_WW_FEATURE_SIZE * ww->current_stride_step;
    for (size_t i = 0; i < out.size && i < LUKA_WW_FEATURE_SIZE; ++i) {
      slot[i] = feature_to_int8(out.values[i]);
    }
    ++ww->current_stride_step;

    if (ww->current_stride_step >= stride) {
      ww->current_stride_step = 0;
      if (ww->interpreter->Invoke() != kTfLiteOk) {
        ESP_LOGW(TAG, "la inferencia falló");
        return -1;
      }
      if (written < max_probabilities) {
        probabilities[written++] = ww->interpreter->output(0)->data.uint8[0];
      }
    }
  }

  return static_cast<int>(written);
}

extern "C" size_t luka_ww_arena_used(const luka_ww_t *ww) {
  if (ww == nullptr || ww->interpreter == nullptr) return 0;
  return ww->interpreter->arena_used_bytes();
}

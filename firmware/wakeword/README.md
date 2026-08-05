# Wake word "Luka"

Cómo se entrena el modelo que hace que la placa despierte al oír su nombre, y
por qué está hecho así.

El resultado de todo esto es **un fichero de ~40 kB** que se copia a
`crates/luka-wakeword/modelo/luka.tflite` y viaja empotrado en el firmware. No
hay nada que aprovisionar en el dispositivo: reentrenar la palabra es
recompilar y grabar.

## Lo que hace distinto a este entrenamiento

El flujo estándar de [microWakeWord](https://github.com/kahrendt/microWakeWord)
da por hecho que la palabra es inglesa. Sus positivos salen del checkpoint
LibriTTS-R de `piper-sample-generator`: **904 hablantes**, toda la variedad que
uno pueda querer... y solo en inglés. No existe un equivalente en español.

Aquí el corpus se construye con las **8 voces españolas** que hay en Piper
(España, México, Argentina). Son dos órdenes de magnitud menos hablantes, así
que la variedad tiene que venir de otro sitio: velocidades de habla,
reverberación de habitaciones reales, ruido de fondo entre -5 y +10 dB de SNR,
cambios de tono y de EQ. Esa augmentación no es un adorno, es lo que sustituye a
los 900 hablantes que no tenemos.

## El problema de fondo: "Luka" es una palabra corta

Dos sílabas y poca energía. Eso no hace difícil reconocerla; hace difícil **no**
reconocerla de más. Todo lo que sigue está inclinado hacia ese lado:

- Se genera un corpus de **negativos adversarios**: 24 palabras vecinas (`loca`,
  `lupa`, `luna`, `nunca`, `Lucas`...) que entran al entrenamiento con el doble
  de penalización. Es lo que le enseña al modelo dónde está la frontera.
- Los pesos que se guardan se eligen **primero** por bajar de 0,5 falsos
  positivos por hora sobre una grabación de cena real, y solo después por
  reconocer bien. Al revés sale un modelo que dispara con la tele.
- En el dispositivo, el umbral por defecto es alto (200/255) y hay una ventana
  de media móvil y un tiempo refractario. Ver `crates/luka-wakeword/src/decision.rs`.

Y hay un límite que no se puede entrenar: **en español "Luca" suena exactamente
igual que "Luka"**. Si alguien en la sala se llama así, la placa va a despertar,
y no hay modelo que lo arregle. `Lucas` y `Lucía` sí son distinguibles (hay
sonido después) y por eso están entre los negativos.

## Poner en marcha

Todo el trabajo pesado vive **fuera del repo**, en `~/luka-wakeword`: son unos
15 GB entre datasets, features y dos venvs, y este repositorio es público.

```bash
cd firmware/wakeword
./preparar_entorno.sh          # venvs, repos y voces (~10 min, varios GB)
./generar_muestras.sh          # 4000 "Luka" + 3600 vecinas (~20 min)
./descargar_fondos.py          # ruido, música e impulsos de sala
./preparar_features.py         # augmentación -> espectrogramas (~10 min, 6 GB)
./entrenar.sh                  # ~1-2 h en una GPU de escritorio
```

Y luego, al firmware:

```bash
cp ~/luka-wakeword/trained_models/luka/tflite_stream_state_internal_quant/stream_state_internal_quant.tflite \
   ../crates/luka-wakeword/modelo/luka.tflite
```

## Ajustar el umbral sin instrumentar nada

En `cfg.toml`:

```toml
[device]
wake_threshold = 200      # 0-255, sobre la media de la ventana
wake_calibration = true   # el anillo enseña la confianza en violeta
```

Con `wake_calibration = true`, estando en reposo el anillo se convierte en un
vúmetro de **cuánto se parece lo que oye a "Luka"**. Te pones a tres metros, lo
dices, y ves hasta dónde sube. Si llega al final holgadamente, el umbral puede
subir; si se queda a media altura, hay que bajarlo. En violeta y no en cian para
no confundirlo con el vúmetro del micro, que mide otra cosa.

Para el uso diario, `wake_calibration = false`: el reposo tiene que ser
discreto.

## Trampas que ya costaron una vuelta

- **Las libs de CUDA viven dentro de los venvs** y ni PyTorch ni TensorFlow las
  encuentran solos. Sin `LD_LIBRARY_PATH` entrenan en CPU **sin avisar**, con un
  aviso perdido entre cien líneas de log. Los scripts de aquí ya lo ponen.
- **`datasets` está anclado a la serie 3.** Desde la 4 decodificar audio exige
  `torchcodec`, que arrastra PyTorch entero al venv de TensorFlow.
- **HuggingFace devuelve 200 con un JSON de 15 bytes** cuando una ruta ya no
  existe (le pasó a AudioSet, que se sirve en parquet desde hace tiempo). Por
  eso `descargar_fondos.py` comprueba el tamaño de lo que baja.
- **`Clips` no busca en subcarpetas.** Las palabras adversarias viven en una
  carpeta por palabra y necesitan `**/*.wav`, o el corpus sale vacío sin decir
  nada.

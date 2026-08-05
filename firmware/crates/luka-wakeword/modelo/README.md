# El modelo

Aquí va `luka.tflite`: el modelo de wake word cuantizado a int8 y convertido a
streaming, que `lib.rs` empotra en el binario con `include_bytes!`.

Ocupa ~40 kB, así que **se versiona**: es parte del firmware, igual que el
código. Sin él, compilar con la feature `wakeword` (que va activada por defecto)
falla con un `include_bytes!` que no encuentra el fichero.

Se genera con `firmware/wakeword/entrenar.sh` y se copia con:

```bash
cp ~/luka-wakeword/trained_models/luka/tflite_stream_state_internal_quant/stream_state_internal_quant.tflite \
   firmware/crates/luka-wakeword/modelo/luka.tflite
```

Para compilar sin él —el firmware de la Fase 1, solo botón—:

```bash
cargo build -p luka-firmware --no-default-features
```

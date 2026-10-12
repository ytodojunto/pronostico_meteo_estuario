# pronostico_meteo_estuario

Captura el **pronóstico** de presión a nivel del mar y viento a 10 m (0-72 h) en los mismos 8 puntos que
`ERA5_reanalisis_estuario`: norden, buenos_aires, san_clemente, punta_del_este, mar_del_plata,
plataforma_norte, plataforma_sur, bahia_blanca. Sirve para operar el modelo de residuo de marea con
presión y plataforma, entrenado con ERA5 pero alimentado con pronóstico real.

Fuente: [Open-Meteo Single Runs API](https://open-meteo.com/en/docs/single-runs-api) (CC BY 4.0), modelos
`ecmwf_ifs` (IFS HRES 9 km) y `gfs_seamless`. Se pide cada ciclo (00/06/12/18Z) por su hora de inicio,
así que `ciclo_init` es exacto.

## Archivos
- `capturar_pronostico_meteo.py`: el captador. Solo guarda ciclos completos (8 puntos x 73 h x 3 variables), no repite, mira las últimas 48 h para rellenar huecos y no pide un ciclo antes de 4 h de emitido.
- `.github/workflows/capturar_pronostico_meteo.yml`: corre cada hora (minuto 35). A mano admite `modelos`, `desde` (backfill) y `max_ciclos`.
- `test_capturar.py`: pruebas con respuestas simuladas (`python -m unittest test_capturar`).
- `data/pronostico_<modelo>.jsonl`: una línea por ciclo (~11 KB, ~16 MB por año y modelo).
- `data/_debug_primer_pedido_<modelo>.json`: forma real de la primera respuesta (claves, unidades, primeras horas). `data/_debug_error_<modelo>.json` aparece solo si una respuesta no pasó la validación.

## Formato de cada línea
`modelo`, `ciclo_init` (UTC), `capturado_en`, `retraso_captura_h`, `disponible_desde_estimado` (= init + 6 h), `horas` (73),
`puntos.<punto>` = `{lat, lon, grilla_lat, grilla_lon, pressure_msl_hpa[73], wind_speed_ms[73], wind_dir_deg[73]}`.
La posición `i` de cada arreglo vale para `ciclo_init + i horas`. Los nombres de las series son los de las columnas de ERA5
(dirección = de dónde viene el viento).

```python
import json
from datetime import datetime, timedelta
for linea in open("data/pronostico_ecmwf_ifs.jsonl"):
    s = json.loads(linea)
    init = datetime.fromisoformat(s["ciclo_init"])
    p = s["puntos"]["san_clemente"]["pressure_msl_hpa"]      # p[i] -> init + i h
```

**Para validar un modelo:** usar `disponible_desde_estimado` como hora de emisión (no `capturado_en`, que en un backfill
es posterior). Es el mismo criterio de `publicado_en` en `SMN_captura_viento`.

## Puesta en marcha
1. Crear el repo `pronostico_meteo_estuario` en GitHub (público, como los demás) y subir estos archivos.
2. Settings → Actions → General → Workflow permissions: *Read and write*.
3. Actions → *Capturar pronóstico meteorológico* → Run workflow (sin parámetros). Revisar `data/_debug_primer_pedido_*.json`: ahí se ve el formato real. Si un modelo falla, el log dice por qué y el workflow queda en rojo.
4. Backfill (opcional): Run workflow con `desde` = `2026-04-02` (la mayoría de los modelos se archivan desde el 2/4/2026; `ecmwf_ifs` desde el 14/3/2024) y `max_ciclos` = 40; repetir hasta que el log diga 0 pendientes. Cada ciclo cuenta 8 llamadas contra el límite diario de 10.000.

## Límites
- La API gratuita es solo para **uso no comercial**. Si el producto pasa a ser pago hace falta una clave comercial de Open-Meteo, o migrar a datos abiertos directos (ECMWF open data, NOAA GFS en AWS).
- La disponibilidad de 4-6 h es la que informa Open-Meteo; el captador mide el real en `retraso_captura_h` (el primer ciclo disponible en cada corrida horaria).
- Mantener sincronizada la lista `PUNTOS` con `ERA5_reanalisis_estuario/capturar_era5.py`.
- Los archivos históricos de las corridas anteriores a abril de 2026 pueden ser reanálisis/hindcasts y no el pronóstico operativo exacto.

"""
Captura de PRONÓSTICO meteorológico (presión a nivel del mar + viento a 10 m)
en los 8 puntos de ERA5 (estuario, boca y plataforma), desde la Single Runs API
de Open-Meteo. Es el equivalente "pronosticado" de ERA5_reanalisis_estuario:
mismas variables, mismos puntos, mismas unidades, para poder entrenar con ERA5
y operar con pronóstico real.

Por qué la Single Runs API y no la API común: la común no dice de qué corrida
sale cada hora, y para validar un modelo hay que saber cuándo se emitió el
pronóstico. Acá se pide cada corrida por su hora de inicio (`run=`), así que
`ciclo_init` es exacto.

Reglas (mismo criterio que SMN_captura_viento v2):
  - Solo se guarda un ciclo COMPLETO: 8 puntos x 73 horas (0..72) x 3 variables,
    sin vacíos. Si falta algo, no se escribe nada y se reintenta en la próxima.
  - No repite: si (modelo, ciclo_init) ya está con 73 horas, se saltea.
  - Mira los últimos LOOKBACK_H horas, así rellena ciclos que se hayan perdido.
  - Un ciclo no se pide antes de ESPERA_MIN_H horas después de su inicio (la API
    dice que los modelos globales tardan "típicamente 4-6 h" en estar).
  - Backfill: `--desde AAAA-MM-DD` recorre ciclos viejos (del más viejo al más
    nuevo, hasta --max-ciclos por corrida) y se puede repetir hasta completar.

Salida: data/pronostico_<modelo>.jsonl, una línea por (modelo, ciclo):
    modelo, ciclo_init (UTC), capturado_en, retraso_captura_h,
    disponible_desde_estimado (= ciclo_init + 6 h; USAR ESTE para validar, no
    capturado_en), horas (73), unidades, puntos
    puntos[<punto>] = {lat, lon, grilla_lat, grilla_lon,
                       pressure_msl_hpa[73], wind_speed_ms[73], wind_dir_deg[73]}
    La hora i del arreglo es valida_para = ciclo_init + i horas. Los nombres de
    las tres series son los mismos que las columnas de ERA5.

Atribución (CC BY 4.0): datos de Open-Meteo.com. La API gratuita es para uso
NO comercial (600 llamadas/min, 5.000/h, 10.000/día); si esto pasa a producto
pago hay que usar una clave comercial o migrar a datos abiertos directos
(ECMWF open data / NOAA GFS en AWS).

NOTA: el formato de respuesta está escrito según la documentación; la primera
corrida real lo valida y falla con un mensaje claro si algo difiere, y guarda
data/_debug_primer_pedido_<modelo>.json con la forma real de la respuesta.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

URL = "https://single-runs-api.open-meteo.com/v1/forecast"
DATA_DIR = Path(__file__).parent / "data"

# Mismos puntos que ERA5_reanalisis_estuario/capturar_era5.py (mantener sincronizados)
PUNTOS = {
    "norden": (-34.629, -57.927),
    "buenos_aires": (-34.561, -58.399),
    "san_clemente": (-36.355, -56.715),
    "punta_del_este": (-34.950, -54.950),
    "mar_del_plata": (-38.000, -57.500),
    "plataforma_norte": (-36.000, -54.000),
    "plataforma_sur": (-39.000, -56.000),
    "bahia_blanca": (-39.300, -61.500),
}
MODELOS_DEFAULT = ("ecmwf_ifs", "gfs_seamless")
HORAS = 73                      # horas 0..72
CICLOS_HORAS = (0, 6, 12, 18)
ESPERA_MIN_H = 4                # no pedir un ciclo antes de init + 4 h
DISPONIBLE_H = 6                # disponibilidad nominal (conservadora) para validar
LOOKBACK_H = 48
MAX_CICLOS = 8
PAUSA_S = 1.0
GRILLA_TOL_GRADOS = 0.3         # la grilla devuelta no puede estar más lejos que esto del punto pedido
VARS = ("pressure_msl", "wind_speed_10m", "wind_direction_10m")
UNIDADES_ESPERADAS = {"pressure_msl": "hPa", "wind_speed_10m": "m/s", "wind_direction_10m": "°"}


class NoDisponible(Exception):
    """El ciclo todavía no está (o ya no está) en la API: se reintenta, no es un error."""


# ---------- histórico ----------

def ruta_modelo(modelo: str) -> Path:
    return DATA_DIR / f"pronostico_{modelo}.jsonl"


def ya_capturados(modelo: str) -> set[str]:
    hechos = set()
    p = ruta_modelo(modelo)
    if not p.exists():
        return hechos
    with p.open(encoding="utf-8") as f:
        for linea in f:
            linea = linea.strip()
            if not linea:
                continue
            try:
                s = json.loads(linea)
            except json.JSONDecodeError:
                continue
            if s.get("horas", 0) >= HORAS and s.get("ciclo_init"):
                hechos.add(datetime.fromisoformat(s["ciclo_init"]).astimezone(timezone.utc).isoformat())
    return hechos


def candidatos(ahora: datetime, lookback_h: int = LOOKBACK_H, desde: datetime | None = None) -> list[datetime]:
    """Inits 00/06/12/18Z ya pedibles (init <= ahora - ESPERA_MIN_H), del más viejo al más nuevo."""
    limite_nuevo = ahora - timedelta(hours=ESPERA_MIN_H)
    t = limite_nuevo.replace(minute=0, second=0, microsecond=0)
    t -= timedelta(hours=t.hour % 6)
    piso = desde if desde else ahora - timedelta(hours=lookback_h)
    out = []
    while t >= piso:
        out.append(t)
        t -= timedelta(hours=6)
    return sorted(out)


# ---------- API ----------

def pedir(modelo: str, init: datetime) -> list | dict:
    lats = ",".join(str(la) for la, _ in PUNTOS.values())
    lons = ",".join(str(lo) for _, lo in PUNTOS.values())
    params = {
        "latitude": lats, "longitude": lons,
        "hourly": ",".join(VARS),
        "wind_speed_unit": "ms", "timezone": "UTC",
        "models": modelo, "run": init.strftime("%Y-%m-%dT%H:%M"),
        "forecast_days": 5,
    }
    for intento in range(5):
        r = requests.get(URL, params=params, timeout=180)
        if r.status_code == 429:
            espera = 60 * (intento + 1)
            print(f"    429 (límite de uso), espero {espera}s")
            time.sleep(espera)
            continue
        break
    else:
        raise RuntimeError("Open-Meteo siguió respondiendo 429 tras 5 intentos")
    try:
        j = r.json()
    except ValueError:
        j = None
    if isinstance(j, dict) and j.get("error"):
        # La API devuelve error para corridas que no existen / todavía no están
        raise NoDisponible(str(j.get("reason", j))[:200])
    if r.status_code == 400:
        raise NoDisponible(f"HTTP 400: {r.text[:200]}")
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    if j is None:
        raise RuntimeError(f"respuesta no es JSON: {r.text[:200]}")
    return j


def _serie(h: dict, base: str, modelo: str):
    for k in (base, f"{base}_{modelo}"):
        if k in h:
            return k, h[k]
    raise ValueError(f"falta la variable {base!r} (claves: {sorted(h)})")


def extraer(modelo: str, init: datetime, j) -> dict:
    """Valida la respuesta y devuelve {punto: {...}}. NoDisponible si faltan horas; ValueError si el formato no es el esperado."""
    locs = [j] if isinstance(j, dict) else j
    if len(locs) != len(PUNTOS):
        raise ValueError(f"se esperaban {len(PUNTOS)} ubicaciones y llegaron {len(locs)}")
    tiempos = [(init + timedelta(hours=k)).strftime("%Y-%m-%dT%H:%M") for k in range(HORAS)]
    puntos = {}
    for (nombre, (lat, lon)), loc in zip(PUNTOS.items(), locs):
        h = loc.get("hourly")
        if not h or "time" not in h:
            raise ValueError(f"{nombre}: respuesta sin 'hourly.time' (claves: {sorted(loc)})")
        glat, glon = loc.get("latitude"), loc.get("longitude")
        if glat is None or glon is None or abs(glat - lat) > GRILLA_TOL_GRADOS or abs(glon - lon) > GRILLA_TOL_GRADOS:
            raise ValueError(f"{nombre}: la ubicación devuelta ({glat}, {glon}) no coincide con la pedida ({lat}, {lon}); ¿cambió el orden?")
        unidades = loc.get("hourly_units", {})
        idx = {t: i for i, t in enumerate(h["time"])}
        faltan = [t for t in tiempos if t not in idx]
        if faltan:
            raise NoDisponible(f"{nombre}: la respuesta no cubre {len(faltan)} de {HORAS} horas (primera: {faltan[0]})")
        valores = {}
        for var in VARS:
            clave, serie = _serie(h, var, modelo)
            u = unidades.get(clave)
            if u != UNIDADES_ESPERADAS[var]:
                raise ValueError(f"{nombre}: unidad inesperada para {clave}: {u!r} (esperada {UNIDADES_ESPERADAS[var]!r})")
            vals = [serie[idx[t]] for t in tiempos]
            if any(v is None for v in vals):
                raise NoDisponible(f"{nombre}: {var} con {sum(v is None for v in vals)} valores vacíos")
            valores[var] = vals
        p, v, d = valores["pressure_msl"], valores["wind_speed_10m"], valores["wind_direction_10m"]
        if not all(900 <= x <= 1080 for x in p):
            raise ValueError(f"{nombre}: presión fuera de rango ({min(p)}..{max(p)} hPa)")
        if not all(0 <= x <= 70 for x in v):
            raise ValueError(f"{nombre}: viento fuera de rango ({min(v)}..{max(v)} m/s)")
        if not all(0 <= x <= 360 for x in d):
            raise ValueError(f"{nombre}: dirección fuera de rango ({min(d)}..{max(d)}°)")
        puntos[nombre] = {
            "lat": lat, "lon": lon, "grilla_lat": glat, "grilla_lon": glon,
            "pressure_msl_hpa": [round(x, 1) for x in p],
            "wind_speed_ms": [round(x, 2) for x in v],
            "wind_dir_deg": [int(round(x)) % 360 for x in d],
        }
    return puntos


def resumen_respuesta(j) -> dict:
    """Forma de la respuesta (sin los datos completos) para diagnosticar el formato real."""
    locs = [j] if isinstance(j, dict) else j
    out = {"tipo": "lista" if isinstance(j, list) else "objeto", "n_ubicaciones": len(locs)}
    if locs and isinstance(locs[0], dict):
        l0 = locs[0]
        h = l0.get("hourly", {})
        out["claves_ubicacion"] = sorted(l0)
        out["latitude"], out["longitude"] = l0.get("latitude"), l0.get("longitude")
        out["generationtime_ms"] = l0.get("generationtime_ms")
        out["hourly_units"] = l0.get("hourly_units")
        out["claves_hourly"] = sorted(h)
        out["n_horas"] = len(h.get("time", []))
        out["primeras_horas"] = {k: v[:3] for k, v in h.items() if isinstance(v, list)}
        out["ultimas_horas"] = {k: v[-2:] for k, v in h.items() if isinstance(v, list)}
    return out


def guardar_debug(nombre: str, contenido: dict):
    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / nombre).write_text(json.dumps(contenido, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- ciclo ----------

def capturar_ciclo(modelo: str, init: datetime) -> dict:
    j = pedir(modelo, init)
    primero = DATA_DIR / f"_debug_primer_pedido_{modelo}.json"
    if not primero.exists():
        guardar_debug(primero.name, {"modelo": modelo, "ciclo_init": init.isoformat(), **resumen_respuesta(j)})
    try:
        puntos = extraer(modelo, init, j)
    except ValueError:
        guardar_debug(f"_debug_error_{modelo}.json", {"modelo": modelo, "ciclo_init": init.isoformat(), **resumen_respuesta(j)})
        raise
    ahora = datetime.now(timezone.utc)
    return {
        "modelo": modelo,
        "ciclo_init": init.isoformat(),
        "capturado_en": ahora.isoformat(),
        "retraso_captura_h": round((ahora - init).total_seconds() / 3600, 2),
        "disponible_desde_estimado": (init + timedelta(hours=DISPONIBLE_H)).isoformat(),
        "horas": HORAS,
        "unidades": {"pressure_msl_hpa": "hPa", "wind_speed_ms": "m/s", "wind_dir_deg": "grados, de dónde viene"},
        "fuente": "Open-Meteo Single Runs API (CC BY 4.0)",
        "puntos": puntos,
    }


def guardar(snapshot: dict):
    DATA_DIR.mkdir(exist_ok=True)
    with ruta_modelo(snapshot["modelo"]).open("a", encoding="utf-8") as f:
        f.write(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")) + "\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--modelos", default=os.environ.get("MODELOS") or ",".join(MODELOS_DEFAULT),
                    help="lista separada por comas (ids de Open-Meteo)")
    ap.add_argument("--desde", default=os.environ.get("DESDE") or "", help="backfill: AAAA-MM-DD (UTC)")
    ap.add_argument("--max-ciclos", type=int, default=int(os.environ.get("MAX_CICLOS") or MAX_CICLOS),
                    help="tope de ciclos por modelo en esta corrida")
    args = ap.parse_args(argv)

    ahora = datetime.now(timezone.utc)
    desde = datetime.fromisoformat(args.desde).replace(tzinfo=timezone.utc) if args.desde else None
    modelos = [m.strip() for m in args.modelos.split(",") if m.strip()]
    DATA_DIR.mkdir(exist_ok=True)
    print(f"=== {ahora.isoformat()} — modelos {modelos}" + (f", backfill desde {args.desde}" if desde else "") + " ===")

    errores = 0
    for modelo in modelos:
        hechos = ya_capturados(modelo)
        pend = [i for i in candidatos(ahora, LOOKBACK_H, desde) if i.isoformat() not in hechos][: args.max_ciclos]
        print(f"\n[{modelo}] {len(hechos)} ciclos completos ya guardados, {len(pend)} pendientes en esta corrida")
        guardados, motivo, viejos_fallidos = 0, "", 0
        for init in pend:
            try:
                snap = capturar_ciclo(modelo, init)
            except NoDisponible as e:
                motivo = str(e)
                if ahora - init > timedelta(hours=12):
                    viejos_fallidos += 1
                print(f"  {init:%Y-%m-%d %HZ}: no disponible todavía / incompleto ({e}); se reintenta")
            except (ValueError, RuntimeError, requests.RequestException) as e:
                print(f"  {init:%Y-%m-%d %HZ}: ERROR {e.__class__.__name__}: {e}")
                errores += 1
            else:
                guardar(snap)
                guardados += 1
                print(f"  {init:%Y-%m-%d %HZ}: guardado ({HORAS} h x {len(PUNTOS)} puntos)")
            time.sleep(PAUSA_S)
        # Un modelo que nunca logró guardar nada y falla con ciclos de más de 12 h
        # no está "esperando": probablemente el id del modelo o el formato están mal.
        if not hechos and not guardados and viejos_fallidos:
            print(f"  ERROR: ningún ciclo de {modelo} disponible (último motivo: {motivo}); revisar el id del modelo y los datos de la respuesta (ver data/_debug_*.json)")
            errores += 1
    print(f"\nTerminado con {errores} errores")
    return 1 if errores else 0


if __name__ == "__main__":
    sys.exit(main())

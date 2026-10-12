"""
Chequeo de salud de las capturas (SHN pronóstico, SHN alturas, viento SMN,
CARP, INA, pronóstico meteorológico Open-Meteo). Lee los datos de los repos públicos vía raw.githubusercontent.com,
imprime un reporte, escribe data/health.json y sale con código 1 si hay algún
FAIL — en un workflow programado eso hace que GitHub mande el mail de
"workflow failed". WARN e INFO no hacen fallar el workflow.

Pensado para vivir en su propio repo (ej. ytodojunto/salud_capturas) con el
workflow health_check.yml; no depende de ninguno de los repos que vigila.

Uso: python health_check.py [ruta_salida_json]   (default: data/health.json)
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

RAW = "https://raw.githubusercontent.com/ytodojunto"
UTC = timezone.utc
ART = timezone(timedelta(hours=-3))
AHORA = datetime.now(UTC)

# Umbrales (horas) -> (ok_hasta, warn_hasta); más que warn_hasta = FAIL
SHN_PRONO_EDAD = (16, 26)        # 2 corridas por día
SHN_ALTURAS_EDAD = (16, 30)      # 2 corridas por día
VIENTO_CICLO_EDAD = (13, 19)     # ciclo completo más nuevo: 6 h entre ciclos + publicación
CARP_EDAD = (72, 120)            # fuente con ~1 día de atraso, sync diario
INA_EDAD = (36, 72)
VIENTO_COMPLETOS_OK, VIENTO_COMPLETOS_WARN = 1.0, 0.75   # fracción de ciclos de las últimas 48 h
ARCHIVOS_POR_CICLO = 73
# Pronóstico meteorológico Open-Meteo (repo pronostico_meteo_estuario): un ciclo cada 6 h,
# disponible ~4-6 h después del init y capturado con el cron horario
PRONO_METEO_MODELOS = ("ecmwf_ifs", "gfs_seamless")
PRONO_METEO_EDAD = (14, 20)      # edad (h) del init del último ciclo completo
PRONO_METEO_ESPERA_H = 8         # un ciclo "ya debería estar" 8 h después de su init
PRONO_METEO_HORAS, PRONO_METEO_PUNTOS = 73, 8
PRONO_METEO_COLA_BYTES = 262144  # se lee solo el final del jsonl (~25 ciclos), el archivo crece
ALTURAS_COMPLETITUD_WARN = 0.70  # fracción de las 240 h de la ventana por estación
# Estaciones INA que hoy NO traen pronóstico en el feed capturado (conocido, no alarma)
INA_SIN_PRONOSTICO_CONOCIDAS = {
    "victoria", "san_pedro", "baradero", "ibicuy", "martin_garcia", "buenos_aires", "la_plata",
}

resultados = []  # (nivel, fuente, mensaje)


def anotar(nivel, fuente, msg):
    resultados.append({"nivel": nivel, "fuente": fuente, "mensaje": msg})


def por_edad(horas, umbral):
    return "OK" if horas <= umbral[0] else ("WARN" if horas <= umbral[1] else "FAIL")


def bajar(url, rango=None):
    h = {"Range": rango, "Accept-Encoding": "identity"} if rango else {}
    r = requests.get(url, headers=h, timeout=180)
    r.raise_for_status()
    return r.text


def jsonl(url):
    out = []
    for linea in bajar(url).splitlines():
        linea = linea.strip()
        if linea:
            try:
                out.append(json.loads(linea))
            except json.JSONDecodeError:
                pass
    return out


def iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)


def horas_desde(t):
    return (AHORA - t).total_seconds() / 3600


# ---------------- SHN pronóstico ----------------
def chequear_shn_prono():
    f = "SHN pronóstico"
    s = jsonl(f"{RAW}/SHN_captura_pronosticos/main/data/historico.jsonl")
    if not s:
        return anotar("FAIL", f, "histórico vacío o ilegible")
    ult = max(iso(x["capturado_en"]) for x in s)
    edad = horas_desde(ult)
    anotar(por_edad(edad, SHN_PRONO_EDAD), f, f"última captura hace {edad:.1f} h ({len(s)} snapshots)")
    n48 = sum(1 for x in s if horas_desde(iso(x["capturado_en"])) <= 48)
    if n48 < 3:
        anotar("WARN", f, f"solo {n48} snapshots en las últimas 48 h (esperados ~4)")
    lugares = {p["lugar"] for p in s[-1].get("puertos", [])}
    if len(lugares) < 4:
        anotar("WARN", f, f"el último snapshot trae {len(lugares)} puertos (esperados 4)")
    if all(x.get("correccion_interior") is None and x.get("correccion_exterior") is None for x in s):
        anotar("INFO", f, "correccion_interior/exterior son null en TODOS los snapshots (parser o campo ausente)")


# ---------------- SHN alturas ----------------
def parsear_art(txt):
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(txt, fmt).replace(tzinfo=ART).astimezone(UTC)
        except ValueError:
            pass
    return None


def chequear_shn_alturas():
    f = "SHN alturas"
    s = jsonl(f"{RAW}/SHN_captura_pronosticos/main/data/historico_alturas.jsonl")
    if not s:
        return anotar("FAIL", f, "histórico vacío o ilegible")
    ult = s[-1]
    edad = horas_desde(iso(ult["capturado_en"]))
    anotar(por_edad(edad, SHN_ALTURAS_EDAD), f, f"última captura hace {edad:.1f} h ({len(s)} snapshots)")
    por_est = {}
    for m in ult.get("mediciones", []):
        t = parsear_art(m["fecha_hora"])
        if t:
            por_est.setdefault(m["estacion"], []).append(t)
    if len(por_est) < 11:
        anotar("WARN", f, f"el último snapshot trae {len(por_est)} estaciones (esperadas 11)")
    for est, ts in sorted(por_est.items()):
        comp = len(ts) / 240
        ultima = max(ts)
        if horas_desde(ultima) > SHN_ALTURAS_EDAD[1]:
            anotar("FAIL", f, f"{est}: última observación hace {horas_desde(ultima):.0f} h")
        elif comp < ALTURAS_COMPLETITUD_WARN:
            anotar("WARN", f, f"{est}: solo {comp:.0%} de las 240 h de la ventana")
    cuerpo = max((max(v) for v in por_est.values()), default=None)
    if cuerpo:
        anotar("INFO", f, f"observación más reciente hace {horas_desde(cuerpo):.1f} h")


# ---------------- Viento SMN ----------------
def chequear_viento():
    f = "Viento SMN"
    s = jsonl(f"{RAW}/SMN_captura_viento/main/data/historico_viento.jsonl")
    if not s:
        return anotar("FAIL", f, "histórico vacío o ilegible")
    completos = {}
    for x in s:
        if x.get("archivos_procesados", 0) >= ARCHIVOS_POR_CICLO:
            completos[iso(x["ciclo_init"])] = x
    if not completos:
        return anotar("FAIL", f, "ningún ciclo completo en el histórico")
    ult_init = max(completos)
    edad = horas_desde(ult_init)
    anotar(por_edad(edad, VIENTO_CICLO_EDAD), f,
           f"ciclo completo más nuevo: {ult_init:%d/%m %Hz}, init hace {edad:.1f} h")
    # ciclos de las últimas 48 h que ya tendrían que estar publicados (init ≤ ahora − 7 h)
    t = AHORA.replace(minute=0, second=0, microsecond=0)
    t -= timedelta(hours=t.hour % 6)
    esperados = []
    while t >= AHORA - timedelta(hours=48):
        if horas_desde(t) >= 7:
            esperados.append(t)
        t -= timedelta(hours=6)
    faltan = [e for e in esperados if e not in completos]
    frac = 1 - len(faltan) / len(esperados) if esperados else 1.0
    msg = f"{len(esperados) - len(faltan)}/{len(esperados)} ciclos completos en las últimas 48 h"
    if faltan:
        msg += " — faltan: " + ", ".join(f"{e:%d/%m %Hz}" for e in sorted(faltan))
    nivel = "OK" if frac >= VIENTO_COMPLETOS_OK else ("WARN" if frac >= VIENTO_COMPLETOS_WARN else "FAIL")
    anotar(nivel, f, msg)
    inc = [x for x in s[-12:] if x.get("archivos_procesados", 0) < ARCHIVOS_POR_CICLO]
    if inc:
        anotar("INFO", f, f"{len(inc)} snapshots incompletos entre los últimos 12 (con la v1 del script se guardaban igual)")


# ---------------- CARP ----------------
def chequear_carp():
    f = "CARP"
    for est in ("norden", "colonia", "conchillas", "carmelo"):
        for var in ("tide", "wind"):
            try:
                txt = bajar(f"{RAW}/CARP_historicos_martin_garcia/main/data/{est}_{var}.csv", "bytes=-4096")
                ultima = [l for l in txt.splitlines() if l.strip()][-1]
                t = parsear_art(ultima.split(",")[0].strip())
                if not t:
                    anotar("WARN", f, f"{est}_{var}: no pude leer la fecha de la última fila ({ultima[:40]!r})")
                    continue
                edad = horas_desde(t)
                nivel = por_edad(edad, CARP_EDAD)
                if nivel != "OK" or (est == "norden" and var == "tide"):
                    anotar(nivel, f, f"{est}_{var}: último dato hace {edad / 24:.1f} días")
            except requests.RequestException as e:
                anotar("FAIL", f, f"{est}_{var}: no se pudo leer ({e.__class__.__name__})")


# ---------------- INA ----------------
def chequear_ina():
    f = "INA"
    try:
        c = json.loads(bajar(f"{RAW}/INA_captura_delta_2/main/data/comparacion_prono/comparacion.json"))
    except (requests.RequestException, json.JSONDecodeError) as e:
        return anotar("FAIL", f, f"comparacion.json no legible ({e.__class__.__name__})")
    edad = horas_desde(iso(c["generado_en"]))
    anotar(por_edad(edad, INA_EDAD), f, f"comparacion.json generado hace {edad:.1f} h ({c['n_snapshots']} snapshots)")
    sin = {k for k, v in c["estaciones"].items() if v.get("n_comparaciones", 0) == 0}
    nuevas = sin - INA_SIN_PRONOSTICO_CONOCIDAS
    if nuevas:
        anotar("WARN", f, "estaciones que antes tenían comparaciones y ahora no: " + ", ".join(sorted(nuevas)))
    recuperadas = INA_SIN_PRONOSTICO_CONOCIDAS - sin
    if recuperadas:
        anotar("INFO", f, "estaciones que ahora SÍ tienen pronóstico: " + ", ".join(sorted(recuperadas)))


# ---------------- Pronóstico meteorológico (Open-Meteo) ----------------
def chequear_pronostico_meteo():
    for modelo in PRONO_METEO_MODELOS:
        f = f"Pronóstico meteo {modelo}"
        url = f"{RAW}/pronostico_meteo_estuario/main/data/pronostico_{modelo}.jsonl"
        try:
            txt = bajar(url, f"bytes=-{PRONO_METEO_COLA_BYTES}")
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                anotar("WARN", f, "todavía no hay datos (el repo pronostico_meteo_estuario o el archivo no existen)")
                continue
            raise
        s = []
        for linea in txt.splitlines():
            try:
                s.append(json.loads(linea))
            except json.JSONDecodeError:
                pass          # la primera línea del rango suele venir cortada
        completos = {iso(x["ciclo_init"]): x for x in s if x.get("horas", 0) >= PRONO_METEO_HORAS and x.get("ciclo_init")}
        if not completos:
            anotar("FAIL", f, "ningún ciclo completo en el final del histórico")
            continue
        ult_init = max(completos)
        edad = horas_desde(ult_init)
        anotar(por_edad(edad, PRONO_METEO_EDAD), f, f"ciclo completo más nuevo: {ult_init:%d/%m %Hz}, init hace {edad:.1f} h")
        t = AHORA.replace(minute=0, second=0, microsecond=0)
        t -= timedelta(hours=t.hour % 6)
        esperados = []
        while t >= AHORA - timedelta(hours=48):
            if horas_desde(t) >= PRONO_METEO_ESPERA_H:
                esperados.append(t)
            t -= timedelta(hours=6)
        faltan = [e for e in esperados if e not in completos]
        frac = 1 - len(faltan) / len(esperados) if esperados else 1.0
        msg = f"{len(esperados) - len(faltan)}/{len(esperados)} ciclos completos en las últimas 48 h"
        if faltan:
            msg += " — faltan: " + ", ".join(f"{e:%d/%m %Hz}" for e in sorted(faltan))
        nivel = "OK" if frac >= VIENTO_COMPLETOS_OK else ("WARN" if frac >= VIENTO_COMPLETOS_WARN else "FAIL")
        anotar(nivel, f, msg)
        ultimo = completos[ult_init]
        if len(ultimo.get("puntos", {})) < PRONO_METEO_PUNTOS:
            anotar("WARN", f, f"el último ciclo trae {len(ultimo.get('puntos', {}))} puntos (esperados {PRONO_METEO_PUNTOS})")
        retrasos = [x["retraso_captura_h"] for x in list(completos.values())[-8:] if "retraso_captura_h" in x]
        if retrasos:
            anotar("INFO", f, f"retraso medio de captura (desde el init) en los últimos {len(retrasos)} ciclos: {sum(retrasos) / len(retrasos):.1f} h")


def main():
    for fn in (chequear_shn_prono, chequear_shn_alturas, chequear_viento, chequear_carp, chequear_ina,
               chequear_pronostico_meteo):
        try:
            fn()
        except Exception as e:  # un chequeo roto no tapa a los demás
            anotar("FAIL", fn.__name__, f"el chequeo mismo falló: {e.__class__.__name__}: {e}")

    orden = {"FAIL": 0, "WARN": 1, "OK": 2, "INFO": 3}
    print(f"=== Chequeo de salud {AHORA:%Y-%m-%d %H:%M} UTC ===")
    for r in sorted(resultados, key=lambda r: (orden[r["nivel"]], r["fuente"])):
        print(f"[{r['nivel']:4s}] {r['fuente']}: {r['mensaje']}")
    fails = [r for r in resultados if r["nivel"] == "FAIL"]
    warns = [r for r in resultados if r["nivel"] == "WARN"]
    resumen = {"generado_en": AHORA.isoformat(), "fails": len(fails), "warns": len(warns), "detalle": resultados}
    salida = Path(sys.argv[1] if len(sys.argv) > 1 else "data/health.json")
    salida.parent.mkdir(parents=True, exist_ok=True)
    salida.write_text(json.dumps(resumen, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{len(fails)} FAIL, {len(warns)} WARN -> {salida}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

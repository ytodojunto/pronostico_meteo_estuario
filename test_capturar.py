"""Pruebas con respuestas simuladas de la API (no tocan la red). Correr: python -m unittest -v test_capturar"""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import capturar_pronostico_meteo as c

INIT = datetime(2026, 10, 12, 0, 0, tzinfo=timezone.utc)


class Resp:
    def __init__(self, j, status=200):
        self._j, self.status_code = j, status
        self.text = json.dumps(j)

    def json(self):
        return self._j


def respuesta(init=INIT, sufijo="", horas_total=120, desplazar_orden=False, unidad_p="hPa", hueco=None, p0=1010.0):
    """Lista de 8 ubicaciones como la devolvería Open-Meteo con timezone=UTC."""
    base = init.replace(hour=0)
    from datetime import timedelta
    tiempos = [(base + timedelta(hours=k)).strftime("%Y-%m-%dT%H:%M") for k in range(horas_total)]
    locs = []
    for n, (nombre, (la, lo)) in enumerate(c.PUNTOS.items()):
        h = {"time": tiempos,
             f"pressure_msl{sufijo}": [p0 + n + 0.04 * k for k in range(horas_total)],
             f"wind_speed_10m{sufijo}": [3.0 + n * 0.1 + 0.01 * k for k in range(horas_total)],
             f"wind_direction_10m{sufijo}": [(100 + 3 * k + n) % 360 for k in range(horas_total)]}
        if hueco:
            h[hueco[0] + sufijo][hueco[1]] = None
        locs.append({"latitude": la + 0.02, "longitude": lo - 0.02, "generationtime_ms": 1.2,
                     "hourly_units": {f"pressure_msl{sufijo}": unidad_p, f"wind_speed_10m{sufijo}": "m/s",
                                      f"wind_direction_10m{sufijo}": "°", "time": "iso8601"},
                     "hourly": h})
    if desplazar_orden:
        locs = locs[1:] + locs[:1]
    return locs


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(c, "DATA_DIR", Path(self.tmp.name)); p.start(); self.addCleanup(p.stop)
        s = mock.patch.object(c.time, "sleep", lambda *_: None); s.start(); self.addCleanup(s.stop)


class TestExtraer(unittest.TestCase):
    def test_ok_y_formato(self):
        pts = c.extraer("ecmwf_ifs", INIT, respuesta())
        self.assertEqual(set(pts), set(c.PUNTOS))
        n = pts["norden"]
        self.assertEqual(len(n["pressure_msl_hpa"]), 73)
        self.assertEqual(n["pressure_msl_hpa"][0], 1010.0)
        self.assertEqual(n["pressure_msl_hpa"][72], round(1010.0 + 0.04 * 72, 1))
        self.assertEqual(n["wind_dir_deg"][1], 103)
        self.assertAlmostEqual(n["grilla_lat"], c.PUNTOS["norden"][0] + 0.02)

    def test_claves_con_sufijo_de_modelo(self):
        pts = c.extraer("gfs_seamless", INIT, respuesta(sufijo="_gfs_seamless"))
        self.assertEqual(len(pts["bahia_blanca"]["wind_speed_ms"]), 73)

    def test_ciclo_con_inicio_a_mitad_de_la_serie(self):
        # corrida 12Z: la serie arranca a las 00 del día y el ciclo empieza en el índice 12
        init = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
        pts = c.extraer("ecmwf_ifs", init, respuesta(init=init))
        self.assertEqual(pts["norden"]["pressure_msl_hpa"][0], round(1010.0 + 0.04 * 12, 1))

    def test_respuesta_corta_es_no_disponible(self):
        with self.assertRaises(c.NoDisponible):
            c.extraer("ecmwf_ifs", INIT, respuesta(horas_total=60))

    def test_valor_vacio_es_no_disponible(self):
        with self.assertRaises(c.NoDisponible):
            c.extraer("ecmwf_ifs", INIT, respuesta(hueco=("pressure_msl", 30)))

    def test_orden_de_ubicaciones_cambiado(self):
        with self.assertRaisesRegex(ValueError, "no coincide"):
            c.extraer("ecmwf_ifs", INIT, respuesta(desplazar_orden=True))

    def test_unidad_inesperada(self):
        with self.assertRaisesRegex(ValueError, "unidad inesperada"):
            c.extraer("ecmwf_ifs", INIT, respuesta(unidad_p="Pa"))

    def test_presion_fuera_de_rango(self):
        with self.assertRaisesRegex(ValueError, "presión fuera de rango"):
            c.extraer("ecmwf_ifs", INIT, respuesta(p0=101000.0))

    def test_objeto_unico_no_alcanza(self):
        with self.assertRaisesRegex(ValueError, "se esperaban 8"):
            c.extraer("ecmwf_ifs", INIT, respuesta()[0])


class TestCandidatos(unittest.TestCase):
    def test_espera_minima_y_lookback(self):
        ahora = datetime(2026, 10, 12, 14, 20, tzinfo=timezone.utc)
        ini = c.candidatos(ahora)
        self.assertEqual(ini[-1], datetime(2026, 10, 12, 6, tzinfo=timezone.utc))   # el 12Z (2 h 20) todavía no
        self.assertEqual(ini[0], datetime(2026, 10, 10, 18, tzinfo=timezone.utc))   # 48 h para atrás
        self.assertEqual(len(ini), 7)

    def test_justo_a_las_4_horas(self):
        ahora = datetime(2026, 10, 12, 16, 0, tzinfo=timezone.utc)
        self.assertEqual(c.candidatos(ahora)[-1], datetime(2026, 10, 12, 12, tzinfo=timezone.utc))

    def test_backfill_desde(self):
        ahora = datetime(2026, 10, 12, 14, 20, tzinfo=timezone.utc)
        ini = c.candidatos(ahora, desde=datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.assertEqual(ini[0], datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.assertEqual(len(ini), 11 * 4 + 2)


class TestMain(Base):
    AHORA = datetime(2026, 10, 12, 14, 20, tzinfo=timezone.utc)

    def _correr(self, fn_get, argv=("--modelos", "ecmwf_ifs")):
        class F(datetime):
            @classmethod
            def now(cls, tz=None):
                return TestMain.AHORA
        with mock.patch.object(c, "datetime", F), mock.patch.object(c.requests, "get", side_effect=fn_get):
            return c.main(list(argv))

    def _get_ok(self, url, params=None, timeout=None):
        init = datetime.strptime(params["run"], "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
        return Resp(respuesta(init=init))

    def test_captura_y_no_duplica(self):
        self.assertEqual(self._correr(self._get_ok), 0)
        lineas = c.ruta_modelo("ecmwf_ifs").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lineas), 7)
        s = json.loads(lineas[-1])
        self.assertEqual(s["ciclo_init"], "2026-10-12T06:00:00+00:00")
        self.assertEqual(s["disponible_desde_estimado"], "2026-10-12T12:00:00+00:00")
        self.assertEqual(s["horas"], 73)
        self.assertTrue((Path(self.tmp.name) / "_debug_primer_pedido_ecmwf_ifs.json").exists())
        # segunda corrida: nada nuevo, ni una llamada a la API
        llamadas = []
        self.assertEqual(self._correr(lambda *a, **k: llamadas.append(1)), 0)
        self.assertEqual(llamadas, [])
        self.assertEqual(len(c.ruta_modelo("ecmwf_ifs").read_text(encoding="utf-8").splitlines()), 7)

    def test_ciclo_no_publicado_se_reintenta_sin_error(self):
        def get(url, params=None, timeout=None):
            if params["run"] >= "2026-10-12T06:00":
                return Resp({"error": True, "reason": "run not available"}, 400)
            return self._get_ok(url, params)
        self.assertEqual(self._correr(get), 0)
        lineas = c.ruta_modelo("ecmwf_ifs").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lineas), 6)
        self.assertEqual(json.loads(lineas[-1])["ciclo_init"], "2026-10-12T00:00:00+00:00")

    def test_parcial_no_se_guarda(self):
        # datos siempre incompletos en ciclos viejos de un modelo sin historia = problema real, hace fallar
        self.assertEqual(self._correr(lambda u, params=None, timeout=None: Resp(respuesta(init=datetime.strptime(params["run"], "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc), hueco=("wind_speed_10m", 40)))), 1)
        self.assertFalse(c.ruta_modelo("ecmwf_ifs").exists())

    def test_modelo_que_nunca_anda_hace_fallar(self):
        rc = self._correr(lambda u, params=None, timeout=None: Resp({"error": True, "reason": "invalid models"}, 400))
        self.assertEqual(rc, 1)

    def test_formato_inesperado_hace_fallar_y_deja_debug(self):
        rc = self._correr(lambda u, params=None, timeout=None: Resp(respuesta(unidad_p="Pa")))
        self.assertEqual(rc, 1)
        self.assertTrue((Path(self.tmp.name) / "_debug_error_ecmwf_ifs.json").exists())

    def test_error_5xx_hace_fallar(self):
        rc = self._correr(lambda u, params=None, timeout=None: Resp({"x": 1}, 503))
        self.assertEqual(rc, 1)

    def test_429_reintenta(self):
        n = {"i": 0}
        def get(url, params=None, timeout=None):
            n["i"] += 1
            return Resp({}, 429) if n["i"] == 1 else self._get_ok(url, params)
        self.assertEqual(self._correr(get, ("--modelos", "ecmwf_ifs", "--max-ciclos", "1")), 0)
        self.assertEqual(len(c.ruta_modelo("ecmwf_ifs").read_text(encoding="utf-8").splitlines()), 1)

    def test_backfill_toma_los_mas_viejos_primero(self):
        self.assertEqual(self._correr(self._get_ok, ("--modelos", "gfs_seamless", "--desde", "2026-10-01", "--max-ciclos", "3")), 0)
        inits = [json.loads(l)["ciclo_init"] for l in c.ruta_modelo("gfs_seamless").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(inits, ["2026-10-01T00:00:00+00:00", "2026-10-01T06:00:00+00:00", "2026-10-01T12:00:00+00:00"])

    def test_parametros_de_la_llamada(self):
        vistos = []
        def get(url, params=None, timeout=None):
            vistos.append((url, params)); return self._get_ok(url, params)
        self._correr(get, ("--modelos", "ecmwf_ifs", "--max-ciclos", "1"))
        url, p = vistos[0]
        self.assertEqual(url, c.URL)
        self.assertEqual(len(p["latitude"].split(",")), 8)
        self.assertEqual(p["hourly"], "pressure_msl,wind_speed_10m,wind_direction_10m")
        self.assertEqual((p["wind_speed_unit"], p["timezone"], p["models"]), ("ms", "UTC", "ecmwf_ifs"))


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Volatilidad anualizada y matriz de correlación de los activos en cartera
# (CEDEAR + Acciones argentinas con ADR conocido en EEUU), usando precios
# diarios de:
#   - Alpaca Market Data (primario, EEUU — Cedear directo + Acciones vía
#     ADR mapeado). Free tier generoso (200 req/min), sin cola necesaria.
#     Se pide con adjustment=all (splits + dividendos) — sin esto el día
#     ex-dividendo se ve como una caída de precio real y distorsiona la
#     volatilidad calculada.
#   - Alpha Vantage (fallback secundario, sólo si Alpaca no encuentra el
#     símbolo). Free tier muy limitado (25 req/día) — no cubre BCBA
#     (confirmado en vivo), así que en la práctica se usa poco o nada. Sus
#     precios NO vienen ajustados por dividendos (el endpoint ajustado es
#     premium/pago) — limitación conocida, sin arreglo gratuito disponible.
#
# Volatilidad: log-retornos (ln(P_t/P_t-1)), no retornos simples — es lo
# correcto para anualizar la volatilidad de un proceso geométrico, y se usa
# la misma serie para la matriz de correlación por consistencia.
#
# Se ejecuta siempre a demanda (nunca en el loop de auto-refresh de la app),
# cacheado por ticker en memoria de proceso + un archivo local
# (mercado_cache.json, gitignoreado) como respaldo mientras el contenedor
# esté vivo.

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

try:
    import numpy as np
    import pandas as pd
except ImportError:  # pragma: no cover
    np = None
    pd = None

_DIR = Path(__file__).parent
CACHE_PATH = _DIR / "mercado_cache.json"

ALPACA_BASE = "https://data.alpaca.markets"
ALPHAVANTAGE_BASE = "https://www.alphavantage.co/query"

ALPHAVANTAGE_DAILY_QUOTA = 20  # tope defensivo (el real de Alpha Vantage es 25/día)
CACHE_TTL_HORAS = 48


# ── Mapeo de ticker Cohen → proveedor de mercado ────────────────────────────

TICKER_MAP_OVERRIDES: dict = {
    # completar iterativamente con lo que vaya fallando en corridas reales,
    # ej: "PNIZF": ("alpaca", "XYZ")
}

# Grandes nombres argentinos con ADR conocido en NYSE. Ojo: esto es un proxy
# en USD del ADR, NO el precio local de la acción en BCBA (son activos
# relacionados pero no idénticos, de por medio el arbitraje CCL) — dejarlo
# explícito en la UI.
ACCIONES_ADR_MAP = {
    "GGAL": "GGAL", "YPFD": "YPF", "PAMP": "PAM", "BMA": "BMA",
    "SUPV": "SUPV", "CRES": "CRESY", "EDN": "EDN", "TGSU2": "TGS",
    "IRSA": "IRS", "LOMA": "LOMA", "CEPU": "CEPU", "BBAR": "BBAR",
    "TXAR": "TX",
}


def map_ticker_mercado(ticker: str, tipo: str):
    """Devuelve (proveedor, símbolo) o None si no hay cobertura conocida.
    proveedor en {"alpaca", "alphavantage"}."""
    if ticker in TICKER_MAP_OVERRIDES:
        return TICKER_MAP_OVERRIDES[ticker]
    base, _, suf = ticker.partition(".")
    if suf.upper() == "U":
        # ".U" = liquidación en USD de un instrumento que ya cotiza en
        # dólares (ETFs/CEDEARs cruzados, ej "USO", "SOXX.U", "ABT.U") —
        # vienen tageados "Acciones" en Cohen pero el símbolo de mercado es
        # el ticker base en EEUU.
        return ("alpaca", base)
    if tipo == "Cedear":
        return ("alpaca", ticker)  # mismo símbolo que el subyacente US
    if ticker in ACCIONES_ADR_MAP:
        return ("alpaca", ACCIONES_ADR_MAP[ticker])
    return None  # Acciones sin ADR conocido → sin cobertura


# ── Cache en disco (best-effort) ────────────────────────────────────────────

def _cargar_cache() -> dict:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _guardar_cache(cache: dict) -> None:
    try:
        CACHE_PATH.write_text(json.dumps(cache), encoding="utf-8")
    except Exception:
        pass  # best-effort; no bloquear el flujo si el filesystem es read-only


_CACHE = _cargar_cache()  # dict de módulo — compartido entre todas las sesiones del proceso
_AV_CONTADOR = {"dia": None, "usadas": 0}


def _cache_valido(entry) -> bool:
    if not entry or "ts" not in entry:
        return False
    edad_h = (time.time() - entry["ts"]) / 3600
    return edad_h < CACHE_TTL_HORAS


def _av_cuota_disponible() -> bool:
    hoy = date.today().isoformat()
    if _AV_CONTADOR["dia"] != hoy:
        _AV_CONTADOR["dia"] = hoy
        _AV_CONTADOR["usadas"] = 0
    return _AV_CONTADOR["usadas"] < ALPHAVANTAGE_DAILY_QUOTA


# ── Alpaca ───────────────────────────────────────────────────────────────────

def fetch_precios_alpaca(simbolo: str, n_dias: int, api_key: str, api_secret: str) -> list:
    """1 llamada, trae barras diarias de cierre. [] si falla."""
    desde = (date.today() - timedelta(days=int(n_dias * 1.6) + 10)).isoformat()
    try:
        resp = requests.get(
            f"{ALPACA_BASE}/v2/stocks/{simbolo}/bars",
            params={
                "timeframe": "1Day", "start": desde, "limit": 1000, "feed": "iex",
                # sin esto Alpaca trae precios "raw": el día ex-dividendo se ve
                # como una caída de precio real y distorsiona la volatilidad.
                "adjustment": "all",
            },
            headers={"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": api_secret},
            timeout=15,
        )
        if resp.status_code != 200:
            return []
        bars = resp.json().get("bars", [])
        precios = [{"fecha": b["t"][:10], "close": b["c"]} for b in bars]
        return precios[-n_dias:]
    except Exception:
        return []


# ── Alpha Vantage (fallback) ────────────────────────────────────────────────

def fetch_precios_alphavantage(simbolo: str, n_dias: int, api_key: str) -> list:
    """1 llamada = 1 símbolo, consume cuota diaria. [] si falla o sin cuota."""
    if not _av_cuota_disponible():
        return []
    try:
        resp = requests.get(ALPHAVANTAGE_BASE, params={
            "function": "TIME_SERIES_DAILY", "symbol": simbolo,
            "outputsize": "compact", "apikey": api_key,
        }, timeout=15)
        _AV_CONTADOR["usadas"] += 1
        serie = resp.json().get("Time Series (Daily)", {})
        precios = sorted(
            ({"fecha": f, "close": float(v["4. close"])} for f, v in serie.items()),
            key=lambda x: x["fecha"],
        )
        return precios[-n_dias:]
    except Exception:
        return []


# ── Orquestación ─────────────────────────────────────────────────────────────

def calcular_vol_corr(posiciones: list, n_dias: int, top_n: int,
                       alpaca_key: str, alpaca_secret: str, alphavantage_key: str) -> dict:
    """posiciones: filas de DATA (GNR), con ticker/tipo/valor_usd.

    Devuelve un dict con:
      vol: [{"ticker", "vol_anualizada_pct", "n_obs"}, ...] ordenado desc.
      corr_tickers: lista de tickers (orden de filas/columnas de corr_matrix)
      corr_matrix: matriz NxN de correlación (o None donde no hay dato)
      sin_cobertura: tickers sin proveedor conocido o cuyo fetch falló
      fecha_calculo: timestamp legible
    """
    exposicion, tipos = {}, {}
    for p in posiciones:
        t = p.get("ticker")
        if not t:
            continue
        exposicion[t] = exposicion.get(t, 0) + (p.get("valor_usd") or 0)
        tipos[t] = p.get("tipo", "")

    tickers_ordenados = sorted(exposicion, key=lambda t: -exposicion[t])
    if top_n:
        tickers_ordenados = tickers_ordenados[:top_n]

    global _CACHE
    series, sin_cobertura = {}, []
    alpaca_a_pedir, alphavantage_a_pedir = [], []

    for t in tickers_ordenados:
        entry = _CACHE.get(t)
        if _cache_valido(entry):
            series[t] = entry["precios"]
            continue
        ruteo = map_ticker_mercado(t, tipos.get(t, ""))
        if ruteo is None:
            sin_cobertura.append(t)
            continue
        proveedor, simbolo = ruteo
        (alpaca_a_pedir if proveedor == "alpaca" else alphavantage_a_pedir).append((t, simbolo))

    if alpaca_a_pedir:
        with ThreadPoolExecutor(max_workers=8) as ex:
            futuros = {
                ex.submit(fetch_precios_alpaca, simbolo, n_dias, alpaca_key, alpaca_secret): t
                for t, simbolo in alpaca_a_pedir
            }
            for fut in as_completed(futuros):
                t = futuros[fut]
                precios = fut.result()
                if precios:
                    series[t] = precios
                    _CACHE[t] = {"precios": precios, "ts": time.time()}
                else:
                    sin_cobertura.append(t)

    for t, simbolo in alphavantage_a_pedir:
        if not _av_cuota_disponible():
            sin_cobertura.append(t)
            continue
        precios = fetch_precios_alphavantage(simbolo, n_dias, alphavantage_key)
        if precios:
            series[t] = precios
            _CACHE[t] = {"precios": precios, "ts": time.time()}
        else:
            sin_cobertura.append(t)
        time.sleep(1)  # respetar burst limit (~1 req/seg) de Alpha Vantage

    _guardar_cache(_CACHE)

    fecha_calculo = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    if pd is None or np is None or not series:
        return {
            "vol": [], "corr_tickers": [], "corr_matrix": [],
            "sin_cobertura": sorted(set(sin_cobertura)), "fecha_calculo": fecha_calculo,
        }

    df = pd.DataFrame({t: {p["fecha"]: p["close"] for p in precios} for t, precios in series.items()}).sort_index()
    # Log-retornos (ln(P_t/P_t-1)) en vez de retornos simples: es lo correcto
    # para anualizar volatilidad de un proceso geométrico (y para la matriz
    # de correlación, por consistencia).
    retornos = np.log(df / df.shift(1)).dropna(how="all")

    vol = []
    for t in df.columns:
        serie = retornos[t].dropna()
        if len(serie) < 5:
            continue
        vol.append({
            "ticker": t,
            "vol_anualizada_pct": round(float(serie.std() * (252 ** 0.5) * 100), 2),
            "n_obs": int(len(serie)),
        })
    vol.sort(key=lambda x: -x["vol_anualizada_pct"])

    corr_df = retornos.corr()
    corr_tickers = list(corr_df.columns)
    corr_matrix = [[(round(float(v), 3) if pd.notna(v) else None) for v in row] for row in corr_df.values]

    return {
        "vol": vol,
        "corr_tickers": corr_tickers,
        "corr_matrix": corr_matrix,
        "sin_cobertura": sorted(set(sin_cobertura)),
        "fecha_calculo": fecha_calculo,
    }

#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Dashboard GNR — Streamlit version
# Preserva 100% la funcionalidad y estética del HTML original.
# Para correr: streamlit run streamlit_app.py

import json
import time
import streamlit as st
import requests
from datetime import date, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import transferencias_cohen as tc
import mercado

# ── Configuración ─────────────────────────────────────────────────────────────

COHEN_BASE = "https://connect.cohen.com.ar"
TIPOS_GNR = {"Acciones", "Cedear"}
ARANCEL = 0.989  # 1 - 1.1% comisión al vender
INTERVAL_S = 30 * 60  # 30 minutos

EMAILS_AUTORIZADOS = [
    "federico@qtmcapital.com.ar",
    "matias@qtmcapital.com.ar",
    "ciro@qtmcapital.com.ar",
]

st.set_page_config(
    page_title="Dashboards Cohen – QTM Capital",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
    menu_items={},
)

# ── Auth ───────────────────────────────────────────────────────────────────────


def obtener_token() -> str:
    resp = requests.get(
        "http://72.60.155.149:8000/api/cohen/login-token",
        headers={"x-user": st.secrets["API_USER"], "x-pass": st.secrets["API_PASS"]},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"Login fallido: {data}")
    return data["token"]


# ── Precio MEP ─────────────────────────────────────────────────────────────────


def get_mep(token: str) -> float:
    hoy = datetime.now().strftime("%Y-%m-%dT00:00:00.000Z")
    resp = requests.post(
        f"{COHEN_BASE}/api/moneda/getCotizacionMoneda",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        json={"skip": 0, "take": 100, "order": [], "fechaCotizacion": hoy},
        timeout=15,
    )
    resp.raise_for_status()
    for item in resp.json():
        if item.get("idMoneda") == 1032:
            return float(item.get("cotizacionActual") or 0)
    return 0.0


# ── API helpers ────────────────────────────────────────────────────────────────


def get_comitentes(token: str) -> list:
    resp = requests.get(
        f"{COHEN_BASE}/api/posicion/getComitentesUsuario",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json() if isinstance(resp.json(), list) else []


def get_posiciones(id_comitente: int, token: str) -> list:
    hoy = datetime.now().strftime("%Y-%m-%dT00:00:00.000Z")
    resp = requests.post(
        f"{COHEN_BASE}/api/posicion/listResumen",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        json={
            "skip": 0,
            "take": 500,
            "order": [],
            "idComitente": id_comitente,
            "fechaHasta": hoy,
            "idReporteTipo": 0,
            "comitenteSelected": str(id_comitente),
            "personaSelected": "",
        },
        timeout=20,
    )
    resp.raise_for_status()
    return [r for r in resp.json().get("data", []) if not r.get("esFilaSubtotal")]


def get_ganancia_realizada(id_comitente: int, token: str) -> list:
    hoy = datetime.now().strftime("%Y-%m-%dT00:00:00.000Z")
    try:
        resp = requests.post(
            f"{COHEN_BASE}/api/gananciaRealizada/list",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json={
                "skip": 0,
                "take": 500,
                "order": [],
                "idComitente": id_comitente,
                "fecha": hoy,
                "idReporteTipo": 0,
                "esFondo": False,
            },
            timeout=20,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        return data.get("items", []) if isinstance(data, dict) else []
    except Exception:
        return []


def parsear_comitente(c: dict) -> tuple:
    desc = c.get("desc", "")
    if " - " in desc:
        partes = desc.split(" - ", 1)
        return partes[0].strip(), partes[1].strip()
    return str(c["id"]), desc


# ── Fetch paralelo — GNR ───────────────────────────────────────────────────────


def fetch_gnr(token: str, mep: float, progress_bar=None) -> list:
    comitentes = get_comitentes(token)
    cmap = {c["id"]: parsear_comitente(c) for c in comitentes}
    posiciones = []
    done = 0
    total = len(comitentes)

    def fetch(c):
        return c["id"], get_posiciones(c["id"], token)

    with ThreadPoolExecutor(max_workers=20) as ex:
        futuros = {ex.submit(fetch, c): c for c in comitentes}
        for fut in as_completed(futuros):
            done += 1
            if progress_bar is not None:
                progress_bar.progress(
                    done / total, text=f"GNR: {done}/{total} comitentes"
                )
            try:
                id_com, rows = fut.result()
            except Exception:
                continue
            nro, nombre = cmap.get(id_com, (str(id_com), ""))
            for r in rows:
                tipo = r.get("tipoInstrumento") or r.get(
                    "instrumentoTipoDescripcion", ""
                )
                if tipo not in TIPOS_GNR:
                    continue
                valor_ars = float(
                    r.get("saldoValorizadoARS") or r.get("saldoValorizado") or 0
                )
                valor_neto = round(valor_ars * ARANCEL, 2)
                costo_usd = float(r.get("costoTotalUSD") or 0)
                pnl_usd = float(r.get("rendimientoUSD") or 0)
                # saldoValorizadoUSD viene nativo de Cohen — más preciso que
                # convertir valor_ars con el MEP de hoy (evita una doble
                # conversión y usa el mismo dólar que ya usa Cohen internamente
                # para costoTotalUSD/rendimientoUSD). Pero a veces Cohen lo
                # devuelve en 0/null puntualmente aunque costoTotalUSD y
                # rendimientoUSD sí vengan bien — en ese caso lo reconstruimos
                # con esos dos (costo + P&L), que sí son confiables.
                valor_usd_raw = r.get("saldoValorizadoUSD")
                valor_usd = (
                    round(float(valor_usd_raw), 2)
                    if valor_usd_raw
                    else round(costo_usd + pnl_usd, 2)
                )
                valor_neto_usd = round(valor_usd * ARANCEL, 2)
                posiciones.append(
                    {
                        "id_comitente": id_com,
                        "nro_cuenta": nro,
                        "cliente": nombre,
                        "ticker": r.get("ticker") or r.get("instrumentoSimbolo", ""),
                        "descripcion": r.get("denominacion")
                        or r.get("instrumentoDescripcion", ""),
                        "tipo": tipo,
                        "moneda": r.get("moneda", ""),
                        "cantidad": float(r.get("cantidad") or 0),
                        "precio_ars": float(
                            r.get("cotizacionARS") or r.get("cotizacion") or 0
                        ),
                        "precio_usd": float(r.get("cotizacionUSD") or 0),
                        "valor_ars": valor_ars,
                        "valor_neto": valor_neto,
                        "valor_usd": valor_usd,
                        "valor_neto_usd": valor_neto_usd,
                        "costo_ars": float(r.get("costoTotalARS") or 0),
                        "costo_usd": costo_usd,
                        "pnl_ars": float(r.get("rendimientoARS") or 0),
                        "pnl_pct_ars": float(r.get("rendimientoPctARS") or 0),
                        "pnl_usd": pnl_usd,
                        "pnl_pct_usd": float(r.get("rendimientoPctUSD") or 0),
                        "var_dia_ars": float(r.get("varDiariaARS") or 0),
                        "var_dia_pct": float(r.get("varDiariaPctARS") or 0),
                        "fecha": r.get("fechaCotizacionString") or "",
                    }
                )
    return posiciones


# ── Fetch paralelo — Ganancia Realizada ───────────────────────────────────────


def fetch_gr(token: str, mep: float, progress_bar=None) -> list:
    comitentes = get_comitentes(token)
    cmap = {c["id"]: parsear_comitente(c) for c in comitentes}
    resultado = []
    done = 0
    total = len(comitentes)

    def fetch(c):
        return c["id"], get_ganancia_realizada(c["id"], token)

    with ThreadPoolExecutor(max_workers=20) as ex:
        futuros = {ex.submit(fetch, c): c for c in comitentes}
        for fut in as_completed(futuros):
            done += 1
            if progress_bar is not None:
                progress_bar.progress(
                    done / total, text=f"GR: {done}/{total} comitentes"
                )
            try:
                id_com, items = fut.result()
            except Exception:
                continue
            nro, nombre = cmap.get(id_com, (str(id_com), ""))
            for it in items:
                tipo = it.get("instrumentoTipoDescripcion", "")
                if tipo not in TIPOS_GNR:
                    continue
                gr_ars = float(it.get("gananciaRealizadaMonedaBase") or 0)
                gr_usd = float(it.get("gananciaRealizadaRentabilidad") or 0)
                resultado.append(
                    {
                        "id_comitente": id_com,
                        "nro_cuenta": nro,
                        "cliente": nombre,
                        "ticker": it.get("instrumentoSimbolo", ""),
                        "descripcion": it.get("instrumentoDescripcion", ""),
                        "tipo": tipo,
                        "moneda": it.get("moneda", ""),
                        "cantidad": float(it.get("cantidad") or 0),
                        "fecha": it.get("fecha", "")[:10] if it.get("fecha") else "",
                        "mov_tipo": it.get("movimientoCustodiaTipo", ""),
                        "precio_compra_ars": float(
                            it.get("precioCompraMonedaBase") or 0
                        ),
                        "precio_venta_ars": float(it.get("precioVentaMonedaBase") or 0),
                        "importe_compra_ars": float(
                            it.get("importeCompraMonedaBase") or 0
                        ),
                        "importe_venta_ars": float(
                            it.get("importeVentaMonedaBase") or 0
                        ),
                        # Pese al nombre "Rentabilidad", estos son los mismos
                        # precios/importes de compra-venta pero en dólares (al
                        # tipo de cambio de cada fecha, no al MEP de hoy) —
                        # verificado: gananciaRealizadaMonedaBase/importeCompraMonedaBase
                        # (retorno en $) difiere del retorno implícito en estos
                        # campos exactamente como se espera por la devaluación
                        # entre la fecha de compra y la de venta.
                        "precio_compra_usd": float(
                            it.get("precioCompraRentabilidad") or 0
                        ),
                        "precio_venta_usd": float(
                            it.get("precioVentaRentabilidad") or 0
                        ),
                        "importe_compra_usd": float(
                            it.get("importeCompraRentabilidad") or 0
                        ),
                        "importe_venta_usd": float(
                            it.get("importeVentaRentabilidad") or 0
                        ),
                        "gr_ars": gr_ars,
                        "gr_pct_ars": float(it.get("porcentajeMonedaBase") or 0),
                        "gr_usd": gr_usd,
                        "gr_pct_usd": float(it.get("porcentajeRentabilidad") or 0),
                    }
                )
    return resultado


# ── Carga de datos con caché ───────────────────────────────────────────────────


@st.cache_data(ttl=INTERVAL_S, show_spinner=False)
def cargar_datos():
    """Obtiene token, MEP, GNR y GR.

    Cacheado a nivel de proceso (no de sesión): el primer usuario que entra
    después de que venza el TTL dispara el fetch real; todos los que entran
    mientras tanto reciben el mismo resultado cacheado sin volver a pegarle
    a la API de Cohen. show_spinner=False porque el feedback de progreso
    (status/progress bars) sólo tiene sentido durante el fetch real, nunca
    en un cache-hit.
    """
    status_container = st.empty()
    with status_container.status(
        "⏳ Cargando datos desde Cohen...", expanded=True
    ) as s:
        st.write("🔐 Obteniendo token...")
        token = obtener_token()

        st.write("💵 Obteniendo cotización MEP...")
        mep = get_mep(token)

        st.write("📈 Descargando posiciones (GNR)...")
        pb1 = st.progress(0, text="Iniciando GNR...")
        gnr = fetch_gnr(token, mep, pb1)
        pb1.empty()

        st.write("✅ Descargando Ganancia Realizada...")
        pb2 = st.progress(0, text="Iniciando GR...")
        gr = fetch_gr(token, mep, pb2)
        pb2.empty()

        s.update(label="✅ Datos cargados", state="complete", expanded=False)

    status_container.empty()

    ts = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    return gnr, gr, mep, ts


# ── Carga de datos — Transferencias ─────────────────────────────────────────────
# Igual que GNR: cacheado a nivel de proceso, compartido entre todos los
# usuarios. Se separa en dos cachés con TTL distinto para no repetir el
# fetch de los 7 días completos en cada refresco:
#   - _cargar_tf_base: los primeros (RANGO_DIAS-1) días, sin contar hoy.
#     Cacheado por día (la fecha de hoy es parte de la cache key), así que
#     Streamlit lo recalcula una única vez por día y lo comparte el resto
#     de la jornada.
#   - _cargar_tf_hoy: sólo el día de hoy, con TTL = tc.INTERVAL_S — se
#     recalcula cada N minutos y ese resultado también se comparte entre
#     todos los usuarios hasta el próximo vencimiento.
# cargar_datos_transferencias() sólo concatena ambos resultados.


def _fetch_transferencias_rango(desde: str, hasta: str, status_msg: str):
    status_container = st.empty()
    with status_container.status(status_msg, expanded=True) as s:
        st.write("🔐 Obteniendo token...")
        token = tc.obtener_token(st.secrets["API_USER"], st.secrets["API_PASS"])

        st.write("📋 Obteniendo comitentes...")
        comitentes = tc.get_comitentes(token)
        ids = [c["id"] for c in comitentes]
        cmap = {c["id"]: tc.parsear_comitente(c) for c in comitentes}
        id_usuario = tc.get_id_usuario(token)

        st.write("💸 Descargando transferencias, FCI e ingresos...")
        with ThreadPoolExecutor(max_workers=3) as ex:
            futuros = {
                ex.submit(tc.fetch_transferencias, comitentes, desde, hasta, token): "tf",
                ex.submit(tc.fetch_fci, comitentes, desde, hasta, token, id_usuario): "fci",
                ex.submit(tc.fetch_cta_cte, ids, desde, hasta, token): "ing",
            }
            resultados = {}
            for fut in as_completed(futuros):
                key = futuros[fut]
                try:
                    resultados[key] = fut.result()
                except Exception:
                    resultados[key] = []

        s.update(label="✅ Cargado", state="complete", expanded=False)
    status_container.empty()

    tf  = tc.normalizar_transferencias(resultados.get("tf", []), cmap)
    fci = tc.normalizar_fci(resultados.get("fci", []))
    ing = tc.normalizar_cte(resultados.get("ing", []), cmap)
    return tf, fci, ing


@st.cache_data(ttl=24 * 3600, show_spinner=False)
def _cargar_tf_base(dia_cache_key: str):
    hoy = date.fromisoformat(dia_cache_key)
    desde = (hoy - timedelta(days=tc.RANGO_DIAS - 1)).isoformat()
    ayer = (hoy - timedelta(days=1)).isoformat()
    return _fetch_transferencias_rango(desde, ayer, "⏳ Cargando historial de transferencias...")


@st.cache_data(ttl=tc.INTERVAL_S, show_spinner=False)
def _cargar_tf_hoy():
    hoy_iso = date.today().isoformat()
    tf, fci, ing = _fetch_transferencias_rango(hoy_iso, hoy_iso, "💸 Actualizando movimientos de hoy...")
    ts = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    return tf, fci, ing, ts


def cargar_datos_transferencias():
    hoy = date.today()
    rango_desde = (hoy - timedelta(days=tc.RANGO_DIAS - 1)).isoformat()

    tf_base, fci_base, ing_base = _cargar_tf_base(hoy.isoformat())
    tf_hoy, fci_hoy, ing_hoy, ts = _cargar_tf_hoy()

    tf = tf_base + tf_hoy
    fci = fci_base + fci_hoy
    ing = ing_base + ing_hoy
    return tf, fci, ing, ts, rango_desde


# ── HTML Template ─────────────────────────────────────────────────────────────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dashboard GNR - Cohen</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=DM+Serif+Display&family=DM+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
/* ═══════════════════════ TOKENS (paleta QTM Capital) ═══════════════════════ */
:root{
  --navy:#0F2044; --navy-deep:#0b1830; --gold:#C9A84C; --gold-soft:#e3cd8a;
  --ok:#2e9e6b;   --ok-bg:rgba(46,158,107,.14);
  --warn:#c98a1c;  --warn-bg:rgba(201,138,28,.16);
  --bad:#c4453f;   --bad-bg:rgba(196,69,63,.14);
  --neu:#6b7fa3;   --neu-bg:rgba(107,127,163,.14);
  --r:12px;
  --fh:'DM Serif Display',Georgia,serif;
  --fb:'DM Sans',-apple-system,Segoe UI,sans-serif;
}
body[data-t="dark"]{
  --bg:#0b1322; --sur:#121d30; --sur2:#172541; --sur3:#1e2e50;
  --tx:#eef1f6; --mu:#7a8fb5; --bo:rgba(255,255,255,.09);
  --hd:linear-gradient(135deg,#0b1830,#0F2044 55%,#16243a);
  --sh:0 8px 24px rgba(0,0,0,.35);
}
body[data-t="light"]{
  --bg:#f3f5f9; --sur:#ffffff; --sur2:#f0f2f8; --sur3:#e8eaf2;
  --tx:#16213e; --mu:#5c6880; --bo:#dde0ec;
  --hd:linear-gradient(135deg,#0F2044,#16345c 60%,#1b3a66);
  --sh:0 4px 16px rgba(15,32,68,.08);
}
body{
  background:var(--bg);color:var(--tx);font-family:var(--fb);font-size:.875rem;transition:background .2s,color .2s;
  --bs-body-bg:var(--bg); --bs-body-color:var(--tx); --bs-emphasis-color:var(--tx);
  --bs-secondary-color:var(--mu); --bs-tertiary-color:var(--mu);
  --bs-border-color:var(--bo); --bs-secondary-bg:var(--sur2); --bs-tertiary-bg:var(--sur2);
  --bs-primary:var(--gold);   --bs-primary-rgb:201,168,76;
  --bs-success:var(--ok);     --bs-success-rgb:46,158,107;
  --bs-danger:var(--bad);     --bs-danger-rgb:196,69,63;
  --bs-warning:var(--warn);   --bs-warning-rgb:201,138,28;
  --bs-secondary:var(--neu);  --bs-secondary-rgb:107,127,163;
  --bs-info:var(--neu);       --bs-info-rgb:107,127,163;
  --bs-light:var(--sur2);     --bs-light-rgb:23,37,65;
  --bs-dark:var(--sur);       --bs-dark-rgb:18,29,48;
}
.navbar{background:var(--hd)!important}
.navbar-brand{font-family:var(--fh)}
.ibtn{background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.18);color:#fff;
  width:32px;height:32px;border-radius:8px;display:inline-flex;align-items:center;justify-content:center;
  cursor:pointer;font-size:.9rem;transition:background .15s}
.ibtn:hover{background:rgba(255,255,255,.24)}
.card,.kpi-card{background:var(--sur)!important;border:1px solid var(--bo)!important;box-shadow:var(--sh)!important;color:var(--tx)}
.kpi-value{font-size:1.5rem;font-weight:700}
.kpi-label{font-size:.72rem;color:var(--mu);text-transform:uppercase;letter-spacing:.05em}
.gain{color:var(--ok)!important}.loss{color:var(--bad)!important}
.badge-gain{background:var(--ok-bg);color:var(--ok)}.badge-loss{background:var(--bad-bg);color:var(--bad)}
.table{--bs-table-bg:var(--sur);--bs-table-color:var(--tx);--bs-table-striped-bg:var(--sur2);
  --bs-table-hover-bg:var(--sur2);--bs-table-hover-color:var(--tx);--bs-table-border-color:var(--bo)}
.table-wrapper{max-height:520px;overflow-y:auto}
table th{position:sticky;top:0;background:var(--sur2)!important;color:var(--mu);z-index:1;cursor:pointer;
  user-select:none;white-space:nowrap;font-size:.67rem;text-transform:uppercase;letter-spacing:.05em;font-weight:700}
table th:hover{color:var(--tx)}
table th.sort-asc::after{content:" \2191"}
table th.sort-desc::after{content:" \2193"}
table th:not(.sort-asc):not(.sort-desc):not(.nosort)::after{content:" \2195";color:var(--mu);font-size:.7rem}
.heat-cell{width:80px;min-width:80px;text-align:center;font-size:.7rem;padding:3px 4px!important;border-radius:4px}
.heat-ticker{font-size:.65rem;font-weight:700;white-space:nowrap;max-width:75px;overflow:hidden;text-overflow:ellipsis}
.section-title{font-family:var(--fb);font-size:.78rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--mu);margin-bottom:.75rem}
.nav-tabs{border-color:var(--bo)}
.nav-tabs .nav-link{color:var(--mu);border:none;border-bottom:2px solid transparent;font-weight:600;transition:color .15s,border-color .15s}
.nav-tabs .nav-link:hover{color:var(--tx);border-color:transparent}
.nav-tabs .nav-link.active{color:var(--tx)!important;background:transparent!important;border-color:transparent transparent var(--gold)!important}
.tab-content{background:var(--sur)!important;border:1px solid var(--bo)!important}
.form-control,.form-select{background:var(--sur2);border-color:var(--bo);color:var(--tx)}
.form-control:focus,.form-select:focus{background:var(--sur2);border-color:var(--gold);color:var(--tx);box-shadow:0 0 0 .2rem rgba(201,168,76,.15)}
.form-check-input{background-color:var(--sur2);border-color:var(--mu)}
.form-check-input:checked{background-color:var(--gold);border-color:var(--gold)}
.form-check-label{color:var(--tx)}
.modal-content{background:var(--sur);color:var(--tx);border:1px solid var(--bo)}
.modal-header,.modal-footer{border-color:var(--bo)}
.btn-close{filter:var(--btn-close-filter,none)}
body[data-t="dark"] .btn-close{filter:invert(1) grayscale(100%) brightness(200%)}
.autocomplete-wrap{position:relative}
.autocomplete-list{position:absolute;top:100%;left:0;right:0;background:var(--sur);border:1px solid var(--bo);
  border-radius:8px;max-height:200px;overflow-y:auto;z-index:100;display:none;box-shadow:var(--sh)}
.autocomplete-item{padding:5px 10px;cursor:pointer;font-size:.82rem}
.autocomplete-item:hover,.autocomplete-item.active{background:var(--sur2)}
.copy-btn{font-size:.72rem;padding:2px 8px}
input[type=checkbox]{width:15px;height:15px;cursor:pointer}
.btn-logout{font-size:.7rem;padding:2px 10px;border-radius:6px;background:rgba(255,255,255,.15);color:#fff;border:1px solid rgba(255,255,255,.3);cursor:pointer;transition:background .15s}
.btn-logout:hover{background:rgba(255,255,255,.28)}
</style>
</head>
<body data-t="dark">

<nav class="navbar navbar-dark py-2 mb-4">
  <div class="container-fluid">
    <span class="navbar-brand fw-bold fs-6">Dashboard GNR &mdash; Cohen</span>
    <span class="text-white-50 d-flex align-items-center gap-2" style="font-size:.72rem">
      MEP: <strong class="text-warning">$ __MEP_DISPLAY__</strong>
      &nbsp;|&nbsp; Arancel: 1.1%
      &nbsp;|&nbsp; Actualizado: __TS__
      &nbsp;|&nbsp; Prox: <span id="countdown"></span>
      &nbsp;|&nbsp; 👤 <span class="text-white-50">__USER_NAME__</span>
      &nbsp;
      <button class="ibtn" id="btnTheme" title="Modo claro/oscuro">&#9789;</button>
      <button class="btn-logout" onclick="window.parent.location.href='/_stcore/logout'">Salir</button>
    </span>
  </div>
</nav>

<div class="container-fluid px-4">
  <div class="row g-3 mb-4" id="kpis"></div>

  <ul class="nav nav-tabs mb-0" id="mainTabs">
    <li class="nav-item"><a class="nav-link active" data-bs-toggle="tab" href="#tab-tabla">Tabla completa</a></li>
    <li class="nav-item"><a class="nav-link" data-bs-toggle="tab" href="#tab-performance">Performance</a></li>
    <li class="nav-item"><a class="nav-link" data-bs-toggle="tab" href="#tab-mercado">Volatilidad / Correlación</a></li>
    <li class="nav-item"><a class="nav-link" data-bs-toggle="tab" href="#tab-cliente">Por cliente</a></li>
    <li class="nav-item"><a class="nav-link" data-bs-toggle="tab" href="#tab-ticker">Por ticker</a></li>
    <li class="nav-item"><a class="nav-link" data-bs-toggle="tab" href="#tab-charts">Rankers</a></li>
    <li class="nav-item"><a class="nav-link" data-bs-toggle="tab" href="#tab-heat">Mapa calor</a></li>
    <li class="nav-item"><a class="nav-link" data-bs-toggle="tab" href="#tab-gr">G. Realizada</a></li>
    <li class="nav-item"><a class="nav-link text-danger" data-bs-toggle="tab" href="#tab-alertas">Alertas</a></li>
  </ul>

  <div class="tab-content bg-white rounded-bottom rounded-end shadow-sm p-3">

    <!-- ── TABLA COMPLETA ── -->
    <div class="tab-pane fade show active" id="tab-tabla">
      <div class="d-flex gap-2 mb-3 flex-wrap align-items-center">
        <input id="search-global" class="form-control form-control-sm" style="max-width:200px" placeholder="Cliente o ticker...">
        <select id="filter-tipo" class="form-select form-select-sm" style="max-width:130px">
          <option value="">Todos</option><option>Acciones</option><option>Cedear</option>
        </select>
        <div class="ms-auto d-flex gap-2">
          <button class="btn btn-sm btn-outline-success" onclick="filterPnl('gain')">Ganancias</button>
          <button class="btn btn-sm btn-outline-danger"  onclick="filterPnl('loss')">Perdidas</button>
          <button class="btn btn-sm btn-outline-secondary" onclick="filterPnl('')">Todos</button>
          <button class="btn btn-sm btn-outline-primary copy-btn" onclick="copyTable('main-table')">Copiar tabla</button>
          <button class="btn btn-sm btn-warning" data-bs-toggle="modal" data-bs-target="#modalOrden">Generar orden WA</button>
        </div>
      </div>
      <div class="table-wrapper">
        <table class="table table-sm table-hover mb-0" id="main-table">
          <thead><tr>
            <th class="nosort"><input type="checkbox" id="chk-all" onchange="toggleAll(this)"></th>
            <th onclick="sortTable('main-table',1)">Cliente</th>
            <th onclick="sortTable('main-table',2)">Ticker</th>
            <th onclick="sortTable('main-table',3)">Tipo</th>
            <th onclick="sortTable('main-table',4)" class="text-end">Cant.</th>
            <th onclick="sortTable('main-table',5)" class="text-end">Precio USD</th>
            <th onclick="sortTable('main-table',6)" class="text-end">Valor USD</th>
            <th onclick="sortTable('main-table',7)" class="text-end">Valor Neto USD</th>
            <th onclick="sortTable('main-table',8)" class="text-end">Costo USD</th>
            <th onclick="sortTable('main-table',9)" class="text-end">P&amp;L USD</th>
            <th onclick="sortTable('main-table',10)" class="text-end">P&amp;L % USD</th>
            <th onclick="sortTable('main-table',11)" class="text-end text-muted">Valor ARS (ref.)</th>
            <th onclick="sortTable('main-table',12)" class="text-end text-muted">P&amp;L ARS (ref.)</th>
          </tr></thead>
          <tbody id="main-tbody"></tbody>
        </table>
      </div>
      <div class="text-muted mt-2" style="font-size:.72rem" id="count-label"></div>
    </div>

    <!-- ── PERFORMANCE POR CLIENTE (CEDEAR + Acciones) ── -->
    <div class="tab-pane fade" id="tab-performance">
      <div class="d-flex gap-2 mb-2 flex-wrap align-items-end">
        <div>
          <label class="form-label" style="font-size:.72rem">Ganancia realizada desde</label>
          <input type="date" id="perf-desde" class="form-control form-control-sm" style="width:150px" onchange="renderPerformance()">
        </div>
        <div class="d-flex gap-1">
          <button class="btn btn-sm btn-outline-secondary" onclick="perfPreset('ytd')">YTD</button>
          <button class="btn btn-sm btn-outline-secondary" onclick="perfPreset('90d')">90d</button>
          <button class="btn btn-sm btn-outline-secondary" onclick="perfPreset('1y')">1 año</button>
          <button class="btn btn-sm btn-outline-secondary" onclick="perfPreset('todo')">Todo</button>
        </div>
        <button class="btn btn-sm btn-outline-primary copy-btn ms-auto" onclick="copyTable('table-performance')">Copiar</button>
      </div>
      <p class="text-muted mb-3" style="font-size:.72rem">
        Todo medido en dólares (costo/valor/precio nativos en USD de Cohen, no convertidos vía MEP).
        Ganancia realizada del período elegido (siempre hasta hoy, al tipo de cambio de cada operación) +
        P&amp;L no realizado actual de las posiciones abiertas (al dólar de hoy), comparado contra el capital
        hoy invertido (costo en USD de las posiciones abiertas — no incluye capital ya rotado/cerrado dentro
        del período).
      </p>
      <div class="table-wrapper">
        <table class="table table-sm table-hover" id="table-performance">
          <thead><tr>
            <th onclick="sortTable('table-performance',0)">Cuenta</th>
            <th onclick="sortTable('table-performance',1)">Cliente</th>
            <th onclick="sortTable('table-performance',2)" class="text-end">Ganancia realizada USD</th>
            <th onclick="sortTable('table-performance',3)" class="text-end">P&amp;L no realizado USD</th>
            <th onclick="sortTable('table-performance',4)" class="text-end">Resultado total USD</th>
            <th onclick="sortTable('table-performance',5)" class="text-end">Capital invertido USD</th>
            <th onclick="sortTable('table-performance',6)" class="text-end">Exposición actual USD</th>
            <th onclick="sortTable('table-performance',7)" class="text-end">Performance % s/ capital invertido</th>
            <th onclick="sortTable('table-performance',8)" class="text-end text-muted">Resultado total ARS (ref.)</th>
            <th onclick="sortTable('table-performance',9)" class="text-end text-muted">Capital invertido ARS (ref.)</th>
          </tr></thead>
          <tbody id="tbody-performance"></tbody>
        </table>
      </div>
      <div class="text-muted mt-2" style="font-size:.72rem" id="performance-count"></div>
    </div>

    <!-- ── VOLATILIDAD / CORRELACIÓN (a demanda, no auto-refresca) ── -->
    <div class="tab-pane fade" id="tab-mercado">
      <div id="mercado-vacio" class="text-muted" style="font-size:.8rem">
        Todavía no se calculó. Usá los controles de arriba de la página (fuera de esta tabla) y tocá
        "Calcular volatilidad y correlación" — no se recalcula solo, tenés que pedirlo vos.
      </div>
      <div id="mercado-contenido" style="display:none">
        <p class="text-muted" style="font-size:.72rem" id="mercado-meta"></p>
        <div id="mercado-sin-cobertura" class="mb-3"></div>
        <div class="section-title">Volatilidad anualizada</div>
        <div class="table-wrapper mb-4" style="max-height:320px">
          <table class="table table-sm table-hover" id="table-vol">
            <thead><tr>
              <th onclick="sortTable('table-vol',0)">Ticker</th>
              <th onclick="sortTable('table-vol',1)" class="text-end">Vol. anualizada</th>
              <th onclick="sortTable('table-vol',2)" class="text-end">Observaciones</th>
            </tr></thead>
            <tbody id="tbody-vol"></tbody>
          </table>
        </div>
        <div class="section-title">Matriz de correlación</div>
        <div style="overflow:auto;max-height:600px">
          <table class="table table-bordered table-sm mb-0" id="corr-table">
            <thead id="corr-head"></thead><tbody id="corr-body"></tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- ── POR CLIENTE ── -->
    <div class="tab-pane fade" id="tab-cliente">
      <div class="d-flex gap-2 align-items-center mb-3">
        <label class="fw-bold text-nowrap" style="font-size:.8rem">Cliente:</label>
        <div class="autocomplete-wrap" style="max-width:380px;width:100%">
          <input id="search-cliente-input" class="form-control form-control-sm" placeholder="Buscar por nombre o nro cuenta..." autocomplete="off">
          <div id="search-cliente-list" class="autocomplete-list"></div>
        </div>
        <button class="btn btn-sm btn-outline-primary copy-btn ms-auto" onclick="copyTable('table-cliente')">Copiar</button>
      </div>
      <div class="row g-3 mb-3" id="kpis-cliente"></div>
      <div class="table-wrapper">
        <table class="table table-sm table-hover" id="table-cliente">
          <thead><tr>
            <th onclick="sortTable('table-cliente',0)">Ticker</th>
            <th onclick="sortTable('table-cliente',1)">Descripcion</th>
            <th onclick="sortTable('table-cliente',2)">Tipo</th>
            <th onclick="sortTable('table-cliente',3)" class="text-end">Cant.</th>
            <th onclick="sortTable('table-cliente',4)" class="text-end">Precio USD</th>
            <th onclick="sortTable('table-cliente',5)" class="text-end">Valor USD</th>
            <th onclick="sortTable('table-cliente',6)" class="text-end">Valor Neto USD</th>
            <th onclick="sortTable('table-cliente',7)" class="text-end">Costo USD</th>
            <th onclick="sortTable('table-cliente',8)" class="text-end">P&amp;L USD</th>
            <th onclick="sortTable('table-cliente',9)" class="text-end">P&amp;L % USD</th>
            <th onclick="sortTable('table-cliente',10)" class="text-end text-muted">Valor ARS (ref.)</th>
            <th onclick="sortTable('table-cliente',11)" class="text-end text-muted">P&amp;L ARS (ref.)</th>
          </tr></thead>
          <tbody id="tbody-cliente"></tbody>
        </table>
      </div>
    </div>

    <!-- ── POR TICKER ── -->
    <div class="tab-pane fade" id="tab-ticker">
      <div class="d-flex gap-2 align-items-center mb-3">
        <label class="fw-bold text-nowrap" style="font-size:.8rem">Ticker:</label>
        <div class="autocomplete-wrap" style="max-width:220px">
          <input id="search-ticker-input" class="form-control form-control-sm" placeholder="Buscar ticker..." autocomplete="off">
          <div id="search-ticker-list" class="autocomplete-list"></div>
        </div>
        <button class="btn btn-sm btn-outline-primary copy-btn ms-auto" onclick="copyTable('table-ticker')">Copiar</button>
      </div>
      <div class="row g-3 mb-3" id="kpis-ticker"></div>
      <div class="table-wrapper">
        <table class="table table-sm table-hover" id="table-ticker">
          <thead><tr>
            <th onclick="sortTable('table-ticker',0)">Nro</th>
            <th onclick="sortTable('table-ticker',1)">Cliente</th>
            <th onclick="sortTable('table-ticker',2)" class="text-end">Cant.</th>
            <th onclick="sortTable('table-ticker',3)" class="text-end">Precio USD</th>
            <th onclick="sortTable('table-ticker',4)" class="text-end">Valor USD</th>
            <th onclick="sortTable('table-ticker',5)" class="text-end">Valor Neto USD</th>
            <th onclick="sortTable('table-ticker',6)" class="text-end">Costo USD</th>
            <th onclick="sortTable('table-ticker',7)" class="text-end">P&amp;L USD</th>
            <th onclick="sortTable('table-ticker',8)" class="text-end">P&amp;L % USD</th>
            <th onclick="sortTable('table-ticker',9)" class="text-end text-muted">Valor ARS (ref.)</th>
            <th onclick="sortTable('table-ticker',10)" class="text-end text-muted">P&amp;L ARS (ref.)</th>
          </tr></thead>
          <tbody id="tbody-ticker"></tbody>
        </table>
      </div>
    </div>

    <!-- ── RANKERS ── -->
    <div class="tab-pane fade" id="tab-charts">
      <div class="row g-3">
        <div class="col-md-6"><div class="section-title">Top 10 ganancias por posicion (USD)</div><canvas id="chart-gainers" height="250"></canvas></div>
        <div class="col-md-6"><div class="section-title">Top 10 perdidas por posicion (USD)</div><canvas id="chart-losers" height="250"></canvas></div>
        <div class="col-md-6"><div class="section-title">Mejores tickers (P&amp;L total USD)</div><canvas id="chart-ticker-gain" height="250"></canvas></div>
        <div class="col-md-6"><div class="section-title">Peores tickers (P&amp;L total USD)</div><canvas id="chart-ticker-loss" height="250"></canvas></div>
      </div>
    </div>

    <!-- ── MAPA CALOR ── -->
    <div class="tab-pane fade" id="tab-heat">
      <p class="text-muted" style="font-size:.72rem">Verde = ganancia USD, rojo = perdida USD. Top 40 tickers por exposicion.</p>
      <div style="overflow:auto;max-height:600px">
        <table class="table table-bordered table-sm mb-0" id="heat-table">
          <thead id="heat-head"></thead><tbody id="heat-body"></tbody>
        </table>
      </div>
    </div>

    <!-- ── GANANCIA REALIZADA ── -->
    <div class="tab-pane fade" id="tab-gr">
      <div class="d-flex gap-2 mb-3 align-items-center">
        <input id="search-gr" class="form-control form-control-sm" style="max-width:200px" placeholder="Cliente o ticker..." oninput="filterGR()">
        <div class="ms-auto d-flex gap-2">
          <button class="btn btn-sm btn-outline-success" onclick="filterGRPnl('gain')">Ganancias</button>
          <button class="btn btn-sm btn-outline-danger"  onclick="filterGRPnl('loss')">Perdidas</button>
          <button class="btn btn-sm btn-outline-secondary" onclick="filterGRPnl('')">Todos</button>
          <button class="btn btn-sm btn-outline-primary copy-btn" onclick="copyTable('gr-table')">Copiar</button>
        </div>
      </div>
      <div id="gr-kpis" class="row g-3 mb-3"></div>
      <div class="table-wrapper">
        <table class="table table-sm table-hover" id="gr-table">
          <thead><tr>
            <th onclick="sortTable('gr-table',0)">Cuenta</th>
            <th onclick="sortTable('gr-table',1)">Ticker</th>
            <th onclick="sortTable('gr-table',2)">Tipo</th>
            <th onclick="sortTable('gr-table',3)" class="text-end">Ops</th>
            <th onclick="sortTable('gr-table',4)" class="text-end">Cant. total</th>
            <th onclick="sortTable('gr-table',5)" class="text-end">Imp.Compra USD</th>
            <th onclick="sortTable('gr-table',6)" class="text-end">Imp.Venta USD</th>
            <th onclick="sortTable('gr-table',7)" class="text-end">GR USD</th>
            <th onclick="sortTable('gr-table',8)" class="text-end">GR % USD</th>
            <th onclick="sortTable('gr-table',9)" class="text-end text-muted">GR ARS (ref.)</th>
            <th onclick="sortTable('gr-table',10)" class="text-end text-muted">GR % ARS (ref.)</th>
          </tr></thead>
          <tbody id="gr-tbody"></tbody>
        </table>
      </div>
      <div class="text-muted mt-2" style="font-size:.72rem" id="gr-count"></div>
    </div>

    <!-- ── ALERTAS ── -->
    <div class="tab-pane fade" id="tab-alertas">
      <div class="row g-3 mb-3">
        <div class="col-auto">
          <label class="form-label" style="font-size:.72rem">Umbral perdida (%)</label>
          <input type="number" id="umbral-loss" class="form-control form-control-sm" value="10" style="width:90px" oninput="renderAlertas()">
        </div>
        <div class="col-auto">
          <label class="form-label" style="font-size:.72rem">Umbral ganancia (%)</label>
          <input type="number" id="umbral-gain" class="form-control form-control-sm" value="20" style="width:90px" oninput="renderAlertas()">
        </div>
      </div>
      <div id="alertas-content"></div>
    </div>

  </div><!-- tab-content -->
</div><!-- container -->

<!-- ── MODAL ORDEN WHATSAPP ── -->
<div class="modal fade" id="modalOrden" tabindex="-1">
  <div class="modal-dialog modal-lg">
    <div class="modal-content">
      <div class="modal-header">
        <h5 class="modal-title">Generar orden para Mesa de Operaciones</h5>
        <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
      </div>
      <div class="modal-body">
        <div class="row g-3 mb-3">
          <div class="col-auto">
            <label class="form-label fw-bold">Operacion</label>
            <select id="orden-tipo" class="form-select form-select-sm">
              <option value="VENTA">VENTA</option>
              <option value="COMPRA">COMPRA</option>
            </select>
          </div>
          <div class="col-auto">
            <label class="form-label fw-bold">Precio</label>
            <select id="orden-precio-tipo" class="form-select form-select-sm" onchange="togglePrecioInput()">
              <option value="MKT">Precio MKT</option>
              <option value="LIMITE">Precio Limite</option>
            </select>
          </div>
          <div class="col-auto" id="wrap-precio-limite" style="display:none">
            <label class="form-label fw-bold">Precio limite</label>
            <input type="text" id="orden-precio-valor" class="form-control form-control-sm" placeholder="ej: 4800">
          </div>
          <div class="col-auto d-flex align-items-end pb-1">
            <div class="form-check form-switch mb-0">
              <input class="form-check-input" type="checkbox" id="orden-mostrar-nombre" onchange="generarOrden()">
              <label class="form-check-label" style="font-size:.78rem" for="orden-mostrar-nombre">Mostrar nombre</label>
            </div>
          </div>
        </div>
        <div id="orden-preview" class="mb-3">
          <p class="text-muted" style="font-size:.8rem">Selecciona posiciones con el checkbox en la tabla principal.</p>
        </div>
        <div class="mb-3">
          <label class="form-label fw-bold">Mensaje generado</label>
          <textarea id="orden-texto" class="form-control" rows="10" style="font-size:.8rem;font-family:monospace"></textarea>
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn btn-secondary btn-sm" onclick="generarOrden()">Actualizar mensaje</button>
        <button class="btn btn-success btn-sm" onclick="copiarOrden()">Copiar para WA</button>
        <button class="btn btn-outline-secondary btn-sm" data-bs-dismiss="modal">Cerrar</button>
      </div>
    </div>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"></script>
<script>
const DATA    = __DATA_JSON__;
const DATA_GR = __DATA_GR_JSON__;
const MEP     = __MEP__;
const MERCADO = __MERCADO_JSON__;

// ── Utils ──────────────────────────────────────────────────────────────────
const fmt    = (v,d=0) => v==null?'':Number(v).toLocaleString('es-AR',{minimumFractionDigits:d,maximumFractionDigits:d});
const fmtPct = v => v==null?'':(v>=0?'+':'')+Number(v).toFixed(2)+'%';
const cls    = v => v>=0?'gain':'loss';
function pnlBadge(v){const c=v>=0?'badge-gain':'badge-loss';return `<span class="badge ${c}">${fmtPct(v)}</span>`;}

// ── KPIs ───────────────────────────────────────────────────────────────────
function renderKPIs(){
  // USD es la base de medición principal (costo/valor/P&L en dólares nativos
  // de Cohen, no convertidos vía MEP) — ARS queda como referencia.
  const totalValorUSD = DATA.reduce((s,d)=>s+(d.valor_usd||0),0);
  const totalNetoUSD  = DATA.reduce((s,d)=>s+(d.valor_neto_usd||0),0);
  const totalCostoUSD = DATA.reduce((s,d)=>s+(d.costo_usd||0),0);
  const totalPnlUSD   = DATA.reduce((s,d)=>s+(d.pnl_usd||0),0);
  const totalPnlARS   = DATA.reduce((s,d)=>s+(d.pnl_ars||0),0);
  const pctUSD = totalCostoUSD ? totalPnlUSD/totalCostoUSD*100 : 0;
  const gan = DATA.filter(d=>d.pnl_usd>0).length;
  const per = DATA.filter(d=>d.pnl_usd<0).length;
  document.getElementById('kpis').innerHTML=[
    {l:'Posiciones',            v:fmt(DATA.length),            s:`${gan} ganadoras / ${per} perdedoras`, color:'var(--gold)'},
    {l:'Valor USD',             v:'U$S '+fmt(totalValorUSD,2), s:'Precio mercado',                       color:'var(--gold)'},
    {l:'Valor neto USD',        v:'U$S '+fmt(totalNetoUSD,2),  s:'Descontando 1.1% arancel',             color:'var(--gold)'},
    {l:'Capital invertido USD', v:'U$S '+fmt(totalCostoUSD,2), s:'Costo de posiciones abiertas',         color:'var(--gold)'},
    {l:'P&L USD',               v:'U$S '+fmt(totalPnlUSD,2),   s:fmtPct(pctUSD)+' sobre costo USD',      color:totalPnlUSD>=0?'var(--ok)':'var(--bad)'},
    {l:'P&L ARS (referencia)',  v:'$ '+fmt(totalPnlARS),       s:'Mismo cálculo, en pesos',               color:totalPnlARS>=0?'var(--ok)':'var(--bad)'},
  ].map(c=>`<div class="col-sm-6 col-xl"><div class="card kpi-card p-3">
    <div class="kpi-label">${c.l}</div>
    <div class="kpi-value mt-1" style="color:${c.color}">${c.v}</div>
    <div style="font-size:.68rem;color:var(--mu)">${c.s}</div></div></div>`).join('');
}

// ── Tabla completa ─────────────────────────────────────────────────────────
let cf={text:'',tipo:'',pnl:''};
function buildMainTable(){
  document.getElementById('search-global').addEventListener('input',e=>{cf.text=e.target.value.toLowerCase();applyFilters();});
  document.getElementById('filter-tipo').addEventListener('change',e=>{cf.tipo=e.target.value;applyFilters();});
  applyFilters();
}
function filterPnl(m){cf.pnl=m;applyFilters();}
function applyFilters(){
  let rows=DATA;
  if(cf.text) rows=rows.filter(d=>(d.cliente+d.ticker+d.nro_cuenta).toLowerCase().includes(cf.text));
  if(cf.tipo) rows=rows.filter(d=>d.tipo===cf.tipo);
  if(cf.pnl==='gain') rows=rows.filter(d=>d.pnl_usd>0);
  if(cf.pnl==='loss') rows=rows.filter(d=>d.pnl_usd<0);
  renderMainRows(rows);
}
function renderMainRows(rows){
  document.getElementById('main-tbody').innerHTML=rows.map(d=>`
    <tr>
      <td><input type="checkbox" class="row-chk" data-nro="${d.nro_cuenta}" data-cliente="${d.cliente}" data-ticker="${d.ticker}" data-cant="${d.cantidad}"></td>
      <td><span class="fw-semibold">${d.nro_cuenta}</span> <span style="font-size:.78rem;color:var(--tx)">${d.cliente.substring(0,20)}</span></td>
      <td><strong>${d.ticker}</strong></td>
      <td><span class="badge bg-secondary">${d.tipo}</span></td>
      <td class="text-end">${fmt(d.cantidad,2)}</td>
      <td class="text-end">${fmt(d.precio_usd,2)}</td>
      <td class="text-end">${fmt(d.valor_usd,2)}</td>
      <td class="text-end text-primary">${fmt(d.valor_neto_usd,2)}</td>
      <td class="text-end">${fmt(d.costo_usd,2)}</td>
      <td class="text-end ${cls(d.pnl_usd)}">${fmt(d.pnl_usd,2)}</td>
      <td class="text-end">${pnlBadge(d.pnl_pct_usd)}</td>
      <td class="text-end text-muted">${fmt(d.valor_ars,0)}</td>
      <td class="text-end text-muted">${fmt(d.pnl_ars,0)}</td>
    </tr>`).join('');
  document.getElementById('count-label').textContent=`Mostrando ${rows.length} de ${DATA.length} posiciones`;
}
function toggleAll(chk){document.querySelectorAll('.row-chk').forEach(c=>c.checked=chk.checked);}

// ── Autocomplete helper ────────────────────────────────────────────────────
function makeAutocomplete(inputId, listId, items, onSelect){
  const inp=document.getElementById(inputId);
  const lst=document.getElementById(listId);
  let idx=-1;
  inp.addEventListener('input',()=>{
    const q=inp.value.toLowerCase();
    const matches=q?items.filter(i=>i.label.toLowerCase().includes(q)).slice(0,15):items.slice(0,15);
    lst.innerHTML=matches.map((m,i)=>`<div class="autocomplete-item" data-val="${m.value}">${m.label}</div>`).join('');
    lst.style.display=matches.length?'block':'none';
    idx=-1;
  });
  inp.addEventListener('keydown',e=>{
    const items2=[...lst.querySelectorAll('.autocomplete-item')];
    if(e.key==='ArrowDown'){idx=Math.min(idx+1,items2.length-1);}
    else if(e.key==='ArrowUp'){idx=Math.max(idx-1,0);}
    else if(e.key==='Enter'&&idx>=0){items2[idx].click();return;}
    else if(e.key==='Escape'){lst.style.display='none';}
    items2.forEach((el,i)=>el.classList.toggle('active',i===idx));
  });
  lst.addEventListener('click',e=>{
    const el=e.target.closest('.autocomplete-item');
    if(!el)return;
    inp.value=el.textContent;
    lst.style.display='none';
    onSelect(el.dataset.val);
  });
  document.addEventListener('click',e=>{if(!inp.contains(e.target)&&!lst.contains(e.target))lst.style.display='none';});
}

// ── Performance por cliente (CEDEAR + Acciones) ─────────────────────────────
function perfDefaultDesde(){
  const hoy=new Date();
  return new Date(hoy.getFullYear(),0,1).toISOString().slice(0,10); // 1 de enero del año en curso
}
function perfPreset(tipo){
  const el=document.getElementById('perf-desde');
  const hoy=new Date();
  let d=null;
  if(tipo==='ytd') d=new Date(hoy.getFullYear(),0,1);
  else if(tipo==='90d'){ d=new Date(hoy); d.setDate(d.getDate()-90); }
  else if(tipo==='1y'){ d=new Date(hoy); d.setFullYear(d.getFullYear()-1); }
  // 'todo' deja d=null -> sin filtro, toma toda la ganancia realizada disponible
  el.value = d ? d.toISOString().slice(0,10) : '';
  renderPerformance();
}
function buildPerformanceTab(){
  document.getElementById('perf-desde').value = perfDefaultDesde();
  renderPerformance();
}
function renderPerformance(){
  const desde = document.getElementById('perf-desde').value; // '' = Todo, sin piso de fecha
  const porCliente = {};
  const asegurar = (k, cliente) => {
    if(!porCliente[k]) porCliente[k]={
      nro_cuenta:k, cliente, gr_usd:0, gr_ars:0, pnl_usd:0, pnl_ars:0,
      costo_usd:0, costo_ars:0, valor_usd:0,
    };
    return porCliente[k];
  };
  DATA.forEach(d=>{
    const p=asegurar(d.nro_cuenta, d.cliente);
    p.pnl_usd += d.pnl_usd||0;
    p.pnl_ars += d.pnl_ars||0;
    p.costo_usd += d.costo_usd||0;
    p.costo_ars += d.costo_ars||0;
    p.valor_usd += d.valor_usd||0;
  });
  DATA_GR.forEach(d=>{
    if(desde && d.fecha < desde) return;
    const p=asegurar(d.nro_cuenta, d.cliente);
    p.gr_usd += d.gr_usd||0;
    p.gr_ars += d.gr_ars||0;
  });
  const rows = Object.values(porCliente).map(p=>{
    const resultado_total_usd = p.gr_usd + p.pnl_usd;
    const resultado_total_ars = p.gr_ars + p.pnl_ars;
    const performance_pct = p.costo_usd ? resultado_total_usd/p.costo_usd*100 : null;
    return {...p, resultado_total_usd, resultado_total_ars, performance_pct};
  }).sort((a,b)=>b.resultado_total_usd-a.resultado_total_usd);

  document.getElementById('tbody-performance').innerHTML = rows.map(p=>`
    <tr>
      <td><strong>${p.nro_cuenta}</strong></td>
      <td>${p.cliente}</td>
      <td class="text-end ${cls(p.gr_usd)}">${fmt(p.gr_usd,2)}</td>
      <td class="text-end ${cls(p.pnl_usd)}">${fmt(p.pnl_usd,2)}</td>
      <td class="text-end ${cls(p.resultado_total_usd)}">${fmt(p.resultado_total_usd,2)}</td>
      <td class="text-end">${fmt(p.costo_usd,2)}</td>
      <td class="text-end text-primary">${fmt(p.valor_usd,2)}</td>
      <td class="text-end">${p.performance_pct!=null?pnlBadge(p.performance_pct):'—'}</td>
      <td class="text-end text-muted">${fmt(p.resultado_total_ars)}</td>
      <td class="text-end text-muted">${fmt(p.costo_ars)}</td>
    </tr>`).join('');
  document.getElementById('performance-count').textContent =
    `${rows.length} clientes — ganancia realizada ${desde?('desde '+desde):'de todo el histórico disponible'}, P&L no realizado siempre actual`;
}

// ── Volatilidad / Correlación (viene calculado desde Python, a demanda) ────
function renderMercado(){
  if(!MERCADO || !MERCADO.corr_tickers || !MERCADO.corr_tickers.length){
    document.getElementById('mercado-vacio').style.display='';
    document.getElementById('mercado-contenido').style.display='none';
    return;
  }
  document.getElementById('mercado-vacio').style.display='none';
  document.getElementById('mercado-contenido').style.display='';
  document.getElementById('mercado-meta').textContent = `Calculado ${MERCADO.fecha_calculo}`;

  const sc = MERCADO.sin_cobertura||[];
  document.getElementById('mercado-sin-cobertura').innerHTML = sc.length
    ? `<div class="alert alert-warning py-2" style="font-size:.75rem">Sin datos de mercado para: ${sc.join(', ')}</div>`
    : '';

  document.getElementById('tbody-vol').innerHTML = (MERCADO.vol||[]).map(v=>`
    <tr><td><strong>${v.ticker}</strong></td>
      <td class="text-end">${fmtPct(v.vol_anualizada_pct)}</td>
      <td class="text-end text-muted">${v.n_obs}</td></tr>`).join('')
    || '<tr><td colspan="3" class="text-center text-muted py-3">Sin datos</td></tr>';

  const tks = MERCADO.corr_tickers;
  const mat = MERCADO.corr_matrix;
  const hc = v => { if(v==null) return 'var(--sur2)'; if(v>=0.7) return '#163f2d'; if(v>=0.3) return 'rgba(46,158,107,.35)';
    if(v>-0.3) return 'transparent'; if(v>-0.7) return 'rgba(196,69,63,.35)'; return '#4a1a17'; };
  const htc = v => (v!=null && Math.abs(v)>=0.7) ? '#fff' : 'var(--tx)';
  document.getElementById('corr-head').innerHTML = '<tr><th style="min-width:70px"></th>' +
    tks.map(t=>`<th style="font-size:.62rem;white-space:nowrap">${t}</th>`).join('') + '</tr>';
  document.getElementById('corr-body').innerHTML = tks.map((t,i)=>
    `<tr><td style="white-space:nowrap;font-size:.72rem"><strong>${t}</strong></td>` +
    tks.map((_,j)=>{
      const v=mat[i][j];
      return `<td class="heat-cell" style="background:${hc(v)};color:${htc(v)}">${v!=null?v.toFixed(2):''}</td>`;
    }).join('') + '</tr>').join('');
}

// ── Por cliente ────────────────────────────────────────────────────────────
function buildClienteTab(){
  const clientes=[...new Map(DATA.map(d=>[d.nro_cuenta,d])).values()]
    .sort((a,b)=>a.nro_cuenta.localeCompare(b.nro_cuenta))
    .map(d=>({value:d.nro_cuenta,label:`${d.nro_cuenta} - ${d.cliente}`}));
  makeAutocomplete('search-cliente-input','search-cliente-list',clientes,nro=>renderCliente(nro));
  if(clientes.length){document.getElementById('search-cliente-input').value=clientes[0].label;renderCliente(clientes[0].value);}
}
function renderCliente(nro){
  const rows=DATA.filter(d=>d.nro_cuenta===nro);
  const tvUSD=rows.reduce((s,d)=>s+(d.valor_usd||0),0);
  const tnUSD=rows.reduce((s,d)=>s+(d.valor_neto_usd||0),0);
  const tcUSD=rows.reduce((s,d)=>s+(d.costo_usd||0),0);
  const tuUSD=rows.reduce((s,d)=>s+(d.pnl_usd||0),0);
  const tp=rows.reduce((s,d)=>s+(d.pnl_ars||0),0);
  const pctUSD=tcUSD?tuUSD/tcUSD*100:0;
  document.getElementById('kpis-cliente').innerHTML=`
    <div class="col-auto"><div class="card kpi-card p-3"><div class="kpi-label">Posiciones</div><div class="kpi-value">${rows.length}</div></div></div>
    <div class="col-auto"><div class="card kpi-card p-3"><div class="kpi-label">Valor USD</div><div class="kpi-value text-primary">U$S ${fmt(tvUSD,2)}</div></div></div>
    <div class="col-auto"><div class="card kpi-card p-3"><div class="kpi-label">Valor neto USD</div><div class="kpi-value text-info">U$S ${fmt(tnUSD,2)}</div></div></div>
    <div class="col-auto"><div class="card kpi-card p-3"><div class="kpi-label">Capital invertido USD</div><div class="kpi-value">U$S ${fmt(tcUSD,2)}</div></div></div>
    <div class="col-auto"><div class="card kpi-card p-3"><div class="kpi-label">P&L USD</div><div class="kpi-value ${cls(tuUSD)}">U$S ${fmt(tuUSD,2)}</div><div style="font-size:.68rem;color:var(--mu)">${fmtPct(pctUSD)}</div></div></div>
    <div class="col-auto"><div class="card kpi-card p-3"><div class="kpi-label">P&L ARS (ref.)</div><div class="kpi-value ${cls(tp)}">$ ${fmt(tp)}</div></div></div>`;
  document.getElementById('tbody-cliente').innerHTML=
    [...rows].sort((a,b)=>(b.valor_usd||0)-(a.valor_usd||0)).map(d=>`
    <tr>
      <td><strong>${d.ticker}</strong></td>
      <td class="text-muted">${d.descripcion.substring(0,32)}</td>
      <td><span class="badge bg-secondary">${d.tipo}</span></td>
      <td class="text-end">${fmt(d.cantidad,2)}</td>
      <td class="text-end">${fmt(d.precio_usd,2)}</td>
      <td class="text-end">${fmt(d.valor_usd,2)}</td>
      <td class="text-end text-primary">${fmt(d.valor_neto_usd,2)}</td>
      <td class="text-end">${fmt(d.costo_usd,2)}</td>
      <td class="text-end ${cls(d.pnl_usd)}">${fmt(d.pnl_usd,2)}</td>
      <td class="text-end">${pnlBadge(d.pnl_pct_usd)}</td>
      <td class="text-end text-muted">${fmt(d.valor_ars,0)}</td>
      <td class="text-end text-muted">${fmt(d.pnl_ars,0)}</td>
    </tr>`).join('');
}

// ── Por ticker ─────────────────────────────────────────────────────────────
function buildTickerTab(){
  const tickers=[...new Set(DATA.map(d=>d.ticker).filter(Boolean))].sort()
    .map(t=>({value:t,label:t}));
  makeAutocomplete('search-ticker-input','search-ticker-list',tickers,t=>renderTicker(t));
  if(tickers.length){document.getElementById('search-ticker-input').value=tickers[0].label;renderTicker(tickers[0].value);}
}
function renderTicker(ticker){
  const rows=DATA.filter(d=>d.ticker===ticker);
  const tvUSD=rows.reduce((s,d)=>s+(d.valor_usd||0),0);
  const tnUSD=rows.reduce((s,d)=>s+(d.valor_neto_usd||0),0);
  const tcUSD=rows.reduce((s,d)=>s+(d.costo_usd||0),0);
  const tuUSD=rows.reduce((s,d)=>s+(d.pnl_usd||0),0);
  const tp   =rows.reduce((s,d)=>s+(d.pnl_ars||0),0);
  const cant =rows.reduce((s,d)=>s+(d.cantidad||0),0);
  const pctUSD=tcUSD?tuUSD/tcUSD*100:0;
  const precioUSD=rows[0]?.precio_usd||0;
  document.getElementById('kpis-ticker').innerHTML=`
    <div class="col-auto"><div class="card kpi-card p-3"><div class="kpi-label">Clientes</div><div class="kpi-value">${rows.length}</div></div></div>
    <div class="col-auto"><div class="card kpi-card p-3"><div class="kpi-label">Cantidad total</div><div class="kpi-value">${fmt(cant,2)}</div></div></div>
    <div class="col-auto"><div class="card kpi-card p-3"><div class="kpi-label">Precio USD</div><div class="kpi-value text-primary">U$S ${fmt(precioUSD,2)}</div></div></div>
    <div class="col-auto"><div class="card kpi-card p-3"><div class="kpi-label">Valor total USD</div><div class="kpi-value">U$S ${fmt(tvUSD,2)}</div></div></div>
    <div class="col-auto"><div class="card kpi-card p-3"><div class="kpi-label">Valor neto USD</div><div class="kpi-value text-info">U$S ${fmt(tnUSD,2)}</div></div></div>
    <div class="col-auto"><div class="card kpi-card p-3"><div class="kpi-label">Capital invertido USD</div><div class="kpi-value">U$S ${fmt(tcUSD,2)}</div></div></div>
    <div class="col-auto"><div class="card kpi-card p-3"><div class="kpi-label">P&L USD</div><div class="kpi-value ${cls(tuUSD)}">U$S ${fmt(tuUSD,2)}</div><div style="font-size:.68rem;color:var(--mu)">${fmtPct(pctUSD)}</div></div></div>
    <div class="col-auto"><div class="card kpi-card p-3"><div class="kpi-label">P&L ARS (ref.)</div><div class="kpi-value ${cls(tp)}">$ ${fmt(tp)}</div></div></div>`;
  document.getElementById('tbody-ticker').innerHTML=
    [...rows].sort((a,b)=>(b.pnl_usd||0)-(a.pnl_usd||0)).map(d=>`
    <tr>
      <td>${d.nro_cuenta}</td><td>${d.cliente.substring(0,28)}</td>
      <td class="text-end">${fmt(d.cantidad,2)}</td>
      <td class="text-end">${fmt(d.precio_usd,2)}</td>
      <td class="text-end">${fmt(d.valor_usd,2)}</td>
      <td class="text-end text-primary">${fmt(d.valor_neto_usd,2)}</td>
      <td class="text-end">${fmt(d.costo_usd,2)}</td>
      <td class="text-end ${cls(d.pnl_usd)}">${fmt(d.pnl_usd,2)}</td>
      <td class="text-end">${pnlBadge(d.pnl_pct_usd)}</td>
      <td class="text-end text-muted">${fmt(d.valor_ars,0)}</td>
      <td class="text-end text-muted">${fmt(d.pnl_ars,0)}</td>
    </tr>`).join('');
}

// ── Charts ─────────────────────────────────────────────────────────────────
function buildCharts(){
  const gridColor='rgba(122,143,181,.15)', textColor='#7a8fb5';
  Chart.defaults.color=textColor;
  Chart.defaults.borderColor=gridColor;
  const sorted=[...DATA].sort((a,b)=>b.pnl_usd-a.pnl_usd);
  const makeBar=(id,rows,color)=>new Chart(document.getElementById(id),{type:'bar',
    data:{labels:rows.map(d=>`${d.ticker}(${d.nro_cuenta})`),datasets:[{data:rows.map(d=>Math.abs(d.pnl_usd)),backgroundColor:color,borderRadius:4}]},
    options:{indexAxis:'y',plugins:{legend:{display:false}},scales:{x:{grid:{color:gridColor},ticks:{color:textColor,callback:v=>'U$S '+fmt(v)}},y:{grid:{color:gridColor},ticks:{color:textColor}}}}});
  makeBar('chart-gainers',sorted.slice(0,10),'#2e9e6b');
  makeBar('chart-losers',sorted.slice(-10).reverse(),'#c4453f');
  const tkMap={};
  DATA.forEach(d=>{if(!tkMap[d.ticker])tkMap[d.ticker]={pnl:0,n:0};tkMap[d.ticker].pnl+=d.pnl_usd||0;tkMap[d.ticker].n++;});
  const tkArr=Object.entries(tkMap).sort((a,b)=>b[1].pnl-a[1].pnl);
  const makeBar2=(id,rows,color)=>new Chart(document.getElementById(id),{type:'bar',
    data:{labels:rows.map(([t,v])=>`${t}(${v.n}cl)`),datasets:[{data:rows.map(([,v])=>Math.abs(v.pnl)),backgroundColor:color,borderRadius:4}]},
    options:{indexAxis:'y',plugins:{legend:{display:false}},scales:{x:{ticks:{callback:v=>'U$S '+fmt(v)}}}}});
  makeBar2('chart-ticker-gain',tkArr.slice(0,10),'#C9A84C');
  makeBar2('chart-ticker-loss',tkArr.slice(-10).reverse(),'#c98a1c');
}

// ── Mapa calor ─────────────────────────────────────────────────────────────
function buildHeatmap(){
  const cm={},tm={};
  DATA.forEach(d=>{
    if(!cm[d.nro_cuenta])cm[d.nro_cuenta]={n:d.cliente,v:0};cm[d.nro_cuenta].v+=d.valor_usd||0;
    if(!tm[d.ticker])tm[d.ticker]={v:0};tm[d.ticker].v+=d.valor_usd||0;
  });
  const clientes=Object.keys(cm).sort((a,b)=>cm[b].v-cm[a].v);
  const tickers=Object.keys(tm).sort((a,b)=>tm[b].v-tm[a].v).slice(0,40);
  const lkp={};DATA.forEach(d=>{lkp[`${d.nro_cuenta}|${d.ticker}`]=d.pnl_pct_usd;});
  const hc=p=>{if(p==null)return'transparent';if(p>=20)return'#163f2d';if(p>=10)return'#2e9e6b';if(p>0)return'rgba(46,158,107,.25)';if(p==0)return'transparent';if(p>-10)return'rgba(196,69,63,.25)';if(p>-20)return'#c4453f';return'#4a1a17';};
  const htc=p=>{if(p==null)return'var(--mu)';if(p>=10||p<=-20)return'#fff';return'var(--tx)';};
  document.getElementById('heat-head').innerHTML='<tr><th style="min-width:120px">Cliente</th>'+tickers.map(t=>`<th style="font-size:.62rem;font-weight:700;white-space:nowrap;max-width:70px;overflow:hidden;text-overflow:ellipsis" title="${t}">${t}</th>`).join('')+'</tr>';
  document.getElementById('heat-body').innerHTML=clientes.map(nro=>{
    const nombre=cm[nro].n.substring(0,16);
    return`<tr><td style="white-space:nowrap;font-size:.7rem"><strong>${nro}</strong> ${nombre}</td>${tickers.map(t=>{const p=lkp[`${nro}|${t}`];if(p==null)return`<td style="background:var(--sur2)"></td>`;return`<td class="heat-cell" style="background:${hc(p)};color:${htc(p)}" title="${t}: ${fmtPct(p)}">${fmtPct(p)}</td>`;}).join('')}</tr>`;
  }).join('');
}

// ── Ganancia Realizada ─────────────────────────────────────────────────────
let grFilter={text:'',pnl:''};

function agruparGR(rows){
  const map={};
  rows.forEach(d=>{
    const k=`${d.nro_cuenta}|${d.ticker}`;
    if(!map[k]) map[k]={
      nro_cuenta:d.nro_cuenta, cliente:d.cliente, ticker:d.ticker, tipo:d.tipo,
      ops:0, cantidad:0, importe_compra_ars:0, importe_venta_ars:0,
      importe_compra_usd:0, importe_venta_usd:0, gr_ars:0, gr_usd:0
    };
    const g=map[k];
    g.ops++;
    g.cantidad           += d.cantidad||0;
    g.importe_compra_ars += d.importe_compra_ars||0;
    g.importe_venta_ars  += d.importe_venta_ars||0;
    g.importe_compra_usd += d.importe_compra_usd||0;
    g.importe_venta_usd  += d.importe_venta_usd||0;
    g.gr_ars              += d.gr_ars||0;
    g.gr_usd               += d.gr_usd||0;
  });
  return Object.values(map).map(g=>({
    ...g,
    gr_pct_ars: g.importe_compra_ars ? g.gr_ars/g.importe_compra_ars*100 : 0,
    gr_pct_usd: g.importe_compra_usd ? g.gr_usd/g.importe_compra_usd*100 : 0,
  }));
}

function buildGR(){
  if(!DATA_GR.length){
    document.getElementById('gr-tbody').innerHTML='<tr><td colspan="11" class="text-center text-muted py-4">Sin datos de ganancia realizada para el periodo actual.</td></tr>';
    return;
  }
  renderGR();
}
function filterGR(){grFilter.text=document.getElementById('search-gr').value.toLowerCase();renderGR();}
function filterGRPnl(m){grFilter.pnl=m;renderGR();}

function renderGR(){
  let raw=DATA_GR;
  if(grFilter.text)raw=raw.filter(d=>(d.cliente+d.ticker+d.nro_cuenta).toLowerCase().includes(grFilter.text));
  if(grFilter.pnl==='gain')raw=raw.filter(d=>d.gr_usd>0);
  if(grFilter.pnl==='loss')raw=raw.filter(d=>d.gr_usd<0);

  const rows=agruparGR(raw).sort((a,b)=>b.gr_usd-a.gr_usd);

  const tp=rows.reduce((s,d)=>s+(d.gr_ars||0),0);
  const tu=rows.reduce((s,d)=>s+(d.gr_usd||0),0);
  const tops=rows.reduce((s,d)=>s+d.ops,0);
  document.getElementById('gr-kpis').innerHTML=`
    <div class="col-auto"><div class="card kpi-card p-3"><div class="kpi-label">Posiciones</div><div class="kpi-value">${rows.length}</div></div></div>
    <div class="col-auto"><div class="card kpi-card p-3"><div class="kpi-label">Operaciones</div><div class="kpi-value">${tops}</div></div></div>
    <div class="col-auto"><div class="card kpi-card p-3"><div class="kpi-label">GR USD</div><div class="kpi-value ${cls(tu)}">U$S ${fmt(tu,2)}</div></div></div>
    <div class="col-auto"><div class="card kpi-card p-3"><div class="kpi-label">GR ARS (ref.)</div><div class="kpi-value ${cls(tp)}">$ ${fmt(tp)}</div></div></div>`;

  document.getElementById('gr-tbody').innerHTML=rows.map(d=>`
    <tr>
      <td><span class="fw-semibold">${d.nro_cuenta}</span> <span style="font-size:.75rem;color:var(--tx)">${d.cliente.substring(0,18)}</span></td>
      <td><strong>${d.ticker}</strong></td>
      <td><span class="badge bg-secondary">${d.tipo}</span></td>
      <td class="text-end">${d.ops}</td>
      <td class="text-end">${fmt(d.cantidad,2)}</td>
      <td class="text-end">${fmt(d.importe_compra_usd,2)}</td>
      <td class="text-end">${fmt(d.importe_venta_usd,2)}</td>
      <td class="text-end ${cls(d.gr_usd)}">${fmt(d.gr_usd,2)}</td>
      <td class="text-end">${pnlBadge(d.gr_pct_usd)}</td>
      <td class="text-end text-muted">${fmt(d.gr_ars,0)}</td>
      <td class="text-end text-muted">${pnlBadge(d.gr_pct_ars)}</td>
    </tr>`).join('');
  document.getElementById('gr-count').textContent=
    `${rows.length} posiciones (${tops} operaciones) — filtrado de ${agruparGR(DATA_GR).length} total`;
}

// ── Alertas ────────────────────────────────────────────────────────────────
function abrirOrdenDesdeAlertas(rows, operacion){
  document.querySelectorAll('.row-chk').forEach(c=>c.checked=false);
  const keys=new Set(rows.map(d=>`${d.nro_cuenta}|${d.ticker}`));
  document.querySelectorAll('.row-chk').forEach(c=>{
    if(keys.has(`${c.dataset.nro}|${c.dataset.ticker}`)) c.checked=true;
  });
  document.getElementById('orden-tipo').value=operacion;
  generarOrden();
  new bootstrap.Modal(document.getElementById('modalOrden')).show();
}

function renderAlertas(){
  const ul=-Math.abs(Number(document.getElementById('umbral-loss').value)||10);
  const ug= Math.abs(Number(document.getElementById('umbral-gain').value)||20);
  const losses=DATA.filter(d=>d.pnl_pct_usd<=ul).sort((a,b)=>a.pnl_pct_usd-b.pnl_pct_usd);
  const gains =DATA.filter(d=>d.pnl_pct_usd>=ug).sort((a,b)=>b.pnl_pct_usd-a.pnl_pct_usd);

  const tbl=(rows,tableId)=>`
    <div class="table-wrapper">
      <table class="table table-sm table-hover" id="${tableId}"><thead><tr>
        <th>Cuenta</th><th>Ticker</th><th>Tipo</th><th class="text-end">Cant.</th>
        <th class="text-end">Valor Neto USD</th><th class="text-end">P&L USD</th>
        <th class="text-end">P&L % USD</th>
      </tr></thead>
      <tbody>${rows.map(d=>`<tr>
        <td><strong>${d.nro_cuenta}</strong> <span style="font-size:.75rem;color:var(--tx)">${d.cliente.substring(0,20)}</span></td>
        <td><strong>${d.ticker}</strong></td><td>${d.tipo}</td>
        <td class="text-end">${fmt(d.cantidad,2)}</td>
        <td class="text-end">${d.valor_neto_usd!=null?fmt(d.valor_neto_usd,0):'-'}</td>
        <td class="text-end ${cls(d.pnl_usd)}">${fmt(d.pnl_usd,2)}</td>
        <td class="text-end">${pnlBadge(d.pnl_pct_usd)}</td>
      </tr>`).join('')}
      </tbody></table>
    </div>`;

  document.getElementById('alertas-content').innerHTML=`
    <div class="row g-3">
      <div class="col-lg-6">
        <div class="d-flex align-items-center mb-2 gap-2">
          <span class="badge bg-danger">${losses.length}</span>
          <span class="section-title mb-0">Perdidas > ${Math.abs(ul)}%</span>
          <button class="btn btn-sm btn-outline-primary copy-btn ms-auto" onclick="copyTable('alertas-loss-table')">Copiar</button>
          <button class="btn btn-sm btn-warning" onclick="abrirOrdenDesdeAlertas(${JSON.stringify(losses.map(d=>({nro_cuenta:d.nro_cuenta,ticker:d.ticker})))}, 'VENTA')">
            Orden WA Masiva
          </button>
        </div>
        ${tbl(losses,'alertas-loss-table')}
      </div>
      <div class="col-lg-6">
        <div class="d-flex align-items-center mb-2 gap-2">
          <span class="badge bg-success">${gains.length}</span>
          <span class="section-title mb-0">Ganancias > ${ug}%</span>
          <button class="btn btn-sm btn-outline-primary copy-btn ms-auto" onclick="copyTable('alertas-gain-table')">Copiar</button>
          <button class="btn btn-sm btn-warning" onclick="abrirOrdenDesdeAlertas(${JSON.stringify(gains.map(d=>({nro_cuenta:d.nro_cuenta,ticker:d.ticker})))}, 'VENTA')">
            Orden WA Masiva
          </button>
        </div>
        ${tbl(gains,'alertas-gain-table')}
      </div>
    </div>`;
}

// ── Sort ───────────────────────────────────────────────────────────────────
const sortSt={};
function sortTable(tableId,col){
  const tbl=document.getElementById(tableId);
  const key=tableId+'_'+col;
  const asc=sortSt[key]!==true;
  sortSt[key]=asc;
  tbl.querySelectorAll('th').forEach((th,i)=>{
    th.classList.remove('sort-asc','sort-desc');
    if(i===col)th.classList.add(asc?'sort-asc':'sort-desc');
  });
  const tbody=tbl.querySelector('tbody');
  [...tbody.querySelectorAll('tr')].sort((a,b)=>{
    const av=cellNum(a.cells[col]),bv=cellNum(b.cells[col]);
    if(typeof av==='number'&&typeof bv==='number')return asc?av-bv:bv-av;
    const as=String(av),bs=String(bv);
    return asc?as.localeCompare(bs):bs.localeCompare(as);
  }).forEach(r=>tbody.appendChild(r));
}
function cellNum(cell){
  if(!cell)return'';
  const txt=cell.textContent.trim().replace(/[.$\s%]/g,'').replace(/,/g,'.');
  const n=parseFloat(txt.replace(/[^\d.\-]/g,''));
  return isNaN(n)?cell.textContent.trim():n;
}

// ── Copiar tabla ───────────────────────────────────────────────────────────
function copyTable(tableId){
  const tbl=document.getElementById(tableId);
  const rows=[...tbl.querySelectorAll('tr')];
  const tsv=rows.map(r=>[...r.cells].map(c=>c.textContent.trim()).join('\t')).join('\n');
  navigator.clipboard.writeText(tsv).then(()=>{
    const btn=event.target;const orig=btn.textContent;
    btn.textContent='Copiado!';btn.classList.add('btn-success');btn.classList.remove('btn-outline-primary');
    setTimeout(()=>{btn.textContent=orig;btn.classList.remove('btn-success');btn.classList.add('btn-outline-primary');},1500);
  });
}

// ── Orden WhatsApp ─────────────────────────────────────────────────────────
function togglePrecioInput(){
  const show=document.getElementById('orden-precio-tipo').value==='LIMITE';
  document.getElementById('wrap-precio-limite').style.display=show?'':'none';
}
function generarOrden(){
  const tipo=document.getElementById('orden-tipo').value;
  const precioTipo=document.getElementById('orden-precio-tipo').value;
  const precioVal=document.getElementById('orden-precio-valor').value;
  const seleccionados=[...document.querySelectorAll('.row-chk:checked')];
  if(!seleccionados.length){
    document.getElementById('orden-texto').value='Sin posiciones seleccionadas. Marca checkboxes en la tabla principal.';
    return;
  }
  const precioStr=precioTipo==='MKT'?'a precio MKT':`a precio limite $ ${precioVal}`;
  const mostrarNombre=document.getElementById('orden-mostrar-nombre').checked;
  let lineas=seleccionados.map(chk=>{
    const nro=chk.dataset.nro;
    const nombre=mostrarNombre?` ${chk.dataset.cliente.substring(0,22)}`:'';
    return `• ${nro}${nombre} - ${chk.dataset.ticker} - ${fmt(chk.dataset.cant,0)} VN`;
  });
  const fecha=new Date().toLocaleDateString('es-AR');
  const txt=`*ORDENES DE OPERACION — ${fecha}*\n\n*${tipo}* ${precioStr}:\n\n${lineas.join('\n')}\n\nSaludos,\nQTM Capital`;
  document.getElementById('orden-texto').value=txt;
}
function copiarOrden(){
  const txt=document.getElementById('orden-texto').value;
  navigator.clipboard.writeText(txt).then(()=>{
    const btn=event.target;btn.textContent='Copiado!';
    setTimeout(()=>btn.textContent='Copiar para WA',1500);
  });
}
document.getElementById('modalOrden').addEventListener('show.bs.modal',()=>generarOrden());

// ── Tema claro/oscuro ──────────────────────────────────────────────────────
function applyTheme(t){
  document.body.setAttribute('data-t',t);
  try{localStorage.setItem('qtm_t',t);}catch(e){}
  document.getElementById('btnTheme').innerHTML=(t==='dark')?'&#9728;':'&#9789;';
}
(function(){
  let s=null;
  try{s=localStorage.getItem('qtm_t');}catch(e){}
  if(!s) s=(window.matchMedia&&window.matchMedia('(prefers-color-scheme:light)').matches)?'light':'dark';
  applyTheme(s);
})();
document.getElementById('btnTheme').addEventListener('click',()=>{
  applyTheme(document.body.getAttribute('data-t')==='dark'?'light':'dark');
});

// ── Auto-refresh countdown ─────────────────────────────────────────────────
const REFRESH_S=__INTERVAL_S__;
let rem=REFRESH_S;
const cdEl=document.getElementById('countdown');
setInterval(()=>{rem--;if(rem<=0){rem=REFRESH_S;}const m=Math.floor(rem/60),s=rem%60;if(cdEl)cdEl.textContent=m+':'+String(s).padStart(2,'0');},1000);

// ── Init ───────────────────────────────────────────────────────────────────
renderKPIs();
buildMainTable();
buildPerformanceTab();
renderMercado();
buildClienteTab();
buildTickerTab();
buildCharts();
buildHeatmap();
buildGR();
renderAlertas();
</script>
</body>
</html>"""


def _js_safe(data) -> str:
    return json.dumps(data, ensure_ascii=False, default=str).replace("<", "\\u003c")


def generar_html(
    posiciones: list, gr: list, mep: float, ts: str, user_name: str = "",
    mercado_result: dict | None = None,
) -> str:
    return (
        HTML_TEMPLATE.replace("__DATA_JSON__", _js_safe(posiciones))
        .replace("__DATA_GR_JSON__", _js_safe(gr))
        .replace("__MEP__", f"{mep:.2f}")
        .replace("__MEP_DISPLAY__", f"{mep:,.2f}")
        .replace("__TS__", ts)
        .replace("__INTERVAL_S__", str(INTERVAL_S))
        .replace("__USER_NAME__", user_name)
        .replace("__MERCADO_JSON__", _js_safe(mercado_result))
    )


# ── Autenticación ─────────────────────────────────────────────────────────────


def check_auth() -> bool:
    """Devuelve True si el usuario está autenticado y autorizado."""

    st.markdown(
        """
    <link href="https://fonts.googleapis.com/css2?family=DM+Serif+Display&display=swap" rel="stylesheet">
    <style>
        #MainMenu, header, footer {visibility: hidden;}
        .login-wrap {
            display: flex; flex-direction: column; align-items: center;
            justify-content: center; min-height: 80vh;
        }
        .login-box {
            background: #fff; border-radius: 16px;
            box-shadow: 0 4px 24px rgba(0,0,0,.10);
            padding: 2.5rem 2rem; min-width: 320px; text-align: center;
        }
        .login-logo {
            font-family: 'DM Serif Display', Georgia, serif;
            font-size: 2rem; font-weight: 400; letter-spacing: -.01em;
            background: linear-gradient(135deg,#0b1830,#0F2044 55%,#16243a);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent;
            margin-bottom: .25rem;
        }
        .login-sub {
            font-size: .68rem; color: #C9A84C; text-transform: uppercase;
            letter-spacing: .14em; font-weight: 600; margin-bottom: 1.5rem;
        }
        .stButton > button {
            background: linear-gradient(135deg,#0b1830,#0F2044 55%,#16243a);
            color: white; border: none; border-radius: 8px;
            padding: .6rem 1.2rem; font-size: .9rem; cursor: pointer;
            width: 100%;
        }
        .stButton > button:hover { opacity: 0.85; }
    </style>
    """,
        unsafe_allow_html=True,
    )

    # ── No logueado ───────────────────────────────────────────────────────────
    if not st.user.is_logged_in:
        st.markdown(
            """
        <div class="login-wrap">
          <div class="login-box">
            <div class="login-logo">📊 QTM Capital</div>
            <div class="login-sub">Dashboards</div>
          </div>
        </div>
        """,
            unsafe_allow_html=True,
        )
        st.button("Ingresar con Google", on_click=st.login, use_container_width=True)
        return False

    # ── Logueado pero no autorizado ───────────────────────────────────────────
    if st.user.email not in EMAILS_AUTORIZADOS:
        st.markdown(
            """
        <div class="login-wrap">
          <div class="login-box">
            <div class="login-logo">📊 QTM Capital</div>
            <div class="login-sub">Dashboards</div>
          </div>
        </div>
        """,
            unsafe_allow_html=True,
        )
        st.error(f"❌ Tu cuenta ({st.user.email}) no tiene acceso a este dashboard.")
        st.button("Cerrar sesión", on_click=st.logout, use_container_width=True)
        return False

    # ── Autorizado ────────────────────────────────────────────────────────────
    return True


if not check_auth():
    st.stop()

# ── Streamlit UI ───────────────────────────────────────────────────────────────

st.markdown(
    """
<style>
    #MainMenu, footer {visibility: hidden;}
    header[data-testid="stHeader"] {background: transparent;}
    .block-container { padding: 1rem 1.5rem 0 !important; max-width: 100% !important; }
    iframe { border: none !important; }
</style>
""",
    unsafe_allow_html=True,
)

user_name = st.user.name  # nombre del usuario autenticado

# ── Selección de dashboard ────────────────────────────────────────────────────
# No se llama a ninguna API hasta que el usuario elige explícitamente qué
# dashboard quiere ver (CEDEAR/GNR es mucho más lento de cargar que Transferencias).
if "pagina" not in st.session_state:
    st.session_state["pagina"] = None

if st.session_state["pagina"] is None:
    st.markdown("## 📊 Dashboards QTM Capital")
    st.caption(f"👤 {user_name}")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("📈 CEDEAR / GNR", use_container_width=True, type="primary"):
            st.session_state["pagina"] = "CEDEAR / GNR"
            st.rerun()
    with col2:
        if st.button("💸 Transferencias", use_container_width=True, type="primary"):
            st.session_state["pagina"] = "Transferencias"
            st.rerun()
    st.button("Cerrar sesión", on_click=st.logout)
    st.stop()

pagina = st.session_state["pagina"]

nav1, nav2, nav3, nav4 = st.columns([2, 2, 3, 1.3])
with nav1:
    if st.button(
        "📈 CEDEAR / GNR",
        use_container_width=True,
        type="primary" if pagina == "CEDEAR / GNR" else "secondary",
    ):
        st.session_state["pagina"] = "CEDEAR / GNR"
        st.rerun()
with nav2:
    if st.button(
        "💸 Transferencias",
        use_container_width=True,
        type="primary" if pagina == "Transferencias" else "secondary",
    ):
        st.session_state["pagina"] = "Transferencias"
        st.rerun()
with nav4:
    st.button("Cerrar sesión", on_click=st.logout, use_container_width=True)
st.caption(f"👤 {user_name}")

# ── CEDEAR / GNR ─────────────────────────────────────────────────────────────
if pagina == "CEDEAR / GNR":
    try:
        gnr, gr, mep, ts = cargar_datos()
    except Exception as e:
        st.error(f"❌ Error al conectar con la API de Cohen: {e}")
        st.stop()

    with st.expander("📊 Volatilidad / Correlación de mercado (a demanda — no se recalcula sola)"):
        col1, col2, col3 = st.columns([1, 1, 2])
        with col1:
            n_dias_mercado = st.number_input(
                "Días de historia", min_value=20, max_value=250, value=60, key="mercado_n_dias"
            )
        with col2:
            top_n_mercado = st.number_input(
                "Top N tickers por exposición", min_value=5, max_value=150, value=25, key="mercado_top_n"
            )
        with col3:
            st.caption(
                "Usa Alpaca (EEUU) para CEDEAR y Acciones con ADR conocido; Alpha Vantage sólo como respaldo. "
                "No cubre acciones argentinas locales sin ADR."
            )
        if st.button("Calcular volatilidad y correlación"):
            progress_bar = st.progress(0, text="Preparando...")

            def _reportar_progreso(hechos, total):
                if total:
                    progress_bar.progress(hechos / total, text=f"Descargando precios: {hechos}/{total}")
                else:
                    progress_bar.progress(1.0, text="Todo ya estaba en caché")

            try:
                mercado.calcular_vol_corr(
                    gnr,
                    int(n_dias_mercado),
                    int(top_n_mercado),
                    st.secrets["ALPACA_API_KEY"],
                    st.secrets["ALPACA_API_SECRET"],
                    st.secrets["ALPHAVANTAGE_API_KEY"],
                    on_progress=_reportar_progreso,
                )
                progress_bar.progress(1.0, text="✅ Listo")
            except Exception as e:
                st.error(f"❌ Error calculando volatilidad/correlación: {e}")
            finally:
                progress_bar.empty()

    # Último resultado calculado por CUALQUIER usuario en este proceso (no
    # session_state) — así todos ven el mismo cálculo sin tener que pedirlo
    # cada uno por su lado.
    html_content = generar_html(gnr, gr, mep, ts, user_name, mercado.obtener_ultimo_resultado())
    st.components.v1.html(html_content, height=900, scrolling=True)

# ── Transferencias ───────────────────────────────────────────────────────────
elif pagina == "Transferencias":
    try:
        tf, fci, ing, tf_ts, tf_range_desde = cargar_datos_transferencias()
    except Exception as e:
        st.error(f"❌ Error al conectar con la API de Cohen: {e}")
        st.stop()

    hoy_iso = date.today().isoformat()
    html_tf = tc.generar_html(tf, fci, ing, tf_range_desde, hoy_iso, tf_ts, user_name)
    st.components.v1.html(html_tf, height=900, scrolling=True)

# ── Auto-rerun de Streamlit para refrescar datos ───────────────────────────────
time.sleep(60)
st.rerun()

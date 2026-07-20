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
                valor_usd = round(valor_ars / mep, 2) if mep else None
                valor_neto_usd = round(valor_neto / mep, 2) if mep else None
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
                        "costo_usd": float(r.get("costoTotalUSD") or 0),
                        "pnl_ars": float(r.get("rendimientoARS") or 0),
                        "pnl_pct_ars": float(r.get("rendimientoPctARS") or 0),
                        "pnl_usd": float(r.get("rendimientoUSD") or 0),
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
                        "gr_ars": gr_ars,
                        "gr_pct_ars": float(it.get("porcentajeMonedaBase") or 0),
                        "gr_usd": gr_usd,
                        "gr_pct_usd": float(it.get("porcentajeRentabilidad") or 0),
                    }
                )
    return resultado


# ── Carga de datos con caché ───────────────────────────────────────────────────


def cargar_datos():
    """Obtiene token, MEP, GNR y GR. Guarda en session_state."""
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

    st.session_state["gnr"] = gnr
    st.session_state["gr"] = gr
    st.session_state["mep"] = mep
    st.session_state["ts"] = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    st.session_state["loaded_at"] = time.time()


# ── Carga de datos — Transferencias ─────────────────────────────────────────────


def _reemplazar_dia(viejas: list, nuevas: list, dia_iso: str) -> list:
    """Pisa sólo las filas de `dia_iso` con `nuevas`; conserva el resto tal cual
    estaba en caché (evita tener que volver a bajar toda la ventana de 7 días
    en cada refresco automático)."""
    resto = [r for r in viejas if r.get("fecha_dia") != dia_iso]
    return resto + nuevas


def cargar_datos_transferencias(full: bool = True):
    """Obtiene token, comitentes, transferencias/FCI/ingresos. Guarda en session_state.

    full=True  → recarga toda la ventana de tc.RANGO_DIAS días (primera carga
                 del día, o del proceso).
    full=False → sólo vuelve a pedir el día de hoy y pisa esas filas en la
                 caché existente; los demás días de la ventana quedan como
                 estaban, sin volver a bajarlos (así el refresco automático
                 cada tc.INTERVAL_S no repite todo el fetch de 7 días).
    """
    hoy = date.today()
    hoy_iso = hoy.isoformat()
    rango_desde = (hoy - timedelta(days=tc.RANGO_DIAS - 1)).isoformat() if full else hoy_iso

    status_container = st.empty()
    with status_container.status(
        "⏳ Cargando transferencias desde Cohen...", expanded=True
    ) as s:
        st.write("🔐 Obteniendo token...")
        token = tc.obtener_token(st.secrets["API_USER"], st.secrets["API_PASS"])

        st.write("📋 Obteniendo comitentes...")
        comitentes = tc.get_comitentes(token)
        ids = [c["id"] for c in comitentes]
        cmap = {c["id"]: tc.parsear_comitente(c) for c in comitentes}
        id_usuario = tc.get_id_usuario(token)

        st.write("💸 Descargando transferencias, FCI e ingresos..." if full else "💸 Actualizando movimientos de hoy...")
        with ThreadPoolExecutor(max_workers=3) as ex:
            futuros = {
                ex.submit(tc.fetch_transferencias, comitentes, rango_desde, hoy_iso, token): "tf",
                ex.submit(tc.fetch_fci, comitentes, rango_desde, hoy_iso, token, id_usuario): "fci",
                ex.submit(tc.fetch_cta_cte, ids, rango_desde, hoy_iso, token): "ing",
            }
            resultados = {}
            for fut in as_completed(futuros):
                key = futuros[fut]
                try:
                    resultados[key] = fut.result()
                except Exception:
                    resultados[key] = []

        s.update(label="✅ Transferencias cargadas", state="complete", expanded=False)

    status_container.empty()

    tf_nuevo  = tc.normalizar_transferencias(resultados.get("tf", []), cmap)
    fci_nuevo = tc.normalizar_fci(resultados.get("fci", []))
    ing_nuevo = tc.normalizar_cte(resultados.get("ing", []), cmap)

    if full:
        st.session_state["tf_data"] = tf_nuevo
        st.session_state["tf_fci"] = fci_nuevo
        st.session_state["tf_ing"] = ing_nuevo
        st.session_state["tf_range_desde"] = rango_desde
    else:
        st.session_state["tf_data"] = _reemplazar_dia(st.session_state.get("tf_data", []), tf_nuevo, hoy_iso)
        st.session_state["tf_fci"] = _reemplazar_dia(st.session_state.get("tf_fci", []), fci_nuevo, hoy_iso)
        st.session_state["tf_ing"] = _reemplazar_dia(st.session_state.get("tf_ing", []), ing_nuevo, hoy_iso)

    st.session_state["tf_range_dia"] = hoy_iso  # día de la última carga (detecta el cambio de día)
    st.session_state["tf_ts"] = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    st.session_state["tf_loaded_at"] = time.time()


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
            <th onclick="sortTable('main-table',5)" class="text-end">Precio ARS</th>
            <th onclick="sortTable('main-table',6)" class="text-end">Valor ARS</th>
            <th onclick="sortTable('main-table',7)" class="text-end">Valor Neto ARS</th>
            <th onclick="sortTable('main-table',8)" class="text-end">Valor Neto USD</th>
            <th onclick="sortTable('main-table',9)" class="text-end">Costo ARS</th>
            <th onclick="sortTable('main-table',10)" class="text-end">P&amp;L % ARS</th>
            <th onclick="sortTable('main-table',11)" class="text-end">P&amp;L USD</th>
            <th onclick="sortTable('main-table',12)" class="text-end">P&amp;L % USD</th>
          </tr></thead>
          <tbody id="main-tbody"></tbody>
        </table>
      </div>
      <div class="text-muted mt-2" style="font-size:.72rem" id="count-label"></div>
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
            <th onclick="sortTable('table-cliente',4)" class="text-end">Precio ARS</th>
            <th onclick="sortTable('table-cliente',5)" class="text-end">Valor ARS</th>
            <th onclick="sortTable('table-cliente',6)" class="text-end">Valor Neto ARS</th>
            <th onclick="sortTable('table-cliente',7)" class="text-end">Valor Neto USD</th>
            <th onclick="sortTable('table-cliente',8)" class="text-end">Costo ARS</th>
            <th onclick="sortTable('table-cliente',9)" class="text-end">P&amp;L % ARS</th>
            <th onclick="sortTable('table-cliente',10)" class="text-end">P&amp;L USD</th>
            <th onclick="sortTable('table-cliente',11)" class="text-end">P&amp;L % USD</th>
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
            <th onclick="sortTable('table-ticker',3)" class="text-end">Precio ARS</th>
            <th onclick="sortTable('table-ticker',4)" class="text-end">Valor ARS</th>
            <th onclick="sortTable('table-ticker',5)" class="text-end">Valor Neto ARS</th>
            <th onclick="sortTable('table-ticker',6)" class="text-end">Valor Neto USD</th>
            <th onclick="sortTable('table-ticker',7)" class="text-end">Costo ARS</th>
            <th onclick="sortTable('table-ticker',8)" class="text-end">P&amp;L % ARS</th>
            <th onclick="sortTable('table-ticker',9)" class="text-end">P&amp;L USD</th>
            <th onclick="sortTable('table-ticker',10)" class="text-end">P&amp;L % USD</th>
          </tr></thead>
          <tbody id="tbody-ticker"></tbody>
        </table>
      </div>
    </div>

    <!-- ── RANKERS ── -->
    <div class="tab-pane fade" id="tab-charts">
      <div class="row g-3">
        <div class="col-md-6"><div class="section-title">Top 10 ganancias por posicion (ARS)</div><canvas id="chart-gainers" height="250"></canvas></div>
        <div class="col-md-6"><div class="section-title">Top 10 perdidas por posicion (ARS)</div><canvas id="chart-losers" height="250"></canvas></div>
        <div class="col-md-6"><div class="section-title">Mejores tickers (P&amp;L total ARS)</div><canvas id="chart-ticker-gain" height="250"></canvas></div>
        <div class="col-md-6"><div class="section-title">Peores tickers (P&amp;L total ARS)</div><canvas id="chart-ticker-loss" height="250"></canvas></div>
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
            <th onclick="sortTable('gr-table',5)" class="text-end">Imp.Compra</th>
            <th onclick="sortTable('gr-table',6)" class="text-end">Imp.Venta</th>
            <th onclick="sortTable('gr-table',7)" class="text-end">GR ARS</th>
            <th onclick="sortTable('gr-table',8)" class="text-end">GR % ARS</th>
            <th onclick="sortTable('gr-table',9)" class="text-end">GR USD</th>
            <th onclick="sortTable('gr-table',10)" class="text-end">GR % USD</th>
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

// ── Utils ──────────────────────────────────────────────────────────────────
const fmt    = (v,d=0) => v==null?'':Number(v).toLocaleString('es-AR',{minimumFractionDigits:d,maximumFractionDigits:d});
const fmtPct = v => v==null?'':(v>=0?'+':'')+Number(v).toFixed(2)+'%';
const cls    = v => v>=0?'gain':'loss';
function pnlBadge(v){const c=v>=0?'badge-gain':'badge-loss';return `<span class="badge ${c}">${fmtPct(v)}</span>`;}

// ── KPIs ───────────────────────────────────────────────────────────────────
function renderKPIs(){
  const totalValor  = DATA.reduce((s,d)=>s+(d.valor_ars||0),0);
  const totalNeto   = DATA.reduce((s,d)=>s+(d.valor_neto||0),0);
  const totalPnlARS = DATA.reduce((s,d)=>s+(d.pnl_ars||0),0);
  const totalPnlUSD = DATA.reduce((s,d)=>s+(d.pnl_usd||0),0);
  const totalCosto  = DATA.reduce((s,d)=>s+(d.costo_ars||0),0);
  const pct = totalCosto ? totalPnlARS/totalCosto*100 : 0;
  const gan = DATA.filter(d=>d.pnl_ars>0).length;
  const per = DATA.filter(d=>d.pnl_ars<0).length;
  document.getElementById('kpis').innerHTML=[
    {l:'Posiciones',    v:fmt(DATA.length),   s:`${gan} ganadoras / ${per} perdedoras`, color:'var(--gold)'},
    {l:'Valor ARS',     v:'$ '+fmt(totalValor), s:'Precio mercado',                     color:'var(--gold)'},
    {l:'Valor neto ARS',v:'$ '+fmt(totalNeto),  s:'Descontando 1.1% arancel',           color:'var(--gold)'},
    {l:'P&L ARS',       v:'$ '+fmt(totalPnlARS),s:fmtPct(pct)+' sobre costo',          color:totalPnlARS>=0?'var(--ok)':'var(--bad)'},
    {l:'P&L USD',       v:'U$S '+fmt(totalPnlUSD,2),s:'Al tipo MEP',                   color:totalPnlUSD>=0?'var(--ok)':'var(--bad)'},
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
  if(cf.pnl==='gain') rows=rows.filter(d=>d.pnl_ars>0);
  if(cf.pnl==='loss') rows=rows.filter(d=>d.pnl_ars<0);
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
      <td class="text-end">${fmt(d.precio_ars,2)}</td>
      <td class="text-end">${fmt(d.valor_ars,0)}</td>
      <td class="text-end text-primary">${fmt(d.valor_neto,0)}</td>
      <td class="text-end">${d.valor_neto_usd!=null?fmt(d.valor_neto_usd,0):'-'}</td>
      <td class="text-end">${fmt(d.costo_ars,0)}</td>
      <td class="text-end">${pnlBadge(d.pnl_pct_ars)}</td>
      <td class="text-end ${cls(d.pnl_usd)}">${fmt(d.pnl_usd,2)}</td>
      <td class="text-end">${pnlBadge(d.pnl_pct_usd)}</td>
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
  const tv=rows.reduce((s,d)=>s+(d.valor_ars||0),0);
  const tn=rows.reduce((s,d)=>s+(d.valor_neto||0),0);
  const tp=rows.reduce((s,d)=>s+(d.pnl_ars||0),0);
  const tu=rows.reduce((s,d)=>s+(d.pnl_usd||0),0);
  const tc=rows.reduce((s,d)=>s+(d.costo_ars||0),0);
  const pct=tc?tp/tc*100:0;
  document.getElementById('kpis-cliente').innerHTML=`
    <div class="col-auto"><div class="card kpi-card p-3"><div class="kpi-label">Posiciones</div><div class="kpi-value">${rows.length}</div></div></div>
    <div class="col-auto"><div class="card kpi-card p-3"><div class="kpi-label">Valor ARS</div><div class="kpi-value text-primary">$ ${fmt(tv)}</div></div></div>
    <div class="col-auto"><div class="card kpi-card p-3"><div class="kpi-label">Valor neto ARS</div><div class="kpi-value text-info">$ ${fmt(tn)}</div></div></div>
    <div class="col-auto"><div class="card kpi-card p-3"><div class="kpi-label">P&L ARS</div><div class="kpi-value ${cls(tp)}">$ ${fmt(tp)}</div><div style="font-size:.68rem;color:var(--mu)">${fmtPct(pct)}</div></div></div>
    <div class="col-auto"><div class="card kpi-card p-3"><div class="kpi-label">P&L USD</div><div class="kpi-value ${cls(tu)}">U$S ${fmt(tu,2)}</div></div></div>`;
  document.getElementById('tbody-cliente').innerHTML=
    [...rows].sort((a,b)=>(b.valor_ars||0)-(a.valor_ars||0)).map(d=>`
    <tr>
      <td><strong>${d.ticker}</strong></td>
      <td class="text-muted">${d.descripcion.substring(0,32)}</td>
      <td><span class="badge bg-secondary">${d.tipo}</span></td>
      <td class="text-end">${fmt(d.cantidad,2)}</td>
      <td class="text-end">${fmt(d.precio_ars,2)}</td>
      <td class="text-end">${fmt(d.valor_ars,0)}</td>
      <td class="text-end text-primary">${fmt(d.valor_neto,0)}</td>
      <td class="text-end">${d.valor_neto_usd!=null?fmt(d.valor_neto_usd,0):'-'}</td>
      <td class="text-end">${fmt(d.costo_ars,0)}</td>
      <td class="text-end">${pnlBadge(d.pnl_pct_ars)}</td>
      <td class="text-end ${cls(d.pnl_usd)}">${fmt(d.pnl_usd,2)}</td>
      <td class="text-end">${pnlBadge(d.pnl_pct_usd)}</td>
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
  const tv  =rows.reduce((s,d)=>s+(d.valor_ars||0),0);
  const tn  =rows.reduce((s,d)=>s+(d.valor_neto||0),0);
  const tp  =rows.reduce((s,d)=>s+(d.pnl_ars||0),0);
  const tu  =rows.reduce((s,d)=>s+(d.pnl_usd||0),0);
  const tc  =rows.reduce((s,d)=>s+(d.costo_ars||0),0);
  const cant=rows.reduce((s,d)=>s+(d.cantidad||0),0);
  const pct =tc?tp/tc*100:0;
  const precio=rows[0]?.precio_ars||0;
  document.getElementById('kpis-ticker').innerHTML=`
    <div class="col-auto"><div class="card kpi-card p-3"><div class="kpi-label">Clientes</div><div class="kpi-value">${rows.length}</div></div></div>
    <div class="col-auto"><div class="card kpi-card p-3"><div class="kpi-label">Cantidad total</div><div class="kpi-value">${fmt(cant,2)}</div></div></div>
    <div class="col-auto"><div class="card kpi-card p-3"><div class="kpi-label">Precio ARS</div><div class="kpi-value text-primary">$ ${fmt(precio,2)}</div></div></div>
    <div class="col-auto"><div class="card kpi-card p-3"><div class="kpi-label">Valor total</div><div class="kpi-value">$ ${fmt(tv)}</div></div></div>
    <div class="col-auto"><div class="card kpi-card p-3"><div class="kpi-label">Valor neto</div><div class="kpi-value text-info">$ ${fmt(tn)}</div></div></div>
    <div class="col-auto"><div class="card kpi-card p-3"><div class="kpi-label">P&L ARS</div><div class="kpi-value ${cls(tp)}">$ ${fmt(tp)}</div><div style="font-size:.68rem;color:var(--mu)">${fmtPct(pct)}</div></div></div>
    <div class="col-auto"><div class="card kpi-card p-3"><div class="kpi-label">P&L USD</div><div class="kpi-value ${cls(tu)}">U$S ${fmt(tu,2)}</div></div></div>`;
  document.getElementById('tbody-ticker').innerHTML=
    [...rows].sort((a,b)=>(b.pnl_usd||0)-(a.pnl_usd||0)).map(d=>`
    <tr>
      <td>${d.nro_cuenta}</td><td>${d.cliente.substring(0,28)}</td>
      <td class="text-end">${fmt(d.cantidad,2)}</td>
      <td class="text-end">${fmt(d.precio_ars,2)}</td>
      <td class="text-end">${fmt(d.valor_ars,0)}</td>
      <td class="text-end text-primary">${fmt(d.valor_neto,0)}</td>
      <td class="text-end">${d.valor_neto_usd!=null?fmt(d.valor_neto_usd,0):'-'}</td>
      <td class="text-end">${fmt(d.costo_ars,0)}</td>
      <td class="text-end">${pnlBadge(d.pnl_pct_ars)}</td>
      <td class="text-end ${cls(d.pnl_usd)}">${fmt(d.pnl_usd,2)}</td>
      <td class="text-end">${pnlBadge(d.pnl_pct_usd)}</td>
    </tr>`).join('');
}

// ── Charts ─────────────────────────────────────────────────────────────────
function buildCharts(){
  const gridColor='rgba(122,143,181,.15)', textColor='#7a8fb5';
  Chart.defaults.color=textColor;
  Chart.defaults.borderColor=gridColor;
  const sorted=[...DATA].sort((a,b)=>b.pnl_ars-a.pnl_ars);
  const makeBar=(id,rows,color)=>new Chart(document.getElementById(id),{type:'bar',
    data:{labels:rows.map(d=>`${d.ticker}(${d.nro_cuenta})`),datasets:[{data:rows.map(d=>Math.abs(d.pnl_ars)),backgroundColor:color,borderRadius:4}]},
    options:{indexAxis:'y',plugins:{legend:{display:false}},scales:{x:{grid:{color:gridColor},ticks:{color:textColor,callback:v=>'$'+fmt(v)}},y:{grid:{color:gridColor},ticks:{color:textColor}}}}});
  makeBar('chart-gainers',sorted.slice(0,10),'#2e9e6b');
  makeBar('chart-losers',sorted.slice(-10).reverse(),'#c4453f');
  const tkMap={};
  DATA.forEach(d=>{if(!tkMap[d.ticker])tkMap[d.ticker]={pnl:0,n:0};tkMap[d.ticker].pnl+=d.pnl_ars||0;tkMap[d.ticker].n++;});
  const tkArr=Object.entries(tkMap).sort((a,b)=>b[1].pnl-a[1].pnl);
  const makeBar2=(id,rows,color)=>new Chart(document.getElementById(id),{type:'bar',
    data:{labels:rows.map(([t,v])=>`${t}(${v.n}cl)`),datasets:[{data:rows.map(([,v])=>Math.abs(v.pnl)),backgroundColor:color,borderRadius:4}]},
    options:{indexAxis:'y',plugins:{legend:{display:false}},scales:{x:{ticks:{callback:v=>'$'+fmt(v)}}}}});
  makeBar2('chart-ticker-gain',tkArr.slice(0,10),'#C9A84C');
  makeBar2('chart-ticker-loss',tkArr.slice(-10).reverse(),'#c98a1c');
}

// ── Mapa calor ─────────────────────────────────────────────────────────────
function buildHeatmap(){
  const cm={},tm={};
  DATA.forEach(d=>{
    if(!cm[d.nro_cuenta])cm[d.nro_cuenta]={n:d.cliente,v:0};cm[d.nro_cuenta].v+=d.valor_ars||0;
    if(!tm[d.ticker])tm[d.ticker]={v:0};tm[d.ticker].v+=d.valor_ars||0;
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
      ops:0, cantidad:0, importe_compra:0, importe_venta:0, gr_ars:0, gr_usd:0
    };
    const g=map[k];
    g.ops++;
    g.cantidad       += d.cantidad||0;
    g.importe_compra += d.importe_compra_ars||0;
    g.importe_venta  += d.importe_venta_ars||0;
    g.gr_ars         += d.gr_ars||0;
    g.gr_usd         += d.gr_usd||0;
  });
  return Object.values(map).map(g=>({
    ...g,
    gr_pct_ars: g.importe_compra ? g.gr_ars/g.importe_compra*100 : 0,
    gr_pct_usd: g.importe_compra ? g.gr_usd/(g.importe_compra/MEP)*100 : 0,
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
  if(grFilter.pnl==='gain')raw=raw.filter(d=>d.gr_ars>0);
  if(grFilter.pnl==='loss')raw=raw.filter(d=>d.gr_ars<0);

  const rows=agruparGR(raw).sort((a,b)=>b.gr_usd-a.gr_usd);

  const tp=rows.reduce((s,d)=>s+(d.gr_ars||0),0);
  const tu=rows.reduce((s,d)=>s+(d.gr_usd||0),0);
  const tops=rows.reduce((s,d)=>s+d.ops,0);
  document.getElementById('gr-kpis').innerHTML=`
    <div class="col-auto"><div class="card kpi-card p-3"><div class="kpi-label">Posiciones</div><div class="kpi-value">${rows.length}</div></div></div>
    <div class="col-auto"><div class="card kpi-card p-3"><div class="kpi-label">Operaciones</div><div class="kpi-value">${tops}</div></div></div>
    <div class="col-auto"><div class="card kpi-card p-3"><div class="kpi-label">GR ARS</div><div class="kpi-value ${cls(tp)}">$ ${fmt(tp)}</div></div></div>
    <div class="col-auto"><div class="card kpi-card p-3"><div class="kpi-label">GR USD</div><div class="kpi-value ${cls(tu)}">U$S ${fmt(tu,2)}</div></div></div>`;

  document.getElementById('gr-tbody').innerHTML=rows.map(d=>`
    <tr>
      <td><span class="fw-semibold">${d.nro_cuenta}</span> <span style="font-size:.75rem;color:var(--tx)">${d.cliente.substring(0,18)}</span></td>
      <td><strong>${d.ticker}</strong></td>
      <td><span class="badge bg-secondary">${d.tipo}</span></td>
      <td class="text-end">${d.ops}</td>
      <td class="text-end">${fmt(d.cantidad,2)}</td>
      <td class="text-end">${fmt(d.importe_compra,0)}</td>
      <td class="text-end">${fmt(d.importe_venta,0)}</td>
      <td class="text-end ${cls(d.gr_ars)}">${fmt(d.gr_ars,0)}</td>
      <td class="text-end">${pnlBadge(d.gr_pct_ars)}</td>
      <td class="text-end ${cls(d.gr_usd)}">${fmt(d.gr_usd,2)}</td>
      <td class="text-end">${pnlBadge(d.gr_pct_usd)}</td>
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
    posiciones: list, gr: list, mep: float, ts: str, user_name: str = ""
) -> str:
    return (
        HTML_TEMPLATE.replace("__DATA_JSON__", _js_safe(posiciones))
        .replace("__DATA_GR_JSON__", _js_safe(gr))
        .replace("__MEP__", f"{mep:.2f}")
        .replace("__MEP_DISPLAY__", f"{mep:,.2f}")
        .replace("__TS__", ts)
        .replace("__INTERVAL_S__", str(INTERVAL_S))
        .replace("__USER_NAME__", user_name)
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
    needs_refresh = False
    if "loaded_at" not in st.session_state:
        needs_refresh = True
    elif time.time() - st.session_state["loaded_at"] > INTERVAL_S:
        needs_refresh = True

    if needs_refresh:
        try:
            cargar_datos()
        except Exception as e:
            st.error(f"❌ Error al conectar con la API de Cohen: {e}")
            st.stop()

    gnr = st.session_state.get("gnr", [])
    gr = st.session_state.get("gr", [])
    mep = st.session_state.get("mep", 0.0)
    ts = st.session_state.get("ts", "")

    html_content = generar_html(gnr, gr, mep, ts, user_name)
    st.components.v1.html(html_content, height=900, scrolling=True)

# ── Transferencias ───────────────────────────────────────────────────────────
elif pagina == "Transferencias":
    hoy_iso = date.today().isoformat()
    # Carga completa (7 días): primera vez, o si cambió el día desde la
    # última carga (la ventana entera se corre un día, hay que rebajarla).
    # Si no, sólo se vuelve a pedir el día de hoy (ver cargar_datos_transferencias).
    tf_needs_full = (
        "tf_loaded_at" not in st.session_state
        or st.session_state.get("tf_range_dia") != hoy_iso
    )
    tf_needs_refresh_hoy = (
        not tf_needs_full
        and time.time() - st.session_state["tf_loaded_at"] > tc.INTERVAL_S
    )

    if tf_needs_full or tf_needs_refresh_hoy:
        try:
            cargar_datos_transferencias(full=tf_needs_full)
        except Exception as e:
            st.error(f"❌ Error al conectar con la API de Cohen: {e}")
            st.stop()

    tf = st.session_state.get("tf_data", [])
    fci = st.session_state.get("tf_fci", [])
    ing = st.session_state.get("tf_ing", [])
    tf_ts = st.session_state.get("tf_ts", "")
    tf_range_desde = st.session_state.get("tf_range_desde", hoy_iso)

    html_tf = tc.generar_html(tf, fci, ing, tf_range_desde, hoy_iso, tf_ts, user_name)
    st.components.v1.html(html_tf, height=900, scrolling=True)

# ── Auto-rerun de Streamlit para refrescar datos ───────────────────────────────
time.sleep(60)
st.rerun()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Dashboard de flujos de caja Cohen:
#   · Transferencias (retiros / pagos al cliente)
#   · FCI — Suscripciones y Rescates confirmados
#   · Ingresos — Recibo de Cobro ARS/USD, Créditos USD CABLE/MEP, etc.
#   · Por cliente — todo el movimiento de un cliente en un lugar
#
# Fuentes:
#   POST /api/transferencias/transferenciasList
#   POST /api/comprobanteSuscripcionRescate/list
#   POST /api/comprobantesCuentaCorriente/list
#
# Genera transferencias_cohen.html y lo abre en el browser.
# Refresca automáticamente cada INTERVAL_S segundos.

import json
import time
import webbrowser
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date, timedelta
from pathlib import Path

_DIR        = Path(__file__).parent
COHEN_BASE  = "https://connect.cohen.com.ar"
OUTPUT_HTML = _DIR / "transferencias_cohen.html"

FECHA_DESDE = (date.today() - timedelta(days=30)).isoformat()
FECHA_HASTA = date.today().isoformat()

PAGE_SIZE  = 500
INTERVAL_S = 5 * 60

_R = "\033[0m"; _B = "\033[1m"; _G = "\033[32m"; _C = "\033[36m"; _Y = "\033[33m"; _RE = "\033[31m"


# ── Auth ──────────────────────────────────────────────────────────────────────

def obtener_token() -> str:
    resp = requests.get("http://72.60.155.149:8000/api/cohen/login-token",
        headers={"x-user": "quantum", "x-pass": "QuantumCapital!+-"}, timeout=15)
    resp.raise_for_status()
    d = resp.json()
    if not d.get("success"):
        raise RuntimeError(f"Login fallido: {d}")
    return d["token"]


# ── Comitentes ────────────────────────────────────────────────────────────────

def get_comitentes(token: str) -> list:
    resp = requests.get(f"{COHEN_BASE}/api/posicion/getComitentesUsuario",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}, timeout=15)
    resp.raise_for_status()
    return resp.json() if isinstance(resp.json(), list) else []


def parsear_comitente(c: dict) -> tuple:
    desc = c.get("desc", "") or ""
    if " - " in desc:
        p = desc.split(" - ", 1)
        return p[0].strip(), p[1].strip()
    return str(c["id"]), desc


# ── Helpers ───────────────────────────────────────────────────────────────────

def _H(token):
    return {"Authorization": f"Bearer {token}", "Accept": "application/json",
            "Content-Type": "application/json"}


def _fmt_fecha(raw):
    if not raw:
        return ""
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(raw)[:16]


def _fecha_dia(raw):
    return (_fmt_fecha(raw) or "")[:10]


def _paginar(url, body_base, token, resultado_key="data", total_key="totalCount",
             label="registros") -> list:
    """Paginador genérico. Itera skip/take hasta traer todos los registros."""
    todos = []
    skip  = 0
    total = None
    while True:
        body  = {**body_base, "skip": skip, "take": PAGE_SIZE}
        resp  = requests.post(url, headers=_H(token), json=body, timeout=60)
        resp.raise_for_status()
        d     = resp.json()
        page  = d.get(resultado_key, [])
        if total is None:
            total = d.get(total_key, 0)
            print(f"  {label}: {_B}{total}{_R} registros")
        todos.extend(page)
        skip += len(page)
        print(f"\r    descargados: {len(todos)}/{total}", end="", flush=True)
        if not page or len(page) < PAGE_SIZE or skip >= total:
            break
    print()
    return todos


# ── Transferencias ────────────────────────────────────────────────────────────

def fetch_transferencias(comitentes: list, fd: str, fh: str, token: str) -> list:
    return _paginar(
        f"{COHEN_BASE}/api/transferencias/transferenciasList",
        {"order": [{"property": "fecha", "descending": True}],
         "fechaDesde": f"{fd}T00:00:00.000Z",
         "fechaHasta": f"{fh}T23:59:59.999Z",
         "comitentes": comitentes},
        token, resultado_key="data", total_key="totalCount", label="Transferencias"
    )


def _cat_estado_transfer(estado: str, es_terminal: bool) -> str:
    e = (estado or "").upper()
    if any(k in e for k in ("RECHAZ", "ANULAD", "CANCEL", "ERROR", "DENEGAD")):
        return "rechazada"
    if any(k in e for k in ("EJECUT", "ACREDIT", "TERMIN", "CONFIRM", "APROBAD", "REALIZAD", "FINALIZ")):
        return "ejecutada"
    if es_terminal:
        return "ejecutada"
    if any(k in e for k in ("PENDIENT", "PROCES", "ESPERA", "REVISION", "AUTORIZ", "CURSO")):
        return "pendiente"
    return "pendiente" if not e else "otro"


_CAT_TRANSFER_LABEL = {"ejecutada": "Ejecutada", "pendiente": "Pendiente",
                        "rechazada": "Rechazada", "otro": "Otro"}


def normalizar_transferencias(items: list, cmap: dict) -> list:
    out = []
    for t in items:
        nro, nombre = cmap.get(t.get("idComitente"),
                                (t.get("comitenteNumero", ""), t.get("comitenteDescripcion", "")))
        estado_raw  = t.get("estado", "") or ""
        es_terminal = bool(t.get("esTerminal"))
        cat = _cat_estado_transfer(estado_raw, es_terminal)
        out.append({
            "id":           t.get("idTransferencia"),
            "fecha":        _fmt_fecha(t.get("fecha")),
            "fecha_dia":    _fecha_dia(t.get("fecha")),
            "fecha_alta":   _fmt_fecha(t.get("fechaAlta")),
            "nro_cuenta":   str(nro),
            "cliente":      nombre,
            "moneda":       t.get("moneda", "") or "",
            "importe":      float(t.get("importe") or 0),
            "banco":        t.get("bancoDescripcion", "") or "",
            "cuenta":       t.get("cuenta", "") or "",
            "cbu":          t.get("cbu", "") or "",
            "titular":      t.get("titular", "") or "",
            "observacion":  t.get("observacion", "") or "",
            "tipo":         t.get("tipo", "") or "",
            "estado":       estado_raw,
            "estado_cat":   cat,
            "estado_label": _CAT_TRANSFER_LABEL.get(cat, "Otro"),
        })
    return out


# ── FCI — Suscripciones / Rescates ───────────────────────────────────────────

def fetch_fci(ids_comitentes: list, fd: str, fh: str, token: str) -> list:
    return _paginar(
        f"{COHEN_BASE}/api/comprobanteSuscripcionRescate/list",
        {"order": [{"property": "comprobanteFecha", "descending": True}],
         "fechaDesde": f"{fd}T00:00:00.000Z",
         "fechaHasta": f"{fh}T23:59:59.999Z",
         "idsComitentes": ids_comitentes,
         "todosComitentes": False},
        token, resultado_key="result", total_key="total", label="FCI comprobantes"
    )


def normalizar_fci(items: list, cmap: dict) -> list:
    out = []
    for t in items:
        nro, nombre = cmap.get(t.get("idComitente"),
                                (str(t.get("comitenteNumero", "")), t.get("comitenteDescripcion", "")))
        solicitud = t.get("solicitudTipo", "") or ""
        out.append({
            "id":              t.get("idComprobante"),
            "fecha":           _fmt_fecha(t.get("comprobanteFecha")),
            "fecha_dia":       _fecha_dia(t.get("comprobanteFecha")),
            "nro_cuenta":      str(nro),
            "cliente":         nombre,
            "fondo":           t.get("fondo", "") or "",
            "solicitud_tipo":  solicitud,
            "comprobante_tipo":t.get("comprobanteTipo", "") or "",
            "moneda":          t.get("monedaDescripcion", "") or "",
            "importe":         float(t.get("importe") or 0),
            "cuotapartes":     float(t.get("cantidadCuotapartes") or 0),
            "cotizacion":      float(t.get("cotizacionCuotaparte") or 0),
        })
    return out


# ── Cuenta Corriente — Ingresos ───────────────────────────────────────────────
# Incluimos TODOS los comprobantes; la clasificacion ingreso/egreso/otro
# se hace en Python para que el HTML sea rápido, y también en JS para filtros.

def _cat_cte(tipo: str) -> str:
    """Clasifica un comprobanteTipo como ingreso / egreso / fee / otro."""
    import unicodedata
    t = unicodedata.normalize("NFD", (tipo or "").upper())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")  # strip diacritics
    if any(k in t for k in ("RECIBO", "CREDITO", "INGRESO", "ACREDITACION", "AJUSTE DE INGRESO",
                             "CREDITOS VARIOS", "NOTA DE CREDITO")):
        return "ingreso"
    if any(k in t for k in ("MANTENIMIENTO",)):
        return "fee"
    if any(k in t for k in ("EGRESO", "ORDEN DE PAGO", "DEBITO", "PERDIDA", "COMISION",
                             "RETIRO", "NOTA DE DEBITO", "DEBITOS", "INTERESES SALDO",
                             "DEBITO GASTOS", "VENTA DOLAR", "COMPRA DOLAR", "RETENCION",
                             "ND POR")):
        return "egreso"
    return "otro"


def fetch_cta_cte(ids_comitentes: list, fd: str, fh: str, token: str) -> list:
    return _paginar(
        f"{COHEN_BASE}/api/comprobantesCuentaCorriente/list",
        {"order": [{"property": "fechaConcertacion", "descending": True}],
         "fechaConcertacionDesde": f"{fd}T00:00:00.000Z",
         "fechaConcertacionHasta": f"{fh}T23:59:59.999Z",
         "idsComitentes": ids_comitentes},
        token, resultado_key="result", total_key="total", label="Comprobantes CTA CTE"
    )


def normalizar_cte(items: list, cmap: dict) -> list:
    out = []
    for t in items:
        nro, nombre = cmap.get(t.get("idComitente"),
                                (str(t.get("comitenteNumero", "")), t.get("comitenteDescripcion", "")))
        tipo = t.get("comprobanteTipo", "") or ""
        cat  = _cat_cte(tipo)
        out.append({
            "id":          t.get("idComprobanteCuentaCorriente"),
            "fecha":       _fmt_fecha(t.get("fechaConcertacion")),
            "fecha_dia":   _fecha_dia(t.get("fechaConcertacion")),
            "fecha_vto":   _fecha_dia(t.get("fechaVencimiento")),
            "nro_cuenta":  str(nro),
            "cliente":     nombre,
            "tipo":        tipo,
            "cat":         cat,
            "moneda":      t.get("monedaDescripcion", "") or "",
            "importe":     float(t.get("importe") or 0),
            "es_gasto":    bool(t.get("esGasto")),
        })
    return out


# ── HTML ──────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Flujos Cohen</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
<style>
body{background:#f0f2f5;font-size:.875rem}
.navbar{background:linear-gradient(135deg,#16213e,#0f3460)}
.kpi-card{border:none;border-radius:12px;box-shadow:0 2px 8px rgba(0,0,0,.08)}
.kpi-value{font-size:1.4rem;font-weight:700}
.kpi-label{font-size:.68rem;color:#6c757d;text-transform:uppercase;letter-spacing:.06em}
.table-wrapper{max-height:520px;overflow-y:auto}
table th{position:sticky;top:0;background:#fff;z-index:1;cursor:pointer;white-space:nowrap;user-select:none}
table th:hover{background:#f0f2f5}
table th.sort-asc::after{content:" \2191"}table th.sort-desc::after{content:" \2193"}
table th:not(.sort-asc):not(.sort-desc):not(.ns)::after{content:" \2195";color:#ccc;font-size:.7rem}
.filter-bar{background:#fff;border-radius:10px;padding:.8rem 1rem;box-shadow:0 1px 4px rgba(0,0,0,.06);margin-bottom:1rem}
.badge-ejecutada{background:#d1e7dd;color:#0a3622}
.badge-pendiente{background:#fff3cd;color:#664d03}
.badge-rechazada{background:#f8d7da;color:#58151c}
.badge-ingreso{background:#cfe2ff;color:#084298}
.badge-egreso{background:#f8d7da;color:#58151c}
.badge-fee{background:#e2e3e5;color:#41464b}
.badge-otro{background:#e2e3e5;color:#41464b}
.badge-rescate{background:#fff3cd;color:#664d03}
.badge-suscripcion{background:#d1e7dd;color:#0a3622}
.autocomplete-wrap{position:relative}
.autocomplete-list{position:absolute;top:100%;left:0;right:0;background:#fff;border:1px solid #dee2e6;
  border-radius:4px;max-height:220px;overflow-y:auto;z-index:200;display:none}
.autocomplete-item{padding:5px 10px;cursor:pointer;font-size:.82rem}
.autocomplete-item:hover,.autocomplete-item.active{background:#e9ecef}
.btn-hoy{font-size:.82rem;padding:.3rem .8rem}
</style>
</head>
<body>

<nav class="navbar navbar-dark py-2 mb-3">
  <div class="container-fluid">
    <span class="navbar-brand fw-bold">Flujos Cohen</span>
    <div class="d-flex align-items-center gap-3">
      <button class="btn btn-warning btn-hoy fw-bold" onclick="verHoy()">&#128197; VER HOY</button>
      <span class="text-white-50" style="font-size:.72rem">
        Datos __DESDE__ → __HASTA__ &nbsp;|&nbsp; __TS__
        &nbsp;|&nbsp; Refresco: <span id="countdown"></span>
      </span>
    </div>
  </div>
</nav>

<div class="container-fluid px-4">

  <ul class="nav nav-tabs mb-0" id="tabs">
    <li class="nav-item"><a class="nav-link active" data-bs-toggle="tab" href="#tab-transfer">Transferencias</a></li>
    <li class="nav-item"><a class="nav-link" data-bs-toggle="tab" href="#tab-fci">FCI Susc/Rescate</a></li>
    <li class="nav-item"><a class="nav-link" data-bs-toggle="tab" href="#tab-ing">Ingresos</a></li>
    <li class="nav-item"><a class="nav-link" data-bs-toggle="tab" href="#tab-cliente">Por cliente</a></li>
  </ul>

  <div class="tab-content bg-white rounded-bottom rounded-end shadow-sm p-3">

    <!-- ═══════════════════════════════════════════
         TAB 1 — TRANSFERENCIAS
    ════════════════════════════════════════════ -->
    <div class="tab-pane fade show active" id="tab-transfer">
      <div class="filter-bar d-flex flex-wrap gap-2 align-items-end">
        <div><label class="form-label mb-1" style="font-size:.7rem">Desde</label>
          <input type="date" id="tf-desde" class="form-control form-control-sm" style="width:136px"></div>
        <div><label class="form-label mb-1" style="font-size:.7rem">Hasta</label>
          <input type="date" id="tf-hasta" class="form-control form-control-sm" style="width:136px"></div>
        <div><label class="form-label mb-1" style="font-size:.7rem">Estado</label>
          <select id="tf-estado" class="form-select form-select-sm" style="width:130px">
            <option value="">Todos</option>
            <option value="pendiente">Pendiente</option>
            <option value="ejecutada">Ejecutada</option>
            <option value="rechazada">Rechazada</option>
          </select></div>
        <div><label class="form-label mb-1" style="font-size:.7rem">Moneda</label>
          <select id="tf-moneda" class="form-select form-select-sm" style="width:120px">
            <option value="">Todas</option>
          </select></div>
        <div><label class="form-label mb-1" style="font-size:.7rem">Cliente</label>
          <input type="text" id="tf-cliente" class="form-control form-control-sm" style="width:190px"
                 placeholder="Nombre o nro cuenta..."></div>
        <div class="d-flex gap-2">
          <button class="btn btn-primary btn-sm" onclick="tfAplicar()">Aplicar</button>
          <button class="btn btn-warning btn-sm" onclick="tfSoloPend()">Solo pendientes</button>
          <button class="btn btn-outline-secondary btn-sm" onclick="tfReset()">Limpiar</button>
          <button class="btn btn-outline-success btn-sm" onclick="copiar('tbl-tf')">Copiar</button>
        </div>
        <div class="ms-auto text-muted" style="font-size:.72rem" id="tf-count"></div>
      </div>
      <div class="row g-3 mb-2" id="tf-kpis"></div>
      <div class="text-muted mb-2" style="font-size:.72rem" id="tf-moneda-break"></div>
      <div class="table-wrapper">
        <table class="table table-sm table-hover mb-0" id="tbl-tf">
          <thead><tr>
            <th onclick="sort('tbl-tf',0)">Fecha</th>
            <th onclick="sort('tbl-tf',1)">Cuenta</th>
            <th onclick="sort('tbl-tf',2)">Cliente</th>
            <th onclick="sort('tbl-tf',3)">Tipo</th>
            <th onclick="sort('tbl-tf',4)">Moneda</th>
            <th onclick="sort('tbl-tf',5)" class="text-end">Importe</th>
            <th onclick="sort('tbl-tf',6)">Banco / Cuenta</th>
            <th onclick="sort('tbl-tf',7)">CBU</th>
            <th onclick="sort('tbl-tf',8)">Estado</th>
            <th onclick="sort('tbl-tf',9)">Alta</th>
          </tr></thead>
          <tbody id="tbody-tf"></tbody>
        </table>
      </div>
    </div>

    <!-- ═══════════════════════════════════════════
         TAB 2 — FCI SUSCRIPCIONES / RESCATES
    ════════════════════════════════════════════ -->
    <div class="tab-pane fade" id="tab-fci">
      <div class="filter-bar d-flex flex-wrap gap-2 align-items-end">
        <div><label class="form-label mb-1" style="font-size:.7rem">Desde</label>
          <input type="date" id="fci-desde" class="form-control form-control-sm" style="width:136px"></div>
        <div><label class="form-label mb-1" style="font-size:.7rem">Hasta</label>
          <input type="date" id="fci-hasta" class="form-control form-control-sm" style="width:136px"></div>
        <div><label class="form-label mb-1" style="font-size:.7rem">Operación</label>
          <select id="fci-tipo" class="form-select form-select-sm" style="width:130px">
            <option value="">Todas</option>
            <option value="Rescate">Rescate</option>
            <option value="Suscripción">Suscripción</option>
          </select></div>
        <div><label class="form-label mb-1" style="font-size:.7rem">Moneda</label>
          <select id="fci-moneda" class="form-select form-select-sm" style="width:120px">
            <option value="">Todas</option>
          </select></div>
        <div><label class="form-label mb-1" style="font-size:.7rem">Fondo</label>
          <input type="text" id="fci-fondo" class="form-control form-control-sm" style="width:200px"
                 placeholder="Nombre del fondo..."></div>
        <div><label class="form-label mb-1" style="font-size:.7rem">Cliente</label>
          <input type="text" id="fci-cliente" class="form-control form-control-sm" style="width:160px"
                 placeholder="Nombre o nro cuenta..."></div>
        <div class="d-flex gap-2">
          <button class="btn btn-primary btn-sm" onclick="fciAplicar()">Aplicar</button>
          <button class="btn btn-warning btn-sm" onclick="fciSoloRescates()">Solo rescates</button>
          <button class="btn btn-outline-secondary btn-sm" onclick="fciReset()">Limpiar</button>
          <button class="btn btn-outline-success btn-sm" onclick="copiar('tbl-fci')">Copiar</button>
        </div>
        <div class="ms-auto text-muted" style="font-size:.72rem" id="fci-count"></div>
      </div>
      <div class="row g-3 mb-2" id="fci-kpis"></div>
      <div class="table-wrapper">
        <table class="table table-sm table-hover mb-0" id="tbl-fci">
          <thead><tr>
            <th onclick="sort('tbl-fci',0)">Fecha</th>
            <th onclick="sort('tbl-fci',1)">Cuenta</th>
            <th onclick="sort('tbl-fci',2)">Cliente</th>
            <th onclick="sort('tbl-fci',3)">Operación</th>
            <th onclick="sort('tbl-fci',4)">Fondo</th>
            <th onclick="sort('tbl-fci',5)">Moneda</th>
            <th onclick="sort('tbl-fci',6)" class="text-end">Importe</th>
            <th onclick="sort('tbl-fci',7)" class="text-end">Cuotapartes</th>
            <th onclick="sort('tbl-fci',8)" class="text-end">Cotización</th>
          </tr></thead>
          <tbody id="tbody-fci"></tbody>
        </table>
      </div>
    </div>

    <!-- ═══════════════════════════════════════════
         TAB 3 — INGRESOS (CTA CTE)
    ════════════════════════════════════════════ -->
    <div class="tab-pane fade" id="tab-ing">
      <div class="filter-bar d-flex flex-wrap gap-2 align-items-end">
        <div><label class="form-label mb-1" style="font-size:.7rem">Desde</label>
          <input type="date" id="ing-desde" class="form-control form-control-sm" style="width:136px"></div>
        <div><label class="form-label mb-1" style="font-size:.7rem">Hasta</label>
          <input type="date" id="ing-hasta" class="form-control form-control-sm" style="width:136px"></div>
        <div><label class="form-label mb-1" style="font-size:.7rem">Categoría</label>
          <select id="ing-cat" class="form-select form-select-sm" style="width:120px">
            <option value="ingreso">Ingresos</option>
            <option value="">Todo</option>
            <option value="egreso">Egresos</option>
            <option value="fee">Fee/Mant.</option>
            <option value="otro">Otros</option>
          </select></div>
        <div><label class="form-label mb-1" style="font-size:.7rem">Moneda</label>
          <select id="ing-moneda" class="form-select form-select-sm" style="width:120px">
            <option value="">Todas</option>
          </select></div>
        <div><label class="form-label mb-1" style="font-size:.7rem">Tipo</label>
          <select id="ing-tipo" class="form-select form-select-sm" style="width:220px">
            <option value="">Todos</option>
          </select></div>
        <div><label class="form-label mb-1" style="font-size:.7rem">Cliente</label>
          <input type="text" id="ing-cliente" class="form-control form-control-sm" style="width:160px"
                 placeholder="Nombre o nro cuenta..."></div>
        <div class="d-flex gap-2">
          <button class="btn btn-primary btn-sm" onclick="ingAplicar()">Aplicar</button>
          <button class="btn btn-warning btn-sm" onclick="ingSoloIngresos()">Solo ingresos ARS/USD</button>
          <button class="btn btn-outline-secondary btn-sm" onclick="ingReset()">Limpiar</button>
          <button class="btn btn-outline-success btn-sm" onclick="copiar('tbl-ing')">Copiar</button>
        </div>
        <div class="ms-auto text-muted" style="font-size:.72rem" id="ing-count"></div>
      </div>
      <div class="row g-3 mb-2" id="ing-kpis"></div>
      <div class="table-wrapper">
        <table class="table table-sm table-hover mb-0" id="tbl-ing">
          <thead><tr>
            <th onclick="sort('tbl-ing',0)">Fecha</th>
            <th onclick="sort('tbl-ing',1)">Cuenta</th>
            <th onclick="sort('tbl-ing',2)">Cliente</th>
            <th onclick="sort('tbl-ing',3)">Tipo de comprobante</th>
            <th onclick="sort('tbl-ing',4)">Moneda</th>
            <th onclick="sort('tbl-ing',5)" class="text-end">Importe</th>
            <th onclick="sort('tbl-ing',6)">Vto</th>
            <th onclick="sort('tbl-ing',7)" class="ns">Cat.</th>
          </tr></thead>
          <tbody id="tbody-ing"></tbody>
        </table>
      </div>
    </div>

    <!-- ═══════════════════════════════════════════
         TAB 4 — POR CLIENTE
    ════════════════════════════════════════════ -->
    <div class="tab-pane fade" id="tab-cliente">
      <div class="d-flex gap-2 align-items-center mb-3">
        <label class="fw-bold text-nowrap" style="font-size:.8rem">Cliente:</label>
        <div class="autocomplete-wrap" style="max-width:400px;width:100%">
          <input id="cli-input" class="form-control form-control-sm"
                 placeholder="Buscar por nombre o nro cuenta..." autocomplete="off">
          <div id="cli-list" class="autocomplete-list"></div>
        </div>
      </div>
      <div class="row g-3 mb-3" id="cli-kpis"></div>

      <p class="text-muted fw-bold mb-1" style="font-size:.75rem">TRANSFERENCIAS</p>
      <div class="table-wrapper mb-3" style="max-height:220px">
        <table class="table table-sm table-hover mb-0" id="tbl-cli-tf">
          <thead><tr>
            <th>Fecha</th><th>Tipo</th><th>Moneda</th>
            <th class="text-end">Importe</th><th>Banco/Cuenta</th><th>CBU</th><th>Estado</th>
          </tr></thead>
          <tbody id="tbody-cli-tf"></tbody>
        </table>
      </div>

      <p class="text-muted fw-bold mb-1" style="font-size:.75rem">FCI — SUSCRIPCIONES / RESCATES</p>
      <div class="table-wrapper mb-3" style="max-height:220px">
        <table class="table table-sm table-hover mb-0" id="tbl-cli-fci">
          <thead><tr>
            <th>Fecha</th><th>Operación</th><th>Fondo</th>
            <th>Moneda</th><th class="text-end">Importe</th><th class="text-end">Cuotapartes</th>
          </tr></thead>
          <tbody id="tbody-cli-fci"></tbody>
        </table>
      </div>

      <p class="text-muted fw-bold mb-1" style="font-size:.75rem">INGRESOS / MOVIMIENTOS CTA CTE</p>
      <div class="table-wrapper" style="max-height:220px">
        <table class="table table-sm table-hover mb-0" id="tbl-cli-ing">
          <thead><tr>
            <th>Fecha</th><th>Tipo de comprobante</th>
            <th>Moneda</th><th class="text-end">Importe</th><th>Cat.</th>
          </tr></thead>
          <tbody id="tbody-cli-ing"></tbody>
        </table>
      </div>
    </div>

  </div><!-- tab-content -->
</div><!-- container -->

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"></script>
<script>
const TF  = __TF_JSON__;
const FCI = __FCI_JSON__;
const ING = __ING_JSON__;

const fmt  = (v,d=2) => v==null?'':Number(v).toLocaleString('es-AR',{minimumFractionDigits:d,maximumFractionDigits:d});
const fmtI = v => v==null?'':Number(v).toLocaleString('es-AR',{minimumFractionDigits:2,maximumFractionDigits:2});

// ── Badge helpers ─────────────────────────────────────────────────────────
function badgeTf(d){return `<span class="badge badge-${d.estado_cat}">${d.estado_label}${d.estado&&d.estado!==d.estado_label?' — '+d.estado:''}</span>`;}
function badgeFci(d){const cat=d.solicitud_tipo.includes('Rescate')?'rescate':'suscripcion';const lbl=d.solicitud_tipo;return `<span class="badge badge-${cat}">${lbl}</span>`;}
function badgeIng(d){const m={'ingreso':'Ingreso','egreso':'Egreso','fee':'Fee/Mant.','otro':'Otro'};return `<span class="badge badge-${d.cat}">${m[d.cat]||d.cat}</span>`;}

// ── VER HOY ──────────────────────────────────────────────────────────────
function verHoy(){
  const hoy = new Date().toLocaleDateString('en-CA'); // YYYY-MM-DD
  ['tf-desde','tf-hasta','fci-desde','fci-hasta','ing-desde','ing-hasta'].forEach(id=>{
    const el=document.getElementById(id); if(el) el.value=hoy;
  });
  tfAplicar(); fciAplicar(); ingAplicar();
  document.querySelector('a[href="#tab-transfer"]').click();
}

// ── Sort ──────────────────────────────────────────────────────────────────
const ss={};
function sort(id,col){
  const tbl=document.getElementById(id);
  const key=id+'_'+col;const asc=ss[key]!==true;ss[key]=asc;
  tbl.querySelectorAll('th').forEach((th,i)=>{th.classList.remove('sort-asc','sort-desc');if(i===col)th.classList.add(asc?'sort-asc':'sort-desc');});
  const tb=tbl.querySelector('tbody');
  [...tb.querySelectorAll('tr')].sort((a,b)=>{
    const av=a.cells[col]?.textContent.trim().replace(/[\$\s%\.]/g,'').replace(',','')||'';
    const bv=b.cells[col]?.textContent.trim().replace(/[\$\s%\.]/g,'').replace(',','')||'';
    const an=parseFloat(av),bn=parseFloat(bv);
    if(!isNaN(an)&&!isNaN(bn)&&av!==''&&bv!=='')return asc?an-bn:bn-an;
    return asc?av.localeCompare(bv,'es'):bv.localeCompare(av,'es');
  }).forEach(r=>tb.appendChild(r));
}

// ── Copiar ────────────────────────────────────────────────────────────────
function copiar(tableId){
  const tbl=document.getElementById(tableId);
  const tsv=[...tbl.querySelectorAll('tr')].map(r=>[...r.cells].map(c=>c.textContent.trim()).join('\t')).join('\n');
  navigator.clipboard.writeText(tsv).then(()=>{
    const btn=event.target;const orig=btn.textContent;
    btn.textContent='Copiado!';btn.classList.add('btn-success');btn.classList.remove('btn-outline-success');
    setTimeout(()=>{btn.textContent=orig;btn.classList.remove('btn-success');btn.classList.add('btn-outline-success');},1500);
  });
}

// ─────────────────────────────────────────────────────────────────────────
// TAB 1 — TRANSFERENCIAS
// ─────────────────────────────────────────────────────────────────────────
let tfFilt=[...TF];

function tfInitFiltros(){
  const fechas=TF.map(d=>d.fecha_dia).filter(Boolean).sort();
  document.getElementById('tf-desde').value=fechas[0]||'';
  document.getElementById('tf-hasta').value=fechas[fechas.length-1]||'';
  const monedas=[...new Set(TF.map(d=>d.moneda).filter(Boolean))].sort();
  const sel=document.getElementById('tf-moneda');
  monedas.forEach(m=>{const o=document.createElement('option');o.value=o.textContent=m;sel.appendChild(o);});
}

function tfAplicar(){
  const d=document.getElementById('tf-desde').value;
  const h=document.getElementById('tf-hasta').value;
  const e=document.getElementById('tf-estado').value;
  const m=document.getElementById('tf-moneda').value;
  const c=document.getElementById('tf-cliente').value.toLowerCase();
  tfFilt=TF.filter(r=>
    (!d||r.fecha_dia>=d)&&(!h||r.fecha_dia<=h)&&(!e||r.estado_cat===e)&&
    (!m||r.moneda===m)&&(!c||(r.cliente||'').toLowerCase().includes(c)||r.nro_cuenta.includes(c))
  );
  tfRender();
}
function tfSoloPend(){document.getElementById('tf-estado').value='pendiente';tfAplicar();}
function tfReset(){
  document.getElementById('tf-estado').value='';document.getElementById('tf-moneda').value='';
  document.getElementById('tf-cliente').value='';tfInitFiltros();tfFilt=[...TF];tfRender();
}
function tfRender(){
  const p=tfFilt.filter(d=>d.estado_cat==='pendiente').length;
  const e=tfFilt.filter(d=>d.estado_cat==='ejecutada').length;
  const r=tfFilt.filter(d=>d.estado_cat==='rechazada').length;
  document.getElementById('tf-kpis').innerHTML=[
    {l:'Total',v:tfFilt.length,c:'#0d6efd'},
    {l:'Pendientes',v:p,c:'#997404'},
    {l:'Ejecutadas',v:e,c:'#198754'},
    {l:'Rechazadas/Otras',v:r+tfFilt.filter(d=>d.estado_cat==='otro').length,c:'#dc3545'},
  ].map(k=>`<div class="col-sm-6 col-xl-3"><div class="card kpi-card p-3">
    <div class="kpi-label">${k.l}</div><div class="kpi-value mt-1" style="color:${k.c}">${k.v}</div>
  </div></div>`).join('');
  const bm={};tfFilt.forEach(d=>{bm[d.moneda]=(bm[d.moneda]||0)+(d.importe||0);});
  document.getElementById('tf-moneda-break').textContent='Importe total: '+
    Object.entries(bm).map(([m,v])=>`${m||'?'} ${fmtI(v)}`).join('  |  ');
  document.getElementById('tf-count').textContent=`${tfFilt.length} de ${TF.length} transferencias`;
  document.getElementById('tbody-tf').innerHTML=tfFilt.map(d=>`
    <tr>
      <td>${d.fecha}</td>
      <td>${d.nro_cuenta}</td>
      <td style="max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${d.cliente}">${d.cliente}</td>
      <td>${d.tipo}</td>
      <td>${d.moneda}</td>
      <td class="text-end">${fmtI(d.importe)}</td>
      <td style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${d.banco} ${d.cuenta}</td>
      <td>${d.cbu}</td>
      <td>${badgeTf(d)}</td>
      <td>${d.fecha_alta}</td>
    </tr>`).join('');
}

// ─────────────────────────────────────────────────────────────────────────
// TAB 2 — FCI
// ─────────────────────────────────────────────────────────────────────────
let fciFilt=[...FCI];

function fciInitFiltros(){
  const fechas=FCI.map(d=>d.fecha_dia).filter(Boolean).sort();
  document.getElementById('fci-desde').value=fechas[0]||'';
  document.getElementById('fci-hasta').value=fechas[fechas.length-1]||'';
  const monedas=[...new Set(FCI.map(d=>d.moneda).filter(Boolean))].sort();
  const sel=document.getElementById('fci-moneda');
  monedas.forEach(m=>{const o=document.createElement('option');o.value=o.textContent=m;sel.appendChild(o);});
}

function fciAplicar(){
  const d=document.getElementById('fci-desde').value;
  const h=document.getElementById('fci-hasta').value;
  const t=document.getElementById('fci-tipo').value;
  const m=document.getElementById('fci-moneda').value;
  const f=document.getElementById('fci-fondo').value.toLowerCase();
  const c=document.getElementById('fci-cliente').value.toLowerCase();
  fciFilt=FCI.filter(r=>
    (!d||r.fecha_dia>=d)&&(!h||r.fecha_dia<=h)&&
    (!t||r.solicitud_tipo.includes(t))&&(!m||r.moneda===m)&&
    (!f||(r.fondo||'').toLowerCase().includes(f))&&
    (!c||(r.cliente||'').toLowerCase().includes(c)||r.nro_cuenta.includes(c))
  );
  fciRender();
}
function fciSoloRescates(){document.getElementById('fci-tipo').value='Rescate';fciAplicar();}
function fciReset(){
  document.getElementById('fci-tipo').value='';document.getElementById('fci-moneda').value='';
  document.getElementById('fci-fondo').value='';document.getElementById('fci-cliente').value='';
  fciInitFiltros();fciFilt=[...FCI];fciRender();
}
function fciRender(){
  const susc=fciFilt.filter(d=>!d.solicitud_tipo.includes('Rescate'));
  const resc=fciFilt.filter(d=>d.solicitud_tipo.includes('Rescate'));
  const impSusc=susc.reduce((s,d)=>s+(d.importe||0),0);
  const impResc=resc.reduce((s,d)=>s+(d.importe||0),0);
  document.getElementById('fci-kpis').innerHTML=[
    {l:'Total',v:fciFilt.length,c:'#0d6efd'},
    {l:'Suscripciones',v:susc.length,c:'#198754'},
    {l:'Importe Susc.',v:fmtI(impSusc),c:'#198754'},
    {l:'Rescates',v:resc.length,c:'#997404'},
    {l:'Importe Rescates',v:fmtI(impResc),c:'#997404'},
  ].map(k=>`<div class="col-sm-6 col-xl"><div class="card kpi-card p-3">
    <div class="kpi-label">${k.l}</div><div class="kpi-value mt-1" style="color:${k.c}">${k.v}</div>
  </div></div>`).join('');
  document.getElementById('fci-count').textContent=`${fciFilt.length} de ${FCI.length}`;
  document.getElementById('tbody-fci').innerHTML=fciFilt.map(d=>`
    <tr>
      <td>${d.fecha}</td>
      <td>${d.nro_cuenta}</td>
      <td style="max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${d.cliente}</td>
      <td>${badgeFci(d)}</td>
      <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${d.fondo}">${d.fondo}</td>
      <td>${d.moneda}</td>
      <td class="text-end">${fmtI(d.importe)}</td>
      <td class="text-end">${fmt(d.cuotapartes,3)}</td>
      <td class="text-end">${fmt(d.cotizacion,6)}</td>
    </tr>`).join('');
}

// ─────────────────────────────────────────────────────────────────────────
// TAB 3 — INGRESOS (CTA CTE)
// ─────────────────────────────────────────────────────────────────────────
let ingFilt=[...ING];

function ingInitFiltros(){
  const fechas=ING.map(d=>d.fecha_dia).filter(Boolean).sort();
  document.getElementById('ing-desde').value=fechas[0]||'';
  document.getElementById('ing-hasta').value=fechas[fechas.length-1]||'';
  const monedas=[...new Set(ING.map(d=>d.moneda).filter(Boolean))].sort();
  const selM=document.getElementById('ing-moneda');
  monedas.forEach(m=>{const o=document.createElement('option');o.value=o.textContent=m;selM.appendChild(o);});
  const tipos=[...new Set(ING.map(d=>d.tipo).filter(Boolean))].sort();
  const selT=document.getElementById('ing-tipo');
  tipos.forEach(t=>{const o=document.createElement('option');o.value=o.textContent=t;selT.appendChild(o);});
}

function ingAplicar(){
  const d=document.getElementById('ing-desde').value;
  const h=document.getElementById('ing-hasta').value;
  const cat=document.getElementById('ing-cat').value;
  const m=document.getElementById('ing-moneda').value;
  const t=document.getElementById('ing-tipo').value;
  const c=document.getElementById('ing-cliente').value.toLowerCase();
  ingFilt=ING.filter(r=>
    (!d||r.fecha_dia>=d)&&(!h||r.fecha_dia<=h)&&(!cat||r.cat===cat)&&
    (!m||r.moneda===m)&&(!t||r.tipo===t)&&
    (!c||(r.cliente||'').toLowerCase().includes(c)||r.nro_cuenta.includes(c))
  );
  ingRender();
}
function ingSoloIngresos(){
  document.getElementById('ing-cat').value='ingreso';
  document.getElementById('ing-tipo').value='';
  ingAplicar();
}
function ingReset(){
  document.getElementById('ing-cat').value='ingreso';
  document.getElementById('ing-moneda').value='';
  document.getElementById('ing-tipo').value='';
  document.getElementById('ing-cliente').value='';
  ingInitFiltros();ingFilt=ING.filter(d=>d.cat==='ingreso');ingRender();
}
function ingRender(){
  const ings=ingFilt.filter(d=>d.cat==='ingreso');
  const bm={};ings.forEach(d=>{bm[d.moneda]=(bm[d.moneda]||0)+(d.importe||0);});
  const kpis=[{l:'Total registros',v:ingFilt.length,c:'#0d6efd'},{l:'Ingresos',v:ings.length,c:'#198754'}];
  Object.entries(bm).forEach(([m,v])=>kpis.push({l:'Total '+m,v:fmtI(v),c:'#0d6efd'}));
  document.getElementById('ing-kpis').innerHTML=kpis.map(k=>`
    <div class="col-sm-6 col-xl"><div class="card kpi-card p-3">
      <div class="kpi-label">${k.l}</div><div class="kpi-value mt-1" style="color:${k.c}">${k.v}</div>
    </div></div>`).join('');
  document.getElementById('ing-count').textContent=`${ingFilt.length} de ${ING.length} comprobantes`;
  document.getElementById('tbody-ing').innerHTML=ingFilt.map(d=>`
    <tr>
      <td>${d.fecha}</td>
      <td>${d.nro_cuenta}</td>
      <td style="max-width:130px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${d.cliente}</td>
      <td>${d.tipo}</td>
      <td>${d.moneda}</td>
      <td class="text-end">${fmtI(d.importe)}</td>
      <td>${d.fecha_vto}</td>
      <td>${badgeIng(d)}</td>
    </tr>`).join('');
}

// ─────────────────────────────────────────────────────────────────────────
// TAB 4 — POR CLIENTE
// ─────────────────────────────────────────────────────────────────────────
function makeAutocomplete(inputId, listId, items, onSelect){
  const inp=document.getElementById(inputId), lst=document.getElementById(listId);
  let idx=-1;
  inp.addEventListener('input',()=>{
    const q=inp.value.toLowerCase();
    const ms=q?items.filter(i=>i.label.toLowerCase().includes(q)).slice(0,15):items.slice(0,15);
    lst.innerHTML=ms.map(m=>`<div class="autocomplete-item" data-val="${m.value}">${m.label}</div>`).join('');
    lst.style.display=ms.length?'block':'none';idx=-1;
  });
  inp.addEventListener('keydown',e=>{
    const it=[...lst.querySelectorAll('.autocomplete-item')];
    if(e.key==='ArrowDown'){idx=Math.min(idx+1,it.length-1);}
    else if(e.key==='ArrowUp'){idx=Math.max(idx-1,0);}
    else if(e.key==='Enter'&&idx>=0){it[idx].click();return;}
    else if(e.key==='Escape'){lst.style.display='none';}
    it.forEach((el,i)=>el.classList.toggle('active',i===idx));
  });
  lst.addEventListener('click',e=>{
    const el=e.target.closest('.autocomplete-item');
    if(!el)return; inp.value=el.textContent; lst.style.display='none'; onSelect(el.dataset.val);
  });
  document.addEventListener('click',e=>{if(!inp.contains(e.target)&&!lst.contains(e.target))lst.style.display='none';});
}

function buildClienteTab(){
  const all=[...new Map([...TF,...FCI,...ING].map(d=>[d.nro_cuenta,d])).values()]
    .sort((a,b)=>a.nro_cuenta.localeCompare(b.nro_cuenta))
    .map(d=>({value:d.nro_cuenta,label:`${d.nro_cuenta} - ${d.cliente}`}));
  makeAutocomplete('cli-input','cli-list',all,nro=>renderCliente(nro));
  if(all.length){document.getElementById('cli-input').value=all[0].label;renderCliente(all[0].value);}
}

function renderCliente(nro){
  const tfs  = [...TF .filter(d=>d.nro_cuenta===nro)].sort((a,b)=>b.fecha.localeCompare(a.fecha));
  const fcis = [...FCI.filter(d=>d.nro_cuenta===nro)].sort((a,b)=>b.fecha.localeCompare(a.fecha));
  const ings = [...ING.filter(d=>d.nro_cuenta===nro)].sort((a,b)=>b.fecha.localeCompare(a.fecha));
  const pend = tfs.filter(d=>d.estado_cat==='pendiente').length;
  const resc = fcis.filter(d=>d.solicitud_tipo.includes('Rescate')).length;
  const ingTot = ings.filter(d=>d.cat==='ingreso').length;
  const nombre = (tfs[0]||fcis[0]||ings[0]||{}).cliente||'';
  document.getElementById('cli-kpis').innerHTML=`
    <div class="col-auto"><div class="card kpi-card p-3"><div class="kpi-label">Cliente</div><div style="font-size:1rem;font-weight:600">${nombre}</div></div></div>
    <div class="col-auto"><div class="card kpi-card p-3"><div class="kpi-label">Transfers pendientes</div><div class="kpi-value" style="color:#997404">${pend}</div></div></div>
    <div class="col-auto"><div class="card kpi-card p-3"><div class="kpi-label">Transfers total</div><div class="kpi-value">${tfs.length}</div></div></div>
    <div class="col-auto"><div class="card kpi-card p-3"><div class="kpi-label">Rescates FCI</div><div class="kpi-value" style="color:#997404">${resc}</div></div></div>
    <div class="col-auto"><div class="card kpi-card p-3"><div class="kpi-label">Susc. FCI</div><div class="kpi-value" style="color:#198754">${fcis.length-resc}</div></div></div>
    <div class="col-auto"><div class="card kpi-card p-3"><div class="kpi-label">Ingresos</div><div class="kpi-value" style="color:#084298">${ingTot}</div></div></div>`;
  document.getElementById('tbody-cli-tf').innerHTML=tfs.map(d=>`
    <tr><td>${d.fecha}</td><td>${d.tipo}</td><td>${d.moneda}</td>
    <td class="text-end">${fmtI(d.importe)}</td><td>${d.banco} ${d.cuenta}</td>
    <td>${d.cbu}</td><td>${badgeTf(d)}</td></tr>`).join('')||
    '<tr><td colspan="7" class="text-center text-muted py-2">Sin transferencias en el período</td></tr>';
  document.getElementById('tbody-cli-fci').innerHTML=fcis.map(d=>`
    <tr><td>${d.fecha}</td><td>${badgeFci(d)}</td>
    <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${d.fondo}</td>
    <td>${d.moneda}</td><td class="text-end">${fmtI(d.importe)}</td>
    <td class="text-end">${fmt(d.cuotapartes,3)}</td></tr>`).join('')||
    '<tr><td colspan="6" class="text-center text-muted py-2">Sin comprobantes FCI en el período</td></tr>';
  document.getElementById('tbody-cli-ing').innerHTML=ings.map(d=>`
    <tr><td>${d.fecha}</td><td>${d.tipo}</td><td>${d.moneda}</td>
    <td class="text-end">${fmtI(d.importe)}</td><td>${badgeIng(d)}</td></tr>`).join('')||
    '<tr><td colspan="5" class="text-center text-muted py-2">Sin comprobantes en el período</td></tr>';
}

// ── Auto-refresh countdown ────────────────────────────────────────────────
const REFRESH_S=__INTERVAL_S__;
let rem=REFRESH_S;
const cdEl=document.getElementById('countdown');
setInterval(()=>{rem--;if(rem<=0){location.reload();return;}
  const m=Math.floor(rem/60),s=rem%60;if(cdEl)cdEl.textContent=m+':'+String(s).padStart(2,'0');},1000);

// ── Init ──────────────────────────────────────────────────────────────────
tfInitFiltros(); tfAplicar();
fciInitFiltros(); fciAplicar();
ingInitFiltros(); ingFilt=ING.filter(d=>d.cat==='ingreso'); ingRender();
buildClienteTab();
</script>
</body>
</html>"""


def _js_safe(data) -> str:
    return json.dumps(data, ensure_ascii=False, default=str).replace("<", "\\u003c")


def generar_html(tf: list, fci: list, ing: list, fd: str, fh: str, ts: str) -> str:
    return (HTML
            .replace("__TF_JSON__",    _js_safe(tf))
            .replace("__FCI_JSON__",   _js_safe(fci))
            .replace("__ING_JSON__",   _js_safe(ing))
            .replace("__DESDE__",      fd)
            .replace("__HASTA__",      fh)
            .replace("__TS__",         ts)
            .replace("__INTERVAL_S__", str(INTERVAL_S)))


# ── Main ──────────────────────────────────────────────────────────────────────

def actualizar():
    print(f"\n{_B}{'='*62}{_R}")
    print(f"  {_C}Flujos Cohen{_R} — {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
    print(f"  Rango: {FECHA_DESDE} -> {FECHA_HASTA}")
    print(f"{'='*62}")

    token      = obtener_token()
    comitentes = get_comitentes(token)
    ids        = [c["id"] for c in comitentes]
    cmap       = {c["id"]: parsear_comitente(c) for c in comitentes}
    print(f"  {_G}[OK] Token{_R}  |  Comitentes: {_B}{len(comitentes)}{_R}")

    # Fetch paralelo de los 3 endpoints
    def _tf():  return fetch_transferencias(comitentes, FECHA_DESDE, FECHA_HASTA, token)
    def _fci(): return fetch_fci(ids, FECHA_DESDE, FECHA_HASTA, token)
    def _ing(): return fetch_cta_cte(ids, FECHA_DESDE, FECHA_HASTA, token)

    resultados = {}
    with ThreadPoolExecutor(max_workers=3) as ex:
        futuros = {ex.submit(_tf): "tf", ex.submit(_fci): "fci", ex.submit(_ing): "ing"}
        for fut in as_completed(futuros):
            key = futuros[fut]
            try:
                resultados[key] = fut.result()
            except Exception as e:
                print(f"\n  {_RE}[!] Error {key}: {e}{_R}")
                resultados[key] = []

    tf  = normalizar_transferencias(resultados.get("tf", []), cmap)
    fci = normalizar_fci(resultados.get("fci", []), cmap)
    ing = normalizar_cte(resultados.get("ing", []), cmap)

    # Resumen
    pend  = sum(1 for f in tf  if f["estado_cat"] == "pendiente")
    resc  = sum(1 for f in fci if f["solicitud_tipo"].startswith("Rescate") or "Rescate" in f["solicitud_tipo"])
    ings  = sum(1 for f in ing if f["cat"] == "ingreso")
    print(f"\n  {_C}Resumen:{_R}")
    print(f"    Transferencias: {_B}{len(tf):>5}{_R}  (pendientes: {_Y}{pend}{_R})")
    print(f"    FCI comprobantes: {_B}{len(fci):>3}{_R}  (rescates: {resc}, susc: {len(fci)-resc})")
    print(f"    Cta Cte total:  {_B}{len(ing):>5}{_R}  (ingresos: {_G}{ings}{_R})")

    ts   = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    html = generar_html(tf, fci, ing, FECHA_DESDE, FECHA_HASTA, ts)
    OUTPUT_HTML.write_text(html, encoding="utf-8")
    print(f"\n  {_G}[OK] HTML -> {OUTPUT_HTML.name}{_R}")
    print(f"{'='*62}\n")


if __name__ == "__main__":
    primera = True
    while True:
        try:
            actualizar()
            if primera:
                webbrowser.open(str(OUTPUT_HTML))
                primera = False
        except KeyboardInterrupt:
            print(f"\n  {_Y}Interrumpido.{_R}\n"); break
        except Exception as e:
            print(f"\n  {_RE}[!] Error: {e}{_R}")

        print(f"  Próxima actualización en {INTERVAL_S // 60} min... (Ctrl+C para salir)")
        try:
            time.sleep(INTERVAL_S)
        except KeyboardInterrupt:
            print(f"\n  {_Y}Interrumpido.{_R}\n"); break

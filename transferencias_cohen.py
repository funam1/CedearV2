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
import tomllib
import webbrowser
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date, timedelta
from pathlib import Path

_DIR        = Path(__file__).parent
COHEN_BASE  = "https://connect.cohen.com.ar"
OUTPUT_HTML = _DIR / "transferencias_cohen.html"
SECRETS_PATH = _DIR / ".streamlit" / "secrets.toml"


def _secrets() -> dict:
    with open(SECRETS_PATH, "rb") as f:
        return tomllib.load(f)

FECHA_DESDE = (date.today() - timedelta(days=30)).isoformat()
FECHA_HASTA = date.today().isoformat()

PAGE_SIZE  = 500
INTERVAL_S = 5 * 60

_R = "\033[0m"; _B = "\033[1m"; _G = "\033[32m"; _C = "\033[36m"; _Y = "\033[33m"; _RE = "\033[31m"


# ── Auth ──────────────────────────────────────────────────────────────────────

def obtener_token(user: str = None, pass_: str = None) -> str:
    if user is None or pass_ is None:
        s = _secrets()
        user = user or s["API_USER"]
        pass_ = pass_ or s["API_PASS"]
    resp = requests.get("http://72.60.155.149:8000/api/cohen/login-token",
        headers={"x-user": user, "x-pass": pass_}, timeout=15)
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
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Flujos Cohen — QTM Capital</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=DM+Serif+Display&family=DM+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
/* ═══════════════════════ TOKENS ═══════════════════════ */
:root{
  --navy:#0F2044; --navy-deep:#0b1830; --gold:#C9A84C; --gold-soft:#e3cd8a;
  --ok:#2e9e6b;   --ok-bg:rgba(46,158,107,.14);
  --warn:#c98a1c;  --warn-bg:rgba(201,138,28,.16);
  --bad:#c4453f;   --bad-bg:rgba(196,69,63,.14);
  --neu:#6b7fa3;   --neu-bg:rgba(107,127,163,.14);
  --r:12px;
  --fh:'DM Serif Display',Georgia,serif;
  --fb:'DM Sans',-apple-system,Segoe UI,sans-serif;
  --trans:color .2s,background .2s,border-color .2s;
}
[data-t="dark"]{
  --bg:#0b1322; --sur:#121d30; --sur2:#172541; --sur3:#1e2e50;
  --tx:#eef1f6; --mu:#7a8fb5; --bo:rgba(255,255,255,.09);
  --hd:linear-gradient(135deg,#0b1830,#0F2044 55%,#16243a);
  --sh:0 8px 24px rgba(0,0,0,.35);
}
[data-t="light"]{
  --bg:#f3f5f9; --sur:#ffffff; --sur2:#f0f2f8; --sur3:#e8eaf2;
  --tx:#16213e; --mu:#5c6880; --bo:#dde0ec;
  --hd:linear-gradient(135deg,#0F2044,#16345c 60%,#1b3a66);
  --sh:0 4px 16px rgba(15,32,68,.08);
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{background:var(--bg);color:var(--tx);font-family:var(--fb);font-size:14.5px;-webkit-tap-highlight-color:transparent;transition:var(--trans)}

/* ═══════════════════════ HEADER ═══════════════════════ */
.hdr{background:var(--hd);padding:12px 14px 14px;position:sticky;top:0;z-index:40;box-shadow:var(--sh)}
.hdr-row{display:flex;align-items:center;gap:8px}
.brand{font-family:var(--fh);color:#fff;font-size:1.25rem;line-height:1.1}
.brand small{display:block;font-family:var(--fb);font-size:.6rem;color:var(--gold-soft);letter-spacing:.14em;text-transform:uppercase}
.spacer{flex:1}
.ibtn{background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.18);color:#fff;
  width:36px;height:36px;border-radius:9px;display:flex;align-items:center;justify-content:center;
  cursor:pointer;font-size:.95rem;flex-none;transition:background .15s}
.ibtn:active{background:rgba(255,255,255,.24)}
.hoy-btn{background:var(--gold);border:none;color:var(--navy-deep);font-family:var(--fb);
  font-weight:700;font-size:.78rem;padding:0 14px;height:36px;border-radius:9px;cursor:pointer;
  white-space:nowrap;letter-spacing:.02em}
.hoy-btn:active{filter:brightness(.9)}
.meta{font-size:.65rem;color:rgba(255,255,255,.5);margin-top:7px;display:flex;gap:10px;flex-wrap:wrap}
.meta b{color:var(--gold-soft);font-weight:600}

/* ═══════════════════════ TABS ═══════════════════════ */
.tabs{display:flex;gap:2px;padding:10px 14px 0;background:var(--bg);position:sticky;top:0;z-index:30;border-bottom:1px solid var(--bo)}
.tbtn{background:transparent;border:none;border-bottom:2px solid transparent;color:var(--mu);
  font-family:var(--fb);font-weight:600;font-size:.8rem;padding:9px 8px 8px;cursor:pointer;transition:var(--trans)}
.tbtn.on{color:var(--tx);border-bottom-color:var(--gold)}
.wrap{padding:12px 14px 56px;max-width:1200px;margin:0 auto}
.panel{display:none}.panel.on{display:block}

/* ═══════════════════════ KPIs ═══════════════════════ */
.kpis{display:grid;grid-template-columns:repeat(2,1fr);gap:9px;margin:12px 0}
@media(min-width:560px){.kpis{grid-template-columns:repeat(4,1fr)}}
@media(min-width:900px){.kpis.kpis5{grid-template-columns:repeat(5,1fr)}}
.kcard{background:var(--sur);border:1px solid var(--bo);border-radius:var(--r);padding:12px 14px;box-shadow:var(--sh)}
.kcard .lbl{font-size:.63rem;text-transform:uppercase;letter-spacing:.07em;color:var(--mu);font-weight:700}
.kcard .val{font-size:1.45rem;font-weight:700;margin-top:2px}

/* ═══════════════════════ FILTER BAR ═══════════════════════ */
.fbar{background:var(--sur);border:1px solid var(--bo);border-radius:var(--r);padding:12px;
  box-shadow:var(--sh);margin-bottom:12px;display:flex;flex-wrap:wrap;gap:9px;align-items:flex-end}
.ff{display:flex;flex-direction:column;gap:3px}
.ff label{font-size:.62rem;text-transform:uppercase;letter-spacing:.06em;color:var(--mu);font-weight:700}
.ff input,.ff select{background:var(--sur2);border:1px solid var(--bo);color:var(--tx);
  border-radius:8px;padding:7px 10px;font-size:.8rem;font-family:var(--fb);outline:none;transition:var(--trans)}
.ff input:focus,.ff select:focus{border-color:var(--gold)}
.btn{border:none;border-radius:8px;padding:8px 13px;font-size:.78rem;font-weight:600;cursor:pointer;font-family:var(--fb);transition:filter .15s}
.btn:active{filter:brightness(.88)}
.btn-gold{background:var(--gold);color:var(--navy-deep)}
.btn-warn{background:var(--warn-bg);color:var(--warn);border:1px solid var(--warn)}
.btn-ghost{background:var(--sur2);color:var(--tx);border:1px solid var(--bo)}
.cnt{margin-left:auto;font-size:.7rem;color:var(--mu);align-self:center}
.bk{font-size:.7rem;color:var(--mu);margin-bottom:8px}

/* ═══════════════════════ TABLE (desktop) ═══════════════════════ */
.tcard{background:var(--sur);border:1px solid var(--bo);border-radius:var(--r);overflow:hidden;box-shadow:var(--sh)}
.tscroll{max-height:60vh;overflow:auto}
table{width:100%;border-collapse:collapse;font-size:.8rem}
th,td{padding:8px 11px;text-align:left;white-space:nowrap;border-bottom:1px solid var(--bo)}
th{position:sticky;top:0;background:var(--sur2);cursor:pointer;user-select:none;
   font-size:.67rem;text-transform:uppercase;letter-spacing:.05em;color:var(--mu);font-weight:700;transition:var(--trans)}
th:hover{color:var(--tx)}
td.num{text-align:right;font-variant-numeric:tabular-nums}
td.ell{max-width:170px;overflow:hidden;text-overflow:ellipsis}
tbody tr:hover{background:var(--sur2)}
@media(max-width:740px){.dt{display:none}}
@media(min-width:741px){.mob{display:none}}

/* ═══════════════════════ MOBILE CARDS ═══════════════════════ */
.mcard{background:var(--sur);border:1px solid var(--bo);border-radius:var(--r);
  padding:12px 13px;margin-bottom:7px;display:flex;gap:11px;align-items:flex-start;box-shadow:var(--sh)}
.dot{width:8px;height:8px;border-radius:50%;flex:none;margin-top:5px}
.dot-ejecutada{background:var(--ok)}.dot-pendiente{background:var(--warn)}
.dot-rechazada{background:var(--bad)}.dot-suscripcion{background:var(--ok)}
.dot-rescate{background:var(--warn)}.dot-ingreso{background:#3b82f6}
.dot-egreso{background:var(--bad)}.dot-fee{background:var(--neu)}.dot-otro{background:var(--neu)}
.mbody{flex:1;min-width:0}
.mr1{display:flex;justify-content:space-between;gap:6px;align-items:baseline}
.mcli{font-weight:700;font-size:.9rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mimp{font-weight:700;font-variant-numeric:tabular-nums;white-space:nowrap;font-size:.9rem}
.mr2{display:flex;justify-content:space-between;gap:6px;margin-top:2px}
.msub{font-size:.72rem;color:var(--mu);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mhor{font-size:.72rem;color:var(--mu);white-space:nowrap}
.mbadge{margin-top:6px}
.dg{font-size:.72rem;color:var(--mu)}

/* ═══════════════════════ BADGES ═══════════════════════ */
.badge{display:inline-block;padding:3px 9px;border-radius:99px;font-size:.68rem;font-weight:700}
.badge-ejecutada{background:var(--ok-bg);color:var(--ok)}
.badge-pendiente{background:var(--warn-bg);color:var(--warn)}
.badge-rechazada{background:var(--bad-bg);color:var(--bad)}
.badge-otro{background:var(--neu-bg);color:var(--neu)}
.badge-rescate{background:var(--warn-bg);color:var(--warn)}
.badge-suscripcion{background:var(--ok-bg);color:var(--ok)}
.badge-ingreso{background:rgba(59,130,246,.15);color:#3b82f6}
.badge-egreso{background:var(--bad-bg);color:var(--bad)}
.badge-fee{background:var(--neu-bg);color:var(--neu)}

/* ═══════════════════════ AUTOCOMPLETE ═══════════════════════ */
.acw{position:relative}
.acl{position:absolute;top:calc(100% + 4px);left:0;right:0;background:var(--sur);
  border:1px solid var(--bo);border-radius:10px;max-height:240px;overflow-y:auto;
  z-index:60;display:none;box-shadow:var(--sh)}
.aci{padding:8px 12px;cursor:pointer;font-size:.8rem;border-bottom:1px solid var(--bo)}
.aci:last-child{border-bottom:none}
.aci:hover,.aci.on{background:var(--sur2)}

/* ═══════════════════════ POR CLIENTE — subtítulos ═══════════════════════ */
.sec-title{font-size:.7rem;font-family:var(--fb);font-weight:700;text-transform:uppercase;
  letter-spacing:.07em;color:var(--mu);margin:14px 0 6px}

/* ═══════════════════════ EMPTY ═══════════════════════ */
.empty{text-align:center;padding:40px 20px;color:var(--mu)}
.empty .big{font-size:1.8rem;margin-bottom:6px}

/* countdown */
#countdown{font-variant-numeric:tabular-nums}
/* ═══════════════════════ MODAL DETALLE ═══════════════════════ */
.mcard{cursor:pointer}
.mcard:active{opacity:.85}
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:80;display:none;
  align-items:flex-end;justify-content:center}
.overlay.open{display:flex}
.drawer{background:var(--sur);border-radius:18px 18px 0 0;width:100%;max-width:520px;
  max-height:88vh;overflow-y:auto;padding:0 0 32px;box-shadow:0 -8px 40px rgba(0,0,0,.4)}
.drawer-handle{width:36px;height:4px;border-radius:2px;background:var(--bo);margin:10px auto 16px}
.drawer-hdr{padding:0 18px 14px;border-bottom:1px solid var(--bo);display:flex;align-items:flex-start;gap:12px}
.drawer-dot{width:12px;height:12px;border-radius:50%;flex:none;margin-top:4px}
.drawer-title{flex:1;min-width:0}
.drawer-title h3{font-size:1.05rem;font-weight:700;line-height:1.2;margin-bottom:2px}
.drawer-title small{font-size:.72rem;color:var(--mu)}
.drawer-badge{align-self:flex-start}
.drawer-body{padding:14px 18px 0}
.drow{display:flex;justify-content:space-between;align-items:baseline;padding:9px 0;border-bottom:1px solid var(--bo)}
.drow:last-child{border-bottom:none}
.dlbl{font-size:.7rem;text-transform:uppercase;letter-spacing:.06em;color:var(--mu);font-weight:700}
.dval{font-size:.88rem;font-weight:500;text-align:right;max-width:62%;word-break:break-all}
.drawer-actions{padding:16px 18px 0;display:flex;gap:9px}
.btn-copy-detail{flex:1;background:var(--gold);color:var(--navy-deep);border:none;
  border-radius:10px;padding:13px;font-family:var(--fb);font-weight:700;font-size:.9rem;cursor:pointer}
.btn-copy-detail:active{filter:brightness(.88)}
.btn-close-drawer{background:var(--sur2);border:1px solid var(--bo);color:var(--tx);
  border-radius:10px;padding:13px 18px;font-family:var(--fb);font-weight:600;font-size:.9rem;cursor:pointer}
</style>
</head>
<body data-t="dark">

<!-- ═══════════ HEADER ═══════════ -->
<div class="hdr">
  <div class="hdr-row">
    <div>
      <div class="brand">Flujos Cohen<small>QTM Capital — Operaciones</small></div>
    </div>
    <div class="spacer"></div>
    <button class="hoy-btn" id="btnHoy">&#128197; HOY</button>
    <button class="ibtn" id="btnTheme" title="Modo claro/oscuro">&#9789;</button>
    <button class="ibtn" id="btnLogout" title="Cerrar sesión" onclick="window.parent.location.href='/_stcore/logout'">&#9099;</button>
  </div>
  <div class="meta">
    <span id="metaRango"></span>
    <span id="metaTs"></span>
    <span>Refresco: <b id="countdown"></b></span>
    <span>&#128100; <b>__USER_NAME__</b></span>
  </div>
</div>

<!-- ═══════════ TABS ═══════════ -->
<div class="tabs">
  <button class="tbtn on" data-p="transfer">Transferencias</button>
  <button class="tbtn" data-p="fci">FCI Susc/Rescate</button>
  <button class="tbtn" data-p="ingresos">Ingresos</button>
  <button class="tbtn" data-p="cliente">Por cliente</button>
</div>

<div class="wrap">

  <!-- ════════════════════ TAB 1 — TRANSFERENCIAS ════════════════════ -->
  <div class="panel on" id="panel-transfer">
    <div class="fbar">
      <div class="ff"><label>Desde</label><input type="date" id="tf-desde"></div>
      <div class="ff"><label>Hasta</label><input type="date" id="tf-hasta"></div>
      <div class="ff"><label>Estado</label>
        <select id="tf-estado">
          <option value="">Todos</option>
          <option value="pendiente">Pendiente</option>
          <option value="ejecutada">Ejecutada</option>
          <option value="rechazada">Rechazada</option>
        </select></div>
      <div class="ff"><label>Moneda</label><select id="tf-moneda"><option value="">Todas</option></select></div>
      <div class="ff" style="flex:1;min-width:150px"><label>Cliente</label><input type="text" id="tf-cli" placeholder="Nombre o cuenta..."></div>
      <button class="btn btn-gold" onclick="tfAplicar()">Aplicar</button>
      <button class="btn btn-warn" onclick="tfSoloPend()">Pendientes</button>
      <button class="btn btn-ghost" onclick="tfReset()">Limpiar</button>
      <button class="btn btn-ghost" id="btnCopyTf">Copiar</button>
      <span class="cnt" id="tf-cnt"></span>
    </div>
    <div class="kpis" id="tf-kpis"></div>
    <div class="bk" id="tf-bk"></div>
    <!-- Desktop -->
    <div class="tcard dt">
      <div class="tscroll">
        <table id="tbl-tf">
          <thead><tr>
            <th onclick="sortTbl('tbl-tf',0)">Fecha</th>
            <th onclick="sortTbl('tbl-tf',1)">Cuenta</th>
            <th onclick="sortTbl('tbl-tf',2)">Cliente</th>
            <th onclick="sortTbl('tbl-tf',3)">Tipo</th>
            <th onclick="sortTbl('tbl-tf',4)">Moneda</th>
            <th onclick="sortTbl('tbl-tf',5)" class="num">Importe</th>
            <th onclick="sortTbl('tbl-tf',6)">Banco/Cuenta</th>
            <th onclick="sortTbl('tbl-tf',7)">CBU</th>
            <th onclick="sortTbl('tbl-tf',8)">Estado</th>
            <th onclick="sortTbl('tbl-tf',9)">Alta</th>
          </tr></thead>
          <tbody id="tbody-tf"></tbody>
        </table>
      </div>
    </div>
    <!-- Mobile -->
    <div class="mob" id="mob-tf"></div>
  </div>

  <!-- ════════════════════ TAB 2 — FCI ════════════════════ -->
  <div class="panel" id="panel-fci">
    <div class="fbar">
      <div class="ff"><label>Desde</label><input type="date" id="fci-desde"></div>
      <div class="ff"><label>Hasta</label><input type="date" id="fci-hasta"></div>
      <div class="ff"><label>Operación</label>
        <select id="fci-tipo"><option value="">Todas</option><option value="Rescate">Rescate</option><option value="Suscripción">Suscripción</option></select></div>
      <div class="ff"><label>Moneda</label><select id="fci-moneda"><option value="">Todas</option></select></div>
      <div class="ff" style="flex:1;min-width:150px"><label>Fondo</label><input type="text" id="fci-fondo" placeholder="Nombre del fondo..."></div>
      <div class="ff" style="min-width:130px"><label>Cliente</label><input type="text" id="fci-cli" placeholder="Nombre o cuenta..."></div>
      <button class="btn btn-gold" onclick="fciAplicar()">Aplicar</button>
      <button class="btn btn-warn" onclick="fciSoloResc()">Rescates</button>
      <button class="btn btn-ghost" onclick="fciReset()">Limpiar</button>
      <button class="btn btn-ghost" id="btnCopyFci">Copiar</button>
      <span class="cnt" id="fci-cnt"></span>
    </div>
    <div class="kpis kpis5" id="fci-kpis"></div>
    <div class="tcard dt">
      <div class="tscroll">
        <table id="tbl-fci">
          <thead><tr>
            <th onclick="sortTbl('tbl-fci',0)">Fecha</th>
            <th onclick="sortTbl('tbl-fci',1)">Cuenta</th>
            <th onclick="sortTbl('tbl-fci',2)">Cliente</th>
            <th onclick="sortTbl('tbl-fci',3)">Operación</th>
            <th onclick="sortTbl('tbl-fci',4)">Fondo</th>
            <th onclick="sortTbl('tbl-fci',5)">Moneda</th>
            <th onclick="sortTbl('tbl-fci',6)" class="num">Importe</th>
            <th onclick="sortTbl('tbl-fci',7)" class="num">Cuotapartes</th>
            <th onclick="sortTbl('tbl-fci',8)" class="num">Cotización</th>
          </tr></thead>
          <tbody id="tbody-fci"></tbody>
        </table>
      </div>
    </div>
    <div class="mob" id="mob-fci"></div>
  </div>

  <!-- ════════════════════ TAB 3 — INGRESOS ════════════════════ -->
  <div class="panel" id="panel-ingresos">
    <div class="fbar">
      <div class="ff"><label>Desde</label><input type="date" id="ing-desde"></div>
      <div class="ff"><label>Hasta</label><input type="date" id="ing-hasta"></div>
      <div class="ff"><label>Categoría</label>
        <select id="ing-cat">
          <option value="ingreso">Ingresos</option>
          <option value="">Todo</option>
          <option value="egreso">Egresos</option>
          <option value="fee">Fee/Mant.</option>
          <option value="otro">Otros</option>
        </select></div>
      <div class="ff"><label>Moneda</label><select id="ing-moneda"><option value="">Todas</option></select></div>
      <div class="ff" style="min-width:180px"><label>Tipo comprobante</label><select id="ing-tipo"><option value="">Todos</option></select></div>
      <div class="ff" style="min-width:130px"><label>Cliente</label><input type="text" id="ing-cli" placeholder="Nombre o cuenta..."></div>
      <button class="btn btn-gold" onclick="ingAplicar()">Aplicar</button>
      <button class="btn btn-warn" onclick="ingSoloIng()">Solo ingresos</button>
      <button class="btn btn-ghost" onclick="ingReset()">Limpiar</button>
      <button class="btn btn-ghost" id="btnCopyIng">Copiar</button>
      <span class="cnt" id="ing-cnt"></span>
    </div>
    <div class="kpis" id="ing-kpis"></div>
    <div class="tcard dt">
      <div class="tscroll">
        <table id="tbl-ing">
          <thead><tr>
            <th onclick="sortTbl('tbl-ing',0)">Fecha</th>
            <th onclick="sortTbl('tbl-ing',1)">Cuenta</th>
            <th onclick="sortTbl('tbl-ing',2)">Cliente</th>
            <th onclick="sortTbl('tbl-ing',3)">Tipo comprobante</th>
            <th onclick="sortTbl('tbl-ing',4)">Moneda</th>
            <th onclick="sortTbl('tbl-ing',5)" class="num">Importe</th>
            <th onclick="sortTbl('tbl-ing',6)">Vto</th>
            <th onclick="sortTbl('tbl-ing',7)">Cat.</th>
          </tr></thead>
          <tbody id="tbody-ing"></tbody>
        </table>
      </div>
    </div>
    <div class="mob" id="mob-ing"></div>
  </div>

  <!-- ════════════════════ TAB 4 — POR CLIENTE ════════════════════ -->
  <div class="panel" id="panel-cliente">
    <div class="fbar">
      <div class="ff acw" style="flex:1;min-width:220px">
        <label>Cliente</label>
        <input type="text" id="cli-input" placeholder="Buscar por nombre o N° de cuenta..." autocomplete="off">
        <div class="acl" id="cli-list"></div>
      </div>
    </div>
    <div class="bk" id="cli-rango"></div>
    <div class="kpis kpis5" id="cli-kpis"></div>

    <p class="sec-title">Transferencias</p>
    <div class="tcard dt" style="margin-bottom:12px">
      <div class="tscroll" style="max-height:220px">
        <table id="tbl-cli-tf">
          <thead><tr>
            <th>Fecha</th><th>Tipo</th><th>Moneda</th>
            <th class="num">Importe</th><th>Banco/Cuenta</th><th>CBU</th><th>Estado</th>
          </tr></thead>
          <tbody id="tbody-cli-tf"></tbody>
        </table>
      </div>
    </div>
    <div class="mob" id="mob-cli-tf" style="margin-bottom:4px"></div>

    <p class="sec-title">FCI — Suscripciones / Rescates</p>
    <div class="tcard dt" style="margin-bottom:12px">
      <div class="tscroll" style="max-height:220px">
        <table id="tbl-cli-fci">
          <thead><tr>
            <th>Fecha</th><th>Operación</th><th>Fondo</th>
            <th>Moneda</th><th class="num">Importe</th><th class="num">Cuotapartes</th>
          </tr></thead>
          <tbody id="tbody-cli-fci"></tbody>
        </table>
      </div>
    </div>
    <div class="mob" id="mob-cli-fci" style="margin-bottom:4px"></div>

    <p class="sec-title">Ingresos / Movimientos cta. cte.</p>
    <div class="tcard dt">
      <div class="tscroll" style="max-height:220px">
        <table id="tbl-cli-ing">
          <thead><tr>
            <th>Fecha</th><th>Tipo comprobante</th>
            <th>Moneda</th><th class="num">Importe</th><th>Cat.</th>
          </tr></thead>
          <tbody id="tbody-cli-ing"></tbody>
        </table>
      </div>
    </div>
    <div class="mob" id="mob-cli-ing"></div>
  </div>

</div><!-- wrap -->

<script>
// ════════════════════════════════════════════════════════════════
//  DATOS — pegá aquí los arrays TF, FCI, ING del script Python
//  El script Python (o Streamlit) reemplaza __TF_JSON__, __FCI_JSON__, __ING_JSON__
// ════════════════════════════════════════════════════════════════
var TF  = __TF_JSON__;
var FCI = __FCI_JSON__;
var ING = __ING_JSON__;

// Meta (también reemplazado por Python)
var META_DESDE = "__DESDE__";
var META_HASTA = "__HASTA__";
var META_TS    = "__TS__";
var REFRESH_S  = __INTERVAL_S__;

// Filtro de fecha compartido entre las 4 vistas (Transferencias, FCI, Ingresos,
// Por cliente). Se persiste en localStorage para sobrevivir a los refrescos.
var FILTRO_DESDE = null;
var FILTRO_HASTA = null;
var CLIENTE_ACTUAL = null;
var _syncingFecha = false;

// ════════════════════════ HELPERS ════════════════════════
function fmtM(v, d) {
  if (d === undefined) d = 2;
  if (v === null || v === undefined) return "";
  return Number(v).toLocaleString("es-AR", {minimumFractionDigits:d, maximumFractionDigits:d});
}
function esc(s) {
  if (!s) return "";
  return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}
function todayStr() {
  var d = new Date();
  var m = d.getMonth() + 1; if (m < 10) m = "0" + m;
  var dd = d.getDate(); if (dd < 10) dd = "0" + dd;
  return d.getFullYear() + "-" + m + "-" + dd;
}

// ════════════════════════ TEMA ════════════════════════
function applyTheme(t) {
  document.body.setAttribute("data-t", t);
  try { localStorage.setItem("qtm_t", t); } catch(e){}
  document.getElementById("btnTheme").innerHTML = (t === "dark") ? "&#9728;" : "&#9789;";
}
(function() {
  var s = null;
  try { s = localStorage.getItem("qtm_t"); } catch(e){}
  if (!s) s = (window.matchMedia && window.matchMedia("(prefers-color-scheme:light)").matches) ? "light" : "dark";
  applyTheme(s);
})();
document.getElementById("btnTheme").addEventListener("click", function() {
  applyTheme(document.body.getAttribute("data-t") === "dark" ? "light" : "dark");
});

// ════════════════════════ META ════════════════════════
document.getElementById("metaRango").innerHTML = "Datos <b>" + META_DESDE + " → " + META_HASTA + "</b>";
document.getElementById("metaTs").innerHTML = "Generado: <b>" + META_TS + "</b>";

// ════════════════════════ TABS ════════════════════════
var tabBtns = document.querySelectorAll(".tbtn");
for (var ti = 0; ti < tabBtns.length; ti++) {
  tabBtns[ti].addEventListener("click", function() {
    var p = this.getAttribute("data-p");
    for (var i = 0; i < tabBtns.length; i++) tabBtns[i].classList.remove("on");
    this.classList.add("on");
    var panels = document.querySelectorAll(".panel");
    for (var i = 0; i < panels.length; i++) panels[i].classList.remove("on");
    document.getElementById("panel-" + p).classList.add("on");
  });
}

// ════════════════════════ FILTRO DE FECHA COMPARTIDO ════════════════════════
// Un solo rango desde/hasta para Transferencias, FCI, Ingresos y Por cliente.
// Se persiste en localStorage: sobrevive a los refrescos automáticos (que
// recargan todo el HTML) y por defecto arranca en el día de hoy.
function _propagarFecha(d, h) {
  if (_syncingFecha) return;
  _syncingFecha = true;
  FILTRO_DESDE = d; FILTRO_HASTA = h;
  document.getElementById("tf-desde").value  = d; document.getElementById("tf-hasta").value  = h;
  document.getElementById("fci-desde").value = d; document.getElementById("fci-hasta").value = h;
  document.getElementById("ing-desde").value = d; document.getElementById("ing-hasta").value = h;
  try { localStorage.setItem("qtm_flt_desde", d); localStorage.setItem("qtm_flt_hasta", h); } catch(e){}
  tfAplicar();
  fciAplicar();
  ingAplicar();
  if (CLIENTE_ACTUAL) renderCliente(CLIENTE_ACTUAL);
  _syncingFecha = false;
}
function restaurarFiltroFecha() {
  var d = null, h = null;
  try { d = localStorage.getItem("qtm_flt_desde"); h = localStorage.getItem("qtm_flt_hasta"); } catch(e){}
  if (!d || !h) { d = h = todayStr(); }
  _propagarFecha(d, h);
}

// ════════════════════════ VER HOY ════════════════════════
function verHoy() {
  var hoy = todayStr();
  _propagarFecha(hoy, hoy);
}
document.getElementById("btnHoy").addEventListener("click", verHoy);

// ════════════════════════ SORT ════════════════════════
var _sortSt = {};
function sortTbl(id, col) {
  var tbl = document.getElementById(id);
  var key = id + "_" + col;
  var asc = _sortSt[key] !== true;
  _sortSt[key] = asc;
  var tb = tbl.querySelector("tbody");
  var rows = [];
  var allRows = tb.querySelectorAll("tr");
  for (var i = 0; i < allRows.length; i++) rows.push(allRows[i]);
  rows.sort(function(a, b) {
    var av = a.cells[col] ? a.cells[col].textContent.trim() : "";
    var bv = b.cells[col] ? b.cells[col].textContent.trim() : "";
    av = av.replace(/[$\s%]/g,"").replace(/\./g,"").replace(",",".");
    bv = bv.replace(/[$\s%]/g,"").replace(/\./g,"").replace(",",".");
    var an = parseFloat(av), bn = parseFloat(bv);
    if (!isNaN(an) && !isNaN(bn) && av !== "" && bv !== "") return asc ? an - bn : bn - an;
    return asc ? av.localeCompare(bv, "es") : bv.localeCompare(av, "es");
  });
  for (var i = 0; i < rows.length; i++) tb.appendChild(rows[i]);
}

// ════════════════════════ COPIAR ════════════════════════
function copiarTbl(tableId, btn) {
  var tbl = document.getElementById(tableId);
  if (!tbl) return;
  var lines = [];
  var trs = tbl.querySelectorAll("tr");
  for (var i = 0; i < trs.length; i++) {
    var cells = trs[i].querySelectorAll("th,td");
    var vals = [];
    for (var j = 0; j < cells.length; j++) vals.push(cells[j].textContent.trim());
    lines.push(vals.join("\t"));
  }
  if (navigator.clipboard) {
    navigator.clipboard.writeText(lines.join("\n")).then(function() {
      var orig = btn.textContent;
      btn.textContent = "Copiado!";
      setTimeout(function() { btn.textContent = orig; }, 1500);
    });
  }
}
document.getElementById("btnCopyTf").addEventListener("click",  function(){ copiarTbl("tbl-tf",  this); });
document.getElementById("btnCopyFci").addEventListener("click", function(){ copiarTbl("tbl-fci", this); });
document.getElementById("btnCopyIng").addEventListener("click", function(){ copiarTbl("tbl-ing", this); });

// ════════════════════════ AUTOCOMPLETE ════════════════════════
function makeAC(inpId, lstId, items, onSel) {
  var inp = document.getElementById(inpId);
  var lst = document.getElementById(lstId);
  var idx = -1;
  inp.addEventListener("input", function() {
    var q = inp.value.toLowerCase();
    var ms = [];
    for (var i = 0; i < items.length; i++) {
      if (!q || items[i].label.toLowerCase().indexOf(q) !== -1) { ms.push(items[i]); }
      if (ms.length >= 15) break;
    }
    var h = "";
    for (var i = 0; i < ms.length; i++) {
      h += '<div class="aci" data-v="' + esc(ms[i].value) + '">' + esc(ms[i].label) + '</div>';
    }
    lst.innerHTML = h;
    lst.style.display = ms.length ? "block" : "none";
    idx = -1;
  });
  inp.addEventListener("keydown", function(e) {
    var els = lst.querySelectorAll(".aci");
    if (e.key === "ArrowDown") { idx = Math.min(idx + 1, els.length - 1); }
    else if (e.key === "ArrowUp") { idx = Math.max(idx - 1, 0); }
    else if (e.key === "Enter" && idx >= 0) { els[idx].click(); return; }
    else if (e.key === "Escape") { lst.style.display = "none"; }
    for (var i = 0; i < els.length; i++) els[i].classList.toggle("on", i === idx);
  });
  lst.addEventListener("click", function(e) {
    var el = e.target.closest ? e.target.closest(".aci") : null;
    if (!el) return;
    inp.value = el.textContent;
    lst.style.display = "none";
    onSel(el.getAttribute("data-v"));
  });
  document.addEventListener("click", function(e) {
    if (e.target !== inp && !lst.contains(e.target)) lst.style.display = "none";
  });
}

// ════════════════════════════════════════════════════════════════
//  TAB 1 — TRANSFERENCIAS
// ════════════════════════════════════════════════════════════════
var tfFilt = [];

function tfInitFiltros() {
  var ms = {};
  for (var i = 0; i < TF.length; i++) { if (TF[i].moneda) ms[TF[i].moneda] = 1; }
  var sel = document.getElementById("tf-moneda");
  sel.innerHTML = '<option value="">Todas</option>';
  var keys = Object.keys(ms).sort();
  for (var i = 0; i < keys.length; i++) {
    var o = document.createElement("option"); o.value = o.textContent = keys[i]; sel.appendChild(o);
  }
}

function tfAplicar() {
  var d = document.getElementById("tf-desde").value;
  var h = document.getElementById("tf-hasta").value;
  var e = document.getElementById("tf-estado").value;
  var m = document.getElementById("tf-moneda").value;
  var c = document.getElementById("tf-cli").value.toLowerCase();
  tfFilt = [];
  for (var i = 0; i < TF.length; i++) {
    var r = TF[i];
    if (d && r.fecha_dia < d) continue;
    if (h && r.fecha_dia > h) continue;
    if (e && r.estado_cat !== e) continue;
    if (m && r.moneda !== m) continue;
    if (c && (r.cliente||"").toLowerCase().indexOf(c) === -1 && r.nro_cuenta.indexOf(c) === -1) continue;
    tfFilt.push(r);
  }
  tfRender();
  if (!_syncingFecha) _propagarFecha(d, h);
}
function tfSoloPend() { document.getElementById("tf-estado").value = "pendiente"; tfAplicar(); }
function tfReset() {
  document.getElementById("tf-estado").value = "";
  document.getElementById("tf-moneda").value = "";
  document.getElementById("tf-cli").value = "";
  tfInitFiltros(); tfAplicar();
}

function badgeTf(d) {
  var extra = "";
  if (d.estado && d.estado !== d.estado_label) extra = " — " + esc(d.estado);
  return '<span class="badge badge-' + d.estado_cat + '">' + esc(d.estado_label) + extra + '</span>';
}

function tfRender() {
  var p = 0, e = 0, r = 0, o = 0;
  var bm = {};
  for (var i = 0; i < tfFilt.length; i++) {
    var d = tfFilt[i];
    if (d.estado_cat === "pendiente") p++;
    else if (d.estado_cat === "ejecutada") e++;
    else if (d.estado_cat === "rechazada") r++;
    else o++;
    bm[d.moneda] = (bm[d.moneda] || 0) + (d.importe || 0);
  }
  document.getElementById("tf-kpis").innerHTML =
    kcard("Total", tfFilt.length, "") +
    kcard("Pendientes", p, "color:var(--warn)") +
    kcard("Ejecutadas", e, "color:var(--ok)") +
    kcard("Rechazadas/Otras", r + o, "color:var(--bad)");
  var bkParts = [];
  var bkKeys = Object.keys(bm).sort();
  for (var i = 0; i < bkKeys.length; i++) { bkParts.push((bkKeys[i]||"?") + " " + fmtM(bm[bkKeys[i]])); }
  document.getElementById("tf-bk").textContent = "Importe total: " + bkParts.join("  |  ");
  document.getElementById("tf-cnt").textContent = tfFilt.length + " de " + TF.length + " transferencias";
  // Desktop table
  var rows = "";
  for (var i = 0; i < tfFilt.length; i++) {
    var d = tfFilt[i];
    rows += '<tr><td>' + esc(d.fecha) + '</td><td>' + esc(d.nro_cuenta) + '</td>' +
      '<td class="ell" title="' + esc(d.cliente) + '">' + esc(d.cliente) + '</td>' +
      '<td>' + esc(d.tipo) + '</td><td>' + esc(d.moneda) + '</td>' +
      '<td class="num">' + fmtM(d.importe) + '</td>' +
      '<td class="ell">' + esc(d.banco) + ' ' + esc(d.cuenta) + '</td>' +
      '<td>' + esc(d.cbu) + '</td><td>' + badgeTf(d) + '</td>' +
      '<td>' + esc(d.fecha_alta) + '</td></tr>';
  }
  document.getElementById("tbody-tf").innerHTML = rows || '<tr><td colspan="10" style="text-align:center;color:var(--mu);padding:24px">Sin resultados</td></tr>';
  // Mobile cards
  var mob = "";
  for (var i = 0; i < tfFilt.length; i++) { mob += mcardTf(tfFilt[i], i); }
  document.getElementById("mob-tf").innerHTML = mob || '<div class="empty"><div class="big">🔍</div>Sin resultados para este filtro.</div>';
}
function mcardTf(d, idx) {
  var tipo = d.tipo ? ' <span style="font-size:.68rem;color:var(--mu)">· ' + esc(d.tipo) + '</span>' : '';
  return '<div class="mcard" onclick="openTf(' + idx + ')">' +
    '<span class="dot dot-' + d.estado_cat + '"></span>' +
    '<div class="mbody"><div class="mr1"><span class="mcli">' + esc(d.cliente || d.nro_cuenta) + '</span>' +
    '<span class="mimp">' + esc(d.moneda) + ' ' + fmtM(d.importe) + '</span></div>' +
    '<div class="mr2"><span class="msub">' + esc(d.banco) + ' ' + esc(d.cuenta) + tipo + '</span>' +
    '<span class="mhor">' + esc(d.fecha) + '</span></div>' +
    '<div class="mbadge">' + badgeTf(d) + '</div></div>' +
    '<span style="color:var(--mu);font-size:1.1rem;align-self:center">&#8250;</span></div>';
}

// ════════════════════════════════════════════════════════════════
//  TAB 2 — FCI
// ════════════════════════════════════════════════════════════════
var fciFilt = [];

function fciInitFiltros() {
  var ms = {};
  for (var i = 0; i < FCI.length; i++) { if (FCI[i].moneda) ms[FCI[i].moneda] = 1; }
  var sel = document.getElementById("fci-moneda");
  sel.innerHTML = '<option value="">Todas</option>';
  var keys = Object.keys(ms).sort();
  for (var i = 0; i < keys.length; i++) {
    var o = document.createElement("option"); o.value = o.textContent = keys[i]; sel.appendChild(o);
  }
}

function fciAplicar() {
  var d = document.getElementById("fci-desde").value;
  var h = document.getElementById("fci-hasta").value;
  var t = document.getElementById("fci-tipo").value;
  var m = document.getElementById("fci-moneda").value;
  var f = document.getElementById("fci-fondo").value.toLowerCase();
  var c = document.getElementById("fci-cli").value.toLowerCase();
  fciFilt = [];
  for (var i = 0; i < FCI.length; i++) {
    var r = FCI[i];
    if (d && r.fecha_dia < d) continue;
    if (h && r.fecha_dia > h) continue;
    if (t && r.solicitud_tipo.indexOf(t) === -1) continue;
    if (m && r.moneda !== m) continue;
    if (f && (r.fondo||"").toLowerCase().indexOf(f) === -1) continue;
    if (c && (r.cliente||"").toLowerCase().indexOf(c) === -1 && r.nro_cuenta.indexOf(c) === -1) continue;
    fciFilt.push(r);
  }
  fciRender();
  if (!_syncingFecha) _propagarFecha(d, h);
}
function fciSoloResc() { document.getElementById("fci-tipo").value = "Rescate"; fciAplicar(); }
function fciReset() {
  document.getElementById("fci-tipo").value = "";
  document.getElementById("fci-moneda").value = "";
  document.getElementById("fci-fondo").value = "";
  document.getElementById("fci-cli").value = "";
  fciInitFiltros(); fciAplicar();
}

function badgeFci(d) {
  var cat = (d.solicitud_tipo && d.solicitud_tipo.indexOf("Rescate") !== -1) ? "rescate" : "suscripcion";
  return '<span class="badge badge-' + cat + '">' + esc(d.solicitud_tipo) + '</span>';
}

function fciRender() {
  var susc = [], resc = [];
  for (var i = 0; i < fciFilt.length; i++) {
    if (fciFilt[i].solicitud_tipo && fciFilt[i].solicitud_tipo.indexOf("Rescate") !== -1) resc.push(fciFilt[i]);
    else susc.push(fciFilt[i]);
  }
  var impS = 0, impR = 0;
  for (var i = 0; i < susc.length; i++) impS += susc[i].importe || 0;
  for (var i = 0; i < resc.length; i++) impR += resc[i].importe || 0;
  document.getElementById("fci-kpis").innerHTML =
    kcard("Total", fciFilt.length, "") +
    kcard("Suscripciones", susc.length, "color:var(--ok)") +
    kcard("Importe Susc.", fmtM(impS), "color:var(--ok)") +
    kcard("Rescates", resc.length, "color:var(--warn)") +
    kcard("Importe Rescates", fmtM(impR), "color:var(--warn)");
  document.getElementById("fci-cnt").textContent = fciFilt.length + " de " + FCI.length;
  var rows = "";
  for (var i = 0; i < fciFilt.length; i++) {
    var d = fciFilt[i];
    rows += '<tr><td>' + esc(d.fecha) + '</td><td>' + esc(d.nro_cuenta) + '</td>' +
      '<td class="ell">' + esc(d.cliente) + '</td>' +
      '<td>' + badgeFci(d) + '</td>' +
      '<td class="ell" title="' + esc(d.fondo) + '">' + esc(d.fondo) + '</td>' +
      '<td>' + esc(d.moneda) + '</td>' +
      '<td class="num">' + fmtM(d.importe) + '</td>' +
      '<td class="num">' + fmtM(d.cuotapartes, 3) + '</td>' +
      '<td class="num">' + fmtM(d.cotizacion, 6) + '</td></tr>';
  }
  document.getElementById("tbody-fci").innerHTML = rows || '<tr><td colspan="9" style="text-align:center;color:var(--mu);padding:24px">Sin resultados</td></tr>';
  var mob = "";
  for (var i = 0; i < fciFilt.length; i++) {
    var d = fciFilt[i];
    var cat = (d.solicitud_tipo && d.solicitud_tipo.indexOf("Rescate") !== -1) ? "rescate" : "suscripcion";
    mob += '<div class="mcard" onclick="openFci(' + i + ')">' +
      '<span class="dot dot-' + cat + '"></span>' +
      '<div class="mbody"><div class="mr1"><span class="mcli">' + esc(d.cliente) + '</span>' +
      '<span class="mimp">' + esc(d.moneda) + ' ' + fmtM(d.importe) + '</span></div>' +
      '<div class="mr2"><span class="msub dg">' + esc(d.fondo) + '</span>' +
      '<span class="mhor">' + esc(d.fecha) + '</span></div>' +
      '<div class="mbadge">' + badgeFci(d) + '</div></div>' +
      '<span style="color:var(--mu);font-size:1.1rem;align-self:center">&#8250;</span></div>';
  }
  document.getElementById("mob-fci").innerHTML = mob || '<div class="empty"><div class="big">🔍</div>Sin resultados.</div>';
}

// ════════════════════════════════════════════════════════════════
//  TAB 3 — INGRESOS
// ════════════════════════════════════════════════════════════════
var ingFilt = [];

function ingInitFiltros() {
  var ms = {};
  for (var i = 0; i < ING.length; i++) { if (ING[i].moneda) ms[ING[i].moneda] = 1; }
  var selM = document.getElementById("ing-moneda");
  selM.innerHTML = '<option value="">Todas</option>';
  var km = Object.keys(ms).sort();
  for (var i = 0; i < km.length; i++) {
    var o = document.createElement("option"); o.value = o.textContent = km[i]; selM.appendChild(o);
  }
  var ts = {};
  for (var i = 0; i < ING.length; i++) { if (ING[i].tipo) ts[ING[i].tipo] = 1; }
  var selT = document.getElementById("ing-tipo");
  selT.innerHTML = '<option value="">Todos</option>';
  var kt = Object.keys(ts).sort();
  for (var i = 0; i < kt.length; i++) {
    var o = document.createElement("option"); o.value = o.textContent = kt[i]; selT.appendChild(o);
  }
}

function ingAplicar() {
  var d = document.getElementById("ing-desde").value;
  var h = document.getElementById("ing-hasta").value;
  var cat = document.getElementById("ing-cat").value;
  var m = document.getElementById("ing-moneda").value;
  var t = document.getElementById("ing-tipo").value;
  var c = document.getElementById("ing-cli").value.toLowerCase();
  ingFilt = [];
  for (var i = 0; i < ING.length; i++) {
    var r = ING[i];
    if (d && r.fecha_dia < d) continue;
    if (h && r.fecha_dia > h) continue;
    if (cat && r.cat !== cat) continue;
    if (m && r.moneda !== m) continue;
    if (t && r.tipo !== t) continue;
    if (c && (r.cliente||"").toLowerCase().indexOf(c) === -1 && r.nro_cuenta.indexOf(c) === -1) continue;
    ingFilt.push(r);
  }
  ingRender();
  if (!_syncingFecha) _propagarFecha(d, h);
}
function ingSoloIng() { document.getElementById("ing-cat").value = "ingreso"; ingAplicar(); }
function ingReset() {
  document.getElementById("ing-cat").value = "ingreso";
  document.getElementById("ing-moneda").value = "";
  document.getElementById("ing-tipo").value = "";
  document.getElementById("ing-cli").value = "";
  ingInitFiltros(); ingAplicar();
}

function badgeIng(d) {
  var m = {ingreso:"Ingreso", egreso:"Egreso", fee:"Fee/Mant.", otro:"Otro"};
  var lbl = m[d.cat] || d.cat;
  return '<span class="badge badge-' + d.cat + '">' + esc(lbl) + '</span>';
}

function ingRender() {
  var nIng = 0; var bm = {};
  for (var i = 0; i < ingFilt.length; i++) {
    var d = ingFilt[i];
    if (d.cat === "ingreso") {
      nIng++;
      bm[d.moneda] = (bm[d.moneda] || 0) + (d.importe || 0);
    }
  }
  var kpis = '<div class="kcard"><div class="lbl">Total registros</div><div class="val">' + ingFilt.length + '</div></div>' +
    '<div class="kcard"><div class="lbl">Ingresos</div><div class="val" style="color:#3b82f6">' + nIng + '</div></div>';
  var bmK = Object.keys(bm).sort();
  for (var i = 0; i < bmK.length; i++) {
    kpis += '<div class="kcard"><div class="lbl">Total ' + esc(bmK[i]) + '</div><div class="val" style="color:var(--ok)">' + fmtM(bm[bmK[i]]) + '</div></div>';
  }
  document.getElementById("ing-kpis").innerHTML = kpis;
  document.getElementById("ing-cnt").textContent = ingFilt.length + " de " + ING.length + " comprobantes";
  var rows = "";
  for (var i = 0; i < ingFilt.length; i++) {
    var d = ingFilt[i];
    rows += '<tr><td>' + esc(d.fecha) + '</td><td>' + esc(d.nro_cuenta) + '</td>' +
      '<td class="ell">' + esc(d.cliente) + '</td>' +
      '<td class="ell">' + esc(d.tipo) + '</td>' +
      '<td>' + esc(d.moneda) + '</td>' +
      '<td class="num">' + fmtM(d.importe) + '</td>' +
      '<td>' + esc(d.fecha_vto) + '</td>' +
      '<td>' + badgeIng(d) + '</td></tr>';
  }
  document.getElementById("tbody-ing").innerHTML = rows || '<tr><td colspan="8" style="text-align:center;color:var(--mu);padding:24px">Sin resultados</td></tr>';
  var mob = "";
  for (var i = 0; i < ingFilt.length; i++) {
    var d = ingFilt[i];
    mob += '<div class="mcard"><span class="dot dot-' + d.cat + '"></span>' +
      '<div class="mbody"><div class="mr1"><span class="mcli">' + esc(d.cliente) + '</span>' +
      '<span class="mimp">' + esc(d.moneda) + ' ' + fmtM(d.importe) + '</span></div>' +
      '<div class="mr2"><span class="msub">' + esc(d.tipo) + '</span>' +
      '<span class="mhor">' + esc(d.fecha) + '</span></div>' +
      '<div class="mbadge">' + badgeIng(d) + '</div></div></div>';
  }
  document.getElementById("mob-ing").innerHTML = mob || '<div class="empty"><div class="big">🔍</div>Sin resultados.</div>';
}

// ════════════════════════════════════════════════════════════════
//  TAB 4 — POR CLIENTE
// ════════════════════════════════════════════════════════════════
function buildClienteTab() {
  var map = {};
  for (var i = 0; i < TF.length; i++)  { if (!map[TF[i].nro_cuenta])  map[TF[i].nro_cuenta] = TF[i]; }
  for (var i = 0; i < FCI.length; i++) { if (!map[FCI[i].nro_cuenta]) map[FCI[i].nro_cuenta] = FCI[i]; }
  for (var i = 0; i < ING.length; i++) { if (!map[ING[i].nro_cuenta]) map[ING[i].nro_cuenta] = ING[i]; }
  var items = [];
  var keys = Object.keys(map).sort(function(a,b){ return a.localeCompare(b); });
  for (var i = 0; i < keys.length; i++) {
    var d = map[keys[i]];
    items.push({value: d.nro_cuenta, label: d.nro_cuenta + " — " + d.cliente});
  }
  makeAC("cli-input", "cli-list", items, function(val) { renderCliente(val); });
  if (items.length) {
    document.getElementById("cli-input").value = items[0].label;
    renderCliente(items[0].value);
  }
}

function renderCliente(nro) {
  CLIENTE_ACTUAL = nro;
  var fd = FILTRO_DESDE, fh = FILTRO_HASTA;
  var rangoEl = document.getElementById("cli-rango");
  if (rangoEl) rangoEl.textContent = (fd && fh) ? ("Movimientos entre " + fd + " y " + fh) : "";
  var tfs = [], fcis = [], ings = [];
  for (var i = 0; i < TF.length;  i++) { var r=TF[i];  if (r.nro_cuenta ===nro && (!fd||r.fecha_dia>=fd) && (!fh||r.fecha_dia<=fh)) tfs.push(r); }
  for (var i = 0; i < FCI.length; i++) { var r=FCI[i]; if (r.nro_cuenta ===nro && (!fd||r.fecha_dia>=fd) && (!fh||r.fecha_dia<=fh)) fcis.push(r); }
  for (var i = 0; i < ING.length; i++) { var r=ING[i]; if (r.nro_cuenta ===nro && (!fd||r.fecha_dia>=fd) && (!fh||r.fecha_dia<=fh)) ings.push(r); }
  tfs.sort(function(a,b){ return b.fecha.localeCompare(a.fecha); });
  fcis.sort(function(a,b){ return b.fecha.localeCompare(a.fecha); });
  ings.sort(function(a,b){ return b.fecha.localeCompare(a.fecha); });
  var pend = 0, resc = 0, ingN = 0;
  for (var i = 0; i < tfs.length;  i++) { if (tfs[i].estado_cat === "pendiente") pend++; }
  for (var i = 0; i < fcis.length; i++) { if (fcis[i].solicitud_tipo && fcis[i].solicitud_tipo.indexOf("Rescate") !== -1) resc++; }
  for (var i = 0; i < ings.length; i++) { if (ings[i].cat === "ingreso") ingN++; }
  var nombre = "";
  for (var i = 0; i < TF.length && !nombre; i++)  { if (TF[i].nro_cuenta  === nro) nombre = TF[i].cliente; }
  for (var i = 0; i < FCI.length && !nombre; i++) { if (FCI[i].nro_cuenta === nro) nombre = FCI[i].cliente; }
  for (var i = 0; i < ING.length && !nombre; i++) { if (ING[i].nro_cuenta === nro) nombre = ING[i].cliente; }

  document.getElementById("cli-kpis").innerHTML =
    '<div class="kcard" style="grid-column:1/-1"><div class="lbl">Cliente</div><div style="font-size:1rem;font-weight:600">' + esc(nombre) + '</div></div>' +
    kcard("Transf. pendientes", pend, "color:var(--warn)") +
    kcard("Transf. total", tfs.length, "") +
    kcard("Rescates FCI", resc, "color:var(--warn)") +
    kcard("Susc. FCI", fcis.length - resc, "color:var(--ok)") +
    kcard("Ingresos", ingN, "color:#3b82f6");

  // TF table
  var rt = "";
  for (var i = 0; i < tfs.length; i++) {
    var d = tfs[i];
    rt += '<tr><td>' + esc(d.fecha) + '</td><td>' + esc(d.tipo) + '</td><td>' + esc(d.moneda) + '</td>' +
      '<td class="num">' + fmtM(d.importe) + '</td>' +
      '<td>' + esc(d.banco) + ' ' + esc(d.cuenta) + '</td><td>' + esc(d.cbu) + '</td>' +
      '<td>' + badgeTf(d) + '</td></tr>';
  }
  document.getElementById("tbody-cli-tf").innerHTML = rt || '<tr><td colspan="7" style="text-align:center;color:var(--mu);padding:16px">Sin transferencias en el período</td></tr>';
  var mct = "";
  for (var i = 0; i < tfs.length; i++) mct += mcardTf(tfs[i], i);
  document.getElementById("mob-cli-tf").innerHTML = mct || '<div class="empty">Sin transferencias.</div>';

  // FCI table
  var rf = "";
  for (var i = 0; i < fcis.length; i++) {
    var d = fcis[i];
    rf += '<tr><td>' + esc(d.fecha) + '</td><td>' + badgeFci(d) + '</td>' +
      '<td class="ell" title="' + esc(d.fondo) + '">' + esc(d.fondo) + '</td>' +
      '<td>' + esc(d.moneda) + '</td><td class="num">' + fmtM(d.importe) + '</td>' +
      '<td class="num">' + fmtM(d.cuotapartes, 3) + '</td></tr>';
  }
  document.getElementById("tbody-cli-fci").innerHTML = rf || '<tr><td colspan="6" style="text-align:center;color:var(--mu);padding:16px">Sin FCI en el período</td></tr>';
  var mcf = "";
  for (var i = 0; i < fcis.length; i++) {
    var d = fcis[i];
    var cat2 = (d.solicitud_tipo && d.solicitud_tipo.indexOf("Rescate") !== -1) ? "rescate" : "suscripcion";
    mcf += '<div class="mcard"><span class="dot dot-' + cat2 + '"></span>' +
      '<div class="mbody"><div class="mr1"><span class="mcli">' + esc(d.fondo) + '</span>' +
      '<span class="mimp">' + esc(d.moneda) + ' ' + fmtM(d.importe) + '</span></div>' +
      '<div class="mr2"><span class="msub">' + esc(d.fecha) + '</span></div>' +
      '<div class="mbadge">' + badgeFci(d) + '</div></div></div>';
  }
  document.getElementById("mob-cli-fci").innerHTML = mcf || '<div class="empty">Sin FCI.</div>';

  // ING table
  var ri = "";
  for (var i = 0; i < ings.length; i++) {
    var d = ings[i];
    ri += '<tr><td>' + esc(d.fecha) + '</td><td class="ell">' + esc(d.tipo) + '</td>' +
      '<td>' + esc(d.moneda) + '</td><td class="num">' + fmtM(d.importe) + '</td>' +
      '<td>' + badgeIng(d) + '</td></tr>';
  }
  document.getElementById("tbody-cli-ing").innerHTML = ri || '<tr><td colspan="5" style="text-align:center;color:var(--mu);padding:16px">Sin comprobantes en el período</td></tr>';
  var mci = "";
  for (var i = 0; i < ings.length; i++) {
    var d = ings[i];
    mci += '<div class="mcard"><span class="dot dot-' + d.cat + '"></span>' +
      '<div class="mbody"><div class="mr1"><span class="mcli">' + esc(d.tipo) + '</span>' +
      '<span class="mimp">' + esc(d.moneda) + ' ' + fmtM(d.importe) + '</span></div>' +
      '<div class="mr2"><span class="msub">' + esc(d.fecha) + '</span></div>' +
      '<div class="mbadge">' + badgeIng(d) + '</div></div></div>';
  }
  document.getElementById("mob-cli-ing").innerHTML = mci || '<div class="empty">Sin comprobantes.</div>';
}

// ════════════════════════ KPI HELPER ════════════════════════
function kcard(lbl, val, style) {
  return '<div class="kcard"><div class="lbl">' + esc(String(lbl)) + '</div>' +
    '<div class="val" style="' + (style||"") + '">' + String(val) + '</div></div>';
}

// ════════════════════════ COUNTDOWN ════════════════════════
var rem = REFRESH_S;
var cdEl = document.getElementById("countdown");
setInterval(function() {
  rem--;
  if (rem <= 0) { location.reload(); return; }
  var m = Math.floor(rem / 60), s = rem % 60;
  if (cdEl) cdEl.textContent = m + ":" + (s < 10 ? "0" : "") + s;
}, 1000);


// ════════════════════════ MODAL DETALLE ════════════════════════
var _copyText = "";

function openDrawer() {
  document.getElementById("overlay").classList.add("open");
  document.body.style.overflow = "hidden";
}
function closeDrawer(e) {
  if (e && e.target !== document.getElementById("overlay")) return;
  document.getElementById("overlay").classList.remove("open");
  document.body.style.overflow = "";
}

function row(lbl, val) {
  if (!val && val !== 0) return "";
  return '<div class="drow"><span class="dlbl">' + lbl + '</span><span class="dval">' + String(val) + '</span></div>';
}

function openTf(idx) {
  var d = tfFilt[idx];
  if (!d) return;
  document.getElementById("dw-dot").className = "drawer-dot dot-" + d.estado_cat;
  document.getElementById("dw-title").textContent = d.cliente || d.nro_cuenta;
  document.getElementById("dw-sub").textContent = "Cuenta " + d.nro_cuenta + " · " + d.tipo;
  document.getElementById("dw-badge").innerHTML = badgeTf(d);
  var icon = d.estado_cat === "ejecutada" ? "✅" : d.estado_cat === "pendiente" ? "⏳" : "❌";
  var bdy = row("Importe", d.moneda + " " + fmtM(d.importe)) +
    row("Fecha", d.fecha) +
    row("Estado", d.estado_label + (d.estado && d.estado !== d.estado_label ? " (" + d.estado + ")" : "")) +
    row("Tipo", d.tipo) +
    row("Banco", d.banco || "—") +
    row("N° cuenta", d.cuenta || "—") +
    row("CBU", d.cbu || "—") +
    row("Titular", d.titular || "—") +
    row("Alta", d.fecha_alta || "—") +
    (d.observacion ? row("Observación", d.observacion) : "");
  document.getElementById("dw-body").innerHTML = bdy;
  _copyText = icon + " TRANSFERENCIA — " + (d.cliente || d.nro_cuenta) + "\n" +
    "Importe: " + d.moneda + " " + fmtM(d.importe) + "\n" +
    "Estado: " + d.estado_label + (d.estado && d.estado !== d.estado_label ? " (" + d.estado + ")" : "") + "\n" +
    "Banco: " + (d.banco || "—") + "  Cta: " + (d.cuenta || "—") + "\n" +
    "CBU: " + (d.cbu || "—") + "\n" +
    "Fecha: " + d.fecha;
  openDrawer();
}

function openFci(idx) {
  var d = fciFilt[idx];
  if (!d) return;
  var esResc = d.solicitud_tipo && d.solicitud_tipo.indexOf("Rescate") !== -1;
  var cat = esResc ? "rescate" : "suscripcion";
  var icon = esResc ? "📤" : "📥";
  document.getElementById("dw-dot").className = "drawer-dot dot-" + cat;
  document.getElementById("dw-title").textContent = d.cliente || d.nro_cuenta;
  document.getElementById("dw-sub").textContent = "Cuenta " + d.nro_cuenta;
  document.getElementById("dw-badge").innerHTML = badgeFci(d);
  var bdy = row("Operación", d.solicitud_tipo) +
    row("Fondo", d.fondo) +
    row("Importe", d.moneda + " " + fmtM(d.importe)) +
    row("Cuotapartes", fmtM(d.cuotapartes, 3)) +
    row("Cotización", fmtM(d.cotizacion, 6)) +
    row("Fecha", d.fecha);
  document.getElementById("dw-body").innerHTML = bdy;
  _copyText = icon + " FCI " + d.solicitud_tipo.toUpperCase() + " — " + (d.cliente || d.nro_cuenta) + "\n" +
    "Fondo: " + d.fondo + "\n" +
    "Importe: " + d.moneda + " " + fmtM(d.importe) + "\n" +
    "Fecha: " + d.fecha;
  openDrawer();
}

function openIng(idx) {
  var d = ingFilt[idx];
  if (!d) return;
  var icon = d.cat === "ingreso" ? "💰" : d.cat === "egreso" ? "💸" : "📋";
  document.getElementById("dw-dot").className = "drawer-dot dot-" + d.cat;
  document.getElementById("dw-title").textContent = d.cliente || d.nro_cuenta;
  document.getElementById("dw-sub").textContent = "Cuenta " + d.nro_cuenta;
  document.getElementById("dw-badge").innerHTML = badgeIng(d);
  var bdy = row("Tipo comprobante", d.tipo) +
    row("Importe", d.moneda + " " + fmtM(d.importe)) +
    row("Fecha", d.fecha) +
    row("Vencimiento", d.fecha_vto || "—");
  document.getElementById("dw-body").innerHTML = bdy;
  _copyText = icon + " " + d.tipo.toUpperCase() + " — " + (d.cliente || d.nro_cuenta) + "\n" +
    "Importe: " + d.moneda + " " + fmtM(d.importe) + "\n" +
    "Fecha: " + d.fecha;
  openDrawer();
}

document.getElementById("btnCopyDetail").addEventListener("click", function() {
  var self = this;
  if (navigator.clipboard) {
    navigator.clipboard.writeText(_copyText).then(function() {
      self.textContent = "✓ Copiado!";
      setTimeout(function() { self.innerHTML = "&#128203; Copiar para compartir"; }, 1600);
    });
  } else {
    var ta = document.createElement("textarea");
    ta.value = _copyText; document.body.appendChild(ta); ta.select();
    document.execCommand("copy"); document.body.removeChild(ta);
    self.textContent = "✓ Copiado!";
    setTimeout(function() { self.innerHTML = "&#128203; Copiar para compartir"; }, 1600);
  }
});

// ════════════════════════ INIT ════════════════════════
tfInitFiltros();
fciInitFiltros();
ingInitFiltros();
restaurarFiltroFecha();
buildClienteTab();
</script>

<!-- ═══════════ MODAL DETALLE ═══════════ -->
<div class="overlay" id="overlay" onclick="closeDrawer(event)">
  <div class="drawer" id="drawer">
    <div class="drawer-handle"></div>
    <div class="drawer-hdr">
      <span class="drawer-dot" id="dw-dot"></span>
      <div class="drawer-title">
        <h3 id="dw-title"></h3>
        <small id="dw-sub"></small>
      </div>
      <span class="drawer-badge" id="dw-badge"></span>
    </div>
    <div class="drawer-body" id="dw-body"></div>
    <div class="drawer-actions">
      <button class="btn-copy-detail" id="btnCopyDetail">&#128203; Copiar para compartir</button>
      <button class="btn-close-drawer" onclick="closeDrawer()">Cerrar</button>
    </div>
  </div>
</div>
</body>
</html>
"""


def _js_safe(data) -> str:
    return json.dumps(data, ensure_ascii=False, default=str).replace("<", "\\u003c")


def generar_html(tf: list, fci: list, ing: list, fd: str, fh: str, ts: str,
                  user_name: str = "") -> str:
    return (HTML
            .replace("__TF_JSON__",    _js_safe(tf))
            .replace("__FCI_JSON__",   _js_safe(fci))
            .replace("__ING_JSON__",   _js_safe(ing))
            .replace("__DESDE__",      fd)
            .replace("__HASTA__",      fh)
            .replace("__TS__",         ts)
            .replace("__INTERVAL_S__", str(INTERVAL_S))
            .replace("__USER_NAME__",  user_name))


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

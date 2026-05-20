#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Dashboard interactivo GNR (Ganancia No Realizada + Realizada) — Acciones y CEDEARs.
# Genera dashboard_gnr.html y lo abre en el browser. Refresca cada INTERVAL_S segundos.

import streamlit as st
import json
import requests
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import streamlit.components.v1 as components
from template import HTML_TEMPLATE  # Importamos el string largo del HTML

# --- CONFIGURACIÓN DE PÁGINA ---
st.set_page_config(
    page_title="Dashboard GNR - Cohen",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# --- CONSTANTES ---
COHEN_BASE = "https://connect.cohen.com.ar"
TIPOS_GNR = {"Acciones", "Cedear"}
ARANCEL = 0.989  # 1 - 1.1% comisión
INTERVALO_REFRESCO_S = 30 * 60

# --- FUNCIONES DE AUTENTICACIÓN Y API ---

def obtener_token() -> str:
    # Nota: En producción, usa st.secrets para no exponer la clave
    user = st.secrets["X_USER"]
    pw = st.secrets["X_PASS"]
    
    resp = requests.get(
        "http://72.60.155.149:8000/api/cohen/login-token",
        headers={"x-user": user, "x-pass": pw}, 
        timeout=15
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"Login fallido: {data}")
    return data["token"]

def get_mep(token: str) -> float:
    hoy = datetime.now().strftime("%Y-%m-%dT00:00:00.000Z")
    resp = requests.post(
        f"{COHEN_BASE}/api/moneda/getCotizacionMoneda",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "Content-Type": "application/json"},
        json={"skip": 0, "take": 100, "order": [], "fechaCotizacion": hoy},
        timeout=15
    )
    resp.raise_for_status()
    for item in resp.json():
        if item.get("idMoneda") == 1032:  # DOLAR MEP CONNECT
            return float(item.get("cotizacionActual") or 0)
    return 0.0

def get_comitentes(token: str) -> list:
    resp = requests.get(
        f"{COHEN_BASE}/api/posicion/getComitentesUsuario",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}, 
        timeout=15
    )
    resp.raise_for_status()
    return resp.json() if isinstance(resp.json(), list) else []

def get_posiciones(id_comitente: int, token: str) -> list:
    hoy = datetime.now().strftime("%Y-%m-%dT00:00:00.000Z")
    resp = requests.post(
        f"{COHEN_BASE}/api/posicion/listResumen",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "Content-Type": "application/json"},
        json={
            "skip": 0, "take": 500, "order": [], "idComitente": id_comitente,
            "fechaHasta": hoy, "idReporteTipo": 0,
            "comitenteSelected": str(id_comitente), "personaSelected": ""
        },
        timeout=20
    )
    resp.raise_for_status()
    return [r for r in resp.json().get("data", []) if not r.get("esFilaSubtotal")]

def get_ganancia_realizada(id_comitente: int, token: str) -> list:
    hoy = datetime.now().strftime("%Y-%m-%dT00:00:00.000Z")
    try:
        resp = requests.post(
            f"{COHEN_BASE}/api/gananciaRealizada/list",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "Content-Type": "application/json"},
            json={"skip": 0, "take": 500, "order": [], "idComitente": id_comitente, "fecha": hoy, "idReporteTipo": 0, "esFondo": False},
            timeout=20
        )
        if resp.status_code != 200: return []
        data = resp.json()
        return data.get("items", []) if isinstance(data, dict) else []
    except:
        return []

def parsear_comitente(c: dict) -> tuple:
    desc = c.get("desc", "")
    if " - " in desc:
        partes = desc.split(" - ", 1)
        return partes[0].strip(), partes[1].strip()
    return str(c["id"]), desc

# --- PROCESAMIENTO DE DATOS EN PARALELO ---

@st.cache_data(ttl=1800) # Cache por 30 minutos
def fetch_all_api_data():
    token = obtener_token()
    mep = get_mep(token)
    comitentes = get_comitentes(token)
    cmap = {c["id"]: parsear_comitente(c) for c in comitentes}

    gnr_total = []
    gr_total = []

    # Uso de ThreadPool para acelerar las ~100+ peticiones
    with ThreadPoolExecutor(max_workers=20) as executor:
        # Tareas GNR
        future_to_gnr = {executor.submit(get_posiciones, c["id"], token): c["id"] for c in comitentes}
        # Tareas GR
        future_to_gr = {executor.submit(get_ganancia_realizada, c["id"], token): c["id"] for c in comitentes}

        # Procesar GNR
        for future in as_completed(future_to_gnr):
            id_com = future_to_gnr[future]
            nro, nombre = cmap.get(id_com, (str(id_com), ""))
            try:
                rows = future.result()
                for r in rows:
                    tipo = r.get("tipoInstrumento") or r.get("instrumentoTipoDescripcion", "")
                    if tipo not in TIPOS_GNR: continue
                    
                    valor_ars = float(r.get("saldoValorizadoARS") or r.get("saldoValorizado") or 0)
                    gnr_total.append({
                        "id_comitente": id_com, "nro_cuenta": nro, "cliente": nombre,
                        "ticker": r.get("ticker") or r.get("instrumentoSimbolo", ""),
                        "descripcion": r.get("denominacion") or r.get("instrumentoDescripcion", ""),
                        "tipo": tipo, "moneda": r.get("moneda", ""),
                        "cantidad": float(r.get("cantidad") or 0),
                        "precio_ars": float(r.get("cotizacionARS") or r.get("cotizacion") or 0),
                        "valor_ars": valor_ars, "valor_neto": round(valor_ars * ARANCEL, 2),
                        "valor_usd": round(valor_ars / mep, 2) if mep else 0,
                        "costo_ars": float(r.get("costoTotalARS") or 0),
                        "pnl_ars": float(r.get("rendimientoARS") or 0),
                        "pnl_pct_ars": float(r.get("rendimientoPctARS") or 0),
                        "pnl_usd": float(r.get("rendimientoUSD") or 0),
                        "var_dia_pct": float(r.get("varDiariaPctARS") or 0),
                        "fecha": r.get("fechaCotizacionString") or ""
                    })
            except: continue

        # Procesar GR
        for future in as_completed(future_to_gr):
            id_com = future_to_gr[future]
            nro, nombre = cmap.get(id_com, (str(id_com), ""))
            try:
                items = future.result()
                for it in items:
                    tipo = it.get("instrumentoTipoDescripcion", "")
                    if tipo not in TIPOS_GNR: continue
                    gr_total.append({
                        "nro_cuenta": nro, "cliente": nombre,
                        "ticker": it.get("instrumentoSimbolo", ""),
                        "tipo": tipo, "fecha": it.get("fecha", "")[:10],
                        "mov_tipo": it.get("movimientoCustodiaTipo", ""),
                        "cantidad": float(it.get("cantidad") or 0),
                        "precio_compra_ars": float(it.get("precioCompraMonedaBase") or 0),
                        "precio_venta_ars": float(it.get("precioVentaMonedaBase") or 0),
                        "importe_compra_ars": float(it.get("importeCompraMonedaBase") or 0),
                        "importe_venta_ars": float(it.get("importeVentaMonedaBase") or 0),
                        "gr_ars": float(it.get("gananciaRealizadaMonedaBase") or 0),
                        "gr_pct_ars": float(it.get("porcentajeMonedaBase") or 0),
                        "gr_usd": float(it.get("gananciaRealizadaRentabilidad") or 0)
                    })
            except: continue

    return gnr_total, gr_total, mep

# --- RENDERIZADO ---

def main():
    # Estilo Streamlit para ocultar menú innecesario
    st.markdown("""<style>#MainMenu {visibility: hidden;} footer {visibility: hidden;}</style>""", unsafe_allow_html=True)

    try:
        with st.spinner('Cargando datos de Cohen Connect...'):
            gnr, gr, mep = fetch_all_api_data()

        # Inyectar datos en el HTML_TEMPLATE
        # Usamos json.dumps para asegurar que el formato sea compatible con JS
        html_final = (HTML_TEMPLATE
            .replace("__DATA_JSON__", json.dumps(gnr))
            .replace("__DATA_GR_JSON__", json.dumps(gr))
            .replace("__MEP__", str(mep))
            .replace("__MEP_DISPLAY__", f"{mep:,.2f}")
            .replace("__TS__", datetime.now().strftime("%d/%m/%Y %H:%M:%S"))
            .replace("__INTERVAL_S__", str(INTERVALO_REFRESCO_S))
        )

        # Mostrar el componente HTML (Ajusta el height según prefieras)
        components.html(html_final, height=1000, scrolling=True)
        
        if st.button("🔄 Forzar Actualización"):
            st.cache_data.clear()
            st.rerun()

    except Exception as e:
        st.error(f"Error crítico en la aplicación: {e}")
        if st.button("Reintentar"):
            st.rerun()

if __name__ == "__main__":
    main()

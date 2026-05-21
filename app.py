#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import streamlit as st
import pandas as pd
import requests
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── CONFIGURACIÓN DE STREAMLIT ────────────────────────────────────────────────
st.set_page_config(page_title="Dashboard GNR - Cohen", page_icon="📈", layout="wide")

COHEN_BASE = "https://connect.cohen.com.ar"
TIPOS_GNR = {"Acciones", "Cedear"}
ARANCEL = 0.989  # 1 - 1.1% comision al vender


# ── FUNCIÓN DE LOGIN ──────────────────────────────────────────────────────────
def check_password():
    """Devuelve True si el usuario ingresó la contraseña correcta."""

    def password_entered():
        if (
            st.session_state["password"]
            == st.secrets["credentials"]["dashboard_password"]
        ):
            st.session_state["password_correct"] = True
            del st.session_state[
                "password"
            ]  # Borra la contraseña del estado por seguridad
        else:
            st.session_state["password_correct"] = False

    if "password_correct" not in st.session_state:
        # Primera vez que entra, muestra el formulario
        st.text_input(
            "Introduce la contraseña para acceder al Dashboard:",
            type="password",
            on_change=password_entered,
            key="password",
        )
        return False
    elif not st.session_state["password_correct"]:
        # Contraseña incorrecta, muestra el mensaje y el formulario otra vez
        st.text_input(
            "Introduce la contraseña para acceder al Dashboard:",
            type="password",
            on_change=password_entered,
            key="password",
        )
        st.error("😕 Contraseña incorrecta.")
        return False
    else:
        # Contraseña correcta
        return True


# ── CONTROL DE ACCESO ─────────────────────────────────────────────────────────
if check_password():

    # ── API / AUTH FUNCTIONS (Usando st.secrets) ──────────────────────────────
    @st.cache_data(ttl=600)
    def obtener_token() -> str:
        # Tomamos el usuario y contraseña directamente de st.secrets
        api_user = st.secrets["cohen_api"]["user"]
        api_pass = st.secrets["cohen_api"]["password"]

        resp = requests.get(
            "http://72.60.155.149:8000/api/cohen/login-token",
            headers={"x-user": api_user, "x-pass": api_pass},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            raise RuntimeError(f"Login fallido: {data}")
        return data["token"]

    @st.cache_data(ttl=300)
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

    @st.cache_data(ttl=900)
    def cargar_datos_completos(token, mep):
        comitentes = get_comitentes(token)
        cmap = {c["id"]: parsear_comitente(c) for c in comitentes}

        posiciones = []
        ganancias_realizadas = []

        def fetch_individual(c):
            id_com = c["id"]
            pos = get_posiciones(id_com, token)
            gr = get_ganancia_realizada(id_com, token)
            return id_com, pos, gr

        with ThreadPoolExecutor(max_workers=20) as ex:
            futuros = {ex.submit(fetch_individual, c): c for c in comitentes}
            for fut in as_completed(futuros):
                try:
                    id_com, rows_pos, rows_gr = fut.result()
                except Exception:
                    continue

                nro, nombre = cmap.get(id_com, (str(id_com), ""))

                for r in rows_pos:
                    tipo = r.get("tipoInstrumento") or r.get(
                        "instrumentoTipoDescripcion", ""
                    )
                    if tipo not in TIPOS_GNR:
                        continue
                    valor_ars = float(
                        r.get("saldoValorizadoARS") or r.get("saldoValorizado") or 0
                    )
                    valor_neto = round(valor_ars * ARANCEL, 2)
                    posiciones.append(
                        {
                            "Cuenta": nro,
                            "Cliente": nombre,
                            "Ticker": r.get("ticker")
                            or r.get("instrumentoSimbolo", ""),
                            "Tipo": tipo,
                            "Cantidad": float(r.get("cantidad") or 0),
                            "Precio ARS": float(
                                r.get("cotizacionARS") or r.get("cotizacion") or 0
                            ),
                            "Valor ARS": valor_ars,
                            "Valor Neto ARS": valor_neto,
                            "Valor Neto USD": (
                                round(valor_neto / mep, 2) if mep else 0.0
                            ),
                            "Costo ARS": float(r.get("costoTotalARS") or 0),
                            "PnL % ARS": float(r.get("rendimientoPctARS") or 0),
                            "PnL USD": float(r.get("rendimientoUSD") or 0),
                            "PnL % USD": float(r.get("rendimientoPctUSD") or 0),
                        }
                    )

                for it in rows_gr:
                    tipo = it.get("instrumentoTipoDescripcion", "")
                    if tipo not in TIPOS_GNR:
                        continue
                    importe_compra = float(it.get("importeCompraMonedaBase") or 0)
                    gr_ars = float(it.get("gananciaRealizadaMonedaBase") or 0)
                    gr_usd = float(it.get("gananciaRealizadaRentabilidad") or 0)
                    ganancias_realizadas.append(
                        {
                            "Cuenta": nro,
                            "Cliente": nombre,
                            "Ticker": it.get("instrumentoSimbolo", ""),
                            "Tipo": tipo,
                            "Importe Compra ARS": importe_compra,
                            "Importe Venta ARS": float(
                                it.get("importeVentaMonedaBase") or 0
                            ),
                            "GR ARS": gr_ars,
                            "GR USD": gr_usd,
                        }
                    )

        return pd.DataFrame(posiciones), pd.DataFrame(ganancias_realizadas)

    # ── LOGIC & INTERFACE ─────────────────────────────────────────────────────
    try:
        token = obtener_token()
        mep = get_mep(token)
    except Exception as e:
        st.error(f"Error crítico de conexión o autenticación: {e}")
        st.stop()

    st.title("📈 Dashboard GNR — Cohen")
    st.caption(
        f"Dólar MEP Connect: **${mep:,.2f}** | Arancel Consolidado: 1.1% | Actualizado: {datetime.now().strftime('%H:%M:%S')}"
    )

    with st.spinner("Descargando datos en paralelo desde Cohen..."):
        df_gnr, df_gr = cargar_datos_completos(token, mep)

    if df_gnr.empty:
        st.warning("No se encontraron posiciones activas de Acciones o CEDEARs.")
        st.stop()

    # KPIs PRINCIPALES
    total_valor = df_gnr["Valor ARS"].sum()
    total_neto = df_gnr["Valor Neto ARS"].sum()
    total_pnl_ars = df_gnr["Valor ARS"].sum() - df_gnr["Costo ARS"].sum()
    total_pnl_usd = df_gnr["PnL USD"].sum()
    ganadoras = len(df_gnr[df_gnr["PnL USD"] > 0])
    perdedoras = len(df_gnr[df_gnr["PnL USD"] <= 0])

    kpi1, kpi2, kpi3, kpi4, kpi5 = st.columns(5)
    kpi1.metric("Posiciones totales", len(df_gnr), f"{ganadoras} G / {perdedoras} P")
    kpi2.metric("Valor Mercado ARS", f"$ {total_valor:,.0f}")
    kpi3.metric("Valor Neto (Post Comisión)", f"$ {total_neto:,.0f}")
    kpi4.metric("Total PnL ARS", f"$ {total_pnl_ars:,.0f}")
    kpi5.metric("Total PnL USD", f"U$S {total_pnl_usd:,.2f}")

    st.markdown("---")

    # TABS
    tab_tabla, tab_cliente, tab_ticker, tab_charts, tab_gr, tab_alertas = st.tabs(
        [
            "📊 Tabla Completa",
            "👤 Por Cliente",
            "🔍 Por Ticker",
            "📉 Rankers",
            "💰 G. Realizada",
            "🚨 Alertas",
        ]
    )

    # TAB 1: TABLA COMPLETA
    with tab_tabla:
        col_f1, col_f2 = st.columns([2, 1])
        with col_f1:
            search_global = st.text_input(
                "Buscar por Cliente, Cuenta o Ticker:", placeholder="Ej: AAPL, Pérez..."
            ).lower()
        with col_f2:
            filter_tipo = st.selectbox(
                "Filtrar por Tipo:", ["Todos", "Acciones", "Cedear"]
            )

        df_filtered = df_gnr.copy()
        if filter_tipo != "Todos":
            df_filtered = df_filtered[df_filtered["Tipo"] == filter_tipo]
        if search_global:
            df_filtered = df_filtered[
                df_filtered["Cliente"].str.lower().str.contains(search_global)
                | df_filtered["Ticker"].str.lower().str.contains(search_global)
                | df_filtered["Cuenta"].str.lower().str.contains(search_global)
            ]

        st.dataframe(
            df_filtered,
            column_config={
                "Precio ARS": st.column_config.NumberColumn(format="$ %,.2f"),
                "Valor ARS": st.column_config.NumberColumn(format="$ %,.0f"),
                "Valor Neto ARS": st.column_config.NumberColumn(format="$ %,.0f"),
                "Valor Neto USD": st.column_config.NumberColumn(format="U$S %,.2f"),
                "Costo ARS": st.column_config.NumberColumn(format="$ %,.0f"),
                "PnL % ARS": st.column_config.NumberColumn(format="%.2f%%"),
                "PnL USD": st.column_config.NumberColumn(format="U$S %,.2f"),
                "PnL % USD": st.column_config.NumberColumn(format="%.2f%%"),
            },
            width="stretch",
            hide_index=True,
        )

    # TAB 2: POR CLIENTE
    with tab_cliente:
        clientes_list = sorted(df_gnr["Cuenta"].unique())
        selected_cuenta = st.selectbox("Seleccione Cuenta de Comitente:", clientes_list)

        df_cli = df_gnr[df_gnr["Cuenta"] == selected_cuenta]
        st.subheader(
            f"Posiciones de: {df_cli['Cliente'].iloc[0] if not df_cli.empty else ''}"
        )

        c_v = df_cli["Valor ARS"].sum()
        c_p_usd = df_cli["PnL USD"].sum()
        sc1, sc2 = st.columns(2)
        sc1.metric("Valor en Cuenta (ARS)", f"$ {c_v:,.0f}")
        sc2.metric("PnL USD acumulado", f"U$S {c_p_usd:,.2f}")

        st.dataframe(
            df_cli.drop(columns=["Cuenta", "Cliente"]),
            width="stretch",
            hide_index=True,
        )

    # TAB 3: POR TICKER
    with tab_ticker:
        ticker_list = sorted(df_gnr["Ticker"].unique())
        selected_ticker = st.selectbox("Seleccione un Ticker:", ticker_list)

        df_tick = df_gnr[df_gnr["Ticker"] == selected_ticker]
        st.subheader(f"Exposición global en {selected_ticker}")

        st.dataframe(
            df_tick[
                ["Cuenta", "Cliente", "Cantidad", "Valor ARS", "PnL USD", "PnL % USD"]
            ],
            width="stretch",
            hide_index=True,
        )

    # TAB 4: RANKERS
    with tab_charts:
        col_g1, col_g2 = st.columns(2)
        with col_g1:
            st.subheader("Top 10 Posiciones con Mayor Ganancia (USD)")
            top_ganadores = df_gnr.nlargest(10, "PnL USD")
            st.bar_chart(data=top_ganadores, x="Ticker", y="PnL USD", color="#198754")
        with col_g2:
            st.subheader("Top 10 Posiciones con Mayor Pérdida (USD)")
            top_perdedores = df_gnr.nsmallest(10, "PnL USD")
            st.bar_chart(data=top_perdedores, x="Ticker", y="PnL USD", color="#dc3545")

    # TAB 5: GANANCIA REALIZADA
    with tab_gr:
        if df_gr.empty:
            st.info("Sin datos de ganancia realizada en este periodo.")
        else:
            df_gr_grouped = (
                df_gr.groupby(["Cuenta", "Cliente", "Ticker", "Tipo"])
                .agg(
                    {
                        "Importe Compra ARS": "sum",
                        "Importe Venta ARS": "sum",
                        "GR ARS": "sum",
                        "GR USD": "sum",
                    }
                )
                .reset_index()
            )

            df_gr_grouped["GR % ARS"] = (
                df_gr_grouped["GR ARS"] / df_gr_grouped["Importe Compra ARS"]
            ) * 100

            st.subheader("Resumen de Operaciones Cerradas (Ganancia Realizada)")
            st.dataframe(
                df_gr_grouped,
                column_config={
                    "Importe Compra ARS": st.column_config.NumberColumn(
                        format="$ %,.0f"
                    ),
                    "Importe Venta ARS": st.column_config.NumberColumn(
                        format="$ %,.0f"
                    ),
                    "GR ARS": st.column_config.NumberColumn(format="$ %,.0f"),
                    "GR USD": st.column_config.NumberColumn(format="U$S %,.2f"),
                    "GR % ARS": st.column_config.NumberColumn(format="%.2f%%"),
                },
                width="stretch",
                hide_index=True,
            )

    # TAB 6: ALERTAS
    with tab_alertas:
        col_a1, col_a2 = st.columns(2)
        with col_a1:
            u_loss = st.number_input(
                "Umbral Pérdida Máxima (%)", min_value=1, max_value=100, value=10
            )
        with col_a2:
            u_gain = st.number_input(
                "Umbral Ganancia Objetivo (%)", min_value=1, max_value=1000, value=20
            )

        df_alert_loss = df_gnr[df_gnr["PnL % ARS"] <= -u_loss].sort_values("PnL % ARS")
        df_alert_gain = df_gnr[df_gnr["PnL % ARS"] >= u_gain].sort_values(
            "PnL % ARS", ascending=False
        )

        col_ret1, col_ret2 = st.columns(2)
        with col_ret1:
            st.error(f"🚨 Alertas de Pérdida (Menos de -{u_loss}%)")
            st.dataframe(
                df_alert_loss[["Cuenta", "Ticker", "Cantidad", "PnL % ARS", "PnL USD"]],
                width="stretch",
                hide_index=True,
            )
        with col_ret2:
            st.success(f"🚀 Alertas de Toma de Ganancia (Más de {u_gain}%)")
            st.dataframe(
                df_alert_gain[["Cuenta", "Ticker", "Cantidad", "PnL % ARS", "PnL USD"]],
                width="stretch",
                hide_index=True,
            )

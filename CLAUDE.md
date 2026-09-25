# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Proyecto: Dashboards Dinámicos de Posiciones

Federico trabaja en QTM Capital (servicios financieros, Río Cuarto, Córdoba). El objetivo es sincronizar
información de la API de Cohen (broker) en dashboards web para el equipo.

## Comandos

```bash
pip install -r requirements.txt   # streamlit, requests, authlib (requerido por st.login)
streamlit run app.py              # app unificada: menú lateral → CEDEAR/GNR + Transferencias
python transferencias_cohen.py    # CLI standalone: fetch de transferencias, genera HTML estático y refresca en loop
```

No hay tests, linter ni CI configurados en el repo.

## Arquitectura

**`app.py`** es la única app Streamlit (`streamlit run app.py`), con login vía Google SSO (`st.login`/`st.user`,
configurado en `.streamlit/secrets.toml` bajo `[auth]`). Tras autenticar, `check_auth()` valida contra la
whitelist `EMAILS_AUTORIZADOS`. Un `st.sidebar.radio` ("Dashboard") elige entre las dos páginas — este es el
punto de extensión para futuros dashboards (agregar una opción más al radio + su propio bloque `elif`):

1. **CEDEAR / GNR** — descarga posiciones/ganancia realizada de Cohen (comitentes en paralelo con
   `ThreadPoolExecutor`), arma `HTML_TEMPLATE` (Bootstrap 5 + Chart.js) y lo renderiza con
   `st.components.v1.html(...)`. Refresco cada `INTERVAL_S` (30 min).
2. **Transferencias** — mismo patrón, pero la lógica de fetch/normalización/HTML vive en el módulo
   `transferencias_cohen.py` (importado como `tc`), que `app.py` orquesta vía `cargar_datos_transferencias()`
   guardando en `st.session_state["tf_*"]`. Refresco cada `tc.INTERVAL_S` (5 min).

Ambas páginas siguen el mismo patrón **Python fetch → dict → HTML/JS embebido como string** (sin Jinja ni
frontend framework): `obtener_token()` → `get_comitentes()` → fetch paralelo por comitente → normalización a
filas planas de dict → HTML con tablas ordenables/filtrables client-side en JS puro, renderizado dentro de un
`st.components.v1.html(...)`.

`transferencias_cohen.py` sigue funcionando también como **CLI standalone** (`python transferencias_cohen.py`):
en ese modo escribe `transferencias_cohen.html` a disco y lo abre con `webbrowser`, corriendo en loop propio.
`obtener_token(user=None, pass_=None)` acepta credenciales inyectadas (así las pasa `app.py` desde
`st.secrets`) o, si se llama sin argumentos, las lee de `.streamlit/secrets.toml` vía `tomllib` — mismo
criterio de no hardcodear credenciales que usa `app.py` con `st.secrets`, aplicado a ambos modos de uso.
`generar_html(..., user_name="")` también recibe el nombre del usuario autenticado para mostrarlo en el navbar,
igual que hace `app.py`.

`transferencias_cohen2.py` está vacío (placeholder) y `transferencias_cohenv2.html` es un HTML estático de
referencia para la v2 (paleta y estilos — ver "Sistema de diseño" — no la lógica, que sigue viva en
`transferencias_cohen.py`).

`template.py` y `dashboard_gnr.html`/`html_template.html` son versiones anteriores/de referencia del template
HTML, previas a la migración a Streamlit — no forman parte del flujo activo pero sirven de referencia de estilo.

## Diseño (lineamientos del producto)

1. Pensado para agregar más dashboards a futuro vía el menú lateral (`st.sidebar.radio` en `app.py`) — CEDEAR/GNR
   y Transferencias ya están unificados ahí; el próximo dashboard sigue el mismo patrón.
2. Cada tabla debe permitir filtrado por sus campos clave y subtotalizar dinámicamente lo filtrado.
3. Todas las tablas deben ser exportables (botón "copiar" que pueda pegarse en una hoja de cálculo alcanza).
4. Paleta y estética nueva: ver "Sistema de diseño (rediseño en curso)" abajo — reemplaza a la vieja paleta
   Bootstrap claro de `app.py`/`transferencias_cohen.py`.
5. Los datos se refrescan cada 30 minutos (CEDEAR/GNR).

## Sistema de diseño (rediseño en curso)

`transferencias_cohenv2.html` es la referencia de estilo para el rediseño de **toda** la aplicación (no usar
`transferencias_cohen.html` viejo como guía). Define un sistema con tema oscuro/claro vía CSS custom
properties, tipografía serif+sans y acentos dorados sobre navy — es la base a aplicar en `app.py` y en el
`transferencias_cohen.py` nuevo.

**Tipografía**: `--fh: 'DM Serif Display', Georgia, serif` (headers/marca) · `--fb: 'DM Sans', -apple-system,
'Segoe UI', sans-serif` (cuerpo).

**Colores de marca** (fijos, no cambian con el tema):
- `--navy: #0F2044` · `--navy-deep: #0b1830` — navy base y navy profundo (fondos de header, botón "hoy")
- `--gold: #C9A84C` · `--gold-soft: #e3cd8a` — dorado de acento (bordes activos, texto de marca, botones primarios)

**Semánticos** (estado, fijos):
- `--ok: #2e9e6b` / `--ok-bg: rgba(46,158,107,.14)` — positivo/ejecutado/suscripción
- `--warn: #c98a1c` / `--warn-bg: rgba(201,138,28,.16)` — pendiente/rescate
- `--bad: #c4453f` / `--bad-bg: rgba(196,69,63,.14)` — rechazado/egreso
- `--neu: #6b7fa3` / `--neu-bg: rgba(107,127,163,.14)` — neutro/otro
- Ingresos usa azul suelto `#3b82f6` (no tiene variable propia)

**Tema oscuro** (default, `prefers-color-scheme: dark`):
`--bg:#0b1322` `--sur:#121d30` `--sur2:#172541` `--sur3:#1e2e50` `--tx:#eef1f6` `--mu:#7a8fb5`
`--bo:rgba(255,255,255,.09)` `--hd:linear-gradient(135deg,#0b1830,#0F2044 55%,#16243a)`
`--sh:0 8px 24px rgba(0,0,0,.35)`

**Tema claro**:
`--bg:#f3f5f9` `--sur:#ffffff` `--sur2:#f0f2f8` `--sur3:#e8eaf2` `--tx:#16213e` `--mu:#5c6880`
`--bo:#dde0ec` `--hd:linear-gradient(135deg,#0F2044,#16345c 60%,#1b3a66)` `--sh:0 4px 16px rgba(15,32,68,.08)`

**Otros tokens**: `--r: 12px` (radio estándar) · `--trans: color .2s, background .2s, border-color .2s`.

**Patrones de componente a reusar**: header (`.hdr`) con gradiente `--hd` y sticky top; tabs con
subrayado dorado en el activo (`.tbtn.on`); cards de KPI (`.kcard`) con `--sur`/`--bo`/`--sh`; badges de
estado con fondo `*-bg` y texto del color sólido correspondiente (`.badge-ejecutada`, `.badge-pendiente`, etc.);
drawer inferior (bottom sheet) para el detalle de fila en mobile, con botón "copiar" dorado
(`.btn-copy-detail`).

## Seguridad

- Autenticación de usuarios vía Google SSO (Streamlit `st.login`, requiere el paquete `authlib`), con
  autorización adicional por whitelist de emails (`EMAILS_AUTORIZADOS` en `app.py`).
- Credenciales de la API de Cohen y del OIDC viven en `.streamlit/secrets.toml` (gitignoreado). Tanto `app.py`
  (`st.secrets`) como `transferencias_cohen.py` (`tomllib` sobre el mismo archivo, o credenciales inyectadas por
  `app.py`) las leen de ahí — nunca hardcodear usuario/contraseña de la API en el código fuente.
- **Pendiente**: `.streamlit/secrets.toml` estuvo trackeado en git (el `.gitignore` tenía encoding UTF-16 roto
  y nunca lo excluyó de verdad) hasta el commit que arregla esto; `DASHBOARD_PASS`, `API_USER`/`API_PASS` y
  potencialmente credenciales OAuth de Google quedaron expuestas en la historia del repo en GitHub
  (`funam1/CedearV2`). El archivo ya no está tracked, pero **falta rotar esas credenciales** (nueva password de
  API, nuevo `client_secret`/`cookie_secret` de Google OAuth) para que la exposición histórica deje de ser
  explotable.

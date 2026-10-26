# starccm-copiloto

Agente que opera Simcenter STAR-CCM+ (2410, solo en este PC, 4 procesos) para turbomáquinas. No imita STAR-CCM+ en
una web: trabajas con la **GUI real**, conectada al mismo servidor que el agente, y le hablas desde un **panel
flotante de chat**. Diseño: [ARQUITECTURA.md](ARQUITECTURA.md). Reglas del agente: [CLAUDE.md](CLAUDE.md) (corto: se carga en
cada turno). Lecciones completas: [conocimiento/lecciones.md](conocimiento/lecciones.md). Recetas y casos:
[conocimiento/INDICE.md](conocimiento/INDICE.md).

## Instalar (una vez)

1. `.venv`: `python -m venv .venv` y `.venv\Scripts\pip install mcp`.
2. `.env`: copiar `.env.example` y poner la clave Power-on-Demand en `STARCCM_PODKEY` (ignorado por git; nunca en código ni logs).
3. `.mcp.json`: copiar `.mcp.json.example` y ajustar las dos rutas absolutas (python del `.venv` y `mcp_server.py`); reiniciar
   Claude Code (el servidor `starccm` aparece con sus 15 tools). El panel también lo usa (`--mcp-config`) y, si el archivo no
   existe, lo crea solo con las rutas de este equipo (y avisa en el chat). Un `.mcp.json` copiado con rutas de otra máquina NO se
   repara: borrarlo o corregir las rutas.
4. Carpeta de trabajo: no hace falta crearla; `COPILOTO_WORK` (por defecto `%LOCALAPPDATA%\starccm-copiloto`) se crea sola.

## Usar

1. Panel: `powershell panel\abrir_panel.ps1` (usa `pythonw` del `.venv`, sin consola) o `.venv\Scripts\python panel\chat_panel.py`.
2. Modo **Rápido** (defecto: sonnet, esfuerzo bajo) o **Preciso** (opus) en el selector del panel; Preciso solo para diagnósticos
   difíciles. Si escribes mientras el agente trabaja, el mensaje queda **en cola** y sale al terminar el paso; cada herramienta
   muestra «▶ … 12 s» con **cronómetro** en vivo y cada turno una línea gris con sus tiempos; **Detener** cierra la CLI
   (no toca el solver ni el residente), **Apagar** guarda y apaga con confirmación.
3. **Abrir GUI** (botón o tool `gui_attach`): `starccm+ -host localhost:47827`, sin licencia extra. Con GUI conectada nada apaga el solver solo: aparece el aviso de inactividad.
4. Una bomba de CFturbo: «corre la bomba de `<carpeta con .cfturbo-ccm y STEP>`» → `pump_case` con
   `params={"iterar": true, "post": true}`: importar, malla, física, iterar hasta estable y PNG en UNA macro (3 min 14 s medidos).

El solver solo arranca fuera del sandbox de Claude Code. Un solo solver vivo a la vez (licencia de una copia).

## Cómo funciona por dentro (resumen)

| pieza | qué hace |
|---|---|
| `mcp_server.py` | 15 tools. Un servidor `starccm+ -server`; las macros entran por un **cliente residente** (un cliente batch `-noexit` que vigila una carpeta; 0,7-1 s por macro frente a 8,6 s) y, si falla, por el cliente batch de siempre. Turnos con `.inject.lock` |
| vigilante de inactividad | proceso propio por puerto (`mcp_server.py --vigilante <puerto>`), lanzado desacoplado (fuera del árbol de la CLI: Detener no lo toca); mira macros en disco y GUI, escribe `gui`/`idle_min`/`idle_warning` en `.server.json` (en `COPILOTO_WORK`; `panel_estado.json` dice dónde) y, sin GUI, guarda y apaga a los `STARCCM_IDLE_MIN` (30) |
| `panel/chat_panel.py` | chat flotante (tkinter); lanza `claude -p … --resume` con las tools `mcp__starccm__*` |
| `pipelines/bomba.py` | genera la macro de bomba (y iterar/post) desde el `.cfturbo-ccm` + STEP |

## Probar

| comando | qué comprueba | coste |
|---|---|---|
| `.venv\Scripts\python mcp_server.py --selftest` | las tools contra `tests/fake_starccm.py` (solo temporales) | sin licencia, ~30 s |
| `.venv\Scripts\python panel\chat_panel.py --selftest` | el panel contra `tests/fake_claude.py`, sin pantalla | sin licencia, ~9 s |
| `.venv\Scripts\python pipelines\bomba.py --selftest` | lee un `.cfturbo-ccm` real, compila con el `javac` de STAR-CCM+ el Java generado, prueba el juez de estabilidad | sin licencia, ~70 s |
| `.venv\Scripts\python tests\real_e2e.py [carpeta_cad] [caso]` | **prueba real**: server_start, `pump_case` (iterar + post), server_stop; escribe `casos/<caso>/e2e.json` con tiempos por fase y resultados | **gasta licencia PoD**, ~3-5 min; lanzar desde una terminal normal (fuera del sandbox) y sin otro solver vivo |

## Carpetas

| carpeta | contenido |
|---|---|
| `pipelines/` | `bomba.py`: UNA macro de bomba (importar, regiones, interfaces, malla, física, MRF, fronteras, reportes, Euler) y, por `params`, `iterar` (H y M estables, máx. 1000) y `post` (planos, 3 PNG, y+, resultados) |
| `conocimiento/` | recetas Java (35), `lecciones.md`, casos de referencia, `plantillas_casos.md` y lo del agente anterior |
| `casos/<caso>/` | CAD, reportes, imágenes, `RESUMEN.md`, `e2e.json` (ignorado por git) |
| `panel/`, `tests/`, `poc_residente/` | panel de chat, falsos y prueba real, y la prueba de concepto del cliente residente |
| `COPILOTO_WORK` (fuera del repo y de OneDrive) | `sims/` (.sim de trabajo y logs del solver), `macros/` (macros inyectadas y su salida), `residente/`, `tiempos.csv`, `.server.json`, `.inject.lock` |

Por qué fuera de OneDrive: sincronizaría cada `.sim` de 50-150 MB en cada guardado y bloquearía archivos mientras se escriben.

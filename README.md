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
   Claude Code (el servidor `starccm` aparece con sus 16 tools). El panel también lo usa (`--mcp-config`) y, si el archivo no
   existe, lo crea solo con las rutas de este equipo (y avisa en el chat). `ensure_mcp_config` solo crea: un `.mcp.json` ya existente con rutas de otra máquina NO se
   repara; borrarlo (el panel lo recrea) o corregir las rutas a mano.
4. Carpeta de trabajo: no hace falta crearla; `COPILOTO_WORK` (por defecto `%LOCALAPPDATA%\starccm-copiloto`) se crea sola.

## Usar

1. Panel: `powershell panel\abrir_panel.ps1` (usa `pythonw` del `.venv`, sin consola) o `.venv\Scripts\python panel\chat_panel.py`.
2. Modo **Rápido** (defecto: sonnet, esfuerzo bajo) o **Preciso** (opus) en el selector del panel; Preciso solo para diagnósticos
   difíciles. Si escribes mientras el agente trabaja, el mensaje queda **en cola** y sale al terminar el paso; cada herramienta
   muestra «▶ … 12 s» con **cronómetro** en vivo y cada turno una línea gris con sus tiempos; **Detener** cierra la CLI
   (no toca el solver ni el residente) y, si había una macro en marcha en CUALQUIER fase, pide `cancel` solo; **Apagar** guarda
   y apaga con confirmación (sin timeout). Con el solver encendido más de `COPILOTO_AVISO_HORAS` (2) el panel avisa que la licencia cobra por hora.
3. **Abrir GUI** (botón o tool `gui_attach`): `starccm+ -host localhost:47827`, sin licencia extra. Con GUI conectada nada apaga el solver solo: aparece el aviso de inactividad.
4. Una bomba de CFturbo: «corre la bomba de `<carpeta con .cfturbo-ccm y STEP>`» → `pump_case` con
   `params={"iterar": true, "post": true}`: importar, malla, física, iterar hasta estable y PNG en UNA macro (162 s medidos de punta a punta con el servidor: ver «Medido»).

El solver solo arranca fuera del sandbox de Claude Code. Un solo solver vivo a la vez (licencia de una copia).

## Cómo funciona por dentro (resumen)

| pieza | qué hace |
|---|---|
| `mcp_server.py` | 16 tools (lista abajo). Un servidor `starccm+ -server`; las macros entran por un **cliente residente** (un cliente batch `-noexit` que vigila una carpeta; 0,7-1 s por macro frente a 8,6 s) y, si falla, por el cliente batch de siempre. Turnos con `.inject.lock` |
| `mcp_server.py --cli <tool> [json]` | acción directa SIN modelo (la usa el panel): `gui_attach`, `gui_status`, `server_stop`, `server_status`, `cancel`, `vigilante_relanzar`. Escribe el JSON por stdout y espera el turno `COPILOTO_CLI_LOCK_WAIT_S` (3 s). Salida 0 = respondió, 1 = lanzó, 2 = uso incorrecto |
| progreso en vivo | mientras corren `iterate` o `pump_case`, un hilo escribe `progreso.json` en `COPILOTO_WORK` (~1 s): fase, iteración, H, M, continuidad, transcurrido y falta. El panel lo muestra en una tarjeta; al acabar queda `fase: terminado` |
| vigilante de inactividad | proceso propio por puerto (`mcp_server.py --vigilante <puerto>`), lanzado desacoplado (fuera del árbol de la CLI: Detener no lo toca); mira macros en disco y GUI, escribe `gui`/`idle_min`/`idle_warning` en `.server.json` (en `COPILOTO_WORK`; `panel_estado.json` dice dónde) y, sin GUI, guarda y apaga a los `STARCCM_IDLE_MIN` (30) |
| `panel/chat_panel.py` | chat flotante (tkinter); lanza `claude -p … --resume` con las tools `mcp__starccm__*` |
| `pipelines/bomba.py` | genera la macro de bomba (y iterar/post) desde el `.cfturbo-ccm` + STEP |

## Tools (16)

| grupo | tools |
|---|---|
| servidor | `server_start`, `server_stop`, `server_status` |
| GUI | `gui_attach`, `gui_status` |
| operar | `inspect`, `run_java`, `run_steps`, `iterate`, `pump_case`, `cancel`, `export_scene`, `save` |
| conocimiento | `recipe_search`, `recipe_save`, `docs_search` |

`cancel(wait_s=45, forzar=False)` para la corrida en curso (`iterate` o CUALQUIER fase de `pump_case`): crea `ABORT` («Stop File»)
y `cancel.json`. En 2410 el Stop File **no corta un `step()` en curso**: la macro mira ABORT/`cancel.json` antes de importar,
entre fases, antes de la malla y entre bloques de iteración.

| fase al cancelar | qué pasa |
|---|---|
| iterar | para al final del bloque (≤ 50 it); sale con `stopped=cancelled`, sin post y con resultados parciales (H, M, eta) |
| importar, mallar | no se interrumpen: `cancel` queda `pendiente` (`parara_en_s`), la fase termina y la macro sale SIN iterar ni post |
| post | no se interrumpe; ya casi termina (usar `server_stop`) |
| dueño muerto (Detener mató la CLI y su MCP) | macro huérfana: se cancela igual, en cualquier fase; `cancel` espera el fin y limpia ABORT/`cancel.json` (si vence `wait_s`, el vigilante) |

Nunca se manda `stop()` desde otro cliente: tumba el servidor. El residente solo se mata con `forzar=True` y si la macro lleva
más de max(`COPILOTO_CANCEL_GRACE_S` = 120 s, 3 bloques según el ritmo) SIN salida; mientras avance se espera, y con macro
huérfana nunca se mata. Sin corrida en curso no crea ABORT.

## Medido (solver real, 4 procesos, BombaRapida, 2026-10-26)

| prueba | resultado |
|---|---|
| completa de punta a punta (`casos/BombaRapida_e2e_193651`) | **162 s** = `server_start` 36,5 + `pump_case` 119,4 + `server_stop` 6,6 |
| `pump_case` (119,4 s) | importar 5,5, malla 26,1, iterar 77,8 (250 it), post 2,0, resto ~8 |
| cancel con dueño vivo, it. 101 (`BombaRapida_e2e_203656`) | `cancel` 14,5 s; `pump_case` vuelve `cancelled` a los 21,5 s con resultados parciales (H 16,74 m, eta 88,6 %); servidor vivo, `iterate` de 20 it posterior OK |
| panel real, «Detener» en iterar (`BombaRapida_panel_204007`) | cancel 14,2 s (macro huérfana), apagado 7,4 s |
| panel real, «Detener» en malla (`BombaRapida_panel_204318`) | cancel 25,7 s: la malla termina y la macro sale SIN iterar; apagado 4,7 s |
| resultados | H 16,74 m, eta 88,6 %, H_Euler 18,75 m, `EULER_OK`, balance de masa 1,8e-4 % |

## Variables de entorno

Se leen del entorno, de `.env` en la raíz del repo y de `%USERPROFILE%\.starccm-agent.env` (manda el entorno real). Plantilla
completa en [.env.example](.env.example). Nunca claves reales en el repo.

| variable | por defecto | qué hace |
|---|---|---|
| `STARCCM_PODKEY` | vacío | clave Power-on-Demand (solo en `.env`) |
| `STARCCM_LICPATH` | `1999@flex.cd-adapco.com` | servidor de licencias |
| `STARCCM_HOME` | la carpeta más nueva de `C:\Program Files\Siemens\*\STAR-CCM+*` | instalación |
| `STARCCM_EXE` | `<HOME>\star\bin\starccm+.bat` | lanzador (un `.py` = falso de pruebas) |
| `STARCCM_PORT` | 47827 | puerto del servidor |
| `STARCCM_NP` | núcleos - 2 (4 en este PC) | procesos paralelos |
| `STARCCM_IDLE_MIN` | 30 | minutos sin actividad antes de guardar y apagar (con GUI solo avisa) |
| `STARCCM_VIGILANTE` | 1 | 0 = no lanzar el vigilante |
| `STARCCM_RESIDENT` | 1 | 0 = siempre cliente batch |
| `STARCCM_LOCK_WAIT_S` | 120 | segundos que una tool espera su turno |
| `STARCCM_PRECOMPILE` | 1 | compilar con `javac` antes de inyectar (solo vía batch) |
| `COPILOTO_WORK` | `%LOCALAPPDATA%\starccm-copiloto` | carpeta de trabajo pesada |
| `COPILOTO_VIGILANTE_S` | 30 | segundos entre comprobaciones del vigilante |
| `COPILOTO_VIG_GONE_S` | 3 | segundos con el servidor muerto antes de que el vigilante termine |
| `COPILOTO_CANCEL_GRACE_S` | 120 | segundos mínimos SIN salida de la macro tras `cancel` antes de darla por colgada (con `forzar=True` se mata el residente; nunca menos de 3 bloques de 50 it según el ritmo) |
| `STARCCM_MAX_HORAS` | 0 (sin tope) | tope DURO de horas con el solver encendido: el vigilante guarda y apaga aunque haya GUI |
| `COPILOTO_AVISO_HORAS` | 2 | solo el panel: horas con el solver vivo a partir de las cuales avisa (≤ 0 lo desactiva) |
| `COPILOTO_CLI_LOCK_WAIT_S` | 3 | espera del turno en `--cli` |
| `COPILOTO_DETACHED` | 0 | 1 = `DETACHED_PROCESS` (solo diagnóstico) |
| `COPILOTO_AGENT_LOG` | `<COPILOTO_WORK>\agent.log` | registro de eventos del agente |
| `PANEL_CLAUDE_BIN` | `claude` del PATH | solo el panel: ejecutable de la CLI |
| `PANEL_MCP_SERVER` | `mcp_server.py` del repo | solo el panel: script que lanza con `--cli` |
| `PANEL_MODEL_RAPIDO` / `PANEL_MODEL_PRECISO` | `sonnet` / `opus` | solo el panel: modelo de cada modo |

## Probar

| comando | qué comprueba | coste |
|---|---|---|
| `.venv\Scripts\python mcp_server.py --selftest` (delega en `tests/selftest_mcp.py`) | las tools contra `tests/fake_starccm.py` (solo temporales); falla si pasa de 45 s | sin licencia, ~37 s |
| `.venv\Scripts\python panel\chat_panel.py --selftest` | el panel contra `tests/fake_claude.py`, sin pantalla | sin licencia, ~10-20 s |
| `.venv\Scripts\python pipelines\bomba.py --selftest` | lee un `.cfturbo-ccm` real, compila con el `javac` de STAR-CCM+ el Java generado, prueba el juez de estabilidad | sin licencia, ~70 s |
| `.venv\Scripts\python tests\real_e2e.py [carpeta_cad] [caso] [--cancel]` | **prueba real**: server_start, `pump_case` (iterar + post), server_stop; escribe `casos/<caso>/e2e.json` con tiempos por fase y resultados. `--cancel` (nunca se lanza solo): pasada la it. 100 llama a `cancel()` y comprueba resultados parciales, ABORT/`cancel.json` borrados y servidor vivo (`iterate` de 20 it) | **gasta licencia PoD**, ~3-5 min; lanzar desde una terminal normal (fuera del sandbox) y sin otro solver vivo |
| `.venv\Scripts\python tests\real_panel_e2e.py [carpeta_cad] [--fase-detener N] [--detener-en-malla] [--sin-apagar] [--visible]` | **prueba real del panel**: la clase real `Panel` en Tk, `claude -p` → MCP → `pump_case`; pulsa «Detener» en la it. 100 (o la N; `--detener-en-malla` en la fase mallar), espera el cancel, apaga con el botón real; escribe `casos/<caso>/panel_e2e.json`. `--sin-apagar` deja el solver vivo, `--visible` muestra la ventana | **gasta licencia PoD y turnos de `claude`**, ~2-3 min; fuera del sandbox, sin otro solver vivo |

## Carpetas

| carpeta | contenido |
|---|---|
| `pipelines/` | `bomba.py`: UNA macro de bomba (importar, regiones, interfaces, malla, física, MRF, fronteras, reportes, Euler) y, por `params`, `iterar` (H y M estables, máx. 1000) y `post` (planos, 3 PNG, y+, resultados) |
| `conocimiento/` | recetas Java (35), `lecciones.md`, casos de referencia, `plantillas_casos.md` y lo del agente anterior |
| `casos/<caso>/` | CAD, reportes, imágenes, `RESUMEN.md`, `e2e.json` (ignorado por git) |
| `panel/`, `tests/`, `poc_residente/` | panel de chat, falsos y prueba real, y la prueba de concepto del cliente residente |
| `COPILOTO_WORK` (fuera del repo y de OneDrive) | `sims/` (.sim de trabajo y logs del solver), `macros/` (macros inyectadas y su salida), `residente/`, `tiempos.csv`, `.server.json`, `.inject.lock` |

Por qué fuera de OneDrive: sincronizaría cada `.sim` de 50-150 MB en cada guardado y bloquearía archivos mientras se escriben.

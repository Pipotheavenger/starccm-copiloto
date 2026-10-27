# EVAL_4 — evaluación independiente, ronda 4 (2026-10-26, tras el commit 698567e)

Alcance: CLAUDE.md, ARQUITECTURA.md, README.md, EVAL_3.md, `git log`/`git show --stat` de 2b82f81..698567e, código de
`mcp_server.py` (3268 l.), `panel/chat_panel.py` (1878), `pipelines/bomba.py` (1075), `tests/` y los artefactos reales
`casos/BombaRapida_e2e_193651`, `..._191110`, `..._panel_193939`, más `conocimiento/lecciones.md` (Servidor y operación).
No arranqué solver ni toqué licencia; no leí `.env`; solo creé este archivo (el árbol git sigue limpio).

## 0. Lo ejecutado (solver falso)

| comando | resultado |
|---|---|
| `mcp_server.py --selftest` | todo en verde, 42-43 s (límite del propio test: 45 s) |
| `pipelines/bomba.py --selftest` | TODO BIEN, ~50 s (compila la macro con el javac real) |
| `panel/chat_panel.py --selftest` | TODO OK, 19,0 s |

Aviso: por un descuido lancé dos tandas en paralelo, así que los tiempos están algo inflados (EVAL_3 midió 37 s / 9,1 s en
solitario). El panel pasó de 9 a 19 s con la carga, pero el margen del MCP (45 s) es corto.

## 1. Evidencia real leída (no repetida)

| prueba | dato |
|---|---|
| e2e 193651 | server_start 36,5 · pump_case 119,4 (importar 5,5 + malla 26,1 + iterar 77,8 + post 2,0) · server_stop 6,6 · total 162,5 s. H 16,74 m, eta 88,6 %, H_Euler 18,75 m, `EULER_OK`, balance de masa 1,8e-4 %, 3 PNG, y+ álabes medio 24,5 (zona 5-30), `ITER_STOP` ya en ASCII (`iteracion`, `variacion_max_pct`) |
| e2e 191110 | cancel desde el MCP: 14,3 s, `pump_case` cancelled a los 21 s, iterate de 20 it posterior OK. **Es anterior a los arreglos de la ronda 5**: `resultados {}` y mojibake en `ITER_STOP` siguen en ese archivo |
| panel 193939 | `claude -p` → MCP → pump_case; a la it. 100 se simula «Detener» (`kill_tree`, MCP dueño muerto); `--cli cancel` con `duenio_muerto`: 13,0 s, ABORT y cancel.json borrados, servidor vivo (`server_status`), `server_stop` 6,6 s, vigilante vivo. `progreso_final.resultado = {cancelled, stopped, huerfana}` |

Lectura crítica:
- El camino panel→CLI→MCP→solver ya es real, pero **«Detener» lo ejecuta el script** (`tests/real_panel_e2e.py:46-59` llama a
  `cp.kill_tree` y `cp.run_cli("cancel")`), no `ChatPanel.on_stop`/`auto_cancel`/`check_after_stop` ni la ventana Tk.
- Con dueño muerto no hay quien parsee: el resultado parcial de la macro huérfana no llega a ningún sitio (solo `cancelled`).
- La macro con RESULT parcial al cancelar (bomba.py:316-320) **no se ha visto con solver**: la única cancelación con dueño vivo
  (191110) es previa al cambio.
- ETA engañosa: en 193651 `falta_s` baja a 18,4 s (it. 91) y salta a 231,6 s (it. 109) porque pasa a estimar contra el tope de
  1000 it; la corrida acabó en la it. 250, ~40 s después.

## 2. Estado de los 12 defectos de EVAL_3

| id | estado | verificación en código/evidencia |
|---|---|---|
| 1 macro huérfana no cancelable tras Detener | **PARCIAL (casi resuelto)** | `cancel()` (mcp_server.py:2397) acepta dueño muerto con `macro_running()` y `fase=="iterar"`; real: 13 s, servidor vivo (193939); selftest 872-880 (con y sin macro). Sigue sin cubrir otras fases ni reapertura: defecto A |
| 2 flujo panel→CLI→MCP→solver sin ver | **PARCIAL** | visto con solver (193939) pero con «Detener» simulado por el script; la UI no: defecto B |
| 3 gracia fija de 60 s que mata el residente | **RESUELTO** | `_cancel_stall_s()` = max(120 s, 3×bloque según `ritmo`) de SIN salida de la macro (mcp_server.py:1233-1238, 1436-1446, 1586); con dueño huérfano `kill:false`. Residual: tras el plazo se mata por defecto, sin `kill` explícito (preferencia) |
| 4 mojibake | **RESUELTO** | 193651 `ITER_STOP` ASCII; selftest bomba 1001-1007 exige ASCII en todo `sim.println` de la macro y de las recetas |
| 5 cancelado sin resultados | **PARCIAL** | bomba.py:316-320 imprime RESULT parciales y `pump_case` los pone en `resultados` con `ok:false, cancelled:true` (mcp_server.py:2993-2995); solo probado por javac/selftest, nunca con solver: defecto C |
| 6 documentación atrasada | **PARCIAL** | README «16 tools», variables y `--cli` al día; ARQUITECTURA.md:58 dice «60 s … mata el residente (limitación conocida)», pero el código es 120 s/3 bloques/sin avance; falta `vigilante_relanzar`, cancel huérfano, 193651 y 193939 en «Medido»: defecto E |
| 7 `mcp_server.py` de 4 k líneas | **PARCIAL** | selftest a `tests/selftest_mcp.py` (4266→3268; mismo comportamiento, selftest verde). Vigilante, progreso, cancel, `--cli` y recetas siguen dentro |
| 8 vigilante: latido y relanzar | **RESUELTO** | hilo `latido` cada ≤5 s (mcp_server.py:723-731), `COPILOTO_TEST_VIG_BLOCK_S` lo prueba; `--cli vigilante_relanzar` (:757-763, `CLI_TOOLS` :3220); 193939 `vigilante_vivo:true` |
| 9 `--cli server_stop` con timeout 180 s | **RESUELTO** | chat_panel.py:917 `tmo = None if tool == "server_stop"`, selftest 1473 |
| 10 `_pid_is_ours` laxo | **PARCIAL** | `server_start`/`server_stop` comparan hora de creación (`t_create`, :2184, :2222, :2315). Siguen sin ella `_res_kill` (:1254-1261) y el fallback por nombre `cmd|python|java…` (:296) |
| 11 cancel solo en iterar, panel sin explicación | **RESUELTO** | `no_cancel_text` (chat_panel.py:572) y `explain_no_cancel` dicen fase, `falta_s` y qué cuesta apagar; selftest 1503-1506. No se añadió `save` tras mallar |
| 12 GUI abierta sin tope de horas | **ABIERTO** (preferencia) | no hay `STARCCM_IDLE_HARD_H`; solo `aviso_horas` en el panel (chat_panel.py:580) |

Resumen: 5 resueltos (3, 4, 8, 9, 11), 6 parciales (1, 2, 5, 6, 7, 10), 1 abierto (12).

## 3. Defectos (por severidad, 10)

**P2**

A. **La macro huérfana solo se cancela si Detener cae en la fase `iterar`.** `cancel()` exige `p.get("fase") == "iterar"`
   (mcp_server.py:2397-2400) y el panel solo cancela si `iterando` (chat_panel.py:950-956; el selftest 1577-1584 *exige* que con
   fase «mallar» no se llame a cancel). Reproducción: pump_case por el panel, Detener a los ~20 s (malla, de 113 s): la macro
   sigue sola hacia `iterar` (hasta 1000 it, ~5 min a 3,3 it/s; el criterio estable la para ~250 it), nadie actualiza
   `progreso.json` (viejo > 45 s → `active=False`, chat_panel.py:525-553), el candado tiene dueño muerto (`stop_warning` devuelve
   `None`) y no aparece «Parar iteraciones»; `cancel` responde «no hay iteración en curso». Lo mismo si se cierra y reabre el
   panel con la macro en `iterar` ya sin latido. Arreglo: en `cancel()` basar `huerfana` en `macro_running()` + dueño muerto, sin
   mirar la fase del JSON viejo, y poner ABORT/cancel.json aunque aún sea malla (la macro mira `pxCan.c()` antes del primer
   `step()`, bomba.py:277, y salta el post). En el panel, Detener con dueño muerto y `macro_running` llama siempre a `cancel`;
   `stop_warning` debe avisar de macro huérfana por el `.pid` de la macro, no por el candado.

B. **El «Detener» real del panel no se ha ejercido con solver.** `tests/real_panel_e2e.py:46-59` reimplementa on_stop.
   `ChatPanel.on_stop`, `auto_cancel`, `check_after_stop`, `ensure_mcp_config` y la ventana Tk solo tienen selftest con
   `fake_claude`/`fake_starccm`. Es lo que separa 8,5 de 9 en robustez. Arreglo: arnés que instancie `ChatPanel` (Tk real,
   `root.update()`), llame a `on_send`/`on_stop`/`on_cancel`/`on_off_now` y guarde `panel_e2e.json` con la salida del chat; o
   corrida manual registrada en `casos/`.

C. **Resultados parciales al cancelar sin verificar con solver (y la evidencia vieja contradice).** bomba.py:316-320 pide cada
   reporte con `getReport(k).getReportMonitorValue()` dentro de try/catch silencioso; si algún reporte no existe o falla,
   `resultados` sale `{}` sin aviso. `casos/BombaRapida_e2e_191110/e2e.json` sigue mostrando `resultados {}` y mojibake. Arreglo:
   una corrida `tests\real_e2e.py --cancel` (dueño vivo) archivada; imprimir `WARN` en el `catch`.

**P3**

D. **ETA que salta.** `falta_s` 18,4 → 231,6 s al pasar de la it. 100 (193651 y 191110): tras la it. 100 estima contra 1000 it. El
   panel pinta «faltan ~4 min» cuando restan 15-40 s. Arreglo: mostrar `falta_s` como «≤ N s» con `falta_fuente == "ritmo"` e
   `iteracion ≥ 100`, o alisar con la variación observada (`falta_max_s` ya existe).

E. **Documentación aún desfasada.** ARQUITECTURA.md:58 (60 s, «mata el residente», «limitación conocida») contradice
   mcp_server.py:1233-1238/1586; falta `vigilante_relanzar`, cancel con dueño muerto y `real_panel_e2e.py` en README/ARQUITECTURA/
   CLAUDE.md; «Medido» en ARQUITECTURA sigue en 184823 (no 193651/193939). Arreglo: actualizar y, si se quiere evitar otro
   «15 vs 16», generar la lista de tools desde `@_tool` en el selftest (comparar con README).

F. **PIDs sin hora de creación en procesos que se matan.** `_res_kill` (mcp_server.py:1254-1261) mata `residente.json:pid` tras
   `_pid_alive`; el fallback de `_pid_is_ours` (:296) acepta cualquier `cmd|python|java|star|mpi…`; `cancel` da por vivo al dueño
   con solo `_pid_alive` (:1974-1977). Probabilidad baja, daño alto. Arreglo: guardar y comparar `t_create` (ya existe
   `_proc_ctime`) también para residente, vigilante y dueño del turno.

G. **Tras el plazo, `cancel` mata el residente por defecto.** Con `kill:true` implícito (mcp_server.py:1987-1988, 1441-1446),
   si la macro lleva > max(120 s, 3 bloques) sin salida se abandona y se mata el residente: es lo que la propia lección dice que
   puede tumbar el servidor y se pierde el bloque. Más acotado que en EVAL_3, pero sigue siendo decisión silenciosa. Arreglo:
   `cancel(kill=False)` por defecto; devolver «sigue parando» y matar solo con `kill=True`. (Preferencia con coste: máx. 0,25.)

H. **`mcp_server.py` sigue en 3268 líneas** (vigilante, progreso, cancel, `--cli`, recetas, residente en un archivo). Preferencia
   (0,25): extraer `vigilante.py` y `progreso.py`; la frontera ya existe porque `--vigilante` y `--cli` no importan `mcp`.

I. **GUI abierta = sin apagado automático, sin tope** (defecto 12 de EVAL_3, abierto). Con PoD por hora, una GUI olvidada cobra
   indefinidamente; solo hay aviso a las 2 h. Preferencia (0,25): `STARCCM_IDLE_HARD_H` (p. ej. 4 h) que guarde y apague igual.

J. **Margen de los selftests.** El del MCP tarda 42-43 s contra un propio check de «< 45 s» (flaky bajo carga); el del panel
   19 s con dos tandas en paralelo. Arreglo: subir el umbral a 60 s o medirlo con `time.process_time`. Preferencia.

## 4. Notas

| eje | nota | justificación |
|---|---|---|
| **DISEÑO** | **8,5** (EVAL_3: 8,0) | Sube: selftest fuera de `mcp_server.py` (−1000 l.), latido del vigilante en hilo propio, `t_create` en el apagado, ASCII verificado por test, README/CLAUDE.md al día, `--cli` sin modelo. Resta 1,5: decisión de cancelar basada en la **fase de un JSON viejo** en vez del estado vivo de la macro (A, 0,5); ARQUITECTURA contradice al código en cancel y no refleja lo medido (E, 0,5); archivo grande (H, 0,25); sin tope de GUI (I, 0,25) |
| **FUNCIONALIDAD** | **9,0** (EVAL_3: 9,0) | Evidencia real fuerte: 162 s de punta a punta (de 187), Euler OK, balance de masa 2e-4 %, camino del panel con `claude -p` real, cancel con dueño muerto en 13 s, apagado 6,6 s. Resta 1,0: resultado parcial al cancelar sin ver con solver (C, 0,5), un solo caso y sin curva de caudales, y+ medio 24,5 solo como dato (0,25), ETA engañosa (D, 0,25) |
| **ROBUSTEZ** | **8,5** (EVAL_3: 8,0) | Sube: gracia dependiente del ritmo sin matar mientras avanza (3), cancel con dueño muerto probado real (1 parcial), apagado sin timeout, vigilante con latido propio y relanzable, JSON de estado con `_replace_retry` (el vigilante moría en su primer latido: arreglado y encontrado por la prueba real). Resta 1,5: orphan fuera de `iterar` invisible e incancelable (A, 0,75); UI real del panel sin ejercer (B, 0,5); PIDs y kill por defecto (F, G, 0,25) |

## 5. Qué falta para 9 o más

- **DISEÑO → 9**: A (estado vivo de la macro como fuente de verdad, no `fase` del JSON); ARQUITECTURA.md coherente con el código
  y con 193651/193939 (E); extraer vigilante/progreso (H). Con eso 9,0-9,25.
- **FUNCIONALIDAD → 9,5**: una corrida `--cancel` con dueño vivo y su `resultados` parcial archivada (C); ETA honesta (D); un
  segundo caso real (otra bomba o 3 caudales de la curva) con su e2e.json; el aviso de y+ en 5-30 con acción recomendada
  integrado en el resumen de `pump_case`.
- **ROBUSTEZ → 9**: cerrar A con selftest «dueño muerto + malla»; una corrida real que ejerza `ChatPanel` por su UI (B);
  `t_create` en residente/vigilante/dueño (F) y `kill` explícito (G). El riesgo residual sería el solver mismo.

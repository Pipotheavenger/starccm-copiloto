# EVAL_3 — evaluación independiente, ronda 3 (2026-10-26, tras el commit 2b82f81)

Alcance: lectura de CLAUDE.md, ARQUITECTURA.md, README.md, EVAL_2.md, `mcp_server.py` (4024 l.), `panel/chat_panel.py` (1643 l.),
`pipelines/bomba.py` (1045 l.), `tests/`, y los artefactos reales `casos/BombaRapida_e2e_184823/e2e.json` y `..._191110/e2e.json`.
No arranqué solver ni toqué licencia; no leí `.env`; solo creé este archivo.

## 0. Lo ejecutado (solver falso)

| comando | resultado |
|---|---|
| `mcp_server.py --selftest` | todo en verde, 37,0 s (< 45 s); 158 `check(` en el archivo |
| `pipelines/bomba.py --selftest` | TODO BIEN (compila con el javac real) |
| `panel/chat_panel.py --selftest` | TODO OK, 9,1 s |

Sin cambios en el árbol git (limpio) antes y después, salvo este archivo.

## 1. Evidencia real leída (no repetida)

| prueba | dato |
|---|---|
| e2e 184823 | server_start 36,1 s · pump_case 147,4 s (malla 41,2 + iterar 88,1 + post 2,4 + resto ~8) · server_stop 4,0 s · total 187,4 s. H 16,74 m, η 88,6 %, H_Euler 18,75 m, `EULER_OK`, balance de masa 1,8e-4 %, 3 PNG. Vía residente |
| e2e 191110 (cancel) | cancel llamado en la it. 103; `cancel` devuelve en 14,3 s, pump_case vuelve `cancelled` a los 21,3 s (la macro paró en la it. 150: fin de bloque), ABORT y cancel.json borrados, turno libre, `iterate` de 20 it OK, server_stop 6,1 s |

Lectura crítica: en 191110 `resultados` queda `{}` (no hay H/M aunque la solución en la it. 150 existe) y `ITER_STOP` trae mojibake
(`iteraciÃ³n`, `variaciÃ³n`): ver defectos 4 y 5. El cancel se llamó **en el mismo proceso** que corría pump_case (`tests/real_e2e.py`
hace `import mcp_server as m`), no por el camino del panel.

## 2. Defectos de EVAL_2

| id | estado | evidencia |
|---|---|---|
| N1 aviso Euler espurio | **RESUELTO** | 184823: `avisos` solo `BALANCE_MASA` y `EULER_OK`; selftest bomba comprueba que la validación va tras iterar |
| N2 apagar depende del modelo | **RESUELTO** | `--cli server_stop` (mcp_server.py:3984+, chat_panel.py:449-470, 858-880); selftest panel «Sí llama a --cli server_stop directo» 0,7 s; selftest MCP lo apaga de verdad |
| N3 vigilante muerto invisible | **RESUELTO** (aviso) | banda roja `vigil_down` por `idle_checked` > 2 min (chat_panel.py:417, selftest 1281-1291). Nadie relanza el vigilante (defecto 9) |
| D8 macro sin cancelación | **PARCIAL** | Cancelar DURANTE `iterar` funciona con solver real (191110). No cubre: dueño muerto (defecto 1), fases distintas de iterar (3), cuelgue por timeout |
| Prueba real de punta a punta | **PARCIAL** | e2e Python directo repetido sin carga (187 s). Panel → `claude -p` → MCP → solver sigue sin verse con el solver real (defecto 2) |
| N4 documentación | **NO RESUELTO / peor** | defecto 6 |
| N5 pids que no coinciden | **RESUELTO** | `estado_panel` de e2e.json trae `pid`/`pid_real`, `vigilante`, `residente`/`residente_real` con valores reales (antes `null`) |
| N6 tiempos contaminados | **RESUELTO** | 184823: 187 s limpios, desglose coherente con 174159 |
| N7 `.mcp.json` obsoleto no se repara | **PENDIENTE** | README paso 3 sigue diciéndolo; `ensure_mcp_config` solo crea |
| N8 GUI con 30 s de retraso | **PENDIENTE** (P3) | sin cambios |
| D13 codificación | **NO RESUELTO, ahora con evidencia** | defecto 4 |
| D15 progreso en vivo / DPI | **RESUELTO** | `progreso.json` con fase, it., H, M, falta_s (muestras en e2e.json); tarjeta en el panel; `dpi_aware` |
| D16 poda de logs/clases | **RESUELTO** | selftest «mantenimiento: residente-*.log deja los 3 últimos / .classes > 7 días» |

## 3. Defectos nuevos y pendientes (por severidad, 12)

**P2 (reales, afectan al dinero o a la corrida)**

1. **Tras «Detener» la macro huérfana ya no se puede cancelar.** Detener hace `taskkill /T` de la CLI y por tanto del MCP dueño de `pump_case`; la macro sigue en el solver (por diseño). `cancel()` exige `_progreso_vivo` = `_pid_alive(p["pid"])` del dueño muerto (mcp_server.py:1884-1887, 2284-2290) y responde «no hay iteración en curso», aunque la macro siga iterando hasta 1000 it. Además nadie actualiza `progreso.json`: el panel deja de ver cálculo tras `PROG_STALE_S` (chat_panel.py:535) y el botón «Parar iteraciones» (check_after_stop, :911) puede no aparecer o no servir. Es justo el escenario para el que se hizo cancel. Un `server_stop` posterior queda en la cola del residente detrás de la macro. Reproducir: iniciar pump_case por el panel, Detener a los 60 s, pulsar «Parar iteraciones». Arreglo: en `cancel()`, aceptar dueño muerto si `progreso.fase == "iterar"` y el residente está vivo con macro en curso (`.pid` de la macro / latido) y escribir igualmente ABORT + cancel.json (la macro ya los mira por su cuenta, bomba.py recibe `abort_file`/`cancel_file`); en el panel, `active` por `fase=="iterar"` + residente ocupado, no solo por la edad del archivo. Añadir al selftest «dueño muerto».
2. **Flujo panel → CLI real → MCP → solver nunca verificado con el solver real.** `real_e2e` importa el MCP en proceso; cancel por `--cli` (proceso aparte, comunicación por `cancel.json`), `--disallowedTools` junto a `mcp__starccm__*`, Detener a mitad, cierre con «Sí», `ensure_mcp_config` solo tienen selftest con falsos. Arreglo: una corrida real manual con el panel (cancel por botón, Detener, Apagar) y registrarla en `casos/`. Es lo que separa 8 de 9 en robustez.
3. **El cuarto de gracia del cancel puede tumbar el servidor.** `CANCEL_GRACE_S = 60` fijo (mcp_server.py:1497) y a los 60 s se mata el residente (:1354-1355, `_res_kill` :1366) a mitad de `step()`: la propia lección de CLAUDE.md dice que eso TUMBA el servidor. Con 175 k celdas un bloque de 50 it dura ~17 s; con ~600 k+ celdas (o la máquina cargada) supera 60 s y el «cancelar» destruye la solución sin guardar. Arreglo: gracia = max(60, 3 × 50 / `ritmo_it_s`) con el ritmo de `progreso.json`, y en vez de matar, devolver «sigue parando, el bloque termina en ~N s» (no abandonar la macro); matar solo con `kill:true` explícito.

**P3 (calidad, mantenimiento, documentación)**

4. **Mojibake persistente (D13).** `pipelines/bomba.py:302` y `conocimiento/recetas/iterar-hasta-estable.java:83` imprimen `"iteración"`, `"variación_max_%"` desde un fuente Java escrito en UTF-8 y compilado/leído con la codificación del sistema: e2e 191110 `parsed.ITER_STOP` = `iteraciÃ³n 150 variaciÃ³n_max_%`. Rompe cualquier regex sobre esas claves y viola la lección «solo ASCII». Arreglo: ASCII (`iteracion`, `variacion_max_pct`) en todas las líneas marcadas del pipeline y recetas; añadir al selftest bomba un check «solo ASCII en println de marcadores».
5. **Cancelado = sin resultados.** Un `pump_case` cancelado devuelve `ok: true` con `resultados: {}` (191110) aunque el solver tiene solución en la it. 150; `iterate` sí devuelve `reports`. Quien lee `ok: true` cree que acabó. Arreglo: la macro imprime los reportes (H, M, eta, Q) al ver ABORT y `pump_case` los pone en `RESULT` con `cancelled: true`, `ok` solo si hubo post; o `ok:false, cancelled:true`.
6. **Documentación atrasada respecto al código.** README dice «15 tools» (3 sitios) y no menciona `cancel`, `--cli`, `progreso.json`, `--cancel` de `real_e2e`; ARQUITECTURA tiene «Herramientas (15)» con una lista de «Heredadas» duplicada, la sección «Ronda 4 (en curso… no documentado como hecho)» cuando ya está hecho, «Medido» sin 184823/191110 y sigue diciendo «pendiente de verlo con solver» del Euler (ya visto: `EULER_OK`). El código tiene 16 `@_tool`. Arreglo: actualizar los tres archivos y las variables de entorno nuevas (`COPILOTO_CANCEL_GRACE_S`, `COPILOTO_VIGILANTE_S`, `COPILOTO_VIG_GONE_S`, `STARCCM_VIGILANTE`).
7. **`mcp_server.py` de 4024 líneas con ~900 de selftest embebido** (los `check(` desde ~3140). Mezcla servidor, vigilante, cancelación, progreso, `--cli`, recetas. Arreglo: mover el selftest a `tests/selftest_mcp.py` y `vigilante`/`progreso` a módulos propios (el `--vigilante` ya evita el import de `mcp`, la frontera existe). Es preferencia con coste de mantenimiento real, no un bug.
8. **Vigilante: latido cada 30 s y ninguno durante un `server_stop` largo; nadie lo relanza.** `beat()` solo corre dentro del chequeo (mcp_server.py:~690) y `_idle_stop → server_stop` puede durar minutos: `_vigilante_pid` (umbral 3×30+30 = 120 s, :644) lo da por muerto y un `start_watchdog` simultáneo lanzaría otro. Si muere de verdad, el panel solo avisa (banda roja). Arreglo: hilo de latido independiente; que `--cli server_status` o el botón «Reiniciar vigilante» llame a `_vigilante_ensure`.
9. **`--cli server_stop` con timeout 180 s en el panel** (chat_panel.py `direct`, `tmo = 180`): `server_stop` inyecta con timeout 1800 s y guarda un `.sim` grande; `subprocess.run(timeout)` mata el python a mitad y deja `.server.json` y el residente a medias, con el solver cobrando. Arreglo: 600 s o sin matar (sondear `.server.json`).
10. **`_pid_is_ours` acepta cualquier `cmd|python|java|star|mpi…`** (mcp_server.py:250-256). Un PID reasignado a un python ajeno se mata con `/T /F` (`server_stop` :2213, `server_start` :2082-2092). Probabilidad baja, daño alto. Arreglo: comparar también hora de creación o línea de comandos (`-server`, puerto).
11. **Cancel solo existe en la fase `iterar`.** Malla (26-50 s en este caso, minutos en mallas grandes) e importar no se interrumpen: `cancel` devuelve «la macro está en la fase X: usa server_stop» (mcp_server.py:2288-2291), que pierde lo hecho. Limitación del solver (el Stop File no corta una operación), pero el panel no lo explica. Arreglo: texto en el panel «la malla no se puede parar; termina en ~N s» con el `falta_s` de progreso, y `save` tras mallar para poder apagar sin perder.
12. **GUI abierta = apagado automático desactivado sin tope** (mcp_server.py:~585): con PoD por hora, una GUI olvidada abierta cobra indefinidamente; solo hay aviso. Preferencia de diseño, pero con coste: tope duro opcional (`STARCCM_IDLE_HARD_H`, p. ej. 4 h) que guarde y apague aunque haya GUI.

## 4. Notas

| eje | nota | justificación |
|---|---|---|
| **DISEÑO** | **8,0** | Arquitectura clara y coherente (un servidor, un residente, un vigilante, turnos por archivo, acciones directas `--cli` sin modelo, estado fuera de OneDrive), pipeline fijo en una macro, conocimiento bien separado, selftests sin licencia con solver falso. Resta: archivo de 4 k líneas con el selftest dentro (7), documentación que va por detrás del código (6), mojibake que contradice la propia lección ASCII (4) |
| **FUNCIONALIDAD** | **9,0** | Evidencia real fuerte y repetida: 187 s de punta a punta sin carga, Euler validado (`EULER_OK`), balance de masa 2e-4 %, progreso en vivo con falta_s, cancel limpio en 14 s con el servidor vivo y reutilizable, apagado directo 4 s. Panel con tarjeta de progreso y acciones sin modelo. Resta: un solo caso verificado con solver (BombaRapida; curva de caudales y geometrías sin CFturbo siguen «por verificar»), cancelado sin resultados (5), y y+ medio 24,5 en la zona 5-30 que el pipeline solo avisa |
| **ROBUSTEZ** | **8,0** | Subió de 7,5: cancelación sin tumbar el servidor (probada real), vigilante visible, apagado sin LLM, estado sin carreras de `.tmp`, pids reales, poda. Resta: macro huérfana no cancelable tras Detener (1), gracia fija que mata el residente (3), camino panel → solver real sin probar (2), timeout de apagado del panel (9), `_pid_is_ours` laxo (10) |

## 5. Qué falta para 9/10

- **DISEÑO → 9**: mover selftest y vigilante/progreso fuera de `mcp_server.py` (7); README/ARQUITECTURA/CLAUDE.md al día con cancel, `--cli`, progreso, variables de entorno (6); ASCII en marcadores (4); unificar la lista de tools en un solo sitio generado del código (evita el «15 vs 16»).
- **FUNCIONALIDAD → 9,5**: resultados al cancelar (5); segundo caso real (otra bomba o curva de 3 caudales) con su e2e.json; el aviso de y+ en 5-30 con la acción recomendada (afinar prismas) integrada en el resumen del `pump_case`.
- **ROBUSTEZ → 9**: cerrar 1, 3 y 2 (cancel de macro huérfana con selftest; gracia dependiente del ritmo sin matar el residente; UNA corrida real por el panel con cancel por botón, Detener y cierre), más 8-10 como endurecimiento. Con eso el único riesgo residual sería el propio solver.

# EVAL_1 — evaluación del evaluador (2026-10-26 ~17:30–17:50)

Alcance: código y documentos leídos completos (mcp_server.py, panel/chat_panel.py, pipelines/bomba.py, tests, ARQUITECTURA, CLAUDE,
README, constructores A–C2, poc_residente, INDICE, plantillas). No se arrancó solver ni `starccm+`; no se tocó `.server.json`,
`.inject.lock` ni `%LOCALAPPDATA%\starccm-copiloto`. Se leyeron (solo lectura) `tiempos.csv`, `agent.log`, `macros/*.out` y `residente/*.log`
de las dos corridas reales del orquestador (17:36 y 17:37). Estado del árbol al evaluar: mcp_server.py, pipelines/bomba.py y dos recetas
con cambios SIN commit hechos por el orquestador durante la evaluación (marcados abajo).

Comandos que SÍ corrí (todos en verde):

| comando | resultado |
|---|---|
| `panel/chat_panel.py --selftest` | TODO OK, 35 checks, 3,0 s |
| `pipelines/bomba.py --selftest` | TODO BIEN, ~45 checks (javac de macro rápida/normal/iterar+post/13 bloques de plantillas), 70 s |
| `mcp_server.py --selftest` | todo en verde, 107 checks, 33 s. Comprobado antes en el código que solo usa temporales: `STATE_FILE`, `LOCK_FILE`, `AGENT_LOG`, `WORK`, `SIMS`, `MACROS`, `RES_DIR`, `CASES`, `RECIPES` se redirigen a `tempfile.mkdtemp` (l. 2047-2059), puerto libre aleatorio, `STARCCM_EXE` = falso, clave de mentira; solo mata pids propios. Tras correrlo el `.server.json` real ya no existía porque el orquestador había apagado su solver a las 17:38:55 (`agent.log`), antes de mi arranque |

## 1. Tabla de verificación

### MCP (ARQUITECTURA §1)

| # | requisito | veredicto | evidencia |
|---|---|---|---|
| 1 | Turnos `.inject.lock` (pid, tool, inicio; espera; busy; huérfano) | OK con reserva | mcp_server.py:269-362; selftest cubre pid muerto, >2 h, propio, vivo → busy, espera y corre. Reserva: D6 (OneDrive) |
| 2 | `gui_attach` / `gui_status`; nunca mata la GUI; `server_stop` avisa | OK | l. 1310-1341, 1261-1264; selftest con GUI falsa real; el panel real mostró la GUI de la corrida real |
| 3 | Vigilante: con GUI avisa (`idle_warning`), sin GUI guarda y apaga | PARCIAL | l. 459-500 correcto en el MCP; pero solo vive mientras vive el proceso MCP: en modo panel cada turno es un `claude -p` nuevo (D3) y el panel no sabe pintar el dict (D4) |
| 4 | `run_steps` en UNA macro, STEP_*, resultado por paso | OK | l. 1384-1501; selftest (paso falla, skipped, PARAMS, params sin uso) |
| 5 | `iterate(until="stable")`, plateau, motivo de parada | PARCIAL | lógica OK (l. 1655-1737, selftest stable/plateau/max/n). `n=100` por defecto (l. 1685) hace imposible «stable»: necesita ≥150 it con ventana 100 y bloque 50 (D9) |
| 6 | `pump_case` ↔ `pipelines/bomba.py` (`parse_ccm`, `macro_body`, `IMPORTS`) | PARCIAL | Contrato roto en la corrida real de las 17:36 (D1, ya corregido sin commit, l. 1781-1783); sin test de regresión: el selftest usa una bomba falsa con `IMPORTS = ""` (l. 2022). Nombres de reportes: el pipeline crea `H` y `M` (bomba.py:638,643) = los de `iterate` por defecto → OK |
| 7 | Seguridad: clave solo en `.env`, `_redact`/`_scrub` | PARCIAL | Barrido de la clave en repo, WORK y historial git: solo aparece en `.env` (0 fugas en `.server.json`, `agent.log`, `.out`, `.log`, `residente.json`, git). Brecha: tool `Read` sin restricción (D5) |
| 8 | Residente por defecto, fallback batch, `via`, `ms`, `tiempos.csv` | OK | l. 691-1031; `tiempos.csv` real: server_start 47 s, pump_case 3,6 s / 66 s, server_stop 7-8 s, todas `via=residente` |
| 9 | Carpetas pesadas fuera de OneDrive | PARCIAL | `sims/`, `macros/`, `residente/`, `tiempos.csv` sí (WORK = `%LOCALAPPDATA%`). `.server.json`, `.inject.lock`, `agent.log`, `casos/`, `.venv` y `.env` siguen en OneDrive (D6) |
| 10 | Ruta con espacios | OK | `Program Files` y `OneDrive\Documentos` funcionaron en la corrida real. Acentos en rutas: no probados (ver D13) |

Casos de borde:

| caso | veredicto | evidencia |
|---|---|---|
| servidor muerto antes de inyectar | OK | `RuntimeError("No hay servidor…")` l. 657-661 |
| servidor muere a mitad de macro | OK | `server_died` l. 1012-1022, selftest |
| residente muerto entre macros / a mitad / sin latido | OK | relanza; a mitad no repite por batch (l. 926-940); selftest |
| residente que no arranca | OK | 2 intentos, batch + pausa 5 min (l. 843-859) |
| candado huérfano | OK | l. 282-289 (salvo D6) |
| macro colgada / timeout | PARCIAL | mata el residente, relanza, `client_killed` (selftest). La macro del solver NO se aborta y el turno se libera: la siguiente macro entra en cola detrás de la colgada sin aviso (D8) |
| GUI conectada | OK | corrida real verificada por el orquestador; tabla ARQUITECTURA |
| «Detener» del panel | FALLA | D2 |
| error de compilación en el residente | OK | real: ms 3 643, devuelve el texto de javac en `stdout` (últimos 4000 car.) y el residente sigue vivo |

### Panel (ARQUITECTURA §2)

| requisito | veredicto | evidencia |
|---|---|---|
| ventana pequeña, siempre encima, redimensionable, tema claro, solo stdlib | OK | chat_panel.py:305-311; captura real |
| historial con negrita/tablas mono, Enter/Shift+Enter | OK | l. 366-367, 389-401, selftest |
| barra de estado: solver, caso, inactividad, GUI, «agente trabajando» | PARCIAL | La captura real mostró «Solver vivo · BombaRapida_e2e · 0 min inactivo \| GUI ? \| agente trabajando (pump_case)»: la GUI sale siempre «?» (D4) |
| botones Abrir GUI, Detener, Apagar (confirmación), Nueva | PARCIAL | existen (l. 335-340); Abrir GUI y Apagar mandan un mensaje al modelo en lugar de actuar (D14); Detener (D2) |
| línea «▶ … 12 s ✓» por herramienta + cronómetro | OK | l. 508-523, 556-563; selftest |
| comando `claude -p … --resume`, `--mcp-config`, `--allowedTools`, env sin `CLAUDE*`, `--append-system-prompt` | OK | l. 116-127, 105-106 |
| `--mcp-config` apunta a `.mcp.json` | FALLA en instalación nueva | no existe `.mcp.json` en el repo (solo `.example`, ignorado por git); `build_cmd` no lo comprueba (D7) |
| hilo, ventana nunca se congela, selftest sin pantalla | OK | l. 139-183, 580-717 |
| extras (modo Rápido/Preciso, cola, tiempos por turno) | OK | selftest |

### Documentación y conocimiento

| punto | veredicto | evidencia |
|---|---|---|
| CLAUDE.md describe carpetas fuera de OneDrive y residente | OK | CLAUDE.md:33-43 |
| README al día | FALLA | Lista `sims/`, `macros/` como carpetas del repo (README:34); no menciona `COPILOTO_WORK`, residente, `tiempos.csv`, `STARCCM_RESIDENT` |
| `.env.example` al día | FALLA | l. 1 dice «Copiar a starccm/.env»; faltan `COPILOTO_WORK`, `STARCCM_RESIDENT`, `STARCCM_LOCK_WAIT_S`, `STARCCM_PRECOMPILE` |
| CLAUDE.md «Cómo se inyecta» | PARCIAL | l. 100-113 sigue describiendo el cliente batch por tool y «antes de inyectar compila con javac»; ahora el residente no precompila y la tabla de 8-10 s es la vía de respaldo |
| ARQUITECTURA | PARCIAL | no recoge residente, WORK, modos del panel; «recetas (34)» |
| 36 recetas en el índice | PARCIAL | hay 35 `.java` (+ README); las 35 están en INDICE.md (sin faltantes); el título dice «(36)» (INDICE.md) y ARQUITECTURA «34». Frente al agente anterior (33) no falta ninguna |
| `caso.json` / `sim_destino` / `mirror_sim` | FALLA doc | implementado (mcp_server.py:1851-1880) pero no aparece en CLAUDE/README/ARQUITECTURA |
| lecciones vigentes conservadas | PARCIAL | Se conservan las de entorno, API, malla, física, escenas, servidor. Se perdieron: criterio de y+ en álabes (30-300, o <5 con all-y+); pasos de CAD genérico (surface wrapper, leak detection, extracción de volumen) por si la geometría no viene de CFturbo; «estimar celdas antes de mallar y respetar el límite del usuario» (queda solo «preguntar > ~400 k»); las `pregunta` con JSON/botones (ya no hay UI web: ahora son texto) |

## 2. Defectos

### P0

**D1. `pump_case` rompía la macro con el pipeline real (contrato `IMPORTS`).** Corrida real 17:36:18: `pump_case` falló en 3,6 s.
`residente-47827-480.log`: `error: unclosed character literal  import 'star.dualmesher.*';`. Causa: `pipelines/bomba.py:54` define `IMPORTS`
como lista y `mcp_server.py` hacía `str(lista)`. Estado: corregido en el árbol (mcp_server.py:1781-1783, `",".join`), SIN commit.
Reproducir el defecto antiguo: `str(mod.IMPORTS)` pasado a `_wrap` da 19/19 líneas `import` rotas (lo comprobé). Faltan: commit y un test de regresión; los
selftests nunca ejercitan `pump_case` con el `bomba.py` real (D12).

**D1b. Monitores de Euler/eta con división por cero abortan `StepSimulation`.** Corrida real 17:37 (`Agent_20261026_173741_174.out`):
«Non-finite value detected … ExpressionReport("eta_Euler")» con `H_Euler = 0` en la iteración 1 (UVu1=UVu2=0) → `AGENT_ERROR`, la macro entera falla tras 66 s
de malla. Estado: el orquestador ya protegió `eta`, `eta_Euler` y `factor_deslizamiento` con `abs(den) > 1e-9 ? a/b : 0` (diff sin commit en
`bomba.py:646`, `reportes-euler-triangulos.java`, `bomba-reportes.java`). NO verificado con solver todavía. Reproducir: `pump_case(..., params={"iterar": true})`
sobre BombaRapida_v1 antes de la corrección. Riesgo restante: cualquier otra expresión de reporte con denominador nulo en iteración 1 (p. ej. `P_eje` ≈ 0 en `eta`
ya cubierto; `Vu2_inf` con β2 = 0).

### P1

**D2. «Detener» y cerrar el panel pueden matar el solver y la macro.** `kill_tree` (chat_panel.py:130-136) hace `taskkill /T /F` sobre `claude.exe`; el MCP lanza
el servidor y el residente como hijos suyos (mcp_server.py:1218, 823). Verifiqué con procesos de sustitución (`scratchpad/tree.py`, mismas banderas
`CREATE_NEW_PROCESS_GROUP`): tras `taskkill /T` del padre, el «servidor» hijo muere. Efecto: con el flujo normal (`pump_case` arranca el servidor en el mismo turno),
pulsar Detener o cerrar la ventana a mitad pierde el .sim sin guardar y deja `.server.json` apuntando a un pid muerto. Si el servidor ya existía de un turno anterior,
sobrevive y la macro en curso sigue corriendo sin que el panel lo sepa (el candado queda huérfano → «agente trabajando» desaparece mientras el solver está ocupado).
Arreglo: lanzar servidor y residente con `DETACHED_PROCESS|CREATE_BREAKAWAY_FROM_JOB` o un `start`/tarea aparte, y que Detener solo cierre la CLI.

**D3. No hay vigilancia de inactividad en modo panel (PoD cobra por hora).** El watchdog es un hilo del proceso MCP (l. 496-500, `__main__` l. 2477); en el panel cada
turno es un `claude -p` que arranca y mata su MCP. Entre turnos nadie apaga ni avisa: el solver queda cobrando indefinidamente al cerrar el panel. Solo el panel
muestra «N min inactivo» (l. 261) sin actuar. Arreglo: que el panel (o un `idle_watch.py` aparte) haga el chequeo con `.server.json` y avise/apague, o dejar un MCP
residente. Reproducir: abrir el panel, pedir `pump_case`, cerrar la ventana tras acabar: `Get-Process starccm+` sigue 30+ min.

**D4. Panel: «GUI ?» fija y `idle_warning` se pintaría como un dict.** El panel lee `st["gui"]` (chat_panel.py:268) pero ningún código del MCP lo escribe (solo
`server_status` lo devuelve); el panel solo conoce «?». Y `idle_warning` es un dict `{idle_min, gui, at, msg}` (mcp_server.py:472-473) que el panel interpola
tal cual en la barra (l. 567: «⚠ {'idle_min': 31, 'gui': True, …}»); el selftest del panel usa una cadena y no lo ve. Evidencia: captura real «GUI ?».
Arreglo: el MCP escribe `gui` en `.server.json` al comprobar (o el panel cuenta procesos); el panel usa `idle_warning["msg"]`.

**D5. La clave PoD sí puede salir por la herramienta `Read`/`Grep`.** `DEFAULT_TOOLS = "mcp__starccm__*,Read,Glob,Grep"` (chat_panel.py:113) con cwd = repo: el
modelo puede leer `.env` y escribirla en el chat y en el historial de `claude`. Un nombre de cara o un texto del `.cfturbo-ccm` hostil bastaría para inducirlo.
Arreglo: `--disallowedTools "Read(.env)" "Grep(.env)"` o mover `.env` fuera del cwd (`%USERPROFILE%\.starccm-agent.env` ya está soportado). La clave además está en la línea de
comandos de `starccm+` (visible en cualquier listado de procesos) y `.env` está en OneDrive (nube).

**D6. `.inject.lock`, `.server.json`, `agent.log` siguen en OneDrive.** El propio CLAUDE.md dice que OneDrive bloquea archivos mientras sincroniza. `_release` y `_acquire` tragan
`OSError` en `unlink` (mcp_server.py:329-335, 318-321): si OneDrive lo tiene abierto el candado queda con el pid dueño VIVO; solo ese proceso lo limpia (`_lock_orphan` l. 288) y los
demás esperan hasta 2 h (busy). Además cada macro hace `os.utime` y crea/borra el candado → tráfico de sincronización. Arreglo: moverlos a `WORK`; el panel puede importar `_work_dir()`.

**D7. El panel no funciona en una instalación nueva: falta `.mcp.json`.** `build_cmd` (chat_panel.py:125) pasa `--mcp-config <repo>/.mcp.json --strict-mcp-config`; el archivo no existe
(está en `.gitignore`) y `abrir_panel.ps1` no lo crea. Resultado: «la CLI terminó con código 1». El `.example` además lleva rutas absolutas de OneDrive. Arreglo: el panel genera un JSON temporal
con `sys.executable` y `mcp_server.py` relativos al repo, o `abrir_panel.ps1` lo crea.

**D8. Un timeout/colgada libera el turno con el solver aún ocupado.** `_inject_resident` mata el residente y devuelve error (l. 926-940) pero la macro sigue en el servidor; el turno se
libera y la siguiente herramienta se encola detrás sin saberlo (y su `timeout` empieza a contar). `server_stop` en ese estado tarda hasta 1800 s en `_try_save`. Falta una orden de
cancelación del solver (p. ej. `sim.getSimulationIterator().stopRun()` por un segundo cliente) o al menos `busy` mientras `Residente` no responda. Con `iterar:true` toda la corrida es UNA macro
sin forma de cancelarla.

**D9. `iterate` por defecto no puede dar «stable».** `n=100` (mcp_server.py:1685) con `window=100`, `block=50` y primera muestra en it 50: `_judge` exige `hist[0].it <= last.it - 100` (l. 1666) → siempre `stopped: "max"`
tras 100 it. Además la historia (`_iter_hist`) es de proceso y el panel arranca un MCP por turno, así que no se acumula entre llamadas. CLAUDE.md siempre dice `n=1000`; el ESTILO del panel dice `until="stable"` sin `n`.
Arreglo: `n=1000` por defecto (o juzgar con la ventana disponible).

**D10. Verificado falso positivo en `PARED_CONEXION`.** Corrida real 17:37 sobre BombaRapida_v1 (geometría en «Flow domain», la misma que dio balance 0,0001 %): las 6 caras de conexión salen
«100,0 % de la cara» y 6 `AVISO interfaz mal emparejada … exportar de nuevo desde CFturbo`. Las áreas de la pared (3428,77 y 3428,77; 5968,98 y 5968,78 mm²) coinciden con la cara completa y `aInt` salió 0
(el `SumReport` de Area sobre las fronteras `… [int_… 1]` no devuelve área). Con `Q_in -15,7 / Q_out +13,9` en la iteración 1 las interfaces sí funcionan. Esto manda a la IA y al usuario a pedir re-exportar CAD
sin motivo. Arreglo: medir con el área de la parte (`Part` surface) o dejar de avisar si la frontera de interfaz hermana existe (`getBoundaryManager()` contiene `bn + " ["`) y comparar con `Area` de la parte.

### P2

**D11. `pump_case` devuelve `stdout[-3000:]`** (l. 1798): con `iterar`+`post` los `PIPE`, `CELLS`, `AVISO`, `MESH_SECONDS` del principio se pierden entre las líneas de la tabla de iteración del solver. La IA tendría que
abrir el `.out`. Mejor devolver campos estructurados (`pipe_s`, `cells`, `avisos`, `results`, `iter_stop`).

**D12. Sin test de contrato `pump_case` ↔ `bomba.py` real.** Selftest de pump_case usa módulo falso (l. 2021-2030). Añadir: `pump_case(dry_run=True)` con el ccm real + `_wrap` + javac, y comprobar que ninguna línea `import` contiene `'`, `[` o `]`.

**D13. Codificación.** `Residente.java` y `_wrap` escriben UTF-8, pero `_copy_live` decodifica el stdout del cliente como UTF-8 mientras la JVM redirigida usa la página de códigos nativa para `System.out`:
caracteres con acento en `sim.println` (los `AVISO` están en español) pueden salir como «�». El pipeline escapa sus literales (`json.dumps`), las recetas hechas a mano no. Rutas de usuario con acentos: sin probar.

**D14. Costes fijos evitables.** Medido en `tiempos.csv`/`agent.log`: `server_start` 47 s (36,7 s solver + ~10 s lanzar el residente en serie), `server_stop` 7-8 s con guardado de 0,6 s (consulta de GUI con PowerShell ~1,6 s sin caché,
sondeos de 1 s `POLL_S` en l. 1278-1281, `_res_stop(3.0)`). `server_status` también paga la consulta de GUI cuando la caché (10 s) caduca. `Abrir GUI` y `Apagar` gastan un turno de modelo (~10-20 s) para una acción que el panel
puede hacer directo. Prompt por turno: `claude -p` en cwd = repo carga CLAUDE.md (23 KB ≈ 6-7 k tokens) + 15 esquemas de herramientas + ESTILO (571 car., bien).

**D15. UX del panel.** Una corrida con `iterar` dura minutos con una sola línea «▶ Montando la bomba … 3 min»: el panel podría leer el `.out` vivo (ya existe) y mostrar iteración, H y M. «N min inactivo» (mtime de `.server.json`) es engañoso durante una macro larga.
Sesión caducada (`--resume` con id inexistente) falla en cada turno hasta que el usuario pulsa «Nueva». Sin DPI-awareness (ventana borrosa en pantallas escaladas).

**D16. Mantenimiento.** `residente-*.log` por servidor, `macros/.classes` y `.cache` no se podan; `.venv` dentro de OneDrive; `.gitignore` ignora `casos/*/` (los RESUMEN.md de casos no se versionan); aviso Euler espurio antes de iterar
(«radio medio de la entrada (0.0) no es menor…», visto en la corrida real: la validación necesita solución y la macro la hace también tras iterar).

## 3. Notas

| nota | valor | justificación |
|---|---|---|
| DISEÑO | 6,5/10 | Arquitectura limpia y mínima (GUI real + panel de 31 KB, solo stdlib), cola de mensajes, modos y tiempos por turno bien pensados; la captura es legible, sin cortes y sobria. Resta: «GUI ?» fijo, aviso de inactividad que no se renderiza, sin progreso en vivo de la corrida, botones que gastan un turno de modelo, README/ARQUITECTURA desfasados |
| FUNCIONALIDAD | 6,5/10 | 15/15 herramientas y las del panel implementadas y coherentes entre sí; 107+45+35 checks verdes; residente 10× real y fallback probados. Pero la prueba real todavía no completó un `pump_case` (dos corridas, dos fallos de contrato/Euler corregidos sin verificar) y hay un falso positivo en interfaces; `iterate` por defecto no estabiliza |
| ROBUSTEZ | 5,5/10 | Turnos, residente, servidor muerto, timeouts y redacción de la clave están muy bien probados con el falso. Pero Detener/cerrar puede matar el solver, no hay vigilancia sin MCP vivo (coste PoD), `.env` legible por la IA, archivos de coordinación en OneDrive, `.mcp.json` ausente y selftests que no cubren el contrato con el pipeline real (por eso D1 llegó a la prueba con solver) |

9/10 exigiría: D1-D5 y D7 cerrados y verificados con una corrida real completa (importar → resultados → PNG → apagar), selftest de contrato con `bomba.py`, y panel con estado fiable.

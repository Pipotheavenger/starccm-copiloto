# EVAL_2 — evaluación de la ronda 3 (2026-10-26 ~18:15–18:40)

Alcance: código de `mcp_server.py` (3088 l.), `panel/chat_panel.py` (1068 l.), `pipelines/bomba.py`, tests, CLAUDE.md, README, ARQUITECTURA,
`.env.example`, constructores A3/B3/C3, y los artefactos de la prueba real del orquestador (`casos/BombaRapida_e2e_181510/`,
`agent.log`, `tiempos.csv`, solo lectura). No arranqué solver ni `starccm+`; no toqué `.server.json`, `.inject.lock` ni el contenido de
`%LOCALAPPDATA%\starccm-copiloto`. No modifiqué código ni hice commits (el árbol solo tiene `tests/real_e2e.py` modificado, del orquestador).

## 0. Lo que ejecuté

| comando / prueba | resultado |
|---|---|
| `panel/chat_panel.py --selftest` | TODO OK, 57 checks, 8,4 s (escribe solo en temporales; el `panel_estado.json` que toca es de un REPO falso) |
| `pipelines/bomba.py --selftest` | TODO BIEN, 44 checks (javac), ~70 s |
| `mcp_server.py --selftest` | todo en verde, 131 checks, 36,3 s (<45 s); `panel_estado.json` real intacto; limpia su temporal |
| `claude -p` REAL con `--disallowedTools` del panel (carpeta de prueba con `.env` falso) | `Read .env`, `Read sub/.env`, rutas absolutas con `\` y con `/`: DENEGADO. `Grep` no devuelve `.env`. `.env.example` sigue legible. Sintaxis válida en Claude Code 2.1.139 |
| `claude.exe` REAL → Bash → `_spawn_detached` (proceso dormilón), luego `taskkill /PID claude /T /F` (lo que hace Detener) | `breakaway: true`; el desacoplado SOBREVIVE, la CLI muere. Verificado con la CLI real, no solo con falsos |
| Panel abierto y cerrado | Solver apagado en ese momento (la corrida real ya había acabado a las 18:19): cerró directo, sin diálogo, no hubo que responder nada |

Aviso de medición: mis tres selftests corrieron entre las 18:15 y las 18:17, SOLAPADOS con el `pump_case` real (18:15:48–18:18:59) en una CPU de 4 procesos.
Eso contamina los tiempos de esa corrida (ver §3). Lo hice sin querer del todo: la regla no lo prohibía, pero la próxima vez conviene no correr nada en paralelo.

## 1. Verificación de los defectos de EVAL_1

| # | defecto | veredicto | evidencia |
|---|---|---|---|
| D1 | `pump_case` rompía por `IMPORTS` | **RESUELTO** | `_pump_imports` compartida por pump_case y selftest; selftest compila con el javac real la macro de `bomba.py` (rápida e iterar+post) con el `.cfturbo-ccm` real (mcp selftest, últimas líneas); `pump_case` real verde dos veces (174159 y 181510) |
| D1b | división por cero en Euler/eta | **RESUELTO** | `salida_pump.txt` de 181510: 250 it, `H_Euler 18,75`, `eta_Euler 0,892`, sin `Non-finite`; CLAUDE.md lo recoge como lección |
| D2 | Detener mata solver/macro | **RESUELTO** | (a) lanzamiento desacoplado: `_LAUNCHER` mcp_server.py:269-307, prueba con claude.exe real arriba; (b) panel además excluye pids protegidos (`protected_pids` chat_panel.py:173, `kill_tree` :186) y avisa si queda un `.inject.lock` vigente (`stop_warning`). Residual: la macro en curso sigue en el solver (ver D8) |
| D3 | sin vigilancia en modo panel | **RESUELTO** | vigilante aparte (`--vigilante <puerto>`, mcp_server.py:590-676). Real: `agent.log` 18:15:47 «vigilante lanzado… breakaway True», 18:15:48 «vigila el puerto 47827», 18:19:12 «termina: el servidor ya no está». Selftest: con GUI avisa, sin GUI guarda y apaga, uno por puerto. Residual N3: nada avisa si el vigilante muere |
| D4 | «GUI ?» y `idle_warning` como dict | **RESUELTO** | el vigilante escribe `gui`, `idle_min`, `idle_checked`, `idle_warning` (TEXTO) en `.server.json` (real, 18:15:48: `gui:false, idle_min:0.0`); el panel usa `warning_text` (chat_panel.py:352) y dice «GUI conectada / no conectada». La banda ámbar solo la vi en selftest, no en pantalla (no podía fabricar un estado sin tocar el real) |
| D5 | clave legible por `Read`/`Grep` | **RESUELTO** | prueba con CLI real (arriba). Residual (P3): `run_java` ejecuta Java arbitrario, aunque la salida pasa por `_redact`; la clave sigue en la línea de comandos de `starccm+` y `.env` está en OneDrive |
| D6 | candado/estado en OneDrive | **RESUELTO (casi)** | `estado_dir()` = WORK (mcp_server.py:94-100); real: `.server.json`, `vigilante-*.json` están en `%LOCALAPPDATA%`; `_migrate_legacy`. Queda en OneDrive `agent.log` (se escribe en cada evento), `casos/`, `.env`, `.venv`, `panel_estado.json` (solo si cambia) |
| D7 | falta `.mcp.json` | **RESUELTO** | `ensure_mcp_config` (chat_panel.py:121) lo crea con rutas del equipo y avisa en el chat; selftest. No lo ejercité en vivo (no quise crear el archivo en el repo) |
| D8 | timeout deja el solver ocupado sin aviso | **PENDIENTE** | sin `stopRun`/cancelación en el MCP (grep vacío). `iterar:true` sigue siendo UNA macro sin cancelar. El vigilante ahora lo mitiga a medias (con el `.pid` del residente muerto, la macro huérfana ya no cuenta como actividad: a los 30 min sin GUI guardaría y apagaría) |
| D9 | `iterate` no podía dar «stable» | **RESUELTO** | `n=0` → 1000 con `stable`, 100 con `n` (mcp_server.py:2017); selftest `iterate(window=1000)` |
| D10 | falso positivo `PARED_CONEXION` | **RESUELTO (pero la medida ya no mide)** | corrida real 181510: 6 líneas `no medible (la interfaz no devuelve area)`, 0 `AVISO` de interfaz, balance de masa 1,8e-4 %. Como la interfaz nunca devuelve área, el chequeo siempre dirá «no medible»; la protección real es `BALANCE_MASA` (está bien, pero no vendas el chequeo como cobertura) |
| D11 | `stdout[-3000:]` | **RESUELTO** | `pump_case` devuelve `parsed` con RESULT, PIPE, CELLS, MESH_SECONDS, ITER_STOP, YPLUS, PLANOS, BALANCE_MASA, PNG, POST_SECONDS y `salida_pump.txt` (209 KB); visto en `e2e.json` real |
| D12 | sin test de contrato con `bomba.py` real | **RESUELTO** | ver D1 (SKIP si no existe el `.cfturbo-ccm` hermano en `cfturbo-agent`: depende de otra carpeta) |
| D13 | codificación UTF-8/cp1252 | **PENDIENTE (sin evidencia de fallo)** | sin cambios en `_copy_live`; no hay `�` en los `.out` recientes de la corrida real, pero las `AVISO` con acentos del pipeline salieron bien porque `json.dumps` escapa; no probado con receta manual |
| D14 | costes fijos | **PARCIAL** | `server_start` 47 → 37,3 s (residente sin esperar), GUI por PEB ~100 ms (selftest). `server_stop` sigue en 7-9,5 s. «Abrir GUI» y «Apagar solver» siguen gastando un turno de modelo (chat_panel.py:644-648) |
| D15 | UX del panel | **PARCIAL** | «N min inactivo» ya no engaña durante una macro (cuenta como actividad). Siguen: sin progreso en vivo (iteración/H/M del `.out`), sin recuperación de sesión caducada, sin DPI-awareness |
| D16 | mantenimiento | **PARCIAL** | siguen sin podarse `residente-*.log` (5 ya) ni `macros/.classes`; el aviso Euler espurio sigue (ver N1) |

Documentación y conocimiento (los puntos que pedías):

| punto | veredicto | evidencia |
|---|---|---|
| CLAUDE.md ≤ 9 KB | OK | 8861 bytes |
| lecciones no perdidas frente a `CLAUDE_agente_anterior.md` + `lecciones.md` | OK | comparé los identificadores en código de cada viñeta del anterior contra `lecciones.md`+CLAUDE.md: lo no recogido es solo la UI web/API REST/remoto (obsoleto, sigue en el archivo anterior) y detalle de una plantilla de caso (`Cell Relative Velocity`). y+, CAD genérico, estimar celdas, división por cero, `PARED_CONEXION`: presentes |
| README/ARQUITECTURA/.env.example coherentes con el código | PARCIAL | ver N4 |
| `panel_estado.json` coherente entre panel y MCP | OK | MCP lo escribe (`_write_panel_estado`, mcp_server.py:309, al arrancar y en server_start; solo si cambia) con `estado_dir`; el panel lee `estado_dir` (chat_panel.py:330) y cae a `COPILOTO_WORK`/`%LOCALAPPDATA%`; está en `.gitignore`. Contenido real con la ruta correcta |
| nombres `gui` / `idle_min` / `idle_warning` | OK | MCP escribe esos tres (+`idle_checked`); el panel lee esos tres (chat_panel.py:~368-376). `idle_warning` texto o dict con `msg` |
| pids que excluye el panel vs. cómo los registra el MCP | OK | panel protege `.server.json.pid` (el `.bat` lanzado) y `residente.json.pid`/`server_pid` y todo lo que cuelga de ellos; el MCP registra exactamente esos (visto en `residente.json` real: `pid`, `server_pid`, `breakaway:true`). El vigilante y la GUI no se protegen, pero tampoco cuelgan del árbol de la CLI (desacoplados) |
| vigilante uno por puerto | OK | `vigilante-<puerto>.json` con pid + latido; en la corrida real hubo DOS lanzamientos (18:15:47 y 18:15:48) y solo uno vigiló (el segundo salió) |
| lanzamiento desacoplado con CREATE_NO_WINDOW y logs | OK | con el `starccm+.bat` REAL: el log del servidor se llenó (`Server::start`, arranque en 36,6 s) y `sims/*.server.log` existen; el riesgo declarado por A3 queda cerrado |
| `--disallowedTools` en sintaxis válida | OK | probado con la CLI real (arriba) |

## 2. Defectos nuevos y pendientes (por severidad)

**P1: ninguno.** Los P0/P1 de EVAL_1 están cerrados salvo D8, que ahora es P2 (el vigilante lo acota).

**P2**

- **N1. Aviso Euler espurio en CADA corrida con `iterar`, y CLAUDE.md manda desconfiar de él.** `salida_pump.txt:2159-2161`:
  `AVISO Euler: … radio medio de la entrada (0.0) no es menor…` y `EULER_VALIDAR sin solución (iteración 0)`, porque la validación corre antes de iterar y el
  `post` no la repite (no hay `EULER_OK`/`EULER_MAL` después). `parsed["AVISO"]` y `parsed["EULER"]` lo traen tal cual, mientras CLAUDE.md dice «`AVISO`/`EULER_MAL`: no confiar en `eta_Euler`»
  aunque el resultado final es bueno (H_Euler 18,75 > H 16,74). Pasos: en `pipelines/bomba.py`, no emitir la validación si `iterar` (o repetirla en `post`) y que `_parse_pump` descarte `EULER_VALIDAR`. (Es D16 + una contradicción nueva con las reglas.)
- **N2. Apagar el solver desde el panel depende de un turno del modelo.** «Apagar solver», «Sí» al cerrar y el aviso tras Detener mandan `MSG_APAGAR` = «Guarda y apaga el solver con server_stop» (chat_panel.py:33,644-655,703). El prompt de sistema y CLAUDE.md piden *preguntar antes de apagar*; si el modelo pregunta, el cierre espera 60 s y se rinde («No cerré el panel»). Sin CLI/sesión/crédito no hay forma directa de dejar de pagar PoD (solo el vigilante a los 30 min). Pasos: botón que llame `python mcp_server.py --stop` (server_stop sin LLM), o que `MSG_APAGAR` diga «sin preguntar; el usuario ya confirmó».
- **N3. Si el vigilante muere, el panel miente.** `read_status` toma `idle_min`/`gui` de `.server.json` sin mirar `idle_checked` (chat_panel.py:~368): un vigilante caído deja «0 min inactivo · GUI no conectada» para siempre mientras el solver cobra, y nadie lo relanza hasta la próxima tool. Además `_vigilante_pid` exige latido < 120 s pero el vigilante no late durante un `server_stop` largo (hasta 1800 s) → un `server_start`/`start_watchdog` simultáneo lanzaría un segundo. Pasos: marcar en la barra «sin vigilante» si `idle_checked` > 2 min y mostrar el aviso; latir en un hilo.
- **D8 (sigue).** Una macro colgada sigue en el solver tras un timeout o Detener, sin cancelación. Pasos: `sim.getSimulationIterator().stopRun()` desde un segundo cliente al hacer timeout o «Detener»; mientras, que `server_stop` avise.
- **Verificación pendiente de extremo a extremo:** la prueba real se lanzó con Python directo (`tests/real_e2e.py`), no con panel → `claude -p` → MCP → solver. Nunca se ha visto con el solver real: `ensure_mcp_config` + `--disallowedTools` junto a `mcp__starccm__*`, Detener a mitad de `pump_case`, cierre con «Sí». Es la única forma de pasar de 8 a 9 en robustez.

**P3**

- **N4. Documentación desfasada de la ronda 3.** Ni README, ARQUITECTURA, `.env.example` ni lecciones.md mencionan: `_spawn_detached`/breakaway, la lección «con `DETACHED_PROCESS` un `.bat` pierde la salida redirigida» y «`taskkill /T` mata hijos desacoplados mientras el padre vive» (se quedarían sin aprender), `panel_estado.json`, `vigilante-<puerto>.json`, `COPILOTO_VIGILANTE_S`, `STARCCM_VIGILANTE`, `COPILOTO_VIG_GONE_S`, `COPILOTO_DETACHED`, `COPILOTO_AGENT_LOG`. README paso 3 dice que sin `.mcp.json` el panel no funciona (ya lo crea) y da tiempos viejos (panel selftest «~3 s», real 8,4 s). ARQUITECTURA §3 cita el texto «¿apagar?» (el real es «…GUI conectada: no se apaga») y §4 dice que el vigilante mira `.server.json` y candados (mira macros en disco + mtime). «Medido» solo recoge 174159.
- **N5. Pids que se registran/loguean no coinciden.** `_spawn_detached` devuelve el pid del `python.exe` del venv (redirector); el log dice «vigilante lanzado pid 25436» y «vigilante 25228 vigila» (el real). `tests/real_e2e.py` lee `vigilante_pid`/`residente_pid` de `.server.json`, claves que no existen (`e2e.json` real: `null`), así que esa captura del estado del panel no prueba nada. Pasos: usar las claves `vigilante` (no se escribe en el estado: añadirla) o leer `vigilante-<puerto>.json`/`residente.json`.
- **N6. Tiempos de la corrida 181510 no sirven para comparar** (239 s frente a 194 s; `pump_case` 192 s frente a 143 s; sobrecarga fuera del pipeline 21,8 s frente a 1,3 s): coincide con mis selftests en paralelo. Pero el diseño «residente sin esperar» solo traslada ~10 s de `server_start` a la primera inyección; la ganancia neta no está medida limpia. Repetir `real_e2e.py` sin nada más corriendo.
- **N7. `.mcp.json` existente pero obsoleto** (rutas de otra máquina, copiado del `.example` según README) no se repara y da «la CLI terminó con código 1» sin pista. `ensure_mcp_config` solo crea.
- **N8. Estado de la GUI con hasta 30 s de retraso** (el vigilante la publica cada 30 s): tras «Abrir GUI» la barra dice «no conectada» un rato; y con `STARCCM_VIGILANTE=0` nunca se actualiza.
- **D13, D15, D16** como arriba.

## 3. Captura del panel

`shot.ps1` capturó otra región de pantalla (la pantalla es 1280x800 escalada y `CopyFromScreen` salió desplazado): borré ese PNG sin describirlo. Usé `PrintWindow` sobre la ventana
del panel (solo su ventana): `C:\Users\sebas\AppData\Local\Temp\claude\C--Users-sebas-OneDrive-Documentos-starccm-agent\1f931934-d6c6-4959-840c-fa9a7ae547c3\scratchpad\e2_panel1.png`
(436x679, nítida, sin DPI borroso). Estado: «Solver apagado | GUI no conectada», casilla «Encima», selector Rápido/Preciso con «sonnet · esfuerzo low»,
historial vacío «Listo. Escribe lo que necesitas.», botones Enviar (azul)/Detener/Nueva/Abrir GUI/Apagar solver y caja de entrada. Limpio, sobrio y legible; sin cortes.
Le faltan: el progreso en vivo de una corrida larga (iteración, H, M) y distinguir «Apagar solver» (acción cara) con color en reposo; la banda ámbar de aviso y el diálogo de cierre no los pude ver en pantalla (solo selftest) porque no había solver.

## 4. Notas

| nota | valor | justificación |
|---|---|---|
| **DISEÑO** | **7,5/10** | La ronda 3 cerró lo que hacía mentir a la interfaz: estado fuera de OneDrive con puntero en `panel_estado.json`, lanzador desacoplado con una sola explicación (y la lección medida del `.bat`), vigilante propio por puerto, reglas de denegación probadas, `parsed` estructurado, CLAUDE.md 23→8,9 KB sin perder lecciones. Panel sobrio y nítido. Resta: acciones críticas (apagar) pasan por el modelo, sin progreso en vivo, el chequeo `PARED_CONEXION` quedó decorativo, documentación sin las piezas nuevas |
| **FUNCIONALIDAD** | **8,5/10** | `pump_case` real verde dos veces (3 min 14 s limpio; 3 min 59 s con mis selftests encima), 9 resultados, 3 PNG, y+ 3,9-49, balance 1,8e-4 %, parada estable en 250 it, guardado y apagado limpios; 131+57+44 checks verdes; detach verificado con la CLI real. Resta: aviso Euler engañoso en cada corrida, sin cancelación de macro (D8), flujo panel→CLI→solver sin probar en vivo |
| **ROBUSTEZ** | **7,5/10** | Subió desde 5,5: Detener ya no alcanza al solver (probado con claude.exe real), vigilante independiente que apaga/avisa y se vio en la corrida real, clave inaccesible para `Read`/`Grep` (probado), `.mcp.json` autocreado, estado fuera de OneDrive. Falta para el 9: apagado directo sin LLM (N2), que el panel sepa si el vigilante murió (N3), cancelar macros (D8), y UNA prueba real de punta a punta por el panel (Detener a mitad, cerrar con «Sí») |

9/10 exigiría: N1 y N2 cerrados, N3 con aviso visible, D8 con `stopRun`, la prueba real por el panel, `real_e2e` repetido sin carga paralela y la documentación al día con la ronda 3.

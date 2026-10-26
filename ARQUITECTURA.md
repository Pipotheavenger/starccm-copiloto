# starccm-copiloto — diseño

Sucesor de `starccm-agent/starccm`. Idea de Sebastián (2026-10-26): **no imitar STAR-CCM+ en una web**. El usuario
trabaja con la **GUI real de STAR-CCM+** conectada al mismo servidor que el agente, y le habla al agente desde un
**panel flotante de chat**. Ve los cambios aparecer en el árbol y en la salida como si alguien operara la GUI.

Verificado el 2026-10-26 con 2410 y el solver real (`BombaPruebaNR`, 214 k celdas, np 4):

| hecho | resultado |
|---|---|
| `starccm+.bat -host localhost:47827` conecta la GUI al servidor del agente | sí, sin `-podkey` |
| ¿reserva otra licencia? | no: solo el checkout `ccmppower` del servidor |
| árbol de la GUI | se actualiza solo con lo que crea el agente |
| panel Output de la GUI | muestra en vivo la salida de las macros del agente (iteraciones, avisos) |
| velocidad de iteración con la GUI conectada | igual: 0,5 s/iteración |
| `scene.open()` en una macro | abre la escena en el cliente batch, NO en la GUI: el usuario la abre del árbol |

## Piezas

```
Claude Code (o el panel) ──MCP stdio──> mcp_server.py ──inyecta──> starccm+ -server (UN servidor, .sim en memoria)
                                              │                          ▲
panel/chat_panel.py (tkinter, siempre encima) │                          │ cliente GUI (starccm+ -host …): el usuario mira
   └─ lanza `claude -p … --resume` con las    │
      tools mcp__starccm__* y muestra eventos ├── conocimiento/   recetas Java verificadas, lecciones, casos, memoria
                                              ├── pipelines/      guiones fijos (bomba estándar en UNA macro)
                                              ├── casos/<caso>/   cad, reportes, imágenes, RESUMEN.md
                                              └── COPILOTO_WORK/  sims, macros, residente, tiempos.csv, .server.json, .inject.lock
```

### 1. `mcp_server.py` (un archivo, FastMCP; base: `conocimiento/mcp_server_anterior.py`)

Herramientas (15): `server_start`, `server_stop`, `server_status`, `gui_attach`, `gui_status`, `inspect`, `run_java`,
`run_steps`, `iterate`, `pump_case`, `export_scene`, `save`, `recipe_search`, `recipe_save`, `docs_search`. Heredadas: `server_start`, `server_stop`, `server_status`, `run_java`, `inspect`, `iterate`,
`export_scene`, `save`, `recipe_search`, `recipe_save`, `docs_search`. Cambios obligatorios:

1. **Turnos entre procesos**: candado de archivo `.inject.lock` (pid, herramienta, inicio). Toda inyección lo toma; si
   otro proceso vivo lo tiene, espera hasta `timeout` y si no, devuelve `{"ok": false, "busy": "…"}`. Candado
   huérfano (pid muerto o > 2 h) se limpia. El panel lo lee para decir «el agente está trabajando».
2. **GUI conectada**: `gui_attach()` lanza el cliente GUI (`-host localhost:PORT`, proceso separado, sin licencia) y
   `gui_status()` dice si hay uno conectado (proceso `starccm+.exe` con `-host localhost:PORT` y sin `-batch`).
   Nunca mata la GUI; `server_stop` avisa si hay GUI conectada (el usuario debe elegir «desconectar»).
3. **Vigilante de inactividad**: con GUI conectada NO apaga el servidor; deja un aviso en `.server.json`
   (`idle_warning`) que el panel muestra («solver encendido N min sin actividad: ¿apagar?»). Sin GUI: como antes
   (`STARCCM_IDLE_MIN`, por defecto 30): guarda y apaga.
4. **Macros agrupadas**: `run_steps(steps)` — lista de `{"recipe": nombre, "params": {...}}` o `{"java": cuerpo}` que se
   concatenan en UNA macro (cada paso en su try/catch con `STEP_BEGIN/STEP_END <i>`), resultado por paso. Ahorra los
   8–10 s de arranque del cliente Java por paso.
5. **Iterar con criterio de parada propio**: `iterate(n, until="stable")` corre por bloques (p.ej. 50) y para cuando H y
   M (o los reportes indicados) varían < 0,5 % en 100 iteraciones; marca `plateau` si los residuales no bajan en 300
   iteraciones aunque los reportes estén estables. Devuelve el motivo de parada y la historia corta.
6. **Pipeline de bomba**: `pump_case(case, cad_dir, ...)` usa `pipelines/bomba.py` para generar UNA macro: importar los
   STEP de CFturbo + `.cfturbo-ccm` (rpm, caudal, eje, nombres), regiones, interfaces, malla rápida o normal, física,
   MRF, fronteras (presión total entrada + caudal en salida), reportes (H, M, η, Euler correcto). Referencia medida:
   BombaRapida de importar a resultados en 3 min 45 s (`conocimiento/casos/BombaRapida`).
7. Seguridad: la clave PoD solo en `.env`; `_redact` en todo log/salida. Trabajar sobre copias en `sims/`.

### 2. `panel/chat_panel.py` (tkinter, solo biblioteca estándar)

Ventana pequeña **siempre encima** (`-topmost`), redimensionable, tema claro, que se arranca con
`python panel/chat_panel.py`. Contiene:
- historial del chat (texto con formato mínimo: negrita, tablas en monoespaciado) y caja de entrada (Enter envía,
  Shift+Enter salto);
- barra de estado: solver vivo/apagado, caso, minutos de inactividad, GUI conectada, «agente trabajando» (candado);
- botones: **Abrir GUI** (`gui_attach`), **Detener** (mata la CLI y su árbol), **Apagar solver** (con confirmación
  dentro del panel), **Nueva conversación**;
- cada herramienta del agente se ve como una línea «▶ Importando la geometría … 12 s ✓».
El chat lanza `claude -p <msg> --output-format stream-json --verbose --resume <sid>` con `--mcp-config` del repo,
`--allowedTools "mcp__starccm__*,Read,Glob,Grep"`, entorno sin variables `CLAUDE*`, y un `--append-system-prompt`
que fija el estilo: frases cortas, decir qué mirar en la GUI («abre Scenes › presion_alabes»), regla de turnos.
Corre en un hilo; la ventana nunca se congela. `--selftest` con `tests/fake_claude.py` y sin pantalla.

### 3. Conocimiento (`conocimiento/`) y `CLAUDE.md`

Todo lo aprendido por el agente anterior se conserva: recetas (35), lecciones, casos de referencia y memoria.
`CLAUDE.md` del repo nuevo = reglas operativas condensadas + flujo de bomba + lecciones vigentes (las del remoto
retirado y de la UI web antigua quedan solo en `conocimiento/CLAUDE_agente_anterior.md`).

### 4. Carpeta de trabajo, cliente residente y vigilante

- **`COPILOTO_WORK`** (por defecto `%LOCALAPPDATA%\starccm-copiloto`, fuera del repo y de OneDrive): `sims/` (copias de trabajo y
  logs del solver), `macros/` (cada macro inyectada con su salida `.out`), `residente/` (inbox, salida, latido y log del cliente
  residente), `tiempos.csv` (fecha, herramienta, ms, `via`, ok, inyecciones), `.server.json` (estado: pid, puerto, caso,
  `idle_warning`) y `.inject.lock` (turno). Motivo: OneDrive sincronizaría cada `.sim` de 50-150 MB y bloquea archivos
  mientras escribe. En el repo solo cosas pequeñas: `casos/`, recetas, conocimiento, `.env` y `.venv`.
- **Cliente residente** (`STARCCM_RESIDENT=1`, defecto; `poc_residente/RESULTADO.md`): un solo cliente batch `-noexit`
  (`Residente.java`) vigila `residente/inbox` y ejecuta cada macro con `StarScript`: 0,7-1 s por macro frente a 8,6 s del
  cliente batch (~10x). No precompila: el error de compilación vuelve en ~0,6 s con el texto de javac. Se relanza si muere; tras
  2 intentos fallidos cae al cliente batch (con `javac` previo, `STARCCM_PRECOMPILE`). Cada respuesta trae `via` y `ms`.
- **Lanzamiento desacoplado** (`_spawn_detached`): servidor, residente, vigilante y GUI se lanzan con un proceso intermedio
  (`python -I -c _LAUNCHER`) que arranca el destino con `CREATE_NEW_PROCESS_GROUP|CREATE_BREAKAWAY_FROM_JOB` y `CREATE_NO_WINDOW`
  y muere: el destino queda sin padre, fuera del árbol de la CLI. Motivo: `taskkill /T` (Detener del panel) alcanza a los hijos
  desacoplados mientras el padre viva (probado con `claude.exe` real: el desacoplado sobrevive). `CREATE_NO_WINDOW` y no
  `DETACHED_PROCESS` porque con éste un `.bat` pierde la salida redirigida de sus hijos de consola. Si el job no permite
  breakaway reintenta sin él («doble lanzamiento»). `COPILOTO_DETACHED=1` invierte el modo (peor; solo diagnóstico).
- **`panel_estado.json`** (raíz del repo, ignorado por git; lo escribe el MCP al arrancar y en `server_start`, solo si cambia):
  `estado_dir`, `state_file`, `lock_file`, `work`. El panel lo lee para saber dónde están `.server.json` e `.inject.lock`
  (si falta: `COPILOTO_WORK`, luego `%LOCALAPPDATA%\starccm-copiloto`). `_migrate_legacy` mueve el estado viejo del repo.
- **Vigilante de inactividad independiente, por puerto**: `python mcp_server.py --vigilante <puerto>` (no importa `mcp`, arranca en
  ~0,2 s), lanzado por `server_start`, por el arranque del MCP y al reutilizar un servidor vivo. Cada `COPILOTO_VIGILANTE_S` (30)
  mide la inactividad en disco (macros en marcha o recientes, mtime de `.server.json`) y la GUI, y escribe en `.server.json`
  `gui`, `idle_min`, `idle_checked` e `idle_warning` (TEXTO). Uno solo por puerto (`vigilante-<puerto>.json`: pid + latido);
  termina cuando el servidor lleva `COPILOTO_VIG_GONE_S` (3 s) muerto. Sin GUI guarda y apaga a los `STARCCM_IDLE_MIN` (30; PoD
  cobra por hora); con GUI solo avisa. `STARCCM_VIGILANTE=0` lo desactiva. Residuales: si el vigilante muere, nadie avisa (el
  panel debe mirar `idle_checked`); el estado de la GUI llega con hasta 30 s de retraso.
- **Ronda 4 (en curso, A4/B4; no documentado como hecho hasta verlo en el código)**: `mcp_server.py --cli` (tools sin LLM, p. ej.
  apagar el solver directo), `progreso.json` con iteración/H/M en vivo durante `iterar`, cancelación con un archivo `ABORT`
  que el bucle de iterar revisa cada bloque de 50, y en el panel una tarjeta de progreso y acciones directas (apagar, abrir
  GUI) que no gastan un turno del modelo.
- **Panel**: modo Rápido (sonnet, esfuerzo bajo) / Preciso (opus), cola de mensajes mientras el agente trabaja, cronómetro por
  herramienta, línea de tiempos por turno, Abrir GUI, Detener (la CLI y su árbol, nunca el solver, el residente, el vigilante ni
  la GUI: `protected_pids`), Apagar con confirmación. Al cerrar con el solver vivo pregunta «¿Apagar el solver antes de salir?».
  Prohíbe a la CLI leer `.env` (`--disallowedTools` Read/Grep/Glob) y crea `.mcp.json` si falta (`ensure_mcp_config`).

## Medido

Prueba real de punta a punta (`tests/real_e2e.py`, BombaRapida_v1 de CFturbo, modo rápido, `iterar` + `post`, 4 procesos,
2026-10-26; `casos/BombaRapida_e2e_174159/e2e.json`):

| fase | segundos |
|---|---|
| `server_start` (licencia, MPI) | 44,2 |
| `pump_case` | 142,8 |
| · importar, regiones, interfaces, malla, física, fronteras, reportes (resto del pipeline) | 37,5 |
| · iterar (250 it, parada por estabilidad) | 100,8 |
| · post (planos, 3 PNG, y+, Euler) | 3,2 |
| `server_stop` (guarda) | 7,3 |
| **total** | **194,3 (3 min 14 s)** |

Todo por la vía residente (`via=residente`). Resultados: H 16,74 m, M 8,454 N·m, η 88,6 %, H_Euler 18,75 m, η_Euler 0,892,
balance de masa 0,0002 %, Q_in −13,864 / Q_out 13,864, estable en 250 iteraciones, 3 PNG. Referencia anterior: 3 min 45 s con 300 it
y 45 min con una corrida improvisada. Las divisiones entre reportes van protegidas `(abs(den) > 1e-9 ? num/den : 0)`: sin eso
`eta_Euler` aborta en la iteración 1 (`H_Euler = 0`).

### Segunda corrida real (`casos/BombaRapida_e2e_181510`, 2026-10-26)

Mismo caso y parámetros, `salida_pump.txt` (209 KB) con el desglose `PIPE <fase> <s>` del propio pipeline:

| fase PIPE | segundos |
|---|---|
| importar | 7,97 |
| regiones | 0,25 |
| fisica | 1,29 |
| mrf | 0,19 |
| fronteras | 0,17 |
| reportes | 0,98 |
| malla | 50,17 |
| euler | 0,96 |
| iterar (250 it, parada por estabilidad) | 105,38 |
| post | 3,09 |
| **total pipeline** | **170,45** |

Mallado: CELLS 175 316. Parada `stable` en 250 iteraciones (variación máx. 0,062 %). y+ en álabes: mín 3,91, **medio 24,5**, máx 49,3
(parte del rango cae en 5–30: mirar con cuidado η y par). Balance de masa 1,8e-4 %; 6 líneas `PARED_CONEXION` «no medible»,
0 `AVISO` de interfaz. H_Euler 18,75 m, η_Euler 0,892. **Los tiempos están contaminados**: corrieron tres selftests en paralelo
(CPU de 4 procesos), así que el total de `pump_case` (192 s) y de la corrida (239 s) no son comparables con los 143 s / 194 s de la
primera; repetir `tests/real_e2e.py` sin nada más corriendo. Esa corrida mostró además el aviso Euler espurio («radio medio…
0.0», `EULER_VALIDAR sin solución`) y la ausencia de `EULER_OK`: corregido después en `pipelines/bomba.py` (la validación va tras
iterar y `euler_check_body` ya no sale vacío); pendiente de verlo con solver.

## Reglas de diseño

- Lo mínimo que funcione, sin frameworks. Python 3.14, `mcp`; el panel solo con biblioteca estándar.
- Todo lo no trivial con check ejecutable: `python mcp_server.py --selftest` y `python panel/chat_panel.py --selftest`
  (sin licencia ni solver ni CLI real: `tests/fake_starccm.py`, `tests/fake_claude.py`).
- Español, mensajes cortos, números en tablas. Marcar atajos con `# ponytail:` y su límite.
- Un solo solver vivo a la vez (licencia de una copia). Las pruebas con solver real las coordina el orquestador.

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
                                              └── sims/, macros/  de trabajo (ignorados por git)
```

### 1. `mcp_server.py` (un archivo, FastMCP; base: `conocimiento/mcp_server_anterior.py`)

Herramientas heredadas: `server_start`, `server_stop`, `server_status`, `run_java`, `inspect`, `iterate`,
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

Todo lo aprendido por el agente anterior se conserva: recetas (34), lecciones, casos de referencia y memoria.
`CLAUDE.md` del repo nuevo = reglas operativas condensadas + flujo de bomba + lecciones vigentes (las del remoto
retirado y de la UI web antigua quedan solo en `conocimiento/CLAUDE_agente_anterior.md`).

## Reglas de diseño

- Lo mínimo que funcione, sin frameworks. Python 3.14, `mcp`; el panel solo con biblioteca estándar.
- Todo lo no trivial con check ejecutable: `python mcp_server.py --selftest` y `python panel/chat_panel.py --selftest`
  (sin licencia ni solver ni CLI real: `tests/fake_starccm.py`, `tests/fake_claude.py`).
- Español, mensajes cortos, números en tablas. Marcar atajos con `# ponytail:` y su límite.
- Un solo solver vivo a la vez (licencia de una copia). Las pruebas con solver real las coordina el orquestador.

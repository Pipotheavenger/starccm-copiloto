# Constructor A — `mcp_server.py`

Entregado: `mcp_server.py` (raíz, un archivo, FastMCP/MCPServer por stdio) a partir de `conocimiento/mcp_server_anterior.py`,
más `tests/fake_starccm.py` ampliado. No se tocó panel/, pipelines/, conocimiento/, CLAUDE.md ni README.md. Sin commits.

## Herramientas (15)

Heredadas, con el mismo comportamiento: `server_start`, `server_stop`, `server_status`, `run_java` (precompilación javac),
`inspect`, `iterate`, `export_scene`, `save`, `recipe_search`, `recipe_save`, `docs_search`. Rutas nuevas: `casos/`,
`conocimiento/recetas`, `sims/`, `macros/`; `.env` en la raíz del repo o `%USERPROFILE%\.starccm-agent.env`.
Nuevas: `gui_attach`, `gui_status`, `run_steps`, `pump_case`.

| cambio de ARQUITECTURA §1 | cómo quedó |
|---|---|
| 1 Turnos | `.inject.lock` (JSON pid/tool/start/t), `O_EXCL`. Re-entrante en el proceso. Huérfano = pid muerto, o sin latido (mtime) en 2 h, o pid propio sin uso. Sin turno tras `wait_s` (`run_java(wait_s)`; default `STARCCM_LOCK_WAIT_S`=120): `{"ok": false, "busy": "...", "holder": {...}}`. `lock_holder()` (para el panel) y `server_status()["turno"]` dicen quién lo tiene. `server_stop`, `save`, `iterate`, `run_steps`, `pump_case` toman el turno de punta a punta |
| 2 GUI | `gui_status()` = `starccm` + `-host …:PORT` y sin `-batch`/`-server` (PowerShell `Get-CimInstance`, ~1,6 s; caché `GUI_TTL`=10 s). `gui_attach(wait_s)` lanza `starccm+ -host localhost:PORT` desacoplado, sin `-podkey`. `server_stop` devuelve `gui_warning` pero apaga |
| 3 Vigilante | Con GUI: no apaga, escribe `idle_warning` {idle_min, gui, at, msg} en `.server.json` restaurando el mtime (no cuenta como actividad); lo borra si hay actividad. Sin GUI: guarda y apaga como antes |
| 4 `run_steps(steps, stop_on_error=True)` | `{"recipe", "params"}` o `{"java", "name"}`; UNA macro, cada paso en su bloque + try/catch con `STEP_BEGIN/OK/ERROR/END/SKIPPED i`. Resultado por paso: ok, stdout, error, trace, seconds, skipped |
| 5 `iterate(n, until="stable", reports=["H","M"], window=100, tol=0.005, block=50)` | Bloques; para si cada reporte varía < tol en `window` it. `stopped`: `stable`, `plateau` (estable pero el mayor residual no bajó 10 % en `plateau_its`=300) o `max`; `until="n"` = un bloque. Devuelve `history` corta, `variation_pct`, última fila de reportes y residuales |
| 6 `pump_case(case, cad_dir, mode, ccm, params, dry_run)` | Importa `pipelines/bomba.py` en cada llamada (`parse_ccm`, `macro_body`; `IMPORTS` opcional), arranca el servidor con el nombre del caso si no hay, inyecta UNA macro y parsea `STEP_*` si el pipeline los imprime (un paso cerrado sin `STEP_ERROR` cuenta ok) |

Parámetros de recetas: se sustituye `${nombre}` SOLO para las claves de `params` (las recetas ya usan `${Flujo}`,
`${HReport}`… de las expresiones de STAR-CCM+; esas quedan intactas). Una receta puede exigir parámetros con
`// PARAMS: a, b` en la cabecera; parámetros sin uso salen como `warnings`.

Clave PoD: toda tool pasa por `_scrub` (redacta cadenas anidadas), además de `_redact` en `.out`, logs, `.server.json`.
La GUI nunca lleva `-podkey`.

## Cómo probarlo

```
.venv\Scripts\python.exe mcp_server.py --selftest      # ~23-25 s, 100 comprobaciones, sin licencia ni solver
```

Cubre: candado entre dos procesos (uno externo toma el turno, busy, espera y corre, huérfano por pid muerto, por 2 h, propio),
`gui_status` con una GUI falsa real (proceso `fake_starccm.py -host`, detectada por PowerShell) y filas sintéticas,
vigilante con y sin GUI, `run_steps` (paso que falla / siguiente / skipped / PARAMS), `iterate` stable / plateau / max / `until="n"`,
`pump_case` con bomba falsa (módulo temporal), redacción de la clave. Todo se redirige a un temporal (sims, macros,
casos, recetas, `.server.json`, `.inject.lock`, `agent.log`); comprueba que lo real queda intacto. Se ejecutó 3 veces en verde.
Verificado además por stdio (`mcp.client`): las 15 tools se listan con su esquema y `gui_status` ve la GUI real conectada al 47827.

`tests/fake_starccm.py`: GUI falsa (`-host` sin `-batch`), marcadores de `run_steps`, `iterate` con escenas
(`$FAKE_SCEN_FILE`: stable | plateau | drift) que exporta el CSV de residuales; y `localhost` → `127.0.0.1` en el cliente
(en Windows `localhost` probaba `::1` y cada macro tardaba 2 s).

## No verificado sin solver real

- `gui_attach` contra `starccm+.bat`/`starccm+.exe` real (flags `DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP` con el `.bat`;
  si la GUI no abre, probar solo `CREATE_NEW_PROCESS_GROUP`). La detección sí se vio sobre una GUI real.
- `iterate`: la lectura de `Report.getReportMonitorValue()` y del CSV del plot «Residuals» es la del agente anterior; los
  reportes de la bomba deben llamarse `H` y `M` (o pasar `reports=`). El criterio usa muestras de fin de bloque.
- `pump_case` solo con bomba falsa: el contrato real depende de `pipelines/bomba.py` (otro constructor). Un único método Java
  tiene el tope de 64 KB de bytecode (javac lo dirá antes de inyectar); `run_steps` no dice qué paso rompió javac.
- Vigilante con GUI real y el panel leyendo `idle_warning`; efecto de `sim.kill()` con la GUI conectada.
- Ponytails: candado huérfano por carrera entre dos limpiadores; macro de >2 h con el dueño vivo se limpiaría (hay latido
  por inyección); `gui_status` tarda ~1,6 s por consulta (caché 10 s).

# EVAL_5 — evaluación independiente, ronda 5 (2026-10-26, tras el commit f584c87)

Alcance: CLAUDE.md, ARQUITECTURA.md, README.md, EVAL_4.md, `git log`/`git show --stat` de f5efe9a..f584c87, código de
`mcp_server.py` (3420 l.), `panel/chat_panel.py` (1981), `pipelines/bomba.py` (1103), `tests/real_panel_e2e.py`, y los artefactos
reales `casos/BombaRapida_e2e_193651`, `..._e2e_203656`, `..._panel_204007`, `..._panel_204318` y `conocimiento/lecciones.md`.
No arranqué solver ni toqué licencia; no leí `.env`; solo creé este archivo.

## 0. Lo ejecutado (solver falso, en serie)

| comando | resultado |
|---|---|
| `mcp_server.py --selftest` | todo en verde, **37,1 s** medidos por el propio test (límite 45 s), 39 s de pared; incluye «README lista las 16 tools del código» |
| `pipelines/bomba.py --selftest` | TODO BIEN, 38 s (compila la macro real con javac) |
| `panel/chat_panel.py --selftest` | TODO OK, 22,2 s |

## 1. Evidencia real leída (no repetida)

| prueba | dato | lectura |
|---|---|---|
| e2e 193651 (19:36) | 162,5 s; `pump_case` 119,4 s; H 16,74 m, eta 88,6 %, H_Euler 18,75 m, balance 1,8e-4 %, y+ álabes media 24,45 | corrida completa con post. **Es anterior a los cambios de la ronda 6** (f5efe9a, 20:36) |
| e2e 203656 (20:36) | `--cancel` con dueño vivo: `cancel` 14,5 s en la it. 101, la macro para en el bloque (it. 150), `pump_case` vuelve `cancelled` a los 21,5 s con `resultados` de 9 claves (H 16,74, eta 0,886, H_Euler 18,76), ABORT/`cancel.json` borrados, `iterate` de 20 it posterior OK, `server_stop` 7,0 s | cierra el hueco C de EVAL_4 con solver |
| panel 204007 | `Panel` real (Tk, botón «Enviar», botón «Detener» real, `claude -p`), Detener tras la it. 100: macro huérfana cancelada en 14,2 s (para en la it. 104), servidor vivo, vigilante vivo, apagado con el botón real 7,4 s | cierra B |
| panel 204318 | Detener en `mallar`: cancel 25,7 s, la malla termina y la macro sale sin iterar ni post (`fases_s` sin `malla`/`iterar`), servidor vivo, apagado 4,7 s | cierra A en el caso de malla |

Lectura crítica:
- El JSON de 204007 todavía contiene el aviso contradictorio «Detuve la CLI y el MCP, pero el solver sigue vivo…» seguido de «paró
  en la iteración 104»; el arreglo (`check_after_stop` sale si `auto_cancel`, chat_panel.py:1008) llegó en cdc9ecf **después** de esa
  corrida y 204318 ya no lo muestra, pero la cadena «paró antes de iterar» (chat_panel.py:500) solo tiene selftest: 204318 imprimió
  «paró en la iteración None».
- La malla de 204318 dura 26 s < `wait_s` 45 s: el camino `pendiente` (vigilante cierra el progreso, panel espera) no se ejerció real.
- ETA (203656, muestras cada ~5 s): it. 44 → 33,0 s; it. 60 → **135,9 s**; it. 78 → 88,9; it. 90 → 74,6; it. 108 → 26,5. Sigue sin ser monótona.
  La corrida completa 193651 es anterior al cambio de fórmula, así que no hay ETA nueva contra un fin real.
- 193651: y+ media 24,45 (zona 5-30 que CLAUDE.md llama «no fiarse de eta») con `avisos` solo de BALANCE_MASA/EULER_OK: ningún aviso de y+.

## 2. Estado de los defectos A–J de EVAL_4 (verificado en código)

| id | estado | verificación |
|---|---|---|
| A cancel solo en fase `iterar` | **RESUELTO** (residual en el panel, ver D1) | `cancel()` decide `huerfana = not vivo and p and not terminado and macro_running() and _turn_owner_dead(p)` (mcp_server.py:2536 en la versión leída, `cancel` en :2511), sin mirar la fase; ABORT en cualquier fase; `parara_en_s` por `fase_falta_s`; selftest_mcp.py:963-996 (malla con y sin macro); real 204318 |
| B «Detener» real del panel sin ejercer | **RESUELTO** | `real_panel_e2e.py` instancia `cp.Panel` en Tk, `btn["enviar"].invoke()`, `btn["detener"].invoke()` (`Panel.on_stop`), `btn["apagar"]` ×2; evidencia 204007/204318 |
| C parciales al cancelar sin ver con solver | **RESUELTO** | 203656 `resultados` con 9 claves y `ok:false, cancelled:true`. Residual: bomba.py:326 sigue con `catch (Exception pxE) { }` mudo (preferencia) |
| D ETA que salta | **PARCIAL** | fórmula nueva contra la ventana de estabilidad (`_its_hasta_estable`, mcp_server.py ~1966) + tope ×2 por actualización (:2011); 203656 sigue 33 → 135,9 → 88,9 s: ver D2 |
| E documentación desfasada | **RESUELTO** | ARQUITECTURA.md:55-69 y README:53-66 describen 120 s/3 bloques/`forzar`/huérfana; «Medido» lista 193651, 203656, 204007, 204318; README y selftest comparan la lista de tools con el código |
| F PIDs sin hora de creación | **RESUELTO con residual** | `residente.json` guarda `t_create` (:1374) y `_res_kill` usa `_pid_is_ours(pid, t_create, t)` (:1330); dueño y candado con `t_create` (`_owner_alive` :2130, `_turn_owner_dead` :2139); `server_stop` igual (:2446). Residual: `_progreso_vivo` (:2074) solo `_pid_alive` y el fallback por nombre `cmd|python|java…` (:342) para registros viejos (D6) |
| G `cancel` mata el residente por defecto | **RESUELTO** | `forzar=False` por defecto; `cancel.json.kill = forzar and not huerfana` (:2543); `_ctx.cancel_kill` solo con ese flag (:2085); real: los tres cancel con servidor y residente vivos |
| H `mcp_server.py` 3268 l. | **ABIERTO (peor)** | 3420 l. (+152). Vigilante, progreso, cancel, residente y `--cli` siguen en el mismo archivo |
| I GUI olvidada sin tope | **RESUELTO (opt-in)** | `STARCCM_MAX_HORAS` en `_idle_stop` (:147, :703-713): guarda y apaga aunque haya GUI, reintenta si `busy`; con selftest. Por defecto 0, así que sin configurar nada cambia |
| J margen de selftests | **PARCIAL** | MCP 37,1 s contra el mismo límite de 45 s (8 s de margen, medido en solitario; antes 42-43); el umbral no se tocó |

Resumen (10 defectos): 7 resueltos (A, B, C, E, F, G, I; A y F con residual), 2 parciales (D, J), 1 abierto (H).

## 3. Defectos (por severidad, máx. 10)

**P2**

D1. **Macro huérfana invisible tras cerrar/reabrir el panel.** La tarjeta de progreso se muestra solo con `active` (<45 s,
   `PROG_STALE_S`) o con `cancel_pending and abierto` (chat_panel.py:1249); `on_stop` solo actúa si `runner.busy` o `progress_active()`
   (:974-991). Reproducción: pump_case desde el panel, cerrar el panel (o que el MCP muera) sin pulsar Detener, reabrir pasados 45 s con
   la macro en `iterar`: `prog["abierto"]` es verdadero pero no hay tarjeta, ni botón «Parar iteraciones», ni aviso (el aviso solo sale
   en `check_after_stop`); la licencia sigue cobrando hasta que el usuario le diga a la IA «cancela». El MCP ya sabe cancelarla.
   Arreglo: al arrancar el panel y en `refresh_progress`, si `abierto and not active` y `lock_info`/`.pid` de la macro vivos, pintar la
   tarjeta «macro sin dueño» con botón que llame `--cli cancel`, y el mismo `stop_warning` una vez.

**P3**

D2. **ETA sin garantía.** El tope «×2 entre muestras» (mcp_server.py:2011-2012) se aplica por actualización de `progreso.json` (≤ cada
   2 s), no por segundo: en 5 s puede doblarse varias veces (203656: 33,0 → 135,9 s en it. 44 → 60). El panel pinta «faltan ~2 min»
   y baja a 26 s. Arreglo: limitar la variación por tiempo (p. ej. ≤ +25 %/s) y no mostrar cifra hasta la it. `ventana/2`; validar con
   una corrida completa nueva (la 193651 es previa a la fórmula).

D3. **Sin corrida completa posterior a la ronda 6.** 193651 (19:36) precede a f5efe9a (+279 l. en `mcp_server.py`, +32 en `bomba.py`
   —macro ITERAR—, selftest 37 s). Los tres cancel posteriores no pasan por `post` (planos, 3 PNG, y+, Euler tras iterar). Es el
   camino que da el resultado al usuario. Arreglo: un `tests\real_e2e.py` completo archivado (3 min) tras el último cambio de macro.

D4. **Camino `pendiente` real sin ver.** Ambas pruebas de malla duran 26 s < `wait_s` 45 s, así que `cancel` nunca devolvió
   `pendiente`, ni el vigilante cerró `progreso.json` huérfano, ni el panel pasó por `watch_pending` con solver. Hoy solo selftest
   (selftest_mcp.py:963-1005; chat_panel.py:1667-1686). Con `mode=normal` (370 k celdas, minutos de malla) este es el camino normal.
   Arreglo: `real_panel_e2e.py --detener-en-malla --wait-s 5` (o malla normal) archivado.

D5. **y+ en 5-30 sin aviso automático.** 193651 `YPLUS media 24.45` y `avisos` sin y+; CLAUDE.md dice «afinar prismas antes de fiarse de η
   y del par», pero depende de que el modelo lea el campo. Arreglo: `_parse_pump` (o la macro) emite `AVISO yplus` si la media de álabes
   está en 5-30 con la acción recomendada.

D6. **Dos definiciones de «dueño vivo».** `_progreso_vivo` (mcp_server.py:2074) usa `_pid_alive` sin hora de creación; `_owner_alive` (:2130)
   sí. Con un PID reasignado en < 10 s tras Detener, `cancel` entra por la rama no huérfana y su bucle (:2570-2573, sale cuando
   `lock_holder()` es None) responde `cancelled: True` al instante con la macro aún corriendo. Probabilidad muy baja, resultado engañoso.
   Arreglo: `_progreso_vivo` → `_owner_alive`; quitar el fallback por nombre cuando hay `t_ref`.

**P4 (preferencias, ≤ 0,25)**

D7. `mcp_server.py` 3420 líneas y creciendo (H). Extraer `vigilante.py`, `progreso.py` y `cancel.py`; `--vigilante`/`--cli` ya no importan `mcp`.

D8. chat_panel.py:1012-1013 es código muerto (`if act and self.auto_cancel: pass` tras el `return` de :1008); la cadena «paró antes de iterar» y el
   texto sin contradicción solo se prueban con falsos. Arreglo: borrar la rama y re-ejecutar `real_panel_e2e.py --detener-en-malla`.

D9. Selftest MCP con 8 s de margen contra su límite de 45 s (J): subir a 60 s o medir CPU, no pared.

D10. `STARCCM_MAX_HORAS` queda en 0 por defecto (una GUI olvidada cobra indefinidamente salvo que se configure); bomba.py:326 traga en silencio
   un reporte parcial que falle (imprimir `WARN`). Un solo caso (BombaRapida) y una sola geometría validan todo; la curva de 5 caudales
   sigue «pendiente de verificar».

## 4. Notas

| eje | nota | justificación |
|---|---|---|
| **DISEÑO** | **9,0** (EVAL_4: 8,5) | Sube: la decisión de cancelar usa el estado vivo de la macro (`macro_running()` + dueño muerto) y no la fase de un JSON viejo; `forzar` explícito en vez de matar por defecto; `t_create` en residente/dueño/candado/progreso; `STARCCM_MAX_HORAS`; docs y código coinciden y el selftest comprueba README contra las tools; arnés real que usa la clase `Panel`. Resta 1,0: archivo de 3420 l. y creciendo (D7, 0,25); dos criterios de «dueño vivo» (D6, 0,25); el panel duplica con heurísticas propias (`abierto`/`lock_info`) lo que el MCP sabe con `macro_running` y deja la rama muerta de D8 (0,25); ETA con parche de tope en lugar de modelo acotado (D2, 0,25) |
| **FUNCIONALIDAD** | **9,0** (EVAL_4: 9,0) | Evidencia fuerte y nueva: cancel con dueño vivo y resultados parciales (14,5 s), Detener real en iterar (14,2 s) y en malla (sale sin iterar), apagados de 4,7-7,4 s, corrida completa de 162 s. Resta 1,0: sin corrida completa posterior a los cambios de macro (D3, 0,25); ETA aún salta (D2, 0,25); un solo caso/geometría y sin curva de caudales (D10, 0,25); y+ 5-30 solo como dato (D5, 0,25) |
| **ROBUSTEZ** | **9,0** (EVAL_4: 8,5) | Sube: A, B, G resueltos con prueba real; el servidor, el residente y el vigilante siguen vivos en los tres cancel; el panel no mezcla avisos tras el arreglo. Resta 1,0: huérfana invisible al reabrir el panel (D1, 0,5); `pendiente` solo con falsos (D4, 0,25); `_progreso_vivo` sin hora de creación (D6, 0,15); margen del selftest y tope de horas desactivado por defecto (D9/D10, 0,1) |

## 5. Qué falta para 9+ (y más)

- **DISEÑO → 9,5**: partir `mcp_server.py` (vigilante/progreso/cancel); una sola función «dueño vivo» (D6); que el panel pregunte al MCP
  (`--cli server_status` ya devuelve turno/vigilante) en vez de duplicar heurísticas; ETA como intervalo acotado, no cifra con parche.
- **FUNCIONALIDAD → 9,5**: una corrida completa nueva tras el último cambio (D3); aviso y+ en el resumen de `pump_case` (D5); un segundo
  caso (otra bomba) o la curva de 3-5 caudales con su `e2e.json`; ETA validada contra un fin real.
- **ROBUSTEZ → 9,5**: tarjeta/botón para macro huérfana al reabrir el panel (D1) con selftest; una prueba real que fuerce `pendiente`
  (D4); `_owner_alive` en todos los sitios; opcional, `STARCCM_MAX_HORAS` con valor por defecto sensato (p. ej. 6 h) y aviso en la barra.
  El riesgo residual es el solver y el límite de 2410 (importar/mallar no interrumpibles).

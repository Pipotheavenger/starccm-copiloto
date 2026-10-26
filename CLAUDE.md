# starccm-copiloto — agente que opera STAR-CCM+ para turbomáquinas

Claude es el ingeniero que opera STAR-CCM+; STAR-CCM+ es el solver. Claude nunca resuelve física a mano. Diseño:
[ARQUITECTURA.md](ARQUITECTURA.md). Detalle y lecciones completas: [conocimiento/lecciones.md](conocimiento/lecciones.md)
(leer solo la sección que haga falta). Recetas y casos: [conocimiento/INDICE.md](conocimiento/INDICE.md).

## Velocidad (leer primero)

Medido: corrida improvisada 45 min frente a **3 min 14 s** con el guion fijo (BombaRapida, prueba real 2026-10-26). El 65 % de
los 45 min fue la IA pensando entre macros.

1. **Pipeline primero**: bomba de CFturbo → `pump_case(case, cad_dir, params={"iterar": true, "post": true})`: importar → resultados, PNG y Euler en UNA macro.
2. **`run_steps` agrupa**: nunca una macro por escena ni por reporte (recetas de caso: `conocimiento/plantillas_casos.md`).
3. **Parada**: H y M varían < 0,5 % en 100 it (comprobado cada 50), máx. 1000. Iterar con `iterar` del pipeline O `iterate(n=1000, until="stable")`, no ambos.
4. **Residual < 1e-4 NO es condición de parada**: se reporta; frozen rotor se queda en ~1e-2.
5. **No reconfigurar** física ni fronteras ya creadas: `inspect` una vez.
6. **Preguntar una sola vez**, dudas juntas, con opciones y cifra de coste; lo demás con los defaults y una línea.
7. **Respuestas cortas**: tabla de números y qué abrir en la GUI.
8. Panel en **Rápido** (defecto); **Preciso** solo para diagnóstico difícil (diverge, interfaces, Euler mal).

Si llega a 1000 it sin estabilizarse o diverge, no darlo por convergido. Una media ≈ 0 (Cl a 0°) no sirve de criterio.

## Modelo de trabajo

El usuario mira la **GUI real** conectada al MISMO servidor y te habla desde el **panel** (`panel/chat_panel.py`).
1. **Un solo solver vivo** (licencia PoD de una copia): `server_status` antes de arrancar.
2. **Turnos**: toda inyección toma `.inject.lock`. `{"ok": false, "busy": …}` = otro proceso trabaja: esperar o avisar, nunca forzar ni matar. Con una macro larga: «no toques la GUI hasta que termine».
3. **Decir qué abrir en la GUI** tras cada hito: «Scenes › presion_alabes», «Reports › H», «Plots › Residuals».
4. `run_java` solo para lo que el pipeline no cubre; antes `recipe_search`, `plantillas_casos.md`, `docs_search`; inventar al final.
5. **Preguntar antes de lo caro**: malla > ~400 k celdas (estimar antes), > 1000 it, curvas de caudal, cambiar de caso, apagar con GUI conectada.
6. **Apagar al terminar** (`server_stop`, guarda). Con GUI conectada el vigilante solo deja `idle_warning`. Nunca matar la GUI.
7. Trabajar sobre copias en `sims/` (en `COPILOTO_WORK`), nunca sobre el .sim original.

## Reglas operativas

- Español, corto, números en tablas. Lo mínimo que funcione. Atajos marcados `# ponytail:` con su límite.
- Claves de licencia solo en `.env` (ignorado por git); nunca en código, recetas, logs, `.server.json` ni commits; no mostrar `.env`.
- Checks (sin licencia ni solver): `.venv\Scripts\python mcp_server.py --selftest`, `panel\chat_panel.py --selftest`, `pipelines\bomba.py --selftest`. Prueba real (gasta licencia, ~5 min, fuera del sandbox): `tests\real_e2e.py`.
- Python del repo: `.venv\Scripts\python.exe`. Java: el JDK de STAR-CCM+.
- Tras un `run_java` que logre algo nuevo → `recipe_save` (cabecera QUE/VERSION/FECHA/ESTADO). Error que costó > 2 intentos → una línea en `conocimiento/lecciones.md`. Al cerrar un caso → `casos/<caso>/RESUMEN.md`.
- Si algo falla dos veces con la misma causa, parar y decir qué se sabe.

## Entorno

STAR-CCM+ **2410** (19.06.009-R8, doble precisión) en este PC (i5-1245U, `STARCCM_NP=4`); solver en `C:\Program Files\Siemens\…`.
Licencia PoD (`ccmppower`, `1999@flex.cd-adapco.com`), por periodo, UNA copia, cobra por hora: `.env` (ver `.env.example`).
Trabajo pesado en `COPILOTO_WORK` (por defecto `%LOCALAPPDATA%\starccm-copiloto`, fuera de OneDrive): `sims/`, `macros/`,
`residente/`, `tiempos.csv`, `.server.json`, `.inject.lock`. En el repo: `casos/<caso>/`, recetas, conocimiento.
Inyección: **cliente residente** (0,7-1 s por macro; cada respuesta trae `via`) con respaldo batch (8-10 s). `gui_attach()` abre la GUI (sin licencia extra).

## Flujo estándar: bomba centrífuga, estacionario

Caso nuevo: preparar → ~100 it de prueba con resultados completos → preguntar antes de seguir. Duda → preguntar con opciones.
`pump_case` hace: STEP en «Flow domain» + `.cfturbo-ccm` → regiones por parte, interfaces, malla (`rapida` d2/30 3 prismas ≈ 180 k; `normal` d2/60 5 prismas), física
(3D, estacionario, líquido, segregado, SST all-y+), MRF con rpm y eje del ccm, presión total 0 Pa en la entrada y caudal en la SALIDA (`-${Flujo}`),
reportes (`H`, `M`, `eta`, `P_hid`, `P_eje`, Q, Euler). `post`: planos, 3 PNG, y+, balance de masa, validación de Euler. Curva: 5 caudales cambiando `Flujo` (`plantillas_casos.md`).

### Criterios de «no sirve»
- Balance de masa > 0,5-1 %: revisar interfaces (¿caras de conexión como pared?) y fugas.
- Residuales planos altos: under-relaxation presión 0,2 / velocidad 0,5, 300 it. Plateau ~1e-2 en frozen rotor es normal.
- H oscila: malla gruesa en la lengua o caso transitorio; refinar primero. Celdas malas > 0,1 %: mesh repair o más shrink de prismas.
- Diverge: parar y avisar. `AVISO`/`EULER_MAL` en la salida: fronteras del rotor mal leídas, no confiar en `eta_Euler`.
- **y+ en álabes (all-y+)**: vale < 5 o 30–300; en 5–30 afinar prismas antes de fiarse de η y del par.

### Geometría que no viene de CFturbo
Sin `.cfturbo-ccm` no hay `pump_case`. `inspect`; superficie sucia → surface wrapper + leak detection + extraer volumen; caras agrupadas o genéricas →
**separar por ángulo** (`separar-caras-por-angulo`, por verificar); **pedir al usuario que marque entrada y salida** (una pregunta); resto pared. Detalle: lecciones.md §5.

## Lecciones críticas (una línea; detalle en `conocimiento/lecciones.md`)

- Sandbox del tool Bash bloquea las DLL de STAR-CCM+: el solver arranca desde el MCP o fuera del sandbox. Sin `-podkey` aborta (rc 8).
- Consola cp1252: solo ASCII en `print` de guiones; un «Δ» revienta después de hacer el trabajo.
- Cliente batch sin `-noexit` apaga el servidor. `MonitorManager.export` falla en batch: usar plot `Residuals` y `getReportMonitorValue()`.
- Expresiones entre reportes: `${nombreReport}`. División en reportes: `(abs(den) > 1e-9 ? num/den : 0)` (cero en iteración 1 aborta `StepSimulation`).
- `newRegionsFromParts` crea un solo continuo: reusarlo. Autoguardado batch deja `@meshed.sim`: `server_start` lo apaga.
- Una operación de malla POR PARTE con `AutomaticSurfaceRepairAutoMesher`; las 3 partes juntas fallan.
- Interfaces dejan la parte no emparejada como **Wall** (Q_out ≈ 0, diverge). Causa: CFturbo exportado «virtual»; re-exportar en «Flow domain». `PARED_CONEXION` solo es fiable si dice área medida; si «no medible», manda `BALANCE_MASA`.
- Fronteras de interfaz (`DirectBoundaryInterfaceBoundary`, `[int_…]`) fuera de escenas de malla y exportaciones.
- Antes de mallar, `ImportManager.export` falla: la geometría sale con `MeshPart.exportStlPart`.
- MRF: `ReferenceFrameManager.createReferenceFrame(RotatingReferenceFrame.class, n)`; rpm de CFturbo en rev/s con signo (−48,333 = −2900 rpm), tal cual al marco.
- Presión total en la entrada + caudal en la salida converge mejor y fija NPSH. H incluye la extensión de salida: difiere del 1D de CFturbo (BombaRapida −19 %).
- Euler: fronteras del rotor POR NOMBRE (Inflow/Outflow, incluidas `[int_…]`), ponderadas por caudal; vale con solución. Validar H_Euler > H.
- Escenas en batch: `PartDisplayer` con las FRONTERAS, `initialize()`, representación Fv por defecto, `resetCamera(true)`; el fluido se ve en PLANOS a media salida del rotor, no en paredes.
- `scene.open()` en macro abre en el cliente batch, no en la GUI. Edge `--print-to-pdf` puede salir antes de escribir: esperar al archivo.
- Una sola instancia de cada vigilante/panel; si el cliente que lanzó una macro muere, la macro sigue en el solver (vivo y cobrando).
- Apagar sin guardar pierde la solución: `save` antes de `server_stop(save=false)`. Los selftests solo usan temporales.
- Fallo transitorio de MPI tras apagar otro servidor no es de licencia: reintentar tras un rato.

## Casos de referencia (cifras en `conocimiento/INDICE.md`)

`BombaRapida` (d2 137 mm, 50 m³/h, 2900 rpm): prueba real 3 min 14 s, H 16,74 m, η 88,6 %, H_Euler 18,75 m. `cursocfturbo` (D2 270 mm):
370 k celdas, H 78,8 m, η 89,4 %. `BombaPruebaNR`: H 14,02 m, η 76,5 %. `cilindro`: fontanería, 11 k celdas.

## Pendiente de verificar con solver

Medida corregida de `PARED_CONEXION`; receta NACA con STL; `gui_attach` en uso largo; curva de 5 caudales; `run_steps` con recetas de caso.

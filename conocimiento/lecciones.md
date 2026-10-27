# Lecciones y detalle operativo (vigentes)

Aquí vive TODO el detalle que `CLAUDE.md` resume en una línea (el CLAUDE.md se carga en cada turno del panel: debe ser corto).
Leer la sección que haga falta, no el archivo entero. Las lecciones del remoto retirado y de la UI web antigua siguen en
`CLAUDE_agente_anterior.md`. Fechas 2026-10-23 salvo indicación.

Índice: 1 Velocidad · 2 Cómo se inyecta (residente y batch) · 3 Flujo de bomba y criterios · 4 y+ y calidad de malla ·
5 CAD que no viene de CFturbo · 6 Estimar celdas · 7 Lecciones por tema · 8 Preguntas al usuario · 9 Pendiente de verificar.

## 1. Velocidad (detalle)

Objetivo: que todo funcione lo más rápido posible sin depender de los núcleos del solver. Medido el 2026-10-26 con la
misma bomba: corrida improvisada **45 min** frente a **3 min 45 s** con el guion fijo de BombaRapida (12×). El 65 % de los
45 min (≈ 29 min) fue la IA pensando entre macros: física configurada 3 veces, 11 macros para los reportes de Euler, 6
escenas una por una y un criterio de residual < 1e-4 que no se alcanza aunque H y M estén estables desde la iteración 100.

| # | regla | cifra que la respalda |
|---|---|---|
| 1 | **Pipeline primero.** Bomba de CFturbo: `pump_case(case, cad_dir, params={"iterar": true, "post": true})` va de importar a resultados, PNG y validación de Euler en UNA inyección | BombaRapida 3 min 45 s con 300 it; con parada estable (250 it): 3 min 14 s medidos en la prueba real (pump_case 142,8 s) |
| 2 | **`run_steps` agrupa; nunca una macro por escena ni por reporte.** Varios pasos, una llamada (recetas de caso: `conocimiento/plantillas_casos.md`) | 11 macros de Euler = 11 turnos de IA + 95 s de arranque batch; la misma lógica va en el pipeline |
| 3 | **`iterate(until="stable")`** (o `iterar` del pipeline / receta `iterar-hasta-estable`): para cuando H y M varían < 0,5 % en 100 it; **máximo 1000** (no 2000) | BombaPruebaNR: H 14,145 → 14,082 entre it 100 y 200 (0,45 %), corrió 700 it (430 s) sin cambiar nada |
| 4 | **El residual < 1e-4 NO es condición de parada**: se reporta (`RESIDUAL`) y la meseta sale como aviso. Frozen rotor se queda en ~1e-2 | BombaPruebaNR: plateau 3e-2 con H y M estables; BombaRapida: H a 0,5 % en it 100 |
| 5 | **No reconfigurar física ni fronteras ya creadas**: `inspect` una vez antes de tocar; reusar continuo, parámetro `Flujo` y reportes | física configurada 3 veces en la corrida de 45 min |
| 6 | **Preguntar una sola vez**, todas las dudas juntas con opciones y la cifra de coste; lo demás se decide con los defaults de este archivo y se dice en una línea | cada ida y vuelta con el usuario son minutos |
| 7 | **Respuestas cortas**: una tabla de números y qué abrir en la GUI; sin narrar pasos ni repetir lo que la GUI ya muestra | |
| 8 | **Panel en modo Rápido** (sonnet, esfuerzo bajo; es el defecto) salvo diagnóstico difícil (diverge, interfaces, Euler mal): ahí Preciso | la IA es el 65 % del tiempo |

Dos vías equivalentes para iterar un caso nuevo: `params={"iterar": true}` (dentro de la misma macro, ninguna inyección
más) o `pump_case(until="stable")` (después, por bloques con `iterate`). Usar una, no las dos.

Criterio de parada por defecto, para todo caso: H y M estables (< 0,5 % en 100 iteraciones, comprobado cada 50) y máximo
1000 iteraciones. Si llega a 1000 sin estabilizarse o diverge, no dar el resultado como convergido. Una magnitud con media
≈ 0 (p. ej. Cl a 0°) no sirve como criterio: la variación es relativa a la media.

### Lecciones de velocidad
- **Cliente residente** (`poc_residente/RESULTADO.md`, 2026-10-26, solver real): un solo cliente batch `-noexit` vigila una
  carpeta y ejecuta cada macro con `StarScript`: **0,7–1 s por macro frente a 8,6 s** (≈ 10×; ~50 macros por caso ≈ 7 min).
  Es la vía por defecto del MCP (`STARCCM_RESIDENT=1`; cada respuesta trae `via`; cae al cliente batch si falla) y no
  precompila: un error de compilación vuelve en ~0,6 s. Consecuencia: el coste de una macro ya no es el arranque sino el
  turno de la IA; seguir agrupando.
- **Carpetas fuera de OneDrive**: `sims/`, `macros/`, `residente/` y `tiempos.csv` viven en `COPILOTO_WORK` (por defecto
  `%LOCALAPPDATA%\starccm-copiloto`), no en el repo: OneDrive sincronizaría cada `.sim` de 50–150 MB en cada guardado y
  bloquea archivos mientras se escriben (lecturas fallidas de `.server.json`). En el repo solo cosas pequeñas:
  `casos/<caso>/` (CAD, reportes, PNG, RESUMEN.md), recetas, `.server.json`, `.inject.lock`. Todo `.sim` o carpeta de
  resultados grande se guarda fuera de OneDrive.


## 2. Cómo se inyecta

`mcp_server.py` levanta UN `starccm+ -server -port 47827` con el .sim en memoria. Dos vías de inyección:

- **Residente (por defecto, `STARCCM_RESIDENT=1`)**: un solo cliente batch `-noexit` (`Residente.java`) vigila la carpeta
  `residente/` de `COPILOTO_WORK` y ejecuta cada macro con `StarScript`: 0,7-1 s por macro. No precompila: un error de
  compilación vuelve en ~0,6 s con el texto de javac. Si muere se relanza; si no arranca tras 2 intentos cae a batch.
- **Batch (respaldo)**: `starccm+ -host localhost:PORT -batch Macro.java -noexit` por herramienta (8-10 s) y, antes,
  compila con el `javac` de STAR-CCM+ (2 s, sin licencia, `STARCCM_PRECOMPILE=1`). El `-noexit` evita que el cliente apague el servidor.

El cuerpo va entre `AGENT_BEGIN/AGENT_END` con imports estándar (star.common, base.neo, base.report, meshing, flow, kwturb, vis,
cadmodeler, prismmesher, surfacewrapper, material, segregatedflow, turbulence, motion, metrics, resurfacer, dualmesher,
java.util, java.io); `sim` ya existe. El stdout del cliente se escribe en vivo en `macros/<macro>.out` de `COPILOTO_WORK`
(el Output de la GUI también lo muestra). Cada respuesta trae `via` (residente | batch | mixto) y `ms`; `tiempos.csv` los anota.

Tiempos de la vía batch (la residente solo paga el trabajo real):

| paso | segundos |
|---|---|
| `server_start` (licencia + MPI, sim nueva) | 4,5–19 (con .sim de 158 MB en frío 33) |
| macro trivial | 8–10 |
| primera macro que carga módulos (CAD, malla, física) | 50–75 en cursocfturbo; 14 en BombaRapida ya caliente |
| precompilación javac (solo batch) | 2–6 |

GUI: `gui_attach()` lanza `starccm+.bat -host localhost:47827` (fuera del sandbox, sin `-podkey`, no reserva otra
licencia, ~770 MB); `gui_status()` dice si hay una conectada. Verificado 2026-10-26 con BombaPruebaNR: el árbol se
actualiza solo, Output en vivo, iterar no se frena (0,5 s/iteración con o sin GUI).


## 3. Flujo de bomba y criterios (detalle)

Caso nuevo por etapas: preparar → ~100 iteraciones de prueba con resultados completos → preguntar antes de seguir.
Ante una duda (fronteras, modelos, fluido, seguir o no) preguntar con opciones y detenerse.

| etapa | cómo |
|---|---|
| 1 datos | `.cfturbo-ccm` (caudal másico, rpm con signo, eje, origen, ρ, μ, zonas y fronteras) y STEP en «Flow domain» |
| 2 pipeline | `pump_case(case, cad_dir, mode)`: importar STEP (una parte por zona), regiones por parte, interfaces en cadena, malla, física, MRF, fronteras, reportes y Euler |
| 3 malla | `rapida`: base d2/30, 3 prismas (≈180 k celdas); `normal`: d2/60, 5 prismas. Una operación por parte |
| 4 física | 3D, estacionario, líquido, segregado, densidad constante, SST all-y+, Cell Quality Remediation. Gravedad: máquinas grandes/verticales/NPSH. Gamma: Re bajos, y+≈1, +10–20 % por iteración. Decir en una línea cuáles se activan |
| 5 rotor | MRF (frozen rotor) con las rpm y el eje del ccm |
| 6 fronteras | presión total 0 Pa en la entrada; caudal en la SALIDA con parámetro global `Flujo` (`-${Flujo}`); resto paredes |
| 7 reportes | `pt_in`, `pt_out`, `Q_in`, `Q_out`, `M`, `H`, `P_hid`, `P_eje`, `eta` + Euler (U, Vu, Vr, `UVu`, `H_Euler`, `eta_Euler`, factor de deslizamiento si hay β2) |
| 8 correr | `iterate(until="stable")` o `params={"iterar": true}` del pipeline: H y M < 0,5 % en 100 it, aviso de meseta de residuales, máximo 1000 |
| 9 cierre | `params={"post": true}` o receta `post-bomba` (una macro): planos auto, PNG de presión en álabes, velocidad relativa en plano medio y y+ en `casos/<caso>/images`, `RESULT`, balance de masa < 0,5 %, validación de Euler; luego `RESUMEN.md`, guardar, apagar |
| 10 curva | 5 caudales entre 0,6 y 1,3 del nominal reusando malla y solución: basta cambiar `Flujo`; un solo `run_steps` (receta de caso 3 en `plantillas_casos.md`) |

Alternativa de mallado de las plantillas: trimmer en una sola operación (`mallar-trimmer-una-operacion.java`, no probada).
Turbina (o bomba como turbina): mismo flujo; cambia el signo del momento; η = P_eje/P_hid; Euler con MODO turbina.

### Criterios de «no sirve»
- Desbalance de masa > 1 %: revisar interfaces (¿caras de conexión como pared?) y fugas.
- Residuales planos altos: presión 0,2 y velocidad 0,5 de under-relaxation, 300 it, reevaluar. Plateau MRF ~1e-2 es normal en frozen rotor.
- Residual < 1e-4 sin alcanzar con H y M estables: no es fallo; se reporta y se para. Bajarlo pide malla más fina o transitorio.
- H oscila: malla gruesa en la lengua de la voluta o caso transitorio. Refinar primero.
- Celdas malas > 0,1 %: mesh repair o más shrink de prismas.
- Diverge (residuales suben, H o M sin sentido): parar y avisar; no seguir iterando.
- Euler: `AVISO` o `EULER_MAL` en la salida = las fronteras del rotor están mal leídas; no confiar en `eta_Euler`.


## 4. Criterio de y+ (álabes)

- Con wall treatment **all-y+** (el del pipeline) vale y+ < 5 (capa viscosa resuelta) o 30–300 (ley de pared); la zona 5–30
  es la peor (zona de transición): si la media en álabes cae ahí, afinar prismas o subir capas antes de fiarse del par.
- Medir y+ solo en los álabes (`post-bomba` / `params={"post": true}` imprime mín/media/máx). BombaRapida: 4,4–48,2 con
  3 prismas (aceptado para primera pasada). Un y+ > 300 o < 1 no invalida H, pero sí η y el par: decirlo.
- y+ pide solución: no se lee antes de iterar.

## 5. CAD que no viene de CFturbo (geometría genérica)

Sin `.cfturbo-ccm` el pipeline no sirve: nombres de cara, rpm, caudal y eje hay que pedirlos o deducirlos.
1. Importar el STEP/STL/Parasolid y mirar `inspect` (partes, superficies). Si ya viene como sólido cerrado, saltar al 3.
2. Superficie sucia o abierta: **surface wrapper** (`star.surfacewrapper`) sobre las partes; revisar fugas con *leak
   detection* indicando un punto dentro del fluido y otro fuera; si hay fuga, cerrar huecos o subir la resolución del wrapper.
3. Volumen de fluido: extraer del sólido/wrap (3D-CAD *subtract* o `ExtractVolume`) y crear la región desde esa parte.
4. Caras agrupadas o con nombres genéricos (`Surface 1`…): **separar por ángulo** (`separar-caras-por-angulo`, «Split by
   angle», 30–40°, por verificar) y renombrar a mano lo que salga útil.
5. **Pedir al usuario que marque entrada y salida** (una pregunta con la lista de caras y su área/posición); el resto es
   pared. Para un tubo recto en Z sirve `identificar-fronteras-por-posicion`.
6. Si diverge o la masa no cuadra tras 100 iteraciones, parar y avisar; no seguir a ciegas.

## 6. Estimar celdas antes de mallar

Antes de mallar, estimar: celdas ≈ volumen del fluido / (base^3 · ~0,7) + capas prismáticas (~25–30 % más). Referencias:
BombaRapida d2/30 = 180 k; cursocfturbo d2/60 + 5 prismas = 370 k. Respetar el límite que dé el usuario; si la estimación pasa
de ~400 k celdas (o de su límite), preguntar con la cifra de coste (≈ 0,5 s por iteración y 100 k celdas en este PC con 4
procesos) y no mallar sin respuesta.

## 7. Lecciones por tema

Fechas 2026-10-23 salvo indicación. Las del remoto y la UI web: `conocimiento/CLAUDE_agente_anterior.md`.

### Entorno, licencia y sandbox
- El sandbox del tool Bash bloquea las DLL de STAR-CCM+ («Una directiva de Control de aplicaciones bloqueó este
  archivo»). El solver arranca fuera del sandbox: desde el MCP o con el tool Bash sin sandbox. No es política de Windows.
- Sin `-podkey` el solver pide `ccmpsuite` a `1999@flex.cd-adapco.com` y aborta (rc 8, ~60 s). La clave va en `.env`.
- `doc/api` de la instalación es la API de co-simulación (C); el javadoc Java está en `doc/client/html`. `StarMacro` y
  `Simulation` viven en `starbase.jar`; `MeshOperationManager` en `meshing.jar`.
- 2026-10-24 · Git Bash no trae `bc`: medir tiempos con `$SECONDS`.
- 2026-10-24 · La consola de Windows es cp1252: un «Δ», «≈» o «→» en un `print` de un guion revienta DESPUÉS de hacer
  el trabajo (BombaRapida dejó el solver vivo y sin guardar). Imprimir ASCII o `sys.stdout.reconfigure(encoding="utf-8")`.
- Las herramientas de Claude Code corren en serie: para observar la GUI mientras itera, lanzar `iterate` en segundo
  plano sin sandbox y sondear.
- 2026-10-26 · Un fallo transitorio de MPI («init called when another process has exited…») justo tras apagar otro
  servidor no es de licencia; reintentar tras un rato.

### Macros Java y API
- El cliente batch apaga el servidor al acabar la macro: siempre `-noexit`.
- Expresiones entre reportes: `${nombreReport}` (p. ej. `${pt_outReport}`).
- `MonitorManager.export` falla en batch («StarUpdate unexpectedly missing»). Residuales: exportar el plot `Residuals`
  (`sim.getPlotManager().getPlot("Residuals").export(csv, ",")`); valores de reportes: `getReportMonitorValue()`.
  Así lo hace `iterate`.
- javac caza nombres equivocados en 2 s: `ThreeDimensionalModel` vive en `star.metrics`; no existe `AreaReport`
  (área = `SumReport` con la magnitud del field function `Area`).
- `getCurrentIteration()` en `sim.getSimulationIterator()`: 0 antes de iterar; los reportes de campo no valen sin solución.
- `Class[]` crudo con `pc.enable(c)` compila y es el patrón verificado para activar modelos.

### Geometría, regiones y malla
- `newRegionsFromParts` crea solo un continuo «Physics 1»; crear otro deja dos. Reusarlo (o crear si no existe).
- Autoguardado «batch» activo por defecto: deja `<sim>@meshed.sim` y `<sim>@00050.sim`. `server_start` lo apaga.
- `importCadPart(ruta, "SharpEdges", 30.0, 2, 1e-5)` conserva los nombres de cara de CFturbo
  (`[Impeller_1]-Flow domain-BladePS`); con `OneBoundaryPerPartSurface` pasan a fronteras con el mismo nombre
  (renombrar por el sufijo tras el último `-`).
- **Una operación de malla POR PARTE** y añadir `star.resurfacer.AutomaticSurfaceRepairAutoMesher`: las 3 partes en
  una sola fallan en el remallador («Surface intersects self» en r = D2/2 por caras coincidentes).
- Interfaces: `createBoundaryInterface` deja la parte no emparejada de cada cara de conexión como **Wall**
  (`estator/Outflow → Wall` junto a `estator/Outflow [int_entrada 1]`). Esa pared bloquea el flujo (Q_out ≈ 0, diverge).
  Comprobar tras crear interfaces y medir el área que quedó como pared (el pipeline imprime `PARED_CONEXION`).
  Causa de raíz: exportar de CFturbo en modo «virtual» (BombaFinal25: 5 617 frente a 14 830 mm², desfase 13 mm);
  en «Flow domain» coinciden (13 370 frente a 13 371 mm²; BombaRapida: residuos ≤ 0,16 %). Fronteras con «(virtual)» en
  el nombre = pedir re-export en «Flow domain» antes de mallar. En NQ25Q159H91 la causa fueron `RSIConnectionOutHub`
  y `RSIConnectionOutShroud` del estator, fuera de la interfaz.
- Fronteras de interfaz: `b instanceof DirectBoundaryInterfaceBoundary`; su nombre lleva `[int_…]`. Fuera de las
  escenas de malla y de la exportación de superficies (duplican discos internos).
- Antes de mallar, `ImportManager.export` (EnSight) falla («There is no volume mesh»). La geometría recién importada
  sale con `MeshPart.exportStlPart(archivo, false)` (receta `exportar-geometria-stl`).
- Geometría con caras agrupadas o nombres genéricos: separar por ángulo (`separar-caras-por-angulo`, por verificar) y
  pedir al usuario que marque entrada y salida; no seguir iterando si diverge.

### Física, MRF y fronteras
- MRF en 2410: `ReferenceFrameManager.createReferenceFrame(RotatingReferenceFrame.class, n)` +
  `getRotationRate().setValueAndUnits(rpm, "rpm")` + `getAxisDirection/Origin`; se asigna con
  `region.getValues().get(MotionSpecification.class).setReferenceFrame(frame)`. `RotatingMotion` no expone la velocidad.
- CFturbo da la velocidad en rev/s (−48,333 = −2900 rpm sobre +Z): signo y eje tal cual al marco.
- Presión total en la entrada + caudal en la salida converge mejor que lo contrario, fija la presión de entrada
  (NPSH) y el caudal es exacto. La salida es un Mass Flow con caudal NEGATIVO (`-${Flujo}`).
- Frozen rotor con interfaces no conformes: balance de masa exacto sin ajuste alguno.
- La H incluye la extensión de salida (presión total en su salida), que el 1D de CFturbo no cuenta: la diferencia con
  la predicción 1D no es solo deslizamiento (BombaRapida −19 %, cursocfturbo −6,7 %).

### Reportes de Euler
- 2026-10-26 · En BombaPruebaNR los reportes de salida del rotor leían la frontera de ENTRADA (UVu1 = UVu2, H_Euler 0 o
  menor que H): la elección «de menor y mayor radio entre las no-pared» no es fiable cuando quedan piezas de interfaz o
  paredes de conexión. La receta nueva elige POR NOMBRE (Inflow/Outflow de la región rotor, incluidas «… [int_…]»),
  promedia ponderando por caudal másico sobre el conjunto y valida al final (H_Euler > H, UVu2 ≠ UVu1, U2 > U1) con
  `AVISO`/`EULER_MAL`. ESTADO: verificado con solver el 2026-10-26 en BombaRapida_e2e (H_Euler 18,75 m > H 16,74 m, eta_Euler 0,892). Euler solo vale con solución.
- Triángulos: Vu positiva en el sentido de giro (sirve con rpm negativas); U = |ω|·r; `H_Euler = (UVu2 − UVu1)/g`;
  `eta_Euler = H/H_Euler`; σ = Vu2/Vu2∞ con β2 (Vm2 ≈ Vr2, salida radial).

### Escenas y superficies
- `createScene("X")` deja la escena como «X 1». Para que el PNG no salga en blanco en batch: `PartDisplayer` con las
  **fronteras** como entrada (no la región), `initialize()`, `setRepresentation(getDefaultFvRepresentation())` y
  `resetCamera(true)`.
- Vectores en un plano: `getGlyphSettings().setRelativeToModelLength(x)` es un porcentaje (1.0 invisibles; 6.0 bien);
  `setSpacingMode(0)` = GEOMETRIC con `getGeometricSpacing().setOnRatio(6)`. `PartManager` no tiene `getParts()`.
- En una pared sin deslizamiento la velocidad es la de la pared, no el flujo: el fluido se ve en PLANOS. El plano medio
  se coloca a media salida del rotor (Min/Max de `dot($$Position, eje)` en sus Outflow), no a cota fija (z = 30 mm
  cortó Bomba_NQ15, D ≈ 1,4 m, por el ojo del impulsor). Líneas de corriente 12×12 semillas (25×25 tapan el color).
- Exportar superficies con campo: `ImportManager.export(String, Collection<Region>, Collection<Boundary>, Collection<Part>,
  Collection<PartSurface>, Collection<FieldFunction>, boolean append, boolean dataAtVerts)` con `.case` = EnSight Gold
  (`docs_search` no lo encuentra: grep en `doc/client/html/star/common/ImportManager.html`). Vectores →
  `getMagnitudeFunction()`. Trampas: variables con asteriscos en el `.case`; binarios sin cabecera «C Binary».
- 2026-10-24 · AutoExport: `getStarUpdate().getUpdateModeOption().setSelected(StarUpdateModeOption.Type.ITERATION)` +
  `getIterationUpdateFrequency().setIterations(N)`; receta `activar-autoexport`.
- 2026-10-24 · Edge `--headless=new --print-to-pdf` puede salir con 0 antes de escribir el PDF: esperar a que el
  archivo exista y deje de crecer (informe PDF = receta `informe-detalle`, reflexión + try/catch).

### Servidor y operación
- 2026-10-26 · Cancelar con el solver real (BombaRapida, 3 pruebas): el criterio «Stop File» activo (`STOPFILE` en la salida)
  no cortó el `step(50)` en curso: siguió de la 101 a la 150. Respaldo `getSimulationIterator().stop()` desde un segundo cliente
  batch con el residente en `step()`: el servidor murió («Connection reset») las dos veces. Ahora el bucle de la macro mira ABORT
  y cancel.json entre bloques, para con `ITER_STOP cancelled`, salta el post y el servidor sigue sirviendo (14 s, iterate de 20 ok).
  Matar el residente queda solo como último recurso a los 60 s.
- 2026-10-26 · División por cero en un reporte (`eta`, `eta_Euler`, factor de deslizamiento) con la solución en cero: en la
  iteración 1 `H_Euler = 0` y «Non-finite value detected» aborta `StepSimulation` (se perdieron 66 s de malla). Todo
  denominador de un `ExpressionReport` va como `(abs(den) > 1e-9 ? num/den : 0)`.
- 2026-10-26 · `PARED_CONEXION` daba 100 % en las 6 caras de BombaRapida (geometría buena, balance 0,0002 %): el `SumReport` de
  Area sobre una frontera de interfaz interna devuelve 0 y el de la parte devuelve la cara entera. Ahora solo avisa si la
  interfaz hermana devuelve área (>0) y la pared sin emparejar pasa de 2 %; si no es medible lo dice y se confía en
  `BALANCE_MASA` (> 0,5 % = revisar interfaces). No pedir re-exportar CAD por ese aviso si el balance de masa es bueno.
- 2026-10-26 · `taskkill /PID <cli> /T /F` alcanza a los procesos DESACOPLADOS (breakaway, nuevo grupo) mientras el padre
  siga vivo: el desacople por sí solo no basta. Lanzar con un intermediario que muere (`_spawn_detached`: `python -I -c
  _LAUNCHER` lanza el destino y termina), así el destino queda huérfano y fuera del árbol de la CLI. Con `DETACHED_PROCESS` un
  `.bat` (`starccm+.bat`) pierde la salida redirigida de sus hijos de consola: por eso `CREATE_NO_WINDOW`
  (`COPILOTO_DETACHED=1` cambia a lo contrario, peor). Probado con `claude.exe` real: el desacoplado sobrevive a Detener.
- 2026-10-26 · Imports de `pump_case` pasados como lista de Python (`str(lista)`) rompían la macro: debe ser `import 'x';`
  por línea. Fallo real; `_pump_imports` los comparte con el selftest, que compila la macro real con javac, y `_wrap` sanea
  comillas y corchetes sobrantes.
- 2026-10-26 · División por cero en reportes: aborta el step (`Non-finite value detected`). Todo denominador:
  `(abs(den) > 1e-9 ? num/den : 0)` (ver la entrada de arriba; vale para `eta`, `eta_Euler`, `factor_deslizamiento`).
- 2026-10-26 · `server_start` con un `.sim` del mismo nombre que otro ya guardado en `sims/` lo REABRE (con su solución vieja):
  usar un caso nuevo por prueba (`BombaRapida_e2e_<hora>`), nunca repetir el nombre.
- 2026-10-26 · El cliente residente tarda 0,7–1 s por macro con StarScript (frente a 8,6 s del batch): agrupar macros no es
  por velocidad de inyección sino por los turnos de IA entre ellas; aun así, no lanzar una por reporte.
- 2026-10-26 · Aviso Euler espurio en CADA corrida con `iterar` (`AVISO Euler: radio medio de la entrada (0.0)…` y
  `EULER_VALIDAR sin solución`): la validación corría con la malla sin solución. Peor: `euler_check_body` salía VACÍO (cortaba
  hasta el primer `for (Report r : reps)`, que está antes de la validación), así que tampoco había `EULER_OK`/`EULER_MAL`
  tras iterar. Ahora la validación solo va tras el bucle estable (o en `post`) y los AVISO de radio exigen radio > 0.
  Si en una corrida con `iterar` falta `EULER_OK`/`EULER_MAL`, es un error del pipeline, no de la física.
- Ronda 3-4 (referencia): `panel_estado.json` (raíz del repo, ignorado) apunta a `estado_dir`; `vigilante-<puerto>.json` (pid +
  latido) garantiza uno por puerto; variables `COPILOTO_VIGILANTE_S`, `STARCCM_VIGILANTE`, `COPILOTO_VIG_GONE_S`,
  `COPILOTO_DETACHED`, `COPILOTO_AGENT_LOG` (ver `.env.example`).
- Un servidor viejo con un vigilante que no detecta macros largas guardó y mató el solver de otra sesión a los 7 min de
  una malla (2,7 M celdas). Regla: una sola instancia de cada vigilante; antes de probar, mirar procesos
  `Get-CimInstance Win32_Process | ? CommandLine -match 'mcp_server|chat_panel'` y parar las viejas.
- Los selftests nunca tocan `sims/`, `macros/`, `.server.json` reales ni apagan un solver registrado (temporales).
- Si el cliente que lanzó una macro muere, la macro sigue en el solver (p. ej. una malla): queda vivo y cobrando.
  `server_start` se niega con otro .sim cargado; apagar antes con `server_stop()`.
- Apagar sin guardar pierde la solución (BombaRapida quedó sin guardar): `save` antes de `server_stop(save=false)`.
- 2026-10-26 · GUI conectada: el vigilante de 30 min solo cuenta macros del agente y apagaría el servidor mientras el
  usuario mira; por eso con GUI solo avisa. El usuario no toca nada mientras corre una macro.


## 8. Preguntas al usuario (formato)

Las preguntas son texto del panel, no JSON ni botones (ya no hay UI web). Una sola vez, todas juntas: opciones numeradas, la
recomendada primera y la cifra de coste entre paréntesis. Ejemplo: «1) malla fina 450 k celdas (+6 min) · 2) rápida 180 k
(recomendada)». Lo que no cambia el coste ni el resultado, se decide con los defaults y se dice en una línea.

## 9. Pendiente de verificar con solver real

Verificado el 2026-10-26 con solver en BombaRapida_e2e (pump_case con iterar y post: H 16,74 m, eta 88,6 %, H_Euler 18,75 m, eta_Euler 0,892, balance 0,0002 %, estable en 250 it, 3 PNG): `pipelines/bomba.py` completo, `iterar`, `post`, receta de Euler (`reportes-euler-triangulos`) y `bomba-reportes` (con divisiones protegidas). Verificado además: medida de `PARED_CONEXION` corregida (en la corrida 181510 dijo «no medible», 0 AVISO; el balance 1,8e-4 % es lo que protege). Verificado también: Euler tras iterar sin aviso espurio (N1: EULER_OK, AVISO None) y cancelar sin tumbar el servidor. Sigue sin verificar: receta NACA con STL, `gui_attach` desde el MCP en uso largo, la curva de 5 caudales.

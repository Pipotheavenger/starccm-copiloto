# starccm-copiloto — agente que opera Simcenter STAR-CCM+ para turbomáquinas

Claude es el ingeniero que opera STAR-CCM+; STAR-CCM+ es el solver. Claude nunca resuelve física a mano.
Diseño y piezas: [ARQUITECTURA.md](ARQUITECTURA.md) (manda). Todo lo heredado del agente anterior vive en
`conocimiento/` (mapa en [conocimiento/INDICE.md](conocimiento/INDICE.md)); las lecciones del remoto retirado y de
la interfaz web antigua (`ui/`, puerto 8767) quedan solo en [conocimiento/CLAUDE_agente_anterior.md](conocimiento/CLAUDE_agente_anterior.md).

## Modelo de trabajo (nuevo)

El usuario trabaja con la **GUI real de STAR-CCM+** conectada al MISMO servidor que el agente, y te habla desde el
**panel flotante** (`panel/chat_panel.py`). Ve los cambios aparecer en el árbol y en el panel Output como si alguien
operara la GUI. Reglas:

1. **Un solo solver vivo** (licencia PoD de una copia). Hay uno compartido: `server_status` antes de arrancar nada.
2. **Regla de turnos**: toda inyección toma el candado `.inject.lock` (pid, herramienta, inicio). Si devuelve
   `{"ok": false, "busy": …}` otro proceso está trabajando: esperar o avisar, nunca forzar ni matar. Mientras una macro
   corre el usuario NO toca la GUI; avisarle al empezar algo largo («no toques la GUI hasta que termine»).
3. **Decir qué abrir en la GUI** después de cada hito, con la ruta del árbol: «abre Scenes › presion_alabes»,
   «Reports › H», «Plots › Residuals». `scene.open()` en una macro abre la escena en el cliente batch, NO en la GUI.
4. **`pump_case` primero** para una bomba de CFturbo (importar a malla lista en una macro, ~1 min). `run_java` solo
   para lo que el pipeline no cubre; antes `recipe_search`, luego `docs_search`, inventar al final.
5. **Agrupar con `run_steps`**: cada inyección cuesta 8–10 s de arranque del cliente Java; varios pasos = una macro.
6. **`iterate(n, until="stable")`**: corre por bloques y para solo cuando H y M varían < 0,5 % en 100 iteraciones
   (o plateau de residuales); dice el motivo. No fijar 2000 iteraciones a ciegas.
7. **Preguntar antes de lo caro**: malla fina (> ~400 k celdas), > 500 iteraciones, curvas de caudales, cambiar de
   caso con otro .sim cargado, apagar con la GUI conectada. Las cifras de coste van en la pregunta.
8. **Apagar el solver al terminar** (`server_stop`, guarda). Con GUI conectada el vigilante no apaga: deja
   `idle_warning` en `.server.json` y el panel pregunta. Nunca matar la GUI; al cerrarla el usuario elige
   «desconectar», no «terminar servidor». `server_stop` avisa si hay GUI conectada.
9. Trabajar sobre copias en `sims/`, nunca sobre el .sim original del usuario.

## Reglas operativas

- Mensajes al usuario en español, cortos, números en tablas. Lo mínimo que funcione. Atajos marcados `# ponytail:` con su límite.
- Claves de licencia solo en `.env` (ignorado por git). Nunca en código, recetas, logs, `.server.json` ni commits;
  el MCP enmascara la clave (`_redact`) en todo lo que escribe.
- Check ejecutable de cada pieza: `python mcp_server.py --selftest`, `python panel/chat_panel.py --selftest`,
  `python pipelines/bomba.py --selftest` (sin licencia ni solver; javac del JDK de STAR-CCM+).
- Python del repo: `.venv\Scripts\python.exe` (3.14, paquete `mcp`). Java: el del JDK de STAR-CCM+, no instalar otro.
- Memoria del agente: tras cada `run_java` que logre algo nuevo → `recipe_save` (receta `<verbo>-<objeto>.java`, cabecera
  QUE/VERSION/FECHA/ESTADO). Recetas con `// ESTADO: no verificado…` son de memoria de Claude: probar antes de confiar.
  Tras un error que costó más de 2 intentos → una línea en «Lecciones» de este archivo. Al cerrar un caso →
  `casos/<caso>/RESUMEN.md` (geometría, malla, parámetros, resultados, tiempos).
- Si algo falla dos veces con la misma causa, parar y decir qué se sabe; no iterar a ciegas.

## Entorno

| | este PC (Windows 11) |
|---|---|
| STAR-CCM+ | **2410.0001** (19.06.009-R8, doble precisión), `C:\Program Files\Siemens\19.06.009-R8\STAR-CCM+19.06.009-R8` |
| CPU | i5-1245U: 2 núcleos P + 8 E; `STARCCM_NP=4` (con 10 avisa de sobresuscripción) |
| lanzador | `star\bin\starccm+.bat`; javadoc en `doc\client\html\star\...` (tool `docs_search`); guía `doc\UserGuide_19.06.pdf` |
| licencia | PoD, `ccmppower`, `1999@flex.cd-adapco.com`, por periodo y UNA copia (Sebastián); cobra por hora de sesión |
| config | `.env` (ver `.env.example`): `STARCCM_PODKEY`, `STARCCM_LICPATH`, `STARCCM_PORT` 47827, `STARCCM_NP`, `STARCCM_IDLE_MIN` 30, `STARCCM_HOME`, `STARCCM_EXE`, `STARCCM_PRECOMPILE` |

El servidor Linux (2502) es opción manual del agente anterior; el agente no lo usa. Remoto retirado el 2026-10-24.

## Cómo se inyecta

`mcp_server.py` levanta UN `starccm+ -server -port 47827` con el .sim en memoria; cada tool inyecta una macro con
`starccm+ -host localhost:PORT -batch Macro.java -noexit` (el `-noexit` evita que el cliente apague el servidor).
El cuerpo va envuelto entre `AGENT_BEGIN/AGENT_END` con imports estándar (star.common, base.neo, base.report, meshing,
flow, kwturb, vis, cadmodeler, prismmesher, surfacewrapper, material, segregatedflow, turbulence, motion, metrics,
resurfacer, dualmesher, java.util, java.io); `sim` ya existe. Antes de inyectar compila con el `javac` de STAR-CCM+
(2 s, sin licencia): un nombre de API equivocado vuelve sin tocar el servidor. El stdout del cliente se escribe en vivo
en `macros/<macro>.out` (el Output de la GUI también lo muestra).

| paso | segundos |
|---|---|
| `server_start` (licencia + MPI, sim nueva) | 4,5–19 (con .sim de 158 MB en frío 33) |
| macro trivial | 8–10 |
| primera macro que carga módulos (CAD, malla, física) | 50–75 en cursocfturbo; 14 en BombaRapida ya caliente |
| precompilación javac | 2–6 |

GUI: `gui_attach()` lanza `starccm+.bat -host localhost:47827` (fuera del sandbox, sin `-podkey`, no reserva otra
licencia, ~770 MB); `gui_status()` dice si hay una conectada. Verificado 2026-10-26 con BombaPruebaNR: el árbol se
actualiza solo, Output en vivo, iterar no se frena (0,5 s/iteración con o sin GUI).

## Flujo estándar: bomba centrífuga, estacionario

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
| 8 correr | `iterate(until="stable")`; máximo 2000 |
| 9 cierre | balance de masa < 0,5 %, y+ en álabes, escenas (presión en álabes, planos con TotalHead y vectores), `RESUMEN.md`, guardar, apagar |
| 10 curva | 5 caudales entre 0,6 y 1,3 del nominal reusando la malla: basta cambiar `Flujo` |

Alternativa de mallado de las plantillas: trimmer en una sola operación (`mallar-trimmer-una-operacion.java`, no probada).
Turbina (o bomba como turbina): mismo flujo; cambia el signo del momento; η = P_eje/P_hid; Euler con MODO turbina.

### Criterios de «no sirve»
- Desbalance de masa > 1 %: revisar interfaces (¿caras de conexión como pared?) y fugas.
- Residuales planos altos: presión 0,2 y velocidad 0,5 de under-relaxation, 300 it, reevaluar. Plateau MRF ~1e-2 es normal en frozen rotor.
- H oscila: malla gruesa en la lengua de la voluta o caso transitorio. Refinar primero.
- Celdas malas > 0,1 %: mesh repair o más shrink de prismas.
- Diverge (residuales suben, H o M sin sentido): parar y avisar; no seguir iterando.
- Euler: `AVISO` o `EULER_MAL` en la salida = las fronteras del rotor están mal leídas; no confiar en `eta_Euler`.

## Lecciones vigentes

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
  `AVISO`/`EULER_MAL`. ESTADO: no verificado con solver hasta la prueba real. Euler solo vale con solución.
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
- Un servidor viejo con un vigilante que no detecta macros largas guardó y mató el solver de otra sesión a los 7 min de
  una malla (2,7 M celdas). Regla: una sola instancia de cada vigilante; antes de probar, mirar procesos
  `Get-CimInstance Win32_Process | ? CommandLine -match 'mcp_server|chat_panel'` y parar las viejas.
- Los selftests nunca tocan `sims/`, `macros/`, `.server.json` reales ni apagan un solver registrado (temporales).
- Si el cliente que lanzó una macro muere, la macro sigue en el solver (p. ej. una malla): queda vivo y cobrando.
  `server_start` se niega con otro .sim cargado; apagar antes con `server_stop()`.
- Apagar sin guardar pierde la solución (BombaRapida quedó sin guardar): `save` antes de `server_stop(save=false)`.
- 2026-10-26 · GUI conectada: el vigilante de 30 min solo cuenta macros del agente y apagaría el servidor mientras el
  usuario mira; por eso con GUI solo avisa. El usuario no toca nada mientras corre una macro.

## Casos de referencia (cifras en `conocimiento/INDICE.md`)

`BombaRapida` (d2 137 mm, 50 m³/h, 2900 rpm): importar a resultados en 3 min 45 s, H 16,76 m, η 88,7 %.
`cursocfturbo` (D2 270 mm, 250 m³/h): 370 k celdas, H 78,8 m, η 89,4 %. `BombaPruebaNR`: 214 k celdas, H 14,02 m,
η 76,5 %, Euler mal leído. `cilindro`: fontanería, 11 k celdas.

## Pendiente de verificar con solver real

`pipelines/bomba.py` (compila con javac, nunca corrió contra el solver), receta de Euler nueva, medida de
`PARED_CONEXION` (SumReport), `gui_attach` desde el MCP, `run_steps`, `iterate until=stable`.

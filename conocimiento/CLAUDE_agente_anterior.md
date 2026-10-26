# starccm — agente que opera Simcenter STAR-CCM+

Agente que opera STAR-CCM+ en lenguaje natural: recibe una geometría de bomba o turbina, la prepara,
malla, configura la física, corre y entrega altura, torque, eficiencia y escenas.
STAR-CCM+ es el solver. Claude es el ingeniero que lo opera. Claude nunca resuelve física a mano.

Proyecto independiente dentro del repo `starccm-agent`: todo vive en `starccm/`. No depende de
`cfturbo-agent` (el agente hermano de diseño 1D, repositorio aparte en `Documentos\cfturbo-agent`) ni lo toca. Uso paso a paso y endpoints: `README.md`.
Servidor Linux (solo a mano, fuera del agente): `vm/SERVIDOR.md`.

## Entorno

El solver corre solo en este PC. La ejecución remota se retiró el 2026-10-24 (ver «Remoto (retirado el
2026-10-24)» en Lecciones). El servidor Linux sigue existiendo, pero el agente no lo usa: `vm/SERVIDOR.md`.

| | Local (este PC, Windows 11) |
|---|---|
| STAR-CCM+ | **2410.0001** (build 19.06.009-R8, doble precisión) |
| CPU | i5-1245U: 2 núcleos P + 8 E (12 hilos) |
| procesos del solver | `STARCCM_NP` (por defecto hilos − 2; en `.env`: 4) |
| lanzador | `star\bin\starccm+.bat` bajo `STARCCM_HOME` (se detecta solo) |

- Licencia Power-on-Demand contra `1999@flex.cd-adapco.com`. Flags: `-power -podkey $STARCCM_PODKEY
  -licpath $STARCCM_LICPATH`. La clave vive **solo** en `starccm/.env` (ignorado por git),
  o `%USERPROFILE%\.starccm-agent.env`. Nunca en código,
  recetas, logs, `.server.json` ni commits; `mcp_server.py` la enmascara en todo lo que escribe.
  La PoD es por periodo y de una sola copia (Sebastián): un solo solver vivo a la vez
  (también frente a una corrida a mano en el servidor) y PoD cobra por hora de sesión (ver `vm/SERVIDOR.md`).
- Python 3.14 en `starccm/.venv` (paquetes `mcp`, `fastapi`, `uvicorn`). Java: el de STAR-CCM+, no instalar otro
  (JDK en `..\jdk\win64\jdk21.0.2\bin`).
- Javadoc de la API Java: `$STARCCM_HOME\doc\client\html\star\...` (1 595 clases solo en `star.common`):
  fuente de verdad de nombres y métodos → tool `docs_search`. Guía de usuario: `$STARCCM_HOME\doc\UserGuide_19.06.pdf`.
- `vm/bootstrap.sh` es el plan original (MV Rocky con 2606), aparcado: el servidor real es el Ubuntu de `vm/SERVIDOR.md`.

## Arquitectura

```
Claude Code ──MCP stdio──> mcp_server.py ──> starccm+ -server -port 47827 (UN servidor vivo, .sim en memoria)
                               │               ▲
                               │  por tool:    │ starccm+ -host localhost:47827 -batch Agent_<ts>.java -noexit
                               │
                               ├── recipes/   snippets Java verificados (memoria del agente)
                               ├── cases/     un directorio por caso: cad, reportes, imágenes, RESUMEN.md
                               ├── sims/      .sim de trabajo y logs del solver (ignorado por git)
                               ├── macros/    cada macro inyectada + su salida .out (ignorado por git)
                               ├── tests/     starccm+ y claude falsos para los selftests
                               ├── ui/        interfaz web local (puerto 8767)
                               └── vm/        servidor Linux, uso manual (SERVIDOR.md, remote_check.sh) y bootstrap.sh (aparcado)
```

STAR-CCM+ es cliente-servidor. Se levanta UN servidor con el .sim en memoria y cada tool le inyecta
una macro Java con un cliente batch. No se recarga el .sim entre pasos. La macro imprime entre los
marcadores `AGENT_BEGIN <tag>` / `AGENT_END <tag>` y el MCP recorta esa salida.
Inyección verificada el 2026-10-23 contra 2410 real: `starccm+ -host localhost:PORT -batch Macro.java -noexit`
(el `-noexit` evita que el cliente batch apague el servidor). Plan B (macro residente que vigila un
`inbox/`) no hizo falta.

Tiempos medidos en este PC:

| paso | segundos |
|---|---|
| `server_start` (licencia + MPI, sim nueva) | 4,5 |
| `server_start` en frío con el .sim de 158 MB | 32,6 (en caliente 4,5–6) |
| inyección de una macro trivial (arranque del cliente Java) | 8–10 |
| primera macro que carga módulos (3D-CAD/Parasolid, malla, física) | 50–75 |
| precompilación con `javac` | 2 |

## MCP: `mcp_server.py`

Un solo archivo, FastMCP, stdio. `python mcp_server.py --selftest` (starccm+ falso de `tests/`,
sin licencia, 5–20 s) debe pasar antes de entregar. Tools:

| tool | input | output |
|---|---|---|
| `server_start(sim_path?, name?)` | .sim a abrir (se copia a `sims/`) o vacío | pid, host:port, sim, segundos. Reutiliza el vivo; se niega si hay otro .sim cargado |
| `server_stop(save=true)` | | guarda, `sim.kill()`, limpia `.server.json` |
| `server_status()` | | vivo o no, minutos de inactividad |
| `inspect(path?)` | `tree`, `regions`, `physics`, `mesh_ops`, `parts`, `reports`, `monitors`, `scenes`, `solver` | JSON con nombres, tipos y valores |
| `run_java(body, imports?, timeout_s?)` | cuerpo de `execute()`; `sim` ya existe | stdout, exception, trace, segundos, ruta de la macro |
| `iterate(n)` | iteraciones | iteración actual, última fila de todos los monitores (residuales + reportes) |
| `export_scene(name, path?)` | escena | ruta del PNG (`cases/_scenes/` por defecto) |
| `save(path?)` | | ruta guardada |
| `recipe_search(query)` | texto | recetas con cabecera y puntuación |
| `recipe_save(name, body, note)` | | ruta en `recipes/` |
| `docs_search(query)` | texto | clases del javadoc y firmas que coinciden |

`run_java` envuelve el cuerpo así; antes de inyectar lo compila con el `javac` de STAR-CCM+
(classpath `star\lib\java\platform\modules\ext\*` …): un error de sintaxis o de nombre de clase
vuelve en ~2 s sin tocar el servidor (`STARCCM_PRECOMPILE=0` lo desactiva).

```java
import star.common.*; import star.base.neo.*; import star.base.report.*; import star.meshing.*;
import star.flow.*; import star.kwturb.*; import star.vis.*; import star.cadmodeler.*; ... // + imports extra
public class Agent_<timestamp> extends StarMacro {
  public void execute() {
    Simulation sim = getActiveSimulation();
    sim.println("AGENT_BEGIN <tag>");
    try {
      // body
    } catch (Exception e) {
      sim.println("AGENT_ERROR: " + e);   // + stack trace
    }
    sim.println("AGENT_END <tag>");
  }
}
```

Reglas del servidor:
- Apagado por inactividad: 30 min sin actividad medida en disco (mtime de macros/`.out` y `.server.json`)
  → `save` + `server_stop`; lo vigilan el MCP y la UI. Si el guardado falla no se apaga. PoD cobra por hora de sesión.
- Un solo servidor a la vez. Barridos se encadenan en la misma sesión, no se abren varias.
- `server_start` registra PID, puerto, sim y log en `.server.json`. Si el proceso murió, limpia y relanza.
  Si ya hay un .sim distinto cargado se niega: apagar antes con `server_stop()`.
- Siempre trabajar sobre una copia en `sims/`, nunca sobre el .sim original del usuario.
- `server_start` apaga el autoguardado batch (`AutoSave.setAutoSaveBatch(false)`).
- Config por env o por `starccm/.env` (ver `.env.example`): `STARCCM_PODKEY`, `STARCCM_LICPATH`, `STARCCM_PORT`,
  `STARCCM_NP`, `STARCCM_IDLE_MIN`, `STARCCM_HOME`, `STARCCM_EXE`, `STARCCM_PRECOMPILE`.

## Interfaz web: `ui/`

`ui/server.py` (FastAPI) + `ui/surfaces.py` (EnSight/STL → dataset) + `ui/index.html` + `ui/app.js`
(three.js por CDN) + `ui/style.css`, sin build, tema claro por defecto. Preview `starccm-ui`
(`.claude/launch.json` de la raíz, puerto 8767). Paneles, flujo y endpoints: `README.md`.
Comprobación: `python ui/server.py --selftest` (HTTP contra los casos, caso nuevo y subida, `fronteras.json`,
conversión de un EnSight mínimo y chat con `tests/fake_claude.py`; sin solver ni CLI real).

Imita el cliente de STAR-CCM+ asistido por IA, alrededor de UNA simulación (Sebastián, 2026-10-23:
«primero a 1 simulación», «todo animado tipo 3D, como el cliente de ccm pero IA driven, tema claro»).
Zonas: barra superior («＋ Caso nuevo», estado del solver, apagar, abrir en la GUI), árbol
plegable a la izquierda, visor 3D en el centro (campos, Fronteras, Carcasa, Rotor, corte), dock abajo
(Salida, Gráficos en vivo, Resumen) y panel de IA plegable y redimensionable a la derecha.

Reglas:
- La UI solo **lee** (casos, `.server.json`, logs, `macros/`, `surfaces/`) y pide al MCP importado como
  módulo: `server_stop`, `export_scene`, `inspect`, `run_java` (solo la receta de exportación). Nunca
  arranca el solver, nunca edita el .sim, nunca inyecta macros mientras el chat trabaja (el candado del
  MCP es por proceso).
- El chat lanza `claude -p` con cwd `starccm/`, las tools `mcp__starccm__*`, Read, Glob, Grep y escribir
  SOLO dentro de `cases/` (RESUMEN.md, convergencia.csv, 250 turnos máx.). Corre en un hilo del servidor
  que guarda los eventos: recargar o cerrar la pestaña no lo mata (`/api/chat/current` y `/api/chat/stream`
  reenganchan). La IA narra cada paso en una frase corta; cada tool se ve como «▶ Importando la geometría … 12 s ✓».
- Cada mensaje del chat empieza con `(Caso …)` y el estado del solver local.
- Mensaje mientras la IA trabaja («Enviar ya»): `POST /api/chat/queue`. La UI mata la CLI justo al terminar el paso
  en curso (tras un resultado de herramienta o un texto sin herramienta, nunca con una macro a medias) y la retoma
  con `--resume` y el mensaje, en el mismo hilo y el mismo flujo de eventos (`resumed`). «Detener» lo descarta.
- `cases/<caso>/fronteras.json` (modo Fronteras): papel de cada cara elegido por el usuario
  (`inlet|outlet|wall|interface|symmetry`). La IA lo usa tal cual, sin adivinar. Si la geometría trae caras
  agrupadas o con nombres genéricos: separarlas por ángulo (receta `separar-caras-por-angulo.java`),
  exportar la geometría al visor y PARAR pidiendo al usuario que marque entrada y salida. No seguir
  iterando si diverge.
- Al terminar un bloque de trabajo el agente deja lo que la página muestra: `images/*.png`,
  `reports/convergencia.csv` y las superficies (receta `exportar-superficies-ensight.java` o `POST /api/surface`;
  el visor convierte solo los EnSight nuevos de `surfaces/_raw/`).
- Informe PDF (botón): `ui/informe.py` + receta `informe-detalle.java`. La receta usa reflexión y try/catch, así que
  un método que falte en otra versión no la tumba. El PDF lo imprime Edge sin ventana. Copia del .sim: `caso.json` →
  `sim_destino`; `mirror_sim` del MCP la actualiza tras cada guardado. Detalle en `README.md`.
- 3D en vivo: antes de iterar, la IA activa `recipes/activar-autoexport.java`. Usa `CADA` 25 en la prueba de
  100 iteraciones y 50 en corridas largas, con `OUT` = `cases/<caso>/surfaces/_raw/vivo`. El visor sigue el
  dataset «vivo» (≈0,15 s y ≈4 MB por exportación; solo se guarda la última). Al acabar, apagar con `setEnabled(false)`.
- Toda confirmación va dentro de la página (segundo clic «¿Seguro?»): los `confirm()` del navegador no
  aparecen en el panel embebido de la app.
- Verificado contra el solver real el 2026-10-23 (recorrido completo de `cursocfturbo`): `/api/status`,
  `/api/tree?refresh=1` (9,9 s), `POST /api/scene` (9,1 s), `/api/progress`, `POST /api/server/stop`
  (guardó 158 MB), `POST /api/surface` (17 fronteras, 187 400 triángulos), escenas de plano con vectores y
  de malla, 180 iteraciones con la Salida en vivo, chat con `inspect` (4 turnos, 36 s). Por probar: `/api/open_gui`.

## Memoria

- `recipes/<verbo>-<objeto>.java`: snippet que funcionó, con cabecera de 3 líneas: qué hace,
  versión de STAR-CCM+, fecha. Guardar una receta tras CADA `run_java` que logre algo nuevo.
  Antes de escribir Java nuevo: `recipe_search` primero, `docs_search` segundo, inventar tercero.
  Las recetas con `// ESTADO: no verificado` son de memoria de Claude: probarlas antes de confiar.
- Este archivo: tras cualquier error que costara más de dos intentos, añadir una línea en
  «Lecciones» con la causa y la solución. Corto, una línea.
- `cases/<caso>/RESUMEN.md`: al terminar un caso, geometría, malla, parámetros, resultados, tiempo.

## Flujo estándar: bomba centrífuga, estado estacionario

Preguntar en vez de adivinar: ante una duda (fronteras, modelos, fluido, condiciones, seguir o no) terminar la
respuesta con UN bloque ` ```pregunta ` con JSON `{"pregunta", "opciones": [...], "multiple", "ver", "caras"}`
(recomendación primero) y detenerse. `ver` = `fronteras` (con `caras` dudosas «región/frontera»), `escena:<png>`,
`3d` o `graficos`: la página lo enseña y pinta las opciones como botones. Caso nuevo por etapas: preparar → 100
iteraciones de prueba con resultados completos → preguntar antes de seguir hasta el criterio de parada.

0. Geometría nueva: importar, exportarla al visor en seguida (receta `exportar-geometria-stl.java`, segundos) y mirar los nombres de cara. Si vienen agrupadas o genéricas, separar por
   ángulo y pedir al usuario que marque entrada y salida en el modo Fronteras (`fronteras.json`).
1. Importar CAD (STEP/Parasolid) como parte de geometría. Listar superficies y renombrar:
   `inlet`, `outlet`, `blades`, `hub`, `shroud`, `volute`, `walls`.
2. Surface wrapper si el CAD trae huecos. Extraer volumen de fluido interno. Leak detection
   entre inlet y outlet. Diagnóstico de superficie debe dar 0 bordes libres y 0 caras perforadas.
3. Partir en dos regiones: `rotor` (impulsor) y `stator` (voluta + tubos). Interfaz interna entre ambas.
4. Malla: poliédrica, prism layers (5 capas, estiramiento 1.3, espesor total 10 % del tamaño base),
   refinamiento en álabes y huelgo. Tamaño base inicial: diámetro del impulsor / 60. ESTIMAR las celdas antes de
   mallar y respetar el límite que dé el usuario. Alternativa de las plantillas NQ25Q159H91: trimmer en una sola
   operación, sin prismas en las interfaces (`mallar-trimmer-una-operacion.java`). Vale para volutas y tubos grandes,
   o si los poliedros se pasan de celdas; si falla por caras coincidentes, volver a una operación por parte.
5. Física: 3D, estacionario, líquido agua, flujo segregado, densidad constante, turbulento,
   k-omega SST. Tener SIEMPRE en cuenta los modelos de las plantillas (`fisica-modelos-turbo.java`) y decir en
   una línea cuáles se activan y por qué:
   - Cell Quality Remediation: casi siempre sí (barato, da robustez).
   - Gravedad: máquinas grandes o verticales, cavitación/NPSH, superficie libre.
   - Transición Gamma: Reynolds bajos o moderados, bombas pequeñas o cuando importe la eficiencia fina; exige
     y+ ≈ 1 y cuesta ~10-20 % más por iteración.
6. Rotor en Moving Reference Frame con rpm del usuario, eje según el CAD.
7. Fronteras: preferir lo de las plantillas (`fronteras-presion-total-caudal-salida.java`): presión total 0 Pa en la
   entrada y caudal en la SALIDA con el parámetro global `Flujo`. Converge mejor, fija la presión de entrada (base
   para NPSH) y barre la curva cambiando solo `Flujo`. Alternativa: mass flow inlet + pressure outlet 0 Pa.
   Resto walls; en el rotor las walls giran con el marco; shroud estática si aplica.
8. Reportes: presión total masa-ponderada en inlet y outlet, caudal másico en inlet y outlet,
   momento en `blades`+`hub` respecto al eje. Expresiones: altura `H = (p_out - p_in)/(rho*g)`,
   potencia hidráulica `rho*g*Q*H`, potencia eje `M*omega`, eficiencia. SIEMPRE además los de las plantillas
   (`reportes-euler-triangulos.java`):
   - triángulos de velocidad U1, U2, Vu1, Vu2, Vr1, Vr2 en la entrada y la salida del rotor;
   - altura de Euler H_Euler y eficiencia hidráulica eta_Euler = H/H_Euler;
   - factor de deslizamiento si se conoce β2 (del `.cfturbo-ccm` o preguntando);
   - campo TotalHead y gráficos «Altura» y «Eficiencia y potencia».
9. Monitores de todo lo anterior y de residuales. Criterio de parada: residuales < 1e-4 y
   variación de H y M < 0.5 % en 100 iteraciones. Máximo 2000 iteraciones.
10. Correr. Al terminar: balance de masa inlet vs outlet < 0.5 %, y+ en álabes entre 30 y 300
    (o < 5 si all-y+ con malla fina), escenas: presión en álabes, y+ en paredes y las del FLUIDO como
    en las plantillas. Primero `exportar-superficies-ensight` con PLANOS «auto», que coloca plano_medio y
    plano_meridional; luego `escenas-fluido.java`: TotalHead con líneas de corriente y vectores de velocidad
    RELATIVA en cada plano, y PNG de fluido_* y vectores_*. En las paredes la velocidad es la de la pared: el
    flujo se ve en los planos.
11. Curva: repetir con 5 caudales entre 0.6 y 1.3 del nominal, reusando la malla (con `Flujo` basta cambiar el
    parámetro).

Turbina (o bomba como turbina, como la Rev2 de las plantillas): mismo flujo; cambia el sentido de giro o del caudal, el
momento cambia de signo y el objetivo es la potencia extraída. Eficiencia = P_eje/P_hid. En `reportes-euler-triangulos`
va MODO turbina: H_Euler = (U1Vu1 − U2Vu2)/g y eta_Euler = H_Euler/H.

## Criterios de «no sirve»

- Desbalance de masa > 1 %: revisar interfaz rotor/estator y leaks.
- Residuales planos altos: bajar under-relaxation de presión a 0.2 y velocidad a 0.5, 300 iter, reevaluar.
- H oscila: malla gruesa en la lengua de la voluta o el caso es realmente transitorio. Refinar primero.
- Celdas malas > 0.1 %: mesh repair o subir prism layer shrink.
- Diverge (residuales que suben, H o M sin sentido): parar y avisar; no seguir iterando.

## Estilo

- Lo mínimo que funcione. Un archivo de MCP, sin clases de más. Sin frameworks de agentes.
- Toda lógica no trivial deja un check ejecutable: `python mcp_server.py --selftest`.
- Marcar atajos deliberados con `# ponytail:` y su límite conocido.
- Mensajes al usuario en español, cortos, números en tablas.

## Lecciones

Agrupadas por tema; fecha 2026-10-23 salvo indicación.

### Entorno, licencia y sandbox
- El sandbox del tool Bash de Claude Code bloquea las DLL de STAR-CCM+ («Una directiva de Control de
  aplicaciones bloqueó este archivo», `DynamicLoader: couldn't load StarProfile`). El solver solo arranca
  fuera del sandbox: desde el MCP (que Claude Code lanza sin sandbox) o con el tool Bash sin sandbox. No es
  una política de Windows.
- Sin `-podkey`, `starccm+` pide `ccmpsuite` a `1999@flex.cd-adapco.com` y aborta (`Failed to get all
  licenses`, rc 8, ~60 s). No hay ninguna clave guardada en el PC: va en `.env`.
- `doc/api` de la instalación es la API de co-simulación (C), no el javadoc. El javadoc Java está en
  `doc/client/html`. `StarMacro` y `Simulation` viven en `star/lib/java/platform/modules/ext/starbase.jar`;
  `MeshOperationManager` en `meshing.jar`.
- CPU i5-1245U: 2 núcleos P + 8 E. Con `-np 10` el solver avisa de sobresuscripción; `STARCCM_NP=4` en
  `.env`. El selftest y el cilindro no notan la diferencia; medir en una bomba.
- 2026-10-24 · Git Bash no trae `bc`: medir tiempos con `$SECONDS`.
- Herramientas de Claude Code corren en serie: para observar la UI mientras el solver itera, lanzar
  `iterate` con `python -c "import mcp_server…"` en segundo plano **sin sandbox** (el cliente carga las DLL
  de STAR-CCM+) y sondear `/api/progress` o el navegador mientras tanto.

### Macros Java y API
- El cliente batch por defecto apaga el servidor al acabar la macro: siempre `-noexit`.
- La precompilación con `javac` detecta nombres de API equivocados en 2 s sin licencia: usarla antes de
  inyectar. Ejemplos cazados: `MonitorManager.export` necesita 3 argumentos; `ThreeDimensionalModel` vive
  en `star.metrics` (ya en los imports por defecto).
- `MonitorManager.export` falla en batch («StarUpdate unexpectedly missing»). Residuales: exportar el plot
  `Residuals` (`sim.getPlotManager().getPlot("Residuals").export(csv, ",")`); valores de reportes:
  `getReportMonitorValue()` directo. Así lo hace `iterate`.
- Expresiones entre reportes: `${{nombreReport}}` (p.ej. `${{pt_outReport}}`).
- `_inject` escribe el stdout del cliente EN VIVO en `macros/<macro>.out`: la pestaña Salida de `ui/`
  muestra las iteraciones mientras corren; una macro sigue en marcha mientras su `.out` no contiene
  `AGENT_END`. El log del servidor NO trae las iteraciones.

### Geometría, regiones y malla
- `newRegionsFromParts` crea solo un continuo «Physics 1»; crear otro deja dos. Reusarlo.
- Autoguardado «batch» activo por defecto: deja `<sim>@meshed.sim` y `<sim>@00050.sim`. `server_start` lo apaga.
- 2026-10-26 · La GUI de STAR-CCM+ se conecta al servidor del agente con `starccm+.bat -host localhost:47827`
  (fuera del sandbox, sin `-podkey`). Verificado con BombaPruebaNR (214 k celdas, np 4): no pide otra licencia; el
  árbol se actualiza con lo que crea el agente; el panel Output muestra en vivo la salida de las macros; iterar no se
  frena (0,5 s/iteración con o sin GUI); la GUI ocupa ~770 MB. `scene.open()` abre la escena en el cliente batch, no
  en la GUI: el usuario la abre desde el árbol. Riesgos: el vigilante de 30 min solo cuenta macros del agente y
  apagaría el servidor mientras el usuario mira la GUI; el usuario no debe tocar mientras corre una macro; al cerrar
  la GUI elegir «desconectar», no terminar el servidor.
- Interfaces rotor/estátor que no emparejan todas las caras: `createBoundaryInterface` deja la parte no
  emparejada de cada cara de conexión como **Wall** (`estator/Outflow → Wall` junto a
  `estator/Outflow [int_entrada 1] → Internal Interface Boundary`). Esa pared bloquea el flujo: Q_out ≈ 0 y
  divergencia. Comprobar con `inspect("regions")` tras crear interfaces y medir el área que quedó como pared.
  En NQ25Q159H91 la causa fueron `RSIConnectionOutHub` y `RSIConnectionOutShroud` del estator, fuera de la interfaz.
  Causa de raíz en geometrías de CFturbo: exportadas en modo «virtual» las caras de conexión no coinciden
  (BombaFinal25: impulsor→voluta 5 617 frente a 14 830 mm², desfase 13 mm). Exportadas en «Flow domain»
  coinciden (13 370 frente a 13 371 mm², desfase 0). Fronteras con «(virtual)» en el nombre = pedir re-export
  en «Flow domain» antes de mallar (cfturbo-agent lo hace por defecto desde su commit 9aeaa60).
- Export de CFturbo (3 STEP con caras coincidentes en las interfaces): una sola operación de malla con las
  3 partes falla en el remallador («Surface intersects self» en r = D2/2). Cada parte por separado se
  remalla limpia. Regla: **una operación de malla por parte** y añadir
  `star.resurfacer.AutomaticSurfaceRepairAutoMesher` a la lista de malladores.
- `importCadPart(ruta, "SharpEdges", 30.0, 2, 1e-5)` conserva los nombres de cara de CFturbo como
  superficies de parte (`[Impeller_1]-Flow domain-BladePS`…); `newRegionsFromParts` con
  `OneBoundaryPerPartSurface` las convierte en fronteras con el mismo nombre (renombrar por sufijo).
- Fronteras de interfaz (rotor/estator): `b instanceof DirectBoundaryInterfaceBoundary`, y su nombre lleva
  `[int_…]`. Fuera de las escenas de malla y de la exportación de superficies (duplican discos internos).
  Con todas las demás (`pattern "."`): 17 fronteras, 187 400 triángulos, 10,7 s.

### Física y marcos rotatorios
- MRF en 2410: `ReferenceFrameManager.createReferenceFrame(RotatingReferenceFrame.class, n)` +
  `getRotationRate().setValueAndUnits(rpm, "rpm")` + `getAxisDirection/Origin`; se asigna con
  `region.getValues().get(MotionSpecification.class).setReferenceFrame(frame)`. `RotatingMotion` no expone
  la velocidad directamente.

### Escenas y superficies para el visor
- `createScene("X")` deja la escena como «X 1». Para que el PNG no salga en blanco en batch: `PartDisplayer`
  con las **fronteras** como entrada (no la región), `initialize()`,
  `setRepresentation(getDefaultFvRepresentation())` y `resetCamera(true)`. Receta `escena-malla.java`.
- Vectores en un plano (`VectorDisplayer`): `getGlyphSettings().setRelativeToModelLength(x)` es un porcentaje
  (1.0 = invisibles; 6.0 bien), `setSpacingMode(0)` = GEOMETRIC con `getGeometricSpacing().setOnRatio(6)`
  (una flecha de cada 6 celdas); `setSpacingMode(1)` = UNIFORM. `PartManager` no tiene `getParts()`:
  recorrer `getObjects()`. Receta `escena-plano-vectores.java`.
- Exportar superficies con campo: `ImportManager.export(String, Collection<Region>, Collection<Boundary>,
  Collection<Part>, Collection<PartSurface>, Collection<FieldFunction>, boolean append, boolean dataAtVerts)`
  con un `.case` escribe EnSight Gold (javadoc 2410; `docs_search` no lo encuentra porque indexa mal
  `ImportManager`: grep en `doc/client/html/star/common/ImportManager.html`). Las constantes de opciones
  viven en `AutoExport` (`OptionDataAtVerts`, `OptionTriangulatePoly`…). Vectores → `getMagnitudeFunction()`.
- 2026-10-24 · AutoExport (`sim.getSimulationIterator().getAutoExport()`): frecuencia con
  `getStarUpdate().getUpdateModeOption().setSelected(StarUpdateModeOption.Type.ITERATION)` +
  `getIterationUpdateFrequency().setIterations(N)`. Deja `vivo@00600.case` con datos `vivo@0060000000.*`.
  La exportación siguiente puede llegar mientras se convierte la anterior: el visor decide por la `iteration`
  del manifest, no por mtime (con mtime se quedaba en la 600).
- 2026-10-24 · Antes de mallar, `ImportManager.export` (EnSight) falla con «There is no volume mesh», aunque solo
  se pidan superficies de parte. La geometría recién importada sale con `MeshPart.exportStlPart(archivo, false)`
  (ASCII). Receta `exportar-geometria-stl.java`, verificada con BombaP100V1: 6 partes y 155 924 triángulos en
  segundos. Ojo: escribe « solid PRO2STL version 1.0», con un espacio delante y UN sólido por parte, sin nombres
  de cara. El visor da una pieza por parte; las caras con nombre llegan con EnSight tras mallar.
- 2026-10-24 · Un servidor viejo de la UI (puerto 8767, lanzado el día anterior) seguía vivo con el watchdog antiguo,
  que no detectaba las macros largas en marcha. A los 7 min de una malla (2,7 M celdas) guardó y mató el solver de
  otra sesión con una macro `saveState` + `kill`. Regla: una sola instancia de la UI. Antes de probar, mirar
  `Get-CimInstance Win32_Process | ? CommandLine -match 'ui.server'` y parar las viejas. Además, el selftest de la
  UI usa ahora un `.server.json` temporal, y `_state()` reintenta si el archivo existe pero no se puede leer.
- 2026-10-24 · El visor solo pinta fronteras: en una pared sin deslizamiento la velocidad es la de la pared (0 en
  la voluta, ω·r en el rotor), no el flujo. Para ver el fluido: `PLANOS auto` en `exportar-superficies-ensight`
  (grupo «Planos»). El plano_medio con cota fija z = 30 mm (de cursocfturbo, D2 270 mm) cortaba Bomba_NQ15
  (D ≈ 1,4 m, rotor z 0…152 mm) por el ojo del impulsor: un anillo y una cuña de voluta. Ahora se coloca a media
  salida del rotor con Min/Max de `dot($$Position, eje)` en sus fronteras Outflow. Las escenas de plano lo reusan.
- 2026-10-24 · Edge `--headless=new --print-to-pdf` puede salir con código 0 antes de escribir el PDF. Pasa sobre
  todo lanzado desde el servidor de la UI. `informe.to_pdf` espera a que el archivo exista y deje de crecer. Además,
  un `·` de `json.dumps` dentro de una variable CSS sale tal cual en el pie de página: no usar JSON para CSS.
  `ui/surfaces.py` lee ASCII y C Binary, `tria3/quad4/nsided`. Verificado con el solver real a las 12:21:
  8 fronteras del impulsor, 107 189 triángulos, 4 campos en 13,5 s; conversión 0,3 s.
- Trampas del EnSight de STAR-CCM+: el `.case` nombra las variables con asteriscos
  (`superficies*****.Pressure` → índice `00000`) y los archivos de variables binarios NO llevan la cabecera
  «C Binary» (su primera línea es la descripción): el formato lo fija la geometría.

### Servidor, MCP y UI (operación)
- `mcp_server.py --selftest` escribía en `sims/`, `macros/`, `agent.log` y borraba el `.server.json` real
  (peligroso con un solver vivo de otra sesión; la Salida de la UI enseñaba el «FAKE server»). Ahora
  redirige `SIMS`, `MACROS`, `STATE_FILE` y `AGENT_LOG` a un temporal, nunca apaga un solver registrado, y
  la UI filtra además `FAKE`, `<fake>` y `starccm-selftest-` en logs y macros viejos. El selftest de la UI
  fuerza modo local (antes leía el modo remoto de `.ui-settings.json` y lanzaba un solver real).
- `server_start` falló justo después de que otra sesión apagara el servidor (12:23, stop sin guardar): el
  log dice `Fatal error in MPI_Init_thread … init called when another process has exited without calling
  init`. Es un fallo transitorio de MPI, no de licencia: el reintento una hora después arrancó en 32,6 s.
- 2026-10-24 · Si el cliente del chat (navegador) se desconecta, la UI mata la CLI, pero la macro en curso
  sigue en el solver (p. ej. una malla): el solver queda vivo y cobrando. `server_start` de otro caso se
  niega si hay un .sim distinto cargado; apagar con `server_stop()`. (Desde f953f56 el chat corre en un
  hilo y sobrevive a cerrar la pestaña; «Detener» sigue matando la CLI.)

### Remoto (retirado el 2026-10-24)
Retirado por decisión de Sebastián: era la parte más frágil (7 fallos solo por ser remoto, dos versiones de
STAR-CCM+, solvers huérfanos) y para montar casos va mejor en local. Para recuperarlo: commits `038629d`…`e163e33`.
Las corridas grandes se hacen a mano: `vm/SERVIDOR.md`. Lecciones que dejó:
- 2026-10-24 · Arranca en 16,6 s y la macro se inyecta en 5 s, pero el `scp` PC→servidor va a ≈0,24 MB/s
  (158 MB = 694 s): subir el .sim una vez y no recopiarlo por `server_start`.
- 2026-10-24 · El servidor de la UI (lanzado por el preview) no tiene `HOME` y el `ssh` de Git busca la
  llave en `$HOME/.ssh`: el SSH fallaba en silencio (BatchMode) y la UI daba el solver por muerto.
  `_ssh_env()` pone `HOME=USERPROFILE` en cada ssh/scp; y el ssh va con stdin vacío (sin consola colgaba).
- 2026-10-24 · La IA arma rutas como «…/cad/» + nombre; la subida automática sube los archivos de una
  carpeta de `cases/` citada en la macro. El `.sim` remoto solo se pisa si el del PC es más nuevo (la
  primera versión comparaba solo el tamaño y pisó un .sim guardado en el servidor: 20 iteraciones perdidas).

### Casos de referencia
- `cursocfturbo` (bomba CFturbo, D2 270 mm, 250 m³/h, 2980 rpm): 370 k celdas, converge en 400 iteraciones
  (82 s/100 con 4 procesos): H 78,8 m, M 192,5 N·m, η 89,4 %, balance de masa 0 %. A 580 iteraciones:
  H 78,77 m, M 192,50 N·m, η 0,8935, continuidad 8e-7; aviso «Reversed flow on 55 faces on voluta: outlet»
  (reflujo en la salida del difusor, balance de masa intacto). Guion `cases/cursocfturbo/pump.py`; recetas
  `bomba-*.java`; escenas `plano_vectores`, `malla`.
- 2026-10-24 · Plantillas de Sebastián `NQ25Q159H91_Rev0/Rev2.sim` (Downloads; sin resolver, iteración 0). Leídas con
  `informe-detalle`, 501 propiedades. Lo que traen y nuestro flujo no:
  - Entrada de presión total 0 Pa y caudal en la salida (Mass Flow con el parámetro `${Flujo}`).
  - Modelos de transición Gamma, gravedad y Cell Quality Remediation.
  - Mallador trimmer en una sola operación (base 2 cm, 4 prismas de 2,5) con control «No prisms» en las interfaces.
  - Reportes de triángulos de velocidad (U, Vu, Vr en puntos de entrada y salida del rodete), altura de Euler,
    factor de deslizamiento y eficiencia CFD = H/H_Euler; TI de la entrada calculada por Re.
  - Escenas en planos X/Y/Z con TotalHead, vectores y streamlines de `Cell Relative Velocity`.
  - Rev2 es Rev0 con −3000 rpm y η = P_eje/P_hid (modo turbina) y 910 k celdas en vez de 988 k.
  Pasado a recetas y probado en una copia de cursocfturbo (80 iteraciones desde la convergida), todo de una vez:
  modelos (remediación, gravedad, Gamma), presión total + caudal en la salida, Euler y escenas del fluido.

  | magnitud | valor |
  |---|---|
  | H | 78,4 m |
  | H_Euler | 87,3 m |
  | eta_Euler | 0,899 |
  | eta | 0,888 |
  | U2 | 42,1 m/s (= ω·r2) |
  | Vu2 | 20,3 m/s |
  | Vu1 | ≈ 0 (entrada sin prerrotación) |
  | factor de deslizamiento | 0,66 (con β2 = 25°, supuesto) |
  | Q_out | +69,486 kg/s (el Mass Flow Inlet de salida va con −${Flujo}) |
  | residuo de intermitencia | 2e-3 (Gamma todavía asentándose) |

  Detalles: los planos auto quedaron a z = 43 mm; los campos son `TotalHead` y `Relative Velocity`. Las líneas de
  corriente con 25×25 semillas tapaban el color: 12×12.
- `cilindro` (r 50 mm, L 0,5 m, 11 463 celdas poliédricas + prismas, agua 1 m/s, SST): 100 iteraciones,
  residuales 1e-6, Q_in = −Q_out = 7,755 kg/s. `cases/cilindro/RESUMEN.md`.

# Recetas de caso para `run_steps`

Casos habituales que NO son una bomba de CFturbo (la bomba va por `pump_case`). Cada receta es una lista de pasos para UNA
llamada `run_steps(steps=[...])`: un paso es `{"java": <bloque>, "name": "...", "params": {...}}` o
`{"recipe": "<archivo sin .java>", "params": {...}}`. Cada bloque Java de este archivo es el cuerpo de un paso; su primera
línea `// PARAMS: a=1, b=2` da los parámetros y un valor de ejemplo. `run_steps` sustituye `${nombre}` solo por las claves
de `params` (`${Flujo}` y demás expresiones de STAR-CCM+ quedan como están). Cada paso corre en su propio bloque: no
comparten variables, por eso cada uno vuelve a buscar la región y la parte.

Los bloques están en el selftest (`python pipelines/bomba.py --selftest` los compila con el `javac` de STAR-CCM+ con los
valores de ejemplo); compilar NO es verificar: el estado de cada receta dice si corrió contra el solver.

Reglas comunes (CLAUDE.md § Velocidad): una sola llamada `run_steps` para todo; iterar con la receta `iterar-hasta-estable`
(H y M estables, no residual < 1e-4); no recrear física ni fronteras que ya existen; el `.sim` se guarda al apagar.
`kg_s`, nunca `Flujo`, como nombre de parámetro: `${Flujo}` es una expresión de STAR-CCM+ y se sustituiría.
Una magnitud cuya media es ≈ 0 (p. ej. Cl a 0°) nunca da «estable» (la variación es relativa a la media): no ponerla en
`reportes`.

Tiempos: «medido» = corrió en este PC (2410, 4 procesos); «estimado» = salido de una cifra medida de otro caso, sin correr.
Las cifras de arranque en frío (carga de módulos de CAD y física: 60–75 s la primera macro) aparecen solo en el cilindro,
que se midió en frío; con el servidor ya caliente BombaRapida hizo importar+regiones+física+fronteras+reportes en 14 s.

---

## 1. Tubería o cilindro (fontanería)

Estado: **verificado** (caso `cilindro`, 2026-10-23, pasos con recetas fijas r = 0,05 m, L = 0,5 m; aquí parametrizados:
la parametrización no se ha vuelto a correr). Agua, velocity inlet y pressure outlet, k-ω SST all-y+, poliédrica.

Antes: `server_start(name="tuberia")`. Parámetros: `radio_m` 0,05 · `largo_m` 0,5 · `v` 1 m/s · `base_m` 0,008.

| paso | qué | tiempo |
|---|---|---|
| 1 | cilindro en Z y superficie partida por ángulo | medido 74 s la primera macro de CAD en frío; ~2 s con módulos cargados (estimado) |
| 2 | región, fronteras `inlet` / `outlet` / `wall` por posición Z | macro trivial: 8–10 s medidos con cliente batch, ~1 s con el residente |
| 3 | física: receta `fisica-agua-segregado-sst` (sin parámetros) | medido 62 s en frío |
| 4 | malla poliédrica, 11 k celdas con base 8 mm | medido 48 s |
| 5 | fronteras + reportes `pt_in`, `pt_out`, `dp`, `Q_in`, `Q_out` | macro trivial: 8–10 s con batch, ~1 s con el residente |
| 6 | `iterar-hasta-estable`, `reportes` = `"dp"` | medido 12 s por 50 it: ~50–100 it → 12–25 s |
| 7 | resultados + escena de velocidad a PNG | estimado 5 s |

Medido en frío: 74 + 62 + 48 s (CAD, física, malla) + 24 s (100 it) ≈ 3 min 30 s más unos 10 s por macro trivial. Esperado
con el servidor ya caliente y los pasos en UNA llamada: ≈ 1 min 30 s (estimado). `iterar-hasta-estable` con `dp` puede parar
en 100–150 it: 12–25 s.

Paso 1 (geometría):

```java
// PARAMS: radio_m=0.05, largo_m=0.5
Units um = sim.getUnitsManager().getUnits("m");
SimpleCylinderPart cyl = sim.get(MeshPartFactory.class).createNewCylinderPart(sim.get(SimulationPartManager.class));
cyl.setCoordinateSystem(sim.getCoordinateSystemManager().getLabCoordinateSystem());
cyl.getStartCoordinate().setCoordinate(um, um, um, new DoubleVector(new double[] {0.0, 0.0, 0.0}));
cyl.getEndCoordinate().setCoordinate(um, um, um, new DoubleVector(new double[] {0.0, 0.0, ${largo_m}}));
cyl.getRadius().setValueAndUnits(${radio_m}, um);
cyl.getTessellationDensityOption().setSelected(TessellationDensityOption.Type.FINE);
cyl.rebuildSimpleShapePart();
cyl.setPresentationName("pipe");
cyl.getPartSurfaceManager().splitPartSurfacesByAngle(new ArrayList<PartSurface>(cyl.getPartSurfaces()), 89.0);
for (PartSurface s : cyl.getPartSurfaces()) sim.println("SURFACE " + s.getPresentationName());
```

Paso 2 (región y fronteras por posición):

```java
// PARAMS: largo_m=0.5
GeometryPart cyl = sim.get(SimulationPartManager.class).getPart("pipe");
sim.getRegionManager().newRegionsFromParts(new ArrayList<GeometryPart>(Arrays.asList(cyl)), "OneRegionPerPart", null, "OneBoundaryPerPartSurface", null, false);
Region r = sim.getRegionManager().getRegions().iterator().next();
FieldFunction posZ = ((PrimitiveFieldFunction) sim.getFieldFunctionManager().getFunction("Position")).getComponentFunction(2);
MinReport mn = sim.getReportManager().createReport(MinReport.class); mn.setPresentationName("zmin"); mn.setFieldFunction(posZ);
MaxReport mx = sim.getReportManager().createReport(MaxReport.class); mx.setPresentationName("zmax"); mx.setFieldFunction(posZ);
double L = ${largo_m};
Boundary inlet = null, outlet = null, wall = null;
for (Boundary b : new ArrayList<Boundary>(r.getBoundaryManager().getBoundaries())) {
  mn.getParts().setObjects(b); mx.getParts().setObjects(b);
  double zmin = mn.getReportMonitorValue(), zmax = mx.getReportMonitorValue();
  sim.println("BOUNDARY " + b.getPresentationName() + " zmin " + zmin + " zmax " + zmax);
  if (zmax - zmin > 0.5 * L) wall = b; else if (zmax < 0.5 * L) inlet = b; else outlet = b;
}
if (inlet == null || outlet == null || wall == null) throw new RuntimeException("no pude identificar inlet/outlet/wall: ver las líneas BOUNDARY");
inlet.setPresentationName("inlet"); outlet.setPresentationName("outlet"); wall.setPresentationName("wall");
sim.getReportManager().removeObjects(mn, mx);
sim.println("RENAMED inlet outlet wall");
```

Paso 3: `{"recipe": "fisica-agua-segregado-sst"}`.

Paso 4 (malla):

```java
// PARAMS: base_m=0.008
GeometryPart cyl = sim.get(SimulationPartManager.class).getPart("pipe");
AutoMeshOperation op = sim.get(MeshOperationManager.class).createAutoMeshOperation(
    Arrays.asList("star.resurfacer.ResurfacerAutoMesher", "star.dualmesher.DualAutoMesher", "star.prismmesher.PrismAutoMesher"),
    new ArrayList<GeometryPart>(Arrays.asList(cyl)));
op.setPresentationName("malla");
op.getDefaultValues().get(BaseSize.class).setValueAndUnits(${base_m}, sim.getUnitsManager().getUnits("m"));
long t0 = System.currentTimeMillis();
op.execute();
FvRepresentation fv = (FvRepresentation) sim.getRepresentationManager().getObject("Volume Mesh");
sim.println("CELLS " + fv.getCellCount() + " MESH_SECONDS " + (System.currentTimeMillis() - t0) / 1000.0);
```

Paso 5 (fronteras y reportes; `dp` = caída de presión total):

```java
// PARAMS: v=1.0
Region r = sim.getRegionManager().getRegions().iterator().next();
Boundary inlet = r.getBoundaryManager().getBoundary("inlet"), outlet = r.getBoundaryManager().getBoundary("outlet");
inlet.setBoundaryType(sim.get(ConditionTypeManager.class).get(InletBoundary.class));
outlet.setBoundaryType(sim.get(ConditionTypeManager.class).get(PressureBoundary.class));
inlet.getValues().get(VelocityMagnitudeProfile.class).getMethod(ConstantScalarProfileMethod.class).getQuantity().setValueAndUnits(${v}, sim.getUnitsManager().getUnits("m/s"));
ReportManager rm = sim.getReportManager();
FieldFunction pt = sim.getFieldFunctionManager().getFunction("TotalPressure");
MassFlowAverageReport pi = rm.createReport(MassFlowAverageReport.class); pi.setPresentationName("pt_in"); pi.setFieldFunction(pt); pi.getParts().setObjects(inlet);
MassFlowAverageReport po = rm.createReport(MassFlowAverageReport.class); po.setPresentationName("pt_out"); po.setFieldFunction(pt); po.getParts().setObjects(outlet);
MassFlowReport qi = rm.createReport(MassFlowReport.class); qi.setPresentationName("Q_in"); qi.getParts().setObjects(inlet);
MassFlowReport qo = rm.createReport(MassFlowReport.class); qo.setPresentationName("Q_out"); qo.getParts().setObjects(outlet);
ExpressionReport dp = rm.createReport(ExpressionReport.class); dp.setPresentationName("dp"); dp.setDefinition("${pt_inReport} - ${pt_outReport}");
for (Report q : new Report[] {pi, po, qi, qo, dp}) q.createMonitor();
sim.println("INLET " + inlet.getBoundaryType().getPresentationName() + " OUTLET " + outlet.getBoundaryType().getPresentationName());
```

Paso 6: `{"recipe": "iterar-hasta-estable", "params": {"case_dir": "<casos/tuberia>", "max_iter": 300, "reportes": "\"dp\""}}`.

Paso 7 (resultados y escena de velocidad, PNG en `casos/<caso>/images`):

```java
// PARAMS: case_dir=C:/tmp/tuberia
String img = "${case_dir}/images"; new java.io.File(img).mkdirs();
ReportManager rm = sim.getReportManager();
for (String n : new String[] {"dp", "Q_in", "Q_out"}) sim.println("RESULT\t" + n + "\t" + rm.getReport(n).getReportMonitorValue());
double qi = rm.getReport("Q_in").getReportMonitorValue(), qo = rm.getReport("Q_out").getReportMonitorValue();
sim.println("BALANCE_MASA\t" + 100 * Math.abs(qi + qo) / Math.max(Math.abs(qi), 1e-30) + " %");
PlaneSection pl = (PlaneSection) sim.getPartManager().createImplicitPart(new NeoObjectVector(sim.getRegionManager().getRegions().toArray()),
    new DoubleVector(new double[] {1, 0, 0}), new DoubleVector(new double[] {0, 0, 0}), 0, 1, new DoubleVector(new double[] {0.0}));
pl.setPresentationName("plano_axial");
Scene sc = sim.getSceneManager().createScene("velocidad"); sc.initializeAndWait();
ScalarDisplayer d = sc.getDisplayerManager().createScalarDisplayer("v"); d.initialize();
d.setRepresentation(sim.getRepresentationManager().getDefaultFvRepresentation()); d.getInputParts().setObjects(pl);
d.getScalarDisplayQuantity().setFieldFunction(((PrimitiveFieldFunction) sim.getFieldFunctionManager().getFunction("Velocity")).getMagnitudeFunction());
sc.resetCamera(true);
sc.printAndWait(resolvePath(img + "/velocidad.png"), 1, 1600, 1000, true, false);
sim.println("PNG\t" + img + "/velocidad.png");
```

En la GUI: Scenes › velocidad, Reports › dp, Plots › Residuals.

---

## 2. Perfil 2D tipo NACA desde STL con 4 superficies

Estado: **compila; NO verificado con el solver** (nunca se corrió un caso con STL: `importStlPart`, `FreeStreamBoundary` y
`ForceCoefficientReport` salen del javadoc de 2410). Geometría de prueba: `starccm-agent/starccm/cad_pruebas/naca0012_mini`
(60 puntos, campo lejano 3 cuerdas) o `naca0012` (320 puntos, 10 cuerdas). STL en mm, losa de 5 mm en z, 4 superficies con
nombre: `perfil` (pared), `campo_lejano` (free stream), `simetria_z0` y `simetria_z5` (simetría). No hace falta convertir a
2D: una celda de espesor con simetría en las dos caras equivale a 2D y se malla con el trimmer.

Antes: `server_start(name="naca0012")`. Parámetros: `stl` (ruta con `/`) · `v` 10 m/s · `alfa` 0° · `cuerda_m` 0,1 ·
`espesor_m` 0,005 · `base_mm` 5 · `perfil_mm` 1. Aire, densidad constante 1,18415 kg/m³, k-ω SST all-y+, sin prismas.

| paso | qué | tiempo estimado |
|---|---|---|
| 1 | importar STL, región, lista de superficies y fronteras | 5–10 s (+ 60 s si es la primera macro de módulos en frío) |
| 2 | física de aire | ≤ 10 s (62 s en frío) |
| 3 | fronteras: free stream (v, alfa), simetrías, perfil pared | < 5 s |
| 4 | reportes Cl, Cd con área de referencia cuerda·espesor | < 5 s |
| 5 | malla trimmer, base 5 mm, perfil 1 mm (≈ 10–25 k celdas) | 10–30 s |
| 6 | `iterar-hasta-estable`, `reportes` = `"Cd"` (a 0° Cl ≈ 0, no vale) o `"Cl", "Cd"` si alfa ≠ 0 | 0,24 s/it del cilindro de 11 k celdas: 200–500 it ≈ 1–2 min |
| 7 | resultados y escena de velocidad a PNG | 5 s |

Total esperado ≈ 3–4 min con el servidor ya caliente, 5–6 en frío. Comprobación de cordura: a 0° Cl ≈ 0 por simetría.

Si el STL no trae 4 superficies (nombres genéricos o una sola): partir con `separar-caras-por-angulo` (por verificar) y
preguntar una vez qué cara es el perfil y cuál el campo lejano; no seguir si el paso 1 avisa.

Paso 1 (STL, región, fronteras):

```java
// PARAMS: stl=C:/ruta/naca0012_dominio2d_mini.stl
sim.get(PartImportManager.class).importStlPart("${stl}", "OneSurfacePerPatch", sim.getUnitsManager().getUnits("mm"), true, 1.0E-5);
GeometryPart gp = null;
for (GeometryPart q : sim.get(SimulationPartManager.class).getParts()) gp = q;
if (gp == null) throw new RuntimeException("el STL no creó ninguna parte");
for (PartSurface ps : gp.getPartSurfaces()) sim.println("SURFACE\t" + ps.getPresentationName());
if (gp.getPartSurfaces().size() != 4) sim.println("AVISO\tse esperaban 4 superficies (perfil, campo_lejano, simetria_z0, simetria_z5) y hay " + gp.getPartSurfaces().size());
sim.getRegionManager().newRegionsFromParts(new ArrayList<GeometryPart>(Arrays.asList(gp)), "OneRegionPerPart", null, "OneBoundaryPerPartSurface", null, false);
Region rg = sim.getRegionManager().getRegions().iterator().next();
rg.setPresentationName("dominio");
for (Boundary b : rg.getBoundaryManager().getBoundaries()) sim.println("BOUNDARY\t" + b.getPresentationName());
```

Paso 2 (física de aire; reusa el continuo que dejó `newRegionsFromParts`):

```java
PhysicsContinuum pc = null;
for (Continuum c0 : sim.getContinuumManager().getObjects()) if (c0 instanceof PhysicsContinuum) pc = (PhysicsContinuum) c0;
if (pc == null) pc = sim.getContinuumManager().createContinuum(PhysicsContinuum.class);
pc.setPresentationName("aire");
for (Class cl : new Class[] {ThreeDimensionalModel.class, SteadyModel.class, SingleComponentGasModel.class, SegregatedFlowModel.class, ConstantDensityModel.class,
                            TurbulentModel.class, RansTurbulenceModel.class, KOmegaTurbulence.class, SstKwTurbModel.class, KwAllYplusWallTreatment.class})
  pc.enable(cl);
for (Region rg : sim.getRegionManager().getRegions()) rg.setPhysicsContinuum(pc);
for (Model mdl : pc.getModelManager().getObjects()) sim.println("MODEL " + mdl.getPresentationName());
```

Paso 3 (fronteras):

```java
// PARAMS: v=10.0, alfa=0.0
Region rg = sim.getRegionManager().getRegions().iterator().next();
double a = Math.toRadians(${alfa});
for (Boundary b : rg.getBoundaryManager().getBoundaries()) {
  String n = b.getPresentationName().toLowerCase();
  if (n.contains("simetria")) b.setBoundaryType(sim.get(ConditionTypeManager.class).get(SymmetryBoundary.class));
  else if (n.contains("campo_lejano")) {
    b.setBoundaryType(sim.get(ConditionTypeManager.class).get(FreeStreamBoundary.class));
    b.getValues().get(VelocityMagnitudeProfile.class).getMethod(ConstantScalarProfileMethod.class).getQuantity().setValueAndUnits(${v}, sim.getUnitsManager().getUnits("m/s"));
    b.getValues().get(FlowDirectionProfile.class).getMethod(ConstantVectorProfileMethod.class).getQuantity().setComponents(Math.cos(a), Math.sin(a), 0.0);
  }
  sim.println("BOUNDARY\t" + b.getPresentationName() + "\t" + b.getBoundaryType().getPresentationName());
}
```

Paso 4 (reportes de fuerza):

```java
// PARAMS: v=10.0, alfa=0.0, cuerda_m=0.1, espesor_m=0.005
Region rg = sim.getRegionManager().getRegions().iterator().next();
Boundary perfil = null;
for (Boundary b : rg.getBoundaryManager().getBoundaries()) if (b.getPresentationName().toLowerCase().contains("perfil")) perfil = b;
if (perfil == null) throw new RuntimeException("no hay frontera «perfil»");
double a = Math.toRadians(${alfa});
double[][] dir = {{-Math.sin(a), Math.cos(a), 0.0}, {Math.cos(a), Math.sin(a), 0.0}};
String[] nom = {"Cl", "Cd"};
for (int i = 0; i < 2; i++) {
  ForceCoefficientReport fc = sim.getReportManager().createReport(ForceCoefficientReport.class);
  fc.setPresentationName(nom[i]);
  fc.getParts().setObjects(perfil);
  fc.getDirection().setComponents(dir[i][0], dir[i][1], dir[i][2]);
  fc.getReferenceDensity().setValue(1.18415);
  fc.getReferenceVelocity().setValue(${v});
  fc.getReferenceArea().setValue(${cuerda_m} * ${espesor_m});
  fc.createMonitor();
}
sim.println("REPORTES Cl y Cd sobre " + perfil.getPresentationName());
```

Paso 5 (malla trimmer, perfil más fino):

```java
// PARAMS: base_mm=5, perfil_mm=1
GeometryPart gp = null;
for (GeometryPart q : sim.get(SimulationPartManager.class).getParts()) gp = q;
AutoMeshOperation op = sim.get(MeshOperationManager.class).createAutoMeshOperation(
    Arrays.asList("star.resurfacer.ResurfacerAutoMesher", "star.trimmer.TrimmerAutoMesher"), new ArrayList<GeometryPart>(Arrays.asList(gp)));
op.setPresentationName("malla");
op.getDefaultValues().get(BaseSize.class).setValueAndUnits(${base_mm}, sim.getUnitsManager().getUnits("mm"));
SurfaceCustomMeshControl sc = op.getCustomMeshControls().createSurfaceControl();
sc.setPresentationName("perfil");
List<GeometryObject> pf = new ArrayList<GeometryObject>();
for (PartSurface ps : gp.getPartSurfaces()) if (ps.getPresentationName().toLowerCase().contains("perfil")) pf.add(ps);
sc.getGeometryObjects().setObjects(pf);
sc.getCustomConditions().get(PartsTargetSurfaceSizeOption.class).setSelected(PartsTargetSurfaceSizeOption.Type.CUSTOM);
sc.getCustomValues().get(PartsTargetSurfaceSize.class).getRelativeSize().setPercentage(100.0 * ${perfil_mm} / ${base_mm});
long t0 = System.currentTimeMillis();
op.execute();
FvRepresentation fv = (FvRepresentation) sim.getRepresentationManager().getObject("Volume Mesh");
sim.println("CELLS " + fv.getCellCount() + " MESH_SECONDS " + (System.currentTimeMillis() - t0) / 1000.0);
```

Paso 6: `{"recipe": "iterar-hasta-estable", "params": {"case_dir": "<casos/naca0012>", "max_iter": 500, "reportes": "\"Cd\""}}`.

Paso 7 (resultados y escena de velocidad en el plano medio de la losa):

```java
// PARAMS: case_dir=C:/tmp/naca0012, espesor_m=0.005
String img = "${case_dir}/images"; new java.io.File(img).mkdirs();
ReportManager rm = sim.getReportManager();
for (String n : new String[] {"Cl", "Cd"}) sim.println("RESULT\t" + n + "\t" + rm.getReport(n).getReportMonitorValue());
PlaneSection pl = (PlaneSection) sim.getPartManager().createImplicitPart(new NeoObjectVector(sim.getRegionManager().getRegions().toArray()),
    new DoubleVector(new double[] {0, 0, 1}), new DoubleVector(new double[] {0, 0, 0.5 * ${espesor_m}}), 0, 1, new DoubleVector(new double[] {0.0}));
pl.setPresentationName("plano_medio");
Scene sc = sim.getSceneManager().createScene("velocidad"); sc.initializeAndWait();
ScalarDisplayer d = sc.getDisplayerManager().createScalarDisplayer("v"); d.initialize();
d.setRepresentation(sim.getRepresentationManager().getDefaultFvRepresentation()); d.getInputParts().setObjects(pl);
d.getScalarDisplayQuantity().setFieldFunction(((PrimitiveFieldFunction) sim.getFieldFunctionManager().getFunction("Velocity")).getMagnitudeFunction());
sc.resetCamera(true);
sc.printAndWait(resolvePath(img + "/velocidad.png"), 1, 1600, 1000, true, false);
sim.println("PNG\t" + img + "/velocidad.png");
```

En la GUI: Scenes › velocidad, Reports › Cl y Cd, Plots › Residuals. Si el paso 3 falla porque la frontera free stream no
acepta esas condiciones en flujo segregado: partir `campo_lejano` por ángulo en entrada y salida
(`separar-caras-por-angulo`) y usar velocity inlet + pressure outlet como en la receta 1; anotarlo en `recipe_save`.

---

## 3. La misma bomba con otro caudal (solo cambia `Flujo`)

Estado: el parámetro `Flujo` y los reportes los crea `pump_case` (**verificado** el esquema en BombaRapida; la receta
`fronteras-presion-total-caudal-salida` lo declara verificado en copia de cursocfturbo). Cambiar el valor y seguir desde la
solución anterior **no se ha corrido**: se espera que converja más rápido que desde cero porque parte de un campo cercano.

Con el servidor vivo y la bomba ya iterada: NO volver a importar, mallar ni configurar física, fronteras o reportes. Un
solo `run_steps` con tantos tripletes (cambiar → iterar → leer) como caudales. Parámetros: `kg_s` = ρ·Q/3600
(ρ de `ccm["density"]`, 998,2 en agua), `q_m3h` solo para la etiqueta. Curva de BombaRapida (50 m³/h = 13,864 kg/s):

| fracción | 0,6 | 0,8 | 1,0 | 1,15 | 1,3 |
|---|---|---|---|---|---|
| Q (m³/h) | 30 | 40 | 50 | 57,5 | 65 |
| `kg_s` | 8,318 | 11,091 | 13,864 | 15,944 | 18,023 |

| paso (por caudal) | qué | tiempo estimado |
|---|---|---|
| a | cambiar `Flujo` | < 1 s |
| b | `iterar-hasta-estable` (`reportes` `"H", "M"`, `max_iter` 600) | 100–250 it × 0,46 s/it (BombaRapida, 23 s por 50 it) = 50–115 s |
| c | leer el punto (`CURVA`) | < 1 s |
| final | `post-bomba` una sola vez, tras el último punto | 15–30 s |

Cinco puntos ≈ 5–9 min en UNA llamada, una sola vez la inyección (0,7–1 s con el cliente residente), frente a 15 llamadas
sueltas. Preguntar antes (curva de caudales es «caro»): cifra de tiempo en la pregunta. Si un punto avisa `ITER_STOP diverge`
o `max`, el resto de pasos igual corre: mirar `ITER_STOP` de cada uno y repetir ese punto desde la solución del anterior.

Paso a (cambiar el caudal):

```java
// PARAMS: kg_s=11.091
ScalarGlobalParameter f = (ScalarGlobalParameter) sim.get(GlobalParameterManager.class).getObject("Flujo");
double antes = f.getQuantity().getValue();
f.getQuantity().setValue(${kg_s});
sim.println("FLUJO\t" + antes + " -> " + ${kg_s} + " kg/s");
```

Paso b: `{"recipe": "iterar-hasta-estable", "params": {"case_dir": "<casos/caso>", "max_iter": 600, "reportes": "\"H\", \"M\""}}`.

Paso c (leer el punto de la curva):

```java
// PARAMS: q_m3h=40
ReportManager rm = sim.getReportManager();
StringBuilder sb = new StringBuilder("CURVA\t${q_m3h} m3/h");
for (String n : new String[] {"H", "M", "P_hid", "P_eje", "eta"}) { try { sb.append("\t").append(n).append(" ").append(rm.getReport(n).getReportMonitorValue()); } catch (Exception e) { } }
sb.append("\tit ").append(sim.getSimulationIterator().getCurrentIteration());
sim.println(sb.toString());
```

Paso final: `{"recipe": "post-bomba", "params": {"case_dir": "<casos/caso>"}}`. Después `save` y, si no hay más trabajo,
`server_stop`. Anotar la curva en `casos/<caso>/RESUMEN.md` (tabla Q, H, M, η).

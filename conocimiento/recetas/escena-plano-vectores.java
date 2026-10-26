// QUE: escena de plano de corte (plano_medio, a media salida del rotor) con |velocidad| y vectores encima, exportable a PNG en batch
// VERSION: 19.06.009-R8
// FECHA: 2026-10-23 · VERIFICADO contra el solver real (cases/cursocfturbo/images/plano_vectores.png)
// Reusa el plano «plano_medio» si existe (lo crea bomba-escenas.java); si no, lo crea sobre todas las regiones.
// Vectores: longitud relativa al modelo en % (1.0 = invisible; 6.0 se ve bien), espaciado GEOMETRIC (setSpacingMode(0);
// 1 = UNIFORM) con onRatio 6 (una flecha de cada 6 celdas). PartManager no tiene getParts(): usar getObjects().
List<Region> regs = new ArrayList<Region>(sim.getRegionManager().getRegions());
PlaneSection plane = null;
for (Object o : sim.getPartManager().getObjects()) if (o instanceof PlaneSection && ((PlaneSection) o).getPresentationName().equals("plano_medio")) plane = (PlaneSection) o;
if (plane == null) throw new RuntimeException("falta plano_medio: corre antes exportar-superficies-ensight con PLANOS auto (la cota fija de cursocfturbo corta mal otras bombas)");
Scene sc = sim.getSceneManager().createScene("plano_vectores"); sc.initializeAndWait();   // queda como «plano_vectores 1»
ScalarDisplayer sd = sc.getDisplayerManager().createScalarDisplayer("v"); sd.initialize();
sd.setRepresentation(sim.getRepresentationManager().getDefaultFvRepresentation());
sd.getInputParts().setObjects(plane);
sd.getScalarDisplayQuantity().setFieldFunction(((PrimitiveFieldFunction) sim.getFieldFunctionManager().getFunction("Velocity")).getMagnitudeFunction());
VectorDisplayer vd = sc.getDisplayerManager().createVectorDisplayer("vectores"); vd.initialize();
vd.setRepresentation(sim.getRepresentationManager().getDefaultFvRepresentation());
vd.getInputParts().setObjects(plane);
vd.getVectorDisplayQuantity().setFieldFunction(sim.getFieldFunctionManager().getFunction("Velocity"));
vd.setColorMode(VectorDisplayer.COLOR_USER_DEFINED);
vd.setSpacingMode(0);                                   // GEOMETRIC
GlyphSettings g = vd.getGlyphSettings();
g.setRelativeToModelLength(6.0);                        // % del tamaño del modelo
g.getGeometricSpacing().setOnRatio(6);
sc.resetCamera(true);
sim.println("SCENE " + sc.getPresentationName() + " vectores " + vd.getPresentationName() + " " + g.getSpacingModeEnum() + " rel " + g.getRelativeToModelLength());

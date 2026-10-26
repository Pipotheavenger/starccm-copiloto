// QUE: escenas del FLUIDO como las de las plantillas NQ25Q159H91: en cada plano (plano_medio, plano_meridional) una
//      escena con TotalHead + líneas de corriente de la velocidad RELATIVA y otra con vectores de velocidad relativa;
//      las paredes van al 12 % de opacidad como referencia.
// VERSION: 19.06.009-R8 · FECHA: 2026-10-24 · ESTADO: verificado con el solver en una copia de cursocfturbo (4 escenas, PNG)
// Antes: exportar-superficies-ensight con PLANOS «auto» (crea los planos en su sitio) y reportes-euler-triangulos (crea
// TotalHead). Velocidad relativa = la que ve el rodete que gira (en las regiones fijas es la absoluta): es la que
// enseña cómo entra el agua a los álabes; la absoluta en el rotor está dominada por el giro (ω·r).
// Escenas: fluido_<plano> y vectores_<plano> (STAR-CCM+ les añade « 1»). PNG: export_scene.
FieldFunctionManager ffm = sim.getFieldFunctionManager();
FieldFunction th = null, vrel = null;
for (String nm : new String[] {"TotalHead", "TotalPressure"}) { if (th == null) try { th = ffm.getFunction(nm); } catch (Exception e) { } }
for (String nm : new String[] {"RelativeVelocity", "CellRelativeVelocity", "Velocity"}) { if (vrel == null) try { vrel = ffm.getFunction(nm); } catch (Exception e) { } }
sim.println("CAMPOS\t" + th.getPresentationName() + "\t" + vrel.getPresentationName());
java.util.List<Boundary> walls = new java.util.ArrayList<Boundary>();
for (Region r : sim.getRegionManager().getRegions()) for (Boundary b : r.getBoundaryManager().getBoundaries()) if (b.getBoundaryType() instanceof WallBoundary) walls.add(b);
int hechos = 0;
for (String pn : new String[] {"plano_medio", "plano_meridional"}) {
  PlaneSection pl = null;
  for (Object o : sim.getPartManager().getObjects()) if (o instanceof PlaneSection && ((PlaneSection) o).getPresentationName().equals(pn)) pl = (PlaneSection) o;
  if (pl == null) { sim.println("WARN\tfalta " + pn + ": corre antes exportar-superficies-ensight con PLANOS auto"); continue; }
  String tag = pn.replace("plano_", "");
  ConstrainedStreamPart sp = null;
  for (Object o : sim.getPartManager().getObjects()) if (o instanceof ConstrainedStreamPart && ((ConstrainedStreamPart) o).getPresentationName().equals("lineas_" + tag)) sp = (ConstrainedStreamPart) o;
  if (sp == null) {
    // 12×12 semillas: con 25×25 las líneas tapaban el color de TotalHead (verificado en cursocfturbo)
    sp = sim.getPartManager().createConstrainedStreamPart(new java.util.Vector<Object>(java.util.Arrays.asList(pl)), pl, vrel, 12, 12);
    sp.setPresentationName("lineas_" + tag);
  }
  Scene s1 = sim.getSceneManager().createScene("fluido_" + tag); s1.initializeAndWait();
  PartDisplayer pd = s1.getDisplayerManager().createPartDisplayer("paredes", -1, 4); pd.initialize();
  pd.setRepresentation(sim.getRepresentationManager().getDefaultFvRepresentation()); pd.getInputParts().setObjects(walls); pd.setOpacity(0.12);
  ScalarDisplayer sd = s1.getDisplayerManager().createScalarDisplayer("altura"); sd.initialize();
  sd.setRepresentation(sim.getRepresentationManager().getDefaultFvRepresentation()); sd.getInputParts().setObjects(pl); sd.getScalarDisplayQuantity().setFieldFunction(th);
  StreamDisplayer st = s1.getDisplayerManager().createStreamDisplayer("lineas"); st.initialize();
  st.getInputParts().setObjects(sp);
  try { st.getScalarDisplayQuantity().setFieldFunction(((PrimitiveFieldFunction) vrel).getMagnitudeFunction()); } catch (Exception e) { }  // color = |v relativa|
  s1.resetCamera(true);
  Scene s2 = sim.getSceneManager().createScene("vectores_" + tag); s2.initializeAndWait();
  PartDisplayer pd2 = s2.getDisplayerManager().createPartDisplayer("paredes", -1, 4); pd2.initialize();
  pd2.setRepresentation(sim.getRepresentationManager().getDefaultFvRepresentation()); pd2.getInputParts().setObjects(walls); pd2.setOpacity(0.12);
  VectorDisplayer vd = s2.getDisplayerManager().createVectorDisplayer("vrel"); vd.initialize();
  vd.setRepresentation(sim.getRepresentationManager().getDefaultFvRepresentation()); vd.getInputParts().setObjects(pl);
  vd.getVectorDisplayQuantity().setFieldFunction(vrel);
  vd.getGlyphSettings().setRelativeToModelLength(6.0); vd.getGlyphSettings().setSpacingMode(0); vd.getGlyphSettings().getGeometricSpacing().setOnRatio(6);
  s2.resetCamera(true);
  hechos++;
}
for (Scene s : sim.getSceneManager().getScenes()) sim.println("SCENE\t" + s.getPresentationName());
sim.println("ESCENAS_FLUIDO\t" + hechos + " planos");

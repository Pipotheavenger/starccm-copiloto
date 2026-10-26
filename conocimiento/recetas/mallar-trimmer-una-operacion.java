// QUE: malla como la de las plantillas NQ25Q159H91: UNA operación para todas las partes, remallador + trimmer
//      (hexaédrica) + capas prismáticas, y sin prismas en las interfaces (superficies coincidentes rotor/estator).
// VERSION: 19.06.009-R8 · FECHA: 2026-10-24 · ESTADO: compila con javac; NO probada todavía con el solver
// Alternativa a la de una operación por parte con poliedros (bomba-malla). Trimmer: menos celdas por volumen y celdas
// alineadas, bueno en tubos y volutas grandes; poliedros: mejores en geometrías retorcidas. Ojo: con los STEP de
// CFturbo una sola operación poliédrica falló por las caras coincidentes («Surface intersects self»); si falla aquí,
// volver a una operación por parte.
// BASE (mm), CAPAS (n), ESTIRAMIENTO, ESPESOR (% del base), SIN_PRISMAS: regex de superficies sin capas
// (por defecto interfaces: «(?i)interface|inflow|outflow»).
double base = BASE; int capas = CAPAS; double estir = ESTIRAMIENTO, esp = ESPESOR;
java.util.regex.Pattern sinp = java.util.regex.Pattern.compile("SIN_PRISMAS");
MeshOperationManager mom = sim.get(MeshOperationManager.class);
java.util.List<GeometryPart> parts = new java.util.ArrayList<GeometryPart>(sim.get(SimulationPartManager.class).getLeafParts());
AutoMeshOperation op = mom.createAutoMeshOperation(
    java.util.Arrays.asList("star.resurfacer.ResurfacerAutoMesher", "star.prismmesher.PrismAutoMesher", "star.trimmer.TrimmerAutoMesher"), parts);
op.setPresentationName("malla_trimmer");
op.getDefaultValues().get(BaseSize.class).setValueAndUnits(base, sim.getUnitsManager().getUnits("mm"));
op.getDefaultValues().get(NumPrismLayers.class).setNumLayers(capas);
op.getDefaultValues().get(PrismLayerStretching.class).setStretching(estir);
op.getDefaultValues().get(PrismThickness.class).setRelativeSize(esp);
java.util.List<GeometryObject> sin = new java.util.ArrayList<GeometryObject>();
for (GeometryPart p : parts) for (PartSurface s : p.getPartSurfaces()) if (sinp.matcher(s.getPresentationName()).find()) sin.add(s);
if (!sin.isEmpty()) {
  SurfaceCustomMeshControl sc = op.getCustomMeshControls().createSurfaceControl();
  sc.setPresentationName("sin_prismas");
  sc.getGeometryObjects().setObjects(sin);
  sc.getCustomConditions().get(PartsCustomizePrismMesh.class).getCustomPrismOptions().setSelected(PartsCustomPrismsOption.Type.DISABLE);
}
sim.println("MESH_OP\tmalla_trimmer\t" + parts.size() + " partes\t" + sin.size() + " superficies sin prismas\tbase " + base + " mm");
long t0 = System.currentTimeMillis();
op.execute();
FvRepresentation fv = (FvRepresentation) sim.getRepresentationManager().getObject("Volume Mesh");
sim.println("CELLS " + fv.getCellCount() + "\tMESH_SECONDS " + (System.currentTimeMillis() - t0) / 1000.0);

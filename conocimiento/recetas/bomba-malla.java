// QUE: UNA operación de malla POR PARTE (las 3 juntas fallan: 'Surface intersects self' en las caras coincidentes de las interfaces): remallador + reparación automática + poliédrica + 5 prismas (1,3; 10 % del base), base 5.0 mm = D2/54, álabes al 50 %
// VERSION: 19.06.009-R8
// FECHA: 2026-10-23

Units mm = sim.getUnitsManager().getUnits("mm");
MeshOperationManager mom = sim.get(MeshOperationManager.class);
for (GeometryPart part : sim.get(SimulationPartManager.class).getParts()) {
  String n = part.getPresentationName().contains("Stator") ? "estator" : part.getPresentationName().contains("Impeller") ? "rotor" : "voluta";
  AutoMeshOperation op = mom.createAutoMeshOperation(
      Arrays.asList("star.resurfacer.ResurfacerAutoMesher", "star.resurfacer.AutomaticSurfaceRepairAutoMesher", "star.dualmesher.DualAutoMesher", "star.prismmesher.PrismAutoMesher"),
      new ArrayList<GeometryPart>(Arrays.asList(part)));
  op.setPresentationName("malla_" + n);
  op.getDefaultValues().get(BaseSize.class).setValueAndUnits(5.0, mm);
  op.getDefaultValues().get(NumPrismLayers.class).setNumLayers(5);
  op.getDefaultValues().get(PrismLayerStretching.class).setStretching(1.3);
  op.getDefaultValues().get(PrismThickness.class).setRelativeSize(10.0);
  if (n.equals("rotor")) {
    SurfaceCustomMeshControl sc = op.getCustomMeshControls().createSurfaceControl();
    sc.setPresentationName("alabes");
    List<GeometryObject> blades = new ArrayList<GeometryObject>();
    for (PartSurface s : part.getPartSurfaces()) if (s.getPresentationName().contains("Blade")) blades.add(s);
    sc.getGeometryObjects().setObjects(blades);
    sc.getCustomConditions().get(PartsTargetSurfaceSizeOption.class).setSelected(PartsTargetSurfaceSizeOption.Type.CUSTOM);
    sc.getCustomValues().get(PartsTargetSurfaceSize.class).getRelativeSize().setPercentage(50.0);
  }
  long t0 = System.currentTimeMillis();
  op.execute();
  sim.println("MESH_SECONDS " + n + " " + (System.currentTimeMillis() - t0) / 1000.0);
}
FvRepresentation fv = (FvRepresentation) sim.getRepresentationManager().getObject("Volume Mesh");
sim.println("CELLS " + fv.getCellCount());

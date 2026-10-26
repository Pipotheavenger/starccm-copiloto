// QUE: operación de malla automática: remallador + poliédrica + prismas, tamaño base dado, y la ejecuta
// VERSION: 19.06.009-R8
// FECHA: 2026-10-23

Units um = sim.getUnitsManager().getUnits("m");
GeometryPart cyl = sim.get(SimulationPartManager.class).getPart("pipe");
AutoMeshOperation op = sim.get(MeshOperationManager.class).createAutoMeshOperation(
    Arrays.asList("star.resurfacer.ResurfacerAutoMesher", "star.dualmesher.DualAutoMesher", "star.prismmesher.PrismAutoMesher"),
    new ArrayList<GeometryPart>(Arrays.asList(cyl)));
op.setPresentationName("malla");
op.getDefaultValues().get(BaseSize.class).setValueAndUnits(0.008, um);
long t0 = System.currentTimeMillis();
op.execute();
sim.println("MESH_SECONDS " + (System.currentTimeMillis() - t0) / 1000.0);
try {
  FvRepresentation fv = (FvRepresentation) sim.getRepresentationManager().getObject("Volume Mesh");
  sim.println("CELLS " + fv.getCellCount());
} catch (Exception e) { sim.println("CELLS n/a " + e.getMessage()); }

// QUE: importa los 3 STEP de CFturbo como partes de geometría con sus caras nombradas (PartImportManager)
// VERSION: 19.06.009-R8
// FECHA: 2026-10-23

PartImportManager pim = sim.get(PartImportManager.class);
for (String f : new String[] {"cursocfturbo_Co1.stp", "cursocfturbo_Co2.stp", "cursocfturbo_Co3.stp"})
  pim.importCadPart("C:/Users/sebas/OneDrive/Documentos/starccm-agent/starccm/cases/cursocfturbo/cad/" + f, "SharpEdges", 30.0, 2, 1.0E-5);
for (GeometryPart p : sim.get(SimulationPartManager.class).getParts()) {
  sim.println("PART\t" + p.getPresentationName() + "\t" + p.getClass().getSimpleName());
  for (PartSurface s : p.getPartSurfaces()) sim.println("SURFACE\t" + p.getPresentationName() + "/" + s.getPresentationName());
}

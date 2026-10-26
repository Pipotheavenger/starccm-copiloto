// QUE: imprime regiones, fronteras, continuos y reportes de la simulación activa (base de la tool inspect)
// VERSION: 2606.0002
// ESTADO: no verificado
sim.println("REGIONS");
for (Region r : sim.getRegionManager().getRegions()) {
  sim.println("  " + r.getPresentationName() + " [" + r.getClass().getSimpleName() + "]");
  for (Boundary b : r.getBoundaryManager().getBoundaries()) {
    sim.println("    " + b.getPresentationName() + " : " + b.getBoundaryType().getClass().getSimpleName());
  }
}
sim.println("CONTINUA");
for (Continuum c : sim.getContinuumManager().getObjects()) {
  sim.println("  " + c.getPresentationName());
  if (c instanceof PhysicsContinuum) {
    for (Model m : ((PhysicsContinuum) c).getModelManager().getObjects()) sim.println("    " + m.getPresentationName());
  }
}
sim.println("REPORTS");
for (Report rep : sim.getReportManager().getObjects()) {
  sim.println("  " + rep.getPresentationName() + " = " + rep.getReportMonitorValue());
}

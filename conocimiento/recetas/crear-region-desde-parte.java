// QUE: crea una región por parte y una frontera por superficie de parte, sin interfaces
// VERSION: 19.06.009-R8
// FECHA: 2026-10-23

GeometryPart cyl = sim.get(SimulationPartManager.class).getPart("pipe");
sim.getRegionManager().newRegionsFromParts(new ArrayList<GeometryPart>(Arrays.asList(cyl)),
    "OneRegionPerPart", null, "OneBoundaryPerPartSurface", null, false);
for (Region r : sim.getRegionManager().getRegions()) {
  sim.println("REGION " + r.getPresentationName());
  for (Boundary b : r.getBoundaryManager().getBoundaries()) sim.println("  BOUNDARY " + b.getPresentationName());
}

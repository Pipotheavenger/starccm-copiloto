// QUE: una región por parte, una frontera por superficie; renombra regiones estator/rotor/voluta y fronteras por el sufijo tras el último '-'
// VERSION: 19.06.009-R8
// FECHA: 2026-10-23

Collection<GeometryPart> parts = sim.get(SimulationPartManager.class).getParts();
sim.getRegionManager().newRegionsFromParts(new ArrayList<GeometryPart>(parts), "OneRegionPerPart", null, "OneBoundaryPerPartSurface", null, false);
for (Region r : sim.getRegionManager().getRegions()) {
  String n = r.getPresentationName();
  if (n.contains("Stator")) r.setPresentationName("estator");
  else if (n.contains("Impeller")) r.setPresentationName("rotor");
  else if (n.contains("Volute")) r.setPresentationName("voluta");
  for (Boundary b : new ArrayList<Boundary>(r.getBoundaryManager().getBoundaries())) {
    String bn = b.getPresentationName();
    int i = bn.lastIndexOf('-');
    if (i >= 0) b.setPresentationName(bn.substring(i + 1));
  }
  sim.println("REGION\t" + r.getPresentationName());
  for (Boundary b : r.getBoundaryManager().getBoundaries()) sim.println("BOUNDARY\t" + r.getPresentationName() + "/" + b.getPresentationName());
}

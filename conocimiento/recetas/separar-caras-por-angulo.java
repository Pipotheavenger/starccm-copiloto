// QUE: separar las caras agrupadas de la geometría por ángulo (como «Split by angle»), para que entrada, salida y paredes sean fronteras distintas que el usuario pueda elegir en el visor
// VERSION: 19.06.009-R8 y 2502 · FECHA: 2026-10-24 · ESTADO: compila con javac; por verificar contra el solver real
// ANGULO: ángulo de rasgo en grados (89 separa caras planas de las curvas que las rodean; 30 separa más).
// Con malla: parte las FRONTERAS (MeshManager.splitBoundariesByAngle). Sin malla (lanza excepción): parte las
// SUPERFICIES DE PARTE de las CadPart (PartSurfaceManager.splitPartSurfacesByAngle), antes de newRegionsFromParts.
double ang = ANGULO;
int antes = 0, despues = 0;
java.util.List<Boundary> bnds = new java.util.ArrayList<Boundary>();
for (Region r : sim.getRegionManager().getRegions())
  for (Boundary b : r.getBoundaryManager().getBoundaries())
    if (!(b instanceof DirectBoundaryInterfaceBoundary) && !b.getPresentationName().contains("[")) bnds.add(b);
boolean hecho = false;
if (!bnds.isEmpty()) {
  try {
    antes = bnds.size();
    sim.getMeshManager().splitBoundariesByAngle(ang, bnds);
    for (Region r : sim.getRegionManager().getRegions()) despues += r.getBoundaryManager().getBoundaries().size();
    sim.println("SPLIT\tfronteras\t" + antes + "\t" + despues);
    hecho = true;
  } catch (Exception e) { sim.println("WARN\tsin malla para partir fronteras: " + e); antes = 0; despues = 0; }
}
if (!hecho) {
  for (Object o : sim.get(SimulationPartManager.class).getObjects()) {
    if (!(o instanceof CadPart)) continue;  // las partes importadas de STEP/Parasolid
    CadPart gp = (CadPart) o;
    java.util.Collection<PartSurface> ps = gp.getPartSurfaces();
    antes += ps.size();
    gp.getPartSurfaceManager().splitPartSurfacesByAngle(ps, ang);
    despues += gp.getPartSurfaces().size();
  }
  sim.println("SPLIT\tsuperficies de parte\t" + antes + "\t" + despues);
}
for (Region r : sim.getRegionManager().getRegions())
  for (Boundary b : r.getBoundaryManager().getBoundaries()) sim.println("BOUNDARY\t" + r.getPresentationName() + "/" + b.getPresentationName());

// QUE: activar la exportación automática de superficies con campo (EnSight) cada N iteraciones, para el 3D en vivo del visor
// VERSION: 19.06.009-R8 · FECHA: 2026-10-24 · ESTADO: compila con javac; verificado con el solver local (ver CLAUDE.md)
// CADA: cada cuántas iteraciones (50 recomendado). OUT: carpeta destino (cases/<caso>/surfaces/_raw/vivo).
// FUNCS: field functions escalares separadas por coma (los vectores se exportan como magnitud).
// Mismas fronteras que exportar-superficies-ensight (sin interfaces). Para apagarla: ae.setEnabled(false).
int cada = CADA;
AutoExport ae = sim.getSimulationIterator().getAutoExport();
java.util.Vector<Boundary> bnds = new java.util.Vector<Boundary>();
StringBuilder meta = new StringBuilder();
for (Region r : sim.getRegionManager().getRegions())
  for (Boundary b : r.getBoundaryManager().getBoundaries()) {
    if (b instanceof DirectBoundaryInterfaceBoundary || b.getPresentationName().contains("[")) continue;
    bnds.add(b); meta.append("BOUNDARY\t" + r.getPresentationName() + "/" + b.getPresentationName() + "\n");
  }
java.util.Vector<ClientServerObject> ffs = new java.util.Vector<ClientServerObject>();
for (String n : "FUNCS".split(",")) {
  n = n.trim(); if (n.isEmpty()) continue;
  try {
    FieldFunction f = sim.getFieldFunctionManager().getFunction(n);
    try { if (f.getType().toString().toLowerCase().contains("vector")) f = f.getMagnitudeFunction(); } catch (Exception e) { }
    ffs.add(f);
  } catch (Exception e) { sim.println("WARN\tno existe la field function " + n); }
}
for (Region r : sim.getRegionManager().getRegions()) {  // marcos rotatorios -> el visor gira cada región sobre su eje
  try {
    Object rf = r.getValues().get(MotionSpecification.class).getReferenceFrame();
    if (rf instanceof RotatingReferenceFrame) {
      RotatingReferenceFrame f = (RotatingReferenceFrame) rf;
      DoubleVector ax = f.getAxisDirection().getSIVector(), og = f.getAxisOrigin().getSIVector();
      meta.append("ROTFRAME\t" + r.getPresentationName() + "\t" + ax.get(0) + "," + ax.get(1) + "," + ax.get(2) + "\t" + og.get(0) + "," + og.get(1) + "," + og.get(2) + "\t" + (f.getRotationRate().getSIValue() * 60.0 / (2.0 * Math.PI)) + "\n");
    }
  } catch (Exception e) { }
}
ae.getSolutionExportFormat().setSelected(SolutionExportFormat.Type.CASE);
ae.setRegions(new java.util.Vector<Region>());
ae.setBoundaries(bnds);
ae.setScalars(ffs);
ae.setOptionDataAtVerts(true);
ae.setOptionAppendToFile(false);
ae.setExportDirectory(resolvePath("OUT"));
ae.setBaseName("vivo");
ae.getStarUpdate().getUpdateModeOption().setSelected(StarUpdateModeOption.Type.ITERATION);
ae.getStarUpdate().getIterationUpdateFrequency().setIterations(cada);
ae.setEnabled(true);
try { new java.io.File(resolvePath("OUT")).mkdirs();
      java.nio.file.Files.write(java.nio.file.Paths.get(resolvePath("OUT"), "vivo.meta"), meta.toString().getBytes("UTF-8")); }
catch (Exception e) { sim.println("WARN\tno pude escribir vivo.meta: " + e); }
sim.println("AUTOEXPORT\t" + cada + "\t" + bnds.size() + " fronteras\t" + ffs.size() + " campos\t" + ae.getExportDirectory());

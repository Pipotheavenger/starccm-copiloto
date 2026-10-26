// Exporta fronteras (malla superficial + campos en los vértices) a EnSight Gold (.case) para el visor 3D de ui/.
// STAR-CCM+ 2410 y 2502 · 2026-10-24 · VERIFICADO contra el solver real (export) · el .meta compila con javac.
// Parámetros del cuerpo: PATTERN (regex sobre "region/frontera"), FUNCS (nombres internos de field functions;
// los vectores se exportan como magnitud), OUT (ruta .case). Firma usada (javadoc 2410):
// ImportManager.export(String, Collection<Region>, Collection<Boundary>, Collection<Part>, Collection<PartSurface>,
//                      Collection<FieldFunction>, boolean appendToFile, boolean dataAtVerts)
StringBuilder meta = new StringBuilder();  // BOUNDARY/ROTFRAME también en <OUT>.meta: el visor lo lee aunque la macro la lance la IA
java.util.regex.Pattern pat = java.util.regex.Pattern.compile("PATTERN", java.util.regex.Pattern.CASE_INSENSITIVE);
java.util.Vector<Region> regs = new java.util.Vector<Region>();
java.util.Vector<Boundary> bnds = new java.util.Vector<Boundary>();
for (Region r : sim.getRegionManager().getRegions())
  for (Boundary b : r.getBoundaryManager().getBoundaries()) {
    // las fronteras de interfaz (rotor/estator) duplican superficies internas: fuera del visor
    if (b instanceof DirectBoundaryInterfaceBoundary || b.getPresentationName().contains("[")) continue;
    String full = r.getPresentationName() + "/" + b.getPresentationName();
    if (pat.matcher(full).find()) { bnds.add(b); sim.println("BOUNDARY\t" + full); meta.append("BOUNDARY\t" + full + "\n"); }
  }
if (bnds.isEmpty()) throw new RuntimeException("Ninguna frontera coincide con PATTERN");
java.util.Vector<FieldFunction> ffs = new java.util.Vector<FieldFunction>();
for (String n : "FUNCS".split(",")) {
  n = n.trim(); if (n.isEmpty()) continue;
  FieldFunction f;
  try { f = sim.getFieldFunctionManager().getFunction(n); }
  catch (Exception e) { sim.println("WARN\tno existe la field function " + n); continue; }
  // vector (Velocity, WallShearStress...) -> su magnitud, que es lo que pinta el visor
  try { if (f.getType().toString().toLowerCase().contains("vector")) f = f.getMagnitudeFunction(); }
  catch (Exception e) { }
  ffs.add(f); sim.println("FUNC\t" + f.getPresentationName());
}
// PLANOS (opcional, 2026-10-24): cortes por el FLUIDO para ver el campo dentro del volumen y no solo en las paredes
// (en una pared sin deslizamiento la velocidad es la de la pared: 0 en la voluta, ω·r en el rotor). Formato
// «nombre:nx,ny,nz,ox,oy,oz;…» (normal y origen en m), p.ej. «medio:0,0,1,0,0,0.03;meridional:1,0,0,0,0,0».
// Se crean (o se reusan) como plano_<nombre> sobre todas las regiones y salen en el visor en el grupo «Planos».
// «auto»: sin cotas a mano (una cota copiada de otra bomba corta por el sitio equivocado): eje y origen del marco
// rotatorio; «medio» perpendicular al eje a media anchura de la SALIDA del rotor (sus fronteras Outflow/outlet,
// medidas en STAR-CCM+ con Min/Max de la coordenada axial) y «meridional» que contiene el eje.
java.util.Vector<Part> planes = new java.util.Vector<Part>();
String planos = "PLANOS";
if (planos.equals("PLANO" + "S")) planos = "";  // sin sustituir: ningún plano
if (planos.trim().equalsIgnoreCase("auto")) {
  double[] ax = {0, 0, 1}, og = {0, 0, 0}; Region rot = null;
  for (Region r : sim.getRegionManager().getRegions()) {
    try {
      Object rf = r.getValues().get(MotionSpecification.class).getReferenceFrame();
      if (rf instanceof RotatingReferenceFrame) {
        DoubleVector a = ((RotatingReferenceFrame) rf).getAxisDirection().getSIVector(), o = ((RotatingReferenceFrame) rf).getAxisOrigin().getSIVector();
        double n = Math.sqrt(a.get(0) * a.get(0) + a.get(1) * a.get(1) + a.get(2) * a.get(2));
        ax = new double[] {a.get(0) / n, a.get(1) / n, a.get(2) / n}; og = new double[] {o.get(0), o.get(1), o.get(2)}; rot = r; break;
      }
    } catch (Exception e) { }
  }
  java.util.Vector<Boundary> outs = new java.util.Vector<Boundary>();
  if (rot != null) {
    for (Boundary b : rot.getBoundaryManager().getBoundaries()) if (b.getPresentationName().toLowerCase().matches(".*(outflow|outlet|salida).*")) outs.add(b);
    if (outs.isEmpty()) outs.addAll(rot.getBoundaryManager().getBoundaries());
  }
  double mid = ax[0] * og[0] + ax[1] * og[1] + ax[2] * og[2];
  if (!outs.isEmpty()) {
    UserFieldFunction uf = sim.getFieldFunctionManager().createFieldFunction();
    uf.setPresentationName("agente_coord_axial"); uf.setFunctionName("agente_coord_axial");
    uf.setDefinition("dot($$Position, [" + ax[0] + ", " + ax[1] + ", " + ax[2] + "])");
    MaxReport mx = sim.getReportManager().createReport(MaxReport.class); mx.setFieldFunction(uf); mx.getParts().setObjects(outs);
    MinReport mn = sim.getReportManager().createReport(MinReport.class); mn.setFieldFunction(uf); mn.getParts().setObjects(outs);
    double zmax = mx.getReportMonitorValue(), zmin = mn.getReportMonitorValue();
    mid = 0.5 * (zmin + zmax);
    sim.println("PLANO_AUTO\tsalida del rotor " + rot.getPresentationName() + ": " + outs.size() + " fronteras, eje " + zmin + " .. " + zmax + " m -> " + mid);
    sim.getReportManager().removeObjects(mx, mn);
    sim.getFieldFunctionManager().removeObjects(uf);
  }
  double[] e = Math.abs(ax[0]) < 0.9 ? new double[] {1, 0, 0} : new double[] {0, 1, 0};  // normal del meridional: eje × e
  double[] nm = {ax[1] * e[2] - ax[2] * e[1], ax[2] * e[0] - ax[0] * e[2], ax[0] * e[1] - ax[1] * e[0]};
  double t = mid - (ax[0] * og[0] + ax[1] * og[1] + ax[2] * og[2]);
  planos = "medio:" + ax[0] + "," + ax[1] + "," + ax[2] + "," + (og[0] + t * ax[0]) + "," + (og[1] + t * ax[1]) + "," + (og[2] + t * ax[2])
         + ";meridional:" + nm[0] + "," + nm[1] + "," + nm[2] + "," + og[0] + "," + og[1] + "," + og[2];
  sim.println("PLANOS\t" + planos);
}
for (String spec : planos.split(";")) {
  spec = spec.trim(); if (spec.isEmpty() || !spec.contains(":")) continue;
  String pname = "plano_" + spec.substring(0, spec.indexOf(':')).trim();
  String[] v = spec.substring(spec.indexOf(':') + 1).split(",");
  if (v.length != 6) { sim.println("WARN\tplano mal escrito: " + spec); continue; }
  double[] x = new double[6]; for (int i = 0; i < 6; i++) x[i] = Double.parseDouble(v[i].trim());
  Part ps = null;
  try { ps = sim.getPartManager().getPart(pname); } catch (Exception e) { }
  if (ps instanceof PlaneSection) {  // reusar, pero en la posición pedida (un plano viejo puede estar en otra cota)
    ((PlaneSection) ps).getOriginCoordinate().setCoordinate(sim.getUnitsManager().getUnits("m"), sim.getUnitsManager().getUnits("m"), sim.getUnitsManager().getUnits("m"), new DoubleVector(new double[] {x[3], x[4], x[5]}));
    ((PlaneSection) ps).getOrientationCoordinate().setCoordinate(sim.getUnitsManager().getUnits("m"), sim.getUnitsManager().getUnits("m"), sim.getUnitsManager().getUnits("m"), new DoubleVector(new double[] {x[0], x[1], x[2]}));
  }
  if (ps == null) {
    PlaneSection pl = (PlaneSection) sim.getPartManager().createImplicitPart(new NeoObjectVector(sim.getRegionManager().getRegions().toArray()),
        new DoubleVector(new double[] {x[0], x[1], x[2]}), new DoubleVector(new double[] {x[3], x[4], x[5]}), 0, 1, new DoubleVector(new double[] {0.0}));
    pl.setPresentationName(pname); ps = pl;
  }
  planes.add(ps); sim.println("BOUNDARY\tPlanos/" + pname); meta.append("BOUNDARY\tPlanos/" + pname + "\n");
}
sim.getImportManager().export(resolvePath("OUT"), regs, bnds, planes, new java.util.Vector<PartSurface>(), ffs, false, true);
// Marcos rotatorios (MRF) y regiones que los usan: el visor gira esas regiones sobre el eje real
// (vale para bombas, turbinas, ventiladores, compresores: el eje y el centro salen de STAR-CCM+, no se suponen).
for (Region r : sim.getRegionManager().getRegions()) {
  try {
    Object rf = r.getValues().get(MotionSpecification.class).getReferenceFrame();
    if (rf instanceof RotatingReferenceFrame) {
      RotatingReferenceFrame f = (RotatingReferenceFrame) rf;
      DoubleVector ax = f.getAxisDirection().getSIVector(), og = f.getAxisOrigin().getSIVector();  // SI: origen en m
      double rpm = f.getRotationRate().getSIValue() * 60.0 / (2.0 * Math.PI);
      sim.println("ROTFRAME	" + r.getPresentationName() + "	" + ax.get(0) + "," + ax.get(1) + "," + ax.get(2) + "	" + og.get(0) + "," + og.get(1) + "," + og.get(2) + "	" + rpm);
      meta.append("ROTFRAME	" + r.getPresentationName() + "	" + ax.get(0) + "," + ax.get(1) + "," + ax.get(2) + "	" + og.get(0) + "," + og.get(1) + "," + og.get(2) + "	" + rpm + "\n");
    }
  } catch (Exception e) { sim.println("WARN	sin marco en " + r.getPresentationName() + ": " + e); }
}
try { java.nio.file.Files.write(java.nio.file.Paths.get(resolvePath("OUT") + ".meta"), meta.toString().getBytes("UTF-8")); }
catch (Exception e) { sim.println("WARN\tno pude escribir " + "OUT" + ".meta: " + e); }
sim.println("CASE OUT");

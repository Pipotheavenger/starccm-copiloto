// QUE: post-proceso de bomba (autónomo): planos auto, escenas presion_alabes / velocidad_relativa / yplus a PNG en
//      <case_dir>/images, y+ en álabes, RESULT por reporte, BALANCE_MASA y validación de Euler. Tras iterar.
// VERSION: 19.06.009-R8 · FECHA: 2026-10-26 · ESTADO: compila con javac; el juez de estabilidad se probó con datos de
//      BombaRapida; NO verificado con el solver real
// PARAMS: case_dir
// GENERADA con `python pipelines/bomba.py --write-recetas` desde pipelines/bomba.py: no editar a mano (el selftest la compara).
// Pensada para run_steps: agrupa iterar y post con el cambio de caudal en UNA macro.
// --- post-proceso: planos auto, escenas a PNG (presión en álabes, velocidad relativa en plano medio, y+), resultados
{
  long pxT0 = System.currentTimeMillis();
  String pxImg = "${case_dir}/images"; int pxW = 1600, pxHt = 1000; boolean pxPng = true;
  new java.io.File(pxImg).mkdirs();
  ReportManager pxRm = sim.getReportManager(); FieldFunctionManager pxFfm = sim.getFieldFunctionManager();
  int pxIt = 0; try { pxIt = sim.getSimulationIterator().getCurrentIteration(); } catch (Exception e) { }
  boolean pxSol = pxIt >= 1;
  if (!pxSol) sim.println("AVISO\tpost sin solución (iteración 0): se crean planos y escenas pero no se exportan PNG");
  class PxR { Report get(String n) { try { return pxRm.getReport(n); } catch (Exception e) { return null; } } }
  PxR pxr = new PxR();
  class PxS { Scene find(String base) { for (Scene s : sim.getSceneManager().getScenes()) if (s.getPresentationName().startsWith(base)) return s; return null; } }
  PxS pxs = new PxS();
  // rotor, eje y origen: del marco rotatorio (nada supuesto)
  Region pxRot = null; double[] pxAx = {0, 0, 1}, pxOg = {0, 0, 0};
  for (Region r : sim.getRegionManager().getRegions()) {
    try {
      Object o = r.getValues().get(MotionSpecification.class).getReferenceFrame();
      if (o instanceof RotatingReferenceFrame) {
        RotatingReferenceFrame f = (RotatingReferenceFrame) o;
        DoubleVector a = f.getAxisDirection().getSIVector(), og = f.getAxisOrigin().getSIVector();
        double n = Math.sqrt(a.get(0) * a.get(0) + a.get(1) * a.get(1) + a.get(2) * a.get(2));
        pxAx = new double[] {a.get(0) / n, a.get(1) / n, a.get(2) / n}; pxOg = new double[] {og.get(0), og.get(1), og.get(2)}; pxRot = r; break;
      }
    } catch (Exception e) { }
  }
  // fronteras: álabes+cubo del rotor, solo álabes, salida del rotor, todas las paredes (sin interfaces)
  java.util.List<Boundary> pxBl = new java.util.ArrayList<Boundary>(), pxBlade = new java.util.ArrayList<Boundary>(), pxOut = new java.util.ArrayList<Boundary>(), pxWalls = new java.util.ArrayList<Boundary>();
  for (Region r : sim.getRegionManager().getRegions()) for (Boundary b : r.getBoundaryManager().getBoundaries()) {
    String bn = b.getPresentationName();
    if (r == pxRot && bn.toLowerCase().matches(".*(outflow|outlet|salida).*")) pxOut.add(b);
    if (b instanceof DirectBoundaryInterfaceBoundary || bn.contains("[")) continue;
    if (b.getBoundaryType() instanceof WallBoundary) pxWalls.add(b);
    if (r == pxRot && bn.contains("Blade")) { pxBlade.add(b); pxBl.add(b); } else if (r == pxRot && bn.equals("Hub")) pxBl.add(b);
  }
  if (pxBl.isEmpty()) pxBl.addAll(pxWalls);
  if (pxBlade.isEmpty()) pxBlade.addAll(pxBl);
  // plano medio a media salida del rotor (Min/Max de la coordenada axial en sus Outflow) y plano meridional que contiene el eje
  double pxMid = pxAx[0] * pxOg[0] + pxAx[1] * pxOg[1] + pxAx[2] * pxOg[2];
  if (!pxOut.isEmpty()) {
    try {
      UserFieldFunction uf = pxFfm.createFieldFunction(); uf.setPresentationName("agente_coord_axial"); uf.setFunctionName("agente_coord_axial");
      uf.setDefinition("dot($$Position, [" + pxAx[0] + ", " + pxAx[1] + ", " + pxAx[2] + "])");
      MaxReport mx = pxRm.createReport(MaxReport.class); mx.setFieldFunction(uf); mx.getParts().setObjects(pxOut);
      MinReport mn = pxRm.createReport(MinReport.class); mn.setFieldFunction(uf); mn.getParts().setObjects(pxOut);
      pxMid = 0.5 * (mn.getReportMonitorValue() + mx.getReportMonitorValue());
      pxRm.removeObjects(mx, mn); pxFfm.removeObjects(uf);
    } catch (Exception e) { sim.println("WARN\tplano medio: no pude medir la salida del rotor, uso el origen del eje: " + e); }
  }
  double[] pxE = Math.abs(pxAx[0]) < 0.9 ? new double[] {1, 0, 0} : new double[] {0, 1, 0};
  double[] pxNm = {pxAx[1] * pxE[2] - pxAx[2] * pxE[1], pxAx[2] * pxE[0] - pxAx[0] * pxE[2], pxAx[0] * pxE[1] - pxAx[1] * pxE[0]};
  double pxT = pxMid - (pxAx[0] * pxOg[0] + pxAx[1] * pxOg[1] + pxAx[2] * pxOg[2]);
  double[][] pxPl = {{pxAx[0], pxAx[1], pxAx[2], pxOg[0] + pxT * pxAx[0], pxOg[1] + pxT * pxAx[1], pxOg[2] + pxT * pxAx[2]}, {pxNm[0], pxNm[1], pxNm[2], pxOg[0], pxOg[1], pxOg[2]}};
  String[] pxPn = {"plano_medio", "plano_meridional"};
  PlaneSection[] pxPlane = new PlaneSection[2];
  Units pxM = sim.getUnitsManager().getUnits("m");
  for (int pi = 0; pi < 2; pi++) {
    try {
      Part ps = null; try { ps = sim.getPartManager().getPart(pxPn[pi]); } catch (Exception e) { }
      if (ps instanceof PlaneSection) {  // reusar, pero en la cota de ahora
        ((PlaneSection) ps).getOriginCoordinate().setCoordinate(pxM, pxM, pxM, new DoubleVector(new double[] {pxPl[pi][3], pxPl[pi][4], pxPl[pi][5]}));
        ((PlaneSection) ps).getOrientationCoordinate().setCoordinate(pxM, pxM, pxM, new DoubleVector(new double[] {pxPl[pi][0], pxPl[pi][1], pxPl[pi][2]}));
      } else {
        PlaneSection pl = (PlaneSection) sim.getPartManager().createImplicitPart(new NeoObjectVector(sim.getRegionManager().getRegions().toArray()),
            new DoubleVector(new double[] {pxPl[pi][0], pxPl[pi][1], pxPl[pi][2]}), new DoubleVector(new double[] {pxPl[pi][3], pxPl[pi][4], pxPl[pi][5]}), 0, 1, new DoubleVector(new double[] {0.0}));
        pl.setPresentationName(pxPn[pi]); ps = pl;
      }
      pxPlane[pi] = (PlaneSection) ps;
    } catch (Exception e) { sim.println("WARN\tplano " + pxPn[pi] + ": " + e); }
  }
  sim.println("PLANOS\tmedio a " + pxMid + " m del origen del eje; " + pxOut.size() + " fronteras de salida del rotor");
  // escena 1: presión en álabes y cubo
  try {
    Scene sc = pxs.find("presion_alabes");
    if (sc == null) {
      sc = sim.getSceneManager().createScene("presion_alabes"); sc.initializeAndWait();
      ScalarDisplayer d = sc.getDisplayerManager().createScalarDisplayer("p"); d.initialize();
      d.setRepresentation(sim.getRepresentationManager().getDefaultFvRepresentation());
      d.getInputParts().setObjects(pxBl);
      d.getScalarDisplayQuantity().setFieldFunction(pxFfm.getFunction("Pressure"));
      sc.resetCamera(true);
    }
    if (pxSol && pxPng) { sc.printAndWait(resolvePath(pxImg + "/presion_alabes.png"), 1, pxW, pxHt, true, false); sim.println("PNG\t" + pxImg + "/presion_alabes.png"); }
  } catch (Exception e) { sim.println("WARN\tescena presion_alabes: " + e); }
  // escena 2: velocidad relativa en el plano medio (color = módulo, flechas encima, paredes tenues)
  try {
    FieldFunction vrel = null;
    for (String fn : new String[] {"RelativeVelocity", "CellRelativeVelocity", "Velocity"}) { if (vrel == null) try { vrel = pxFfm.getFunction(fn); } catch (Exception e) { } }
    if (pxPlane[0] == null || vrel == null) throw new RuntimeException("falta plano_medio o campo de velocidad relativa");
    Scene sc = pxs.find("velocidad_relativa");
    if (sc == null) {
      sc = sim.getSceneManager().createScene("velocidad_relativa"); sc.initializeAndWait();
      PartDisplayer pd = sc.getDisplayerManager().createPartDisplayer("paredes", -1, 4); pd.initialize();
      pd.setRepresentation(sim.getRepresentationManager().getDefaultFvRepresentation()); pd.getInputParts().setObjects(pxWalls); pd.setOpacity(0.12);
      ScalarDisplayer d = sc.getDisplayerManager().createScalarDisplayer("vrel"); d.initialize();
      d.setRepresentation(sim.getRepresentationManager().getDefaultFvRepresentation()); d.getInputParts().setObjects(pxPlane[0]);
      d.getScalarDisplayQuantity().setFieldFunction(((PrimitiveFieldFunction) vrel).getMagnitudeFunction());
      try {
        VectorDisplayer vd = sc.getDisplayerManager().createVectorDisplayer("flechas"); vd.initialize();
        vd.setRepresentation(sim.getRepresentationManager().getDefaultFvRepresentation()); vd.getInputParts().setObjects(pxPlane[0]);
        vd.getVectorDisplayQuantity().setFieldFunction(vrel);
        vd.setColorMode(VectorDisplayer.COLOR_USER_DEFINED); vd.setSpacingMode(0);
        vd.getGlyphSettings().setRelativeToModelLength(6.0); vd.getGlyphSettings().getGeometricSpacing().setOnRatio(6);
      } catch (Exception e) { sim.println("WARN\tflechas de velocidad relativa: " + e); }
      sc.resetCamera(true);
    }
    if (pxSol && pxPng) { sc.printAndWait(resolvePath(pxImg + "/velocidad_relativa.png"), 1, pxW, pxHt, true, false); sim.println("PNG\t" + pxImg + "/velocidad_relativa.png"); }
  } catch (Exception e) { sim.println("WARN\tescena velocidad_relativa: " + e); }
  // escena 3: y+ en todas las paredes; reportes mín/media/máx en álabes
  try {
    FieldFunction yp = pxFfm.getFunction("WallYplus");
    Report r0 = pxr.get("yplus_max_alabes");
    MaxReport ymx = r0 instanceof MaxReport ? (MaxReport) r0 : pxRm.createReport(MaxReport.class);
    r0 = pxr.get("yplus_min_alabes");
    MinReport ymn = r0 instanceof MinReport ? (MinReport) r0 : pxRm.createReport(MinReport.class);
    r0 = pxr.get("yplus_media_alabes");
    AreaAverageReport yav = r0 instanceof AreaAverageReport ? (AreaAverageReport) r0 : pxRm.createReport(AreaAverageReport.class);
    ymx.setPresentationName("yplus_max_alabes"); ymn.setPresentationName("yplus_min_alabes"); yav.setPresentationName("yplus_media_alabes");
    ymx.setFieldFunction(yp); ymx.getParts().setObjects(pxBlade);
    ymn.setFieldFunction(yp); ymn.getParts().setObjects(pxBlade);
    yav.setFieldFunction(yp); yav.getParts().setObjects(pxBlade);
    Scene sc = pxs.find("yplus");
    if (sc == null) {
      sc = sim.getSceneManager().createScene("yplus"); sc.initializeAndWait();
      ScalarDisplayer d = sc.getDisplayerManager().createScalarDisplayer("y+"); d.initialize();
      d.setRepresentation(sim.getRepresentationManager().getDefaultFvRepresentation()); d.getInputParts().setObjects(pxWalls);
      d.getScalarDisplayQuantity().setFieldFunction(yp);
      sc.resetCamera(true);
    }
    if (pxSol) {
      sim.println("YPLUS\talabes min " + ymn.getReportMonitorValue() + " media " + yav.getReportMonitorValue() + " max " + ymx.getReportMonitorValue());
      if (pxPng) { sc.printAndWait(resolvePath(pxImg + "/yplus.png"), 1, pxW, pxHt, true, false); sim.println("PNG\t" + pxImg + "/yplus.png"); }
    }
  } catch (Exception e) { sim.println("WARN\tescena yplus: " + e); }
  // resultados en una línea cada uno (y balance de masa): no hace falta otra macro para leerlos
  if (pxSol) {
    for (String rn : new String[] {"H", "M", "P_hid", "P_eje", "eta", "Q_in", "Q_out", "H_Euler", "eta_Euler"}) {
      Report q = pxr.get(rn);
      if (q != null) { try { sim.println("RESULT\t" + rn + "\t" + q.getReportMonitorValue()); } catch (Exception e) { sim.println("RESULT\t" + rn + "\tn/a"); } }
    }
    try {
      double qi = pxr.get("Q_in").getReportMonitorValue(), qo = pxr.get("Q_out").getReportMonitorValue();
      double bal = 100 * Math.abs(qi + qo) / Math.max(Math.abs(qi), 1e-30);
      sim.println("BALANCE_MASA\t" + bal + " %" + (bal > 0.5 ? "\tAVISO: > 0,5 %: revisar interfaces" : ""));
    } catch (Exception e) { }
  }
  sim.println("POST_SECONDS\t" + (System.currentTimeMillis() - pxT0) / 1000.0);
}
// --- validación de Euler con la solución (AVISO / EULER_MAL / EULER_OK)
{
boolean turbina = false; String hrep = "H"; ReportManager rm = sim.getReportManager();
}


// QUE: reportes de las plantillas NQ25Q159H91, generales para cualquier turbomáquina: triángulos de velocidad a la
//      entrada y salida del rotor (U, Vu, Vr), altura de Euler, eficiencia hidráulica de Euler (H/H_Euler en bomba;
//      H_Euler/H en turbina), factor de deslizamiento (si se da β2), campo TotalHead y gráficos de altura y eficiencia.
// VERSION: 19.06.009-R8 · FECHA: 2026-10-26 · ESTADO: no verificado con solver (reescrita tras BombaPruebaNR; la anterior
//      era verificada en cursocfturbo pero en BombaPruebaNR leyó la frontera de ENTRADA en la salida: UVu1 = UVu2, H_Euler 0)
// Eje, origen y ω (con signo) salen del marco rotatorio: nada se supone.
// FRONTERAS SIN AMBIGÜEDAD: por NOMBRE dentro de la región del rotor. Entrada = fronteras que no son pared y cuyo nombre
//      empieza por «Inflow» (incluye «Inflow [int_…]» de las interfaces); salida = las que empiezan por «Outflow»
//      (incluye «Outflow [int_…]»). La parte de cara de conexión que quedó como PARED se ignora por tipo. Con varias
//      piezas se usan todas juntas (promedio ponderado por caudal másico sobre el conjunto). Los nombres de CFturbo
//      son por sentido del flujo: también en turbina la entrada es «Inflow» (no se invierte por radio).
//      El radio medio solo VALIDA (aviso si en una bomba la entrada no queda a menor radio), nunca elige.
// MODO: bomba | turbina. RHO: kg/m³. H_REPORT: nombre del reporte de altura existente (p. ej. H). BETA2: ángulo de
// salida del álabe en grados respecto a la tangente (vacío = sin factor de deslizamiento).
// ENTRADA_ROTOR / SALIDA_ROTOR: nombres exactos de frontera del rotor, separados por «;», para forzar la elección
//      (solo si el rotor no usa nombres Inflow/Outflow; hay que dar las dos).
// Al final (tras «VALIDACION») comprueba UVu2 ≠ UVu1 y H_Euler > H (bomba) o H_Euler < H (turbina) y escribe AVISO si no.
// La validación necesita solución (iteración ≥ 1): lanzar la receta otra vez tras iterar, o solo esa parte.
boolean turbina = "MODO".trim().equalsIgnoreCase("turbina");
double rho = RHO; String hrep = "H_REPORT", beta2 = "BETA2", eIn = "ENTRADA_ROTOR", eOut = "SALIDA_ROTOR";
Region rot = null; RotatingReferenceFrame fr = null;
for (Region r : sim.getRegionManager().getRegions()) {
  try { Object o = r.getValues().get(MotionSpecification.class).getReferenceFrame(); if (o instanceof RotatingReferenceFrame) { rot = r; fr = (RotatingReferenceFrame) o; break; } } catch (Exception e) { }
}
if (rot == null) throw new RuntimeException("no hay región con marco rotatorio");
DoubleVector a = fr.getAxisDirection().getSIVector(), o = fr.getAxisOrigin().getSIVector();
double n = Math.sqrt(a.get(0) * a.get(0) + a.get(1) * a.get(1) + a.get(2) * a.get(2));
String ax = "[" + a.get(0) / n + ", " + a.get(1) / n + ", " + a.get(2) / n + "]", og = "[" + o.get(0) + ", " + o.get(1) + ", " + o.get(2) + "]";
double w = fr.getRotationRate().getSIValue();
FieldFunctionManager ffm = sim.getFieldFunctionManager();
class F { UserFieldFunction mk(String name, String def, String dims) {
  UserFieldFunction f = null; try { f = (UserFieldFunction) ffm.getFunction(name); } catch (Exception e) { }
  if (f == null) { f = ffm.createFieldFunction(); f.setPresentationName(name); f.setFunctionName(name); }
  f.setDefinition(def); return f; } }
F ff = new F();
String rv = "cross(" + ax + ", $$Position - " + og + ")";  // r·ê_θ (módulo = radio)
UserFieldFunction fR = ff.mk("agente_r", "mag(" + rv + ")", "");
UserFieldFunction fU = ff.mk("agente_U", "abs(" + w + ") * mag(" + rv + ")", "");
int sg = w >= 0 ? 1 : -1;  // Vu positiva en el sentido de giro (sirve con rpm negativas)
UserFieldFunction fVu = ff.mk("agente_Vu", sg + " * dot(" + rv + ", $$Velocity) / max(mag(" + rv + "), 1e-12)", "");
UserFieldFunction fUVu = ff.mk("agente_UVu", Math.abs(w) * sg + " * dot(" + rv + ", $$Velocity)", "");
UserFieldFunction fVr = ff.mk("agente_Vr", "dot(cross(" + rv + ", " + ax + "), $$Velocity) / max(mag(" + rv + "), 1e-12)", "");
UserFieldFunction fTH = ff.mk("TotalHead", "$TotalPressure / (" + rho + " * 9.80665)", "");
// fronteras de entrada y salida del rotor, por nombre
java.util.List<Boundary> bIn = new java.util.ArrayList<Boundary>(), bOut = new java.util.ArrayList<Boundary>();
boolean forzar = !eIn.equals("ENTRADA" + "_ROTOR") && !eIn.isEmpty() && !eOut.equals("SALIDA" + "_ROTOR") && !eOut.isEmpty();
for (Boundary b : rot.getBoundaryManager().getBoundaries()) {
  String bn = b.getPresentationName();
  boolean pared = b.getBoundaryType() instanceof WallBoundary;
  sim.println("CAND\t" + bn + "\t" + b.getBoundaryType().getPresentationName());
  if (forzar) {
    for (String s : eIn.split(";")) if (bn.equals(s.trim())) bIn.add(b);
    for (String s : eOut.split(";")) if (bn.equals(s.trim())) bOut.add(b);
  } else if (!pared) {
    String l = bn.toLowerCase();
    if (l.startsWith("inflow")) bIn.add(b); else if (l.startsWith("outflow")) bOut.add(b);
  }
}
if (bIn.isEmpty() || bOut.isEmpty()) throw new RuntimeException("no encuentro entrada/salida del rotor por nombre (Inflow/Outflow que no sean pared): da ENTRADA_ROTOR y SALIDA_ROTOR (ver líneas CAND)");
for (Boundary b : bIn) if (bOut.contains(b)) throw new RuntimeException("la misma frontera sirve de entrada y de salida del rotor: " + b.getPresentationName());
ReportManager rm = sim.getReportManager();
class RM2 { double radio(java.util.List<Boundary> l) {
  AreaAverageReport ar = rm.createReport(AreaAverageReport.class); ar.setFieldFunction(fR); ar.getParts().setObjects(l);
  double r = Double.NaN; try { r = ar.getReportMonitorValue(); } catch (Exception e) { } rm.removeObjects(ar); return r; } }  // NaN si aún no hay malla
double rIn = new RM2().radio(bIn), rOut = new RM2().radio(bOut);
StringBuilder sIn = new StringBuilder(), sOut = new StringBuilder();
for (Boundary b : bIn) sIn.append(b.getPresentationName()).append(" | ");
for (Boundary b : bOut) sOut.append(b.getPresentationName()).append(" | ");
sim.println("ROTOR\t" + rot.getPresentationName() + "\tentrada " + sIn + "\tsalida " + sOut + "\tω " + w + " rad/s\tr_ent " + rIn + "\tr_sal " + rOut);
if (!turbina && rIn >= rOut) sim.println("AVISO\tEuler: en bomba el radio medio de la entrada (" + rIn + ") no es menor que el de la salida (" + rOut + "): revisa ENTRADA_ROTOR/SALIDA_ROTOR");
if (turbina && rIn <= rOut) sim.println("AVISO\tEuler: en turbina el radio medio de la entrada (" + rIn + ") no es mayor que el de la salida (" + rOut + "): revisa ENTRADA_ROTOR/SALIDA_ROTOR");
class R { Report mf(String name, FieldFunction f, java.util.List<Boundary> b) {
  Report r0 = null; try { r0 = rm.getReport(name); } catch (Exception e) { }
  MassFlowAverageReport r = r0 instanceof MassFlowAverageReport ? (MassFlowAverageReport) r0 : rm.createReport(MassFlowAverageReport.class);
  r.setPresentationName(name); r.setFieldFunction(f); r.getParts().setObjects(b); return r; }
  Report ex(String name, String def) {
  Report r0 = null; try { r0 = rm.getReport(name); } catch (Exception e) { }
  ExpressionReport r = r0 instanceof ExpressionReport ? (ExpressionReport) r0 : rm.createReport(ExpressionReport.class);
  r.setPresentationName(name); r.setDefinition(def); return r; } }
R rr = new R();
java.util.List<Report> reps = new java.util.ArrayList<Report>();
reps.add(rr.mf("U1", fU, bIn)); reps.add(rr.mf("U2", fU, bOut));
reps.add(rr.mf("Vu1", fVu, bIn)); reps.add(rr.mf("Vu2", fVu, bOut));
reps.add(rr.mf("Vr1", fVr, bIn)); reps.add(rr.mf("Vr2", fVr, bOut));
reps.add(rr.mf("UVu1", fUVu, bIn)); reps.add(rr.mf("UVu2", fUVu, bOut));
reps.add(rr.ex("H_Euler", turbina ? "(${UVu1Report} - ${UVu2Report}) / 9.80665" : "(${UVu2Report} - ${UVu1Report}) / 9.80665"));
boolean hasH = false; try { rm.getReport(hrep); hasH = true; } catch (Exception e) { }
if (hasH) reps.add(rr.ex("eta_Euler", turbina ? "(abs(${" + hrep + "Report}) > 1e-9 ? ${H_EulerReport} / ${" + hrep + "Report} : 0)" : "(abs(${H_EulerReport}) > 1e-9 ? ${" + hrep + "Report} / ${H_EulerReport} : 0)"));
else sim.println("WARN\tno existe el reporte " + hrep + ": sin eta_Euler");
if (!beta2.isEmpty() && !beta2.equals("BET" + "A2")) {  // Vu2∞ = U2 − Vm2 / tan β2 ; σ = Vu2 / Vu2∞ (Vm2 ≈ Vr2 en salida radial)
  double b2 = Math.toRadians(Double.parseDouble(beta2));
  reps.add(rr.ex("Vu2_inf", "${U2Report} - ${Vr2Report} / " + Math.tan(b2)));
  reps.add(rr.ex("factor_deslizamiento", "(abs(${Vu2_infReport}) > 1e-9 ? ${Vu2Report} / ${Vu2_infReport} : 0)"));
}
java.util.List<Monitor> mons = new java.util.ArrayList<Monitor>();
for (Report r : reps) { Monitor m = null; try { m = sim.getMonitorManager().getMonitor(r.getPresentationName() + " Monitor"); } catch (Exception e) { }
  if (m == null) m = r.createMonitor(); mons.add(m); }
// gráficos: altura (H y H_Euler) y eficiencia
java.util.List<Monitor> mh = new java.util.ArrayList<Monitor>(), me = new java.util.ArrayList<Monitor>();
for (Monitor m : sim.getMonitorManager().getObjects()) {
  String mn = m.getPresentationName();
  if (mn.equals(hrep + " Monitor") || mn.equals("H_Euler Monitor")) mh.add(m);
  if (mn.matches("(eta|eta_Euler|P_eje|P_hid) Monitor")) me.add(m);
}
StarPlot p1 = null, p2 = null;
try { p1 = sim.getPlotManager().getPlot("Altura"); } catch (Exception e) { }
if (p1 == null && !mh.isEmpty()) sim.getPlotManager().createMonitorPlot(mh, "Altura");
try { p2 = sim.getPlotManager().getPlot("Eficiencia y potencia"); } catch (Exception e) { }
if (p2 == null && !me.isEmpty()) sim.getPlotManager().createMonitorPlot(me, "Eficiencia y potencia");
// ---- VALIDACION (necesita solución; bomba.py la reutiliza sola con euler_check_body)
int itEu = 0; try { itEu = sim.getSimulationIterator().getCurrentIteration(); } catch (Exception e) { }
if (itEu < 1) sim.println("EULER_VALIDAR\tsin solución (iteración 0): repetir tras iterar");
else {
  double vUVu1 = Double.NaN, vUVu2 = Double.NaN, vHe = Double.NaN, vH = Double.NaN, vU1 = Double.NaN, vU2 = Double.NaN;
  try { vUVu1 = rm.getReport("UVu1").getReportMonitorValue(); vUVu2 = rm.getReport("UVu2").getReportMonitorValue();
        vHe = rm.getReport("H_Euler").getReportMonitorValue(); vU1 = rm.getReport("U1").getReportMonitorValue(); vU2 = rm.getReport("U2").getReportMonitorValue(); } catch (Exception e) { sim.println("AVISO\tEuler: no pude leer los reportes: " + e); }
  try { vH = rm.getReport(hrep).getReportMonitorValue(); } catch (Exception e) { }
  boolean malo = false;
  if (Double.isNaN(vUVu1) || Double.isNaN(vUVu2) || Math.abs(vUVu2 - vUVu1) <= 1e-6 * Math.max(Math.abs(vUVu1), Math.abs(vUVu2)) + 1e-12) {
    sim.println("AVISO\tEuler: UVu2 (" + vUVu2 + ") = UVu1 (" + vUVu1 + "): el reporte de salida lee la entrada o no hay trabajo"); malo = true; }
  if (!Double.isNaN(vU1) && !Double.isNaN(vU2) && (turbina ? vU2 >= vU1 : vU2 <= vU1)) {
    sim.println("AVISO\tEuler: U2 (" + vU2 + ") frente a U1 (" + vU1 + ") no cuadra con entrada→salida del rotor"); malo = true; }
  if (!Double.isNaN(vH) && !Double.isNaN(vHe) && (turbina ? vHe >= vH : vHe <= vH)) {
    sim.println("AVISO\tEuler: H_Euler (" + vHe + ") " + (turbina ? "no es menor" : "no es mayor") + " que H (" + vH + ")"); malo = true; }
  sim.println(malo ? "EULER_MAL\trevisar fronteras del rotor" : "EULER_OK\tH_Euler " + vHe + " m, H " + vH + " m");
}
for (Report r : reps) { String v; try { v = String.valueOf(r.getReportMonitorValue()); } catch (Exception e) { v = "n/a"; } sim.println("REPORT\t" + r.getPresentationName() + "\t" + v); }

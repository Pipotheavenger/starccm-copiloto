// QUE: reportes de las plantillas NQ25Q159H91, generales para cualquier turbomáquina: triángulos de velocidad a la
//      entrada y salida del rotor (U, Vu, Vr), altura de Euler, eficiencia hidráulica de Euler (H/H_Euler en bomba;
//      H_Euler/H en turbina), factor de deslizamiento (si se da β2), campo TotalHead y gráficos de altura y eficiencia.
// VERSION: 19.06.009-R8 · FECHA: 2026-10-24 · ESTADO: verificado con el solver en una copia de cursocfturbo (ver CLAUDE.md)
// Eje, origen y ω (con signo) salen del marco rotatorio: nada se supone. Entrada/salida del rotor: sus fronteras que no
// son pared (interfaces o abiertas); la de menor radio medio es la entrada en una BOMBA y la salida en una TURBINA.
// MODO: bomba | turbina. RHO: kg/m³. H_REPORT: nombre del reporte de altura existente (p. ej. H). BETA2: ángulo de
// salida del álabe en grados respecto a la tangente (vacío = sin factor de deslizamiento).
// ENTRADA_ROTOR / SALIDA_ROTOR: nombres de frontera del rotor para forzar la elección (vacío = automático).
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
// fronteras de entrada y salida del rotor
java.util.List<Boundary> cand = new java.util.ArrayList<Boundary>();
for (Boundary b : rot.getBoundaryManager().getBoundaries()) if (!(b.getBoundaryType() instanceof WallBoundary)) cand.add(b);
ReportManager rm = sim.getReportManager();
Boundary bIn = null, bOut = null; double rIn = 1e30, rOut = -1;
for (Boundary b : cand) {
  AreaAverageReport ar = rm.createReport(AreaAverageReport.class); ar.setFieldFunction(fR); ar.getParts().setObjects(b);
  double r = ar.getReportMonitorValue(); rm.removeObjects(ar);
  sim.println("CAND\t" + b.getPresentationName() + "\tradio medio " + r + " m");
  if (r < rIn) { rIn = r; bIn = b; } if (r > rOut) { rOut = r; bOut = b; }
}
if (turbina) { Boundary t = bIn; bIn = bOut; bOut = t; }
for (Boundary b : rot.getBoundaryManager().getBoundaries()) { if (b.getPresentationName().equals(eIn)) bIn = b; if (b.getPresentationName().equals(eOut)) bOut = b; }
if (bIn == null || bOut == null || bIn == bOut) throw new RuntimeException("no distingo entrada y salida del rotor: da ENTRADA_ROTOR y SALIDA_ROTOR");
sim.println("ROTOR\t" + rot.getPresentationName() + "\tentrada " + bIn.getPresentationName() + "\tsalida " + bOut.getPresentationName() + "\tω " + w + " rad/s");
class R { Report mf(String name, FieldFunction f, Boundary b) {
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
if (hasH) reps.add(rr.ex("eta_Euler", turbina ? "${H_EulerReport} / ${" + hrep + "Report}" : "${" + hrep + "Report} / ${H_EulerReport}"));
else sim.println("WARN\tno existe el reporte " + hrep + ": sin eta_Euler");
if (!beta2.isEmpty() && !beta2.equals("BET" + "A2")) {  // Vu2∞ = U2 − Vm2 / tan β2 ; σ = Vu2 / Vu2∞ (Vm2 ≈ Vr2 en salida radial)
  double b2 = Math.toRadians(Double.parseDouble(beta2));
  reps.add(rr.ex("Vu2_inf", "${U2Report} - ${Vr2Report} / " + Math.tan(b2)));
  reps.add(rr.ex("factor_deslizamiento", "${Vu2Report} / ${Vu2_infReport}"));
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
for (Report r : reps) { String v; try { v = String.valueOf(r.getReportMonitorValue()); } catch (Exception e) { v = "n/a"; } sim.println("REPORT\t" + r.getPresentationName() + "\t" + v); }

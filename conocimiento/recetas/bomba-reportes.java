// QUE: reportes: pt_in/pt_out (promedio másico de presión total), Q_in/Q_out, M (momento en álabes+hub+shroud sobre Z), expresiones H, P_hid, P_eje, eta, y monitores
// VERSION: 19.06.009-R8
// FECHA: 2026-10-23

Region est = sim.getRegionManager().getRegion("estator"), rot = sim.getRegionManager().getRegion("rotor"), vol = sim.getRegionManager().getRegion("voluta");
FieldFunction ptot = sim.getFieldFunctionManager().getFunction("TotalPressure");
MassFlowAverageReport pin = sim.getReportManager().createReport(MassFlowAverageReport.class);
pin.setPresentationName("pt_in"); pin.setFieldFunction(ptot); pin.getParts().setObjects(est.getBoundaryManager().getBoundary("inlet"));
MassFlowAverageReport pout = sim.getReportManager().createReport(MassFlowAverageReport.class);
pout.setPresentationName("pt_out"); pout.setFieldFunction(ptot); pout.getParts().setObjects(vol.getBoundaryManager().getBoundary("outlet"));
MassFlowReport qi = sim.getReportManager().createReport(MassFlowReport.class);
qi.setPresentationName("Q_in"); qi.getParts().setObjects(est.getBoundaryManager().getBoundary("inlet"));
MassFlowReport qo = sim.getReportManager().createReport(MassFlowReport.class);
qo.setPresentationName("Q_out"); qo.getParts().setObjects(vol.getBoundaryManager().getBoundary("outlet"));
MomentReport mom = sim.getReportManager().createReport(MomentReport.class);
mom.setPresentationName("M");
mom.getDirection().setComponents(0.0, 0.0, 1.0);
mom.getOrigin().setComponents(0.0, 0.0, 0.0);
List<Boundary> walls = new ArrayList<Boundary>();
for (String n : new String[] {"Hub", "Shroud", "BladeSS", "BladePS", "BladeLE", "BladeTE"}) walls.add(rot.getBoundaryManager().getBoundary(n));
mom.getParts().setObjects(walls);
ExpressionReport H = sim.getReportManager().createReport(ExpressionReport.class);
H.setPresentationName("H"); H.setDefinition("(${pt_outReport} - ${pt_inReport}) / (998.2 * 9.80665)");
ExpressionReport Ph = sim.getReportManager().createReport(ExpressionReport.class);
Ph.setPresentationName("P_hid"); Ph.setDefinition("abs(${Q_inReport}) * 9.80665 * ${HReport}");
ExpressionReport Pe = sim.getReportManager().createReport(ExpressionReport.class);
Pe.setPresentationName("P_eje"); Pe.setDefinition("abs(${MReport}) * 2980.0 * 2 * 3.14159265 / 60");
ExpressionReport eta = sim.getReportManager().createReport(ExpressionReport.class);
eta.setPresentationName("eta"); eta.setDefinition("${P_hidReport} / ${P_ejeReport}");
for (Report r : new Report[] {pin, pout, qi, qo, mom, H, Pe, eta}) r.createMonitor();
for (Report r : sim.getReportManager().getObjects()) sim.println("REPORT\t" + r.getPresentationName() + "\t" + r.getClass().getSimpleName());

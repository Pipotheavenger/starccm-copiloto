// QUE: inlet = velocity inlet 1 m/s, outlet = pressure outlet, reportes de caudal másico Q_in/Q_out con monitor
// VERSION: 19.06.009-R8
// FECHA: 2026-10-23

Region r = sim.getRegionManager().getRegions().iterator().next();
Boundary inlet = r.getBoundaryManager().getBoundary("inlet");
Boundary outlet = r.getBoundaryManager().getBoundary("outlet");
inlet.setBoundaryType(sim.get(ConditionTypeManager.class).get(InletBoundary.class));
outlet.setBoundaryType(sim.get(ConditionTypeManager.class).get(PressureBoundary.class));
inlet.getValues().get(VelocityMagnitudeProfile.class).getMethod(ConstantScalarProfileMethod.class)
     .getQuantity().setValueAndUnits(1.0, sim.getUnitsManager().getUnits("m/s"));
MassFlowReport qi = sim.getReportManager().createReport(MassFlowReport.class);
qi.setPresentationName("Q_in"); qi.getParts().setObjects(inlet); qi.createMonitor();
MassFlowReport qo = sim.getReportManager().createReport(MassFlowReport.class);
qo.setPresentationName("Q_out"); qo.getParts().setObjects(outlet); qo.createMonitor();
sim.println("INLET " + inlet.getBoundaryType().getPresentationName());
sim.println("OUTLET " + outlet.getBoundaryType().getPresentationName());

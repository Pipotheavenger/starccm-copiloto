// QUE: renombra inlet/outlet/wall de un tubo en Z usando reportes Min/Max de Position[Z] por frontera
// VERSION: 19.06.009-R8
// FECHA: 2026-10-23

Region r = sim.getRegionManager().getRegions().iterator().next();
FieldFunction posZ = ((PrimitiveFieldFunction) sim.getFieldFunctionManager().getFunction("Position")).getComponentFunction(2);
MinReport mn = sim.getReportManager().createReport(MinReport.class); mn.setPresentationName("zmin"); mn.setFieldFunction(posZ);
MaxReport mx = sim.getReportManager().createReport(MaxReport.class); mx.setPresentationName("zmax"); mx.setFieldFunction(posZ);
Boundary inlet = null, outlet = null, wall = null;
for (Boundary b : new ArrayList<Boundary>(r.getBoundaryManager().getBoundaries())) {
  mn.getParts().setObjects(b); mx.getParts().setObjects(b);
  double zmin = mn.getReportMonitorValue(), zmax = mx.getReportMonitorValue();
  sim.println("BOUNDARY " + b.getPresentationName() + " zmin " + zmin + " zmax " + zmax);
  if (zmax - zmin > 0.1) wall = b; else if (zmax < 0.1) inlet = b; else outlet = b;
}
inlet.setPresentationName("inlet"); outlet.setPresentationName("outlet"); wall.setPresentationName("wall");
sim.println("RENAMED inlet outlet wall");

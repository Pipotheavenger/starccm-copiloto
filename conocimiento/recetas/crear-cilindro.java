// QUE: crea un cilindro simple r=0.05 m, L=0.5 m en Z, lo tesela fino y parte su superficie por ángulo (89°)
// VERSION: 19.06.009-R8
// FECHA: 2026-10-23

Units um = sim.getUnitsManager().getUnits("m");
MeshPartFactory mpf = sim.get(MeshPartFactory.class);
SimpleCylinderPart cyl = mpf.createNewCylinderPart(sim.get(SimulationPartManager.class));
cyl.setCoordinateSystem(sim.getCoordinateSystemManager().getLabCoordinateSystem());
cyl.getStartCoordinate().setCoordinate(um, um, um, new DoubleVector(new double[] {0.0, 0.0, 0.0}));
cyl.getEndCoordinate().setCoordinate(um, um, um, new DoubleVector(new double[] {0.0, 0.0, 0.5}));
cyl.getRadius().setValueAndUnits(0.05, um);
cyl.getTessellationDensityOption().setSelected(TessellationDensityOption.Type.FINE);
cyl.rebuildSimpleShapePart();
cyl.setPresentationName("pipe");
cyl.getPartSurfaceManager().splitPartSurfacesByAngle(new ArrayList<PartSurface>(cyl.getPartSurfaces()), 89.0);
for (PartSurface s : cyl.getPartSurfaces()) sim.println("SURFACE " + s.getPresentationName());

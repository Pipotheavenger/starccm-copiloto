// QUE: marco de referencia rotatorio (MRF) -2980.0 rpm sobre +Z en el origen, creado directo en ReferenceFrameManager, asignado a la región rotor
// VERSION: 19.06.009-R8
// FECHA: 2026-10-23

RotatingReferenceFrame frame = sim.get(ReferenceFrameManager.class).createReferenceFrame(RotatingReferenceFrame.class, "MRF_rotor");
frame.getRotationRate().setValueAndUnits(-2980.0, sim.getUnitsManager().getUnits("rpm"));
frame.getAxisDirection().setComponents(0.0, 0.0, 1.0);
frame.getAxisOrigin().setComponents(0.0, 0.0, 0.0);
Region rot = sim.getRegionManager().getRegion("rotor");
rot.getValues().get(MotionSpecification.class).setReferenceFrame(frame);
sim.println("MRF rotor -> " + frame.getPresentationName() + " " + frame.getRotationRate().getRawValue() + " " + frame.getRotationRate().getUnits().getPresentationName());

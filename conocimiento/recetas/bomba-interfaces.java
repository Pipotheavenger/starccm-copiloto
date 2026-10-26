// QUE: interfaces internas estator→rotor (Outflow/Inflow) y rotor→voluta; in-place, frozen rotor con MRF
// VERSION: 19.06.009-R8
// FECHA: 2026-10-23

Region est = sim.getRegionManager().getRegion("estator"), rot = sim.getRegionManager().getRegion("rotor"), vol = sim.getRegionManager().getRegion("voluta");
BoundaryInterface i1 = sim.getInterfaceManager().createBoundaryInterface(est.getBoundaryManager().getBoundary("Outflow"), rot.getBoundaryManager().getBoundary("Inflow"), "int_entrada");
BoundaryInterface i2 = sim.getInterfaceManager().createBoundaryInterface(rot.getBoundaryManager().getBoundary("Outflow"), vol.getBoundaryManager().getBoundary("Inflow"), "int_salida");
for (Interface i : sim.getInterfaceManager().getObjects())
  sim.println("INTERFACE\t" + i.getPresentationName() + "\t" + i.getClass().getSimpleName());

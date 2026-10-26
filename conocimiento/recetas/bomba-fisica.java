// QUE: reusa el continuo 'Physics 1': 3D estacionario, líquido, segregado, densidad constante, k-ω SST all-y+, y lo asigna a las 3 regiones
// VERSION: 19.06.009-R8
// FECHA: 2026-10-23

PhysicsContinuum pc = null;
for (Continuum c : sim.getContinuumManager().getObjects()) if (c instanceof PhysicsContinuum) pc = (PhysicsContinuum) c;
if (pc == null) pc = sim.getContinuumManager().createContinuum(PhysicsContinuum.class);
pc.setPresentationName("agua");
for (Class c : new Class[] {ThreeDimensionalModel.class, SteadyModel.class, SingleComponentLiquidModel.class, SegregatedFlowModel.class, ConstantDensityModel.class,
                            TurbulentModel.class, RansTurbulenceModel.class, KOmegaTurbulence.class, SstKwTurbModel.class, KwAllYplusWallTreatment.class})
  pc.enable(c);
for (Region r : sim.getRegionManager().getRegions()) r.setPhysicsContinuum(pc);
for (Model mdl : pc.getModelManager().getObjects()) sim.println("MODEL\t" + mdl.getPresentationName());

// QUE: continuo: 3D, estacionario, líquido, flujo segregado, densidad constante, k-omega SST all-y+, y lo asigna a la región
// VERSION: 19.06.009-R8
// FECHA: 2026-10-23

// OJO: newRegionsFromParts ya crea un continuo "Physics 1" (solo con Three Dimensional). Crear otro
// deja dos; mejor reusarlo: pc = (PhysicsContinuum) sim.getContinuumManager().getContinuum("Physics 1").
// ESTADO: no verificado el reuso; verificado crear uno nuevo y asignarlo (deja "Physics 1" huérfano).
PhysicsContinuum pc = sim.getContinuumManager().createContinuum(PhysicsContinuum.class);
pc.setPresentationName("agua");
pc.enable(ThreeDimensionalModel.class);
pc.enable(SteadyModel.class);
pc.enable(SingleComponentLiquidModel.class);
pc.enable(SegregatedFlowModel.class);
pc.enable(ConstantDensityModel.class);
pc.enable(TurbulentModel.class);
pc.enable(RansTurbulenceModel.class);
pc.enable(KOmegaTurbulence.class);
pc.enable(SstKwTurbModel.class);
pc.enable(KwAllYplusWallTreatment.class);
Region r = sim.getRegionManager().getRegions().iterator().next();
r.setPhysicsContinuum(pc);
for (Model mdl : pc.getModelManager().getObjects()) sim.println("MODEL " + mdl.getPresentationName());

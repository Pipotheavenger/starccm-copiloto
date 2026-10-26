// QUE: inlet = mass flow inlet 69.4858111111111 kg/s (estator/Inflow), outlet = pressure outlet 0 Pa (voluta/Outflow); renombra a inlet/outlet
// VERSION: 19.06.009-R8
// FECHA: 2026-10-23

Region est = sim.getRegionManager().getRegion("estator"), vol = sim.getRegionManager().getRegion("voluta");
Boundary inlet = est.getBoundaryManager().getBoundary("Inflow");
Boundary outlet = vol.getBoundaryManager().getBoundary("Outflow");
inlet.setBoundaryType(sim.get(ConditionTypeManager.class).get(MassFlowBoundary.class));
inlet.getValues().get(MassFlowRateProfile.class).getMethod(ConstantScalarProfileMethod.class).getQuantity().setValueAndUnits(69.4858111111111, sim.getUnitsManager().getUnits("kg/s"));
outlet.setBoundaryType(sim.get(ConditionTypeManager.class).get(PressureBoundary.class));
inlet.setPresentationName("inlet"); outlet.setPresentationName("outlet");
sim.println("INLET " + inlet.getBoundaryType().getPresentationName());
sim.println("OUTLET " + outlet.getBoundaryType().getPresentationName());

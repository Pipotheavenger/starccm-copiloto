// QUE: densidad y viscosidad del agua del .cfturbo-ccm en el modelo de líquido
// VERSION: 19.06.009-R8
// FECHA: 2026-10-23

PhysicsContinuum pc = (PhysicsContinuum) sim.getContinuumManager().getContinuum("agua");
SingleComponentLiquidModel lm = pc.getModelManager().getModel(SingleComponentLiquidModel.class);
Liquid liq = (Liquid) lm.getMaterial();
((ConstantMaterialPropertyMethod) liq.getMaterialProperties().getMaterialProperty(ConstantDensityProperty.class).getMethod()).getQuantity().setValue(998.2);
((ConstantMaterialPropertyMethod) liq.getMaterialProperties().getMaterialProperty(DynamicViscosityProperty.class).getMethod()).getQuantity().setValue(0.0009982);
sim.println("RHO " + ((ConstantMaterialPropertyMethod) liq.getMaterialProperties().getMaterialProperty(ConstantDensityProperty.class).getMethod()).getQuantity().getRawValue());

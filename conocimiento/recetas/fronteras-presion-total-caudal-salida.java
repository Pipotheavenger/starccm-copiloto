// QUE: condiciones de las plantillas NQ25Q159H91: presión total en la entrada y caudal impuesto en la SALIDA, con el
//      caudal en un parámetro global «Flujo» (kg/s) para barrer la curva cambiando solo el parámetro.
// VERSION: 19.06.009-R8 · FECHA: 2026-10-24 · ESTADO: verificado con el solver en una copia de cursocfturbo (ver CLAUDE.md)
// Por qué: en bombas converge mejor que caudal en la entrada + presión en la salida, la presión de entrada queda fija
// (base para NPSH) y el caudal es exacto. ENTRADA y SALIDA: «región/frontera». CAUDAL: kg/s, positivo.
// PTOTAL: presión total de entrada en Pa (relativa a la de referencia; 0 por defecto).
// La salida es un Mass Flow Inlet con el caudal NEGATIVO (sale del dominio), como en las plantillas.
double caudal = CAUDAL, ptot = PTOTAL;
String[] ent = "ENTRADA".split("/"), sal = "SALIDA".split("/");
Boundary inlet = sim.getRegionManager().getRegion(ent[0]).getBoundaryManager().getBoundary(ent[1]);
Boundary outlet = sim.getRegionManager().getRegion(sal[0]).getBoundaryManager().getBoundary(sal[1]);
GlobalParameterManager gpm = sim.get(GlobalParameterManager.class);
ScalarGlobalParameter flujo = null;
try { flujo = (ScalarGlobalParameter) gpm.getObject("Flujo"); } catch (Exception e) { }
if (flujo == null) flujo = gpm.createGlobalParameter(ScalarGlobalParameter.class, "Flujo");
flujo.getQuantity().setValue(caudal);
inlet.setBoundaryType(sim.get(ConditionTypeManager.class).get(StagnationBoundary.class));
inlet.getValues().get(TotalPressureProfile.class).getMethod(ConstantScalarProfileMethod.class).getQuantity().setValueAndUnits(ptot, sim.getUnitsManager().getUnits("Pa"));
outlet.setBoundaryType(sim.get(ConditionTypeManager.class).get(MassFlowBoundary.class));
outlet.getValues().get(MassFlowRateProfile.class).getMethod(ConstantScalarProfileMethod.class).getQuantity().setDefinition("-${Flujo}");
sim.println("INLET\t" + inlet.getPresentationName() + "\t" + inlet.getBoundaryType().getPresentationName() + "\t" + ptot + " Pa");
sim.println("OUTLET\t" + outlet.getPresentationName() + "\t" + outlet.getBoundaryType().getPresentationName() + "\t-${Flujo} = " + (-caudal) + " kg/s");

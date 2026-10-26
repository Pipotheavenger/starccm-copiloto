// QUE: modelos extra de las plantillas NQ25Q159H91 sobre el continuo existente: corrección de celdas malas,
//      gravedad y transición Gamma (k-ω SST). Cada uno se activa solo si se pide.
// VERSION: 19.06.009-R8 · FECHA: 2026-10-24 · ESTADO: verificado con el solver en una copia de cursocfturbo (ver CLAUDE.md)
// REMEDIACION (true|false): Cell Quality Remediation; barato y da robustez con celdas malas: casi siempre true.
// GRAVEDAD («gx,gy,gz» en m/s², vacío = sin gravedad): máquinas grandes o verticales, cavitación/NPSH, superficie libre.
// GAMMA (true|false): transición laminar-turbulenta (Gamma ReTheta de Menter). Reynolds bajos o moderados, bombas
//        pequeñas, cuando importa la eficiencia fina; necesita y+ ≈ 1 y cuesta ~10-20 % más por iteración.
boolean remed = REMEDIACION, gamma = GAMMA;
String grav = "GRAVEDAD";
PhysicsContinuum pc = null;
for (Continuum c : sim.getContinuumManager().getObjects()) if (c instanceof PhysicsContinuum) { pc = (PhysicsContinuum) c; break; }
if (pc == null) throw new RuntimeException("no hay continuo físico: crea la física antes (fisica-agua-segregado-sst)");
if (remed) pc.enable(CellQualityRemediationModel.class);
if (!grav.isEmpty() && !grav.equals("GRAVE" + "DAD")) {
  String[] g = grav.split(",");
  pc.enable(GravityModel.class);
  pc.getReferenceValues().get(Gravity.class).setComponentsAndUnits(Double.parseDouble(g[0].trim()), Double.parseDouble(g[1].trim()),
      Double.parseDouble(g[2].trim()), sim.getUnitsManager().getUnits("m/s^2"));
}
if (gamma) pc.enable(GammaTransitionModel.class);
for (Model m : pc.getModelManager().getObjects()) sim.println("MODEL\t" + m.getPresentationName());

import star.common.*;
public class Paso3 extends StarMacro {
  public void execute() {
    Simulation sim = getActiveSimulation();
    sim.println("PASO 3: escenas=" + sim.getSceneManager().getScenes().size() + " regiones=" + sim.getRegionManager().getRegions().size());
  }
}

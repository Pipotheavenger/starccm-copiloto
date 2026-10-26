import star.common.*;
public class Paso2 extends StarMacro {
  public void execute() {
    Simulation sim = getActiveSimulation();
    sim.println("PASO 2: escenas=" + sim.getSceneManager().getScenes().size() + " regiones=" + sim.getRegionManager().getRegions().size());
  }
}

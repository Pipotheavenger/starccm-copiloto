import star.common.*;
public class Paso4 extends StarMacro {
  public void execute() {
    Simulation sim = getActiveSimulation();
    sim.println("PASO 4: escenas=" + sim.getSceneManager().getScenes().size() + " regiones=" + sim.getRegionManager().getRegions().size());
  }
}

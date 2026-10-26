import star.common.*;
public class Paso5 extends StarMacro {
  public void execute() {
    Simulation sim = getActiveSimulation();
    sim.println("PASO 5: escenas=" + sim.getSceneManager().getScenes().size() + " regiones=" + sim.getRegionManager().getRegions().size());
  }
}

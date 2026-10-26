import star.common.*;
public class Paso1 extends StarMacro {
  public void execute() {
    Simulation sim = getActiveSimulation();
    sim.println("PASO 1: escenas=" + sim.getSceneManager().getScenes().size() + " regiones=" + sim.getRegionManager().getRegions().size());
  }
}

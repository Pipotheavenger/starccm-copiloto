import star.common.*;
import java.io.*;
import java.nio.file.*;
// Cliente residente: vigila inbox/, ejecuta cada .java con StarScript (sin arrancar otra JVM) y escribe el resultado.
public class Residente extends StarMacro {
  public void execute() {
    Simulation sim = getActiveSimulation();
    File base = new File("C:/Users/sebas/OneDrive/Documentos/starccm-copiloto/poc_residente");
    File inbox = new File(base, "inbox"), done = new File(base, "done");
    long fin = System.currentTimeMillis() + 15 * 60 * 1000;
    try { Files.writeString(new File(base, "LISTO").toPath(), "" + System.currentTimeMillis()); } catch (Exception e) {}
    while (System.currentTimeMillis() < fin) {
      if (new File(inbox, "STOP").exists()) break;
      File[] fs = inbox.listFiles((d, n) -> n.endsWith(".java"));
      if (fs != null) {
        java.util.Arrays.sort(fs);
        for (File f : fs) {
          long t0 = System.nanoTime(); String err = "";
          try { new StarScript(getActiveRootObject(), f).play(); } catch (Throwable e) { err = e.toString(); }
          double ms = (System.nanoTime() - t0) / 1e6;
          try { Files.writeString(new File(done, f.getName() + ".res").toPath(), ms + " ms " + err); } catch (Exception e) {}
          f.delete();
        }
      }
      try { Thread.sleep(50); } catch (InterruptedException e) { break; }
    }
  }
}

// QUE: corre N iteraciones, exporta el plot "Residuals" a CSV y lee todos los reportes (base de la tool iterate)
// VERSION: 19.06.009-R8
// FECHA: 2026-10-23
// MonitorManager.export(...) falla en batch 2410 ("StarUpdate unexpectedly missing"); el plot sí exporta.
sim.getSimulationIterator().step(50);
sim.println("ITERATION " + sim.getSimulationIterator().getCurrentIteration());
StarPlot rp = sim.getPlotManager().getPlot("Residuals");
rp.export("C:/ruta/residuals.csv", ",");
for (Report rep : sim.getReportManager().getObjects())
  sim.println("REPORT\t" + rep.getPresentationName() + "\t" + rep.getReportMonitorValue());

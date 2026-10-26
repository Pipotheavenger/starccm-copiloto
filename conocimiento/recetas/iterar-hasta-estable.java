// QUE: itera en bloques de 50 hasta que los reportes dados varían < 0,5 % en 100 iteraciones (máx. max_iter); el residual
//      < 1e-4 se reporta y la meseta sale como AVISO, pero no condiciona. ITER_STOP dice el motivo (stable|max|diverge).
//      reportes: nombres entre comillas separados por coma, p. ej. "H", "M" (bomba) o "dp" (tubería).
// VERSION: 19.06.009-R8 · FECHA: 2026-10-26 · ESTADO: compila con javac; el juez de estabilidad se probó con datos de
//      BombaRapida; NO verificado con el solver real
// PARAMS: case_dir, max_iter, reportes
// GENERADA con `python pipelines/bomba.py --write-recetas` desde pipelines/bomba.py: no editar a mano (el selftest la compara).
// Pensada para run_steps: agrupa iterar y post con el cambio de caudal en UNA macro.
// --- iterar hasta que los reportes dados se estabilicen (< 0.5 % en 100 iteraciones, comprobado cada 50; máx. ${max_iter}). El residual < 1e-4 NO es condición: se reporta
{
  class PxJudge {
    // muestras {iteración, v1, v2…}; variación relativa máxima (máx−mín)/|media| en la ventana, NaN si aún no hay `win` iteraciones
    double var(java.util.List<double[]> h, int win) {
      double cur = h.get(h.size() - 1)[0];
      if (h.get(0)[0] > cur - win) return Double.NaN;
      double worst = 0;
      for (int j = 1; j < h.get(0).length; j++) {
        double mn = Double.POSITIVE_INFINITY, mx = Double.NEGATIVE_INFINITY, sum = 0; int c = 0;
        for (double[] s : h) if (s[0] >= cur - win) { mn = Math.min(mn, s[j]); mx = Math.max(mx, s[j]); sum += s[j]; c++; }
        worst = Math.max(worst, (mx - mn) / Math.max(Math.abs(sum / c), 1e-30));
      }
      return worst;
    }
  }
  PxJudge pxJ = new PxJudge();
  class PxN { double d(String s) { try { return Double.parseDouble(s.trim().replace("\"", "")); } catch (Exception e) { return Double.NaN; } } }
  PxN pxN = new PxN();
  int pxMax = ${max_iter}, pxBlk = 50, pxWin = 100, pxPl = 300; double pxTol = 0.005;
  String[] pxNm = {${reportes}};
  java.util.ArrayList<double[]> pxHist = new java.util.ArrayList<double[]>();
  int pxIt0 = sim.getSimulationIterator().getCurrentIteration(), pxCur = pxIt0;
  String pxWhy = "max"; double pxVar = Double.NaN; long pxT0 = System.currentTimeMillis();
  while (pxCur - pxIt0 < pxMax) {
    sim.getSimulationIterator().step(Math.min(pxBlk, pxMax - (pxCur - pxIt0)));
    pxCur = sim.getSimulationIterator().getCurrentIteration();
    double[] pxS = new double[pxNm.length + 1]; pxS[0] = pxCur; boolean pxFin = true;
    for (int pj = 0; pj < pxNm.length; pj++) {
      pxS[pj + 1] = sim.getReportManager().getReport(pxNm[pj]).getReportMonitorValue();
      if (Double.isNaN(pxS[pj + 1]) || Double.isInfinite(pxS[pj + 1])) pxFin = false;
    }
    StringBuilder pxLn2 = new StringBuilder();
    for (int pj = 0; pj < pxNm.length; pj++) pxLn2.append("\t").append(pxNm[pj]).append(" ").append(pxS[pj + 1]);
    sim.println("ITER\t" + pxCur + pxLn2);
    if (!pxFin) { pxWhy = "diverge"; break; }
    pxHist.add(pxS);
    pxVar = pxJ.var(pxHist, pxWin);
    if (!Double.isNaN(pxVar) && pxVar < pxTol) { pxWhy = "stable"; break; }
  }
  double pxRes = Double.NaN, pxOld = Double.NaN;
  try {
    String pxCsv = "${case_dir}/reports/residuals_pipe.csv";
    new java.io.File(pxCsv).getParentFile().mkdirs();
    sim.getPlotManager().getPlot("Residuals").export(pxCsv, ",");
    java.util.List<String> pxLn = java.nio.file.Files.readAllLines(java.nio.file.Paths.get(pxCsv), java.nio.charset.StandardCharsets.UTF_8);
    boolean pxHasIt = !pxLn.isEmpty() && pxLn.get(0).toLowerCase().contains("iter");
    for (int pk = 1; pk < pxLn.size(); pk++) {
      String[] pc = pxLn.get(pk).split(",");
      double pit = pxHasIt ? pxN.d(pc[0]) : pk, pmx = Double.NaN;
      for (int pm = 1; pm < pc.length; pm++) { double pv = Math.abs(pxN.d(pc[pm])); if (!Double.isNaN(pv)) pmx = Double.isNaN(pmx) ? pv : Math.max(pmx, pv); }
      if (Double.isNaN(pmx) || Double.isNaN(pit)) continue;
      if (pit <= pxCur - pxPl) pxOld = pmx;
      pxRes = pmx;
    }
  } catch (Exception ex) { sim.println("WARN\tno pude leer los residuales: " + ex); }
  boolean pxPlat = !Double.isNaN(pxOld) && !Double.isNaN(pxRes) && pxRes >= 0.9 * pxOld;
  sim.println("RESIDUAL\t" + pxRes + "\t(1e-4 solo informativo: " + (pxRes < 1e-4 ? "cumplido" : "no alcanzado; no condiciona") + ")");
  if (pxPlat) sim.println("AVISO\tmeseta de residuales: el mayor residual no bajó un 10 % en " + pxPl + " iteraciones (normal en frozen rotor, ~1e-2)");
  if (pxWhy.equals("max")) sim.println("AVISO\t" + String.join(" y ", pxNm) + " no se estabilizaron en " + pxMax + " iteraciones: no dar el resultado como convergido");
  if (pxWhy.equals("diverge")) sim.println("AVISO\tdiverge: " + String.join(" o ", pxNm) + " no son números; parar y revisar interfaces y fronteras");
  sim.println("ITER_STOP\t" + pxWhy + "\titeraciones " + (pxCur - pxIt0) + "\titeración " + pxCur + "\tvariación_max_% " + pxVar * 100 + "\tmeseta " + pxPlat + "\t" + (System.currentTimeMillis() - pxT0) / 1000.0 + " s");
}

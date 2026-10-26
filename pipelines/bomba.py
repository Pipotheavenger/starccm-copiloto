"""Pipeline de bomba: genera el cuerpo Java de UNA macro para una bomba exportada por CFturbo.

Contrato (lo usa mcp_server.pump_case):
  parse_ccm(path) -> dict            lee el XML .cfturbo-ccm
  macro_body(case_dir, cad_dir, ccm, mode="rapida"|"normal", params={}) -> str   cuerpo de execute() (sin la clase)
  euler_check_body(params={}) -> str  solo la validación de Euler (para correr tras iterar)
  iterate_body(case_dir, params={}) -> str  iterar hasta que H y M se estabilicen (criterio por defecto, ver STOP)
  post_body(case_dir, params={}) -> str  post-proceso en UNA macro: planos auto, 3 escenas a PNG, y+, resultados, Euler

Criterio de parada por defecto (STOP): H y M varían < 0,5 % en 100 iteraciones, comprobado cada 50, máximo 1000.
El residual < 1e-4 NO es condición: se reporta (RESIDUAL) y la meseta de residuales sale como AVISO.

Flujo (el verificado de BombaRapida/run.py, en una sola macro): importar STEP (una parte por zona) → regiones por
parte (nombres de cara) → interfaces en cadena → física SST + Cell Quality Remediation → MRF → fronteras (presión total
en la entrada, caudal con parámetro Flujo en la salida) → reportes H, M, P_eje, P_hid, eta → malla (una operación por
parte) → reportes de Euler → escena de presión. Imprime «PIPE <fase> <segundos>» por fase.
Modos: rapida = base d2/30 y 3 prismas; normal = base d2/60 y 5 prismas.

params opcionales: d2_mm, base_mm, prismas, flujo (kg/s), ptotal (Pa, 0), beta2 (grados), gamma (bool), gravedad
("gx,gy,gz"), remediacion (bool, True), euler (bool, True), escena (bool, True).
Al final de la MISMA macro, opcionales (por defecto apagados: la macro de preparación sigue igual):
  iterar: True (máx. 1000) o un entero (máx.); además max_iter, tol (0.005), ventana (100), bloque (50), meseta (300)
  post: True añade planos + escenas PNG en casos/<caso>/images + y+ + RESULT + validación de Euler;
        post_png (True), img_ancho (1600), img_alto (1000), post_euler (por defecto = euler). Con post no se crea la
        escena previa de presión (la hace el post, con datos).
d2 (diámetro de salida del rotor): params["d2_mm"], o se mide en los STL de CFturbo que acompañan al CAD
(…Impeller…Outflow.stl); si no hay ninguna de las dos, ValueError.

Uso:  python pipelines/bomba.py --selftest
      python pipelines/bomba.py --emit <ccm> <cad_dir> [rapida|normal]     imprime el cuerpo Java
      python pipelines/bomba.py --write-recetas      regenera conocimiento/recetas/{iterar-hasta-estable,post-bomba}.java
"""
from __future__ import annotations

import glob
import json
import math
import os
import re
import struct
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
RECETA_EULER = HERE.parent / "conocimiento" / "recetas" / "reportes-euler-triangulos.java"
G = 9.80665
# criterio de parada por defecto (CLAUDE.md § Velocidad): H y M estables, no residual < 1e-4
STOP = {"max_iter": 1000, "tol": 0.005, "ventana": 100, "bloque": 50, "meseta": 300}

# imports que el MCP pone a la macro (igual que mcp_server._wrap); el selftest los repite
IMPORTS = [
    "star.common.*", "star.base.neo.*", "star.base.report.*", "star.meshing.*", "star.flow.*",
    "star.kwturb.*", "star.vis.*", "star.cadmodeler.*", "star.prismmesher.*", "star.surfacewrapper.*",
    "star.material.*", "star.segregatedflow.*", "star.turbulence.*", "star.motion.*", "star.metrics.*",
    "star.resurfacer.*", "star.dualmesher.*", "java.util.*", "java.io.*",
]


# ----------------------------------------------------------------------------- .cfturbo-ccm
def _f(node, tag, default=None):
    t = node.findtext(tag) if node is not None else None
    return float(t) if t not in (None, "") else default


def _vec(node, tag, default):
    v = node.find(tag) if node is not None else None
    if v is None:
        return list(default)
    return [float(v.findtext(k, str(d))) for k, d in zip("xyz", default)]


def _short(geom: str) -> str:
    """'[Stator_1]-Flow domain-Inflow' -> 'Inflow' (como renombra el flujo verificado: sufijo tras el último '-')."""
    return geom.rsplit("-", 1)[-1]


def _bounds(zone, tag):
    out = []
    for b in zone.findall(f"{tag}/Boundary"):
        g = (b.findtext("Geometry") or "").strip()
        if g:
            out.append(g)
    return out


def _region_name(z: dict, idx: int, used: set) -> str:
    cap = (z["caption"] + " " + z.get("component", "")).lower()
    if "extension" in cap:
        n = "extension"
    elif z["frame"].lower() == "rotating" or "imp" in cap:
        n = "rotor"
    elif "stator" in cap:
        n = "estator"
    elif "volute" in cap:
        n = "voluta"
    elif "diffuser" in cap:
        n = "difusor"
    else:
        n = f"zona{idx + 1}"
    base, k = n, 2
    while n in used:
        n, k = f"{base}{k}", k + 1
    used.add(n)
    return n


def parse_ccm(path: str) -> dict:
    """Lee un .cfturbo-ccm. Claves: name, project_type, mass_flow (kg/s), axis, rpm (con signo, de la zona rotatoria),
    origin, density, viscosity, vapor_pressure, volumetric_flow (m³/s, de la entrada si lo trae), zones (en orden de
    flujo): caption, region, step, frame (Static|Rotating), component, rpm, axis, origin, inflow, outflow,
    static_walls, rotating_walls (listas de «geometría» completas; short() da el nombre de frontera)."""
    root = ET.parse(path).getroot()
    pd, fd = root.find("ProjectData"), root.find("FluidData")
    axis0 = _vec(pd, "RotationAxis", (0, 0, 1))
    zones, used = [], set()
    for i, zn in enumerate(root.findall("FluidZone")):
        frame = (zn.findtext("Frame") or "Static").strip()
        z = {
            "caption": (zn.findtext("Caption") or "").strip(),
            "step": (zn.findtext("Geometry") or "").strip(),
            "frame": frame,
            "component": (zn.findtext("ComponentType") or "").strip(),
            "rpm": round(_f(zn, "RotationalSpeed", 0.0) * 60.0, 6) if frame.lower() == "rotating" else 0.0,  # rev/s -> rpm
            "axis": _vec(zn, "RotationAxis", axis0),
            "origin": _vec(zn, "Origin", (0, 0, 0)),
            "inflow": _bounds(zn, "Inflow"),
            "outflow": _bounds(zn, "Outflow"),
            "static_walls": _bounds(zn, "StaticWalls"),
            "rotating_walls": _bounds(zn, "RotatingWalls"),
        }
        z["region"] = _region_name(z, i, used)
        zones.append(z)
    rot = next((z for z in zones if z["frame"].lower() == "rotating"), None)
    volq = root.find("FluidZone/Inflow/Boundary/Conditions/VolumetricFlow")
    return {
        "path": str(path), "name": Path(path).stem.replace("_cfd", ""),
        "project_type": (pd.findtext("ProjectType") or "").strip() if pd is not None else "",
        "mass_flow": _f(pd, "MassFlow"), "axis": axis0,
        "rpm": rot["rpm"] if rot else 0.0, "origin": rot["origin"] if rot else [0.0, 0.0, 0.0],
        "density": _f(fd, "Density", 998.2), "viscosity": _f(fd, "DynamicViscosity", 0.001),
        "vapor_pressure": _f(fd, "VaporPressure"),
        "volumetric_flow": float(volq.text) if volq is not None and volq.text else None,
        "zones": zones,
    }


short = _short


# ----------------------------------------------------------------------------- d2 desde los STL de CFturbo
def _stl_points(p: Path):
    data = p.read_bytes()
    if data[:5].lower() == b"solid" and b"facet" in data[:2000]:
        for m in re.finditer(rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)", data):
            yield float(m.group(1)), float(m.group(2)), float(m.group(3))
        return
    n = struct.unpack_from("<I", data, 80)[0]
    for k in range(n):
        v = struct.unpack_from("<12f", data, 84 + 50 * k)
        for j in (3, 6, 9):
            yield v[j], v[j + 1], v[j + 2]


def estimate_d2_mm(cad_dir: str, ccm: dict) -> float | None:
    """2·r máximo de la frontera Outflow del rotor (STL de CFturbo junto al CAD o al .ccm). None si no hay STL."""
    rot = next((z for z in ccm["zones"] if z["frame"].lower() == "rotating"), None)
    if rot is None:
        return None
    key = re.sub(r"\s+", "", rot["caption"]).lower()
    dirs = {Path(cad_dir), Path(ccm.get("path", ".")).parent}
    cands = [p for d in dirs if d.is_dir() for p in d.glob("*.stl")
             if key in re.sub(r"\s+", "", p.name).lower() and "outflow" in p.name.lower()]
    if not cands:
        return None
    ax, og = rot["axis"], rot["origin"]
    nrm = math.sqrt(sum(c * c for c in ax)) or 1.0
    ax = [c / nrm for c in ax]
    rmax = 0.0
    for x, y, z in _stl_points(cands[0]):
        d = (x - og[0], y - og[1], z - og[2])
        dot = sum(a * b for a, b in zip(d, ax))
        rr = math.sqrt(max(0.0, sum(c * c for c in d) - dot * dot))
        rmax = max(rmax, rr)
    if rmax <= 0:
        return None
    return 2 * rmax * (1000.0 if rmax < 1.5 else 1.0)  # CFturbo exporta en mm; < 1,5 se toma como metros


# ----------------------------------------------------------------------------- recetas de Euler
def _euler_text(ccm: dict, params: dict) -> str:
    txt = RECETA_EULER.read_text(encoding="utf-8")
    lines = [ln for ln in txt.splitlines() if not ln.startswith("//")]
    txt = "\n".join(lines)
    a = 'boolean turbina = "MODO".trim().equalsIgnoreCase("turbina");'
    b = 'double rho = RHO; String hrep = "H_REPORT", beta2 = "BETA2", eIn = "ENTRADA_ROTOR", eOut = "SALIDA_ROTOR";'
    if a not in txt or b not in txt:
        raise RuntimeError("la receta de Euler cambió de cabecera: actualiza pipelines/bomba.py")
    beta2 = "" if params.get("beta2") in (None, "") else str(params["beta2"])
    txt = txt.replace(a, 'boolean turbina = false;')
    txt = txt.replace(b, f'double rho = {ccm["density"]!r}; String hrep = "H", beta2 = {json.dumps(beta2)}, '
                         f'eIn = "ENTRADA_ROTOR", eOut = "SALIDA_ROTOR";')
    return txt


def euler_check_body(params: dict | None = None) -> str:
    """Solo la validación de Euler (UVu2 ≠ UVu1, H_Euler > H): correr tras iterar con los reportes ya creados."""
    txt = RECETA_EULER.read_text(encoding="utf-8")
    i = txt.index("// ---- VALIDACION")
    j = txt.index("for (Report r : reps)")
    return ('{\nboolean turbina = false; String hrep = "H"; ReportManager rm = sim.getReportManager();\n'
            + txt[i:j] + "}\n")



# ----------------------------------------------------------------------------- iterar y post-proceso
# Juez de estabilidad: se prueba de verdad en el selftest (java del JDK, datos de BombaRapida) y el mismo texto va a la macro.
JUDGE = r"""class PxJudge {
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
  }"""

ITERAR = r'''// --- iterar hasta que @@NOMBRES@@ se estabilicen (< @@TOLPCT@@ % en @@WIN@@ iteraciones, comprobado cada @@BLK@@; máx. @@MAX@@). El residual < 1e-4 NO es condición: se reporta
{
  @@JUDGE@@
  PxJudge pxJ = new PxJudge();
  class PxN { double d(String s) { try { return Double.parseDouble(s.trim().replace("\"", "")); } catch (Exception e) { return Double.NaN; } } }
  PxN pxN = new PxN();
  int pxMax = @@MAX@@, pxBlk = @@BLK@@, pxWin = @@WIN@@, pxPl = @@PL@@; double pxTol = @@TOL@@;
  String[] pxNm = @@REPS@@;
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
    String pxCsv = @@CSV@@;
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
}'''

POST = r'''// --- post-proceso: planos auto, escenas a PNG (presión en álabes, velocidad relativa en plano medio, y+), resultados
{
  long pxT0 = System.currentTimeMillis();
  String pxImg = @@IMG@@; int pxW = @@W@@, pxHt = @@HT@@; boolean pxPng = @@PNG@@;
  new java.io.File(pxImg).mkdirs();
  ReportManager pxRm = sim.getReportManager(); FieldFunctionManager pxFfm = sim.getFieldFunctionManager();
  int pxIt = 0; try { pxIt = sim.getSimulationIterator().getCurrentIteration(); } catch (Exception e) { }
  boolean pxSol = pxIt >= 1;
  if (!pxSol) sim.println("AVISO\tpost sin solución (iteración 0): se crean planos y escenas pero no se exportan PNG");
  class PxR { Report get(String n) { try { return pxRm.getReport(n); } catch (Exception e) { return null; } } }
  PxR pxr = new PxR();
  class PxS { Scene find(String base) { for (Scene s : sim.getSceneManager().getScenes()) if (s.getPresentationName().startsWith(base)) return s; return null; } }
  PxS pxs = new PxS();
  // rotor, eje y origen: del marco rotatorio (nada supuesto)
  Region pxRot = null; double[] pxAx = {0, 0, 1}, pxOg = {0, 0, 0};
  for (Region r : sim.getRegionManager().getRegions()) {
    try {
      Object o = r.getValues().get(MotionSpecification.class).getReferenceFrame();
      if (o instanceof RotatingReferenceFrame) {
        RotatingReferenceFrame f = (RotatingReferenceFrame) o;
        DoubleVector a = f.getAxisDirection().getSIVector(), og = f.getAxisOrigin().getSIVector();
        double n = Math.sqrt(a.get(0) * a.get(0) + a.get(1) * a.get(1) + a.get(2) * a.get(2));
        pxAx = new double[] {a.get(0) / n, a.get(1) / n, a.get(2) / n}; pxOg = new double[] {og.get(0), og.get(1), og.get(2)}; pxRot = r; break;
      }
    } catch (Exception e) { }
  }
  // fronteras: álabes+cubo del rotor, solo álabes, salida del rotor, todas las paredes (sin interfaces)
  java.util.List<Boundary> pxBl = new java.util.ArrayList<Boundary>(), pxBlade = new java.util.ArrayList<Boundary>(), pxOut = new java.util.ArrayList<Boundary>(), pxWalls = new java.util.ArrayList<Boundary>();
  for (Region r : sim.getRegionManager().getRegions()) for (Boundary b : r.getBoundaryManager().getBoundaries()) {
    String bn = b.getPresentationName();
    if (r == pxRot && bn.toLowerCase().matches(".*(outflow|outlet|salida).*")) pxOut.add(b);
    if (b instanceof DirectBoundaryInterfaceBoundary || bn.contains("[")) continue;
    if (b.getBoundaryType() instanceof WallBoundary) pxWalls.add(b);
    if (r == pxRot && bn.contains("Blade")) { pxBlade.add(b); pxBl.add(b); } else if (r == pxRot && bn.equals("Hub")) pxBl.add(b);
  }
  if (pxBl.isEmpty()) pxBl.addAll(pxWalls);
  if (pxBlade.isEmpty()) pxBlade.addAll(pxBl);
  // plano medio a media salida del rotor (Min/Max de la coordenada axial en sus Outflow) y plano meridional que contiene el eje
  double pxMid = pxAx[0] * pxOg[0] + pxAx[1] * pxOg[1] + pxAx[2] * pxOg[2];
  if (!pxOut.isEmpty()) {
    try {
      UserFieldFunction uf = pxFfm.createFieldFunction(); uf.setPresentationName("agente_coord_axial"); uf.setFunctionName("agente_coord_axial");
      uf.setDefinition("dot($$Position, [" + pxAx[0] + ", " + pxAx[1] + ", " + pxAx[2] + "])");
      MaxReport mx = pxRm.createReport(MaxReport.class); mx.setFieldFunction(uf); mx.getParts().setObjects(pxOut);
      MinReport mn = pxRm.createReport(MinReport.class); mn.setFieldFunction(uf); mn.getParts().setObjects(pxOut);
      pxMid = 0.5 * (mn.getReportMonitorValue() + mx.getReportMonitorValue());
      pxRm.removeObjects(mx, mn); pxFfm.removeObjects(uf);
    } catch (Exception e) { sim.println("WARN\tplano medio: no pude medir la salida del rotor, uso el origen del eje: " + e); }
  }
  double[] pxE = Math.abs(pxAx[0]) < 0.9 ? new double[] {1, 0, 0} : new double[] {0, 1, 0};
  double[] pxNm = {pxAx[1] * pxE[2] - pxAx[2] * pxE[1], pxAx[2] * pxE[0] - pxAx[0] * pxE[2], pxAx[0] * pxE[1] - pxAx[1] * pxE[0]};
  double pxT = pxMid - (pxAx[0] * pxOg[0] + pxAx[1] * pxOg[1] + pxAx[2] * pxOg[2]);
  double[][] pxPl = {{pxAx[0], pxAx[1], pxAx[2], pxOg[0] + pxT * pxAx[0], pxOg[1] + pxT * pxAx[1], pxOg[2] + pxT * pxAx[2]}, {pxNm[0], pxNm[1], pxNm[2], pxOg[0], pxOg[1], pxOg[2]}};
  String[] pxPn = {"plano_medio", "plano_meridional"};
  PlaneSection[] pxPlane = new PlaneSection[2];
  Units pxM = sim.getUnitsManager().getUnits("m");
  for (int pi = 0; pi < 2; pi++) {
    try {
      Part ps = null; try { ps = sim.getPartManager().getPart(pxPn[pi]); } catch (Exception e) { }
      if (ps instanceof PlaneSection) {  // reusar, pero en la cota de ahora
        ((PlaneSection) ps).getOriginCoordinate().setCoordinate(pxM, pxM, pxM, new DoubleVector(new double[] {pxPl[pi][3], pxPl[pi][4], pxPl[pi][5]}));
        ((PlaneSection) ps).getOrientationCoordinate().setCoordinate(pxM, pxM, pxM, new DoubleVector(new double[] {pxPl[pi][0], pxPl[pi][1], pxPl[pi][2]}));
      } else {
        PlaneSection pl = (PlaneSection) sim.getPartManager().createImplicitPart(new NeoObjectVector(sim.getRegionManager().getRegions().toArray()),
            new DoubleVector(new double[] {pxPl[pi][0], pxPl[pi][1], pxPl[pi][2]}), new DoubleVector(new double[] {pxPl[pi][3], pxPl[pi][4], pxPl[pi][5]}), 0, 1, new DoubleVector(new double[] {0.0}));
        pl.setPresentationName(pxPn[pi]); ps = pl;
      }
      pxPlane[pi] = (PlaneSection) ps;
    } catch (Exception e) { sim.println("WARN\tplano " + pxPn[pi] + ": " + e); }
  }
  sim.println("PLANOS\tmedio a " + pxMid + " m del origen del eje; " + pxOut.size() + " fronteras de salida del rotor");
  // escena 1: presión en álabes y cubo
  try {
    Scene sc = pxs.find("presion_alabes");
    if (sc == null) {
      sc = sim.getSceneManager().createScene("presion_alabes"); sc.initializeAndWait();
      ScalarDisplayer d = sc.getDisplayerManager().createScalarDisplayer("p"); d.initialize();
      d.setRepresentation(sim.getRepresentationManager().getDefaultFvRepresentation());
      d.getInputParts().setObjects(pxBl);
      d.getScalarDisplayQuantity().setFieldFunction(pxFfm.getFunction("Pressure"));
      sc.resetCamera(true);
    }
    if (pxSol && pxPng) { sc.printAndWait(resolvePath(pxImg + "/presion_alabes.png"), 1, pxW, pxHt, true, false); sim.println("PNG\t" + pxImg + "/presion_alabes.png"); }
  } catch (Exception e) { sim.println("WARN\tescena presion_alabes: " + e); }
  // escena 2: velocidad relativa en el plano medio (color = módulo, flechas encima, paredes tenues)
  try {
    FieldFunction vrel = null;
    for (String fn : new String[] {"RelativeVelocity", "CellRelativeVelocity", "Velocity"}) { if (vrel == null) try { vrel = pxFfm.getFunction(fn); } catch (Exception e) { } }
    if (pxPlane[0] == null || vrel == null) throw new RuntimeException("falta plano_medio o campo de velocidad relativa");
    Scene sc = pxs.find("velocidad_relativa");
    if (sc == null) {
      sc = sim.getSceneManager().createScene("velocidad_relativa"); sc.initializeAndWait();
      PartDisplayer pd = sc.getDisplayerManager().createPartDisplayer("paredes", -1, 4); pd.initialize();
      pd.setRepresentation(sim.getRepresentationManager().getDefaultFvRepresentation()); pd.getInputParts().setObjects(pxWalls); pd.setOpacity(0.12);
      ScalarDisplayer d = sc.getDisplayerManager().createScalarDisplayer("vrel"); d.initialize();
      d.setRepresentation(sim.getRepresentationManager().getDefaultFvRepresentation()); d.getInputParts().setObjects(pxPlane[0]);
      d.getScalarDisplayQuantity().setFieldFunction(((PrimitiveFieldFunction) vrel).getMagnitudeFunction());
      try {
        VectorDisplayer vd = sc.getDisplayerManager().createVectorDisplayer("flechas"); vd.initialize();
        vd.setRepresentation(sim.getRepresentationManager().getDefaultFvRepresentation()); vd.getInputParts().setObjects(pxPlane[0]);
        vd.getVectorDisplayQuantity().setFieldFunction(vrel);
        vd.setColorMode(VectorDisplayer.COLOR_USER_DEFINED); vd.setSpacingMode(0);
        vd.getGlyphSettings().setRelativeToModelLength(6.0); vd.getGlyphSettings().getGeometricSpacing().setOnRatio(6);
      } catch (Exception e) { sim.println("WARN\tflechas de velocidad relativa: " + e); }
      sc.resetCamera(true);
    }
    if (pxSol && pxPng) { sc.printAndWait(resolvePath(pxImg + "/velocidad_relativa.png"), 1, pxW, pxHt, true, false); sim.println("PNG\t" + pxImg + "/velocidad_relativa.png"); }
  } catch (Exception e) { sim.println("WARN\tescena velocidad_relativa: " + e); }
  // escena 3: y+ en todas las paredes; reportes mín/media/máx en álabes
  try {
    FieldFunction yp = pxFfm.getFunction("WallYplus");
    Report r0 = pxr.get("yplus_max_alabes");
    MaxReport ymx = r0 instanceof MaxReport ? (MaxReport) r0 : pxRm.createReport(MaxReport.class);
    r0 = pxr.get("yplus_min_alabes");
    MinReport ymn = r0 instanceof MinReport ? (MinReport) r0 : pxRm.createReport(MinReport.class);
    r0 = pxr.get("yplus_media_alabes");
    AreaAverageReport yav = r0 instanceof AreaAverageReport ? (AreaAverageReport) r0 : pxRm.createReport(AreaAverageReport.class);
    ymx.setPresentationName("yplus_max_alabes"); ymn.setPresentationName("yplus_min_alabes"); yav.setPresentationName("yplus_media_alabes");
    ymx.setFieldFunction(yp); ymx.getParts().setObjects(pxBlade);
    ymn.setFieldFunction(yp); ymn.getParts().setObjects(pxBlade);
    yav.setFieldFunction(yp); yav.getParts().setObjects(pxBlade);
    Scene sc = pxs.find("yplus");
    if (sc == null) {
      sc = sim.getSceneManager().createScene("yplus"); sc.initializeAndWait();
      ScalarDisplayer d = sc.getDisplayerManager().createScalarDisplayer("y+"); d.initialize();
      d.setRepresentation(sim.getRepresentationManager().getDefaultFvRepresentation()); d.getInputParts().setObjects(pxWalls);
      d.getScalarDisplayQuantity().setFieldFunction(yp);
      sc.resetCamera(true);
    }
    if (pxSol) {
      sim.println("YPLUS\talabes min " + ymn.getReportMonitorValue() + " media " + yav.getReportMonitorValue() + " max " + ymx.getReportMonitorValue());
      if (pxPng) { sc.printAndWait(resolvePath(pxImg + "/yplus.png"), 1, pxW, pxHt, true, false); sim.println("PNG\t" + pxImg + "/yplus.png"); }
    }
  } catch (Exception e) { sim.println("WARN\tescena yplus: " + e); }
  // resultados en una línea cada uno (y balance de masa): no hace falta otra macro para leerlos
  if (pxSol) {
    for (String rn : new String[] {"H", "M", "P_hid", "P_eje", "eta", "Q_in", "Q_out", "H_Euler", "eta_Euler"}) {
      Report q = pxr.get(rn);
      if (q != null) { try { sim.println("RESULT\t" + rn + "\t" + q.getReportMonitorValue()); } catch (Exception e) { sim.println("RESULT\t" + rn + "\tn/a"); } }
    }
    try {
      double qi = pxr.get("Q_in").getReportMonitorValue(), qo = pxr.get("Q_out").getReportMonitorValue();
      double bal = 100 * Math.abs(qi + qo) / Math.max(Math.abs(qi), 1e-30);
      sim.println("BALANCE_MASA\t" + bal + " %" + (bal > 0.5 ? "\tAVISO: > 0,5 %: revisar interfaces" : ""));
    } catch (Exception e) { }
  }
  sim.println("POST_SECONDS\t" + (System.currentTimeMillis() - pxT0) / 1000.0);
}'''


def _stop(params: dict) -> dict:
    """Parámetros del criterio de parada (STOP + params). `iterar` entero = máximo de iteraciones."""
    st = dict(STOP)
    it = params.get("iterar")
    if isinstance(it, int) and not isinstance(it, bool) and it > 1:
        st["max_iter"] = it
    for k in STOP:
        if params.get(k) not in (None, ""):
            st[k] = type(STOP[k])(params[k])
    return st


def iterate_body(case_dir: str, params: dict | None = None) -> str:
    """Iterar hasta estabilidad de H y M (criterio por defecto): bloques de 50, para si ΔH y ΔM < 0,5 % en 100
    iteraciones; máx. 1000. Exige los reportes H y M del pipeline (o los de params["reportes"], p. ej. ["dp"] en una tubería). Imprime ITER, RESIDUAL, AVISO (meseta, no estable,
    diverge) e ITER_STOP <stable|max|diverge>. El residual < 1e-4 se reporta, no condiciona."""
    params = dict(params or {})
    st = _stop(params)
    names = [str(x) for x in (params.get("reportes") or ["H", "M"])]
    csv = _j((Path(case_dir) / "reports" / "residuals_pipe.csv").as_posix())
    rep = {"TOLPCT": f"{st['tol'] * 100:g}", "WIN": str(st["ventana"]), "BLK": str(st["bloque"]), "MAX": str(st["max_iter"]),
           "PL": str(st["meseta"]), "TOL": _num(st["tol"]), "CSV": csv, "JUDGE": JUDGE,
           "REPS": _arr(names), "NOMBRES": " y ".join(names)}
    out = ITERAR
    for k, v in rep.items():
        out = out.replace(f"@@{k}@@", v)
    return out + "\n"


def post_body(case_dir: str, params: dict | None = None) -> str:
    """Post-proceso en UNA macro, autónomo (sirve solo, tras iterar): planos «auto» (medio a media salida del rotor y
    meridional), escenas presion_alabes, velocidad_relativa (plano medio) y yplus exportadas a PNG en
    <case_dir>/images, reportes de y+ en álabes, RESULT por reporte, BALANCE_MASA y la validación de Euler
    (post_euler, True). Sin solución crea las escenas pero no exporta PNG. params: post_png, img_ancho, img_alto."""
    params = dict(params or {})
    img = _j((Path(case_dir) / "images").as_posix())
    rep = {"IMG": img, "W": str(int(params.get("img_ancho") or 1600)), "HT": str(int(params.get("img_alto") or 1000)),
           "PNG": "true" if params.get("post_png", True) else "false"}
    out = POST
    for k, v in rep.items():
        out = out.replace(f"@@{k}@@", v)
    if params.get("post_euler", True):
        out += "\n// --- validación de Euler con la solución (AVISO / EULER_MAL / EULER_OK)\n" + euler_check_body(params)
    return out + "\n"


# ----------------------------------------------------------------------------- recetas generadas (para run_steps)
RECETAS_DIR = HERE.parent / "conocimiento" / "recetas"
_CABECERA = """// QUE: {que}
// VERSION: 19.06.009-R8 · FECHA: 2026-10-26 · ESTADO: compila con javac; el juez de estabilidad se probó con datos de
//      BombaRapida; NO verificado con el solver real
// PARAMS: {params}
// GENERADA con `python pipelines/bomba.py --write-recetas` desde pipelines/bomba.py: no editar a mano (el selftest la compara).
// Pensada para run_steps: agrupa iterar y post con el cambio de caudal en UNA macro.
"""


def recetas() -> dict:
    """{archivo: texto} de las recetas que expone el pipeline para run_steps (misma lógica que iterar/post de la macro)."""
    it = iterate_body("@CASE@", {"reportes": ["@REPS@"]})
    it = it.replace('{"@REPS@"}', "{${reportes}}").replace("pxMax = 1000,", "pxMax = ${max_iter},").replace("@CASE@", "${case_dir}")
    it = it.replace("iterar hasta que @REPS@", "iterar hasta que los reportes dados").replace("máx. 1000)", "máx. ${max_iter})")
    po = post_body("@CASE@", {}).replace("@CASE@", "${case_dir}")
    return {
        "iterar-hasta-estable.java": _CABECERA.format(
            que="itera en bloques de 50 hasta que los reportes dados varían < 0,5 % en 100 iteraciones (máx. max_iter); el residual\n//      < 1e-4 se reporta y la meseta sale como AVISO, pero no condiciona. ITER_STOP dice el motivo (stable|max|diverge).\n//      reportes: nombres entre comillas separados por coma, p. ej. \"H\", \"M\" (bomba) o \"dp\" (tubería).",
            params="case_dir, max_iter, reportes") + it,
        "post-bomba.java": _CABECERA.format(
            que="post-proceso de bomba (autónomo): planos auto, escenas presion_alabes / velocidad_relativa / yplus a PNG en\n//      <case_dir>/images, y+ en álabes, RESULT por reporte, BALANCE_MASA y validación de Euler. Tras iterar.",
            params="case_dir") + po,
    }


# ----------------------------------------------------------------------------- macro
def _j(s) -> str:
    return json.dumps(s)  # literal Java válido (ASCII; el resto sale como \uXXXX)


def _arr(items, fmt=_j) -> str:
    return "{" + ", ".join(fmt(x) for x in items) + "}"


def _num(x) -> str:
    return repr(float(x))


TEMPLATE = r'''
// ===== pipeline bomba @@CASO@@ (modo @@MODO@@): importar, regiones, interfaces, física, MRF, fronteras, reportes, malla, Euler, escena
long[] pipeT = {System.currentTimeMillis()}; final long pipeT0 = pipeT[0];
String cadDir = @@CAD@@;
String[] zNom = @@ZNOM@@, zStp = @@ZSTP@@, zCap = @@ZCAP@@;
boolean[] zRot = @@ZROT@@;
double[] zRpm = @@ZRPM@@;
double[][] zAxis = @@ZAXIS@@, zOrg = @@ZORG@@;
String[][] zGeom = @@ZGEOM@@, zPared = @@ZPARED@@;
int nz = zNom.length;
class Nm { String f(String s) { return s.replaceAll("\\s+", "").toLowerCase(); } }
Nm nm = new Nm();

// --- importar: una parte por STEP, ubicada por diferencia de conjuntos (sin adivinar nombres)
PartImportManager pim = sim.get(PartImportManager.class);
SimulationPartManager spm = sim.get(SimulationPartManager.class);
GeometryPart[] zPart = new GeometryPart[nz];
for (int zi = 0; zi < nz; zi++) {
  java.util.Set<GeometryPart> antes = new java.util.HashSet<GeometryPart>(spm.getParts());
  pim.importCadPart(cadDir + "/" + zStp[zi], "SharpEdges", 30.0, 2, 1.0E-5);
  for (GeometryPart gp : spm.getParts()) if (!antes.contains(gp) && zPart[zi] == null) zPart[zi] = gp;
  if (zPart[zi] == null) throw new RuntimeException("el STEP " + zStp[zi] + " no creó ninguna parte");
  sim.println("PART\t" + zNom[zi] + "\t" + zPart[zi].getPresentationName() + "\t" + zPart[zi].getPartSurfaces().size() + " superficies");
}
@@PIPE:importar@@

// --- regiones: una por parte, una frontera por superficie; cada región se asigna a su zona por los nombres de frontera del ccm
RegionManager rgm = sim.getRegionManager();
rgm.newRegionsFromParts(new ArrayList<GeometryPart>(Arrays.asList(zPart)), "OneRegionPerPart", null, "OneBoundaryPerPartSurface", null, false);
Region[] zReg = new Region[nz];
for (Region rg : rgm.getRegions()) {
  int mejor = -1, mejorN = 0;
  for (int zi = 0; zi < nz; zi++) {
    int hits = 0;
    for (Boundary bd : rg.getBoundaryManager().getBoundaries()) for (String g : zGeom[zi]) if (nm.f(bd.getPresentationName()).equals(nm.f(g))) hits++;
    if (hits > mejorN) { mejorN = hits; mejor = zi; }
  }
  if (mejor < 0) { for (int zi = 0; zi < nz; zi++) if (nm.f(rg.getPresentationName()).contains(zCap[zi])) mejor = zi; }
  if (mejor < 0) throw new RuntimeException("no sé a qué zona del ccm corresponde la región " + rg.getPresentationName());
  if (zReg[mejor] != null) throw new RuntimeException("dos regiones para la zona " + zNom[mejor]);
  zReg[mejor] = rg;
}
Boundary bInlet = null, bOutlet = null;
for (int zi = 0; zi < nz; zi++) {
  if (zReg[zi] == null) throw new RuntimeException("ninguna región para la zona " + zNom[zi]);
  Region rg = zReg[zi];
  rg.setPresentationName(zNom[zi]);
  for (Boundary bd : new ArrayList<Boundary>(rg.getBoundaryManager().getBoundaries())) {
    String bn = bd.getPresentationName(); int k = bn.lastIndexOf('-');
    if (k >= 0) bd.setPresentationName(bn.substring(k + 1));
  }
  ArrayList<String> esperadas = new ArrayList<String>(Arrays.asList("Inflow", "Outflow"));
  for (String pn : zPared[zi]) esperadas.add(pn);
  for (String en : esperadas) {
    boolean hay = false;
    for (Boundary bd : rg.getBoundaryManager().getBoundaries()) if (bd.getPresentationName().equals(en)) hay = true;
    if (!hay) sim.println("AVISO\tfalta la frontera " + zNom[zi] + "/" + en + " que declara el ccm");
  }
  if (zi == 0) bInlet = rg.getBoundaryManager().getBoundary("Inflow");
  if (zi == nz - 1) bOutlet = rg.getBoundaryManager().getBoundary("Outflow");
}

// --- interfaces en cadena: Outflow de cada zona con Inflow de la siguiente
InterfaceManager im = sim.getInterfaceManager();
for (int zi = 0; zi < nz - 1; zi++)
  im.createBoundaryInterface(zReg[zi].getBoundaryManager().getBoundary("Outflow"), zReg[zi + 1].getBoundaryManager().getBoundary("Inflow"), "int_" + zNom[zi] + "_" + zNom[zi + 1]);
if (!zRot[0]) bInlet.setPresentationName("inlet");
if (!zRot[nz - 1]) bOutlet.setPresentationName("outlet");
for (Region rg : rgm.getRegions()) for (Boundary bd : rg.getBoundaryManager().getBoundaries())
  sim.println("BOUNDARY\t" + rg.getPresentationName() + "/" + bd.getPresentationName() + "\t" + bd.getBoundaryType().getPresentationName());
@@PIPE:regiones@@

// --- física: reusa el continuo si existe; SST all-y+, estacionario, densidad constante, remediación de celdas
PhysicsContinuum pipePc = null;
for (Continuum c0 : sim.getContinuumManager().getObjects()) if (c0 instanceof PhysicsContinuum) pipePc = (PhysicsContinuum) c0;
if (pipePc == null) pipePc = sim.getContinuumManager().createContinuum(PhysicsContinuum.class);
pipePc.setPresentationName("agua");
for (Class cl : new Class[] {ThreeDimensionalModel.class, SteadyModel.class, SingleComponentLiquidModel.class, SegregatedFlowModel.class, ConstantDensityModel.class,
                            TurbulentModel.class, RansTurbulenceModel.class, KOmegaTurbulence.class, SstKwTurbModel.class, KwAllYplusWallTreatment.class})
  pipePc.enable(cl);
@@MODELOS_EXTRA@@
for (Region rg : rgm.getRegions()) rg.setPhysicsContinuum(pipePc);
Liquid pipeLiq = (Liquid) pipePc.getModelManager().getModel(SingleComponentLiquidModel.class).getMaterial();
((ConstantMaterialPropertyMethod) pipeLiq.getMaterialProperties().getMaterialProperty(ConstantDensityProperty.class).getMethod()).getQuantity().setValue(@@RHO@@);
((ConstantMaterialPropertyMethod) pipeLiq.getMaterialProperties().getMaterialProperty(DynamicViscosityProperty.class).getMethod()).getQuantity().setValue(@@MU@@);
@@PIPE:fisica@@

// --- MRF: un marco por zona rotatoria
for (int zi = 0; zi < nz; zi++) if (zRot[zi]) {
  RotatingReferenceFrame marco = sim.get(ReferenceFrameManager.class).createReferenceFrame(RotatingReferenceFrame.class, "MRF_" + zNom[zi]);
  marco.getRotationRate().setValueAndUnits(zRpm[zi], sim.getUnitsManager().getUnits("rpm"));
  marco.getAxisDirection().setComponents(zAxis[zi][0], zAxis[zi][1], zAxis[zi][2]);
  marco.getAxisOrigin().setComponents(zOrg[zi][0], zOrg[zi][1], zOrg[zi][2]);
  zReg[zi].getValues().get(MotionSpecification.class).setReferenceFrame(marco);
  sim.println("MRF\t" + zNom[zi] + "\t" + zRpm[zi] + " rpm");
}
@@PIPE:mrf@@

// --- fronteras: presión total en la entrada, caudal (parámetro Flujo) en la salida
GlobalParameterManager gpm = sim.get(GlobalParameterManager.class);
ScalarGlobalParameter flujo = gpm.createGlobalParameter(ScalarGlobalParameter.class, "Flujo");
flujo.getQuantity().setValue(@@FLUJO@@);
bInlet.setBoundaryType(sim.get(ConditionTypeManager.class).get(StagnationBoundary.class));
bInlet.getValues().get(TotalPressureProfile.class).getMethod(ConstantScalarProfileMethod.class).getQuantity().setValueAndUnits(@@PTOTAL@@, sim.getUnitsManager().getUnits("Pa"));
bOutlet.setBoundaryType(sim.get(ConditionTypeManager.class).get(MassFlowBoundary.class));
bOutlet.getValues().get(MassFlowRateProfile.class).getMethod(ConstantScalarProfileMethod.class).getQuantity().setDefinition("-${Flujo}");
@@PIPE:fronteras@@

// --- reportes (la expresión de H, P_hid, P_eje y eta usa los nombres «<reporte>Report»)
ReportManager repMgr = sim.getReportManager();
FieldFunction ptotFf = sim.getFieldFunctionManager().getFunction("TotalPressure");
MassFlowAverageReport rpIn = repMgr.createReport(MassFlowAverageReport.class); rpIn.setPresentationName("pt_in"); rpIn.setFieldFunction(ptotFf); rpIn.getParts().setObjects(bInlet);
MassFlowAverageReport rpOut = repMgr.createReport(MassFlowAverageReport.class); rpOut.setPresentationName("pt_out"); rpOut.setFieldFunction(ptotFf); rpOut.getParts().setObjects(bOutlet);
MassFlowReport rqIn = repMgr.createReport(MassFlowReport.class); rqIn.setPresentationName("Q_in"); rqIn.getParts().setObjects(bInlet);
MassFlowReport rqOut = repMgr.createReport(MassFlowReport.class); rqOut.setPresentationName("Q_out"); rqOut.getParts().setObjects(bOutlet);
int zr = 0; for (int zi = 0; zi < nz; zi++) if (zRot[zi]) { zr = zi; break; }
MomentReport rMom = repMgr.createReport(MomentReport.class); rMom.setPresentationName("M");
rMom.getDirection().setComponents(zAxis[zr][0], zAxis[zr][1], zAxis[zr][2]); rMom.getOrigin().setComponents(zOrg[zr][0], zOrg[zr][1], zOrg[zr][2]);
List<Boundary> paredesRotor = new ArrayList<Boundary>();
for (String pn : zPared[zr]) paredesRotor.add(zReg[zr].getBoundaryManager().getBoundary(pn));
rMom.getParts().setObjects(paredesRotor);
ExpressionReport rH = repMgr.createReport(ExpressionReport.class); rH.setPresentationName("H"); rH.setDefinition("(${pt_outReport} - ${pt_inReport}) / (@@RHO@@ * 9.80665)");
ExpressionReport rPh = repMgr.createReport(ExpressionReport.class); rPh.setPresentationName("P_hid"); rPh.setDefinition("abs(${Q_inReport}) * 9.80665 * ${HReport}");
ExpressionReport rPe = repMgr.createReport(ExpressionReport.class); rPe.setPresentationName("P_eje"); rPe.setDefinition("abs(${MReport}) * @@RPMABS@@ * 2 * 3.14159265 / 60");
ExpressionReport rEta = repMgr.createReport(ExpressionReport.class); rEta.setPresentationName("eta"); rEta.setDefinition("(abs(${P_ejeReport}) > 1e-9 ? ${P_hidReport} / ${P_ejeReport} : 0)");
for (Report rp : new Report[] {rpIn, rpOut, rqIn, rqOut, rMom, rH, rPh, rPe, rEta}) rp.createMonitor();
@@PIPE:reportes@@

// --- malla: una operación por parte (las juntas fallan por caras coincidentes), base @@BASE@@ mm, @@PRISMAS@@ prismas
Units mmU = sim.getUnitsManager().getUnits("mm");
MeshOperationManager momgr = sim.get(MeshOperationManager.class);
for (int zi = 0; zi < nz; zi++) {
  GeometryPart pt = zPart[zi];
  AutoMeshOperation op = momgr.createAutoMeshOperation(
      Arrays.asList("star.resurfacer.ResurfacerAutoMesher", "star.resurfacer.AutomaticSurfaceRepairAutoMesher", "star.dualmesher.DualAutoMesher", "star.prismmesher.PrismAutoMesher"),
      new ArrayList<GeometryPart>(Arrays.asList(pt)));
  op.setPresentationName("malla_" + zNom[zi]);
  op.getDefaultValues().get(BaseSize.class).setValueAndUnits(@@BASE@@, mmU);
  op.getDefaultValues().get(NumPrismLayers.class).setNumLayers(@@PRISMAS@@);
  op.getDefaultValues().get(PrismLayerStretching.class).setStretching(1.3);
  op.getDefaultValues().get(PrismThickness.class).setRelativeSize(10.0);
  if (zRot[zi]) {
    SurfaceCustomMeshControl scm = op.getCustomMeshControls().createSurfaceControl();
    scm.setPresentationName("alabes");
    List<GeometryObject> alabes = new ArrayList<GeometryObject>();
    for (PartSurface ps : pt.getPartSurfaces()) if (ps.getPresentationName().contains("Blade")) alabes.add(ps);
    scm.getGeometryObjects().setObjects(alabes);
    scm.getCustomConditions().get(PartsTargetSurfaceSizeOption.class).setSelected(PartsTargetSurfaceSizeOption.Type.CUSTOM);
    scm.getCustomValues().get(PartsTargetSurfaceSize.class).getRelativeSize().setPercentage(50.0);
  }
  long tm0 = System.currentTimeMillis();
  op.execute();
  sim.println("MESH_SECONDS\t" + zNom[zi] + "\t" + (System.currentTimeMillis() - tm0) / 1000.0);
}
FvRepresentation fvRep = (FvRepresentation) sim.getRepresentationManager().getObject("Volume Mesh");
sim.println("CELLS\t" + fvRep.getCellCount());
// partes de cara de conexión que quedaron como pared: su área frente a la de la interfaz hermana (>2 % = pedir re-export en Flow domain)
try {
  FieldFunction areaMag = sim.getFieldFunctionManager().getFunction("Area").getMagnitudeFunction();
  for (Region rg : rgm.getRegions()) for (Boundary bd : rg.getBoundaryManager().getBoundaries()) {
    String bn = bd.getPresentationName();
    if (bn.indexOf('[') >= 0 || !(bd.getBoundaryType() instanceof WallBoundary) || !(bn.equals("Inflow") || bn.equals("Outflow"))) continue;
    double aPared = 0, aInt = 0;
    SumReport ar0 = repMgr.createReport(SumReport.class); ar0.setFieldFunction(areaMag); ar0.getParts().setObjects(bd); aPared = ar0.getReportMonitorValue(); repMgr.removeObjects(ar0);
    for (Boundary b2 : rg.getBoundaryManager().getBoundaries()) if (b2.getPresentationName().startsWith(bn + " [")) {
      SumReport ar1 = repMgr.createReport(SumReport.class); ar1.setFieldFunction(areaMag); ar1.getParts().setObjects(b2); aInt += ar1.getReportMonitorValue(); repMgr.removeObjects(ar1); }
    sim.println("PARED_CONEXION\t" + rg.getPresentationName() + "/" + bn + "\t" + aPared * 1e6 + " mm2\t" + (aInt > 0 ? 100 * aPared / (aPared + aInt) : 100.0) + " % de la cara");
    if (aInt <= 0 || aPared > 0.02 * (aPared + aInt)) sim.println("AVISO\tinterfaz mal emparejada en " + rg.getPresentationName() + "/" + bn + ": exportar de nuevo desde CFturbo en Flow domain");
  }
} catch (Exception ex) { sim.println("WARN\tno pude medir las paredes de conexión: " + ex); }
@@PIPE:malla@@
@@EULER@@
@@ESCENA@@
@@ITERAR@@
@@POST@@
sim.println("PIPE total " + (System.currentTimeMillis() - pipeT0) / 1000.0);
'''

EULER_WRAP = '''// --- Euler: triángulos de velocidad, H_Euler y eta_Euler (receta reportes-euler-triangulos, fronteras del rotor por nombre)
try {
@@TXT@@
} catch (Exception ex) { sim.println("AVISO\\tEuler no se pudo crear: " + ex); }
@@PIPE:euler@@'''

ESCENA = '''// --- escena de presión en álabes y y+ (los reportes de y+ se leen tras iterar)
try {
  Region rotE = zReg[zr];
  List<Boundary> blB = new ArrayList<Boundary>();
  for (Boundary bd : rotE.getBoundaryManager().getBoundaries()) if (bd.getPresentationName().contains("Blade") || bd.getPresentationName().equals("Hub")) blB.add(bd);
  FieldFunction ypFf = sim.getFieldFunctionManager().getFunction("WallYplus");
  List<Boundary> blY = new ArrayList<Boundary>();
  for (Boundary bd : rotE.getBoundaryManager().getBoundaries()) if (bd.getPresentationName().contains("Blade")) blY.add(bd);
  MaxReport yMax = repMgr.createReport(MaxReport.class); yMax.setPresentationName("yplus_max_alabes"); yMax.setFieldFunction(ypFf); yMax.getParts().setObjects(blY);
  MinReport yMin = repMgr.createReport(MinReport.class); yMin.setPresentationName("yplus_min_alabes"); yMin.setFieldFunction(ypFf); yMin.getParts().setObjects(blY);
  AreaAverageReport yAvg = repMgr.createReport(AreaAverageReport.class); yAvg.setPresentationName("yplus_media_alabes"); yAvg.setFieldFunction(ypFf); yAvg.getParts().setObjects(blY);
  Scene escP = sim.getSceneManager().createScene("presion_alabes"); escP.initializeAndWait();
  ScalarDisplayer dP = escP.getDisplayerManager().createScalarDisplayer("p"); dP.initialize();
  dP.setRepresentation(sim.getRepresentationManager().getDefaultFvRepresentation());
  dP.getInputParts().setObjects(blB);
  dP.getScalarDisplayQuantity().setFieldFunction(sim.getFieldFunctionManager().getFunction("Pressure"));
  escP.resetCamera(true);
  sim.println("SCENE\\tpresion_alabes");
} catch (Exception ex) { sim.println("WARN\\tescena de presión: " + ex); }
@@PIPE:escena@@'''


def macro_body(case_dir: str, cad_dir: str, ccm: dict, mode: str = "rapida", params: dict | None = None) -> str:
    """Cuerpo de execute() de la macro del pipeline (ver el docstring del módulo)."""
    params = dict(params or {})
    if mode not in ("rapida", "normal"):
        raise ValueError("mode debe ser 'rapida' o 'normal'")
    zones = ccm["zones"]
    if len(zones) < 2:
        raise ValueError("el ccm debe traer al menos 2 zonas de fluido")
    if not any(z["frame"].lower() == "rotating" for z in zones):
        raise ValueError("el ccm no trae ninguna zona rotatoria")
    for z in zones:
        if not z["step"] or not (Path(cad_dir) / z["step"]).exists():
            raise ValueError(f"falta el STEP {z['step']!r} en {cad_dir}")
    d2 = params.get("d2_mm") or estimate_d2_mm(cad_dir, ccm)
    if params.get("base_mm"):
        base = float(params["base_mm"])
    elif d2:
        base = round(float(d2) / (30.0 if mode == "rapida" else 60.0), 3)
    else:
        raise ValueError("no conozco d2: pasa params['d2_mm'] o deja los STL de CFturbo (…Impeller…Outflow.stl) en cad_dir")
    prismas = int(params.get("prismas") or (3 if mode == "rapida" else 5))
    flujo = float(params.get("flujo") or ccm["mass_flow"])
    rho, mu = float(ccm["density"]), float(ccm["viscosity"])
    zr = next(i for i, z in enumerate(zones) if z["frame"].lower() == "rotating")

    def steps_of(i):
        return [short(g) for g in zones[i]["rotating_walls"] + zones[i]["static_walls"]]

    extra = []
    if params.get("remediacion", True):
        extra.append("pipePc.enable(CellQualityRemediationModel.class);")
    if params.get("gravedad"):
        g = [float(x) for x in str(params["gravedad"]).split(",")]
        extra.append("pipePc.enable(GravityModel.class); pipePc.getReferenceValues().get(Gravity.class).setComponentsAndUnits("
                     f"{g[0]}, {g[1]}, {g[2]}, sim.getUnitsManager().getUnits(\"m/s^2\"));")
    if params.get("gamma"):
        extra.append("pipePc.enable(GammaTransitionModel.class);")

    pipe = lambda ph: f'sim.println("PIPE {ph} " + (System.currentTimeMillis() - pipeT[0]) / 1000.0); pipeT[0] = System.currentTimeMillis();'
    euler = ""
    if params.get("euler", True):
        euler = EULER_WRAP.replace("@@TXT@@", _euler_text(ccm, params))
    rep = {
        "CASO": Path(case_dir).name, "MODO": mode, "CAD": _j(Path(cad_dir).resolve().as_posix()),
        "ZNOM": _arr([z["region"] for z in zones]), "ZSTP": _arr([z["step"] for z in zones]),
        "ZCAP": _arr([re.sub(r"[\s\[\]]+", "", z["caption"]).lower() for z in zones]),
        "ZROT": _arr([z["frame"].lower() == "rotating" for z in zones], lambda b: "true" if b else "false"),
        "ZRPM": _arr([z["rpm"] for z in zones], _num),
        "ZAXIS": "{" + ", ".join(_arr(z["axis"], _num) for z in zones) + "}",
        "ZORG": "{" + ", ".join(_arr(z["origin"], _num) for z in zones) + "}",
        "ZGEOM": "{" + ", ".join(_arr(z["inflow"] + z["outflow"] + z["static_walls"] + z["rotating_walls"]) for z in zones) + "}",
        "ZPARED": "{" + ", ".join(_arr([short(g) for g in z["rotating_walls"] + z["static_walls"]]) for z in zones) + "}",
        "RHO": _num(rho), "MU": _num(mu), "FLUJO": _num(flujo), "PTOTAL": _num(params.get("ptotal", 0.0)),
        "RPMABS": _num(abs(zones[zr]["rpm"])), "BASE": _num(base), "PRISMAS": str(prismas),
    }
    out = TEMPLATE
    for k, v in rep.items():
        out = out.replace(f"@@{k}@@", v)
    out = out.replace("@@MODELOS_EXTRA@@", "\n".join(extra))
    out = out.replace("@@EULER@@", euler)
    post_on, iter_on = bool(params.get("post")), bool(params.get("iterar"))
    out = out.replace("@@ESCENA@@", ESCENA if params.get("escena", True) and not post_on else "")  # con post, la escena sale con datos
    out = out.replace("@@ITERAR@@", (iterate_body(case_dir, params) + "@@PIPE:iterar@@") if iter_on else "")
    if post_on:
        pp = dict(params)
        pp.setdefault("post_euler", bool(params.get("euler", True)))
        out = out.replace("@@POST@@", post_body(case_dir, pp) + "@@PIPE:post@@")
    else:
        out = out.replace("@@POST@@", "")
    out = re.sub(r"@@PIPE:(\w+)@@", lambda m: pipe(m.group(1)), out)
    if "@@" in out:
        raise RuntimeError("quedaron marcas sin sustituir: " + re.findall(r"@@\w+@@", out)[0])
    return out
    # ponytail: las zonas se encadenan en el orden del ccm (Inflow de la zona i+1 con Outflow de la i): vale para
    # bombas de CFturbo (estator, impulsor, voluta, extensión); un ccm con otro orden pide reordenar las zonas.


# ----------------------------------------------------------------------------- selftest
def _javac():
    home = os.environ.get("STARCCM_HOME")
    homes = [Path(home)] if home else [Path(p) for p in sorted(glob.glob(r"C:\Program Files\Siemens\*\STAR-CCM+*"))]
    for h in reversed(homes):
        jc = sorted(glob.glob(str(h.parent / "jdk" / "*" / "jdk*" / "bin" / "javac.exe")))
        if jc:
            plat = h / "star" / "lib" / "java" / "platform"
            cp = os.pathsep.join(str(plat / d / "*") for d in ("modules/ext", "modules", "core", "lib"))
            return jc[-1], cp
    return None


def wrap(body: str, tag: str) -> str:
    imps = "\n".join(f"import {i};" for i in IMPORTS)
    return f"""{imps}

public class Agent_{tag} extends StarMacro {{
  public void execute() {{
    Simulation sim = getActiveSimulation();
    sim.println("AGENT_BEGIN {tag}");
    try {{
{body}
    }} catch (Exception e) {{
      StringWriter sw = new StringWriter();
      e.printStackTrace(new PrintWriter(sw));
      sim.println("AGENT_ERROR: " + e);
      sim.println(sw.toString());
    }}
    sim.println("AGENT_END {tag}");
  }}
}}
"""


def compile_java(body: str, tag: str, tmp: Path):
    """None si compila, texto del error si no, 'SKIP' si no hay javac."""
    jc = _javac()
    if not jc:
        return "SKIP"
    src = tmp / f"Agent_{tag}.java"
    src.write_text(wrap(body, tag), encoding="utf-8")
    out = tmp / "classes"
    out.mkdir(exist_ok=True)
    r = subprocess.run([jc[0], "-nowarn", "-proc:none", "-cp", jc[1], "-d", str(out), str(src)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    return None if r.returncode == 0 else (r.stderr or r.stdout)[-3000:]


CCM_REAL = Path(r"C:\Users\sebas\OneDrive\Documentos\cfturbo-agent\versions\BombaRapida_v1\BombaRapida_cfd.cfturbo-ccm")


def _java_bin():
    jc = _javac()
    if not jc:
        return None
    for name in ("java.exe", "java"):
        j = Path(jc[0]).with_name(name)
        if j.is_file():
            return str(j)
    return None


def test_judge(tmp: Path):
    """Compila y corre PxJudge (el mismo texto de la macro) con series de BombaRapida. None si no hay JDK."""
    jc, jv = _javac(), _java_bin()
    if not jc or not jv:
        return None
    d = tmp / "judge"
    d.mkdir(exist_ok=True)
    (d / "JudgeTest.java").write_text("""import java.util.*;
public class JudgeTest { public static void main(String[] a) {
  %s
  PxJudge j = new PxJudge();
  // BombaRapida (casos/BombaRapida/RESUMEN.md): H y M cada 50 iteraciones
  double[][] br = {{50, 15.81, 8.467}, {100, 16.67, 8.460}, {150, 16.73, 8.459}, {200, 16.75, 8.458}, {250, 16.755, 8.4575}};
  ArrayList<double[]> h = new ArrayList<double[]>();
  for (double[] s : br) { h.add(s); System.out.println("BR " + (int) s[0] + " " + j.var(h, 100)); }
  // oscila 2 %% alrededor de 14: nunca estable
  h = new ArrayList<double[]>();
  for (int i = 1; i <= 8; i++) { h.add(new double[] {50 * i, 14 + (i %% 2 == 0 ? 0.14 : -0.14), 4.8}); System.out.println("OSC " + 50 * i + " " + j.var(h, 100)); }
} }
""" % JUDGE, encoding="utf-8")
    r = subprocess.run([jc[0], "-nowarn", "-proc:none", "-d", str(d), str(d / "JudgeTest.java")], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=120)
    if r.returncode:
        return "javac: " + (r.stderr or r.stdout)[-1500:]
    r = subprocess.run([jv, "-cp", str(d), "JudgeTest"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    return {ln.split()[0] + " " + ln.split()[1]: float(ln.split()[2]) for ln in r.stdout.splitlines() if ln.split()[:1] in (["BR"], ["OSC"])}


def selftest() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # la consola cp1252 de Windows no imprime «≈»
    bad = 0

    def check(cond, msg):
        nonlocal bad
        print(("ok   " if cond else "FALLA ") + msg, flush=True)
        bad += 0 if cond else 1

    ccm = parse_ccm(str(CCM_REAL))
    z = ccm["zones"]
    check(abs(ccm["mass_flow"] - 13.8638888888889) < 1e-9, f"caudal másico {ccm['mass_flow']:.4f} kg/s")
    check(abs(ccm["rpm"] + 2900) < 1e-6, f"rpm con signo {ccm['rpm']:.1f}")
    check(ccm["axis"] == [0.0, 0.0, 1.0] and abs(ccm["density"] - 998.2) < 1e-9, "eje +Z y densidad 998,2")
    check([q["region"] for q in z] == ["estator", "rotor", "voluta", "extension"], f"regiones {[q['region'] for q in z]}")
    check([q["frame"] for q in z] == ["Static", "Rotating", "Static", "Static"], "frames Static/Rotating/Static/Static")
    check(z[1]["step"] == "BombaRapida_cfd_Co2.stp" and len(z[1]["rotating_walls"]) == 6, "rotor: STEP y 6 paredes rotatorias")
    check(short(z[3]["inflow"][0]) == "Inflow" and short(z[0]["static_walls"][0]) == "CasingHub", "nombres cortos de frontera")
    cad = str(CCM_REAL.parent)
    d2 = estimate_d2_mm(cad, ccm)
    check(d2 is not None and abs(d2 - 137) < 4, f"d2 medido en el STL = {d2:.1f} mm (nominal 137)" if d2 else "d2 no medido")
    tmp = Path(tempfile.mkdtemp(prefix="bomba_selftest_"))
    for mode in ("rapida", "normal"):
        body = macro_body(str(tmp / "caso"), cad, ccm, mode, {})
        check("PIPE importar" in body and "PIPE malla" in body and "PIPE euler" in body, f"marcadores PIPE ({mode})")
        err = compile_java(body, mode, tmp)
        if err == "SKIP":
            print("SKIP javac de STAR-CCM+ no encontrado: compilación omitida")
        else:
            check(err is None, f"javac {mode}" + ("" if err is None else "\n" + err))
    body = macro_body(str(tmp / "caso"), cad, ccm, "rapida", {"gamma": True, "gravedad": "0,0,-9.81", "beta2": 25, "euler": True})
    err = compile_java(body, "extra", tmp)
    check(err in (None, "SKIP"), "javac con gamma, gravedad y beta2" + ("" if err in (None, "SKIP") else "\n" + str(err)))
    err = compile_java(euler_check_body(), "check", tmp)
    check(err in (None, "SKIP"), "javac euler_check_body" + ("" if err in (None, "SKIP") else "\n" + str(err)))
    # iterar + post dentro de la MISMA macro (criterio por defecto: máx. 1000) y por separado
    full = macro_body(str(tmp / "caso"), cad, ccm, "rapida", {"iterar": True, "post": True})
    check("pxMax = 1000," in full and "ITER_STOP" in full and "PIPE iterar" in full and "PIPE post" in full, "macro con iterar+post: máx. 1000 y marcadores")
    check("/images\"" in full and "/presion_alabes.png\"" in full and "velocidad_relativa.png" in full and "yplus.png" in full and "EULER_OK" in full,
          "post: 3 PNG en casos/<caso>/images y validación de Euler dentro de la macro")
    check(full.count('createScene("presion_alabes")') == 1, "con post no se duplica la escena previa de presión")
    err = compile_java(full, "full", tmp)
    check(err in (None, "SKIP"), "javac macro rápida con iterar+post" + ("" if err in (None, "SKIP") else "\n" + str(err)))
    check("pxMax = 300," in macro_body(str(tmp / "c"), cad, ccm, "rapida", {"iterar": 300}), "iterar=300 cambia el máximo")
    sin = macro_body(str(tmp / "c"), cad, ccm, "rapida", {})
    check("ITER_STOP" not in sin and "POST_SECONDS" not in sin and 'createScene("presion_alabes")' in sin, "por defecto la macro no itera ni hace post")
    err = compile_java(post_body(str(tmp / "caso"), {"post_euler": False, "img_ancho": 800}), "post", tmp)
    check(err in (None, "SKIP"), "javac post_body suelto (sin Euler)" + ("" if err in (None, "SKIP") else "\n" + str(err)))
    err = compile_java(post_body(str(tmp / "caso")) + iterate_body(str(tmp / "caso"), {"tol": 0.01, "bloque": 25}), "postiter", tmp)
    check(err in (None, "SKIP"), "javac post_body + iterate_body sueltos" + ("" if err in (None, "SKIP") else "\n" + str(err)))
    jr = test_judge(tmp)
    if jr is None:
        print("SKIP java del JDK no encontrado: juez de estabilidad sin probar")
    elif isinstance(jr, str):
        check(False, "juez de estabilidad compila: " + jr)
    else:
        check(math.isnan(jr["BR 100"]) and jr["BR 150"] > 0.005 and jr["BR 200"] < 0.005,
              f"juez: BombaRapida estable en la iteración 200 (var {jr['BR 200'] * 100:.2f} %), no antes")
        check(all(v > 0.005 for k, v in jr.items() if k.startswith("OSC") and not math.isnan(v)), "juez: serie que oscila 2 % nunca es estable")
    # recetas generadas: al día con el pipeline y compilables con sus parámetros
    for fn, tx in recetas().items():
        f = RECETAS_DIR / fn
        al_dia = f.is_file() and f.read_text(encoding="utf-8").replace("\r\n", "\n") == tx.replace("\r\n", "\n")
        check(al_dia, f"receta {fn} al día (si no: python pipelines/bomba.py --write-recetas)")
        body = tx.replace("${case_dir}", (tmp / "caso").as_posix()).replace("${max_iter}", "300").replace("${reportes}", '"H", "M"')
        err = compile_java(body, "rec" + fn[:4], tmp)
        check(err in (None, "SKIP") and "${" not in body, f"javac receta {fn}" + ("" if err in (None, "SKIP") else "\n" + str(err)))
    # bloques Java de conocimiento/plantillas_casos.md (recetas de caso para run_steps)
    pl = HERE.parent / "conocimiento" / "plantillas_casos.md"
    bloques = re.findall(r"```java\n(.*?)```", pl.read_text(encoding="utf-8"), re.S) if pl.is_file() else []
    check(len(bloques) >= 13, f"plantillas_casos.md: {len(bloques)} bloques Java")
    for i, b in enumerate(bloques):
        m = re.match(r"// PARAMS: (.+)", b)
        for kv in (m.group(1).split(", ") if m else []):
            k, v = kv.split("=", 1)
            b = b.replace("${" + k.strip() + "}", v.strip())
        err = compile_java(b, f"plantilla{i}", tmp)
        check(err in (None, "SKIP"), f"javac plantilla bloque {i + 1}" + ("" if err in (None, "SKIP") else "\n" + str(err)))
    # la receta suelta compila con los marcadores sustituidos como lo hace el MCP
    txt = RECETA_EULER.read_text(encoding="utf-8")
    for k, v in (("MODO", "bomba"), ("H_REPORT", "H"), ("BETA2", ""), ("ENTRADA_ROTOR", ""), ("SALIDA_ROTOR", ""), ("RHO", "998.2")):
        txt = re.sub(rf"(?<![A-Za-z_\"]){k}(?![A-Za-z_])" if k == "RHO" else rf'"{k}"', v if k == "RHO" else f'"{v}"', txt)
    err = compile_java(txt, "receta", tmp)
    check(err in (None, "SKIP"), "javac receta reportes-euler-triangulos suelta" + ("" if err in (None, "SKIP") else "\n" + str(err)))
    try:
        macro_body(str(tmp), str(tmp), ccm)
        check(False, "debe fallar sin STEP")
    except ValueError:
        check(True, "ValueError si faltan los STEP")
    print("TODO BIEN" if not bad else f"{bad} fallos")
    return 1 if bad else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if "--write-recetas" in sys.argv:
        for fn, tx in recetas().items():
            (RECETAS_DIR / fn).write_text(tx, encoding="utf-8")
            print("escrita", RECETAS_DIR / fn)
        sys.exit(0)
    if "--emit" in sys.argv:
        a = sys.argv[sys.argv.index("--emit") + 1:]
        c = parse_ccm(a[0])
        print(macro_body(str(Path(a[1]).parent), a[1], c, a[2] if len(a) > 2 else "rapida", {}))
        sys.exit(0)
    print(__doc__)

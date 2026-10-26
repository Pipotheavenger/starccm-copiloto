"""Pipeline de bomba: genera el cuerpo Java de UNA macro para una bomba exportada por CFturbo.

Contrato (lo usa mcp_server.pump_case):
  parse_ccm(path) -> dict            lee el XML .cfturbo-ccm
  macro_body(case_dir, cad_dir, ccm, mode="rapida"|"normal", params={}) -> str   cuerpo de execute() (sin la clase)
  euler_check_body(params={}) -> str  solo la validación de Euler (para correr tras iterar)

Flujo (el verificado de BombaRapida/run.py, en una sola macro): importar STEP (una parte por zona) → regiones por
parte (nombres de cara) → interfaces en cadena → física SST + Cell Quality Remediation → MRF → fronteras (presión total
en la entrada, caudal con parámetro Flujo en la salida) → reportes H, M, P_eje, P_hid, eta → malla (una operación por
parte) → reportes de Euler → escena de presión. Imprime «PIPE <fase> <segundos>» por fase.
Modos: rapida = base d2/30 y 3 prismas; normal = base d2/60 y 5 prismas.

params opcionales: d2_mm, base_mm, prismas, flujo (kg/s), ptotal (Pa, 0), beta2 (grados), gamma (bool), gravedad
("gx,gy,gz"), remediacion (bool, True), euler (bool, True), escena (bool, True).
d2 (diámetro de salida del rotor): params["d2_mm"], o se mide en los STL de CFturbo que acompañan al CAD
(…Impeller…Outflow.stl); si no hay ninguna de las dos, ValueError.

Uso:  python pipelines/bomba.py --selftest
      python pipelines/bomba.py --emit <ccm> <cad_dir> [rapida|normal]     imprime el cuerpo Java
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
ExpressionReport rEta = repMgr.createReport(ExpressionReport.class); rEta.setPresentationName("eta"); rEta.setDefinition("${P_hidReport} / ${P_ejeReport}");
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
    out = out.replace("@@ESCENA@@", ESCENA if params.get("escena", True) else "")
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
    if "--emit" in sys.argv:
        a = sys.argv[sys.argv.index("--emit") + 1:]
        c = parse_ccm(a[0])
        print(macro_body(str(Path(a[1]).parent), a[1], c, a[2] if len(a) > 2 else "rapida", {}))
        sys.exit(0)
    print(__doc__)

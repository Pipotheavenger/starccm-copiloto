"""Caso BombaRapida: bomba de CFturbo (export «Flow domain», 4 STEP) con configuración rápida y cronómetro.

Objetivo: de importar a resultados en ≤ 10 min. Pocas macros grandes (cada inyección cuesta ~10 s):
  preparar  importar + regiones + interfaces + física + MRF + fronteras + reportes
  mallar    una operación por parte, base 4,5 mm = d2/30, 3 prismas
  iterar    bloques de 50 hasta ΔH, ΔM < 0,5 % en 100 iteraciones y continuidad < 1e-4, o hasta el presupuesto
  post      y+ en álabes, escena de presión, guardar
Uso (fuera del sandbox): .venv\\Scripts\\python cases\\BombaRapida\\run.py [--compile]
Datos (BombaRapida_cfd.cfturbo-ccm): ṁ = 13,864 kg/s (50 m³/h), −2900 rpm sobre +Z, agua 998,2 kg/m³.
"""
import json, sys, time
from pathlib import Path
sys.path.insert(0, r"C:\Users\sebas\OneDrive\Documentos\starccm-agent\starccm")
import mcp_server as m

CASE = Path(__file__).resolve().parent
CAD = (CASE / "cad").as_posix()
MDOT, RPM, RHO, MU = 13.8638888888889, -2900.0, 998.2, 0.0009982
BASE_MM, PRISMS = 4.5, 3
BUDGET_S, CHUNK, MAX_IT = 600, 50, 1500

PREPARAR = f"""
PartImportManager pim = sim.get(PartImportManager.class);
for (String f : new String[] {{"BombaRapida_cfd_Co1.stp", "BombaRapida_cfd_Co2.stp", "BombaRapida_cfd_Co3.stp", "BombaRapida_cfd_Co3_Extension.stp"}})
  pim.importCadPart("{CAD}/" + f, "SharpEdges", 30.0, 2, 1.0E-5);
sim.println("T_IMPORT " + System.currentTimeMillis());
Collection<GeometryPart> parts = sim.get(SimulationPartManager.class).getParts();
sim.getRegionManager().newRegionsFromParts(new ArrayList<GeometryPart>(parts), "OneRegionPerPart", null, "OneBoundaryPerPartSurface", null, false);
for (Region r : sim.getRegionManager().getRegions()) {{
  String n = r.getPresentationName();
  if (n.contains("Extension")) r.setPresentationName("extension");
  else if (n.contains("Stator")) r.setPresentationName("estator");
  else if (n.contains("Impeller")) r.setPresentationName("rotor");
  else if (n.contains("Volute")) r.setPresentationName("voluta");
  for (Boundary b : new ArrayList<Boundary>(r.getBoundaryManager().getBoundaries())) {{
    String bn = b.getPresentationName(); int i = bn.lastIndexOf('-');
    if (i >= 0) b.setPresentationName(bn.substring(i + 1));
  }}
}}
RegionManager rgm = sim.getRegionManager();
Region est = rgm.getRegion("estator"), rot = rgm.getRegion("rotor"), vol = rgm.getRegion("voluta"), ext = rgm.getRegion("extension");
InterfaceManager im = sim.getInterfaceManager();
im.createBoundaryInterface(est.getBoundaryManager().getBoundary("Outflow"), rot.getBoundaryManager().getBoundary("Inflow"), "int_entrada");
im.createBoundaryInterface(rot.getBoundaryManager().getBoundary("Outflow"), vol.getBoundaryManager().getBoundary("Inflow"), "int_salida");
im.createBoundaryInterface(vol.getBoundaryManager().getBoundary("Outflow"), ext.getBoundaryManager().getBoundary("Inflow"), "int_extension");
// física: reusa el continuo si existe; SST all-y+, estacionario, densidad constante, remediación de celdas
PhysicsContinuum pc = null;
for (Continuum c : sim.getContinuumManager().getObjects()) if (c instanceof PhysicsContinuum) pc = (PhysicsContinuum) c;
if (pc == null) pc = sim.getContinuumManager().createContinuum(PhysicsContinuum.class);
pc.setPresentationName("agua");
for (Class c : new Class[] {{ThreeDimensionalModel.class, SteadyModel.class, SingleComponentLiquidModel.class, SegregatedFlowModel.class, ConstantDensityModel.class,
                            TurbulentModel.class, RansTurbulenceModel.class, KOmegaTurbulence.class, SstKwTurbModel.class, KwAllYplusWallTreatment.class,
                            CellQualityRemediationModel.class}})
  pc.enable(c);
for (Region r : rgm.getRegions()) r.setPhysicsContinuum(pc);
Liquid liq = (Liquid) pc.getModelManager().getModel(SingleComponentLiquidModel.class).getMaterial();
((ConstantMaterialPropertyMethod) liq.getMaterialProperties().getMaterialProperty(ConstantDensityProperty.class).getMethod()).getQuantity().setValue({RHO});
((ConstantMaterialPropertyMethod) liq.getMaterialProperties().getMaterialProperty(DynamicViscosityProperty.class).getMethod()).getQuantity().setValue({MU});
// MRF
RotatingReferenceFrame frame = sim.get(ReferenceFrameManager.class).createReferenceFrame(RotatingReferenceFrame.class, "MRF_rotor");
frame.getRotationRate().setValueAndUnits({RPM}, sim.getUnitsManager().getUnits("rpm"));
frame.getAxisDirection().setComponents(0.0, 0.0, 1.0);
frame.getAxisOrigin().setComponents(0.0, 0.0, 0.0);
rot.getValues().get(MotionSpecification.class).setReferenceFrame(frame);
// fronteras de las plantillas: presión total 0 Pa en la entrada, caudal (parámetro Flujo) en la salida
Boundary inlet = est.getBoundaryManager().getBoundary("Inflow"), outlet = ext.getBoundaryManager().getBoundary("Outflow");
inlet.setPresentationName("inlet"); outlet.setPresentationName("outlet");
GlobalParameterManager gpm = sim.get(GlobalParameterManager.class);
ScalarGlobalParameter flujo = gpm.createGlobalParameter(ScalarGlobalParameter.class, "Flujo");
flujo.getQuantity().setValue({MDOT});
inlet.setBoundaryType(sim.get(ConditionTypeManager.class).get(StagnationBoundary.class));
inlet.getValues().get(TotalPressureProfile.class).getMethod(ConstantScalarProfileMethod.class).getQuantity().setValueAndUnits(0.0, sim.getUnitsManager().getUnits("Pa"));
outlet.setBoundaryType(sim.get(ConditionTypeManager.class).get(MassFlowBoundary.class));
outlet.getValues().get(MassFlowRateProfile.class).getMethod(ConstantScalarProfileMethod.class).getQuantity().setDefinition("-${{Flujo}}");
// reportes
ReportManager rm = sim.getReportManager();
FieldFunction ptot = sim.getFieldFunctionManager().getFunction("TotalPressure");
MassFlowAverageReport pin = rm.createReport(MassFlowAverageReport.class); pin.setPresentationName("pt_in"); pin.setFieldFunction(ptot); pin.getParts().setObjects(inlet);
MassFlowAverageReport pout = rm.createReport(MassFlowAverageReport.class); pout.setPresentationName("pt_out"); pout.setFieldFunction(ptot); pout.getParts().setObjects(outlet);
MassFlowReport qi = rm.createReport(MassFlowReport.class); qi.setPresentationName("Q_in"); qi.getParts().setObjects(inlet);
MassFlowReport qo = rm.createReport(MassFlowReport.class); qo.setPresentationName("Q_out"); qo.getParts().setObjects(outlet);
MomentReport mom = rm.createReport(MomentReport.class); mom.setPresentationName("M");
mom.getDirection().setComponents(0.0, 0.0, 1.0); mom.getOrigin().setComponents(0.0, 0.0, 0.0);
List<Boundary> walls = new ArrayList<Boundary>();
for (String n : new String[] {{"Hub", "Shroud", "BladeSS", "BladePS", "BladeLE", "BladeTE"}}) walls.add(rot.getBoundaryManager().getBoundary(n));
mom.getParts().setObjects(walls);
ExpressionReport H = rm.createReport(ExpressionReport.class); H.setPresentationName("H"); H.setDefinition("(${{pt_outReport}} - ${{pt_inReport}}) / ({RHO} * 9.80665)");
ExpressionReport Ph = rm.createReport(ExpressionReport.class); Ph.setPresentationName("P_hid"); Ph.setDefinition("abs(${{Q_inReport}}) * 9.80665 * ${{HReport}}");
ExpressionReport Pe = rm.createReport(ExpressionReport.class); Pe.setPresentationName("P_eje"); Pe.setDefinition("abs(${{MReport}}) * {abs(RPM)} * 2 * 3.14159265 / 60");
ExpressionReport eta = rm.createReport(ExpressionReport.class); eta.setPresentationName("eta"); eta.setDefinition("${{P_hidReport}} / ${{P_ejeReport}}");
for (Report r : new Report[] {{pin, pout, qi, qo, mom, H, Pe, eta}}) r.createMonitor();
// comprobación de interfaces: ninguna cara de conexión debe quedar como pared
for (Region r : rgm.getRegions()) for (Boundary b : r.getBoundaryManager().getBoundaries())
  sim.println("BOUNDARY\\t" + r.getPresentationName() + "/" + b.getPresentationName() + "\\t" + b.getBoundaryType().getPresentationName());
"""

MALLAR = f"""
Units mm = sim.getUnitsManager().getUnits("mm");
MeshOperationManager mom2 = sim.get(MeshOperationManager.class);
for (GeometryPart part : sim.get(SimulationPartManager.class).getParts()) {{
  String pn = part.getPresentationName();
  String n = pn.contains("Extension") ? "extension" : pn.contains("Stator") ? "estator" : pn.contains("Impeller") ? "rotor" : "voluta";
  AutoMeshOperation op = mom2.createAutoMeshOperation(
      Arrays.asList("star.resurfacer.ResurfacerAutoMesher", "star.resurfacer.AutomaticSurfaceRepairAutoMesher", "star.dualmesher.DualAutoMesher", "star.prismmesher.PrismAutoMesher"),
      new ArrayList<GeometryPart>(Arrays.asList(part)));
  op.setPresentationName("malla_" + n);
  op.getDefaultValues().get(BaseSize.class).setValueAndUnits({BASE_MM}, mm);
  op.getDefaultValues().get(NumPrismLayers.class).setNumLayers({PRISMS});
  op.getDefaultValues().get(PrismLayerStretching.class).setStretching(1.3);
  op.getDefaultValues().get(PrismThickness.class).setRelativeSize(10.0);
  if (n.equals("rotor")) {{
    SurfaceCustomMeshControl sc = op.getCustomMeshControls().createSurfaceControl();
    List<GeometryObject> blades = new ArrayList<GeometryObject>();
    for (PartSurface s : part.getPartSurfaces()) if (s.getPresentationName().contains("Blade")) blades.add(s);
    sc.getGeometryObjects().setObjects(blades);
    sc.getCustomConditions().get(PartsTargetSurfaceSizeOption.class).setSelected(PartsTargetSurfaceSizeOption.Type.CUSTOM);
    sc.getCustomValues().get(PartsTargetSurfaceSize.class).getRelativeSize().setPercentage(50.0);
  }}
  long t0 = System.currentTimeMillis();
  op.execute();
  sim.println("MESH_SECONDS\\t" + n + "\\t" + (System.currentTimeMillis() - t0) / 1000.0);
}}
FvRepresentation fv = (FvRepresentation) sim.getRepresentationManager().getObject("Volume Mesh");
sim.println("CELLS\\t" + fv.getCellCount());
"""

POST = """
Region rot = sim.getRegionManager().getRegion("rotor");
List<Boundary> blades = new ArrayList<Boundary>();
for (String n : new String[] {"BladeSS", "BladePS", "BladeLE", "BladeTE"}) blades.add(rot.getBoundaryManager().getBoundary(n));
FieldFunction yp = sim.getFieldFunctionManager().getFunction("WallYplus");
MaxReport ymax = sim.getReportManager().createReport(MaxReport.class); ymax.setPresentationName("yplus_max_alabes"); ymax.setFieldFunction(yp); ymax.getParts().setObjects(blades);
MinReport ymin = sim.getReportManager().createReport(MinReport.class); ymin.setPresentationName("yplus_min_alabes"); ymin.setFieldFunction(yp); ymin.getParts().setObjects(blades);
AreaAverageReport yavg = sim.getReportManager().createReport(AreaAverageReport.class); yavg.setPresentationName("yplus_media_alabes"); yavg.setFieldFunction(yp); yavg.getParts().setObjects(blades);
sim.println("YPLUS\\t" + ymin.getReportMonitorValue() + "\\t" + yavg.getReportMonitorValue() + "\\t" + ymax.getReportMonitorValue());
List<Boundary> bl = new ArrayList<Boundary>(blades); bl.add(rot.getBoundaryManager().getBoundary("Hub"));
Scene s1 = sim.getSceneManager().createScene("presion_alabes"); s1.initializeAndWait();
ScalarDisplayer d1 = s1.getDisplayerManager().createScalarDisplayer("p"); d1.initialize();
d1.setRepresentation(sim.getRepresentationManager().getDefaultFvRepresentation());
d1.getInputParts().setObjects(bl);
d1.getScalarDisplayQuantity().setFieldFunction(sim.getFieldFunctionManager().getFunction("Pressure"));
s1.resetCamera(true);
sim.println("SCENE\\t" + s1.getPresentationName());
"""


def compile_all():
    m.MACROS.mkdir(exist_ok=True)
    bad = 0
    for name, body in (("preparar", PREPARAR), ("mallar", MALLAR), ("post", POST)):
        p = m.MACROS / f"Agent_br_{name}.java"
        p.write_text(m._wrap(body, "", "br_" + name), encoding="utf-8")
        e = m._precompile(p); p.unlink()
        print(f"javac {name}: {'ok' if e is None else 'ERROR'}", flush=True)
        if e:
            bad += 1; print(e[:2500])
    return bad


def step(name, body, timeout=3600):
    r = m.run_java(body, timeout_s=timeout)
    out = r["stdout"] if r["ok"] else r["stdout"].split("AGENT_ERROR")[0]
    if not r["ok"]:
        print(f"!!! {name} falló: {r['exception']}\n{(r['trace'] or '')[:1500]}", flush=True)
        raise SystemExit(1)
    return out, r["seconds"]


def main():
    times = {}
    T0 = time.time()
    st = m.server_start(name="BombaRapida")
    times["arrancar solver"] = time.time() - T0
    print(f"solver pid {st['pid']} en {times['arrancar solver']:.1f}s", flush=True)

    t = time.time(); out, _ = step("preparar", PREPARAR); times["importar + configurar"] = time.time() - t
    paredes_mal = [l for l in out.splitlines() if l.startswith("BOUNDARY") and l.endswith("Wall")
                   and any(k in l for k in ("Inflow", "Outflow")) and "[" not in l]
    print(f"preparar {times['importar + configurar']:.0f}s; caras de conexión que quedaron pared: {paredes_mal or 'ninguna'}", flush=True)

    t = time.time(); out, _ = step("mallar", MALLAR); times["mallar"] = time.time() - t
    cells = next((int(l.split("\t")[1]) for l in out.splitlines() if l.startswith("CELLS")), None)
    print(f"malla {times['mallar']:.0f}s, {cells} celdas; " + "; ".join(l.replace("\t", " ") for l in out.splitlines() if l.startswith("MESH_SECONDS")), flush=True)

    rows, hist = [], []
    csv = CASE / "reports" / "convergencia.csv"; csv.parent.mkdir(exist_ok=True)
    csv.write_text("iter,seconds,H,M,pt_in,pt_out,Q_in,Q_out,eta,continuity,xmom\n", encoding="utf-8")
    t = time.time(); stop = "presupuesto"
    post_reserve = 60
    while True:
        r = m.iterate(CHUNK, timeout_s=3600)
        rep, res = r.get("reports", {}), r.get("residuals", {})
        it = r.get("iteration", -1)
        cont = res.get("Continuity: Residual")
        row = [it, r["seconds"], rep.get("H"), rep.get("M"), rep.get("pt_in"), rep.get("pt_out"), rep.get("Q_in"), rep.get("Q_out"),
               rep.get("eta"), cont, res.get("X-momentum: Residual")]
        with open(csv, "a", encoding="utf-8") as f:
            f.write(",".join("" if v is None else f"{v:.6g}" if isinstance(v, float) else str(v) for v in row) + "\n")
        rows.append(row); hist.append((it, rep.get("H"), rep.get("M")))
        el = time.time() - T0
        print(f"iter {it:5d} | {r['seconds']:5.0f}s | H {rep.get('H', float('nan')):7.3f} | M {rep.get('M', float('nan')):7.3f} | "
              f"eta {rep.get('eta', float('nan')):.4f} | cont {cont if cont is None else f'{cont:.2e}'} | total {el:5.0f}s", flush=True)
        if not r["ok"]:
            stop = "error"; print(r.get("exception")); break
        if len(hist) >= 3 and None not in hist[-1][1:] + hist[-3][1:]:
            dH = abs(hist[-1][1] - hist[-3][1]) / max(abs(hist[-1][1]), 1e-9)
            dM = abs(hist[-1][2] - hist[-3][2]) / max(abs(hist[-1][2]), 1e-9)
            if dH < 0.005 and dM < 0.005 and cont is not None and cont < 1e-4:
                stop = f"criterio (ΔH {dH*100:.2f} %, ΔM {dM*100:.2f} % en 100 it)"; break
        per_chunk = r["seconds"]
        if el + per_chunk + post_reserve > BUDGET_S or it >= MAX_IT:
            stop = "presupuesto de tiempo" if it < MAX_IT else "máximo de iteraciones"; break
    times["iterar"] = time.time() - t

    t = time.time()
    out, _ = step("post", POST)
    yl = next((l.split("\t")[1:] for l in out.splitlines() if l.startswith("YPLUS")), None)
    (CASE / "images").mkdir(exist_ok=True)
    m.export_scene("presion_alabes", str(CASE / "images" / "presion_alabes.png"))
    m.save()
    times["post (y+, escena, guardar)"] = time.time() - t
    times["TOTAL"] = time.time() - T0
    last = rows[-1]
    resumen = {"times_s": {k: round(v, 1) for k, v in times.items()}, "cells": cells, "iterations": last[0], "stop": stop,
               "H": last[2], "M": last[3], "eta": last[8], "Q_in": last[6], "Q_out": last[7],
               "desbalance_pct": abs(abs(last[6]) - abs(last[7])) / abs(last[6]) * 100 if last[6] else None,
               "continuity": last[9], "yplus_min_media_max": yl, "paredes_en_conexion": paredes_mal}
    (CASE / "reports" / "tiempos.json").write_text(json.dumps(resumen, indent=1, ensure_ascii=False), encoding="utf-8")
    print("RESUMEN " + json.dumps(resumen), flush=True)  # ASCII: la consola de Windows (cp1252) no imprime «Δ»
    t = time.time(); print("stop:", m.server_stop(save=False).get("stopped"), f"{time.time() - t:.0f}s", flush=True)


if __name__ == "__main__":
    if "--compile" in sys.argv:
        sys.exit(compile_all())
    if compile_all():
        sys.exit("errores de compilación")
    main()

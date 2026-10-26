"""Caso cursocfturbo: bomba centrífuga de CFturbo (3 STEP) de punta a punta con el MCP de starccm/.

Uso (desde starccm/, fuera del sandbox):
  .venv\\Scripts\\python cases\\cursocfturbo\\pump.py --compile          solo javac de todos los pasos
  .venv\\Scripts\\python cases\\cursocfturbo\\pump.py --steps importar,regiones,...   ejecuta esos pasos
  .venv\\Scripts\\python cases\\cursocfturbo\\pump.py --run 2000        itera hasta convergencia
  .venv\\Scripts\\python cases\\cursocfturbo\\pump.py --scenes           escenas y PNG
Datos (cursocfturbo.cfturbo-ccm): ṁ = 69.486 kg/s (Q = 250.6 m³/h), n = −49.667 1/s = −2980 rpm
sobre +Z, agua ρ = 998.2 kg/m³, μ = 9.982e-4 Pa·s, D2 = 270 mm.
"""
import sys, json, time, argparse
from pathlib import Path
sys.path.insert(0, r"C:\Users\sebas\OneDrive\Documentos\starccm-agent\starccm")
import mcp_server as m

CASE = Path(__file__).resolve().parent
CAD = (CASE / "cad").as_posix()
MDOT, RPM, RHO, MU, D2 = 69.4858111111111, -2980.0, 998.2, 0.0009982, 0.270
BASE_MM = 5.0

STEPS = {}
STEPS["importar"] = ("importa los 3 STEP de CFturbo como partes de geometría con sus caras nombradas (PartImportManager)", f"""
PartImportManager pim = sim.get(PartImportManager.class);
for (String f : new String[] {{"cursocfturbo_Co1.stp", "cursocfturbo_Co2.stp", "cursocfturbo_Co3.stp"}})
  pim.importCadPart("{CAD}/" + f, "SharpEdges", 30.0, 2, 1.0E-5);
for (GeometryPart p : sim.get(SimulationPartManager.class).getParts()) {{
  sim.println("PART\\t" + p.getPresentationName() + "\\t" + p.getClass().getSimpleName());
  for (PartSurface s : p.getPartSurfaces()) sim.println("SURFACE\\t" + p.getPresentationName() + "/" + s.getPresentationName());
}}
""")
STEPS["regiones"] = ("una región por parte, una frontera por superficie; renombra regiones estator/rotor/voluta y fronteras por el sufijo tras el último '-'", """
Collection<GeometryPart> parts = sim.get(SimulationPartManager.class).getParts();
sim.getRegionManager().newRegionsFromParts(new ArrayList<GeometryPart>(parts), "OneRegionPerPart", null, "OneBoundaryPerPartSurface", null, false);
for (Region r : sim.getRegionManager().getRegions()) {
  String n = r.getPresentationName();
  if (n.contains("Stator")) r.setPresentationName("estator");
  else if (n.contains("Impeller")) r.setPresentationName("rotor");
  else if (n.contains("Volute")) r.setPresentationName("voluta");
  for (Boundary b : new ArrayList<Boundary>(r.getBoundaryManager().getBoundaries())) {
    String bn = b.getPresentationName();
    int i = bn.lastIndexOf('-');
    if (i >= 0) b.setPresentationName(bn.substring(i + 1));
  }
  sim.println("REGION\\t" + r.getPresentationName());
  for (Boundary b : r.getBoundaryManager().getBoundaries()) sim.println("BOUNDARY\\t" + r.getPresentationName() + "/" + b.getPresentationName());
}
""")
STEPS["interfaces"] = ("interfaces internas estator→rotor (Outflow/Inflow) y rotor→voluta; in-place, frozen rotor con MRF", """
Region est = sim.getRegionManager().getRegion("estator"), rot = sim.getRegionManager().getRegion("rotor"), vol = sim.getRegionManager().getRegion("voluta");
BoundaryInterface i1 = sim.getInterfaceManager().createBoundaryInterface(est.getBoundaryManager().getBoundary("Outflow"), rot.getBoundaryManager().getBoundary("Inflow"), "int_entrada");
BoundaryInterface i2 = sim.getInterfaceManager().createBoundaryInterface(rot.getBoundaryManager().getBoundary("Outflow"), vol.getBoundaryManager().getBoundary("Inflow"), "int_salida");
for (Interface i : sim.getInterfaceManager().getObjects())
  sim.println("INTERFACE\\t" + i.getPresentationName() + "\\t" + i.getClass().getSimpleName());
""")
STEPS["fisica"] = ("reusa el continuo 'Physics 1': 3D estacionario, líquido, segregado, densidad constante, k-ω SST all-y+, y lo asigna a las 3 regiones", """
PhysicsContinuum pc = null;
for (Continuum c : sim.getContinuumManager().getObjects()) if (c instanceof PhysicsContinuum) pc = (PhysicsContinuum) c;
if (pc == null) pc = sim.getContinuumManager().createContinuum(PhysicsContinuum.class);
pc.setPresentationName("agua");
for (Class c : new Class[] {ThreeDimensionalModel.class, SteadyModel.class, SingleComponentLiquidModel.class, SegregatedFlowModel.class, ConstantDensityModel.class,
                            TurbulentModel.class, RansTurbulenceModel.class, KOmegaTurbulence.class, SstKwTurbModel.class, KwAllYplusWallTreatment.class})
  pc.enable(c);
for (Region r : sim.getRegionManager().getRegions()) r.setPhysicsContinuum(pc);
for (Model mdl : pc.getModelManager().getObjects()) sim.println("MODEL\\t" + mdl.getPresentationName());
""")
STEPS["propiedades-agua"] = (f"densidad y viscosidad del agua del .cfturbo-ccm en el modelo de líquido", f"""
PhysicsContinuum pc = (PhysicsContinuum) sim.getContinuumManager().getContinuum("agua");
SingleComponentLiquidModel lm = pc.getModelManager().getModel(SingleComponentLiquidModel.class);
Liquid liq = (Liquid) lm.getMaterial();
((ConstantMaterialPropertyMethod) liq.getMaterialProperties().getMaterialProperty(ConstantDensityProperty.class).getMethod()).getQuantity().setValue({RHO});
((ConstantMaterialPropertyMethod) liq.getMaterialProperties().getMaterialProperty(DynamicViscosityProperty.class).getMethod()).getQuantity().setValue({MU});
sim.println("RHO " + ((ConstantMaterialPropertyMethod) liq.getMaterialProperties().getMaterialProperty(ConstantDensityProperty.class).getMethod()).getQuantity().getRawValue());
""")
STEPS["mrf-rotor"] = (f"marco de referencia rotatorio (MRF) {RPM} rpm sobre +Z en el origen, creado directo en ReferenceFrameManager, asignado a la región rotor", f"""
RotatingReferenceFrame frame = sim.get(ReferenceFrameManager.class).createReferenceFrame(RotatingReferenceFrame.class, "MRF_rotor");
frame.getRotationRate().setValueAndUnits({RPM}, sim.getUnitsManager().getUnits("rpm"));
frame.getAxisDirection().setComponents(0.0, 0.0, 1.0);
frame.getAxisOrigin().setComponents(0.0, 0.0, 0.0);
Region rot = sim.getRegionManager().getRegion("rotor");
rot.getValues().get(MotionSpecification.class).setReferenceFrame(frame);
sim.println("MRF rotor -> " + frame.getPresentationName() + " " + frame.getRotationRate().getRawValue() + " " + frame.getRotationRate().getUnits().getPresentationName());
""")
STEPS["fronteras"] = (f"inlet = mass flow inlet {MDOT} kg/s (estator/Inflow), outlet = pressure outlet 0 Pa (voluta/Outflow); renombra a inlet/outlet", f"""
Region est = sim.getRegionManager().getRegion("estator"), vol = sim.getRegionManager().getRegion("voluta");
Boundary inlet = est.getBoundaryManager().getBoundary("Inflow");
Boundary outlet = vol.getBoundaryManager().getBoundary("Outflow");
inlet.setBoundaryType(sim.get(ConditionTypeManager.class).get(MassFlowBoundary.class));
inlet.getValues().get(MassFlowRateProfile.class).getMethod(ConstantScalarProfileMethod.class).getQuantity().setValueAndUnits({MDOT}, sim.getUnitsManager().getUnits("kg/s"));
outlet.setBoundaryType(sim.get(ConditionTypeManager.class).get(PressureBoundary.class));
inlet.setPresentationName("inlet"); outlet.setPresentationName("outlet");
sim.println("INLET " + inlet.getBoundaryType().getPresentationName());
sim.println("OUTLET " + outlet.getBoundaryType().getPresentationName());
""")
STEPS["reportes"] = (f"reportes: pt_in/pt_out (promedio másico de presión total), Q_in/Q_out, M (momento en álabes+hub+shroud sobre Z), expresiones H, P_hid, P_eje, eta, y monitores", f"""
Region est = sim.getRegionManager().getRegion("estator"), rot = sim.getRegionManager().getRegion("rotor"), vol = sim.getRegionManager().getRegion("voluta");
FieldFunction ptot = sim.getFieldFunctionManager().getFunction("TotalPressure");
MassFlowAverageReport pin = sim.getReportManager().createReport(MassFlowAverageReport.class);
pin.setPresentationName("pt_in"); pin.setFieldFunction(ptot); pin.getParts().setObjects(est.getBoundaryManager().getBoundary("inlet"));
MassFlowAverageReport pout = sim.getReportManager().createReport(MassFlowAverageReport.class);
pout.setPresentationName("pt_out"); pout.setFieldFunction(ptot); pout.getParts().setObjects(vol.getBoundaryManager().getBoundary("outlet"));
MassFlowReport qi = sim.getReportManager().createReport(MassFlowReport.class);
qi.setPresentationName("Q_in"); qi.getParts().setObjects(est.getBoundaryManager().getBoundary("inlet"));
MassFlowReport qo = sim.getReportManager().createReport(MassFlowReport.class);
qo.setPresentationName("Q_out"); qo.getParts().setObjects(vol.getBoundaryManager().getBoundary("outlet"));
MomentReport mom = sim.getReportManager().createReport(MomentReport.class);
mom.setPresentationName("M");
mom.getDirection().setComponents(0.0, 0.0, 1.0);
mom.getOrigin().setComponents(0.0, 0.0, 0.0);
List<Boundary> walls = new ArrayList<Boundary>();
for (String n : new String[] {{"Hub", "Shroud", "BladeSS", "BladePS", "BladeLE", "BladeTE"}}) walls.add(rot.getBoundaryManager().getBoundary(n));
mom.getParts().setObjects(walls);
ExpressionReport H = sim.getReportManager().createReport(ExpressionReport.class);
H.setPresentationName("H"); H.setDefinition("(${{pt_outReport}} - ${{pt_inReport}}) / ({RHO} * 9.80665)");
ExpressionReport Ph = sim.getReportManager().createReport(ExpressionReport.class);
Ph.setPresentationName("P_hid"); Ph.setDefinition("abs(${{Q_inReport}}) * 9.80665 * ${{HReport}}");
ExpressionReport Pe = sim.getReportManager().createReport(ExpressionReport.class);
Pe.setPresentationName("P_eje"); Pe.setDefinition("abs(${{MReport}}) * {abs(RPM)} * 2 * 3.14159265 / 60");
ExpressionReport eta = sim.getReportManager().createReport(ExpressionReport.class);
eta.setPresentationName("eta"); eta.setDefinition("${{P_hidReport}} / ${{P_ejeReport}}");
for (Report r : new Report[] {{pin, pout, qi, qo, mom, H, Pe, eta}}) r.createMonitor();
for (Report r : sim.getReportManager().getObjects()) sim.println("REPORT\\t" + r.getPresentationName() + "\\t" + r.getClass().getSimpleName());
""")
STEPS["malla"] = (f"UNA operación de malla POR PARTE (las 3 juntas fallan: 'Surface intersects self' en las caras coincidentes de las interfaces): remallador + reparación automática + poliédrica + 5 prismas (1,3; 10 % del base), base {BASE_MM} mm = D2/54, álabes al 50 %", f"""
Units mm = sim.getUnitsManager().getUnits("mm");
MeshOperationManager mom = sim.get(MeshOperationManager.class);
for (GeometryPart part : sim.get(SimulationPartManager.class).getParts()) {{
  String n = part.getPresentationName().contains("Stator") ? "estator" : part.getPresentationName().contains("Impeller") ? "rotor" : "voluta";
  AutoMeshOperation op = mom.createAutoMeshOperation(
      Arrays.asList("star.resurfacer.ResurfacerAutoMesher", "star.resurfacer.AutomaticSurfaceRepairAutoMesher", "star.dualmesher.DualAutoMesher", "star.prismmesher.PrismAutoMesher"),
      new ArrayList<GeometryPart>(Arrays.asList(part)));
  op.setPresentationName("malla_" + n);
  op.getDefaultValues().get(BaseSize.class).setValueAndUnits({BASE_MM}, mm);
  op.getDefaultValues().get(NumPrismLayers.class).setNumLayers(5);
  op.getDefaultValues().get(PrismLayerStretching.class).setStretching(1.3);
  op.getDefaultValues().get(PrismThickness.class).setRelativeSize(10.0);
  if (n.equals("rotor")) {{
    SurfaceCustomMeshControl sc = op.getCustomMeshControls().createSurfaceControl();
    sc.setPresentationName("alabes");
    List<GeometryObject> blades = new ArrayList<GeometryObject>();
    for (PartSurface s : part.getPartSurfaces()) if (s.getPresentationName().contains("Blade")) blades.add(s);
    sc.getGeometryObjects().setObjects(blades);
    sc.getCustomConditions().get(PartsTargetSurfaceSizeOption.class).setSelected(PartsTargetSurfaceSizeOption.Type.CUSTOM);
    sc.getCustomValues().get(PartsTargetSurfaceSize.class).getRelativeSize().setPercentage(50.0);
  }}
  long t0 = System.currentTimeMillis();
  op.execute();
  sim.println("MESH_SECONDS " + n + " " + (System.currentTimeMillis() - t0) / 1000.0);
}}
FvRepresentation fv = (FvRepresentation) sim.getRepresentationManager().getObject("Volume Mesh");
sim.println("CELLS " + fv.getCellCount());
""")
STEPS["escenas"] = ("escenas: presión en álabes, velocidad en plano z = 30 mm, y+ en paredes del rotor", """
Region rot = sim.getRegionManager().getRegion("rotor");
List<Boundary> blades = new ArrayList<Boundary>();
for (String n : new String[] {"BladeSS", "BladePS", "BladeLE", "BladeTE", "Hub"}) blades.add(rot.getBoundaryManager().getBoundary(n));
Scene s1 = sim.getSceneManager().createScene("presion_alabes"); s1.initializeAndWait();
ScalarDisplayer d1 = s1.getDisplayerManager().createScalarDisplayer("p"); d1.initialize();
d1.setRepresentation(sim.getRepresentationManager().getDefaultFvRepresentation());
d1.getInputParts().setObjects(blades);
d1.getScalarDisplayQuantity().setFieldFunction(sim.getFieldFunctionManager().getFunction("Pressure"));
s1.resetCamera(true);
List<Region> regs = new ArrayList<Region>(sim.getRegionManager().getRegions());
PlaneSection plane = sim.getPartManager().createImplicitPart(new NeoObjectVector(regs.toArray()), new DoubleVector(new double[] {0.0, 0.0, 1.0}), new DoubleVector(new double[] {0.0, 0.0, 0.030}), 0, 1, new DoubleVector(new double[] {0.0}));
plane.setPresentationName("plano_medio");
Scene s2 = sim.getSceneManager().createScene("velocidad_plano"); s2.initializeAndWait();
ScalarDisplayer d2 = s2.getDisplayerManager().createScalarDisplayer("v"); d2.initialize();
d2.setRepresentation(sim.getRepresentationManager().getDefaultFvRepresentation());
d2.getInputParts().setObjects(plane);
d2.getScalarDisplayQuantity().setFieldFunction(((PrimitiveFieldFunction) sim.getFieldFunctionManager().getFunction("Velocity")).getMagnitudeFunction());
s2.resetCamera(true);
List<Boundary> walls = new ArrayList<Boundary>();
for (Region r : regs) for (Boundary b : r.getBoundaryManager().getBoundaries()) if (b.getBoundaryType() instanceof WallBoundary) walls.add(b);
Scene s3 = sim.getSceneManager().createScene("yplus"); s3.initializeAndWait();
ScalarDisplayer d3 = s3.getDisplayerManager().createScalarDisplayer("y+"); d3.initialize();
d3.setRepresentation(sim.getRepresentationManager().getDefaultFvRepresentation());
d3.getInputParts().setObjects(walls);
d3.getScalarDisplayQuantity().setFieldFunction(sim.getFieldFunctionManager().getFunction("WallYplus"));
s3.resetCamera(true);
MaxReport ymax = sim.getReportManager().createReport(MaxReport.class); ymax.setPresentationName("yplus_max_alabes");
ymax.setFieldFunction(sim.getFieldFunctionManager().getFunction("WallYplus")); ymax.getParts().setObjects(blades);
MinReport ymin = sim.getReportManager().createReport(MinReport.class); ymin.setPresentationName("yplus_min_alabes");
ymin.setFieldFunction(sim.getFieldFunctionManager().getFunction("WallYplus")); ymin.getParts().setObjects(blades);
sim.println("YPLUS alabes min " + ymin.getReportMonitorValue() + " max " + ymax.getReportMonitorValue());
for (Scene s : sim.getSceneManager().getScenes()) sim.println("SCENE\\t" + s.getPresentationName());
""")


def precompile(names):
    m.MACROS.mkdir(exist_ok=True)
    bad = 0
    for name in names:
        tag = "pre_" + name.replace("-", "_")
        p = m.MACROS / f"Agent_{tag}.java"
        p.write_text(m._wrap(STEPS[name][1], "", tag), encoding="utf-8")
        e = m._precompile(p); p.unlink()
        print(f"javac {name}: {'ok' if e is None else 'ERROR'}", flush=True)
        if e:
            bad += 1; print(e[:2000])
    return bad


def run_steps(names):
    st = m.server_start(sim_path=str(m.SIMS / "cursocfturbo.sim") if (m.SIMS / "cursocfturbo.sim").is_file() else "", name="cursocfturbo")
    print("server", st["pid"], st.get("startup_seconds"), "s", st.get("reused", ""), flush=True)
    for name in names:
        note, body = STEPS[name]
        r = m.run_java(body, timeout_s=7200)
        out = r["stdout"] if r["ok"] else r["stdout"].split("AGENT_ERROR")[0]
        print(f"\n=== {name} ({r['seconds']}s) ok={r['ok']}\n{out[-3000:]}", flush=True)
        if not r["ok"]:
            print("EXC:", r["exception"]); print((r["trace"] or "")[:700]); return False
        m.recipe_save("bomba-" + name, body, note)
        print("save:", m.save()["ok"], flush=True)
    return True


def run_solver(max_iter, chunk=100):
    st = m.server_start(sim_path=str(m.SIMS / "cursocfturbo.sim"), name="cursocfturbo")
    print("server", st["pid"], st.get("reused", ""), flush=True)
    hist = []
    log = CASE / "reports" / "convergencia.csv"; log.parent.mkdir(exist_ok=True)
    if not log.is_file():
        log.write_text("iter,seconds,H,M,pt_in,pt_out,Q_in,Q_out,eta,continuity,xmom\n", encoding="utf-8")
    while True:
        r = m.iterate(chunk, timeout_s=7200)
        rep, res = r.get("reports", {}), r.get("residuals", {})
        it = r.get("iteration", -1)
        row = [it, r["seconds"], rep.get("H"), rep.get("M"), rep.get("pt_in"), rep.get("pt_out"), rep.get("Q_in"), rep.get("Q_out"), rep.get("eta"),
               res.get("Continuity: Residual"), res.get("X-momentum: Residual")]
        with open(log, "a", encoding="utf-8") as f:
            f.write(",".join("" if v is None else f"{v:.6g}" if isinstance(v, float) else str(v) for v in row) + "\n")
        print(f"iter {it:5d} | {r['seconds']:6.0f}s | H {rep.get('H', float('nan')):8.3f} m | M {rep.get('M', float('nan')):9.2f} N·m | eta {rep.get('eta', float('nan')):.4f} | "
              f"Qin {rep.get('Q_in', float('nan')):8.3f} Qout {rep.get('Q_out', float('nan')):8.3f} | cont {res.get('Continuity: Residual', float('nan')):.2e}", flush=True)
        if not r["ok"]:
            print("EXC:", r.get("exception")); print((r.get("trace") or "")[:1500]); break
        hist.append((it, rep.get("H"), rep.get("M")))
        if len(hist) >= 2 and all(v is not None for v in hist[-1][1:] + hist[-2][1:]):
            dH = abs(hist[-1][1] - hist[-2][1]) / max(abs(hist[-1][1]), 1e-9)
            dM = abs(hist[-1][2] - hist[-2][2]) / max(abs(hist[-1][2]), 1e-9)
            cont = res.get("Continuity: Residual")
            resid_ok = cont is not None and cont < 1e-4
            print(f"      dH {dH*100:.2f}%  dM {dM*100:.2f}%  continuidad<1e-4: {resid_ok}", flush=True)
            if dH < 0.005 and dM < 0.005 and resid_ok and it >= 300:
                print("CONVERGIDO"); break
        if it >= max_iter:
            print("MAX ITER"); break
        if it % 500 == 0:
            m.save()
    print("save:", m.save(), flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--compile", action="store_true")
    ap.add_argument("--steps", default="")
    ap.add_argument("--run", type=int, default=0)
    ap.add_argument("--scenes", action="store_true")
    a = ap.parse_args()
    if a.compile:
        sys.exit(precompile(list(STEPS)))
    if a.steps:
        names = list(STEPS) if a.steps == "all" else a.steps.split(",")
        if precompile(names):
            sys.exit("errores de compilación")
        ok = run_steps(names)
        sys.exit(0 if ok else 1)
    if a.run:
        run_solver(a.run)
    if a.scenes:
        if precompile(["escenas"]):
            sys.exit("errores de compilación")
        run_steps(["escenas"])
        for s in ("presion_alabes", "velocidad_plano", "yplus"):
            print(m.export_scene(s, str(CASE / "images" / f"{s}.png")))

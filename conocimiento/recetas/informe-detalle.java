// QUE: vuelca TODO el detalle de la simulación para el informe PDF: líneas «D\tsección\tobjeto\tpropiedad\tvalor»
// VERSION: 19.06.009-R8 · FECHA: 2026-10-24 · ESTADO: compila con javac; verificado con el solver local (ver CLAUDE.md)
// Lo que no existe en una versión o en un objeto se salta (reflexión + try/catch): nunca tumba la macro.
final Simulation S = sim;
class D {
  void p(String sec, String obj, String k, Object v) {
    if (v == null) return;
    String s = String.valueOf(v).replace('\t', ' ').replace('\n', ' ').replace('\r', ' ');
    if (s.length() > 400) s = s.substring(0, 400) + "…";
    S.println("D\t" + sec + "\t" + obj + "\t" + k + "\t" + s);
  }
  Object call(Object o, String m) {
    if (o == null) return null;
    try { java.lang.reflect.Method mt = o.getClass().getMethod(m); return mt.invoke(o); } catch (Throwable e) { return null; }
  }
  String name(Object o) {
    if (o == null) return null;
    if (o instanceof NamedObject) return ((NamedObject) o).getPresentationName();
    Object n = call(o, "getPresentationName");
    return n != null ? n.toString() : String.valueOf(o);
  }
  String vec(Object o) {
    Object v = call(o, "getSIVector");
    if (v == null) v = call(o, "getComponents");
    if (v instanceof DoubleVector) { DoubleVector d = (DoubleVector) v; StringBuilder b = new StringBuilder("(");
      for (int i = 0; i < d.size(); i++) b.append(i > 0 ? ", " : "").append(String.format(java.util.Locale.ROOT, "%.6g", d.get(i)));
      return b.append(")").toString(); }
    return null;
  }
  String q(Object o) {  // cantidad física, perfil o método constante -> «valor unidades»
    if (o == null) return null;
    Object m = call(o, "getMethod");
    if (m != null && m != o) { Object qq = call(m, "getQuantity"); if (qq != null) { String s = q(qq); if (s != null) return s; }
      String mv = q(m); if (mv != null) return mv; return "(" + m.getClass().getSimpleName().replace("ProfileMethod", "").replace("Method", "") + ")"; }
    Object def = call(o, "getDefinition"), un = call(o, "getUnits");
    if (def != null && !def.toString().isEmpty()) return def + (un != null ? " " + name(un) : "");
    Object val = call(o, "getRawValue");
    if (val == null) val = call(o, "getValue");
    if (val instanceof Number) return val + (un != null ? " " + name(un) : "");
    String v = vec(o);
    if (v != null) return v + (un != null ? " " + name(un) : "");
    Object sel = call(o, "getSelectedElement");
    if (sel == null) sel = call(o, "getSelected");
    if (sel != null) return name(sel);
    return null;
  }
  void values(String sec, String obj, Object mgr) {  // ValueManager / ConditionManager / ReferenceValueManager...
    Object objs = call(mgr, "getObjects");
    if (!(objs instanceof Collection)) return;
    for (Object v : (Collection) objs) { String s = q(v); if (s == null) s = optionOf(v); p(sec, obj, name(v), s); }
  }
  String optionOf(Object c) {  // condición con una opción dentro (getXxxOption().getSelectedElement())
    for (java.lang.reflect.Method mt : c.getClass().getMethods())
      if (mt.getParameterCount() == 0 && mt.getName().startsWith("get") && mt.getName().endsWith("Option"))
        try { Object op = mt.invoke(c); Object sel = call(op, "getSelectedElement"); if (sel != null) return name(sel); } catch (Throwable e) { }
    return null;
  }
  void some(String sec, String obj, Object o, String... ms) { for (String m : ms) { Object v = call(o, m); if (v != null) { String s = (v instanceof Number || v instanceof Boolean || v instanceof String) ? v.toString() : q(v); if (s == null) s = name(v); p(sec, obj, m.replaceFirst("^(get|is)", ""), s); } } }
}
D d = new D();
// general
d.p("general", "simulación", "nombre", sim.getPresentationName());
d.p("general", "simulación", "archivo", sim.getSessionPath());
d.p("general", "simulación", "iteración", sim.getSimulationIterator().getCurrentIteration());
d.some("general", "simulación", sim, "getNumberOfWorkers", "getVersion");
try { d.p("general", "simulación", "versión", sim.getStarVersion().getString("ReleaseNumber")); } catch (Throwable e) { }
// geometría
for (GeometryPart gp : sim.get(SimulationPartManager.class).getParts()) {
  d.p("geometria", gp.getPresentationName(), "tipo", gp.getClass().getSimpleName());
  StringBuilder b = new StringBuilder(); int n = 0;
  for (PartSurface s : gp.getPartSurfaces()) { b.append(n++ > 0 ? ", " : "").append(s.getPresentationName()); }
  d.p("geometria", gp.getPresentationName(), "superficies (" + n + ")", b);
}
// regiones y fronteras
for (Region r : sim.getRegionManager().getRegions()) {
  String rn = r.getPresentationName();
  d.p("regiones", rn, "tipo", d.name(r.getRegionType()));
  try { d.p("regiones", rn, "continuo físico", r.getPhysicsContinuum().getPresentationName()); } catch (Throwable e) { }
  try { d.p("regiones", rn, "marco de referencia", d.name(r.getValues().get(MotionSpecification.class).getReferenceFrame())); } catch (Throwable e) { }
  try { d.p("regiones", rn, "movimiento", d.name(r.getValues().get(MotionSpecification.class).getMotion())); } catch (Throwable e) { }
  d.p("regiones", rn, "fronteras", r.getBoundaryManager().getBoundaries().size());
  for (Boundary b : r.getBoundaryManager().getBoundaries()) {
    String bn = rn + "/" + b.getPresentationName();
    d.p("fronteras", bn, "tipo", b.getBoundaryType().getPresentationName());
    d.values("fronteras", bn, b.getConditions());
    d.values("fronteras", bn, b.getValues());
  }
}
for (Interface i : sim.getInterfaceManager().getObjects()) {
  d.p("interfaces", i.getPresentationName(), "tipo", i.getClass().getSimpleName());
  for (String m : new String[] {"getBoundary0", "getBoundary1", "getRegion0", "getRegion1"}) { Object o = d.call(i, m); if (o != null) d.p("interfaces", i.getPresentationName(), m.substring(3), d.name(o)); }
  d.some("interfaces", i.getPresentationName(), i, "getTopology");
  d.values("interfaces", i.getPresentationName(), i.getConditions());
  d.values("interfaces", i.getPresentationName(), i.getValues());
}
// física, materiales, valores de referencia y condiciones iniciales
for (Continuum c : sim.getContinuumManager().getObjects()) {
  String cn = c.getPresentationName();
  d.p("fisica", cn, "tipo", c.getClass().getSimpleName());
  if (!(c instanceof PhysicsContinuum)) continue;
  PhysicsContinuum pc = (PhysicsContinuum) c;
  StringBuilder b = new StringBuilder(); int n = 0;
  for (Model m : pc.getModelManager().getObjects()) {
    b.append(n++ > 0 ? " · " : "").append(m.getPresentationName());
    Object mat = d.call(m, "getMaterial");
    if (mat != null) {
      String mn = d.name(mat);
      d.p("materiales", mn, "modelo", m.getPresentationName());
      Object props = d.call(d.call(mat, "getMaterialProperties"), "getObjects");
      if (props instanceof Collection) for (Object pr : (Collection) props) { String s = d.q(pr); d.p("materiales", mn, d.name(pr), s != null ? s : "(" + d.name(d.call(pr, "getMethod")) + ")"); }
    }
  }
  d.p("fisica", cn, "modelos (" + n + ")", b);
  d.values("referencia", cn, pc.getReferenceValues());
  d.values("iniciales", cn, pc.getInitialConditions());
}
for (Object o : sim.get(ReferenceFrameManager.class).getObjects()) {
  String fn = d.name(o);
  d.p("marcos", fn, "tipo", o.getClass().getSimpleName());
  if (o instanceof RotatingReferenceFrame) {
    RotatingReferenceFrame f = (RotatingReferenceFrame) o;
    try { d.p("marcos", fn, "velocidad de giro", String.format(java.util.Locale.ROOT, "%.6g rpm (%.6g rad/s)", f.getRotationRate().getSIValue() * 60 / (2 * Math.PI), f.getRotationRate().getSIValue())); } catch (Throwable e) { }
    d.p("marcos", fn, "eje", d.vec(f.getAxisDirection()));
    d.p("marcos", fn, "origen", d.vec(f.getAxisOrigin()) + " m");
  }
}
// malla
for (MeshOperation op : sim.get(MeshOperationManager.class).getObjects()) {
  String on = op.getPresentationName();
  d.p("malla", on, "tipo", op.getClass().getSimpleName());
  Object parts = d.call(d.call(op, "getInputGeometryObjects"), "getObjects");
  if (parts instanceof Collection) { StringBuilder b = new StringBuilder(); for (Object o : (Collection) parts) b.append(b.length() > 0 ? ", " : "").append(d.name(o)); d.p("malla", on, "partes", b); }
  Object ms = d.call(op, "getMeshersCollection");
  if (ms == null) ms = d.call(op, "getMeshers");
  if (ms != null) d.p("malla", on, "malladores", ms instanceof Collection ? ms.toString() : d.name(ms));
  Object dv = d.call(op, "getDefaultValues");
  Object dvs = d.call(dv, "getObjects");
  if (dvs instanceof Collection) for (Object v : (Collection) dvs) {
    String s = d.q(v);
    if (s == null) for (String m : new String[] {"getNumLayers", "getStretching", "getRelativeSize", "getPercentage", "getAbsoluteSize", "getRelativeSizeScalar"}) {
      Object x = d.call(v, m); if (x != null) { s = (x instanceof Number) ? x.toString() : d.q(x); if (s != null) break; } }
    d.p("malla", on, d.name(v), s);
  }
  Object cc = d.call(d.call(op, "getCustomMeshControls"), "getObjects");
  if (cc instanceof Collection) for (Object c : (Collection) cc) {
    String cn = on + " · " + d.name(c);
    Object go = d.call(d.call(c, "getGeometryObjects"), "getObjects");
    if (go instanceof Collection) d.p("malla", cn, "superficies", ((Collection) go).size());
    d.values("malla", cn, d.call(c, "getCustomConditions"));
    Object cv = d.call(d.call(c, "getCustomValues"), "getObjects");
    if (cv instanceof Collection) for (Object v : (Collection) cv) { String s = d.q(v); if (s == null) { Object rs = d.call(v, "getRelativeSize"); Object pc = d.call(rs, "getPercentage"); if (pc != null) s = pc + " % del base"; } d.p("malla", cn, d.name(v), s); }
  }
}
try {
  FvRepresentation fv = (FvRepresentation) sim.getRepresentationManager().getObject("Volume Mesh");
  d.p("malla", "Volume Mesh", "celdas", fv.getCellCount());
  d.some("malla", "Volume Mesh", fv, "getInteriorFaceCount", "getBoundaryFaceCount", "getVertexCount", "getFaceCount");
} catch (Throwable e) { d.p("malla", "Volume Mesh", "celdas", "sin malla de volumen"); }
// solver y criterios de parada
for (Solver s : sim.getSolverManager().getObjects()) {
  String sn = s.getPresentationName();
  d.p("solver", sn, "tipo", s.getClass().getSimpleName());
  d.some("solver", sn, s, "getUrf", "isFrozen");
  for (String sub : new String[] {"getVelocitySolver", "getPressureSolver", "getEnergySolver"}) {
    Object x = d.call(s, sub); if (x != null) { Object u = d.call(x, "getUrf"); if (u != null) d.p("solver", sn, sub.substring(3) + " · URF", u); }
  }
}
for (Object c : sim.getSolverStoppingCriterionManager().getObjects()) {
  String cn = d.name(c);
  d.p("parada", cn, "tipo", c.getClass().getSimpleName());
  d.some("parada", cn, c, "getIsUsed", "getMaximumNumberSteps", "getInnerIterationCriterion", "getLogicalOption");
  Object ct = d.call(c, "getCriterionType");
  if (ct != null) d.p("parada", cn, "criterio", d.name(ct) + " " + String.valueOf(d.q(d.call(ct, "getMinimum")) == null ? "" : d.q(d.call(ct, "getMinimum"))));
}
// reportes, monitores, funciones de campo, partes derivadas, escenas y gráficos
for (Report rep : sim.getReportManager().getObjects()) {
  String rn = rep.getPresentationName();
  d.p("reportes", rn, "tipo", rep.getClass().getSimpleName());
  try { d.p("reportes", rn, "valor", rep.getReportMonitorValue() + " " + d.name(d.call(rep, "getUnits"))); } catch (Throwable e) { }
  d.some("reportes", rn, rep, "getDefinition");
  Object ff = d.call(rep, "getFieldFunction"); if (ff != null) d.p("reportes", rn, "campo", d.name(ff));
  Object prts = d.call(d.call(rep, "getParts"), "getObjects");
  if (prts instanceof Collection) { StringBuilder b = new StringBuilder(); for (Object o : (Collection) prts) b.append(b.length() > 0 ? ", " : "").append(d.name(o)); d.p("reportes", rn, "partes", b); }
  for (String m : new String[] {"getDirection", "getOrigin"}) { Object v = d.call(rep, m); if (v != null) d.p("reportes", rn, m.substring(3), d.vec(v)); }
}
for (Monitor mo : sim.getMonitorManager().getObjects()) d.p("monitores", mo.getPresentationName(), "tipo", mo.getClass().getSimpleName());
for (Object f : sim.getFieldFunctionManager().getObjects())
  if (f instanceof UserFieldFunction) d.p("funciones", d.name(f), "definición", ((UserFieldFunction) f).getDefinition());
for (Object p : sim.getPartManager().getObjects()) {  // planos, streamlines, isosuperficies…: qué son y dónde están
  String pn = d.name(p);
  d.p("derivadas", pn, "tipo", p.getClass().getSimpleName());
  d.some("derivadas", pn, p, "getOrigin", "getOrientation", "getNormal", "getCoordinateSystem", "getSeedPart", "getMaximumPropagation", "getDirection");
  Object inp = d.call(d.call(p, "getInputParts"), "getObjects");
  if (inp instanceof Collection) { StringBuilder b = new StringBuilder(); for (Object o : (Collection) inp) b.append(b.length() > 0 ? ", " : "").append(d.name(o)); d.p("derivadas", pn, "entrada", b); }
  Object fv = d.call(d.call(p, "getFieldFunction"), "getPresentationName");
  if (fv != null) d.p("derivadas", pn, "campo", fv);
  Object vf = d.call(p, "getVectorField");
  if (vf != null) d.p("derivadas", pn, "campo vectorial", d.name(vf));
}
for (Scene sc : sim.getSceneManager().getScenes()) {  // cada escena con sus displayers: tipo, partes y campo que pinta
  String sn = sc.getPresentationName();
  d.p("escenas", sn, "displayers", sc.getDisplayerManager().getObjects().size());
  for (Object dp : sc.getDisplayerManager().getObjects()) {
    String dn = sn + " · " + d.name(dp);
    d.p("escenas", dn, "tipo", dp.getClass().getSimpleName());
    Object parts = d.call(d.call(dp, "getInputParts"), "getObjects");
    if (parts instanceof Collection) { StringBuilder b = new StringBuilder(); int n = 0;
      for (Object o : (Collection) parts) { if (n++ < 12) b.append(b.length() > 0 ? ", " : "").append(d.name(o)); }
      d.p("escenas", dn, "partes (" + n + ")", b); }
    for (String q : new String[] {"getScalarDisplayQuantity", "getVectorDisplayQuantity"}) {
      Object dq = d.call(dp, q); Object ff = d.call(dq, "getFieldFunction");
      if (ff != null) d.p("escenas", dn, q.contains("Scalar") ? "campo escalar" : "campo vectorial", d.name(ff));
      Object rng = d.call(dq, "getRange"); if (rng != null) d.p("escenas", dn, "rango", d.vec(rng) != null ? d.vec(rng) : String.valueOf(rng));
    }
    d.some("escenas", dn, dp, "getOpacity", "getRepresentation", "getDisplayMode", "getVisibilityOverrideMode");
  }
}
for (Object pl : sim.getPlotManager().getObjects()) d.p("graficos", d.name(pl), "tipo", pl.getClass().getSimpleName());

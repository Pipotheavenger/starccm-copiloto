// QUE: exportar los modelos 3D-CAD de un .sim a Parasolid (.x_t) y STEP, un archivo por modelo con todos sus cuerpos
// VERSION: 19.06.009-R8 · FECHA: 2026-10-24 · ESTADO: verificado con el solver (001-Generico HF5B_Rev2: 3 cuerpos, 12,6 s)
// OUT: carpeta destino. getObjects() da CadModelBase: castear a CadModel. exportModelToStep añade «.step» al nombre que
// se le pase (con «X.stp» deja «X.stp.step»): pasar el nombre SIN extensión. imports: star.cadmodeler.*
String out = resolvePath("OUT");
new java.io.File(out).mkdirs();
for (CadModelBase cmb : sim.get(SolidModelManager.class).getObjects()) {
  if (!(cmb instanceof CadModel)) { sim.println("SKIP\t" + cmb.getPresentationName()); continue; }
  CadModel cm = (CadModel) cmb;
  String n = cm.getPresentationName().replaceAll("[^A-Za-z0-9_]+", "_");
  StringBuilder bn = new StringBuilder();
  for (Body b : cm.getBodyManager().getBodies()) bn.append(b.getPresentationName()).append(";");
  sim.println("MODEL\t" + cm.getPresentationName() + "\t" + bn);
  try { cm.exportModel(out + "/" + n + ".x_t"); sim.println("XT_OK\t" + n); } catch (Exception e) { sim.println("XT_FAIL\t" + n + "\t" + e); }
  try { cm.exportModelToStep(out + "/" + n, true, true, 0); sim.println("STEP_OK\t" + n); } catch (Exception e) { sim.println("STEP_FAIL\t" + n + "\t" + e); }
}

// QUE: escena de malla de superficie exportable a PNG en batch: PartDisplayer con las FRONTERAS como entrada, representación Volume Mesh y resetCamera(true)
// VERSION: 19.06.009-R8
// FECHA: 2026-10-23
// Verificado: con la región como entrada (representación "Latest Surface/Volume") o sin resetCamera(true) el PNG sale en blanco.
// createScene("Malla") deja la escena como "Malla 1"; export_scene acepta el prefijo.
Region r = sim.getRegionManager().getRegions().iterator().next();
Scene sc = sim.getSceneManager().createScene("Malla");
sc.initializeAndWait();
PartDisplayer pd = sc.getDisplayerManager().createPartDisplayer("Malla", -1, 1);
pd.initialize();
pd.setRepresentation(sim.getRepresentationManager().getDefaultFvRepresentation());
pd.getInputParts().setObjects(new ArrayList<Boundary>(r.getBoundaryManager().getBoundaries()));
pd.setMesh(true); pd.setSurface(true);
sc.resetCamera(true);
// ESTADO: no verificado -> orientar la vista tras resetCamera: sc.setViewOrientation(new DoubleVector(new double[] {-1,-1,-1}), new DoubleVector(new double[] {0,1,0}));
sim.println("SCENE " + sc.getPresentationName() + " rep " + pd.getRepresentation().getPresentationName());

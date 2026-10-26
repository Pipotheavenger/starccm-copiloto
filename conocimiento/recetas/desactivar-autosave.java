// QUE: apaga el autoguardado (en 2410 "batch" viene activo y deja <sim>@meshed.sim y <sim>@NNNNN.sim junto al .sim). server_start ya lo hace.
// VERSION: 19.06.009-R8
// FECHA: 2026-10-23
AutoSave as = sim.getSimulationIterator().getAutoSave();
sim.println("BEFORE enabled=" + as.getEnabled() + " mesh=" + as.getAutoSaveMesh() + " batch=" + as.getAutoSaveBatch()
  + " trigger=" + as.getTriggerOption().getSelected() + " freq=" + as.getTriggerFrequency() + " max=" + as.getMaxAutosavedFiles());
as.setEnabled(false); as.setAutoSaveMesh(false); as.setAutoSaveBatch(false);
sim.println("AFTER enabled=" + as.getEnabled() + " mesh=" + as.getAutoSaveMesh() + " batch=" + as.getAutoSaveBatch());

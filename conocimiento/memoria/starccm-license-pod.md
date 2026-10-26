---
name: starccm-license-pod
description: "La licencia STAR-CCM+ es PoD (ccmppower) contra el servidor de Siemens; Sebastián cree que es por periodo, no por horas; caduca ~2027-03"
metadata:
  node_type: memory
  type: project
  originSessionId: 1c9a8f44-bddb-4474-b693-792a45b36147
  modified: 2026-10-24T11:54:07.395Z
---

La clave de STAR-CCM+ es Power-on-Demand (feature `ccmppower`) validada contra `1999@flex.cd-adapco.com`
(servidor de Siemens, no de la universidad); el log del 2026-10-24 dice «expires in 135 days» y «1 copy of
ccmppower checked out». El 2026-10-24 Sebastián dijo que cree que es **por periodo** (uso ilimitado hasta la
fecha), no por horas; no confirmado con quien gestiona la clave.

**Why:** Decide cuánto importa dejar solvers vivos: si es por periodo, no cuesta dinero, pero una sola copia
simultánea bloquea arrancar otro solver (PC o servidor).
**How to apply:** Seguir apagando solvers al terminar (por la copia única, no por coste); ofrecer subir
`STARCCM_IDLE_MIN` (p. ej. 120) si el apagado a 30 min estorba. Ver [[starccm-agent-state-2026-10-23]].

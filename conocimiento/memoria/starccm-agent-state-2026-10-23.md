---
name: starccm-agent-state-2026-10-23
description: "Estado del agente STAR-CCM+ (carpeta starccm/) al 2026-10-23: instalación 2410 en el PC Windows, MCP con selftest, qué falta (clave PoD) y trampas del entorno"
metadata:
  node_type: memory
  type: project
  originSessionId: 2101dcbb-1f4a-4bf1-90f9-bb3c8301e6b8
  modified: 2026-10-23T19:22:16.096Z
---

Sebastián decidió el 2026-10-23 que el agente de STAR-CCM+ corre en SU PC Windows (no en la MV
Linux del brief original) y en carpeta propia `starccm/`, sin conexión con `cfturbo/`.
- STAR-CCM+ 2410.0001 (19.06.009-R8, doble) en `C:\Program Files\Siemens\19.06.009-R8\STAR-CCM+19.06.009-R8`;
  lanzador `star\bin\starccm+.bat`; javadoc en `doc\client\html`; javac en `..\jdk\win64\jdk21.0.2`.
- Licencia PoD contra 1999@flex.cd-adapco.com; la clave (22 caracteres, feature ccmppower, caduca
  ~2027-03) está en `starccm/.env` (`STARCCM_PODKEY`, gitignored), puesta el 2026-10-23.
- Hecho y VERIFICADO contra el solver real el 2026-10-23: `starccm/mcp_server.py` (11 tools),
  inyección `-host … -batch … -noexit` (8–10 s por macro, arranque 4,5 s), caso cilindro completo
  (geometría, malla poliédrica, SST, 100 iteraciones, residuales 1e-6, balance de masa 0 %, PNG),
  9 recetas en `starccm/recipes/`, lecciones en `starccm/CLAUDE.md`.
- Primera bomba real hecha el mismo día: `starccm/cases/cursocfturbo/` (CAD de CFturbo del
  Escritorio\Jobs\CFTurbo, D2 270 mm, 250 m³/h, 2980 rpm): 370 k celdas, 400 iteraciones,
  H 78,8 m, M 192,5 N·m, η 89,4 %. Guion `pump.py` y recetas `bomba-*`. Lección clave: una
  operación de malla por parte (juntas fallan por caras coincidentes).
- Interfaz web de starccm/ (sesión local_71777629, 2026-10-23): visión de Sebastián = imitar el
  viewer de STAR-CCM+ pero "IA driven" alrededor de UNA simulación: chat embebido, árbol, escenas
  CFD (planos, vectores), malla y 3D, progreso y pasos como terminal, parámetros editables desde la
  página, salto al visualizador de STAR-CCM+. Dijo dos veces: primero una sola simulación perfecta,
  luego varias (nada de barridos/curvas aún). README raíz del repo escrito ese día con hoja de ruta.
- UI construida el 2026-10-23 (commits 3c81dd7 → 43b7739 en main): `starccm/ui/` FastAPI + three.js,
  tema claro, puerto 8767 (preview `starccm-ui`). Sebastián rechazó la primera versión de pestañas
  («no me gusta nada la UI, todo animado tipo 3D, como el cliente de ccm pero IA driven, tema claro»):
  la actual imita el cliente de STAR-CCM+ (árbol / visor 3D con campos / dock Salida-Gráficos-Resumen /
  chat). Datos del visor: receta `exportar-superficies-ensight.java` (ImportManager.export .case,
  dataAtVerts) + `ui/surfaces.py`. Endpoints verificados con el solver real por la sesión hermana;
  `POST /api/surface` también (12:21, 8 fronteras del impulsor, 107 k triángulos, 4 campos): el visor
  muestra la bomba real desde `cases/cursocfturbo/surfaces/superficies` (gitignored). Solo `open_gui`
  queda sin probar. Los `confirm()` del navegador no aparecen en el panel embebido de la app.
  Trampa EnSight de STAR-CCM+: variables con asteriscos en el .case y binarias sin cabecera.
- Recorrido completo del caso desde la UI con el solver real (14:10–14:21): superficies de todas las
  fronteras (187 k triángulos), escenas plano_vectores y malla (recetas verificadas), 180 iteraciones
  (580 en total, H 78,77 m, η 0,8935) con Salida en vivo (`_inject` escribe el .out en vivo), chat con
  inspect, apagado desde el botón. Para observar la UI mientras itera: `python -c "import mcp_server…"`
  en segundo plano con el sandbox desactivado (las tools corren en serie). Sebastián quiere la UI
  «como el cliente de ccm pero IA driven», tema claro, una simulación primero.

**Why:** Decide dónde y cómo se prueba el solver; evita volver a buscar la clave o la MV.
**How to apply:** El sandbox del tool Bash bloquea las DLL de STAR-CCM+ (error «Control de
aplicaciones»): lanzar el solver desde el MCP o con Bash sin sandbox. Los heredocs del tool Bash
pierden barras invertidas: escribir archivos con Write. Ver [[sebastian-turbomachinery-agent-goal]]
y [[cfturbo-agent-state-2026-10-23]].

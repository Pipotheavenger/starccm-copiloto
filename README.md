# starccm-copiloto

Agente que opera Simcenter STAR-CCM+ (2410, solo en este PC, 4 procesos) para turbomáquinas. No imita STAR-CCM+ en
una web: trabajas con la **GUI real**, conectada al mismo servidor que el agente, y le hablas desde un **panel
flotante de chat**. Diseño: [ARQUITECTURA.md](ARQUITECTURA.md). Reglas del agente: [CLAUDE.md](CLAUDE.md).
Lo heredado (recetas Java, lecciones, casos): [conocimiento/INDICE.md](conocimiento/INDICE.md).

## Arrancar

1. `.env` (copiar `.env.example`): clave Power-on-Demand en `STARCCM_PODKEY` (no va a git).
2. `.venv`: `python -m venv .venv` y `.venv\Scripts\pip install mcp` (ya hecho en este PC).
3. `.mcp.json`: copiar `.mcp.json.example` y reiniciar Claude Code (el servidor `starccm` aparece con sus tools).
4. Panel: `powershell panel\abrir_panel.ps1` (o `.venv\Scripts\python panel\chat_panel.py`).
5. GUI: botón «Abrir GUI» del panel o la tool `gui_attach` (`starccm+ -host localhost:47827`, sin licencia extra).
6. Una bomba de CFturbo: «corre la bomba de `<carpeta con .cfturbo-ccm y STEP>`» → `pump_case` (con `params={"iterar": true, "post": true}` llega hasta resultados y PNG en una macro).

El solver solo arranca fuera del sandbox de Claude Code. Un solo solver vivo a la vez (licencia de una copia).

## Probar (sin licencia ni solver)

| comando | qué comprueba |
|---|---|
| `.venv\Scripts\python mcp_server.py --selftest` | las tools contra `tests/fake_starccm.py` |
| `.venv\Scripts\python panel\chat_panel.py --selftest` | el panel contra `tests/fake_claude.py`, sin pantalla |
| `.venv\Scripts\python pipelines\bomba.py --selftest` | lee un `.cfturbo-ccm` real, compila con el `javac` de STAR-CCM+ el Java generado (macro con iterar y post, recetas generadas, bloques de `plantillas_casos.md`) y prueba el juez de estabilidad con datos de BombaRapida |

## Carpetas

| carpeta | contenido |
|---|---|
| `pipelines/` | `bomba.py`: genera UNA macro de bomba (importar, regiones, interfaces, malla, física, MRF, fronteras, reportes, Euler) y, opcional por `params`, iterar hasta que H y M se estabilicen (`iterar`, máx. 1000) y el post-proceso (`post`: planos, 3 PNG en `casos/<caso>/images`, y+, resultados) |
| `conocimiento/` | recetas Java, casos de referencia, memoria y las reglas del agente anterior; `plantillas_casos.md`: recetas de caso para `run_steps` (cilindro, perfil NACA desde STL, misma bomba con otro caudal) |
| `casos/<caso>/` | CAD, reportes, imágenes y `RESUMEN.md` de cada caso (ignorado por git) |
| `sims/`, `macros/` | `.sim` de trabajo y macros inyectadas con su salida (ignorados por git) |

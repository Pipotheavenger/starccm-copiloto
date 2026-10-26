# Índice del conocimiento

Todo es herencia de `starccm-agent/starccm` (2026-10-23 a 26) salvo lo marcado «nuevo». Versión de las recetas: STAR-CCM+
19.06.009-R8 (2410). «Verificado» = corrió contra el solver real; «compila» = solo `javac`; «no verificado» = de memoria.

## Archivos

| archivo | qué es |
|---|---|
| `CLAUDE_agente_anterior.md` | reglas y lecciones completas del agente anterior, incluidas las del remoto retirado y la UI web (`ui/`) |
| `README_agente_anterior.md` | README del agente anterior (interfaz web por zonas, endpoints) |
| `mcp_server_anterior.py` | MCP anterior: base de `mcp_server.py` (`_wrap`, `_precompile`, `_javac`, `iterate`, `export_scene`) |
| `memoria/*.md` | memoria de Claude sobre Sebastián, la licencia PoD y el estado del agente al 2026-10-23 |
| `recetas/README.md` | formato de receta: cabecera QUE/VERSION/FECHA(/ESTADO); el cuerpo es lo de dentro de `execute()` |
| `casos/<caso>/RESUMEN.md` | resumen de cada caso; guiones `run.py` (BombaRapida) y `pump.py` (cursocfturbo) |
| `../pipelines/bomba.py` | nuevo: genera una macro de bomba desde el `.cfturbo-ccm` + STEP; opcional `iterar` (H y M estables, máx. 1000) y `post` (planos, PNG, y+, resultados); `post_body`, `iterate_body`, `recetas()` |
| `lecciones.md` | nuevo: detalle operativo y todas las lecciones vigentes (velocidad, inyección, flujo, y+, CAD genérico, estimar celdas, por tema); `../CLAUDE.md` es su resumen |
| `plantillas_casos.md` | nuevo: recetas de caso listas para `run_steps` con pasos, parámetros y tiempos: tubería/cilindro (verificada con pasos fijos), perfil NACA desde STL con 4 superficies (no verificada), misma bomba con otro `Flujo` |

## Recetas (35)

### Bomba por pasos (de cursocfturbo; el pipeline las reemplaza con nombres tomados del ccm)

| receta | qué hace | estado |
|---|---|---|
| `bomba-importar` | importa los 3 STEP como partes con caras nombradas (ruta y nombres fijos de cursocfturbo) | verificado |
| `bomba-regiones` | región por parte, frontera por superficie, renombra por sufijo tras el último `-` | verificado |
| `bomba-interfaces` | interfaces estator→rotor y rotor→voluta | verificado |
| `bomba-fisica` | continuo 3D estacionario, líquido, segregado, SST all-y+ | verificado |
| `bomba-propiedades-agua` | ρ y μ del ccm en el modelo de líquido | verificado |
| `bomba-mrf-rotor` | marco rotatorio (−2980 rpm, +Z) asignado al rotor | verificado |
| `bomba-fronteras` | caudal en la entrada + presión en la salida (esquema viejo; usar `fronteras-presion-total-caudal-salida`) | verificado |
| `bomba-reportes` | pt_in, pt_out, Q, M, H, P_hid, P_eje, eta y monitores (divisiones protegidas) | verificado con solver el 2026-10-26 (BombaRapida_e2e) |
| `bomba-malla` | una operación de malla por parte, 5 prismas, base 5 mm, álabes al 50 % | verificado |
| `bomba-escenas` | presión en álabes, velocidad en plano_medio, y+ | verificado |

### Física, fronteras y malla

| receta | qué hace | estado |
|---|---|---|
| `fisica-agua-segregado-sst` | continuo genérico (cilindro) | verificado |
| `fisica-modelos-turbo` | remediación de celdas, gravedad, transición Gamma (de las plantillas NQ25Q159H91) | verificado en copia de cursocfturbo |
| `fronteras-presion-total-caudal-salida` | presión total en la entrada y caudal en la salida con parámetro `Flujo` | verificado en copia de cursocfturbo; BombaRapida |
| `fronteras-velocity-inlet-pressure-outlet` | velocity inlet 1 m/s + pressure outlet (cilindro) | verificado |
| `identificar-fronteras-por-posicion` | renombra inlet/outlet/wall de un tubo en Z por Min/Max de Position | verificado |
| `crear-region-desde-parte` | región por parte sin interfaces | verificado |
| `mallar-poliedrica-prismas` | operación de malla genérica (cilindro) | verificado |
| `mallar-trimmer-una-operacion` | trimmer en una operación, sin prismas en interfaces (plantillas) | compila; NO probada con solver |
| `separar-caras-por-angulo` | «Split by angle» para entrada/salida/paredes elegibles | compila; por verificar |
| `crear-cilindro` | cilindro r 0,05 m, L 0,5 m | verificado |

### Reportes e iteración

| receta | qué hace | estado |
|---|---|---|
| `reportes-euler-triangulos` | U, Vu, Vr, UVu, H_Euler, eta_Euler, factor de deslizamiento, TotalHead, gráficos; fronteras del rotor por nombre; validación final con AVISO | nuevo: **verificado con solver el 2026-10-26** (BombaRapida_e2e: H_Euler 18,75 m, eta_Euler 0,892; divisiones protegidas); la versión previa fallaba en BombaPruebaNR |
| `iterar-y-leer-residuales` | N iteraciones, exporta `Residuals` a CSV y lee reportes (base de `iterate`) | verificado |
| `iterar-hasta-estable` | bloques de 50 hasta que los reportes dados varían < 0,5 % en 100 it (máx. `max_iter`); residual y meseta solo se reportan; para `run_steps` (PARAMS: case_dir, max_iter, reportes) | nuevo: compila; juez probado con datos de BombaRapida; la misma lógica (`iterar` del pipeline) verificada con solver el 2026-10-26 (250 it, parada estable) |
| `post-bomba` | planos auto, escenas presion_alabes / velocidad_relativa / yplus a PNG, y+ en álabes, RESULT, balance de masa y validación de Euler en una macro (PARAMS: case_dir) | nuevo: verificado con solver el 2026-10-26 como parte de `pump_case` (`iterar`/`post` de `pipelines/bomba.py`, BombaRapida_e2e); la receta suelta para `run_steps` compila |
| `informe-detalle` | vuelca el detalle del .sim para el informe PDF (reflexión + try/catch) | compila; verificado con solver local |
| `print-tree` | regiones, fronteras, continuos y reportes (base de `inspect`) | no verificado |
| `desactivar-autosave` | apaga el autoguardado batch | verificado |

### Escenas y exportación

| receta | qué hace | estado |
|---|---|---|
| `escena-malla`, `escena-malla-rotor` | escena de malla de superficie, PNG en batch | verificado |
| `escena-plano-vectores` | plano a media salida del rotor con |velocidad| y vectores | verificado |
| `escenas-fluido` | TotalHead con líneas de corriente y vectores relativos por plano | verificado en copia de cursocfturbo |
| `exportar-superficies-ensight` | fronteras con campos a EnSight Gold (PATTERN, FUNCS, OUT; planos «auto») | verificado |
| `exportar-geometria-stl` | STL por parte antes de mallar (`MeshPart.exportStlPart`) | verificado (BombaP100V1) |
| `exportar-cad-parasolid-step` | exporta modelos 3D-CAD de un .sim a `.x_t` y STEP | verificado |
| `activar-autoexport` | exportación EnSight cada N iteraciones | compila; verificado con solver local |

## Casos de referencia

| caso | máquina | malla | resultado | tiempo | dónde |
|---|---|---|---|---|---|
| `BombaRapida` | d2 137 mm, 50 m³/h, 2900 rpm, 4 STEP (con extensión) | 180 193 celdas (base 4,5 mm, 3 prismas) | H 16,76 m (1D 20,7), M 8,457 N·m, η 88,7 %, balance 0,0001 %, y+ 4,4–48,2 | importar a resultados 3 min 45 s (300 it, 137 s) | `casos/BombaRapida/RESUMEN.md`, `run.py` |
| `cursocfturbo` | D2 270 mm, 250 m³/h, 2980 rpm, 3 STEP | 370 324 celdas (base 5 mm, 5 prismas) | H 78,8 m (1D 84,4), M 192,5 N·m, η 89,4 %, convergido en 400 it; reflujo en la salida | 82 s/100 it | `casos/cursocfturbo/RESUMEN.md`, `pump.py` |
| `BombaRapida_e2e` | misma geometría que BombaRapida, prueba real del copiloto (2026-10-26) | modo rápido, 4 procesos | H 16,74 m, M 8,454 N·m, η 88,6 %, H_Euler 18,75 m, η_Euler 0,892, balance 0,0002 %, estable en 250 it, 3 PNG | **3 min 14 s** de server_start a server_stop (pump_case 142,8 s) | `casos/BombaRapida_e2e_174159/e2e.json` |
| `BombaPruebaNR` | 2500 rpm, 7 kg/s, 3 partes | 214 085 celdas (base 5 mm, 5 prismas) | H 14,02 m, M 4,807 N·m, η 76,5 %, 700 it, continuidad ~3e-2 (plateau MRF); H_Euler 13,94 mal leído | ~10 min total, 0,5 s/it con GUI | `casos/BombaPruebaNR/RESUMEN.md` |
| `cilindro` | r 50 mm, L 0,5 m, agua 1 m/s | 11 463 celdas | residuales 1e-6, balance 0 % | 100 it, 12 s/50 | `casos/cilindro/RESUMEN.md` |

Plantillas de Sebastián `NQ25Q159H91_Rev0/Rev2.sim` (Downloads): origen de presión total + caudal en la salida, Gamma, gravedad,
remediación, trimmer y Euler; resultados de la prueba en `CLAUDE_agente_anterior.md` § Casos de referencia.

## Dónde están las lecciones

| tema | dónde |
|---|---|
| críticas, una línea | `../CLAUDE.md` § Lecciones críticas |
| vigentes con detalle | `lecciones.md` |
| completas, con fechas | `CLAUDE_agente_anterior.md` § Lecciones (entorno, macros, geometría y malla, física, escenas, servidor, remoto retirado) |
| Euler mal leído (BombaPruebaNR) | `lecciones.md` § Reportes de Euler y el encabezado de `recetas/reportes-euler-triangulos.java` |
| interfaces que dejan pared | `lecciones.md` § Geometría, regiones y malla; `casos/BombaRapida/RESUMEN.md` § Interfaces |
| GUI conectada al servidor | `../ARQUITECTURA.md` (tabla verificada 2026-10-26) y `lecciones.md` § Cómo se inyecta |
| UI web antigua | `README_agente_anterior.md` y `CLAUDE_agente_anterior.md` § Interfaz web |

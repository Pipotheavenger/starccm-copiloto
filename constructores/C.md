# Constructor C: pipelines/bomba.py, receta de Euler, CLAUDE.md, README, INDICE

Entregado: `pipelines/bomba.py`, `conocimiento/recetas/reportes-euler-triangulos.java` (reescrita), `CLAUDE.md` (217 líneas),
`README.md`, `conocimiento/INDICE.md`. No toqué `mcp_server.py`, `panel/`, `tests/`; no arranqué el solver ni hice commits.

- `python pipelines/bomba.py --selftest`: 16 checks, TODO BIEN. Parsea el ccm real de BombaRapida_v1 (ṁ 13,864, −2900 rpm, 4 zonas, d2 139,1 mm medido en el STL) y compila con el javac de STAR-CCM+ los cuerpos `rapida`, `normal`, uno con gamma/gravedad/β2, `euler_check_body` y la receta suelta.
- API: `parse_ccm(path)`, `macro_body(case_dir, cad_dir, ccm, mode, params)`, `euler_check_body(params)`, `estimate_d2_mm`, `wrap/compile_java` (el MCP envuelve con sus propios imports). `python pipelines/bomba.py --emit <ccm> <cad_dir> [modo]` imprime el Java.
- Marcadores `PIPE importar|regiones|fisica|mrf|fronteras|reportes|malla|euler|escena|total <s>`; además `CELLS`, `MESH_SECONDS`, `BOUNDARY`, `PARED_CONEXION`, `AVISO`, `EULER_OK|EULER_MAL`.
- Partes y regiones se ubican sin adivinar nombres: parte por diferencia de conjuntos tras cada `importCadPart`; región por coincidencia de nombres de frontera con las del ccm.
- d2: `params["d2_mm"]` o se mide en los STL de CFturbo (`…Impeller…Outflow.stl`) junto al CAD; si no hay, ValueError. Da 139,1 mm (BombaRapida usó 137 nominal → base 4,64 mm en vez de 4,5).
- Euler: entrada/salida del rotor por NOMBRE (Inflow/Outflow no-pared, incluye «[int_…]»), promedio másico sobre el conjunto, radio solo valida, y al final AVISO si UVu2 = UVu1, U2 ≤ U1 o H_Euler ≤ H. La validación necesita solución (iteración ≥ 1): correr `euler_check_body()` tras iterar. El pipeline reutiliza el texto de la receta (una sola lógica).
- Sin verificar con solver: todo `bomba.py`, Euler nuevo, `PARED_CONEXION` (usa `SumReport` con la magnitud de `Area`; `AreaReport` no existe), crear 4 STEP seguidos y el ajuste de regiones por nombres de frontera. Supuesto: las zonas del ccm van en orden de flujo.
- ponytail: una sola zona rotatoria para el momento M; interfaces en cadena i→i+1.

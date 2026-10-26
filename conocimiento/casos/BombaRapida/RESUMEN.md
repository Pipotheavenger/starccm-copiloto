# Caso BombaRapida: configuración rápida, de importar a resultados en 3 min 45 s

Fecha: 2026-10-25 · STAR-CCM+ 2410 en este PC, 4 procesos · guion `run.py` · `sims/BombaRapida.sim` (sin guardar solución
convergida: ver abajo) · detalle en `reports/tiempos.json` y `reports/convergencia.csv`.

## Geometría y datos (`cad/BombaRapida_cfd.cfturbo-ccm`)

| dato | valor |
|---|---|
| CAD | 4 STEP de CFturbo en «Flow domain»: estator de entrada, impulsor, voluta, extensión de salida |
| Impulsor | d2 137 mm, b2 13,9 mm, 6 álabes, β2 ≈ 31°/22° (cubo/corona) |
| Punto | 50 m³/h (ṁ 13,864 kg/s), 2900 rpm (−2900 sobre +Z), agua 998,2 kg/m³ |
| Predicción 1D de CFturbo | H 20,7 m, η 86,6 % |

## Configuración rápida

| ítem | valor |
|---|---|
| Malla | una operación por parte: remallador + reparación + poliédrica + 3 prismas (1,3; 10 % del base), base 4,5 mm = d2/30, álabes al 50 % → **180 193 celdas** |
| Física | 3D, estacionario, líquido, segregado, densidad constante, k-ω SST all-y+, Cell Quality Remediation; sin Gamma ni gravedad |
| Rotor | MRF (frozen rotor) −2900 rpm |
| Fronteras | entrada: presión total 0 Pa; salida (extensión): caudal −13,864 kg/s con el parámetro `Flujo` |
| Interfaces | estator→rotor, rotor→voluta, voluta→extensión |
| Parada | ΔH y ΔM < 0,5 % en 100 iteraciones y continuidad < 1e-4, o 10 min |

## Tiempos

| fase | segundos |
|---|---|
| arrancar solver (licencia + MPI) | 18,7 |
| importar 4 STEP + regiones + interfaces + física + MRF + fronteras + reportes (una macro) | 14,4 |
| mallar (4 operaciones: 1,8 + 11,1 + 9,5 + 2,1 s de malla) | 31,6 |
| iterar 300 iteraciones (≈ 23 s cada 50) | 136,6 |
| post: y+, escena de presión, guardar | 24,0 |
| **TOTAL** | **225,2 (3 min 45 s)** |

Objetivo de 10 min cumplido con margen de 6 min. Precompilar las macros con `javac` (≈ 6 s) va antes del reloj.

## Resultados (300 iteraciones, convergido)

| magnitud | CFD | CFturbo 1D | diferencia |
|---|---|---|---|
| Altura H | **16,76 m** | 20,7 m | −19 % |
| Torque M | 8,457 N·m | | |
| Potencia eje | 2,57 kW | | |
| Eficiencia hidráulica | **88,7 %** | 86,6 % | +2,1 puntos |
| Balance de masa | 0,0001 % | | |
| Continuidad final | 5,5e-5 | | |
| y+ en álabes (mín / media / máx) | 4,4 / 23,7 / 48,2 | | |

| iter | H (m) | M (N·m) | η | continuidad |
|---|---|---|---|---|
| 50 | 15,81 | 8,467 | 0,834 | 3,4e-2 |
| 100 | 16,67 | 8,460 | 0,882 | 4,8e-3 |
| 200 | 16,75 | 8,458 | 0,887 | 1,2e-3 |
| 300 | 16,76 | 8,457 | 0,887 | 5,5e-5 |

H ya estaba a 0,5 % del valor final en la iteración 100 (≈ 2 min 10 s de reloj total).

## Interfaces

Export «Flow domain»: las caras de conexión que quedan como pared tras crear las interfaces tienen áreas residuales
de 0 a 7 mm² frente a 3 400–6 000 mm² de interfaz (≤ 0,16 %), sin efecto en el balance de masa. El modo «virtual»
anterior dejaba hasta un 62 % sin emparejar.

## Lectura de los resultados

- H sale 19 % por debajo de la predicción 1D. Con la potencia al eje medida, η 88,7 % es razonable, así que el déficit
  es de trabajo transferido (deslizamiento mayor que el que supone el 1D) más que de pérdidas. Para confirmarlo:
  reportes de Euler (`reportes-euler-triangulos.java`), que esta corrida no incluyó para ahorrar tiempo.
- La H incluye la extensión de salida (presión total en su salida), que el 1D no cuenta.
- Malla gruesa (180 k celdas, 3 prismas): la cifra sirve para comparar variantes rápido, no como valor final.

## Si se quiere ir más rápido o más fino

| cambio | efecto esperado |
|---|---|
| parar en la iteración 100–150 (H a 0,5 %) | total ≈ 2 min 30 s |
| solver ya arrancado (servidor vivo entre casos) | −19 s |
| base 3,5 mm y 5 prismas | ~350 k celdas, ≈ 2× tiempo por iteración, total ≈ 6 min |

El `.sim` se guardó en el post sin solución final: el guion falló al imprimir un carácter (Δ) en la consola cp1252
después de escribir `tiempos.json` y antes de apagar; el solver se apagó a mano sin guardar. Corregido en `run.py`.

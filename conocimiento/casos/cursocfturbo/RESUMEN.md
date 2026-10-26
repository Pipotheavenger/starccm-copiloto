# Caso cursocfturbo: bomba centrífuga de CFturbo

Fecha: 2026-10-23 · STAR-CCM+ 2410.0001 (19.06.009-R8) en este PC, 4 procesos · `sims/cursocfturbo.sim` (165 MB)

## Geometría y datos (de `cad/cursocfturbo.cfturbo-ccm`)

| dato | valor |
|---|---|
| CAD | 3 STEP de CFturbo: tubo de entrada (Stator_1), impulsor (Impeller_1, 6 álabes), voluta (Volute_1) |
| Diámetro de salida del impulsor D2 | 270 mm |
| Entrada | anillo r 22,9–70,6 mm en z = −95 mm; salida de la voluta: difusor |
| Caudal | ṁ = 69,486 kg/s (Q = 250,6 m³/h) |
| Velocidad | −2980 rpm sobre +Z (sentido de CFturbo) |
| Fluido | agua ρ = 998,2 kg/m³, μ = 9,98e-4 Pa·s |
| Predicción 1D de CFturbo | Δpt = 8,26 bar → H ≈ 84,4 m |

## Modelo

| ítem | configuración |
|---|---|
| Regiones | estator, rotor, voluta; interfaces internas in-place estator→rotor y rotor→voluta |
| Malla | una operación por parte: remallador + reparación automática + poliédrica + 5 prismas (1,3; 10 % del base), base 5 mm, álabes al 50 % → **370 324 celdas** |
| Física | 3D, estacionario, líquido, flujo segregado, densidad constante, k-ω SST, all-y+ |
| Rotor | marco de referencia rotatorio (MRF, frozen rotor) −2980 rpm; paredes giran con el marco |
| Fronteras | inlet = mass flow inlet 69,486 kg/s; outlet = pressure outlet 0 Pa; resto walls |
| Reportes | pt_in, pt_out (promedio másico de presión total), Q_in, Q_out, M (momento en álabes+hub+shroud sobre Z), H, P_hid, P_eje, eta |

## Resultados (400 iteraciones, 82 s cada 100)

| iter | H (m) | M (N·m) | η | Q_out (kg/s) | continuidad |
|---|---|---|---|---|---|
| 100 | 73,89 | 191,3 | 0,843 | 69,50 | 1,6e-4 |
| 200 | 78,21 | 192,4 | 0,888 | 69,51 | 5,3e-5 |
| 300 | 78,69 | 192,5 | 0,893 | 69,48 | 1,2e-5 |
| **400** | **78,77** | **192,5** | **0,894** | **69,486** | **3,3e-6** |

Criterio de parada cumplido: ΔH 0,10 % y ΔM 0,00 % en 100 iteraciones, continuidad < 1e-4.

| magnitud | valor |
|---|---|
| Altura H | 78,8 m (CFturbo 1D: 84,4 m, −6,7 %) |
| Torque M | 192,5 N·m |
| Potencia eje P = M·ω | 60,1 kW |
| Potencia hidráulica ρgQH | 53,7 kW |
| Eficiencia hidráulica | 89,4 % |
| Balance de masa | 0,000 % (Q_in = −69,486, Q_out = +69,486 kg/s) |
| y+ en álabes | 1,7 – 56,7 (all-y+) |

Escenas en `images/`: `presion_alabes.png`, `velocidad_plano.png` (z = 30 mm, 0,4–34 m/s), `yplus.png`,
`plano_vectores.png` (mismo plano con vectores de velocidad, receta `escena-plano-vectores.java`) y
`malla.png` (malla superficial del impulsor, receta `escena-malla-rotor.java`).
Historial en `reports/convergencia.csv`.

### Continuación del 2026-10-23 por la tarde (desde la interfaz `ui/`)

| iter | H (m) | M (N·m) | η | continuidad |
|---|---|---|---|---|
| 500 | 78,77 | 192,50 | 0,8935 | 7,3e-7 |
| 550 | 78,77 | 192,50 | 0,8935 | 8,2e-7 |
| 580 | 78,77 | 192,50 | 0,8935 | 8,1e-7 |

Sin cambio en H ni M (< 0,01 %): el caso está convergido. El solver avisa «Reversed flow on 55 faces
on voluta: outlet»: algo de reflujo en la salida del difusor; no afecta al balance de masa (Q_in = −Q_out).
Superficies con campo para el visor 3D: `surfaces/superficies/` (17 fronteras sin interfaces: impulsor,
voluta, tubo de entrada; 187 400 triángulos; presión, presión total, |velocidad|, y+; 10,7 s de export).

## Tiempos

| fase | segundos |
|---|---|
| importar 3 STEP | 74 (carga módulos) + 10 |
| regiones, interfaces, física, agua, MRF, fronteras, reportes | 10 cada uno |
| malla (3 operaciones) | ~60 en total |
| 400 iteraciones | 330 |
| escenas y PNG | 11 + 3 × 11 |

## Lecciones del caso

- Las 3 partes en UNA operación de malla fallan («Surface intersects self» en las caras
  coincidentes de las interfaces). Una operación por parte funciona a la primera.
- Esta vez `newRegionsFromParts` NO creó el continuo «Physics 1»: el paso de física busca uno
  existente y si no, lo crea.
- Frozen rotor con interfaces no conformes: balance de masa exacto sin ajuste alguno.

Pendiente: curva de 5 caudales (0,6–1,3 Q) reusando la malla; comparar el sentido de giro con el
de CFturbo si H se aleja de la predicción 1D más de lo esperado.

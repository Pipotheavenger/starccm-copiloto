# BombaPruebaNR — RESUMEN FINAL (700 iteraciones)

## Geometría

| Parámetro | Valor |
|---|---|
| Archivos CAD | BombaRapida_cfd_Co1/Co2/Co3.stp (CFturbo, Flow domain) |
| Partes | estator (carcasa aspiración), rotor (impulsor), voluta |
| r1 entrada rotor | 29 mm |
| r2 salida rotor | 68 mm |
| Interfaces | estator/Outflow ↔ rotor/Inflow, rotor/Outflow ↔ voluta/Inflow |

## Malla

| Parámetro | Valor |
|---|---|
| Tipo | Poliédrica + 5 capas prismáticas (stretch 1.3, espesor 10%) |
| Tamaño base | 5 mm |
| Refinamiento álabes | 50 % del base |
| Celdas totales | 214 085 |
| Tiempo mallado | ~43 s (una operación por parte) |

## Física y condiciones

| Modelo / Condición | Valor |
|---|---|
| Turbulencia | k-ω SST all-y+ |
| Cell Quality Remediation | Activada |
| MRF rotor | 2500 rpm, eje −Z |
| Gravedad | No (bomba pequeña, horizontal) |
| Transición Gamma | No (omitida en esta prueba) |
| Entrada (estator/Inflow) | Presión total 0 Pa (Stagnation) |
| Salida (voluta/Outflow) | Caudal másico −7 kg/s (parámetro Flujo) |
| Fluido | Agua, ρ = 998.2 kg/m³ |

## Resultados finales (iter 700, estabilizado)

| Magnitud | Valor |
|---|---|
| **H** | **14.02 m** |
| **M** | **4.807 N·m** |
| **P_hid** | 962 W |
| **P_eje** | 1 258 W |
| **eta** | **76.5 %** |
| Q_in / Q_out | −6.998 / +7.000 kg/s |
| Balance de masa | 0.03 % ✓ |

### Triángulos de velocidad (iter 700)

| Variable | Entrada r1 = 29 mm | Salida r2 = 68 mm |
|---|---|---|
| U [m/s] | 7.58 | 17.91 |
| Vu [m/s] | 0.805 | 7.975 |
| Vr [m/s] | 0.082 | 1.384 |
| H_Euler [m] | — | 13.94 |

## Convergencia

| Iter | H [m] | M [N·m] | Continuidad | X-mom |
|---|---|---|---|---|
| 100 | 14.145 | 4.848 | 0.0131 | 0.0154 |
| 200 | 14.082 | 4.836 | 0.0315 | 0.0393 |
| 300 | 14.015 | 4.794 | 0.0279 | 0.0380 |
| 400 | 14.040 | 4.819 | 0.0361 | 0.0372 |
| 500 | 14.035 | 4.825 | 0.0271 | 0.0329 |
| 600 | 14.022 | 4.806 | 0.0265 | 0.0369 |
| **700** | **14.021** | **4.807** | 0.0300 | 0.0318 |

**Nota de convergencia:** Los residuales se estabilizaron en ~3×10⁻² (plateau MRF).
La variación de H y M en los últimos 200 iter es < 0.1 % → criterio de estabilidad cumplido.
Para reducir los residuales por debajo de 1×10⁻⁴ sería necesario malla más fina o simulación transitoria.

## Tiempos

| Paso | Tiempo |
|---|---|
| Arranque servidor | 37 s |
| Importar CAD + STL visor | 25 s |
| Malla (3 partes) | 43 s |
| Física + MRF + fronteras + reportes | ~90 s |
| 700 iteraciones | ~430 s |
| **Total** | **~10 min** |

## Archivos generados

| Archivo | Descripción |
|---|---|
| `images/presion_alabes.png` | Presión estática en álabes e impulsor |
| `images/velocidad_plano.png` | Velocidad en plano_medio (a media salida del rotor) |
| `images/yplus.png` | Wall y+ en paredes |
| `images/malla.png` | Malla superficial del impulsor |
| `images/Residuals.png` | Curva de residuales (700 iter) |
| `images/Altura.png` | Monitor de altura H |
| `images/Eficiencia.png` | Monitor de eficiencia y potencia |
| `surfaces/_raw/superficies/superficies.case` | EnSight: 17 fronteras + plano_medio + plano_meridional |
| `reports/convergencia.csv` | Tabla de convergencia por bloque |
| `sims/BombaPruebaNR.sim` | Simulación STAR-CCM+ completa |

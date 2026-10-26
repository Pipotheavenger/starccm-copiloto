# Caso cilindro (prueba de fontanería)

Fecha: 2026-10-23 · STAR-CCM+ 2410.0001 (19.06.009-R8) en este PC · `sims/cilindro.sim`

Objetivo: comprobar de punta a punta que el MCP opera el solver real: geometría por macro, región,
malla, física, condiciones de contorno, iteraciones, lectura de residuales y reportes, escena a PNG.

| dato | valor |
|---|---|
| Geometría | cilindro simple, radio 0,05 m, longitud 0,5 m en Z, superficie partida por ángulo 89° |
| Región | 1 región, 3 fronteras renombradas por posición Z: inlet (z=0), outlet (z=0,5), wall |
| Malla | remallador + poliédrica + prismas, tamaño base 8 mm, 11 463 celdas, 48 s |
| Física | 3D, estacionario, agua (líquido), flujo segregado, densidad constante, k-ω SST, all-y+ |
| Fronteras | inlet = velocity inlet 1 m/s; outlet = pressure outlet; wall |
| Iteraciones | 100 (2 × `iterate(50)`), 12 s cada 50 |
| Residuales finales | continuidad 1,6e-6, momento 1e-7…5e-7, tke 4,3e-6, sdr 1,3e-7 |
| Caudal másico | Q_in = −7,7546 kg/s, Q_out = +7,7546 kg/s (balance 0,000 %) |
| Escena | `images/malla.png` (malla de superficie, vista por el eje Z) |

Tiempos del MCP: arranque 4,5 s; macro trivial 8–10 s; primera macro de CAD 74 s y de física 62 s
(carga de módulos); recetas en `recipes/`: crear-cilindro, crear-region-desde-parte,
mallar-poliedrica-prismas, identificar-fronteras-por-posicion, fisica-agua-segregado-sst,
fronteras-velocity-inlet-pressure-outlet, escena-malla, iterar-y-leer-residuales, desactivar-autosave.

Pendiente de limpiar en el flujo real: reusar el continuo «Physics 1» en vez de crear «agua».

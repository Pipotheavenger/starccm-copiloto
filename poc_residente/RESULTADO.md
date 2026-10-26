# Prueba de concepto del cliente residente — 2026-10-26, STAR-CCM+ 2410 real

Servidor vacío (np 4). Cliente batch lanzado UNA vez: `starccm+.bat -host localhost:47827 -batch Residente.java -noexit`.
`Residente.java` vigila `inbox/` cada 50 ms y ejecuta cada `.java` con `new StarScript(getActiveRootObject(), file).play()`
(compila en el propio cliente, sin otra JVM). La salida de `sim.println` de cada macro sale por el stdout del residente.

| macro | total desde dejar el archivo | dentro del solver (compilar + ejecutar) |
|---|---|---|
| Paso1 | 1056 ms | 796 ms |
| Paso2 | 871 ms | 712 ms |
| Paso3 | 761 ms | 610 ms |
| Paso4 | 746 ms | 622 ms |
| Paso5 | 721 ms | 593 ms |

Antes (un cliente batch por macro): 8,6 s por macro trivial. Mejora ≈ 10×. Con ~50 macros por caso: ≈ 7 min menos.
El residente arrancó en ~1 s con el servidor ya vivo. STOP: archivo `inbox/STOP`.

"""Prueba real de punta a punta con STAR-CCM+ (gasta licencia PoD: ~5 min). Lanzar FUERA del sandbox de Claude Code
(Terminal normal): las DLL de STAR-CCM+ no cargan dentro.

    .venv\\Scripts\\python tests\\real_e2e.py [carpeta_cad] [caso] [--cancel]

Arranca un servidor nuevo, corre pump_case (modo rápido, iterar hasta estable y post-proceso en la MISMA macro), apaga
guardando y escribe casos/<caso>/e2e.json con tiempos por fase y resultados. Mientras corre muestrea estado_dir()/progreso.json
(fase, iteración, H, M, falta_s) y deja las muestras en e2e.json (progreso_muestras) y la última en progreso_final.

--cancel (opcional, NO se lanza solo): en vez de dejar converger, cuando la iteración pasa de 100 llama a cancel() (archivo ABORT),
comprueba que pump_case vuelve con cancelled, que ABORT y cancel.json desaparecen, que el turno queda libre y que el solver sigue
sirviendo (iterate de 20 it), y apaga. Guarda el resultado de cancel() y los segundos que tardó en parar en e2e.json (clave "cancel").
Es la prueba que falta con el solver real: si el criterio «Stop File» no obedece, cancel() lo dice (metodo ABORT+stop())."""
import json, re, sys, threading, time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import mcp_server as m  # noqa: E402

if any(a in ("-h", "--help") for a in sys.argv[1:]):
    print(__doc__); sys.exit(0)
CANCEL = "--cancel" in sys.argv
sys.argv = [a for a in sys.argv if a != "--cancel"]
cad = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\sebas\OneDrive\Documentos\cfturbo-agent\versions\BombaRapida_v1"
caso = sys.argv[2] if len(sys.argv) > 2 else "BombaRapida_e2e_" + time.strftime("%H%M%S")  # caso nuevo: server_start reabre un .sim del mismo nombre
# Validar ANTES de arrancar el solver (cada arranque reserva la licencia PoD): visto el 2026-10-26, «--help» se tomó
# como carpeta de CAD, arrancó el solver 53 s y luego falló.
_cad = Path(cad)
if not _cad.is_dir() or not list(_cad.glob("*.cfturbo-ccm")) or not list(_cad.glob("*.stp")):
    sys.exit(f"carpeta de CAD inválida (faltan .cfturbo-ccm o .stp): {cad} — no arranco el solver")
out = REPO / "casos" / caso
out.mkdir(parents=True, exist_ok=True)
t = {}
T0 = time.time()

def fase(nombre, fn, *a, **k):
    s = time.time()
    r = fn(*a, **k)
    t[nombre] = round(time.time() - s, 1)
    print(f"[{t[nombre]:7.1f} s] {nombre}: ok={r.get('ok', '?') if isinstance(r, dict) else '?'} via={r.get('via') if isinstance(r, dict) else ''}", flush=True)
    return r

muestras, cancel_info = [], None


def muestrear(stop):
    """Una foto de progreso.json cada ~5 s mientras corre pump_case."""
    while not stop.is_set():
        pg = m.read_progreso()
        if pg:
            muestras.append({k: pg.get(k) for k in ("fase", "iteracion", "H", "M", "continuidad", "transcurrido_s", "falta_s", "falta_fuente", "ritmo_it_s")})
        stop.wait(5)


r_start = fase("server_start", m.server_start, "", caso)
stop_s = threading.Event()
threading.Thread(target=muestrear, args=(stop_s,), daemon=True).start()
if CANCEL:
    box = {}
    th = threading.Thread(target=lambda: box.update(r=m.pump_case(caso, cad, "rapida", "", {"iterar": True, "post": True})), daemon=True)
    t_p = time.time()
    th.start()
    while th.is_alive() and not ((m.read_progreso().get("iteracion") or 0) > 100 and m.read_progreso().get("fase") == "iterar"):
        time.sleep(1)
    t_c = time.time()
    rc = m.cancel(wait_s=60) if th.is_alive() else {"error": "pump_case terminó antes de la iteración 100"}
    th.join(timeout=300)
    t["pump_case"] = round(time.time() - t_p, 1)
    r_pump = box.get("r", {})
    it20 = m.iterate(n=20, until="n")  # el solver sigue sirviendo tras el cancel
    cancel_info = {"cancel": rc, "segundos_en_parar": round(time.time() - t_c, 1), "pump_cancelled": r_pump.get("cancelled"),
                   "abort_borrado": not m.abort_file().exists(), "cancel_json_borrado": not m.cancel_file().exists(),
                   "turno_libre": m.lock_holder() is None, "iterate_20_ok": bool(it20.get("ok")) and it20.get("iterations_run") == 20}
    print(json.dumps(cancel_info, indent=1, ensure_ascii=False), flush=True)
else:
    r_pump = fase("pump_case", m.pump_case, caso, cad, "rapida", "", {"iterar": True, "post": True})
stop_s.set()
try:  # estado que ve el panel (lo escribe el vigilante cada ~30 s) justo antes de apagar: claves REALES de .server.json
    estado = json.loads((m.estado_dir() / ".server.json").read_text(encoding="utf-8"))
    estado = {k: estado.get(k) for k in ("pid", "pid_real", "vigilante", "residente", "residente_real", "gui", "idle_min", "idle_checked", "idle_warning")}
    ri = m._res_info()
    estado["residente_json"] = {k: ri.get(k) for k in ("pid", "server_pid")}
except Exception as e:
    estado = {"error": str(e)}
r_stop = fase("server_stop", m.server_stop, True)
t["total"] = round(time.time() - T0, 1)

stdout = (r_pump or {}).get("stdout", "") if isinstance(r_pump, dict) else ""
pipe = {mm.group(1): float(mm.group(2)) for mm in re.finditer(r"PIPE\s+(\S+)\s+([0-9.]+)", stdout)}
res = dict(re.findall(r"RESULT\s+(\S+)\s+(\S+)", stdout))
avisos = [l for l in stdout.splitlines() if "AVISO" in l or "EULER_" in l or "BALANCE" in l][:20]
info = {"estado_panel": estado, "progreso_final": m.read_progreso(), "progreso_muestras": muestras[-60:], "cancel": cancel_info,
        "parsed": (r_pump or {}).get("parsed") if isinstance(r_pump, dict) else None, "tiempos_s": t, "fases_pipeline_s": pipe, "resultados": res, "avisos": avisos,
        "pump_ok": r_pump.get("ok") if isinstance(r_pump, dict) else None,
        "pump_error": (r_pump or {}).get("exception") or (r_pump or {}).get("error") or ((r_pump or {}).get("trace") or "")[:800] or (stdout[:800] if not (r_pump or {}).get("ok") else None),
        "via": (r_pump or {}).get("via"), "stop": {k: r_stop.get(k) for k in ("stopped", "saved", "gui_warning", "timings_s")} if isinstance(r_stop, dict) else r_stop}
(out / "e2e.json").write_text(json.dumps(info, indent=1, ensure_ascii=False), encoding="utf-8")
(out / "e2e_stdout.txt").write_text(stdout, encoding="utf-8")
print(json.dumps(info, indent=1, ensure_ascii=False))

"""Prueba real de punta a punta con STAR-CCM+ (gasta licencia PoD: ~5 min). Lanzar FUERA del sandbox de Claude Code
(Terminal normal): las DLL de STAR-CCM+ no cargan dentro.

    .venv\\Scripts\\python tests\\real_e2e.py [carpeta_cad] [caso]

Arranca un servidor nuevo, corre pump_case (modo rápido, iterar hasta estable y post-proceso en la MISMA macro), apaga
guardando y escribe casos/<caso>/e2e.json con tiempos por fase y resultados."""
import json, re, sys, time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import mcp_server as m  # noqa: E402

cad = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\sebas\OneDrive\Documentos\cfturbo-agent\versions\BombaRapida_v1"
caso = sys.argv[2] if len(sys.argv) > 2 else "BombaRapida_e2e_" + time.strftime("%H%M%S")  # caso nuevo: server_start reabre un .sim del mismo nombre
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

r_start = fase("server_start", m.server_start, "", caso)
r_pump = fase("pump_case", m.pump_case, caso, cad, "rapida", "", {"iterar": True, "post": True})
r_stop = fase("server_stop", m.server_stop, True)
t["total"] = round(time.time() - T0, 1)

stdout = (r_pump or {}).get("stdout", "") if isinstance(r_pump, dict) else ""
pipe = {mm.group(1): float(mm.group(2)) for mm in re.finditer(r"PIPE\s+(\S+)\s+([0-9.]+)", stdout)}
res = dict(re.findall(r"RESULT\s+(\S+)\s+(\S+)", stdout))
avisos = [l for l in stdout.splitlines() if "AVISO" in l or "EULER_" in l or "BALANCE" in l][:20]
info = {"tiempos_s": t, "fases_pipeline_s": pipe, "resultados": res, "avisos": avisos,
        "pump_ok": r_pump.get("ok") if isinstance(r_pump, dict) else None,
        "pump_error": (r_pump or {}).get("exception") or (r_pump or {}).get("error") or ((r_pump or {}).get("trace") or "")[:800] or (stdout[:800] if not (r_pump or {}).get("ok") else None),
        "via": (r_pump or {}).get("via"), "stop": {k: r_stop.get(k) for k in ("stopped", "saved", "gui_warning")} if isinstance(r_stop, dict) else r_stop}
(out / "e2e.json").write_text(json.dumps(info, indent=1, ensure_ascii=False), encoding="utf-8")
(out / "e2e_stdout.txt").write_text(stdout, encoding="utf-8")
print(json.dumps(info, indent=1, ensure_ascii=False))

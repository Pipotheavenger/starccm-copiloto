"""Prueba real del camino del PANEL (gasta licencia PoD ~3 min y turnos de la CLI de Claude). Lanzar FUERA del sandbox:

    .venv\\Scripts\\python tests\\real_panel_e2e.py [carpeta_cad]

Hace lo mismo que el panel: `claude -p` con build_cmd (mismas tools, denegaciones y .mcp.json) pide un pump_case; cuando la
iteración pasa de 100 simula «Detener» (kill_tree de la CLI: muere también el MCP dueño del turno) y llama a `--cli cancel`
como on_stop. Comprueba que cancel para la macro huérfana (duenio_muerto), que el servidor sigue sirviendo (`--cli
server_status`) y apaga con `--cli server_stop`. Escribe casos/<caso>/panel_e2e.json."""
import json, sys, threading, time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "panel"))
sys.path.insert(0, str(REPO))
if any(a in ("-h", "--help") for a in sys.argv[1:]):
    print(__doc__); sys.exit(0)
import chat_panel as cp  # noqa: E402
import mcp_server as m  # noqa: E402

cad = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\sebas\OneDrive\Documentos\cfturbo-agent\versions\BombaRapida_v1"
_cad = Path(cad)
if not _cad.is_dir() or not list(_cad.glob("*.cfturbo-ccm")) or not list(_cad.glob("*.stp")):
    sys.exit(f"carpeta de CAD inválida (faltan .cfturbo-ccm o .stp): {cad} — no arranco el solver")
if not cp.claude_bin():
    sys.exit("no encuentro la CLI `claude`")
caso = "BombaRapida_panel_" + time.strftime("%H%M%S")
out = REPO / "casos" / caso
out.mkdir(parents=True, exist_ok=True)
cp.ensure_mcp_config()
msg = (f"Corre pump_case con case='{caso}', cad_dir='{cad}', mode='rapida', params={{\"iterar\": true, \"post\": true}}. "
       "No hagas nada más ni antes ni después.")
cmd = cp.build_cmd(msg, None)
T0 = time.time()
ev, t = [], {}
import subprocess  # noqa: E402
proc = subprocess.Popen(cmd, cwd=str(REPO), env=cp.clean_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")


def leer():
    for ln in proc.stdout:
        for e in cp.parse_event(ln):
            ev.append([round(time.time() - T0, 1)] + [str(x)[:200] for x in e])
            print(f"[{time.time() - T0:6.1f} s] {e[0]}: {str(e[1:])[:160]}", flush=True)


threading.Thread(target=leer, daemon=True).start()
while proc.poll() is None:
    pg = m.read_progreso()
    if pg.get("fase") == "iterar" and (pg.get("iteracion") or 0) > 100 and not pg.get("terminado"):
        break
    time.sleep(1)
t["hasta_iter_100"] = round(time.time() - T0, 1)
detenido = proc.poll() is None
if detenido:  # «Detener» del panel: matar la CLI (y el MCP hijo), luego --cli cancel
    cp.kill_tree(proc, cp.protected_pids(cp.state_dirs()))
    t_c = time.time()
    rc = cp.run_cli("cancel", {}, repo=REPO, timeout=300)
    t["cancel_s"] = round(time.time() - t_c, 1)
else:
    rc = {"error": "la CLI terminó antes de la iteración 100"}
print("cancel:", json.dumps(rc, ensure_ascii=False)[:600], flush=True)
st = cp.run_cli("server_status", {}, repo=REPO, timeout=60)
print("server_status:", json.dumps(st, ensure_ascii=False)[:400], flush=True)
t_s = time.time()
sp = cp.run_cli("server_stop", {}, repo=REPO, timeout=None)
t["server_stop_s"] = round(time.time() - t_s, 1)
t["total"] = round(time.time() - T0, 1)
info = {"caso": caso, "tiempos_s": t, "detenido_en_iterar": detenido, "cancel": rc, "server_status_tras_cancel": st,
        "server_stop": sp, "progreso_final": m.read_progreso(), "eventos_cli": ev[-80:],
        "abort_borrado": not m.abort_file().exists(), "cancel_json_borrado": not m.cancel_file().exists()}
(out / "panel_e2e.json").write_text(json.dumps(info, indent=1, ensure_ascii=False), encoding="utf-8")
print(json.dumps({k: info[k] for k in ("tiempos_s", "detenido_en_iterar", "abort_borrado", "cancel_json_borrado")}, ensure_ascii=False))

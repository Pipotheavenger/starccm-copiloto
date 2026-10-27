r"""Prueba real del PANEL con su UI (gasta licencia PoD ~3-5 min y turnos de la CLI `claude`). Lanzar FUERA del sandbox:

    .venv\Scripts\python tests\real_panel_e2e.py [carpeta_cad] [--fase-detener N] [--detener-en-malla] [--sin-apagar]

--detener-en-malla: pulsa «Detener» en cuanto progreso.json dice fase «mallar» (cancel queda pendiente y la macro debe parar
antes de iterar, sin post).

Usa la clase real `chat_panel.Panel` en una ventana Tk (retirada; `--visible` la deja a la vista): escribe el mensaje en la
caja y pulsa el botón «Enviar» (`invoke`), bombea Tk (`root.update()`) mientras sondea progreso.json y, al pasar la iteración
100 (o `--fase-detener N`: la iteración N), pulsa el botón real «Detener» (`Panel.on_stop`: mata la CLI y su MCP, mide la
macro, pide `--cli cancel`). Espera el resultado del cancel (si es «pendiente», hasta que progreso.json diga terminado),
consulta `--cli server_status` y apaga con el botón real «Apagar solver» (dos clics, confirmación del panel).
Escribe casos/<caso>/panel_e2e.json con el texto del chat, tiempos, resultado del cancel, progreso y agent.log (cola).
Valida CAD y la CLI `claude` antes de arrancar nada; `--help` no arranca nada."""
import json, os, sys, time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "panel"))
sys.path.insert(0, str(REPO))
if any(a in ("-h", "--help") for a in sys.argv[1:]):
    print(__doc__)
    sys.exit(0)


def opt(name, default=None):
    if name in sys.argv:
        i = sys.argv.index(name)
        v = sys.argv[i + 1] if i + 1 < len(sys.argv) else default
        del sys.argv[i:i + 2]
        return v
    return default


VISIBLE = "--visible" in sys.argv
NO_APAGAR = "--sin-apagar" in sys.argv
EN_MALLA = "--detener-en-malla" in sys.argv
sys.argv = [a for a in sys.argv if a not in ("--visible", "--sin-apagar", "--detener-en-malla")]
IT_STOP = int(opt("--fase-detener", "100"))
cad = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\sebas\OneDrive\Documentos\cfturbo-agent\versions\BombaRapida_v1"

import chat_panel as cp  # noqa: E402
import mcp_server as m  # noqa: E402

_cad = Path(cad)
if not _cad.is_dir() or not list(_cad.glob("*.cfturbo-ccm")) or not list(_cad.glob("*.stp")):
    sys.exit(f"carpeta de CAD inválida (faltan .cfturbo-ccm o .stp): {cad} — no arranco el solver")
if not cp.claude_bin():
    sys.exit("no encuentro la CLI `claude`")

import tempfile  # noqa: E402
import tkinter as tk  # noqa: E402

caso = "BombaRapida_panel_" + time.strftime("%H%M%S")
out = REPO / "casos" / caso
out.mkdir(parents=True, exist_ok=True)
cp.ESTADO = Path(tempfile.mkdtemp(prefix="panel-e2e-")) / ".estado.json"  # sin reanudar ni pisar la sesión real del panel
cp.ensure_mcp_config()
msg = (f"Corre pump_case con case='{caso}', cad_dir='{cad}', mode='rapida', params={{\"iterar\": true, \"post\": true}}. "
       "No hagas nada más ni antes ni después.")

root = tk.Tk()
panel = cp.Panel(root)
panel.sid = None
if not VISIBLE:
    root.withdraw()
T0 = time.time()
t: dict = {}
txt = lambda: panel.hist.get("1.0", "end-1c")


def pump(secs=0.0, until=None, timeout=None):
    """Bombea Tk (el panel vive de root.after/queue). Con `until`, espera a que sea verdad (o al timeout)."""
    end = time.time() + (timeout if timeout is not None else secs)
    while True:
        root.update()
        if until is not None and until():
            return True
        if time.time() >= end:
            return until is None
        time.sleep(0.05)


# 1. mensaje con el mismo método que el botón Enviar
panel.entry.insert("1.0", msg)
panel.btn["enviar"].invoke()
print(f"[{time.time() - T0:6.1f} s] enviado desde el panel", flush=True)

# 2. sondeo de progreso.json con Tk bombeando
vistos = 0


def llegamos():
    global vistos
    pg = m.read_progreso()
    it = pg.get("iteracion") or 0
    if it and it // 25 > vistos:
        vistos = it // 25
        print(f"[{time.time() - T0:6.1f} s] fase {pg.get('fase')} it {it}", flush=True)
    if EN_MALLA:
        return pg.get("fase") == "mallar" and not pg.get("terminado") and pg.get("caso") == caso
    return pg.get("fase") == "iterar" and it > IT_STOP and not pg.get("terminado")


hubo = pump(until=lambda: llegamos() or not panel.runner.busy, timeout=1500)
t["hasta_iteracion"] = round(time.time() - T0, 1)
detenido = bool(panel.runner.busy)
n0 = len(txt())
if detenido:  # botón «Detener» real
    t_c = time.time()
    panel.btn["detener"].invoke()
    print(f"[{time.time() - T0:6.1f} s] pulsado Detener (panel.on_stop)", flush=True)
    if panel.confirm:  # por si on_stop pidiera confirmación dentro del panel: segundo clic
        panel.btn["detener"].invoke()
    pump(until=lambda: "cancel" not in panel.cli_active, timeout=300)  # on_stop lanza el cancel de forma síncrona
    t["cancel_s"] = round(time.time() - t_c, 1)
    if panel.cancel_pending:  # «pendiente»: hasta que progreso.json diga terminado
        t_p = time.time()
        pump(until=lambda: not panel.cancel_pending, timeout=600)
        t["pendiente_s"] = round(time.time() - t_p, 1)
    pump(1.5)  # deja que check_after_stop (1,2 s) escriba sus avisos
else:
    print("la CLI terminó antes de la iteración", IT_STOP, flush=True)
chat_tras_detener = txt()[n0:]
cancel_estado = {"cancel_pending_al_final": bool(panel.cancel_pending), "cli_activos": sorted(panel.cli_active)}

# 3. servidor vivo y apagado con el botón real
from_panel = {}
st = cp.run_cli("server_status", {}, repo=REPO, timeout=60)
print("server_status:", json.dumps(st, ensure_ascii=False)[:400], flush=True)
sp = None
if NO_APAGAR:
    print("--sin-apagar: dejo el solver vivo", flush=True)
else:
    t_s = time.time()
    n1 = len(txt())
    panel.btn["apagar"].invoke()  # 1.er clic: «¿Seguro?»
    panel.btn["apagar"].invoke()  # 2.º clic: server_stop directo
    pump(until=lambda: "server_stop" not in panel.cli_active and "Apagar solver:" in txt()[n1:], timeout=900)
    t["server_stop_s"] = round(time.time() - t_s, 1)
    sp = txt()[n1:]
    print("apagado desde el panel:", sp.strip()[:300], flush=True)
    if "Apagar solver:" not in sp:  # el botón no llegó: apaga por la CLI para no dejar licencia cobrando
        sp = {"fallback_cli": cp.run_cli("server_stop", {}, repo=REPO, timeout=None)}
st_final = cp.run_cli("server_status", {}, repo=REPO, timeout=60)
t["total"] = round(time.time() - T0, 1)


def cola_log(n=40):
    try:
        return m.AGENT_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except OSError:
        return []


info = {"caso": caso, "detener_en_malla": EN_MALLA, "tiempos_s": t, "detenido_en_iteracion": detenido, "iteracion_umbral": IT_STOP,
        "chat_completo": txt(), "chat_tras_detener": chat_tras_detener, "cancel_estado_panel": cancel_estado,
        "server_status_tras_cancel": st, "apagado_panel": sp, "server_status_final": st_final,
        "progreso_final": m.read_progreso(), "agent_log_cola": cola_log(),
        "abort_borrado": not m.abort_file().exists(), "cancel_json_borrado": not m.cancel_file().exists()}
(out / "panel_e2e.json").write_text(json.dumps(info, indent=1, ensure_ascii=False), encoding="utf-8")
print(json.dumps({k: info[k] for k in ("tiempos_s", "detenido_en_iteracion", "abort_borrado", "cancel_json_borrado")}, ensure_ascii=False))
try:
    panel.runner.stop()
    root.destroy()
except Exception:
    pass

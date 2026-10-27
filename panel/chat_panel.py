"""Panel flotante de chat para starccm-copiloto (tkinter, solo biblioteca estándar). Ver ARQUITECTURA.md §2.

Uso:  python panel/chat_panel.py  |  --selftest  |  --demo (datos de ejemplo, sin CLI ni solver)
Lanza `claude -p … --output-format stream-json` en un hilo; los eventos pasan por una cola que la UI vacía con after().
"""
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
ESTADO = HERE / ".estado.json"

ESTILO = (
    "Copiloto de STAR-CCM+ de Sebastián. Español, 1-3 frases, números en tablas. Cada turno del modelo cuesta más que "
    "el solver. Bomba de CFturbo: pump_case; agrupa pasos con run_steps; iterate con until=\"stable\". No re-verifiques "
    "lo que el resultado ya trae ni repitas pasos que funcionaron. Si falta un dato, UNA pregunta. El usuario mira la "
    "GUI real: di qué abrir. No toques nada mientras edita en la GUI. Pregunta antes de lo caro (mallas grandes, "
    "apagar). Lee conocimiento/ solo cuando lo necesites. NUNCA muestres claves, licencias ni el contenido de .env. "
    "Cierra con qué quedó hecho."
)

MODOS = {"rapido": ("Rápido", "PANEL_MODEL_RAPIDO", "sonnet", "low"),
         "preciso": ("Preciso", "PANEL_MODEL_PRECISO", "opus", "medium")}
PREFIJO_COLA = "MENSAJE DEL USUARIO MIENTRAS TRABAJABAS:\n"


def modo_flags(modo: str) -> list[str]:
    _, var, model, effort = MODOS.get(modo, MODOS["rapido"])
    return ["--model", os.environ.get(var) or model, "--effort", effort]


# ---------- traducción de herramientas ----------
TOOLS = {
    "server_start": "Arrancando el solver", "server_stop": "Guardando y apagando el solver",
    "server_status": "Mirando el estado del solver", "run_java": "Ejecutando una macro en STAR-CCM+",
    "run_steps": "Ejecutando varios pasos en una macro", "inspect": "Leyendo el modelo",
    "iterate": "Iterando", "export_scene": "Exportando una escena", "save": "Guardando el .sim",
    "recipe_search": "Buscando una receta", "recipe_save": "Guardando una receta",
    "docs_search": "Consultando la API de STAR-CCM+", "pump_case": "Montando la bomba (importar, malla, física)",
    "gui_attach": "Abriendo la GUI de STAR-CCM+", "gui_status": "Mirando si la GUI está conectada",
    "Read": "Leyendo un archivo", "Glob": "Buscando archivos", "Grep": "Buscando en archivos",
}


def tool_label(name: str, inp: dict | None = None) -> str:
    short = name.split("__")[-1] if name.startswith("mcp__") else name
    base = TOOLS.get(short, "Usando " + short)
    inp = inp or {}
    if short == "iterate" and inp.get("n"):
        base += f" {inp['n']} iteraciones"
    elif short == "inspect" and inp.get("path"):
        base += f" ({inp['path']})"
    elif short == "run_java":
        m = re.search(r"//\s*QUE:\s*(.+)", str(inp.get("body", "")))
        if m:
            base = "Macro: " + m.group(1).strip()[:70]
    elif short in ("Read", "Glob", "Grep"):
        t = inp.get("file_path") or inp.get("pattern") or ""
        if t:
            base += f" ({Path(str(t)).name if short == 'Read' else t})"
    elif short == "pump_case" and inp.get("case"):
        base += f" «{inp['case']}»"
    return base


# ---------- eventos de la CLI ----------
def parse_event(line: str) -> list[tuple]:
    """Una línea stream-json -> eventos ('init',sid) ('text',t) ('tool',id,label) ('tool_result',id,txt,err)
    ('result',sid,subtype,err) ('raw',txt)."""
    line = line.strip()
    if not line:
        return []
    try:
        o = json.loads(line)
    except ValueError:
        return [("raw", line[:500])]
    t, ev = o.get("type"), []
    if t == "system" and o.get("subtype") == "init":
        ev.append(("init", o.get("session_id")))
    elif t == "assistant":
        for b in o.get("message", {}).get("content", []):
            if b.get("type") == "text" and b.get("text"):
                ev.append(("text", b["text"]))
            elif b.get("type") == "tool_use":
                ev.append(("tool", b.get("id", ""), tool_label(b.get("name", ""), b.get("input"))))
    elif t == "user":
        for b in o.get("message", {}).get("content", []):
            if isinstance(b, dict) and b.get("type") == "tool_result":
                c = b.get("content")
                txt = c if isinstance(c, str) else " ".join(x.get("text", "") for x in (c or []) if isinstance(x, dict))
                ev.append(("tool_result", b.get("tool_use_id", ""), txt[:300], bool(b.get("is_error"))))
    elif t == "result":
        ev.append(("result", o.get("session_id"), o.get("subtype"), bool(o.get("is_error")), o.get("num_turns")))
    return ev


def clean_env() -> dict:
    return {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}


def claude_bin() -> str | None:
    return os.environ.get("PANEL_CLAUDE_BIN") or shutil.which("claude")


DEFAULT_TOOLS = "mcp__starccm__*,Read,Glob,Grep"
# La CLI no puede leer la clave PoD: reglas de denegación (sintaxis de permisos de Claude Code, rutas tipo gitignore;
# las de Read también cubren Grep/Glob, y se repiten por si acaso). .env.example sigue legible.
DENY_PATHS = (".env", "**/.env", "~/.starccm-agent.env")
DENIED_TOOLS = [f"{t}({pth if pth.startswith(('*', '~')) else './' + pth})" for t in ("Read", "Grep", "Glob") for pth in DENY_PATHS]


def ensure_mcp_config(repo: Path = REPO) -> tuple[Path, bool]:
    """Devuelve (.mcp.json, creado). Si falta, lo crea desde .mcp.json.example con rutas absolutas del .venv y mcp_server.py."""
    dst = repo / ".mcp.json"
    if dst.is_file():
        return dst, False
    cfg = _json(repo / ".mcp.json.example") or {"mcpServers": {"starccm": {}}}
    srv = cfg.setdefault("mcpServers", {}).setdefault("starccm", {})
    vpy = repo / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    srv["command"] = str(vpy if vpy.is_file() else sys.executable)
    srv["args"] = [str(repo / "mcp_server.py")]
    dst.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return dst, True


def build_cmd(msg: str, sid: str | None, allowed: str = DEFAULT_TOOLS, modo: str = "rapido",
              mcp: Path | None = None) -> list[str] | None:
    b = claude_bin()
    if not b:
        return None
    pre = [sys.executable, b] if b.lower().endswith(".py") else [b]
    cmd = pre + ["-p", msg, "--output-format", "stream-json", "--verbose"]
    if sid:
        cmd += ["--resume", sid]
    cmd += modo_flags(modo)
    cmd += ["--mcp-config", str(mcp or REPO / ".mcp.json"), "--strict-mcp-config", "--allowedTools", allowed,
            "--disallowedTools", ",".join(DENIED_TOOLS), "--append-system-prompt", ESTILO]
    return cmd


def _descendants(pid: int) -> list[tuple[int, int]]:
    """(pid, ppid) de todos los descendientes de `pid` (Windows: una consulta CIM)."""
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "Get-CimInstance Win32_Process | ForEach-Object { \"$($_.ProcessId) $($_.ParentProcessId)\" }"],
                             capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    kids: dict[int, list[int]] = {}
    for ln in out.splitlines():
        a = ln.split()
        if len(a) == 2 and a[0].isdigit() and a[1].isdigit():
            kids.setdefault(int(a[1]), []).append(int(a[0]))
    res, todo = [], [pid]
    while todo:
        x = todo.pop()
        for k in kids.get(x, []):
            res.append((k, x))
            todo.append(k)
    return res


def protected_pids(dirs) -> set[int]:
    """Procesos que Detener nunca toca: el solver y el residente (de .server.json y residente/residente.json)."""
    out: set[int] = set()
    for d in dirs:
        st, rj = _json(Path(d) / ".server.json"), _json(Path(d) / "residente" / "residente.json")
        for v in (st.get("pid"), rj.get("pid"), rj.get("server_pid")):
            try:
                out.add(int(v))
            except (TypeError, ValueError):
                pass
    return out


def kill_tree(p: subprocess.Popen, protect=()) -> None:
    """Mata la CLI y su MCP, pero NO el solver ni el residente (`protect`), aunque cuelguen del árbol:
    `taskkill /T` mata también a los hijos lanzados 'desacoplados' mientras el padre siga vivo."""
    if p.poll() is not None:
        return
    protect = {int(x) for x in protect}
    if os.name == "nt":
        if protect:
            kids = _descendants(p.pid)
            safe = set(protect)
            for _ in kids:  # todo lo que cuelga de un protegido también queda a salvo
                safe |= {c for c, par in kids if par in safe}
            for c, _par in reversed(kids):
                if c not in safe:
                    subprocess.run(["taskkill", "/PID", str(c), "/F"], capture_output=True, timeout=30)
            p.kill()
        else:
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, timeout=30)
    if p.poll() is None:
        p.kill()


class Runner:
    """Una conversación por vez: un hilo lee la CLI y mete eventos en `q`. El último evento es ('done', rc, detenido)."""

    def __init__(self, q: queue.Queue, repo: Path = REPO, sdir: Path | None = None):
        self.q, self.proc, self.stopped, self.repo, self.sdir = q, None, False, Path(repo), sdir
        self.msg, self.resumed = "", None

    @property
    def busy(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def send(self, msg: str, sid: str | None, allowed: str = DEFAULT_TOOLS, modo: str = "rapido") -> bool:
        try:
            mcp, creado = ensure_mcp_config(self.repo)
        except OSError as e:
            mcp, creado = self.repo / ".mcp.json", False
            self.q.put(("error", f"no pude crear .mcp.json: {e}"))
        if creado:
            self.q.put(("aviso", "No existía .mcp.json: lo creé desde .mcp.json.example con las rutas de este equipo "
                                 f"({mcp.name})."))
        cmd = build_cmd(msg, sid, allowed, modo, mcp)
        if not cmd:
            self.q.put(("error", "no encuentro la CLI `claude` (PATH o PANEL_CLAUDE_BIN)"))
            self.q.put(("done", -1, False))
            return False
        self.stopped = False
        self.msg, self.resumed = msg, sid
        try:
            self.proc = subprocess.Popen(cmd, cwd=self.repo, env=clean_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
        except OSError as e:
            self.q.put(("error", f"no pude lanzar la CLI: {e}"))
            self.q.put(("done", -1, False))
            return False
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()
        return True

    def _read(self, p):
        err: list[str] = []
        te = threading.Thread(target=lambda: [err.append(x) for x in p.stderr], daemon=True)
        te.start()
        got_result = lost = False
        for line in p.stdout:
            if self.resumed and SESSION_RE.search(line):
                lost = True  # --resume con una sesión caducada: el panel empieza una nueva y reenvía
            for ev in parse_event(line):
                if lost and ev[0] in ("result", "raw"):
                    continue
                got_result |= ev[0] == "result"
                self.q.put(ev)
        rc = p.wait()
        te.join(2)
        if self.resumed and rc != 0 and SESSION_RE.search(" ".join(err)):
            lost = True
        if lost and rc != 0 and not self.stopped:
            self.q.put(("session_lost", self.resumed))
        elif rc != 0 and not self.stopped and not got_result:
            self.q.put(("error", f"la CLI terminó con código {rc}: " + " ".join(err)[-300:]))
        self.q.put(("done", rc, self.stopped))

    def stop(self):
        self.stopped = True
        if self.proc:
            kill_tree(self.proc, protected_pids(state_dirs(self.repo, self.sdir)))


# ---------- sesión y estado ----------
def load_estado() -> dict:
    try:
        d = json.loads(ESTADO.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_estado(**kw) -> None:
    d = load_estado()
    d.update(kw)
    try:
        ESTADO.write_text(json.dumps(d), encoding="utf-8")
    except OSError:
        pass


def load_sid() -> str | None:
    return load_estado().get("session_id")


def save_sid(sid: str | None) -> None:
    save_estado(session_id=sid)


def load_modo() -> str:
    m = load_estado().get("modo")
    return m if m in MODOS else "rapido"


def fmt_s(x: float) -> str:
    x = int(round(x))
    return f"{x} s" if x < 60 else f"{x // 60} min {x % 60:02d} s"


def pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _json(path: Path) -> dict:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def state_dir() -> Path:
    """Carpeta de estado del MCP: panel_estado.json (lo escribe el MCP) -> COPILOTO_WORK -> %LOCALAPPDATA%/starccm-copiloto."""
    cfg = _json(REPO / "panel_estado.json").get("estado_dir")
    if cfg:
        return Path(str(cfg))
    if os.environ.get("COPILOTO_WORK"):
        return Path(os.environ["COPILOTO_WORK"])
    base = os.environ.get("LOCALAPPDATA") if os.name == "nt" else None
    return (Path(base) if base else Path.home() / (("AppData/Local") if os.name == "nt" else ".local/share")) / "starccm-copiloto"


def state_dirs(repo: Path | None = None, sdir: Path | None = None) -> list[Path]:
    """Dónde buscar .server.json/.inject.lock: la carpeta de estado y, por compatibilidad, el repo. Sin argumentos usa las reales."""
    if repo is None and sdir is None:
        return [state_dir(), REPO]
    return [Path(x) for x in (sdir, repo) if x]


def _first(name: str, dirs) -> Path:
    for d in dirs:
        if (d / name).is_file():
            return d / name
    return dirs[0] / name


def warning_text(w, idle_min=None) -> str | None:
    """idle_warning puede ser texto o un dict {msg, idle_min, ...}."""
    if isinstance(w, dict):
        w = w.get("msg") or (f"{w.get('idle_min', idle_min)} min sin actividad: ¿apagar el solver?" if w else None)
    return str(w) if w else None


def lock_info(repo: Path | None = None, sdir: Path | None = None) -> dict | None:
    """.inject.lock vigente (menos de 2 h): {tool, pid, alive}. Sirve aunque el dueño ya haya muerto (la macro sigue en el solver)."""
    lf = _first(".inject.lock", state_dirs(repo, sdir))
    try:
        if time.time() - lf.stat().st_mtime > 2 * 3600:
            return None
    except OSError:
        return None
    lk = _json(lf)
    return {"tool": lk.get("tool") or lk.get("herramienta") or "?", "pid": lk.get("pid"), "alive": pid_alive(lk.get("pid"))}


def stop_warning(repo: Path | None = None, sdir: Path | None = None) -> str | None:
    """Tras Detener: aviso si el solver sigue vivo con una macro en curso."""
    s = read_status(repo, sdir)
    lk = lock_info(repo, sdir)
    if s["solver"] and lk:
        return (f"Detuve la CLI y el MCP, pero el solver sigue vivo ({s['caso']}) y la macro «{lk['tool']}» puede seguir "
                "corriendo en él. Cobra por hora: apágalo si ya no lo necesitas.")
    return None


VIGIL_MAX_S = 120


def read_status(repo: Path | None = None, sdir: Path | None = None) -> dict:
    """Lee .server.json y .inject.lock (sin importar mcp_server). Devuelve campos + `texto` para la barra."""
    dirs = state_dirs(repo, sdir)
    sf = _first(".server.json", dirs)
    st = _json(sf)
    alive = bool(st.get("pid")) and pid_alive(st["pid"])
    out = {"solver": alive, "caso": None, "idle_min": None, "idle_warning": None, "gui": False, "busy": None,
           "vigil_age": None, "vigil_down": False, "horas": None}
    parts = []
    if alive:
        out["caso"] = Path(st["sim"]).stem if st.get("sim") else "(sin .sim)"
        im = st.get("idle_min")
        if isinstance(im, (int, float)) and not isinstance(im, bool):
            out["idle_min"] = int(im)
        else:
            try:
                out["idle_min"] = int((time.time() - sf.stat().st_mtime) // 60)
            except OSError:
                pass
        parts.append(f"Solver vivo · {out['caso']}" + (f" · {out['idle_min']} min inactivo" if out["idle_min"] is not None else ""))
        out["idle_warning"] = warning_text(st.get("idle_warning"), out["idle_min"])
        age_s = iso_age(st["started"]) if st.get("started") else None
        out["horas"] = None if age_s is None else max(age_s, 0) / 3600
        out["vigil_age"] = iso_age(st["idle_checked"]) if st.get("idle_checked") else None
        out["vigil_down"] = out["vigil_age"] is not None and out["vigil_age"] > VIGIL_MAX_S  # el vigilante no late: cobra sin control
    else:
        parts.append("Solver apagado")
    out["gui"] = bool(st.get("gui")) if alive else False
    parts.append("GUI conectada" if out["gui"] else "GUI no conectada")
    lf = _first(".inject.lock", dirs)
    lk = _json(lf)
    try:
        lk_old = time.time() - lf.stat().st_mtime > 2 * 3600
    except OSError:
        lk_old = False
    if lk.get("pid") and not lk_old and pid_alive(lk["pid"]) and int(lk["pid"]) != os.getpid():
        out["busy"] = lk.get("tool") or lk.get("herramienta") or "?"
        parts.append(f"agente trabajando ({out['busy']})")
    out["texto"] = " | ".join(parts)
    return out


# ---------- acciones directas (sin turno de modelo) y progreso ----------
LABEL_CLI = {"gui_attach": "Abrir GUI", "gui_status": "Estado de la GUI", "server_stop": "Apagar solver",
             "server_status": "Estado del solver", "cancel": "Parar iteraciones"}
SESSION_RE = re.compile(r"no conversation found|session.{0,40}not found|could not find session", re.I)


def repo_python(repo: Path = REPO) -> str:
    vpy = Path(repo) / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return str(vpy if vpy.is_file() else sys.executable)


def cli_cmd(tool: str, args: dict | None = None, repo: Path | None = None) -> list[str]:
    repo = Path(repo or REPO)
    cmd = [repo_python(repo), os.environ.get("PANEL_MCP_SERVER") or str(repo / "mcp_server.py"), "--cli", tool]
    if args:
        cmd.append(json.dumps(args))
    return cmd


def run_cli(tool: str, args: dict | None = None, repo: Path | None = None, timeout: float | None = 120) -> dict:
    """`mcp_server.py --cli <herramienta> [json]` -> dict (JSON por stdout). Nunca lanza: los fallos vuelven como {'error': ...}.
    `timeout=None`: sin límite (server_stop: matarlo a medias deja el solver cobrando)."""
    try:
        p = subprocess.run(cli_cmd(tool, args, repo), cwd=repo or REPO, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
                           creationflags=0x08000000 if os.name == "nt" else 0)
    except subprocess.TimeoutExpired:
        return {"error": f"sin respuesta en {int(timeout)} s"}
    except OSError as e:
        return {"error": f"no pude lanzar mcp_server.py: {e}"}
    res = None
    for ln in reversed((p.stdout or "").strip().splitlines()):
        try:
            res = json.loads(ln)
            break
        except ValueError:
            continue
    if not isinstance(res, dict):
        res = {"error": ((p.stderr or "").strip()[-200:] or "sin salida JSON") if p.returncode else "sin salida JSON"}
    elif p.returncode and "error" not in res:
        res["error"] = (p.stderr or "").strip()[-200:] or f"código {p.returncode}"
    return res


SECRET_RE = re.compile(r"(-podkey\s+|\b(?:podkey|license[_-]?key|licencia|clave|password|passwd|token|api[_-]?key|secret)\w*\s*[=:]\s*)\S+", re.I)


def redact(text: str) -> str:
    """Nada de claves en pantalla: `-podkey X` y `clave=X` salen como «***» (red de seguridad; el MCP ya redacta)."""
    return SECRET_RE.sub(lambda m: m.group(1) + "***", text)


def fmt_cli(tool: str, res: dict) -> tuple[str, bool]:
    """(texto para el chat, ok)."""
    label = LABEL_CLI.get(tool, tool)
    if res.get("error"):
        return f"{label}: {res['error']}", False
    if tool == "cancel" and "cancelled" in res:
        if res.get("pendiente") or res["cancelled"] == "pendiente":  # ABORT puesto: la macro parará sola en cuanto la fase lo permita
            return f"{label}: " + str(res.get("reason") or res.get("mensaje") or res.get("msg") or
                                      "pendiente: la macro parará al terminar la fase en curso"), True
        if res["cancelled"]:
            if res.get("iteracion") is None:  # cancel durante importar/mallar: la macro paró antes de iterar
                return f"{label}: paró antes de iterar" + (f" (al terminar la fase «{res['fase']}»)" if res.get("fase") else ""), True
            return f"{label}: paró en la iteración {res['iteracion']}", True
        if res.get("reason"):
            return f"{label}: {res['reason']}", res.get("ok", True) is not False
    for k in ("msg", "mensaje", "message", "resumen", "summary", "status", "estado"):
        if isinstance(res.get(k), str) and res[k]:
            return f"{label}: {res[k]}", res.get("ok", True) is not False
    items = [f"{k}: {v}" for k, v in res.items() if isinstance(v, (str, int, float, bool)) and v is not None][:6]
    return f"{label}: " + (" · ".join(items) or "hecho"), res.get("ok", True) is not False


def iso_age(s) -> float | None:
    """Segundos desde una marca ISO (hora local si no trae zona)."""
    from datetime import datetime
    try:
        dt = datetime.fromisoformat(str(s))
    except ValueError:
        return None
    return (datetime.now(dt.tzinfo) - dt).total_seconds()


def _num(d: dict, *keys):
    for k in keys:
        v = d.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return v
    return None


PROG_STALE_S = 45
ITER_FASES = ("iterar", "iterando")  # la única fase que `cancel` puede parar (mallar e importar no)


def read_progress(repo: Path | None = None, sdir: Path | None = None) -> dict | None:
    """progreso.json (lo escribe el MCP ≤ cada 2 s durante iterate/pump_case). `active` = hay cálculo en curso:
    fase distinta de «terminado» y archivo vivo (< 45 s)."""
    f = _first("progreso.json", state_dirs(repo, sdir))
    d = _json(f)
    if not d:
        return None
    try:
        age = time.time() - f.stat().st_mtime
    except OSError:
        return None
    fase = str(d.get("fase") or "").strip()
    it, tot = _num(d, "iteracion", "iteración", "iter", "it"), _num(d, "iteraciones", "iter_max", "n", "total", "max_iter")
    frac = _num(d, "fraccion", "frac")
    if frac is None and (pct := _num(d, "pct", "porcentaje")) is not None:
        frac = pct / 100
    if frac is None and it is not None and tot:
        frac = it / tot
    eta = _num(d, "eta", "eficiencia")
    fin = bool(d.get("terminado")) or fase.lower() in ("terminado", "listo", "fin")
    return {"caso": d.get("caso") or "", "fase": fase, "it": it, "tot": tot, "frac": None if frac is None else max(0.0, min(1.0, frac)),
            "H": _num(d, "H", "h", "altura"), "M": _num(d, "M", "m", "torque"), "eta": eta,
            "cont": _num(d, "continuidad", "cont", "continuity"), "seg": _num(d, "segundos", "s", "transcurrido_s"),
            "resta": _num(d, "falta_s", "restante_s", "faltan_s", "restante", "eta_s"), "age": age,
            "iterando": fase.lower() in ITER_FASES, "terminado": fin,
            "abierto": bool(fase) and not fin and age <= 2 * 3600,  # sin terminar aunque nadie lo actualice (dueño muerto)
            "active": bool(fase) and not fin and age <= PROG_STALE_S}


def fnum(x, dec=1, unit="") -> str:
    if x is None:
        return "—"
    return f"{x:.{dec}f}".replace(".", ",") + unit


def fsci(x) -> str:
    return "—" if x is None else f"{x:.1e}".replace(".", ",")


def fresta(s) -> str:
    if s is None:
        return ""
    return f"faltan ~{max(int(round(s / 60)), 1)} min" if s >= 60 else f"faltan ~{int(round(s))} s"


def no_cancel_text(pr: dict) -> str:
    """Una línea: por qué «Parar iteraciones» no sirve en esta fase y cuánto falta."""
    falta = pr.get("resta")
    cuando = f"termina sola; {fresta(falta)}" if falta else "termina sola (sin tiempo estimado)"
    return (f"La fase «{pr['fase']}» no se puede parar: {cuando}. Apagar el solver la corta, pero pierdes lo hecho "
            "desde el último guardado.")


def aviso_horas() -> float:
    """Horas con el solver vivo a partir de las cuales el panel avisa (COPILOTO_AVISO_HORAS, 2 por defecto; ≤ 0 lo desactiva)."""
    try:
        return float(os.environ.get("COPILOTO_AVISO_HORAS", "2").replace(",", "."))
    except ValueError:
        return 2.0


def horas_text(h: float, gui: bool) -> str:
    return (f"⏱ Solver encendido hace {fnum(h, 1)} h: la licencia cobra por hora"
            + (" y con la GUI abierta no se apaga solo." if gui else "."))


def dpi_aware() -> bool:
    """Windows: proceso consciente del DPI ANTES de crear Tk (si no, a 150 % todo sale borroso)."""
    if os.name != "nt":
        return False
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # por monitor
        return True
    except Exception:
        try:
            return bool(ctypes.windll.user32.SetProcessDPIAware())
        except Exception:
            return False


# ---------- ventana ----------
FONT, MONO = ("Segoe UI", 10), ("Consolas", 10)
INLINE = re.compile(r"(\*\*[^*\n]+\*\*|`[^`\n]+`)")
C = {"bg": "#f4f5f7", "card": "#ffffff", "line": "#e2e5ea", "ink": "#1f2937", "mute": "#6b7280", "blue": "#2563eb",
     "user": "#dbeafe", "amber_bg": "#fef3c7", "amber": "#92400e", "red_bg": "#fee2e2", "red": "#991b1b"}
CHIPS = {  # (bg, fg) de cada «chip» encendido; apagado = gris
    "off": ("#e5e7eb", "#4b5563"), "solver": ("#dcfce7", "#166534"), "gui": ("#dbeafe", "#1e40af"), "agent": ("#fef3c7", "#92400e")}
BTN = {  # clave: (bg, fg, activebg)
    "enviar": ("#2563eb", "#ffffff", "#1d4ed8"), "detener": ("#ffedd5", "#9a3412", "#fed7aa"),
    "nueva": ("#eceef2", "#374151", "#dde0e6"), "gui": ("#dbeafe", "#1d4ed8", "#bfdbfe"),
    "apagar": ("#fee2e2", "#b91c1c", "#fecaca"), "peligro": ("#dc2626", "#ffffff", "#b91c1c")}
SUGERENCIAS = (("Simular una bomba de CFturbo", "msg", "Quiero simular una bomba de CFturbo"),
               ("Abrir la GUI", "cli", "gui_attach"), ("Estado del solver", "cli", "server_status"))


class Panel:
    def __init__(self, root, runner_q=None, repo: Path | None = None, sdir: Path | None = None):
        import tkinter as tk
        self.tk, self.root = tk, root
        self.repo, self.sdir = repo, sdir  # None = ubicaciones reales (panel_estado.json / COPILOTO_WORK / LOCALAPPDATA)
        self.sc = max(root.winfo_fpixels("1i") / 96, 1.0)
        self.closed = self.closing = False
        self.aft: dict[str, str] = {}
        self.close_timeout, self.dlg, self.close_wait = 60.0, None, False
        self.q = runner_q or queue.Queue()
        self.runner = Runner(self.q, repo or REPO, sdir)
        self.sid = load_sid()
        self.last_msg = ""
        self.tools: dict[str, tuple[str, float]] = {}
        self.pending: list[tuple[str, str]] = []  # (mark, texto) mensajes en cola
        self.turn_t0 = 0.0
        self.tool_s = 0.0
        self.n_tools = 0
        self.npend = 0
        self.confirm: dict[str, float] = {}
        self.cli_active: set[str] = set()
        self.lost = False
        self.prog: dict | None = None
        self.vis: tuple = (False, False, False, False)  # vigilante, horas, idle_warning, progreso
        self.cli_t0: dict[str, float] = {}
        self.cancel_pending = False  # cancel «pendiente» en vigilancia
        self.orphan_key = None  # (caso, fase) de la macro sin dueño ya avisada
        self.auto_cancel = False  # el cancel lo pidió Detener (no el usuario): si no para, avisa del solver cobrando
        self.empty = None
        root.title("Copiloto STAR-CCM+")
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w, h = min(self.px(440), sw - 20), min(self.px(700), sh - 80)
        root.geometry(f"{w}x{h}+{max(sw - w - 20, 0)}+{max((sh - h) // 2 - 20, 0)}")
        root.minsize(self.px(340), self.px(420))
        root.configure(bg=C["bg"])
        self.top = tk.BooleanVar(value=True)
        root.attributes("-topmost", True)
        P = self.px

        # cabecera: título, «chips» de estado y Encima
        head = tk.Frame(root, bg=C["card"], highlightthickness=1, highlightbackground=C["line"])
        head.pack(fill="x")
        r1 = tk.Frame(head, bg=C["card"])
        r1.pack(fill="x", padx=P(12), pady=(P(8), P(2)))
        tk.Label(r1, text="Copiloto STAR-CCM+", bg=C["card"], fg=C["ink"], font=(FONT[0], 12, "bold")).pack(side="left")
        tk.Checkbutton(r1, text="Encima", variable=self.top, command=self.toggle_top, bg=C["card"], activebackground=C["card"],
                       fg=C["mute"], font=(FONT[0], 9), bd=0, highlightthickness=0).pack(side="right")
        r2 = tk.Frame(head, bg=C["card"])
        r2.pack(fill="x", padx=P(12), pady=(P(2), P(2)))
        self.chip = {}
        for key in ("solver", "gui", "agent"):
            lb = tk.Label(r2, text="", font=(FONT[0], 9, "bold"), padx=P(9), pady=P(2))
            lb.pack(side="left", padx=(0, P(6)))
            self.chip[key] = lb
        self.sub = tk.Label(head, text="", anchor="w", bg=C["card"], fg=C["mute"], font=(FONT[0], 9))
        self.sub.pack(fill="x", padx=P(12), pady=(0, P(6)))
        mbar = self.modo_bar = tk.Frame(root, bg=C["bg"])
        mbar.pack(fill="x", padx=P(8), pady=(P(4), 0))
        tk.Label(mbar, text="Modo", bg=C["bg"], fg=C["mute"], font=(FONT[0], 9)).pack(side="left", padx=(P(4), P(2)))
        self.modo = tk.StringVar(value=load_modo())
        for k, (nombre, *_r) in MODOS.items():
            tk.Radiobutton(mbar, text=nombre, value=k, variable=self.modo, command=self.on_modo, bg=C["bg"], activebackground=C["bg"],
                           font=FONT, bd=0, highlightthickness=0).pack(side="left", padx=2)
        self.modo_info = tk.Label(mbar, text="", bg=C["bg"], fg=C["mute"], font=(FONT[0], 9))
        self.modo_info.pack(side="left", padx=P(6))
        self.on_modo(save=False)

        # bandas y tarjeta de progreso (se muestran solo si hacen falta; ver relayout)
        self.mid = tk.Frame(root, bg=C["bg"])
        self.mid.pack(fill="x", padx=P(8))
        self.vig = tk.Frame(self.mid, bg=C["red_bg"])
        self.vig_lbl = tk.Label(self.vig, text="⛔ El vigilante no responde: el solver puede quedar cobrando", anchor="w", justify="left",
                                bg=C["red_bg"], fg=C["red"], font=(FONT[0], 10, "bold"), wraplength=P(300), padx=P(8), pady=P(5))
        self.vig_lbl.pack(side="left", fill="x", expand=True)
        self.vig_btn = self.button(self.vig, "Apagar", lambda: self.twice("vig", self.vig_btn, "Apagar", self.on_off_now), "peligro", small=True)
        self.vig_btn.pack(side="right", padx=P(6), pady=P(4))
        self.hrs = tk.Frame(self.mid, bg=C["amber_bg"])
        self.hrs_lbl = tk.Label(self.hrs, text="", anchor="w", justify="left", bg=C["amber_bg"], fg=C["amber"], font=(FONT[0], 10, "bold"),
                                wraplength=P(300), padx=P(8), pady=P(5))
        self.hrs_lbl.pack(side="left", fill="x", expand=True)
        self.hrs_btn = self.button(self.hrs, "Apagar", lambda: self.twice("horas", self.hrs_btn, "Apagar", self.on_off_now), "apagar", small=True)
        self.hrs_btn.pack(side="right", padx=P(6), pady=P(4))
        self.warn = tk.Label(self.mid, text="", anchor="w", justify="left", bg=C["amber_bg"], fg=C["amber"], font=(FONT[0], 10, "bold"),
                             wraplength=P(380), padx=P(8), pady=P(5))  # banda ámbar: idle_warning
        self.card = tk.Frame(self.mid, bg=C["card"], highlightthickness=1, highlightbackground=C["line"])
        ch = tk.Frame(self.card, bg=C["card"])
        ch.pack(fill="x", padx=P(10), pady=(P(8), P(4)))
        self.p_title = tk.Label(ch, text="", anchor="w", bg=C["card"], fg=C["ink"], font=(FONT[0], 10, "bold"))
        self.p_title.pack(side="left")
        self.p_rest = tk.Label(ch, text="", anchor="e", bg=C["card"], fg=C["blue"], font=(FONT[0], 9, "bold"))
        self.p_rest.pack(side="right")
        self.bar = tk.Canvas(self.card, height=P(8), bg=C["card"], highlightthickness=0, bd=0)
        self.bar.pack(fill="x", padx=P(10))
        self.bar.bind("<Configure>", lambda e: self.draw_bar())
        self.bar_frac = None
        tl = tk.Frame(self.card, bg=C["card"])
        tl.pack(fill="x", padx=P(6), pady=(P(6), P(8)))
        self.tile = {}
        for i, (key, cap) in enumerate((("it", "Iteración"), ("H", "H"), ("M", "M"), ("eta", "η"), ("cont", "Continuidad"))):
            tl.columnconfigure(i, weight=1, uniform="t")
            f = tk.Frame(tl, bg=C["card"])
            f.grid(row=0, column=i, sticky="ew")
            tk.Label(f, text=cap, bg=C["card"], fg=C["mute"], font=(FONT[0], 8)).pack()
            v = tk.Label(f, text="—", bg=C["card"], fg=C["ink"], font=(FONT[0], 10, "bold"))
            v.pack()
            self.tile[key] = v
        root.bind("<Configure>", self.on_resize)

        # pie: entrada y botones
        foot = tk.Frame(root, bg=C["bg"])
        foot.pack(side="bottom", fill="x")
        self.live = tk.Label(foot, text="", anchor="w", bg=C["bg"], fg=C["blue"], font=(FONT[0], 9))
        self.live.pack(fill="x", padx=P(12))
        box = tk.Frame(foot, bg=C["card"], highlightthickness=1, highlightbackground="#cfd4dc", highlightcolor=C["blue"])
        box.pack(fill="x", padx=P(10), pady=(P(2), P(6)))
        self.entry = tk.Text(box, height=3, wrap="word", font=FONT, relief="flat", bd=0, padx=P(8), pady=P(6), bg=C["card"], fg=C["ink"],
                             insertbackground=C["ink"])
        self.entry.pack(fill="x")
        row = tk.Frame(foot, bg=C["bg"])
        row.pack(fill="x", padx=P(10), pady=(0, P(10)))
        self.btn = {}
        for key, txt, cmd, side in (("enviar", "Enviar", self.on_send, "left"), ("detener", "Detener", self.on_stop, "left"),
                                    ("apagar", "Apagar solver", self.on_off, "right"), ("gui", "Abrir GUI", self.on_gui, "right"),
                                    ("nueva", "Nueva", self.on_new, "right")):
            b = self.button(row, txt, cmd, key)
            b.pack(side=side, padx=(0, P(5)) if side == "left" else (P(5), 0))
            self.btn[key] = b

        # chat
        self.hist = tk.Text(root, wrap="word", state="disabled", font=FONT, bg=C["card"], fg=C["ink"], relief="flat", bd=0,
                            padx=P(12), pady=P(8), spacing3=P(3), highlightthickness=1, highlightbackground=C["line"], cursor="arrow")
        from tkinter import ttk
        sb = ttk.Scrollbar(root, command=self.hist.yview)
        self.hist.config(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y", pady=(P(6), 0))
        self.hist.pack(side="left", fill="both", expand=True, padx=(P(8), 0), pady=(P(6), 0))
        h = self.hist
        h.tag_config("user", background=C["user"], justify="right", lmargin1=P(60), lmargin2=P(60), rmargin=P(10), spacing1=P(6), spacing3=P(6))
        h.tag_config("unl", justify="right", lmargin1=P(60), lmargin2=P(60), rmargin=P(10), spacing1=P(6), spacing3=P(6))
        h.tag_config("agent", lmargin1=P(4), lmargin2=P(4), rmargin=P(30))
        h.tag_config("bold", font=(FONT[0], FONT[1], "bold"))
        h.tag_config("code", font=MONO, background="#eef0f3")
        h.tag_config("table", font=MONO, background="#f3f5f8")
        h.tag_config("tool", foreground="#555555", font=(FONT[0], 9, "italic"), lmargin1=P(4), lmargin2=P(4))
        h.tag_config("ok", foreground="#1a7f37")
        h.tag_config("bad", foreground="#c62828")
        h.tag_config("err", foreground="#c62828", font=(FONT[0], FONT[1], "bold"))
        h.tag_config("sys", foreground="#777777", font=(FONT[0], 9, "italic"))
        h.tag_config("stat", foreground="#888888", font=(FONT[0], 9), spacing1=2, spacing3=8)
        h.tag_config("queued", foreground="#b45309", font=(FONT[0], 9, "italic"))

        self.entry.bind("<Return>", lambda e: (self.on_send(), "break")[1])
        self.entry.bind("<Shift-Return>", lambda e: None)
        self.entry.bind("<Up>", self.on_up)
        root.bind("<Control-t>", lambda e: (self.top.set(not self.top.get()), self.toggle_top()))
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        if self.sid:
            self.say("Continúo la conversación anterior.", "sys")
        else:
            self.show_empty()
        self.entry.focus_set()
        self.pump()
        self.refresh()
        self.refresh_progress()

    def px(self, n) -> int:
        return int(round(n * self.sc))

    def button(self, parent, text, cmd, key, small=False):
        bg, fg, act = BTN[key]
        b = self.tk.Button(parent, text=text, command=cmd, font=(FONT[0], 9 if small else 10, "bold" if key == "enviar" else "normal"),
                           bg=bg, fg=fg, activebackground=act, activeforeground=fg, relief="flat", bd=0, cursor="hand2",
                           padx=self.px(10 if small else 12), pady=self.px(4 if small else 6), highlightthickness=0)
        b._key, b._text = key, text
        return b

    def on_resize(self, e):
        if e.widget is self.root:
            w = max(self.root.winfo_width() - self.px(40), 100)
            self.warn.config(wraplength=w)
            self.vig_lbl.config(wraplength=max(w - self.px(90), 100))
            self.hrs_lbl.config(wraplength=max(w - self.px(90), 100))

    # --- estado vacío con sugerencias
    def show_empty(self):
        tk, P = self.tk, self.px
        f = tk.Frame(self.hist, bg=C["card"])
        tk.Label(f, text="¿Qué quieres hacer?", bg=C["card"], fg=C["ink"], font=(FONT[0], 14, "bold")).pack()
        tk.Label(f, text="Escribe abajo o elige una opción", bg=C["card"], fg=C["mute"], font=(FONT[0], 10)).pack(pady=(P(2), P(12)))
        self.sug = []
        for txt, kind, arg in SUGERENCIAS:
            b = tk.Button(f, text=txt, font=FONT, bg="#eff4ff", fg="#1e40af", activebackground="#dbeafe", relief="flat", bd=0,
                          cursor="hand2", padx=P(14), pady=P(8), width=28,
                          command=(lambda a=arg: self.on_send(a)) if kind == "msg" else (lambda a=arg: self.direct(a)))
            b.pack(pady=P(4))
            self.sug.append(b)
        f.place(relx=0.5, rely=0.42, anchor="center")
        self.empty = f

    def hide_empty(self):
        if self.empty is not None:
            self.empty.destroy()
            self.empty = None

    # --- texto
    def toggle_top(self):
        self.root.attributes("-topmost", bool(self.top.get()))

    def put(self, text, *tags):
        self.hide_empty()
        self.hist.config(state="normal")
        self.hist.insert("end", redact(text), tags)
        self.hist.config(state="disabled")
        self.hist.see("end")

    def say(self, text, tag):
        self.put(text + "\n", tag)

    def agent_text(self, text):
        for line in text.split("\n"):
            if re.fullmatch(r"\s*\|[-:| ]+\|\s*", line):  # separador de tabla markdown: no se pinta
                continue
            if line.lstrip().startswith("|"):
                self.put(line + "\n", "agent", "table")
                continue
            for part in INLINE.split(line):
                if part.startswith("**") and part.endswith("**") and len(part) > 4:
                    self.put(part[2:-2], "agent", "bold")
                elif part.startswith("`") and part.endswith("`") and len(part) > 2:
                    self.put(part[1:-1], "agent", "code")
                else:
                    self.put(part, "agent")
            self.put("\n", "agent")

    # --- acciones
    def on_send(self, msg=None):
        msg = (msg if isinstance(msg, str) else self.entry.get("1.0", "end")).strip()
        if not msg:
            return
        self.last_msg = msg
        self.entry.delete("1.0", "end")
        if self.runner.busy:  # cola: se envía con --resume al terminar el paso en curso
            self.put(msg, "user")
            self.npend += 1
            mk = f"q_{self.npend}"
            self.put("  · en cola", "queued")
            self.hist.mark_set(mk, "end-1c")
            self.hist.mark_gravity(mk, "left")
            self.put("\n")
            self.pending.append((mk, msg))
            return
        self.put(msg, "user")
        self.put("\n", "unl")
        self.start(msg)

    def on_modo(self, save=True):
        fl = modo_flags(self.modo.get())
        self.modo_info.config(text=f"{fl[1]} · esfuerzo {fl[3]}")
        if save:
            save_estado(modo=self.modo.get())

    def start(self, msg):
        self.turn_t0, self.tool_s, self.n_tools = time.time(), 0.0, 0
        self.auto_cancel = False
        self.runner.send(msg, self.sid, modo=self.modo.get())

    def flush_queue(self):
        if not self.pending:
            return
        items, self.pending = self.pending, []
        self.hist.config(state="normal")
        for mk, _ in items:  # quita la marca «en cola»
            r = self.hist.tag_ranges("queued")
            for i in range(0, len(r), 2):
                if self.hist.compare(r[i + 1], "==", mk):
                    self.hist.delete(r[i], r[i + 1])
                    break
        self.hist.config(state="disabled")
        self.say("▸ enviando tus mensajes pendientes…", "sys")
        self.start(PREFIJO_COLA + "\n".join(m for _, m in items))

    def on_up(self, e):
        if not self.entry.get("1.0", "end").strip() and self.last_msg:
            self.entry.insert("1.0", self.last_msg)
            return "break"

    # --- acciones directas: mcp_server.py --cli en un hilo, resultado en el chat, sin turno de modelo
    def direct(self, tool, args=None):
        if tool in self.cli_active:
            if tool == "server_stop":
                self.say("Ya está apagando el solver: espera, guardar el .sim puede tardar minutos.", "sys")
            return
        self.cli_active.add(tool)
        self.cli_t0[tool] = time.time()
        self.say(f"▸ {LABEL_CLI.get(tool, tool)}…" + (" (puede tardar minutos; no se interrumpe)" if tool == "server_stop" else ""), "sys")
        repo = self.repo or REPO
        tmo = None if tool == "server_stop" else 90  # matar el apagado a medias deja .server.json y el solver cobrando

        def work():
            self.q.put(("cli", tool, run_cli(tool, args, repo, tmo)))
        threading.Thread(target=work, daemon=True).start()

    def on_gui(self):
        self.direct("gui_attach")

    def on_off_now(self):
        self.direct("server_stop")

    def on_cancel(self):
        self.refresh_progress(reschedule=False)
        if self.progress_active() and not self.prog["iterando"]:  # malla/importación: el solver no las interrumpe
            self.explain_no_cancel()
            return
        self.direct("cancel")

    def explain_no_cancel(self):
        self.say(no_cancel_text(self.prog), "err")
        self.stop_btn = self.chat_button("Apagar solver", lambda: self.twice("nocancel", self.stop_btn, "Apagar solver", self.on_off_now), "apagar")
        self.put("  ")
        self.wait_btn = self.chat_button("Parar al terminar la malla", self.on_cancel_wait, "detener")
        self.put("\n")

    def on_cancel_wait(self):
        """Pide cancelar aunque la fase no sea interrumpible: el MCP pone el ABORT y la macro para antes de iterar."""
        self.direct("cancel")

    def macro_en_marcha(self) -> bool:
        """¿Hay una macro en el solver? progreso.json sin terminar (aunque esté viejo: dueño muerto) o .inject.lock vigente."""
        pr = self.prog
        return bool((pr and (pr["active"] or pr["abierto"])) or lock_info(self.repo, self.sdir))

    def watch_orphan(self, pr) -> bool:
        """Macro sin dueño: progreso.json sin terminar y sin actualizar (el MCP que la lanzó murió: panel cerrado, CLI matada)
        con el solver vivo. Sigue corriendo y cobrando; nadie más lo avisaría. Una vez por macro: aviso + «Parar la macro»."""
        if (not pr or pr["active"] or not pr["abierto"] or self.runner.busy or self.cancel_pending or "cancel" in self.cli_active
                or self.closed or not read_status(self.repo, self.sdir)["solver"]):
            return False
        key = (pr["caso"], pr["fase"])
        if self.orphan_key != key:
            self.orphan_key = key
            self.say(f"Hay una macro sin dueño en el solver ({pr['caso'] or '?'}, fase «{pr['fase']}»): nadie la vigila y la "
                     "licencia sigue cobrando. Puedes pararla (sin apagar) o apagar el solver.", "err")
            self.cancel_btn = self.chat_button("Parar la macro", lambda: self.direct("cancel"), "detener")
            self.put("  ")
            self.stop_btn = self.chat_button("Apagar solver", lambda: self.twice("huerfana", self.stop_btn, "Apagar solver", self.on_off_now), "apagar")
            self.put("\n")
        return True

    def watch_pending(self):
        """Cancel «pendiente»: el estado sigue visible hasta que progreso.json diga terminado; avisa al terminar."""
        if not self.cancel_pending or self.closed:
            return
        pr = self.prog
        if pr is None or pr["terminado"] or (not pr["abierto"] and not lock_info(self.repo, self.sdir)):
            self.cancel_pending = False
            self.say("✓ La macro terminó (paró por el cancel pendiente)." if pr and pr["terminado"]
                     else "La macro ya no figura en curso. Revisa el estado del solver.", "ok")

    def on_off(self):
        self.twice("apagar", self.btn["apagar"], "Apagar solver", self.on_off_now)

    # --- Detener
    def on_stop(self):
        if self.runner.busy:
            n = len(self.pending)
            self.pending.clear()
            self.refresh_progress(reschedule=False)
            en_marcha = self.macro_en_marcha()  # medido ANTES de matar la CLI (y su MCP, dueño del turno)
            fase = (self.prog or {}).get("fase") or "macro"
            self.runner.stop()
            self.say("Detenido." + (f" Descarté {n} mensaje(s) en cola." if n else ""), "sys")
            if en_marcha:  # sin CLI nadie vigila la macro (en cualquier fase): se cancela para que no siga (y cobrando)
                self.auto_cancel = True
                self.say(f"Había una macro en marcha (fase «{fase}»): pido cancelarla.", "sys")
                self.direct("cancel")
            self.root.after(self.stop_delay_ms, self.check_after_stop)
        elif self.progress_active():
            self.check_after_stop()

    stop_delay_ms = 1200

    def progress_active(self) -> bool:
        return bool(self.prog and self.prog["active"])

    def chat_button(self, text, cmd, key):
        b = self.button(self.hist, text, cmd, key, small=True)
        self.hist.config(state="normal")
        self.hist.window_create("end", window=b, padx=self.px(4), pady=self.px(2))
        self.hist.config(state="disabled")
        return b

    def check_after_stop(self):
        self.refresh_progress(reschedule=False)
        w, act = stop_warning(self.repo, self.sdir), self.progress_active()
        if (not w and not act) or self.closed:
            return
        if self.auto_cancel:  # el cancel ya está pedido: su resultado (paró / pendiente / no paró) dice si sigue cobrando
            return
        if w:
            self.say(w, "err")
        if act and self.auto_cancel:
            pass  # el cancel ya está pedido: su resultado sale en el chat
        elif act and not self.prog["iterando"]:
            self.explain_no_cancel()
        elif act:
            self.say("Hay iteraciones en marcha en el solver" + (f" ({self.prog['caso']})" if self.prog["caso"] else "") +
                     ": «Parar iteraciones» las cancela sin apagar nada.", "sys" if w else "err")
            self.cancel_btn = self.chat_button("Parar iteraciones", self.on_cancel, "detener")
        if w:
            self.stop_btn = self.chat_button("Apagar solver", self.on_off_now, "apagar")
        self.put("\n")

    def twice(self, key, button, normal, action):
        """Confirmación dentro del panel: el primer clic pide confirmar; el segundo (en 4 s) ejecuta."""
        now = time.time()
        bg, fg, act = BTN[button._key]

        def restore():
            button.config(text=normal, bg=bg, fg=fg, activebackground=act)
        if now - self.confirm.get(key, 0) < 4:
            self.confirm.pop(key, None)
            restore()
            action()
            return
        self.confirm[key] = now
        button.config(text="¿Seguro? Otro clic", bg="#dc2626", fg="#ffffff", activebackground="#b91c1c")

        def reset():
            if key in self.confirm and time.time() - self.confirm[key] >= 3.9:
                self.confirm.pop(key, None)
                restore()
        self.root.after(4000, reset)

    def on_new(self):
        def go():
            self.runner.stop()
            self.sid = None
            self.pending.clear()
            save_sid(None)
            self.hist.config(state="normal")
            self.hist.delete("1.0", "end")
            self.hist.config(state="disabled")
            self.show_empty()
        self.twice("nueva", self.btn["nueva"], "Nueva", go)

    # --- cerrar: si el solver está vivo, pregunta dentro del panel
    def status_now(self):
        return read_status(self.repo, self.sdir)

    def on_close(self):
        if self.closing or self.dlg:
            return
        if self.status_now()["solver"]:
            self.show_dlg()
        else:
            self.finish()

    def finish(self):
        self.runner.stop()
        self.closed = True
        for a in self.aft.values():
            try:
                self.root.after_cancel(a)
            except Exception:
                pass
        self.root.destroy()

    def show_dlg(self):
        tk, P = self.tk, self.px
        f = tk.Frame(self.root, bg="#ffffff", highlightthickness=1, highlightbackground="#9ca3af", padx=P(18), pady=P(14))
        tk.Label(f, text="¿Apagar el solver antes de salir?", bg="#ffffff", font=(FONT[0], 11, "bold")).pack()
        tk.Label(f, text="Cobra por hora mientras esté encendido", bg="#ffffff", fg=C["mute"], font=(FONT[0], 9)).pack(pady=(0, P(10)))
        row = tk.Frame(f, bg="#ffffff")
        row.pack()
        self.dlg_btn = {}
        for key, txt, st in (("si", "Sí", "enviar"), ("no", "No", "nueva"), ("cancelar", "Cancelar", "nueva")):
            b = self.button(row, txt, lambda k=key: self.dlg_answer(k), st)
            b.config(width=8)
            b.pack(side="left", padx=3)
            self.dlg_btn[key] = b
        f.place(relx=0.5, rely=0.45, anchor="center")
        f.lift()
        self.dlg = f

    def dlg_answer(self, a):
        if self.dlg:
            self.dlg.destroy()
            self.dlg = None
        if a == "cancelar":
            return
        if a == "no":
            self.finish()
            return
        self.closing, self.close_t0, self.close_wait = True, time.time(), False
        self.say("Apagando el solver antes de salir… (máx. %d s)" % self.close_timeout, "sys")
        self.on_off_now()  # directo: sin pasar por el modelo
        self.root.after(300, self.close_poll)

    def close_poll(self):
        if self.closed:
            return
        if not self.cli_active and not self.status_now()["solver"]:
            self.finish()
        elif time.time() - self.close_t0 > self.close_timeout and "server_stop" in self.cli_active:
            if not self.close_wait:  # el apagado sigue en curso: no se da por perdido ni se interrumpe
                self.close_wait = True
                self.say("Sigue guardando y apagando el solver (puede tardar minutos). Espero; no cierro todavía.", "sys")
            self.root.after(300, self.close_poll)
        elif time.time() - self.close_t0 > self.close_timeout:
            self.closing = False
            self.say("El solver no se apagó a tiempo: sigue vivo. No cerré el panel; usa «Apagar solver» o ciérralo con «No».", "err")
        else:
            self.root.after(300, self.close_poll)

    # --- eventos
    def handle(self, ev):
        k = ev[0]
        if k == "init":
            if ev[1]:
                self.sid = ev[1]
                save_sid(self.sid)
        elif k == "text":
            self.agent_text(ev[1])
        elif k == "tool":
            self.n_tools += 1
            self.put(f"▶ {ev[2]} … ", "tool")
            self.hist.mark_set("m_" + ev[1], "end-1c")
            self.hist.mark_gravity("m_" + ev[1], "left")
            self.put("\n")
            self.tools[ev[1]] = (ev[2], time.time())
        elif k == "tool_result":
            if ev[1] in self.tools:
                _, t0 = self.tools.pop(ev[1])
                self.tool_s += time.time() - t0
                self.hist.config(state="normal")
                self.hist.insert("m_" + ev[1], f"{round(time.time() - t0)} s ", "tool", "✗" if ev[3] else "✓", "bad" if ev[3] else "ok")
                self.hist.config(state="disabled")
                if ev[3] and ev[2]:
                    self.put("   " + ev[2][:200] + "\n", "bad")
        elif k == "result":
            if ev[1]:
                self.sid = ev[1]
                save_sid(self.sid)
            if ev[3]:
                self.say("La conversación terminó con error.", "err")
            if self.turn_t0:
                tot = time.time() - self.turn_t0
                nt = ev[4] if len(ev) > 4 and ev[4] else "?"
                self.say(f"⏱ {fmt_s(tot)} en total · {nt} turnos del modelo · {fmt_s(self.tool_s)} en herramientas "
                         f"({self.n_tools}) · {fmt_s(max(tot - self.tool_s, 0))} pensando", "stat")
                self.turn_t0 = 0.0
        elif k == "error":
            self.say(ev[1], "err")
        elif k == "aviso":
            self.say("⚠ " + ev[1], "sys")
        elif k == "raw":
            self.say(ev[1], "sys")
        elif k == "session_lost":
            self.lost = True
        elif k == "cli":
            self.cli_active.discard(ev[1])
            self.cli_t0.pop(ev[1], None)
            txt, ok = fmt_cli(ev[1], ev[2])
            self.say(("✓ " if ok else "✗ ") + txt, "ok" if ok else "err")
            pend = ev[1] == "cancel" and (bool(ev[2].get("pendiente")) or ev[2].get("cancelled") == "pendiente")
            if pend:
                self.cancel_pending = True
                self.say("Sigue en curso: avisaré cuando termine. Si no quieres esperar, apaga el solver (pierdes lo no guardado).", "sys")
            if ev[1] == "cancel" and (self.auto_cancel or pend):
                self.auto_cancel = False
                if pend or not ev[2].get("cancelled"):  # sin cancel (o pendiente), la macro sigue en el solver cobrando
                    if not pend:
                        self.say("La macro puede seguir iterando en el solver y cobrando. Apágalo si no la necesitas.", "err")
                    self.stop_btn = self.chat_button("Apagar solver", lambda: self.twice("autocancel", self.stop_btn, "Apagar solver", self.on_off_now), "apagar")
                    self.put("\n")
            if not self.closed:
                self.refresh()
        elif k == "done":
            self.tools.clear()
            self.live.config(text="")
            if self.lost and self.runner.msg:
                self.lost = False
                self.sid = None
                save_sid(None)
                self.say("⚠ La sesión anterior caducó: empiezo una conversación nueva y reenvío tu mensaje.", "sys")
                self.start(self.runner.msg)
                return
            if ev[2] or ev[1] != 0:
                self.pending.clear()
            self.flush_queue()

    def pump(self):
        try:
            while True:
                self.handle(self.q.get_nowait())
        except queue.Empty:
            pass
        self.tick()
        self.aft["pump"] = self.root.after(100, self.pump)

    def tick(self):
        if "server_stop" in self.cli_active:  # el apagado guarda el .sim: que se vea que sigue vivo
            t0 = self.cli_t0.get("server_stop", time.time())
            self.live.config(text=f"▶ Guardando y apagando el solver … {fmt_s(time.time() - t0)} (no lo interrumpas)", fg=C["amber"])
        elif self.tools:
            label, t0 = max(self.tools.values(), key=lambda v: v[1])
            self.live.config(text=f"▶ {label} … {fmt_s(time.time() - t0)}", fg=C["blue"])
        elif self.runner.busy:
            self.live.config(text=f"… la IA piensa {fmt_s(time.time() - self.turn_t0)}" if self.turn_t0 else "", fg=C["mute"])
        else:
            self.live.config(text="")
        if self.bar_frac is None and self.card.winfo_manager() == "pack":
            self.draw_bar()

    def draw_bar(self):
        c, w, h = self.bar, self.bar.winfo_width(), self.px(8)
        c.delete("all")
        c.create_rectangle(0, 0, w, h, fill="#e5e7eb", width=0)
        if self.bar_frac is not None:
            c.create_rectangle(0, 0, w * self.bar_frac, h, fill=C["blue"], width=0)
        elif w > 10:  # fase sin porcentaje: bloque que se desliza
            x = ((time.time() * 0.6) % 1.0) * (w + w * 0.3) - w * 0.3
            c.create_rectangle(max(x, 0), 0, min(x + w * 0.3, w), h, fill=C["blue"], width=0)

    def relayout(self, show: tuple):
        if show == self.vis:
            return
        self.vis = show
        for w in (self.vig, self.hrs, self.warn, self.card):
            w.pack_forget()
        for on, w in zip(show, (self.vig, self.hrs, self.warn, self.card)):
            if on:
                w.pack(fill="x", pady=(self.px(6), 0))

    def refresh_progress(self, reschedule=True):
        pr = self.prog = read_progress(self.repo, self.sdir)
        self.watch_pending()
        orphan = self.watch_orphan(pr)
        show = bool(pr and (pr["active"] or ((self.cancel_pending or orphan) and pr["abierto"])))
        if show:
            self.p_title.config(text=f"● {pr['caso'] or 'Cálculo'} · {pr['fase'].capitalize()}")
            self.p_rest.config(text=fresta(pr["resta"]))
            self.bar_frac = pr["frac"]
            it = "—" if pr["it"] is None else (f"{int(pr['it'])}/{int(pr['tot'])}" if pr["tot"] else f"{int(pr['it'])}")
            eta = pr["eta"]
            self.tile["it"].config(text=it)
            self.tile["H"].config(text=fnum(pr["H"], 1, " m"))
            self.tile["M"].config(text=fnum(pr["M"], 2, " N·m"))
            self.tile["eta"].config(text=fnum(None if eta is None else (eta * 100 if eta <= 1.5 else eta), 1, " %"))
            self.tile["cont"].config(text=fsci(pr["cont"]))
        self.relayout((self.vis[0], self.vis[1], self.vis[2], show))
        if show:
            self.draw_bar()
        if reschedule and not self.closed:
            if "prog" in self.aft:
                self.root.after_cancel(self.aft["prog"])
            self.aft["prog"] = self.root.after(2000, self.refresh_progress)

    def refresh(self):
        s = self.status_now()
        off_bg, off_fg = CHIPS["off"]

        def chip(key, on, on_text, off_text):
            bg, fg = CHIPS[key] if on else (off_bg, off_fg)
            self.chip[key].config(text=("● " if on else "○ ") + (on_text if on else off_text), bg=bg, fg=fg)
        chip("solver", s["solver"], "Solver encendido", "Solver apagado")
        chip("gui", s["gui"], "GUI conectada", "GUI no conectada")
        chip("agent", self.runner.busy or bool(s["busy"]) or bool(self.cli_active), "Agente trabajando", "Agente libre")
        self.sub.config(text=(f"{s['caso']}" + (f" · {s['idle_min']} min inactivo" if s["idle_min"] is not None else "")) if s["solver"]
                        else "Sin solver: se arranca cuando haga falta")
        if s["idle_warning"]:
            self.warn.config(text="⚠ " + s["idle_warning"])
        lim = aviso_horas()
        hrs = bool(s["solver"] and s["horas"] is not None and lim > 0 and s["horas"] >= lim)
        if hrs:
            self.hrs_lbl.config(text=horas_text(s["horas"], s["gui"]))
        self.relayout((bool(s["vigil_down"]), hrs, bool(s["idle_warning"]), self.vis[3]))
        if "refresh" in self.aft:
            self.root.after_cancel(self.aft["refresh"])
        self.aft["refresh"] = self.root.after(3000, self.refresh)


def demo_setup(p: "Panel", sd: Path) -> None:
    """--demo: estado y mensajes de ejemplo, sin CLI ni solver."""
    from datetime import datetime, timedelta
    t0 = time.time()
    vigil, vacio = "--vigil" in sys.argv, "--empty" in sys.argv  # variantes para revisar el aspecto

    def write():
        if p.closed:
            return
        n = 120 + int((time.time() - t0) / 2)
        if vacio:
            (sd / "progreso.json").unlink(missing_ok=True)
        else:
            (sd / "progreso.json").write_text(json.dumps({"caso": "BombaRapida", "fase": "iterando", "iteracion": n, "iteraciones": 250,
                                                          "H": 16.74, "M": 3.12, "eta": 0.887, "continuidad": 2.3e-4, "segundos": n * 0.8,
                                                          "restante_s": (250 - n) * 0.8}), encoding="utf-8")
        chk = datetime.now() - timedelta(minutes=5 if vigil else 0)
        (sd / ".server.json").write_text(json.dumps({"pid": os.getpid(), "sim": "C:/x/BombaRapida.sim", "gui": True, "idle_min": 0,
                                                     "idle_checked": chk.isoformat(timespec="seconds"),
                                                     "idle_warning": "35 min sin actividad: ¿apagar el solver?" if vigil else None}),
                                                     encoding="utf-8")
        p.aft["demo"] = p.root.after(2000, write)
    write()
    p.refresh()
    p.refresh_progress(reschedule=False)
    if vacio:
        return
    p.put("Simula la bomba de CFturbo con 250 iteraciones", "user")
    p.put("\n", "unl")
    p.agent_text("Importo la geometría y monto la física de **BombaRapida**.")
    p.put("▶ Montando la bomba (importar, malla, física) «BombaRapida» … ", "tool")
    p.put("96 s ", "tool", "✓", "ok")
    p.put("\n")
    p.agent_text("Malla lista: 412 000 celdas, y+ medio 38. Itero hasta que se estabilice.")
    p.put("▶ Iterando 250 iteraciones … ", "tool")
    p.put("\n")
    p.put("¿Cuál es la altura ahora?", "user")
    p.put("\n", "unl")
    p.agent_text("Vas en la iteración 120 de 250:\n| magnitud | valor |\n|---|---|\n| H | 16,7 m |\n| η | 88,7 % |\n"
                 "Se estabiliza: faltan unos 2 min. Todavía **no** apago el solver.")
    p.say("⏱ 1 min 52 s en total · 3 turnos del modelo · 1 min 40 s en herramientas (2) · 12 s pensando", "stat")


def main():
    global ESTADO
    dpi_aware()
    import tkinter as tk
    root = tk.Tk()
    if "--demo" in sys.argv:
        ESTADO = Path(tempfile.mkdtemp(prefix="panel-demo-")) / ".estado.json"
        sd = Path(tempfile.mkdtemp(prefix="panel-demo-"))
        os.environ["COPILOTO_WORK"] = str(sd)
        p = Panel(root, repo=REPO, sdir=sd)
        p.sid = None
        demo_setup(p, sd)
    else:
        Panel(root)
    root.mainloop()


# ---------- selftest ----------
def selftest():
    global ESTADO, REPO
    ok_all = True
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    def check(c, msg):
        nonlocal ok_all
        ok_all &= bool(c)
        print(("OK   " if c else "FALLA"), msg)

    def run_new_checks(p, root, spin, trepo, tmp, tmp_sd, sj):
        """Ronda 4: chips, tarjeta de progreso, acciones directas, vigilante caído, sesión caducada, estado vacío."""
        from datetime import datetime, timedelta
        base = {"pid": os.getpid(), "sim": "C:/x/Nueva.sim", "gui": True}
        check(isinstance(dpi_aware(), bool) and p.sc >= 1.0, "DPI-aware: dpi_aware() (SetProcessDpiAwareness) no revienta y hay factor de escala")
        # chips de estado
        p.refresh()
        c = p.chip
        check(c["solver"].cget("text") == "● Solver encendido" and c["solver"].cget("bg") == CHIPS["solver"][0]
              and c["gui"].cget("bg") == CHIPS["gui"][0] and c["agent"].cget("text") == "○ Agente libre", "chips: solver verde, GUI azul, agente libre gris")
        p.cli_active.add("x")
        p.refresh()
        check(c["agent"].cget("bg") == CHIPS["agent"][0] and "trabajando" in c["agent"].cget("text"), "chip del agente en ámbar mientras trabaja")
        p.cli_active.discard("x")
        sj.write_text(json.dumps({**base, "pid": 999999, "gui": True}))
        p.refresh()
        check(c["solver"].cget("text") == "○ Solver apagado" and c["solver"].cget("bg") == CHIPS["off"][0] and c["gui"].cget("text") == "○ GUI no conectada",
              "chips: solver apagado y GUI en gris")
        sj.write_text(json.dumps(base))
        # tarjeta de progreso
        pj = tmp_sd / "progreso.json"
        pj.unlink(missing_ok=True)
        p.refresh_progress(reschedule=False)
        check(read_progress(trepo, tmp_sd) is None and p.card.winfo_manager() == "", "sin progreso.json la tarjeta está oculta")
        pj.write_text(json.dumps({"caso": "BombaX", "fase": "iterando", "iteracion": 100, "iteraciones": 250, "H": 16.74, "M": 3.1,
                                  "eta": 0.887, "continuidad": 2.3e-4, "segundos": 80, "restante_s": 180}))
        p.refresh_progress(reschedule=False)
        check(p.card.winfo_manager() == "pack" and "BombaX" in p.p_title.cget("text") and "Iterando" in p.p_title.cget("text")
              and p.tile["it"].cget("text") == "100/250" and p.tile["H"].cget("text") == "16,7 m" and p.tile["eta"].cget("text") == "88,7 %"
              and p.tile["cont"].cget("text") == "2,3e-04" and p.p_rest.cget("text") == "faltan ~3 min" and abs(p.bar_frac - 0.4) < 1e-9,
              "tarjeta de progreso: caso, fase, iteración, H, η, continuidad, «faltan ~3 min» y barra al 40 %")
        pj.write_text(json.dumps({"caso": "BombaX", "fase": "malla"}))
        p.refresh_progress(reschedule=False)
        check(p.card.winfo_manager() == "pack" and p.bar_frac is None and p.tile["it"].cget("text") == "—", "fase sin iteraciones: barra indeterminada y «—»")
        pj.write_text(json.dumps({"caso": "BombaX", "fase": "terminado", "iteracion": 250, "iteraciones": 250}))
        p.refresh_progress(reschedule=False)
        check(p.card.winfo_manager() == "", "fase «terminado»: la tarjeta se oculta")
        pj.write_text(json.dumps({"caso": "BombaX", "fase": "iterando", "iteracion": 5, "iteraciones": 250}))
        old = time.time() - 300
        os.utime(pj, (old, old))
        p.refresh_progress(reschedule=False)
        viva = read_status(trepo, tmp_sd)["solver"]  # con el solver vivo, abandonado = macro sin dueño: tarjeta y aviso (no se oculta)
        check(read_progress(trepo, tmp_sd)["active"] is False and (p.card.winfo_manager() == "pack" and p.orphan_key is not None if viva
                                                                   else p.card.winfo_manager() == ""),
              "progreso.json abandonado (> 45 s): no «activo»; tarjeta oculta sin solver, o aviso de macro sin dueño con solver vivo")
        # vigilante caído
        sj.write_text(json.dumps({**base, "idle_checked": (datetime.now() - timedelta(minutes=5)).isoformat(timespec="seconds")}))
        p.refresh()
        check(p.vig.winfo_manager() == "pack" and "vigilante no responde" in p.vig_lbl.cget("text") and read_status(trepo, tmp_sd)["vigil_down"],
              "idle_checked > 2 min con solver vivo: banda roja «El vigilante no responde»")
        sj.write_text(json.dumps({**base, "idle_checked": datetime.now().isoformat(timespec="seconds"), "idle_warning": "x"}))
        p.refresh()
        check(p.vig.winfo_manager() == "" and p.warn.winfo_manager() == "pack", "idle_checked reciente: sin banda roja")
        sj.write_text(json.dumps(base))
        p.refresh()
        check(p.vig.winfo_manager() == "" and not read_status(trepo, tmp_sd)["vigil_down"], "sin idle_checked (vigilante apagado a propósito): sin banda roja")
        sj.write_text(json.dumps({**base, "idle_checked": (datetime.now() - timedelta(minutes=5)).isoformat(timespec="seconds"), "idle_warning": "aviso"}))
        pj.write_text(json.dumps({"caso": "B", "fase": "iterando", "iteracion": 1, "iteraciones": 9}))
        p.refresh()
        p.refresh_progress(reschedule=False)
        order = [("vig" if w is p.vig else "warn" if w is p.warn else "card") for w in p.mid.pack_slaves()]
        check(order == ["vig", "warn", "card"], f"orden de bandas: roja, ámbar, tarjeta ({order})")
        # acciones directas: sin turno de modelo
        log = Path(os.environ["FAKE_CLI_LOG"])
        n0, last = (len(log.read_text().splitlines()) if log.exists() else 0), p.last_msg
        p.vig_btn.invoke()
        check(p.vig_btn.cget("text").startswith("¿Seguro?") and not p.cli_active,
              "Apagar (banda roja) pide confirmación en el panel")
        p.vig_btn.invoke()
        check(spin(lambda: "✓ Apagar solver: solver apagado (falso)" in p.hist.get("1.0", "end"), 10), "Apagar (banda roja): server_stop directo y resultado en el chat")
        sj.write_text(json.dumps(base))
        p.refresh()
        p.on_gui()
        p.on_gui()  # el segundo clic con la primera en vuelo se ignora
        check(spin(lambda: "✓ Abrir GUI: GUI conectada (falsa)" in p.hist.get("1.0", "end"), 10), "Abrir GUI: gui_attach directo, resultado en el chat")
        spin(lambda: not p.cli_active, 5)
        p.on_off()
        check(p.btn["apagar"].cget("text").startswith("¿Seguro?"), "«Apagar solver»: primer clic pide confirmar")
        p.on_off()
        spin(lambda: not p.cli_active and p.btn["apagar"].cget("text") == "Apagar solver", 10)
        calls = [ln.split()[0] for ln in log.read_text().splitlines()[n0:]]
        check(calls.count("gui_attach") == 1 and calls.count("server_stop") == 2 and p.last_msg == last and not p.runner.busy, f"acciones directas no gastan turno del modelo (llamadas: {calls})")
        check(run_cli("herramienta_rara")["error"] == "herramienta desconocida" and "error" in run_cli("x", repo=tmp / "nada"),
              "run_cli: errores del MCP y del lanzamiento vuelven como {'error': …}")
        check(cli_cmd("cancel", {"a": 1})[-3:] == ["--cli", "cancel", '{"a": 1}'], "cli_cmd: mcp_server.py --cli herramienta json")
        txt, ok = fmt_cli("cancel", {"ok": True, "cancelado": True})
        check(ok and txt.startswith("Parar iteraciones:") and fmt_cli("gui_attach", {"error": "x"}) == ("Abrir GUI: x", False), "fmt_cli: texto del resultado")
        check(fmt_cli("cancel", {"cancelled": True, "iteracion": None, "fase": "mallar"})[0] == "Parar iteraciones: paró antes de iterar (al terminar la fase «mallar»)"
              and fmt_cli("cancel", {"cancelled": True, "iteracion": 104})[0] == "Parar iteraciones: paró en la iteración 104",
              "fmt_cli: cancel en la malla dice «paró antes de iterar» (visto con el solver real: «iteración None»)")
        # Detener con progreso activo: ofrece «Parar iteraciones»
        pj.write_text(json.dumps({"caso": "BombaX", "fase": "iterando", "iteracion": 10, "iteraciones": 250}))
        p.refresh_progress(reschedule=False)
        p.on_stop()
        check(hasattr(p, "cancel_btn") and p.cancel_btn.cget("text") == "Parar iteraciones" and "Parar iteraciones" in p.hist.get("1.0", "end"),
              "Detener con iteraciones en marcha ofrece «Parar iteraciones»")
        p.cancel_btn.invoke()
        check(spin(lambda: "✓ Parar iteraciones:" in p.hist.get("1.0", "end"), 10) and "cancel" in log.read_text(), "«Parar iteraciones» llama a --cli cancel")
        pj.write_text(json.dumps({"caso": "BombaX", "fase": "terminado"}))
        p.refresh_progress(reschedule=False)
        sj.write_text(json.dumps({**base}))
        p.refresh()
        # estado vacío y sugerencias
        p.on_new()
        check(p.btn["nueva"].cget("text").startswith("¿Seguro?"), "«Nueva» pide confirmar")
        p.on_new()
        check(p.empty is not None and len(p.sug) == 3 and [b.cget("text") for b in p.sug][0] == "Simular una bomba de CFturbo", "estado vacío con 3 sugerencias")
        p.sug[2].invoke()
        check(spin(lambda: "✓ Estado del solver:" in p.hist.get("1.0", "end"), 10) and p.empty is None, "sugerencia «Estado del solver»: acción directa y desaparece el estado vacío")
        # sesión caducada: --resume falla -> conversación nueva y reenvío automático
        os.environ["FAKE_PAUSE"] = "0.1"
        os.environ["PANEL_CLAUDE_BIN"] = str(fake_resume)
        save_sid("viejo")
        p.sid = "viejo"
        p.on_send("hola otra vez")
        check(spin(lambda: "caducó" in p.hist.get("1.0", "end") and "Importo la geometría" in p.hist.get("1.0", "end") and not p.runner.busy, 15),
              "sesión caducada: empieza conversación nueva y reenvía el mensaje")
        t = p.hist.get("1.0", "end")
        check(t.count("hola otra vez") == 1 and "terminó con" not in t and p.sid == "fake-0001" and load_sid() == "fake-0001",
              "sesión caducada: un solo mensaje del usuario, sin error, nueva sesión guardada")
        os.environ["FAKE_RESUME_MODE"] = "stdout"
        q3: queue.Queue = queue.Queue()
        r3 = Runner(q3, trepo)
        r3.send("x", "viejo")
        evs3 = []
        while True:
            evs3.append(q3.get(timeout=15))
            if evs3[-1][0] == "done":
                break
        k3 = [e[0] for e in evs3]
        check("session_lost" in k3 and "result" not in k3 and "error" not in k3, f"sesión caducada avisada por stdout (result de error): {k3}")
        os.environ.pop("FAKE_RESUME_MODE")
        os.environ["PANEL_CLAUDE_BIN"] = str(REPO / "tests" / "fake_claude.py")
        os.environ["FAKE_PAUSE"] = "0.4"

    def run_r5_checks(p, root, spin, trepo, tmp_sd, sj):
        """Ronda 5: apagado sin timeout, malla no cancelable, aviso por horas, cancel tras Detener, nada de claves en pantalla."""
        from datetime import datetime, timedelta
        base = {"pid": os.getpid(), "sim": "C:/x/Nueva.sim", "gui": True}
        pj, log = tmp_sd / "progreso.json", Path(os.environ["FAKE_CLI_LOG"])
        ncalls = lambda tool: [ln.split()[0] for ln in log.read_text().splitlines()].count(tool)
        txt = lambda: p.hist.get("1.0", "end-1c")
        spin(lambda: not p.cli_active, 10)
        # 9. server_stop nunca muere por timeout; se ve que sigue apagando
        seen: dict = {}
        real = globals()["run_cli"]
        globals()["run_cli"] = lambda tool, args=None, repo=None, timeout=120: seen.update({tool: timeout}) or {"ok": True, "msg": "x"}
        p.direct("server_stop")
        p.direct("cancel")
        spin(lambda: not p.cli_active, 5)
        globals()["run_cli"] = real
        check(seen.get("server_stop", 1) is None and seen.get("cancel") == 90, f"server_stop sin timeout (None) y el resto 90 s: {seen}")
        os.environ["FAKE_STOP_SLEEP"] = "2.5"
        p.direct("server_stop")
        check(spin(lambda: "no lo interrumpas" in p.live.cget("text") and "Guardando y apagando" in p.live.cget("text"), 5),
              "apagando: la barra muestra «Guardando y apagando… (no lo interrumpas)» con el tiempo")
        n0 = len(txt())
        p.direct("server_stop")
        check("Ya está apagando" in txt()[n0:], "segundo clic en Apagar mientras apaga: avisa, no lanza otro")
        check(spin(lambda: "✓ Apagar solver: solver apagado (falso)" in txt()[n0:] and not p.cli_active, 10), "el apagado largo termina y se informa (no se mató a medias)")
        os.environ.pop("FAKE_STOP_SLEEP")
        # cierre: el apagado sigue en curso pasado el máximo -> el panel espera, no lo da por perdido
        os.environ["FAKE_STOP_SLEEP"] = "2.0"
        sj.write_text(json.dumps(base))
        p.close_timeout = 0.5
        p.closing = False
        p.dlg = None
        p.on_close()
        p.dlg_btn["si"].invoke()
        check(spin(lambda: "Sigue guardando y apagando" in txt(), 6) and not p.closed, "cerrar: pasado el máximo con server_stop en curso espera (no dice «no se apagó»)")
        spin(lambda: not p.cli_active, 10)
        spin(lambda: not p.closing, 10)
        os.environ.pop("FAKE_STOP_SLEEP")
        p.close_timeout = 60.0
        p.closing = False
        sj.write_text(json.dumps(base))
        # 11. malla / importación no se cancelan: explica con fase y tiempo, ofrece apagar
        pj.write_text(json.dumps({"caso": "BombaX", "fase": "mallar", "falta_s": 40, "terminado": False}))
        p.refresh_progress(reschedule=False)
        pr = read_progress(trepo, tmp_sd)
        check(pr["resta"] == 40 and pr["iterando"] is False and pr["active"] and p.p_rest.cget("text") == "faltan ~40 s", "progreso.json real: falta_s alimenta «faltan ~40 s»")
        n_c, n0 = ncalls("cancel"), len(txt())
        p.on_cancel()
        t = txt()[n0:]
        check("«mallar»" in t and "no se puede parar" in t and "faltan ~40 s" in t and ncalls("cancel") == n_c and not p.cli_active,
              "«Parar iteraciones» en la malla: explica fase y tiempo restante y NO llama a cancel")
        n_s = ncalls("server_stop")
        p.stop_btn.invoke()
        check(p.stop_btn.cget("text").startswith("¿Seguro?") and ncalls("server_stop") == n_s and not p.cli_active,
              "la malla ofrece «Apagar solver» con confirmación dentro del panel")
        pj.write_text(json.dumps({"caso": "BombaX", "fase": "importar"}))
        p.refresh_progress(reschedule=False)
        p.on_stop()  # sin CLI en marcha: Detener también lo explica
        check(txt().count("no se puede parar") == 2 and "«importar»" in txt() and "sin tiempo estimado" in txt(), "importación: explica sin tiempo estimado")
        pj.write_text(json.dumps({"caso": "BombaX", "fase": "iterar", "terminado": True}))
        check(read_progress(trepo, tmp_sd)["active"] is False, "fase iterar con terminado=true: no hay iteración en curso")
        p.refresh_progress(reschedule=False)
        # 12. aviso por horas con la GUI abierta
        os.environ.pop("COPILOTO_AVISO_HORAS", None)
        hace = lambda h: (datetime.now() - timedelta(hours=h)).isoformat(timespec="seconds")
        sj.write_text(json.dumps({**base, "started": hace(1.0)}))
        p.refresh()
        check(aviso_horas() == 2.0 and p.hrs.winfo_manager() == "", "1 h de solver con límite 2 h: sin aviso")
        sj.write_text(json.dumps({**base, "started": hace(3.2)}))
        p.refresh()
        check(p.hrs.winfo_manager() == "pack" and "3,2 h" in p.hrs_lbl.cget("text") and "GUI abierta" in p.hrs_lbl.cget("text") and "cobra por hora" in p.hrs_lbl.cget("text"),
              "3,2 h con GUI abierta: banda «Solver encendido hace 3,2 h… con la GUI abierta no se apaga solo»")
        n_s = ncalls("server_stop")
        p.hrs_btn.invoke()
        check(p.hrs_btn.cget("text").startswith("¿Seguro?") and ncalls("server_stop") == n_s and not p.cli_active, "botón Apagar de la banda: confirma dentro del panel (sin diálogo)")
        p.hrs_btn.invoke()
        check(spin(lambda: ncalls("server_stop") == n_s + 1 and not p.cli_active, 10), "segundo clic: server_stop directo")
        os.environ["COPILOTO_AVISO_HORAS"] = "0,5"
        sj.write_text(json.dumps({**base, "started": hace(0.8)}))
        p.refresh()
        check(aviso_horas() == 0.5 and p.hrs.winfo_manager() == "pack", "COPILOTO_AVISO_HORAS=0,5 y 0,8 h: aviso")
        os.environ["COPILOTO_AVISO_HORAS"] = "0"
        p.refresh()
        check(p.hrs.winfo_manager() == "", "COPILOTO_AVISO_HORAS=0 lo desactiva")
        os.environ["COPILOTO_AVISO_HORAS"] = "mucho"
        check(aviso_horas() == 2.0, "valor inválido: vuelve a 2 h")
        os.environ.pop("COPILOTO_AVISO_HORAS")
        sj.write_text(json.dumps({**base, "pid": 999999, "started": hace(9)}))
        p.refresh()
        check(p.hrs.winfo_manager() == "", "solver muerto: sin aviso de horas")
        sj.write_text(json.dumps(base))
        p.refresh()
        # Detener (mata la CLI) con iteración en curso -> --cli cancel y resultado en el chat
        pj.write_text(json.dumps({"caso": "BombaX", "fase": "iterar", "iteracion": 20, "iteraciones": 250, "terminado": False}))
        p.refresh_progress(reschedule=False)
        n_c, n0 = ncalls("cancel"), len(txt())
        p.stop_delay_ms = 10
        p.on_send("sigue")
        check(spin(lambda: p.runner.busy, 5), "CLI en marcha para probar Detener")
        p.on_stop()
        check(spin(lambda: "✓ Parar iteraciones: paró en la iteración" in txt()[n0:], 10) and ncalls("cancel") == n_c + 1 and "Había una macro en marcha" in txt()[n0:],
              "Detener con iterar en curso: llama a --cli cancel y muestra el resultado")
        spin(lambda: not p.runner.busy and not p.cli_active, 10)
        check("Hay iteraciones en marcha" not in txt()[n0:], "tras el cancel automático no ofrece otro «Parar iteraciones»")
        # cancel que no para (macro huérfana): avisa del solver cobrando y ofrece apagar
        os.environ["FAKE_CANCEL_NO"] = "1"
        n_c, n0 = ncalls("cancel"), len(txt())
        pj.write_text(json.dumps({"caso": "BombaX", "fase": "iterar", "iteracion": 21, "iteraciones": 250}))
        p.refresh_progress(reschedule=False)
        p.on_send("otra vez")
        spin(lambda: p.runner.busy, 5)
        p.on_stop()
        check(spin(lambda: "puede seguir iterando" in txt()[n0:] and not p.cli_active, 10) and ncalls("cancel") == n_c + 1,
              "cancel sin efecto: avisa que la macro puede seguir iterando y cobrando")
        os.environ.pop("FAKE_CANCEL_NO")
        spin(lambda: not p.runner.busy, 10)
        # Detener con macro en CUALQUIER fase (mallar, importar, iterar): llama a cancel; terminado: no
        for fase in ({"fase": "mallar", "falta_s": 30}, {"fase": "importar"}):
            pj.write_text(json.dumps({"caso": "BombaX", **fase}))
            p.refresh_progress(reschedule=False)
            n_c, n0 = ncalls("cancel"), len(txt())
            p.on_send("x")
            spin(lambda: p.runner.busy, 5)
            p.on_stop()
            check(spin(lambda: ncalls("cancel") == n_c + 1 and not p.cli_active, 10) and f"fase «{fase['fase']}»" in txt()[n0:],
                  f"Detener con fase «{fase['fase']}»: llama a cancel y lo dice")
            spin(lambda: not p.runner.busy, 10)
        pj.write_text(json.dumps({"caso": "BombaX", "fase": "terminado", "terminado": True}))
        p.refresh_progress(reschedule=False)
        n_c = ncalls("cancel")
        p.on_send("x")
        spin(lambda: p.runner.busy, 5)
        p.on_stop()
        spin(lambda: not p.runner.busy, 10)
        time.sleep(0.3)
        root.update()
        check(ncalls("cancel") == n_c, "Detener con fase «terminado»: no llama a cancel")
        # progreso.json viejo (dueño muerto, > 45 s) sin terminar: también cancela
        pj.write_text(json.dumps({"caso": "BombaX", "fase": "mallar"}))
        os.utime(pj, (time.time() - 300, time.time() - 300))
        p.refresh_progress(reschedule=False)
        check(not p.progress_active() and p.prog["abierto"], "progreso viejo sin terminar: no «activo» pero sí «abierto»")
        # macro sin dueño con el solver vivo (panel reabierto, CLI muerta): aviso UNA vez, tarjeta y «Parar la macro» -> cancel
        if read_status(trepo, tmp_sd)["solver"]:
            p.orphan_key = None
            n0, n_c = len(txt()), ncalls("cancel")
            p.refresh_progress(reschedule=False)
            p.refresh_progress(reschedule=False)
            t_o = txt()[n0:]
            check(t_o.count("macro sin dueño") == 1 and p.card.winfo_manager() == "pack" and p.cancel_btn.cget("text") == "Parar la macro",
                  "macro sin dueño: un solo aviso, tarjeta visible y botón «Parar la macro»")
            p.cancel_btn.invoke()
            check(spin(lambda: ncalls("cancel") == n_c + 1 and not p.cli_active, 10), "«Parar la macro» llama a --cli cancel")
        else:
            check(False, "macro sin dueño: el selftest necesita el solver falso vivo en este punto")
        n_c = ncalls("cancel")
        p.on_send("x")
        spin(lambda: p.runner.busy, 5)
        p.on_stop()
        check(spin(lambda: ncalls("cancel") == n_c + 1 and not p.cli_active, 10), "Detener con progreso viejo sin terminar: llama a cancel")
        spin(lambda: not p.runner.busy, 10)
        # solo .inject.lock vigente (sin progreso.json): también cancela
        pj.unlink()
        lockf = tmp_sd / ".inject.lock"
        lockf.write_text(json.dumps({"pid": os.getpid(), "tool": "run_java", "started": time.time()}))
        p.refresh_progress(reschedule=False)
        n_c = ncalls("cancel")
        p.on_send("x")
        spin(lambda: p.runner.busy, 5)
        p.on_stop()
        check(spin(lambda: ncalls("cancel") == n_c + 1 and not p.cli_active, 10), "Detener con .inject.lock vigente y sin progreso: llama a cancel")
        spin(lambda: not p.runner.busy, 10)
        lockf.unlink()
        # cancel «pendiente»: muestra el texto, mantiene estado y botón de apagar y avisa al terminar
        os.environ["FAKE_CANCEL_PENDING"] = "1"
        pj.write_text(json.dumps({"caso": "BombaX", "fase": "mallar", "falta_s": 30}))
        p.refresh_progress(reschedule=False)
        n_c, n0 = ncalls("cancel"), len(txt())
        p.on_send("x")
        spin(lambda: p.runner.busy, 5)
        p.on_stop()
        check(spin(lambda: "la malla no se interrumpe, la macro parara" in txt()[n0:] and not p.cli_active, 10) and p.cancel_pending,
              "cancel pendiente: muestra el texto del MCP y queda en vigilancia")
        check("avisaré cuando termine" in txt()[n0:] and p.stop_btn.cget("text") == "Apagar solver", "cancel pendiente: ofrece apagar el solver")
        os.utime(pj, (time.time() - 300, time.time() - 300))  # el dueño murió: archivo viejo, aún sin terminar
        p.refresh_progress(reschedule=False)
        check(p.cancel_pending and p.card.winfo_manager() == "pack", "cancel pendiente: el estado sigue visible aunque el archivo sea viejo")
        pj.write_text(json.dumps({"caso": "BombaX", "fase": "terminado", "terminado": True}))
        p.refresh_progress(reschedule=False)
        check(not p.cancel_pending and "La macro terminó" in txt()[n0:] and p.card.winfo_manager() == "", "cancel pendiente: al decir terminado, avisa y quita la tarjeta")
        spin(lambda: not p.runner.busy, 10)
        # «Parar iteraciones» en la malla con la CLI viva: explica y ofrece «Parar al terminar la malla» (llama a cancel)
        pj.write_text(json.dumps({"caso": "BombaX", "fase": "mallar", "falta_s": 30}))
        p.refresh_progress(reschedule=False)
        n_c, n0 = ncalls("cancel"), len(txt())
        p.on_cancel()
        check("no se puede parar" in txt()[n0:] and ncalls("cancel") == n_c and p.wait_btn.cget("text") == "Parar al terminar la malla", "malla: explica y ofrece «Parar al terminar la malla»")
        p.wait_btn.invoke()
        check(spin(lambda: ncalls("cancel") == n_c + 1 and not p.cli_active, 10) and p.cancel_pending, "«Parar al terminar la malla»: llama a cancel y queda pendiente")
        os.environ.pop("FAKE_CANCEL_PENDING")
        p.cancel_pending = False
        pj.write_text(json.dumps({"caso": "BombaX", "fase": "terminado", "terminado": True}))
        p.refresh_progress(reschedule=False)
        # claves fuera de pantalla
        p.say("Arranque: mcp -podkey ABC123SECRETO -np 4", "sys")
        p.say("licencia: XYZ987 y password=hunter2", "sys")
        t = txt()
        check("ABC123SECRETO" not in t and "XYZ987" not in t and "hunter2" not in t and "-podkey ***" in t,
              "el panel redacta -podkey y claves=valor en lo que muestra")
        check("***" in redact("API_KEY: sk-abc") and redact("H 16,7 m, eta 88 %") == "H 16,7 m, eta 88 %", "redact no toca texto normal")
        sj.write_text(json.dumps(base))
        p.refresh()
        p.hist.config(state="normal")  # los checks siguientes buscan frases que aquí ya salieron
        p.hist.delete("1.0", "end")
        p.hist.config(state="disabled")

    t0 = time.time()
    tmp = Path(tempfile.mkdtemp(prefix="panel-selftest-"))
    ESTADO = tmp / ".estado.json"
    # mcp_server falso: `--cli <herramienta> [json]` -> JSON por stdout (el real lo añade el constructor A4)
    fake_mcp = tmp / "fake_mcp_server.py"
    fake_mcp.write_text(
        "import json, os, sys, time\n"
        "a = sys.argv; i = a.index('--cli'); tool = a[i + 1]; args = json.loads(a[i + 2]) if len(a) > i + 2 else {}\n"
        "open(os.environ['FAKE_CLI_LOG'], 'a').write(tool + ' ' + json.dumps(args) + '\\n')\n"
        "if tool == 'server_stop':\n"
        "    time.sleep(float(os.environ.get('FAKE_STOP_SLEEP', '0.4'))); p = os.environ.get('FAKE_STOP_REMOVE')\n"
        "    if p and os.path.exists(p): os.unlink(p)\n"
        "    print(json.dumps({'ok': True, 'msg': 'solver apagado (falso)'}))\n"
        "elif tool == 'gui_attach': print(json.dumps({'ok': True, 'msg': 'GUI conectada (falsa)'}))\n"
        "elif tool == 'cancel' and os.environ.get('FAKE_CANCEL_NO'): print(json.dumps({'ok': True, 'cancelled': False, 'reason': 'no hay iteración en curso'}))\n"
        "elif tool == 'cancel' and os.environ.get('FAKE_CANCEL_PENDING'): print(json.dumps({'ok': True, 'cancelled': False, 'pendiente': True, 'reason': 'ABORT puesto: la malla no se interrumpe, la macro parara antes de iterar'}))\n"
        "elif tool == 'cancel': print(json.dumps({'ok': True, 'cancelled': True, 'cancelado': True, 'fase': 'iterar', 'iteracion': 20}))\n"
        "elif tool == 'server_status': print(json.dumps({'ok': True, 'solver': False, 'caso': None}))\n"
        "else: print(json.dumps({'error': 'herramienta desconocida'})); sys.exit(2)\n", encoding="utf-8")
    os.environ["PANEL_MCP_SERVER"] = str(fake_mcp)
    os.environ["FAKE_CLI_LOG"] = str(tmp / "cli.log")
    os.environ.pop("FAKE_STOP_REMOVE", None)
    fake_resume = tmp / "fake_claude_resume.py"  # como la CLI con --resume de una sesión caducada; sin --resume, la falsa normal
    fake_resume.write_text(
        "import os, runpy, sys\n"
        "if '--resume' in sys.argv:\n"
        "    if os.environ.get('FAKE_RESUME_MODE') == 'stdout':\n"
        "        print('{\"type\": \"result\", \"subtype\": \"error_during_execution\", \"is_error\": true, \"session_id\": \"x\", "
        "\"result\": \"No conversation found with session ID: viejo\"}', flush=True)\n"
        "    else:\n"
        "        sys.stderr.write('No conversation found with session ID: viejo\\n')\n"
        "    sys.exit(1)\n"
        f"runpy.run_path(r'{REPO / 'tests' / 'fake_claude.py'}', run_name='__main__')\n", encoding="utf-8")
    os.environ["PANEL_CLAUDE_BIN"] = str(REPO / "tests" / "fake_claude.py")
    os.environ["FAKE_PAUSE"] = "0.1"

    check(tool_label("mcp__starccm__iterate", {"n": 100}) == "Iterando 100 iteraciones", "traducción iterate")
    check("bomba" in tool_label("mcp__starccm__pump_case", {"case": "X"}).lower(), "traducción pump_case")
    check(tool_label("mcp__starccm__gui_attach").startswith("Abriendo la GUI"), "traducción gui_attach")
    check(tool_label("mcp__starccm__run_java", {"body": "// QUE: importar los STEP\nx"}) == "Macro: importar los STEP", "traducción run_java con QUE")
    check(tool_label("mcp__starccm__algo_raro") == "Usando algo_raro", "tool desconocida")
    ev = parse_event(json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "a", "content": "x", "is_error": True}]}}))
    check(ev == [("tool_result", "a", "x", True)], "parseo tool_result con error")
    check(parse_event("basura")[0][0] == "raw" and parse_event("") == [], "línea no JSON y vacía")
    cmd = build_cmd("hola", "sid-1")
    check(cmd and "--resume" in cmd and cmd[cmd.index("--resume") + 1] == "sid-1" and "--strict-mcp-config" in cmd, "comando con --resume y MCP estricto")
    check("--resume" not in build_cmd("hola", None), "sin sesión no hay --resume")
    c = build_cmd("hola", None)
    check(c[c.index("--model") + 1] == "sonnet" and c[c.index("--effort") + 1] == "low", "modo Rápido por defecto: sonnet / low")
    c = build_cmd("hola", None, modo="preciso")
    check(c[c.index("--model") + 1] == "opus" and c[c.index("--effort") + 1] == "medium", "modo Preciso: opus / medium")
    os.environ["PANEL_MODEL_RAPIDO"] = "haiku"
    check(build_cmd("x", None)[build_cmd("x", None).index("--model") + 1] == "haiku", "PANEL_MODEL_RAPIDO configurable")
    del os.environ["PANEL_MODEL_RAPIDO"]
    check(len(ESTILO) < 900 and "pump_case" in ESTILO and "run_steps" in ESTILO and "stable" in ESTILO, f"prompt de velocidad corto ({len(ESTILO)} car.)")
    save_sid("s-9")
    save_estado(modo="preciso")
    check(load_modo() == "preciso" and load_sid() == "s-9", "modo persistente sin pisar la sesión")
    save_estado(modo="rapido", session_id=None)
    check(parse_event(json.dumps({"type": "result", "session_id": "a", "subtype": "success", "num_turns": 4}))[0][4] == 4, "result trae num_turns")
    os.environ["CLAUDE_CODE_X"] = "1"
    check(not any(k.startswith("CLAUDE") for k in clean_env()), "entorno sin CLAUDE*")

    trepo = tmp / "repo0"  # repo de mentira: Runner.send crea ahí su .mcp.json
    trepo.mkdir()
    (trepo / ".mcp.json.example").write_text(json.dumps({"mcpServers": {"starccm": {"command": "C:\\viejo\\python.exe", "args": ["C:\\viejo\\mcp_server.py"]}}}), encoding="utf-8")
    q: queue.Queue = queue.Queue()
    r = Runner(q, trepo)
    r.send("hola", None)
    evs = []
    while True:
        e = q.get(timeout=15)
        evs.append(e)
        if e[0] == "done":
            break
    kinds = [e[0] for e in evs]
    cfgp = trepo / ".mcp.json"
    cj = _json(cfgp).get("mcpServers", {}).get("starccm", {})
    check(cfgp.is_file() and any(e[0] == "aviso" for e in evs) and cj.get("args") == [str(trepo / "mcp_server.py")]
          and Path(cj.get("command", "")).is_absolute() and "viejo" not in json.dumps(cj),
          "sin .mcp.json: el panel lo crea desde el .example con rutas absolutas y avisa en el chat")
    check(ensure_mcp_config(trepo)[1] is False, "con .mcp.json existente no lo vuelve a crear")
    kinds = [k for k in kinds if k != "aviso"]
    check(kinds[0] == "init" and "tool" in kinds and "tool_result" in kinds and kinds.count("text") == 2
          and "result" in kinds and evs[-1] == ("done", 0, False), f"eventos de la CLI falsa: {kinds}")
    save_sid("fake-0001")
    check(load_sid() == "fake-0001", "guardado de sesión")
    save_sid(None)

    q2: queue.Queue = queue.Queue()
    os.environ["FAKE_PAUSE"] = "5"
    r2 = Runner(q2, trepo)
    r2.send("lento", None)
    q2.get(timeout=10)  # init
    time.sleep(0.3)
    check(r2.busy, "CLI en marcha")
    t1 = time.time()
    r2.stop()
    while True:
        last = q2.get(timeout=10)
        if last[0] == "done":
            break
    check(last[2] is True and not r2.busy and time.time() - t1 < 5, "Detener mata la CLI")
    os.environ["FAKE_PAUSE"] = "0.1"

    repo = tmp / "repo"
    repo.mkdir()
    s = read_status(repo)
    check(s["texto"].startswith("Solver apagado") and s["gui"] is False and "GUI no conectada" in s["texto"], "estado sin archivos: GUI no conectada (no «?»)")
    (repo / ".server.json").write_text(json.dumps({"pid": os.getpid(), "sim": "C:/x/Bomba1.sim", "idle_warning": "N min sin actividad: ¿apagar?", "gui": True}))
    old = time.time() - 7 * 60
    os.utime(repo / ".server.json", (old, old))
    s = read_status(repo)
    check(s["solver"] and s["caso"] == "Bomba1" and s["idle_min"] == 7 and s["idle_warning"] and s["gui"] is True
          and "GUI conectada" in s["texto"] and "?" not in s["texto"], f"estado con solver: {s['texto']}")
    base = {"pid": os.getpid(), "sim": "C:/x/Bomba1.sim"}
    (repo / ".server.json").write_text(json.dumps({**base, "gui": False, "idle_min": 31,
                                                   "idle_warning": {"idle_min": 31, "gui": True, "msg": "31 min sin actividad"}}))
    s2 = read_status(repo)
    check(s2["idle_warning"] == "31 min sin actividad" and s2["idle_min"] == 31 and s2["gui"] is False and "GUI no conectada" in s2["texto"],
          "idle_min del vigilante y idle_warning en dict -> texto (no un dict en la barra)")
    # nueva ubicación: estado_dir > repo; panel_estado.json > COPILOTO_WORK > %LOCALAPPDATA%
    sd = tmp / "estado"
    sd.mkdir()
    (sd / ".server.json").write_text(json.dumps({**base, "sim": "C:/x/Nueva.sim", "gui": True}))
    check(read_status(repo, sd)["caso"] == "Nueva" and read_status(repo, sd)["gui"] is True, "lee .server.json de la carpeta de estado (antes que el repo)")
    check(read_status(repo, tmp / "vacia")["caso"] == "Bomba1", "compatibilidad: si no está en la carpeta de estado, mira el repo")
    repo_real, REPO = REPO, tmp / "repo_pe"
    REPO.mkdir()
    try:
        (REPO / "panel_estado.json").write_text(json.dumps({"estado_dir": str(sd)}))
        c1 = state_dir() == sd
        (REPO / "panel_estado.json").unlink()
        os.environ["COPILOTO_WORK"] = str(tmp / "w")
        c2 = state_dir() == tmp / "w"
        del os.environ["COPILOTO_WORK"]
        c3 = state_dir().name == "starccm-copiloto"
        check(c1 and c2 and c3 and state_dirs()[0] == state_dir(), "state_dir: panel_estado.json > COPILOTO_WORK > %LOCALAPPDATA%/starccm-copiloto")
    finally:
        REPO = repo_real
    (repo / ".server.json").write_text(json.dumps({**base, "idle_warning": "N min sin actividad: ¿apagar?", "gui": True}))
    os.utime(repo / ".server.json", (old, old))
    sp = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    (repo / ".inject.lock").write_text(json.dumps({"pid": sp.pid, "tool": "iterate", "started": time.time()}))
    s = read_status(repo)
    check(s["busy"] == "iterate" and "agente trabajando" in s["texto"], "candado de otro proceso vivo")
    sp.kill()
    sp.wait()
    check(read_status(repo)["busy"] is None, "candado huérfano ignorado")

    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
    except Exception as e:  # sin pantalla
        print("OMITE parte visual:", str(e)[:60])
    else:
        os.environ["FAKE_PAUSE"] = "0.4"
        tmp_sd = tmp / "estado_panel"
        tmp_sd.mkdir()
        p = Panel(root, repo=trepo, sdir=tmp_sd)

        def spin(cond, secs=15):
            end = time.time() + secs
            while time.time() < end and not cond():
                root.update()
                time.sleep(0.02)
            return cond()
        t_ini = time.time()
        p.on_send("hola")
        check(spin(lambda: p.live.cget("text").startswith("▶ Macro: importar los STEP del caso … ")), "cronómetro en vivo de la herramienta")
        p.modo.set("preciso")
        p.on_modo()
        check(load_modo() == "preciso", "selector de modo guarda en .estado.json")
        p.on_send("segundo mensaje")  # CLI ocupada -> cola
        check(len(p.pending) == 1 and "en cola" in p.hist.get("1.0", "end"), "mensaje encolado visible como «en cola»")
        check(spin(lambda: "Recibido: segundo mensaje" in p.hist.get("1.0", "end") and not p.runner.busy and not p.pending), "cola enviada con --resume al terminar")
        txt = p.hist.get("1.0", "end")
        check("en cola" not in txt and txt.count("⏱") == 2 and "turnos del modelo" in txt and "en herramientas" in txt, "línea gris de tiempos por turno (2 turnos)")
        check("▶ Macro: importar los STEP del caso … 0 s ✓" in txt.replace("… 0 s", "… 0 s") or "Macro: importar los STEP del caso … " in txt, "ventana: historial con línea de herramienta")
        check(load_estado().get("modo") == "preciso", "modo sigue guardado")
        p.on_send("otro")
        p.on_send("y otro")
        p.stop_delay_ms = 10
        p.on_stop()
        spin(lambda: not p.runner.busy)
        check(not p.pending, "Detener descarta la cola")
        spin(lambda: p.live.cget("text") == "", 3)
        p.modo.set("rapido")
        p.on_modo()
        check(load_sid() == "fake-0001", "ventana: sesión guardada")
        p.agent_text("**fuerte** y `cod`\n| a | b |\n|---|---|")
        check("fuerte y cod" in p.hist.get("1.0", "end"), "markdown renderizado")
        # banda ámbar con idle_warning y GUI sí/no en la barra
        sj = tmp_sd / ".server.json"
        sj.write_text(json.dumps({"pid": os.getpid(), "sim": "C:/x/Nueva.sim", "gui": False, "idle_warning": {"msg": "40 min sin actividad: ¿apagar?"}}))
        p.refresh()
        check(p.warn.winfo_manager() == "pack" and "40 min sin actividad" in p.warn.cget("text") and "GUI no conectada" in p.chip["gui"].cget("text"),
              "ventana: idle_warning en banda ámbar y «GUI no conectada»")
        sj.write_text(json.dumps({"pid": os.getpid(), "sim": "C:/x/Nueva.sim", "gui": True}))
        p.refresh()
        check(p.warn.winfo_manager() == "" and "GUI conectada" in p.chip["gui"].cget("text"), "ventana: sin aviso la banda se oculta; GUI conectada")
        run_new_checks(p, root, spin, trepo, tmp, tmp_sd, sj)
        run_r5_checks(p, root, spin, trepo, tmp_sd, sj)
        # Detener: avisa si el solver sigue vivo con una macro en curso
        check(stop_warning(trepo, tmp_sd) is None, "tras Detener sin macro en curso: sin aviso")
        lock = tmp_sd / ".inject.lock"
        lock.write_text(json.dumps({"pid": 999999, "tool": "iterate", "start": "x"}))
        w = stop_warning(trepo, tmp_sd)
        check(w and "iterate" in w and "sigue vivo" in w, "tras Detener con .inject.lock: aviso de solver vivo con macro en curso")
        p.check_after_stop()
        check(hasattr(p, "stop_btn") and p.stop_btn.cget("text") == "Apagar solver" and "sigue vivo" in p.hist.get("1.0", "end"),
              "aviso en el chat con botón «Apagar solver»")
        lock.unlink()
        # cerrar el panel con el solver vivo: diálogo Sí / No / Cancelar dentro del panel
        p.on_close()
        check(p.dlg is not None and set(p.dlg_btn) == {"si", "no", "cancelar"} and not p.closed, "cerrar con solver vivo pregunta «¿Apagar el solver antes de salir?»")
        p.dlg_btn["cancelar"].invoke()
        check(p.dlg is None and not p.closed, "Cancelar no cierra")
        p.close_timeout = 1.0
        p.on_close()
        p.dlg_btn["si"].invoke()
        check(spin(lambda: "no se apagó a tiempo" in p.hist.get("1.0", "end"), 10) and not p.closed and not p.closing,
              "Sí con solver que no se apaga: espera el máximo y no cierra a ciegas")
        spin(lambda: not p.cli_active, 10)
        log = Path(os.environ["FAKE_CLI_LOG"]).read_text(encoding="utf-8")
        check("Apagando el solver antes de salir" in p.hist.get("1.0", "end") and "server_stop" in log and p.last_msg != "Guarda y apaga el solver",
              "Sí llama a `mcp_server.py --cli server_stop` directo (sin turno de modelo)")
        root2 = tk.Tk()  # No: cierra sin apagar
        root2.withdraw()
        p2 = Panel(root2, repo=trepo, sdir=tmp_sd)
        p2.on_close()
        p2.dlg_btn["no"].invoke()
        check(p2.closed, "No: cierra sin apagar el solver")
        p.close_timeout = 20.0  # Sí con apagado real (el .server.json desaparece al segundo)
        os.environ["FAKE_STOP_REMOVE"] = str(sj)  # el falso server_stop borra .server.json
        p.on_close()
        p.dlg_btn["si"].invoke()
        t_si = time.time()
        while time.time() - t_si < 15 and not p.closed:
            try:
                root.update()
            except Exception:
                break
            time.sleep(0.02)
        check(p.closed and time.time() - t_si < 14, f"Sí: server_stop, espera a que el solver muera y cierra ({time.time() - t_si:.1f} s)")
        root3 = tk.Tk()  # solver apagado: cierra directo
        root3.withdraw()
        p3 = Panel(root3, repo=trepo, sdir=tmp_sd)
        p3.on_close()
        check(p3.closed and p3.dlg is None, "cerrar con el solver apagado: cierra directo, sin preguntar")
    # Detener no mata solver/residente aunque cuelguen del árbol de la CLI (taskkill /T los alcanzaría)
    if os.name == "nt":
        pf = tmp / "hijos.txt"
        code = ("import subprocess,sys,time;c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],creationflags=0x200);"
                f"open(r'{pf}','w').write(str(c.pid));time.sleep(60)")
        par = subprocess.Popen([sys.executable, "-c", code])
        for _ in range(100):
            if pf.exists() and pf.read_text():
                break
            time.sleep(0.1)
        hijo = int(pf.read_text())
        kill_tree(par, protect={hijo})
        time.sleep(0.4)
        check(par.poll() is not None and pid_alive(hijo), "kill_tree mata la CLI/MCP pero respeta el solver (pid protegido)")
        subprocess.run(["taskkill", "/PID", str(hijo), "/F"], capture_output=True)
    check(all(x in DENIED_TOOLS for x in ("Read(./.env)", "Read(**/.env)", "Grep(./.env)", "Grep(**/.env)", "Glob(./.env)", "Glob(**/.env)")),
          "reglas de denegación Read/Grep/Glob para .env y **/.env")
    c = build_cmd("x", None)
    check("--disallowedTools" in c and c[c.index("--disallowedTools") + 1] == ",".join(DENIED_TOOLS), "la CLI se lanza con --disallowedTools")
    check("nunca muestres claves" in ESTILO.lower() and "conocimiento/" in ESTILO and len(ESTILO) < 900,
          f"prompt: prohíbe mostrar credenciales, lee conocimiento/ solo si hace falta, < 900 car. ({len(ESTILO)})")
    print("TODO OK" if ok_all else "HAY FALLAS", f"({time.time() - t0:.1f} s)")
    return 0 if ok_all else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    main()

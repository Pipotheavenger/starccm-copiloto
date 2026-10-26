"""Panel flotante de chat para starccm-copiloto (tkinter, solo biblioteca estándar). Ver ARQUITECTURA.md §2.

Uso:  python panel/chat_panel.py        |   python panel/chat_panel.py --selftest
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
MSG_APAGAR = "Guarda y apaga el solver con server_stop"
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
        threading.Thread(target=lambda: [err.append(x) for x in p.stderr], daemon=True).start()
        got_result = False
        for line in p.stdout:
            for ev in parse_event(line):
                got_result |= ev[0] == "result"
                self.q.put(ev)
        rc = p.wait()
        if rc != 0 and not self.stopped and not got_result:
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


def read_status(repo: Path | None = None, sdir: Path | None = None) -> dict:
    """Lee .server.json y .inject.lock (sin importar mcp_server). Devuelve campos + `texto` para la barra."""
    dirs = state_dirs(repo, sdir)
    sf = _first(".server.json", dirs)
    st = _json(sf)
    alive = bool(st.get("pid")) and pid_alive(st["pid"])
    out = {"solver": alive, "caso": None, "idle_min": None, "idle_warning": None, "gui": False, "busy": None}
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


# ---------- ventana ----------
FONT, MONO = ("Segoe UI", 10), ("Consolas", 10)
INLINE = re.compile(r"(\*\*[^*\n]+\*\*|`[^`\n]+`)")
BTN_BG = "SystemButtonFace" if os.name == "nt" else "#d9d9d9"


class Panel:
    def __init__(self, root, runner_q=None, repo: Path | None = None, sdir: Path | None = None):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.root = tk, root
        self.repo, self.sdir = repo, sdir  # None = ubicaciones reales (panel_estado.json / COPILOTO_WORK / LOCALAPPDATA)
        self.closed = self.closing = False
        self.aft: dict[str, str] = {}
        self.close_timeout, self.dlg = 60.0, None
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
        root.title("Copiloto STAR-CCM+")
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"420x640+{max(sw - 440, 0)}+{max((sh - 640) // 2 - 20, 0)}")
        root.minsize(320, 360)
        root.configure(bg="#f5f6f8")
        self.top = tk.BooleanVar(value=True)
        root.attributes("-topmost", True)

        bar = tk.Frame(root, bg="#e8ebf0")
        bar.pack(fill="x")
        self.status = tk.Label(bar, text="…", anchor="w", justify="left", bg="#e8ebf0", fg="#222", font=FONT, wraplength=300)
        self.status.pack(side="left", fill="x", expand=True, padx=6, pady=3)
        tk.Checkbutton(bar, text="Encima", variable=self.top, command=self.toggle_top, bg="#e8ebf0", font=FONT).pack(side="right", padx=4)
        bar.bind("<Configure>", lambda e: self.status.config(wraplength=max(e.width - 100, 100)))
        mbar = self.modo_bar = tk.Frame(root, bg="#e8ebf0")
        mbar.pack(fill="x")
        tk.Label(mbar, text="Modo:", bg="#e8ebf0", font=FONT).pack(side="left", padx=(6, 2))
        self.modo = tk.StringVar(value=load_modo())
        for k, (nombre, *_r) in MODOS.items():
            tk.Radiobutton(mbar, text=nombre, value=k, variable=self.modo, command=self.on_modo, bg="#e8ebf0",
                           activebackground="#e8ebf0", font=FONT).pack(side="left", padx=2)
        self.modo_info = tk.Label(mbar, text="", bg="#e8ebf0", fg="#666", font=(FONT[0], 9))
        self.modo_info.pack(side="left", padx=6)
        self.on_modo(save=False)
        self.warn = tk.Label(root, text="", anchor="w", justify="left", bg="#fde68a", fg="#7a4100", font=(FONT[0], FONT[1], "bold"),
                             wraplength=380, padx=8, pady=3)  # banda ámbar: idle_warning (solo visible si hay aviso)
        root.bind("<Configure>", lambda e: self.warn.config(wraplength=max(root.winfo_width() - 24, 100)) if e.widget is root else None)

        foot = tk.Frame(root, bg="#f5f6f8")
        foot.pack(side="bottom", fill="x")
        row = tk.Frame(foot, bg="#f5f6f8")
        row.pack(fill="x", padx=6, pady=(4, 0))
        self.btn = {}
        for key, txt, cmd in (("enviar", "Enviar", self.on_send), ("detener", "Detener", self.on_stop),
                              ("nueva", "Nueva", self.on_new), ("gui", "Abrir GUI", self.on_gui),
                              ("apagar", "Apagar solver", self.on_off)):
            b = tk.Button(row, text=txt, command=cmd, font=FONT, padx=4)
            b.pack(side="left", padx=2, pady=2)
            self.btn[key] = b
        self.btn["enviar"].config(bg="#2563eb", fg="white")
        self.entry = tk.Text(foot, height=3, wrap="word", font=FONT, relief="solid", bd=1)
        self.entry.pack(fill="x", padx=6, pady=6)
        self.live = tk.Label(root, text="", anchor="w", bg="#f5f6f8", fg="#1d4ed8", font=FONT)
        self.live.pack(side="bottom", fill="x", padx=8)

        self.hist = tk.Text(root, wrap="word", state="disabled", font=FONT, bg="#ffffff", fg="#1b1b1b", relief="flat",
                            padx=8, pady=6, spacing3=3)
        sb = ttk.Scrollbar(root, command=self.hist.yview)
        self.hist.config(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.hist.pack(side="left", fill="both", expand=True)
        h = self.hist
        h.tag_config("user", background="#dde9ff", lmargin1=8, lmargin2=8, rmargin=8, spacing1=6, spacing3=6)
        h.tag_config("bold", font=(FONT[0], FONT[1], "bold"))
        h.tag_config("code", font=MONO, background="#eef0f3")
        h.tag_config("table", font=MONO, background="#f3f5f8")
        h.tag_config("tool", foreground="#555555", font=(FONT[0], FONT[1], "italic"))
        h.tag_config("ok", foreground="#1a7f37")
        h.tag_config("bad", foreground="#c62828")
        h.tag_config("err", foreground="#c62828", font=(FONT[0], FONT[1], "bold"))
        h.tag_config("sys", foreground="#777777")
        h.tag_config("stat", foreground="#888888", font=(FONT[0], 9), spacing1=2, spacing3=8)
        h.tag_config("queued", foreground="#b45309", font=(FONT[0], 9, "italic"))

        self.entry.bind("<Return>", lambda e: (self.on_send(), "break")[1])
        self.entry.bind("<Shift-Return>", lambda e: None)
        self.entry.bind("<Up>", self.on_up)
        root.bind("<Control-t>", lambda e: (self.top.set(not self.top.get()), self.toggle_top()))
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.say("Listo. Escribe lo que necesitas." + (" (continúo la conversación anterior)" if self.sid else ""), "sys")
        self.entry.focus_set()
        self.pump()
        self.refresh()

    # --- texto
    def toggle_top(self):
        self.root.attributes("-topmost", bool(self.top.get()))

    def put(self, text, *tags):
        self.hist.config(state="normal")
        self.hist.insert("end", text, tags)
        self.hist.config(state="disabled")
        self.hist.see("end")

    def say(self, text, tag):
        self.put(text + "\n", tag)

    def agent_text(self, text):
        for line in text.split("\n"):
            if line.lstrip().startswith("|"):
                self.put(line + "\n", "table")
                continue
            for part in INLINE.split(line):
                if part.startswith("**") and part.endswith("**") and len(part) > 4:
                    self.put(part[2:-2], "bold")
                elif part.startswith("`") and part.endswith("`") and len(part) > 2:
                    self.put(part[1:-1], "code")
                else:
                    self.put(part)
            self.put("\n")

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
        self.put(msg + "\n", "user")
        self.start(msg)

    def on_modo(self, save=True):
        fl = modo_flags(self.modo.get())
        self.modo_info.config(text=f"{fl[1]} · esfuerzo {fl[3]}")
        if save:
            save_estado(modo=self.modo.get())

    def start(self, msg):
        self.turn_t0, self.tool_s, self.n_tools = time.time(), 0.0, 0
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

    def on_stop(self):
        if self.runner.busy:
            n = len(self.pending)
            self.pending.clear()
            self.runner.stop()
            self.say("Detenido." + (f" Descarté {n} mensaje(s) en cola." if n else ""), "sys")
            self.root.after(self.stop_delay_ms, self.check_after_stop)

    stop_delay_ms = 1200

    def check_after_stop(self):
        w = stop_warning(self.repo, self.sdir)
        if not w or self.closed:
            return
        self.say(w, "err")
        self.hist.config(state="normal")
        b = self.tk.Button(self.hist, text="Apagar solver", font=FONT, bg="#ffd6d6", command=self.on_off_now)
        self.hist.window_create("end", window=b, padx=4, pady=2)
        self.hist.insert("end", "\n")
        self.hist.config(state="disabled")
        self.hist.see("end")
        self.stop_btn = b

    def twice(self, key, button, normal, action):
        """Confirmación dentro del panel: el primer clic pide confirmar; el segundo (en 4 s) ejecuta."""
        now = time.time()
        if now - self.confirm.get(key, 0) < 4:
            self.confirm.pop(key, None)
            button.config(text=normal, bg=BTN_BG)
            action()
            return
        self.confirm[key] = now
        button.config(text="¿Seguro? Otro clic", bg="#ffd6d6")

        def reset():
            if key in self.confirm and time.time() - self.confirm[key] >= 3.9:
                self.confirm.pop(key, None)
                button.config(text=normal, bg=BTN_BG)
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
            self.say("Conversación nueva.", "sys")
        self.twice("nueva", self.btn["nueva"], "Nueva", go)

    def on_gui(self):
        self.on_send("Abre la GUI con gui_attach")

    def on_off_now(self):
        self.on_send(MSG_APAGAR)

    def on_off(self):
        self.twice("apagar", self.btn["apagar"], "Apagar solver", self.on_off_now)

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
        tk = self.tk
        f = tk.Frame(self.root, bg="#ffffff", bd=2, relief="solid", padx=14, pady=12)
        tk.Label(f, text="¿Apagar el solver antes de salir?\n(cobra por hora)", bg="#ffffff", font=(FONT[0], 11, "bold"),
                 justify="center").pack(pady=(0, 10))
        row = tk.Frame(f, bg="#ffffff")
        row.pack()
        self.dlg_btn = {}
        for key, txt in (("si", "Sí"), ("no", "No"), ("cancelar", "Cancelar")):
            b = tk.Button(row, text=txt, font=FONT, width=9, command=lambda k=key: self.dlg_answer(k))
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
        self.closing, self.close_t0 = True, time.time()
        self.say("Apagando el solver antes de salir… (máx. %d s)" % self.close_timeout, "sys")
        self.on_send(MSG_APAGAR)  # si la CLI está ocupada se encola y sale al terminar el turno
        self.root.after(300, self.close_poll)

    def close_poll(self):
        if self.closed:
            return
        if not self.runner.busy and not self.pending and not self.status_now()["solver"]:
            self.finish()
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
        elif k == "done":
            self.tools.clear()
            self.live.config(text="")
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
        if self.tools:
            label, t0 = max(self.tools.values(), key=lambda v: v[1])
            self.live.config(text=f"▶ {label} … {fmt_s(time.time() - t0)}", fg="#1d4ed8")
        elif self.runner.busy:
            self.live.config(text=f"… la IA piensa {fmt_s(time.time() - self.turn_t0)}" if self.turn_t0 else "", fg="#777777")
        else:
            self.live.config(text="")

    def refresh(self):
        s = self.status_now()
        self.status.config(text=s["texto"])
        if s["idle_warning"]:
            self.warn.config(text="⚠ " + s["idle_warning"])
            if self.warn.winfo_manager() != "pack":
                self.warn.pack(fill="x", after=self.modo_bar)
        elif self.warn.winfo_manager() == "pack":
            self.warn.pack_forget()
        if "refresh" in self.aft:
            self.root.after_cancel(self.aft["refresh"])
        self.aft["refresh"] = self.root.after(3000, self.refresh)


def main():
    import tkinter as tk
    root = tk.Tk()
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

    t0 = time.time()
    tmp = Path(tempfile.mkdtemp(prefix="panel-selftest-"))
    ESTADO = tmp / ".estado.json"
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
        check(p.warn.winfo_manager() == "pack" and "40 min sin actividad" in p.warn.cget("text") and "GUI no conectada" in p.status.cget("text"),
              "ventana: idle_warning en banda ámbar y «GUI no conectada»")
        sj.write_text(json.dumps({"pid": os.getpid(), "sim": "C:/x/Nueva.sim", "gui": True}))
        p.refresh()
        check(p.warn.winfo_manager() == "" and "GUI conectada" in p.status.cget("text"), "ventana: sin aviso la banda se oculta; GUI conectada")
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
        spin(lambda: not p.runner.busy and not p.pending, 10)
        check("Apagando el solver antes de salir" in p.hist.get("1.0", "end") and p.last_msg == MSG_APAGAR, "Sí manda server_stop por el chat")
        root2 = tk.Tk()  # No: cierra sin apagar
        root2.withdraw()
        p2 = Panel(root2, repo=trepo, sdir=tmp_sd)
        p2.on_close()
        p2.dlg_btn["no"].invoke()
        check(p2.closed, "No: cierra sin apagar el solver")
        p.close_timeout = 20.0  # Sí con apagado real (el .server.json desaparece al segundo)
        threading.Timer(1.0, lambda: sj.unlink()).start()
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

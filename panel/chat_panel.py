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
    "Eres el copiloto de STAR-CCM+ de Sebastián. Responde en español, con frases cortas y los números en tablas. "
    "El usuario mira la GUI real de STAR-CCM+ conectada al mismo servidor: di qué mirar allí "
    "(p. ej. «abre Scenes › presion_alabes»). No toques nada mientras el usuario edita en la GUI: si no estás seguro, "
    "pregunta. Antes de cada herramienta escribe UNA frase corta de qué vas a hacer. Pregunta antes de lo caro "
    "(mallas grandes, muchas iteraciones, apagar el solver). Termina con una línea: qué quedó hecho."
)

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
        ev.append(("result", o.get("session_id"), o.get("subtype"), bool(o.get("is_error"))))
    return ev


def clean_env() -> dict:
    return {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}


def claude_bin() -> str | None:
    return os.environ.get("PANEL_CLAUDE_BIN") or shutil.which("claude")


DEFAULT_TOOLS = "mcp__starccm__*,Read,Glob,Grep"


def build_cmd(msg: str, sid: str | None, allowed: str = DEFAULT_TOOLS) -> list[str] | None:
    b = claude_bin()
    if not b:
        return None
    pre = [sys.executable, b] if b.lower().endswith(".py") else [b]
    cmd = pre + ["-p", msg, "--output-format", "stream-json", "--verbose"]
    if sid:
        cmd += ["--resume", sid]
    cmd += ["--mcp-config", str(REPO / ".mcp.json"), "--strict-mcp-config", "--allowedTools", allowed,
            "--append-system-prompt", ESTILO]
    return cmd


def kill_tree(p: subprocess.Popen) -> None:
    if p.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, timeout=30)
    if p.poll() is None:
        p.kill()


class Runner:
    """Una conversación por vez: un hilo lee la CLI y mete eventos en `q`. El último evento es ('done', rc, detenido)."""

    def __init__(self, q: queue.Queue):
        self.q, self.proc, self.stopped = q, None, False

    @property
    def busy(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def send(self, msg: str, sid: str | None, allowed: str = DEFAULT_TOOLS) -> bool:
        cmd = build_cmd(msg, sid, allowed)
        if not cmd:
            self.q.put(("error", "no encuentro la CLI `claude` (PATH o PANEL_CLAUDE_BIN)"))
            self.q.put(("done", -1, False))
            return False
        self.stopped = False
        try:
            self.proc = subprocess.Popen(cmd, cwd=REPO, env=clean_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
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
            kill_tree(self.proc)


# ---------- sesión y estado ----------
def load_sid() -> str | None:
    try:
        return json.loads(ESTADO.read_text(encoding="utf-8")).get("session_id")
    except (OSError, ValueError):
        return None


def save_sid(sid: str | None) -> None:
    try:
        ESTADO.write_text(json.dumps({"session_id": sid}), encoding="utf-8")
    except OSError:
        pass


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


def read_status(repo: Path = REPO) -> dict:
    """Lee .server.json y .inject.lock (sin importar mcp_server). Devuelve campos + `texto` para la barra."""
    sf, lf = repo / ".server.json", repo / ".inject.lock"
    st = _json(sf)
    alive = bool(st.get("pid")) and pid_alive(st["pid"])
    out = {"solver": alive, "caso": None, "idle_min": None, "idle_warning": None, "gui": "?", "busy": None}
    parts = []
    if alive:
        out["caso"] = Path(st["sim"]).stem if st.get("sim") else "(sin .sim)"
        try:
            out["idle_min"] = int((time.time() - sf.stat().st_mtime) // 60)
        except OSError:
            pass
        parts.append(f"Solver vivo · {out['caso']}" + (f" · {out['idle_min']} min inactivo" if out["idle_min"] is not None else ""))
        out["idle_warning"] = st.get("idle_warning") or None
    else:
        parts.append("Solver apagado")
    g = st.get("gui")
    out["gui"] = "?" if g is None else bool(g)
    parts.append("GUI " + {"?": "?", True: "conectada", False: "no conectada"}[out["gui"]])
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
    def __init__(self, root, runner_q=None):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.root = tk, root
        self.q = runner_q or queue.Queue()
        self.runner = Runner(self.q)
        self.sid = load_sid()
        self.last_msg = ""
        self.tools: dict[str, tuple[str, float]] = {}
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

        foot = tk.Frame(root, bg="#f5f6f8")
        foot.pack(side="bottom", fill="x")
        row = tk.Frame(foot, bg="#f5f6f8")
        row.pack(fill="x", padx=6, pady=(4, 0))
        self.btn = {}
        for key, txt, cmd in (("enviar", "Enviar", self.on_send), ("detener", "Detener", self.on_stop),
                              ("nueva", "Nueva conversación", self.on_new), ("gui", "Abrir GUI", self.on_gui),
                              ("apagar", "Apagar solver", self.on_off)):
            b = tk.Button(row, text=txt, command=cmd, font=FONT, padx=6)
            b.pack(side="left", padx=2, pady=2)
            self.btn[key] = b
        self.btn["enviar"].config(bg="#2563eb", fg="white")
        self.entry = tk.Text(foot, height=3, wrap="word", font=FONT, relief="solid", bd=1)
        self.entry.pack(fill="x", padx=6, pady=6)

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
        if self.runner.busy:
            self.say("Espera a que termine o pulsa Detener.", "sys")
            return
        self.last_msg = msg
        self.put(msg + "\n", "user")
        self.entry.delete("1.0", "end")
        self.btn["enviar"].config(state="disabled")
        if not self.runner.send(msg, self.sid):
            self.btn["enviar"].config(state="normal")

    def on_up(self, e):
        if not self.entry.get("1.0", "end").strip() and self.last_msg:
            self.entry.insert("1.0", self.last_msg)
            return "break"

    def on_stop(self):
        if self.runner.busy:
            self.runner.stop()
            self.say("Detenido.", "sys")

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
            save_sid(None)
            self.hist.config(state="normal")
            self.hist.delete("1.0", "end")
            self.hist.config(state="disabled")
            self.btn["enviar"].config(state="normal")
            self.say("Conversación nueva.", "sys")
        self.twice("nueva", self.btn["nueva"], "Nueva conversación", go)

    def on_gui(self):
        self.on_send("Abre la GUI con gui_attach")

    def on_off(self):
        self.twice("apagar", self.btn["apagar"], "Apagar solver", lambda: self.on_send("Guarda y apaga el solver con server_stop"))

    def on_close(self):
        self.runner.stop()
        self.root.destroy()

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
            self.put(f"▶ {ev[2]} … ", "tool")
            self.hist.mark_set("m_" + ev[1], "end-1c")
            self.hist.mark_gravity("m_" + ev[1], "left")
            self.put("\n")
            self.tools[ev[1]] = (ev[2], time.time())
        elif k == "tool_result":
            if ev[1] in self.tools:
                _, t0 = self.tools.pop(ev[1])
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
        elif k == "error":
            self.say(ev[1], "err")
        elif k == "raw":
            self.say(ev[1], "sys")
        elif k == "done":
            self.tools.clear()
            self.btn["enviar"].config(state="normal")

    def pump(self):
        try:
            while True:
                self.handle(self.q.get_nowait())
        except queue.Empty:
            pass
        self.root.after(100, self.pump)

    def refresh(self):
        s = read_status()
        self.status.config(text=s["texto"] + (f"\n⚠ {s['idle_warning']}" if s["idle_warning"] else ""),
                           fg="#8a4b00" if s["idle_warning"] else "#222222")
        self.root.after(3000, self.refresh)


def main():
    import tkinter as tk
    root = tk.Tk()
    Panel(root)
    root.mainloop()


# ---------- selftest ----------
def selftest():
    global ESTADO
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
    os.environ["CLAUDE_CODE_X"] = "1"
    check(not any(k.startswith("CLAUDE") for k in clean_env()), "entorno sin CLAUDE*")

    q: queue.Queue = queue.Queue()
    r = Runner(q)
    r.send("hola", None)
    evs = []
    while True:
        e = q.get(timeout=15)
        evs.append(e)
        if e[0] == "done":
            break
    kinds = [e[0] for e in evs]
    check(kinds[0] == "init" and "tool" in kinds and "tool_result" in kinds and kinds.count("text") == 2
          and "result" in kinds and evs[-1] == ("done", 0, False), f"eventos de la CLI falsa: {kinds}")
    save_sid("fake-0001")
    check(load_sid() == "fake-0001", "guardado de sesión")
    save_sid(None)

    q2: queue.Queue = queue.Queue()
    os.environ["FAKE_PAUSE"] = "5"
    r2 = Runner(q2)
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
    check(s["texto"].startswith("Solver apagado") and s["gui"] == "?", "estado sin archivos")
    (repo / ".server.json").write_text(json.dumps({"pid": os.getpid(), "sim": "C:/x/Bomba1.sim", "idle_warning": "N min sin actividad: ¿apagar?", "gui": True}))
    old = time.time() - 7 * 60
    os.utime(repo / ".server.json", (old, old))
    s = read_status(repo)
    check(s["solver"] and s["caso"] == "Bomba1" and s["idle_min"] == 7 and s["idle_warning"] and s["gui"] is True, f"estado con solver: {s['texto']}")
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
        p = Panel(root)
        p.on_send("hola")
        end = time.time() + 15
        while time.time() < end and not (p.btn["enviar"]["state"] == "normal" and "Listo: 3 partes" in p.hist.get("1.0", "end")):
            root.update()
            time.sleep(0.02)
        txt = p.hist.get("1.0", "end")
        check("▶ Macro: importar los STEP del caso … 0 s ✓" in txt and "Listo: 3 partes" in txt, "ventana: historial con línea de herramienta ✓")
        check(load_sid() == "fake-0001", "ventana: sesión guardada")
        p.agent_text("**fuerte** y `cod`\n| a | b |\n|---|---|")
        check("fuerte y cod" in p.hist.get("1.0", "end"), "markdown renderizado")
        root.destroy()
    print("TODO OK" if ok_all else "HAY FALLAS", f"({time.time() - t0:.1f} s)")
    return 0 if ok_all else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    main()

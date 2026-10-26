"""MCP para operar Simcenter STAR-CCM+ (2410, Windows) desde Claude Code y desde el panel de chat.

Arquitectura: UN servidor de STAR-CCM+ vivo con el .sim en memoria (starccm+ -server -port P). Cada
tool inyecta una macro Java con un cliente batch (`starccm+ -host localhost:P -batch M.java -noexit`)
y lee lo que la macro imprimió con sim.println entre los marcadores AGENT_BEGIN/AGENT_END.
El usuario puede mirar con la GUI real (`gui_attach`: starccm+ -host localhost:P, sin licencia propia).
No se recarga el .sim entre pasos. Claude nunca resuelve física a mano: STAR-CCM+ es el solver.

Tools (15): server_start, server_stop, server_status, gui_attach, gui_status, inspect, run_java, run_steps,
iterate, pump_case, export_scene, save, recipe_search, recipe_save, docs_search.

Turnos: toda inyección toma el candado de archivo `.inject.lock` (pid, herramienta, inicio). Si otro proceso
vivo lo tiene, se espera `wait_s` y si no se devuelve {"ok": false, "busy": "..."}. Huérfano (pid muerto o sin
latido en 2 h) se limpia solo.

Uso:   python mcp_server.py            (stdio, lo lanza Claude Code desde .mcp.json)
       python mcp_server.py --selftest (starccm+ falso de tests/, sin licencia ni solver, < 45 s)
       python mcp_server.py --vigilante <puerto>  (lo lanza server_start; no se llama a mano)

Env (se leen también de .env en la raíz del repo y de ~/.starccm-agent.env, formato KEY=VALUE):
  STARCCM_HOME     carpeta STAR-CCM+<ver> (default: la más nueva en C:\\Program Files\\Siemens)
  STARCCM_EXE      lanzador (default: $STARCCM_HOME/star/bin/starccm+.bat; un .py = falso de pruebas)
  STARCCM_PODKEY   clave Power-on-Demand. NUNCA en código, recetas, logs ni commits (_redact la borra de toda salida).
  STARCCM_LICPATH  servidor de licencias (default 1999@flex.cd-adapco.com)
  STARCCM_PORT     puerto del servidor (default 47827)
  STARCCM_NP       procesos paralelos (default: núcleos - 2)
  STARCCM_IDLE_MIN minutos sin actividad antes de guardar y apagar (default 30; PoD cobra por hora). Lo hace el
                   VIGILANTE (proceso aparte, sobrevive al MCP). Con GUI conectada NO apaga: deja `idle_warning`
                   (texto) en .server.json. STARCCM_VIGILANTE=0 lo desactiva.
  STARCCM_LOCK_WAIT_S segundos que una tool espera el turno (default 120)
  STARCCM_PRECOMPILE 1 (default) compila la macro con el javac del JDK de STAR-CCM+ antes de
                   inyectarla SOLO en la vía batch (fallback): un error de sintaxis o de nombre de clase vuelve en
                   2 s sin tocar el servidor; con caché por hash del cuerpo. 0 para saltarlo. La vía residente
                   no precompila (compila en el propio cliente en ~0,6 s y devuelve el error).
  STARCCM_RESIDENT 1 (default) inyecta con un cliente residente (un solo cliente batch -noexit que vigila un
                   inbox y ejecuta cada macro con StarScript: ~0,7-1 s por macro en vez de ~8,6 s). 0 = siempre
                   el cliente batch de siempre.
  COPILOTO_WORK    carpeta de trabajo pesada FUERA de OneDrive (default %LOCALAPPDATA%\\starccm-copiloto): sims/,
                   macros/, residente/ (inbox, out, latido, log del residente) y tiempos.csv.

Carpetas: en COPILOTO_WORK sims/ (.sim de trabajo, nunca el original del usuario), macros/ (cada macro inyectada,
con su salida .out), residente/, logs del solver (sims/*.server.log), tiempos.csv. En la raíz del repo: casos/<caso>/
(cad, reportes, imágenes, RESUMEN.md), conocimiento/recetas/ (snippets Java que funcionaron), pipelines/ (guiones
fijos). Estado del servidor (.server.json), turnos (.inject.lock) y vigilante-<puerto>.json viven en estado_dir() = la
carpeta de trabajo (fuera de OneDrive); el repo solo guarda panel_estado.json, que dice dónde (el panel lo lee).

Medición: cada respuesta de tool trae `ms` (total) y `via` (residente | batch | mixto | ninguno); además se anota
en WORK/tiempos.csv (fecha, herramienta, ms, via, ok, inyecciones).
"""
from __future__ import annotations

import functools
import glob
import hashlib
import html
import importlib.util
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _work_dir(env: dict | None = None) -> Path:
    """Carpeta de trabajo pesada (fuera de OneDrive: sincronizaría cada .sim de 50-150 MB en cada guardado).
    COPILOTO_WORK manda; si no, %LOCALAPPDATA%\\starccm-copiloto (en otros SO ~/.local/share/starccm-copiloto)."""
    env = os.environ if env is None else env
    if env.get("COPILOTO_WORK"):
        return Path(env["COPILOTO_WORK"])
    base = env.get("LOCALAPPDATA") or (str(Path.home() / ".local" / "share") if os.name != "nt" else str(Path.home() / "AppData" / "Local"))
    return Path(base) / "starccm-copiloto"


WORK = _work_dir()
SIMS = WORK / "sims"
MACROS = WORK / "macros"
RES_DIR = WORK / "residente"
TIEMPOS = WORK / "tiempos.csv"
CASES = HERE / "casos"
RECIPES = HERE / "conocimiento" / "recetas"
PIPELINE_BOMBA = HERE / "pipelines" / "bomba.py"


def estado_dir() -> Path:
    """Carpeta del estado compartido entre procesos (.server.json, .inject.lock, vigilante): la de trabajo, FUERA de OneDrive
    (que bloquea archivos mientras sincroniza y dejaba candados huérfanos). El panel la encuentra por panel_estado.json."""
    return WORK


STATE_FILE = estado_dir() / ".server.json"
LOCK_FILE = estado_dir() / ".inject.lock"
AGENT_LOG = Path(os.environ.get("COPILOTO_AGENT_LOG") or HERE / "agent.log")
PANEL_ESTADO = HERE / "panel_estado.json"  # único archivo de estado que queda en el repo: dice dónde está lo demás
KEEP_MACROS = 200


# ----------------------------------------------------------------------------- configuración
def _load_env_files() -> None:
    """KEY=VALUE de .env (raíz del repo) y ~/.starccm-agent.env; el entorno real manda."""
    for p in (HERE / ".env", Path.home() / ".starccm-agent.env"):
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip().removeprefix("export ").strip()
            v = v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)


_load_env_files()


def _find_home() -> Path | None:
    env = os.environ.get("STARCCM_HOME")
    if env:
        return Path(env)
    hits = sorted(glob.glob(r"C:\Program Files\Siemens\*\STAR-CCM+*"))
    return Path(hits[-1]) if hits else None


HOME = _find_home()
EXE = os.environ.get("STARCCM_EXE") or (str(HOME / "star" / "bin" / "starccm+.bat") if HOME else "starccm+")
PORT = int(os.environ.get("STARCCM_PORT", "47827"))
NP = int(os.environ.get("STARCCM_NP", str(max(1, (os.cpu_count() or 2) - 2))))
IDLE_MIN = float(os.environ.get("STARCCM_IDLE_MIN", "30"))
LICPATH = os.environ.get("STARCCM_LICPATH", "1999@flex.cd-adapco.com")
PRECOMPILE = os.environ.get("STARCCM_PRECOMPILE", "1") == "1"
LOCK_WAIT = float(os.environ.get("STARCCM_LOCK_WAIT_S", "120"))
DOC_DIR = (HOME / "doc" / "client" / "html") if HOME else None


def _starccm_cmd() -> list[str]:
    """Comando base. Un STARCCM_EXE .py es el falso de pruebas (python -I)."""
    if EXE.lower().endswith(".py"):
        return [sys.executable, "-I", EXE]
    return [EXE]


def _lic_flags() -> list[str]:
    key = os.environ.get("STARCCM_PODKEY", "")
    return ["-power", "-podkey", key, "-licpath", LICPATH] if key else []


def _redact(s: str) -> str:
    key = os.environ.get("STARCCM_PODKEY", "")
    return s.replace(key, "***PODKEY***") if key and isinstance(s, str) else s


def _scrub(o):
    """_redact en todas las cadenas de un resultado (dict/list anidados): nada con la clave sale de una tool."""
    if isinstance(o, str):
        return _redact(o)
    if isinstance(o, dict):
        return {k: _scrub(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_scrub(v) for v in o]
    return o


def _javac(real: bool = False) -> tuple[str, str] | None:
    """(javac, classpath) del JDK que trae STAR-CCM+, o None si no está o el exe es el falso. real=True ignora el falso y
    STARCCM_PRECOMPILE (lo usa el selftest para compilar macros reales aunque el solver sea el de mentira)."""
    if not HOME or (not real and (not PRECOMPILE or EXE.lower().endswith(".py"))):
        return None
    jdks = sorted(glob.glob(str(HOME.parent / "jdk" / "*" / "jdk*" / "bin" / "javac.exe")))
    if not jdks:
        return None
    plat = HOME / "star" / "lib" / "java" / "platform"
    cp = os.pathsep.join(str(plat / d / "*") for d in ("modules/ext", "modules", "core", "lib"))
    return jdks[-1], cp


# ----------------------------------------------------------------------------- estado
_lock = threading.RLock()
_last_tool = time.time()


def _state() -> dict:
    """.server.json o {} si no existe. Si existe pero no se puede leer (OneDrive o una escritura a medias lo bloquean
    un instante), reintenta: un {} falso hace creer que no hay solver (así el selftest de la UI apagó uno real)."""
    for i in range(20):
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except Exception:  # noqa: BLE001
            time.sleep(0.05 * (i + 1))
    return {}


def _write_state(d: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_FILE)


def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        # OpenProcess + WaitForSingleObject: microsegundos (tasklist costaba ~0,19 s por sondeo)
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        h = k32.OpenProcess(0x00100000, False, int(pid))  # SYNCHRONIZE
        if not h:
            return False
        try:
            return k32.WaitForSingleObject(h, 0) == 0x102  # WAIT_TIMEOUT: sigue corriendo
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _pid_is_ours(pid: int) -> bool:
    """Evita matar un proceso ajeno si Windows reasignó el PID guardado (el PID es el del .bat/starccm+)."""
    if os.name != "nt":
        return True
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=30).stdout
    m = re.match(r'"([^"]+)"', out.strip())
    return bool(m) and re.search(r"cmd|star|java|python|mpi|ccm|hydra|smpd", m.group(1), re.I) is not None


def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _kill_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=30)
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


# Lanzamiento FUERA del árbol de la CLI. «Detener» y cerrar el panel hacen `taskkill /T /F` sobre el claude.exe, que es el
# padre del MCP: todo hijo (y nieto) vivo del MCP muere con él, solver incluido. Con un proceso intermedio que lanza el
# destino y termina enseguida, el padre del destino queda muerto y ningún árbol que parta de claude.exe lo alcanza.
# CREATE_NEW_PROCESS_GROUP|CREATE_BREAKAWAY_FROM_JOB (el breakaway solo si el job de la CLI lo permite: si no, se reintenta
# sin él) y CREATE_NO_WINDOW (consola oculta PROPIA). OJO: con DETACHED_PROCESS (sin consola) un .bat como starccm+.bat lanza
# hijos de consola que pierden la salida redirigida (medido: el log del servidor quedaba vacío y nunca salía «Server::start»);
# con consola oculta llega todo. COPILOTO_DETACHED=1 cambia CREATE_NO_WINDOW por DETACHED_PROCESS.
_LAUNCHER = r"""
import json, os, subprocess, sys
a = json.loads(sys.stdin.read())
kw = dict(stdin=subprocess.DEVNULL, close_fds=True, cwd=a.get("cwd") or None)
kw["stdout"] = open(a["log"], "ab") if a.get("log") else subprocess.DEVNULL
kw["stderr"] = subprocess.STDOUT
brk = None
if os.name == "nt":
    fl = (0x00000008 if a.get("detached") else 0x08000000) | 0x00000200  # (DETACHED_PROCESS|CREATE_NO_WINDOW) | NEW_PROCESS_GROUP
    try:
        p = subprocess.Popen(a["cmd"], creationflags=fl | 0x01000000, **kw)  # CREATE_BREAKAWAY_FROM_JOB
        brk = True
    except OSError:
        p = subprocess.Popen(a["cmd"], creationflags=fl, **kw)
        brk = False
else:
    p = subprocess.Popen(a["cmd"], start_new_session=True, **kw)
print(json.dumps({"pid": p.pid, "breakaway": brk}), flush=True)
"""


def _spawn_detached(cmd: list[str], log: Path | str | None = None, cwd: Path | str | None = None) -> dict:
    """Lanza `cmd` desacoplado del árbol del MCP (doble lanzamiento). stdout y stderr a `log` (o a la nada), stdin a la nada.
    Devuelve {pid, breakaway}. No espera al destino: el intermedio termina en cuanto lo ha creado."""
    arg = {"cmd": cmd, "log": str(log) if log else None, "cwd": str(cwd) if cwd else None,
           "detached": os.environ.get("COPILOTO_DETACHED") == "1"}
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    p = subprocess.Popen([sys.executable, "-I", "-c", _LAUNCHER], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, creationflags=flags)
    try:
        out, err = p.communicate(json.dumps(arg), timeout=60)
    except subprocess.TimeoutExpired:
        p.kill()
        raise RuntimeError("el lanzador intermedio no respondió en 60 s")
    try:
        return json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise RuntimeError(f"no pude lanzar {Path(cmd[0]).name}: {_redact((err or out or 'sin salida').strip()[-400:])}")


def _write_panel_estado() -> None:
    """panel_estado.json (en el repo) dice al panel dónde están .server.json e .inject.lock. Solo se reescribe si cambia."""
    d = {"estado_dir": str(estado_dir()), "state_file": str(STATE_FILE), "lock_file": str(LOCK_FILE),
         "work": str(WORK), "residente_dir": str(RES_DIR)}
    try:
        if json.loads(PANEL_ESTADO.read_text(encoding="utf-8")) == d:
            return
    except (OSError, ValueError):
        pass
    try:
        tmp = PANEL_ESTADO.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, PANEL_ESTADO)
    except OSError:
        pass


def _migrate_legacy(old_dir: Path, new_state: Path, new_lock: Path) -> list[str]:
    """Antes .server.json e .inject.lock vivían en el repo (OneDrive). Si aún están allí y en la carpeta de estado no,
    se mueven (un solver vivo no se pierde de vista). Devuelve lo movido."""
    moved = []
    for name, dst in ((".server.json", new_state), (".inject.lock", new_lock)):
        src = Path(old_dir) / name
        if src.is_file() and src.resolve() != Path(dst).resolve() and not Path(dst).exists():
            try:
                Path(dst).parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                moved.append(name)
            except OSError:
                pass
    return moved


def alive(st: dict | None = None) -> bool:
    """¿Servidor vivo (proceso y puerto)? La usan las tools y el panel."""
    st = _state() if st is None else st
    return bool(st.get("pid")) and _pid_alive(st["pid"]) and _port_open(st.get("port", 0))


def _touch() -> None:
    """Marca actividad en memoria y en disco (mtime de .server.json): el watchdog de cualquier proceso la ve."""
    global _last_tool
    _last_tool = time.time()
    try:
        os.utime(STATE_FILE, None)
    except OSError:
        pass


# ----------------------------------------------------------------------------- turnos entre procesos
_turn_depth = 0  # re-entrada dentro de este proceso (una tool llama a otras); protegido por _lock


def _read_lock() -> tuple[bool, dict | None, float]:
    """(existe, contenido o None si ilegible, edad en s desde el último latido)."""
    try:
        age = time.time() - LOCK_FILE.stat().st_mtime
    except OSError:
        return False, None, 0.0
    try:
        info = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
        return True, (info if isinstance(info, dict) else None), age
    except Exception:  # noqa: BLE001
        return True, None, age


def _lock_orphan(info: dict | None, age: float) -> bool:
    """Huérfano: dueño muerto, o sin latido en 2 h (ponytail: una sola macro de >2 h con el dueño vivo se limpiaría)."""
    if not info:
        return age > 5  # recién creado y aún sin escribir: no es huérfano; basura vieja sí
    pid = int(info.get("pid") or 0)
    if pid == os.getpid():
        return _turn_depth == 0  # es mío pero no lo estoy usando: resto de un fallo anterior
    return (not _pid_alive(pid)) or age > 2 * 3600


def lock_holder() -> dict | None:
    """Quién tiene el turno ahora ({pid, tool, start, seconds}) o None. Solo lectura: la usan el panel y server_status."""
    exists, info, age = _read_lock()
    if not exists or _lock_orphan(info, age):
        return None
    info = info or {}
    return {"pid": info.get("pid"), "tool": info.get("tool", "?"), "start": info.get("start"),
            "seconds": round(time.time() - info.get("t", time.time()))}


def _acquire(tool: str, wait_s: float) -> dict | None:
    """Toma .inject.lock (O_EXCL). None si lo logra; si no, el dueño (dict) tras esperar wait_s."""
    deadline = time.time() + max(0.0, wait_s)
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)  # la carpeta de estado puede no existir aún (primer uso)
    while True:
        try:
            fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError:  # existe (o Windows lo tiene a medio borrar)
            fd = None
        if fd is not None:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"pid": os.getpid(), "tool": tool, "start": datetime.now().isoformat(timespec="seconds"),
                           "t": time.time()}, f)
            return None
        exists, info, age = _read_lock()
        if exists and _lock_orphan(info, age):
            _log(f"turno: limpio candado huérfano {info}")
            if _read_lock()[1] == info:  # si cambió, otro proceso ya lo limpió o lo tomó
                try:
                    LOCK_FILE.unlink(missing_ok=True)
                except OSError:
                    pass
            continue
        if time.time() >= deadline:
            return lock_holder() or {"pid": None, "tool": "?", "seconds": 0}
        time.sleep(0.2)


def _release() -> None:
    exists, info, _ = _read_lock()
    if exists and info and info.get("pid") == os.getpid():
        try:
            LOCK_FILE.unlink(missing_ok=True)
        except OSError:
            pass


def _touch_lock() -> None:
    """Latido: una macro larga del dueño vivo no se confunde con un huérfano."""
    try:
        os.utime(LOCK_FILE, None)
    except OSError:
        pass


@contextmanager
def _turn(tool: str, wait_s: float | None = None):
    """Turno del solver. Rinde None si lo tiene (re-entrante en este proceso) o el dueño actual si sigue ocupado."""
    global _turn_depth
    with _lock:
        if _turn_depth == 0:
            holder = _acquire(tool, LOCK_WAIT if wait_s is None else wait_s)
            if holder:
                yield holder
                return
        _turn_depth += 1
        try:
            yield None
        finally:
            _turn_depth -= 1
            if _turn_depth == 0:
                _release()


def _busy_msg(h: dict) -> str:
    return f"el solver lo usa «{h.get('tool')}» (pid {h.get('pid')}) desde hace {h.get('seconds')} s; reintenta luego"


def _busy_result(h: dict) -> dict:
    """Resultado de _inject cuando no hay turno: mismas claves que uno normal, para que los llamadores no cambien."""
    return {"ok": False, "busy": _busy_msg(h), "holder": h, "stdout": "", "stderr": "", "exception": "ocupado: " + _busy_msg(h),
            "trace": None, "seconds": 0, "macro": None, "returncode": None}


def _busy_only(h: dict) -> dict:
    return {"ok": False, "busy": _busy_msg(h), "holder": h}


# ----------------------------------------------------------------------------- inactividad
def last_activity() -> float | None:
    """Última actividad medida en DISCO (la ven todos los procesos: MCP de Claude Code, MCP del chat, panel):
    la macro más reciente (.java o su .out, que crece en vivo mientras itera) o .server.json."""
    ts = []
    if MACROS.is_dir():
        ts += [p.stat().st_mtime for p in MACROS.glob("Agent_*") if p.is_file()]
    if STATE_FILE.is_file():
        ts.append(STATE_FILE.stat().st_mtime)
        # (el log del solver NO cuenta: el simple sondeo del puerto, que hace el propio vigilante, ya escribe una línea en él)
    return max(ts) if ts else None


STALE_MACRO_H = 24  # una macro «sin terminar» más vieja que esto se da por abandonada (cliente muerto)


def _macro_finished(out: Path) -> bool:
    """La salida de una macro terminada trae AGENT_END, o «--- stderr» si el cliente falló."""
    try:
        with open(out, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 20_000))
            t = f.read().decode("utf-8", "replace")
    except OSError:
        return True
    return "AGENT_END" in t or t.startswith("--- stderr") or "\n--- stderr" in t


def macro_running() -> bool:
    """¿Hay una macro en marcha (en cualquier proceso)? Su .out aún no tiene AGENT_END y su cliente (Agent_*.pid)
    sigue vivo; sin .pid, solo si el .out es de hace menos de 15 min. Una macro callada (malla de media hora) no
    escribe en disco: sin esto el watchdog apagaría el solver a mitad de trabajo."""
    now = time.time()
    outs = []
    for o in MACROS.glob("Agent_*.out") if MACROS.is_dir() else []:
        try:
            outs.append((o.stat().st_mtime, o))
        except OSError:
            pass
    for mt, o in sorted(outs, reverse=True)[:8]:
        age = now - mt
        if age > STALE_MACRO_H * 3600 or _macro_finished(o):
            continue
        pf = o.with_suffix(".pid")
        if pf.is_file():
            try:
                if _pid_alive(int(pf.read_text().strip())):
                    return True
            except (OSError, ValueError):
                pass
        elif age < 900:
            return True
    return False


def idle_disk_seconds() -> float:
    """Inactividad medida SOLO en disco (0 con una macro en marcha): lo que ven todos los procesos."""
    if macro_running():
        return 0.0
    return time.time() - (last_activity() or 0)


def idle_seconds() -> float:
    return min(idle_disk_seconds(), time.time() - _last_tool)


def _publish_idle(st: dict, gui: bool, idle_min: float, warning: str | None) -> None:
    """Escribe gui, idle_min, idle_warning (TEXTO legible) e idle_checked en .server.json SIN contar como actividad
    (se restaura el mtime). No toca nada si el servidor ya es otro o ya se apagó."""
    try:
        s = STATE_FILE.stat()
    except OSError:
        return
    cur = _state()
    if not cur.get("pid") or cur.get("pid") != st.get("pid"):
        return
    cur.update(gui=bool(gui), idle_min=idle_min, idle_checked=datetime.now().isoformat(timespec="seconds"))
    if warning:
        cur["idle_warning"] = warning
    else:
        cur.pop("idle_warning", None)
    _write_state(cur)
    os.utime(STATE_FILE, ns=(s.st_atime_ns, s.st_mtime_ns))


def idle_warning_text(mins: int) -> str:
    return f"solver encendido {mins} min sin actividad; GUI conectada: no se apaga"


def _idle_stop() -> str:
    """Un chequeo de inactividad (lo hace el vigilante cada 30 s): publica gui/idle_min/idle_warning en .server.json y,
    sin GUI y pasado STARCCM_IDLE_MIN, guarda y apaga. Solo mira el DISCO (macros en marcha o recientes y .server.json), que es
    lo que ven todos los procesos. Devuelve "sin_servidor" | "ok" | "aviso" | "apagado" | "no_apagado"."""
    st = _state()
    if not (st.get("pid") and alive(st)):
        return "sin_servidor"
    idle = idle_disk_seconds()
    mins = int(idle // 60)
    over = idle > IDLE_MIN * 60
    gui = bool(_gui(fresh=True)["connected"])
    if over and gui:  # alguien mira: no se apaga, se avisa (el panel lo muestra)
        _publish_idle(st, gui, round(idle / 60, 1), idle_warning_text(mins))
        return "aviso"
    _publish_idle(st, gui, round(idle / 60, 1), None)
    if not over:
        return "ok"
    with _lock:  # si una tool de este proceso tenía el candado, se reevalúa al liberarse
        if idle_disk_seconds() <= IDLE_MIN * 60:
            return "ok"
        r = server_stop(save=True)
        if r.get("save_error") or r.get("busy"):
            _log(f"vigilante: no apago: {r.get('save_error') or r.get('busy')}")
            return "no_apagado"
        _log(f"vigilante: solver apagado por {mins} min sin actividad (guardado: {r.get('saved')})")
        return "apagado"


# ----------------------------------------------------------------------------- vigilante independiente
# Un proceso pequeño (`python mcp_server.py --vigilante <puerto>`) lanzado por server_start FUERA del árbol de la CLI. En el
# panel cada turno es un `claude -p` nuevo que arranca y mata su MCP: un hilo del MCP no vigila nada entre turnos y PoD
# cobra por hora. El vigilante vive mientras viva el servidor: cada VIGILANTE_S (30 s) hace _idle_stop(); termina cuando
# el servidor muere. Uno por puerto: vigilante-<puerto>.json en la carpeta de estado (pid + latido).
VIGILANTE_S = float(os.environ.get("COPILOTO_VIGILANTE_S", "30"))
VIGILANTE_ON = os.environ.get("STARCCM_VIGILANTE", "1") != "0"
VIG_GONE_S = float(os.environ.get("COPILOTO_VIG_GONE_S", "3"))  # s seguidos sin servidor antes de terminar (un server_start que reinicia no lo mata)


def _vig_file(port: int) -> Path:
    return estado_dir() / f"vigilante-{port}.json"


def _vig_info(port: int) -> dict:
    try:
        d = json.loads(_vig_file(port).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _vigilante_pid(port: int) -> int | None:
    """Pid del vigilante vivo de ese puerto (proceso vivo y latido reciente) o None."""
    d = _vig_info(port)
    pid = int(d.get("pid") or 0)
    if pid and pid != os.getpid() and _pid_alive(pid) and time.time() - float(d.get("t") or 0) < 3 * VIGILANTE_S + 30:
        return pid
    return None


def _vigilante_ensure(port: int) -> dict:
    """Garantiza UN vigilante para el puerto: si no hay, lo lanza desacoplado. No espera a que arranque."""
    if not VIGILANTE_ON:
        return {"vigilante": "desactivado"}
    pid = _vigilante_pid(port)
    if pid:
        return {"vigilante": pid, "nuevo": False}
    py = HERE / ".venv" / "Scripts" / "python.exe"
    cmd = [str(py) if py.is_file() else sys.executable, str(Path(__file__).resolve()), "--vigilante", str(port)]
    try:
        estado_dir().mkdir(parents=True, exist_ok=True)
        sp = _spawn_detached(cmd, log=estado_dir() / f"vigilante-{port}.log", cwd=estado_dir())
    except Exception as e:  # noqa: BLE001
        _log(f"vigilante: no pude lanzarlo: {e}")
        return {"vigilante": None, "error": str(e)[:200]}
    _log(f"vigilante lanzado pid {sp['pid']} (puerto {port}, breakaway {sp.get('breakaway')})")
    return {"vigilante": sp["pid"], "nuevo": True}


def _vigilante_main(port: int) -> int:
    me = os.getpid()
    if _vigilante_pid(port):
        return 0  # ya hay uno
    f = _vig_file(port)

    def beat() -> None:
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps({"pid": me, "port": port, "t": time.time(), "every_s": VIGILANTE_S}), encoding="utf-8")
        os.replace(tmp, f)

    beat()
    _log(f"vigilante {me} vigila el puerto {port} (cada {VIGILANTE_S:g} s, apaga a los {IDLE_MIN:g} min sin GUI)")
    last, gone_since = 0.0, None
    try:
        while True:
            st = _state()
            up = bool(st.get("pid")) and st.get("port") == port and _pid_alive(st["pid"])
            if up:
                gone_since = None
            else:
                gone_since = gone_since or time.time()
                if time.time() - gone_since >= VIG_GONE_S:
                    break
            if up and time.time() - last >= VIGILANTE_S:
                last = time.time()
                try:
                    beat()
                    _idle_stop()
                except Exception as e:  # noqa: BLE001
                    _log(f"vigilante: {type(e).__name__}: {e}")
            time.sleep(min(1.0, VIGILANTE_S))
    finally:
        if _vig_info(port).get("pid") == me:
            f.unlink(missing_ok=True)
    _log(f"vigilante {me} termina: el servidor del puerto {port} ya no está")
    return 0


def start_watchdog() -> None:
    """Al arrancar el MCP: si ya hay un solver vivo (de otra sesión o de antes), asegura su vigilante."""
    try:
        st = _state()
        if st.get("pid") and alive(st):
            _vigilante_ensure(int(st["port"]))
    except Exception as e:  # noqa: BLE001
        _log(f"watchdog: {e}")


def _log(msg: str) -> None:
    with open(AGENT_LOG, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {_redact(msg)}\n")


# ----------------------------------------------------------------------------- GUI conectada
GUI_TTL = 10.0  # s de caché del listado de procesos (nativo ~20 ms; el respaldo de PowerShell ~1,6 s)
_gui_cache: tuple[float, dict] | None = None


def _procs_native(needle: str) -> list[dict]:
    """Procesos de Windows cuya línea de comandos contiene `needle`, SIN lanzar PowerShell ni wmic (~1,6 s por llamada):
    CreateToolhelp32Snapshot para listarlos y la línea de comandos leída del PEB de cada uno (ctypes, ~20 ms). Los que no
    se pueden abrir (de otro usuario o del sistema) se saltan. Lanza si algo falla: el llamador cae a PowerShell."""
    import ctypes
    from ctypes import wintypes as w
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        raise OSError("solo Python de 64 bits")
    k32, nt = ctypes.WinDLL("kernel32", use_last_error=True), ctypes.WinDLL("ntdll")

    class PE(ctypes.Structure):
        _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ProcessID", w.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", w.DWORD), ("cntThreads", w.DWORD), ("th32ParentProcessID", w.DWORD),
                    ("pcPriClassBase", w.LONG), ("dwFlags", w.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    class PBI(ctypes.Structure):
        _fields_ = [("Reserved1", ctypes.c_void_p), ("PebBaseAddress", ctypes.c_void_p), ("Reserved2", ctypes.c_void_p * 2),
                    ("UniqueProcessId", ctypes.c_void_p), ("Reserved3", ctypes.c_void_p)]

    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    k32.Process32FirstW.argtypes = k32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PE)]
    k32.ReadProcessMemory.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    nt.NtQueryInformationProcess.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == ctypes.c_void_p(-1).value:
        raise OSError("CreateToolhelp32Snapshot")
    rows = []

    def rpm(h, addr, n):
        buf = ctypes.create_string_buffer(n)
        got = ctypes.c_size_t(0)
        if not k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, n, ctypes.byref(got)) or got.value != n:
            raise OSError("ReadProcessMemory")
        return buf.raw

    try:
        pe = PE()
        pe.dwSize = ctypes.sizeof(PE)
        ok = k32.Process32FirstW(snap, ctypes.byref(pe))
        while ok:
            pid, name = pe.th32ProcessID, pe.szExeFile
            ok = k32.Process32NextW(snap, ctypes.byref(pe))
            if pid in (0, 4):
                continue
            h = k32.OpenProcess(0x1000 | 0x0010, False, pid)  # QUERY_LIMITED_INFORMATION | VM_READ
            if not h:
                h = k32.OpenProcess(0x0400 | 0x0010, False, pid)  # QUERY_INFORMATION | VM_READ
            if not h:
                continue
            try:
                pbi = PBI()
                if nt.NtQueryInformationProcess(h, 0, ctypes.byref(pbi), ctypes.sizeof(pbi), None) != 0 or not pbi.PebBaseAddress:
                    continue
                params = int.from_bytes(rpm(h, pbi.PebBaseAddress + 0x20, 8), "little")  # PEB.ProcessParameters
                us = rpm(h, params + 0x70, 16)  # RTL_USER_PROCESS_PARAMETERS.CommandLine (UNICODE_STRING)
                length, buf = int.from_bytes(us[:2], "little"), int.from_bytes(us[8:16], "little")
                if not length or not buf or length > 65_000:
                    continue
                cmd = rpm(h, buf, length).decode("utf-16-le", "replace")
                if needle in cmd:
                    rows.append({"pid": pid, "name": name, "cmd": cmd})
            except OSError:
                continue
            finally:
                k32.CloseHandle(h)
    finally:
        k32.CloseHandle(snap)
    return rows


def _list_procs() -> list[dict]:
    """Procesos con «-host» en la línea de comandos: [{pid, name, cmd}]. [] si no se puede listar.
    STARCCM_TEST_PROCS=<archivo.json> sustituye el listado (gancho de pruebas: el vigilante es otro proceso)."""
    hook = os.environ.get("STARCCM_TEST_PROCS")
    if hook:
        try:
            return json.loads(Path(hook).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
    try:
        if os.name == "nt":
            try:
                return _procs_native("-host")
            except Exception:  # noqa: BLE001  (sin ctypes/PEB: PowerShell, una sola llamada)
                pass
            ps = ("Get-CimInstance Win32_Process -Filter \"CommandLine like '%-host%'\" | "
                  "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress")
            out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True,
                                 text=True, encoding="utf-8", errors="replace", timeout=30).stdout
            data = json.loads(out) if out.strip() else []
            data = [data] if isinstance(data, dict) else data
            return [{"pid": d["ProcessId"], "name": d.get("Name") or "", "cmd": d.get("CommandLine") or ""} for d in data]
        out = subprocess.run(["ps", "-eo", "pid=,comm=,args="], capture_output=True, text=True, timeout=15).stdout
        rows = []
        for line in out.splitlines():
            parts = line.split(None, 2)
            if len(parts) == 3 and "-host" in parts[2]:
                rows.append({"pid": int(parts[0]), "name": parts[1], "cmd": parts[2]})
        return rows
    except Exception:  # noqa: BLE001
        return []


def _gui_clients(rows: list[dict], port: int) -> list[int]:
    """Pids de clientes GUI: starccm+ con «-host …:PORT», sin -batch (cliente de macro) ni -server."""
    pids = []
    for r in rows:
        c = r.get("cmd", "")
        low = c.lower()
        if ("starccm" in low and re.search(rf"-host\s+\S+:{port}\b", c) and "-batch" not in low
                and "-server" not in low and not r.get("name", "").lower().startswith("powershell")):
            pids.append(r["pid"])
    return pids


def _gui(fresh: bool = False) -> dict:
    global _gui_cache
    if not fresh and _gui_cache and time.time() - _gui_cache[0] < GUI_TTL:
        return _gui_cache[1]
    port = _state().get("port") or PORT
    pids = _gui_clients(_list_procs(), port)
    res = {"connected": bool(pids), "pids": pids, "port": port}
    _gui_cache = (time.time(), res)
    return res


# ----------------------------------------------------------------------------- macro java
IMPORTS = [
    "star.common.*", "star.base.neo.*", "star.base.report.*", "star.meshing.*", "star.flow.*",
    "star.kwturb.*", "star.vis.*", "star.cadmodeler.*", "star.prismmesher.*", "star.surfacewrapper.*",
    "star.material.*", "star.segregatedflow.*", "star.turbulence.*", "star.motion.*", "star.metrics.*",
    "star.resurfacer.*", "star.dualmesher.*",
    "java.util.*", "java.io.*",
]

POLL_S = 1.0  # s entre sondeos al arrancar/apagar/esperar la GUI (el selftest lo baja)


def _wrap(body: str, imports: str, tag: str) -> str:
    # (defensa: «['a', 'b']» de un str(lista) daba «import 'a';» y la macro no compilaba: se quitan comillas y corchetes)
    extra = [i.strip().strip("[]'\" ").rstrip(";").removeprefix("import ").strip()
             for i in imports.replace("\n", ",").split(",") if i.strip().strip("[]'\" ")]
    imps = "\n".join(f"import {i};" for i in IMPORTS + [e for e in extra if e not in IMPORTS])
    cls = f"Agent_{tag}"
    return f"""{imps}

public class {cls} extends StarMacro {{
  public void execute() {{
    Simulation sim = getActiveSimulation();
    sim.println("AGENT_BEGIN {tag}");
    try {{
{body}
    }} catch (Exception e) {{
      StringWriter sw = new StringWriter();
      e.printStackTrace(new PrintWriter(sw));
      sim.println("AGENT_ERROR: " + e);
      sim.println(sw.toString());
    }}
    sim.println("AGENT_END {tag}");
  }}
}}
"""


def _prune_macros() -> None:
    files = sorted(MACROS.glob("Agent_*"), key=lambda p: p.stat().st_mtime)
    for p in files[:-KEEP_MACROS]:
        p.unlink(missing_ok=True)


def _run_javac(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)


def _precompile(src: Path, key: str = "") -> str | None:
    """Compila la macro con javac (solo vía batch). Devuelve el texto del error o None si compila (o no hay javac).
    key = hash del cuerpo: si ya compiló una vez no se recompila lo mismo (la clase cambia de nombre, el cuerpo no)."""
    jc = _javac()
    if not jc:
        return None
    mark = MACROS / ".cache" / key if key else None
    if mark and mark.is_file():
        return None
    out = MACROS / ".classes"
    out.mkdir(exist_ok=True)
    r = _run_javac([jc[0], "-nowarn", "-proc:none", "-cp", jc[1], "-d", str(out), str(src)])
    if r.returncode != 0:
        return (r.stderr or r.stdout)[-3000:]
    if mark:
        mark.parent.mkdir(exist_ok=True)
        mark.write_text("", encoding="utf-8")
    return None


# AutoExport del 3D en vivo: apagado por defecto; si una receta lo activa, cada >= AUTOEXPORT_MIN_IT iteraciones
# (exportar superficies cada 50 it frena la corrida).
AUTOEXPORT_MIN_IT = 100
_AE_FREQ = re.compile(r"(getIterationUpdateFrequency\(\)\s*\.setIterations\(\s*)(\d+)(\s*\))")


def _guard_autoexport(body: str) -> tuple[str, list[str]]:
    """Sube a AUTOEXPORT_MIN_IT cualquier frecuencia literal menor en un cuerpo que usa AutoExport."""
    if "getAutoExport" not in body:
        return body, []
    warns = []

    def fix(m):
        if int(m.group(2)) < AUTOEXPORT_MIN_IT:
            warns.append(f"AutoExport cada {m.group(2)} it subido a {AUTOEXPORT_MIN_IT} (exportar más seguido frena la corrida)")
            return f"{m.group(1)}{AUTOEXPORT_MIN_IT}{m.group(3)}"
        return m.group(0)
    return _AE_FREQ.sub(fix, body), warns


def _inject(body: str, imports: str = "", timeout: float = 3600, expect_death: bool = False,
            wait_s: float | None = None, tool: str = "inyección") -> dict:
    """Toma el turno, escribe la macro, la ejecuta (residente por defecto, cliente batch de respaldo) y devuelve su
    salida. Sin turno tras wait_s: {"ok": False, "busy": ...} (con las mismas claves que un resultado normal)."""
    with _turn(tool, wait_s) as busy:
        if busy:
            return _busy_result(busy)
        return _inject_locked(body, imports, timeout, expect_death)


def _inject_locked(body: str, imports: str, timeout: float, expect_death: bool) -> dict:
    st = _state()
    if not st.get("pid") or not _pid_alive(st["pid"]):
        raise RuntimeError("No hay servidor de STAR-CCM+ vivo. Usa server_start primero.")
    if st.get("init") == "pendiente":  # server_start no la corre (no espera al residente): la primera inyección la hace antes
        _run_init(st)
        st = _state()
    if not _port_open(st.get("port", 0)):
        raise RuntimeError(f"El proceso del servidor (pid {st['pid']}) sigue pero no responde en el puerto {st.get('port')}: "
                           "si acaba de arrancar espera; si está colgado, server_stop() lo mata.")
    _touch_lock()
    MACROS.mkdir(parents=True, exist_ok=True)
    body, warns = _guard_autoexport(body)
    key = hashlib.sha1((imports + "\0" + body).encode("utf-8", "replace")).hexdigest()[:20]
    tag = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    src = MACROS / f"Agent_{tag}.java"
    src.write_text(_wrap(body, imports, tag), encoding="utf-8")
    t0 = time.time()
    why = None
    if RES_ON:
        r, why = _inject_resident(st, src, tag, t0, timeout, expect_death)
        if r is not None:
            _note_via("residente")
            return {**r, "via": "residente", **({"warnings": warns} if warns else {})}
        _log(f"inyección por batch (residente no disponible): {why}")
    _note_via("batch", why)
    err_c = _precompile(src, key)
    if err_c:
        # el .out marca la macro como terminada (no se da por «en marcha»)
        src.with_suffix(".out").write_text(_redact(f"javac: no compila\n{err_c}\n--- stderr\n"), encoding="utf-8")
        _touch()
        return {"ok": False, "stdout": "", "stderr": "", "exception": "javac: no compila", "via": "batch",
                "trace": _redact(err_c), "seconds": round(time.time() - t0, 2), "macro": str(src), "returncode": 1}
    rc, err, killed, out_file = _inject_local(st, src, timeout)
    _touch_lock()
    r = _inject_result(st, src, tag, t0, rc, err, killed, out_file, expect_death)
    return {**r, "via": "batch", **({"via_fallback": why} if why else {}), **({"warnings": warns} if warns else {})}


# ----------------------------------------------------------------------------- cliente residente
# Un único cliente batch (-batch Residente.java -noexit) lanzado una vez; vigila RES_DIR/inbox y ejecuta cada .java con
# StarScript (compila y corre dentro del cliente, sin otra JVM: ~0,7-1 s por macro frente a ~8,6 s). Su stdout (la salida de
# sim.println) va a un log por servidor; el MCP recorta de ahí lo que hay entre AGENT_BEGIN/AGENT_END <tag>. Un hilo
# aparte escribe `heartbeat` cada ~1 s. Estado en RES_DIR/residente.json (lo ven todos los procesos MCP).
RES_ON = os.environ.get("STARCCM_RESIDENT", "1") != "0"
RES_HB_STALE = 5.0        # s sin latido => muerto (antes de enviar una macro)
RES_BUSY_STALE = 60.0     # s sin latido => colgado, MIENTRAS corre una macro (más holgado: no matar un cliente ocupado)
RES_START_WAIT = 60.0     # s esperando el primer latido tras lanzarlo
RES_POLL = 0.03
RES_PAUSE_S = 300.0       # tras fallar el residente dos veces, el batch directo durante este tiempo
_res_off_until = 0.0
_res_launches = 0
_res_waits = 0            # veces que una inyección esperó el primer latido de un residente recién lanzado (lo prueba el selftest)

_RESIDENTE_JAVA = """import star.common.*;
import java.io.*;
import java.nio.file.*;
// Cliente residente del copiloto (generado por mcp_server.py). Vigila inbox/, ejecuta cada .java con StarScript y escribe
// out/<macro>.res (primera linea OK|ERR ms; luego el error). Latido en un hilo aparte. STOP en inbox/ lo termina.
public class Residente extends StarMacro {
  public void execute() {
    final File base = new File("%BASE%");
    final File inbox = new File(base, "inbox"), out = new File(base, "out"), hb = new File(base, "heartbeat");
    final boolean[] run = {true};
    Thread t = new Thread(new Runnable() { public void run() {
      while (run[0]) {
        try { Files.write(hb.toPath(), Long.toString(System.currentTimeMillis()).getBytes("UTF-8")); } catch (Exception e) { }
        try { Thread.sleep(1000); } catch (InterruptedException e) { return; }
      } } });
    t.setDaemon(true);
    t.start();
    System.out.println("RESIDENTE listo");
    long fin = System.currentTimeMillis() + 12L * 3600 * 1000;
    while (System.currentTimeMillis() < fin) {
      if (new File(inbox, "STOP").exists()) break;
      File[] fs = inbox.listFiles(new FilenameFilter() { public boolean accept(File d, String n) { return n.endsWith(".java"); } });
      if (fs != null && fs.length > 0) {
        java.util.Arrays.sort(fs);
        for (File f : fs) {
          long t0 = System.nanoTime();
          String err = "";
          try { new StarScript(getActiveRootObject(), f).play(); }
          catch (Throwable e) { StringWriter sw = new StringWriter(); e.printStackTrace(new PrintWriter(sw)); err = sw.toString(); }
          double ms = (System.nanoTime() - t0) / 1e6;
          System.out.println("RESIDENTE_FIN " + f.getName());
          System.out.flush();
          System.err.flush();
          try {
            File tmp = new File(out, f.getName() + ".tmp");
            Files.write(tmp.toPath(), ((err.isEmpty() ? "OK " : "ERR ") + ms + "\\n" + (err.length() > 8000 ? err.substring(0, 8000) : err)).getBytes("UTF-8"));
            Files.move(tmp.toPath(), new File(out, f.getName() + ".res").toPath(), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
          } catch (Exception e) { }
          f.delete();
        }
      }
      try { Thread.sleep(30); } catch (InterruptedException e) { break; }
    }
    run[0] = false;
    System.out.println("RESIDENTE fin");
  }
}
"""


def _res_info() -> dict:
    try:
        d = json.loads((RES_DIR / "residente.json").read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _hb_age() -> float:
    try:
        return time.time() - (RES_DIR / "heartbeat").stat().st_mtime
    except OSError:
        return 1e9


def _res_healthy(st: dict) -> bool:
    ri = _res_info()
    return (bool(ri.get("pid")) and ri.get("server_pid") == st.get("pid") and _pid_alive(ri["pid"])
            and _hb_age() < RES_HB_STALE)


def _res_kill(why: str) -> None:
    """Mata el residente (nunca el servidor) y borra su estado."""
    ri = _res_info()
    if ri.get("pid") and _pid_alive(ri["pid"]):
        _log(f"residente {ri['pid']} matado: {why}")
        _kill_tree(ri["pid"])
    (RES_DIR / "residente.json").unlink(missing_ok=True)
    (RES_DIR / "heartbeat").unlink(missing_ok=True)


def _res_stop(wait: float = 8.0) -> bool:
    """Detiene el residente con el archivo STOP; si no sale en `wait` s lo mata. True si había uno vivo."""
    ri = _res_info()
    pid = ri.get("pid")
    had = bool(pid) and _pid_alive(pid)
    if had:
        try:
            (RES_DIR / "inbox").mkdir(parents=True, exist_ok=True)
            (RES_DIR / "inbox" / "STOP").write_text("", encoding="utf-8")
        except OSError:
            pass
        end = time.time() + wait
        while time.time() < end and _pid_alive(pid):
            time.sleep(0.1)
    _res_kill("parada")
    return had


def _res_spawn(st: dict) -> dict:
    """Lanza el residente desacoplado (fuera del árbol de la CLI) y vuelve SIN esperar su latido. Devuelve lo que
    escribe en residente.json (pid, server_pid, log, t de lanzamiento)."""
    global _res_launches
    inbox, out = RES_DIR / "inbox", RES_DIR / "out"
    for d in (inbox, out):
        d.mkdir(parents=True, exist_ok=True)
        for f in d.glob("*"):
            f.unlink(missing_ok=True)
    (RES_DIR / "heartbeat").unlink(missing_ok=True)
    java = RES_DIR / "Residente.java"
    java.write_text(_RESIDENTE_JAVA.replace("%BASE%", _jpath(RES_DIR)), encoding="utf-8")
    log = RES_DIR / f"residente-{st.get('port')}-{st.get('pid')}.log"
    cmd = _starccm_cmd() + ["-host", f"localhost:{st['port']}", "-batch", str(java), "-noexit"] + _lic_flags()
    t_launch = time.time()
    sp = _spawn_detached(cmd, log=log, cwd=RES_DIR)
    _res_launches += 1
    info = {"pid": sp["pid"], "server_pid": st.get("pid"), "port": st.get("port"), "log": str(log), "t": t_launch,
            "started": datetime.now().isoformat(timespec="seconds"), "breakaway": sp.get("breakaway")}
    (RES_DIR / "residente.json").write_text(json.dumps(info), encoding="utf-8")
    _log(f"residente lanzado pid {sp['pid']} (servidor {st.get('pid')}): {_redact(' '.join(cmd))}")
    return info


def _res_wait_beat(info: dict) -> tuple[bool, str]:
    """Espera el primer latido de un residente recién lanzado. (ok, motivo del fallo)."""
    global _res_waits
    _res_waits += 1
    t_launch, pid, log = info.get("t", time.time()), info["pid"], Path(info.get("log") or "")
    while time.time() - t_launch < RES_START_WAIT:
        if not _pid_alive(pid):
            try:
                tail = _redact(log.read_text(encoding="utf-8", errors="replace")[-600:])
            except OSError:
                tail = ""
            return False, f"el residente terminó al arrancar: {tail}"
        try:
            if (RES_DIR / "heartbeat").stat().st_mtime >= t_launch - 0.5:
                return True, ""
        except OSError:
            pass
        time.sleep(0.05)
    return False, f"el residente no dio latido en {RES_START_WAIT:.0f} s"


def _res_launch(st: dict) -> tuple[bool, str]:
    """Lanza el residente y espera su primer latido. (ok, motivo del fallo)."""
    return _res_wait_beat(_res_spawn(st))


def _res_booting(ri: dict, st: dict) -> bool:
    """¿Residente de ESTE servidor, vivo, lanzado hace poco y aún sin su primer latido? (server_start no lo espera)"""
    if not (ri.get("pid") and ri.get("server_pid") == st.get("pid") and _pid_alive(ri["pid"])):
        return False
    t = ri.get("t") or 0
    if time.time() - t >= RES_START_WAIT:
        return False
    try:
        return (RES_DIR / "heartbeat").stat().st_mtime < t - 0.5
    except OSError:
        return True


def _res_ensure(st: dict) -> tuple[bool, str]:
    """Residente sano para este servidor: lo reutiliza, espera al que está arrancando, o lo (re)lanza una vez más si falla."""
    global _res_off_until
    if time.time() < _res_off_until:
        return False, "residente en pausa tras fallar dos veces (se reintenta en unos minutos)"
    if _res_healthy(st):
        return True, ""
    ri = _res_info()
    if _res_booting(ri, st):  # lanzado por server_start sin esperar: la primera inyección espera su latido
        ok, why = _res_wait_beat(ri)
        if ok:
            return True, ""
        _res_kill(why)
    elif ri.get("pid"):
        _res_kill("sin latido, muerto o de otro servidor")
    why = ""
    for _ in range(2):  # lanzar y, si falla, una vez más
        ok, why = _res_launch(st)
        if ok:
            return True, ""
        _res_kill(why)
    _res_off_until = time.time() + RES_PAUSE_S
    return False, why


def _copy_live(log: Path, pos: int, out_file: Path) -> tuple[int, str]:
    """Copia lo nuevo del log del residente al .out de la macro (el panel lo sigue en vivo). (pos nuevo, texto nuevo)."""
    try:
        if log.stat().st_size <= pos:
            return pos, ""
        with open(log, "rb") as f:
            f.seek(pos)
            data = f.read()
    except OSError:
        return pos, ""
    if not data:
        return pos, ""
    txt = data.decode("utf-8", "replace")
    with open(out_file, "a", encoding="utf-8", errors="replace") as fo:
        fo.write(_redact(txt))
    return pos + len(data), txt


def _inject_resident(st: dict, src: Path, tag: str, t0: float, timeout: float,
                     expect_death: bool) -> tuple[dict | None, str]:
    """Ejecuta la macro con el residente. (resultado, "") o (None, motivo) si no se pudo y toca el batch.
    Una vez enviada la macro NUNCA se repite por batch (podría duplicar efectos): los errores vuelven como resultado."""
    ok, why = _res_ensure(st)
    if not ok:
        return None, why
    ri = _res_info()
    rpid, log = ri["pid"], Path(ri["log"])
    name = src.name
    resf = RES_DIR / "out" / (name + ".res")
    resf.unlink(missing_ok=True)
    out_file, pidf = src.with_suffix(".out"), src.with_suffix(".pid")
    out_file.write_text("", encoding="utf-8")
    pidf.write_text(str(rpid))  # macro_running() lo lee desde otros procesos
    pos = log.stat().st_size if log.is_file() else 0
    tmpf = RES_DIR / "inbox" / (name + ".tmp")
    tmpf.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    os.replace(tmpf, RES_DIR / "inbox" / name)  # el residente solo ve *.java: nunca una macro a medias
    acc, res, killed, died, last_chk, last_pid = "", None, False, False, 0.0, 0.0
    deadline = time.time() + timeout  # tras comprobar el residente: lanzarlo no gasta el tiempo de la macro
    try:
        while True:
            if resf.is_file():
                try:
                    res = resf.read_text(encoding="utf-8", errors="replace")
                    break
                except OSError:
                    pass
            now = time.time()
            if now > deadline:
                killed = True
                break
            if now - last_pid > 0.1:  # barato (microsegundos): detecta pronto un residente muerto
                last_pid = now
                if not _pid_alive(rpid) or _hb_age() > RES_BUSY_STALE:
                    died = True
                    break
            if now - last_chk > 0.4:
                last_chk = now
                pos, new = _copy_live(log, pos, out_file)
                acc += new
                _touch_lock()
                if expect_death and not alive(st):  # (cada 0,4 s)
                    break  # sim.kill(): el servidor se fue, no habrá resultado
            time.sleep(RES_POLL)
        if killed or died:
            reason = (f"timeout {timeout}s" if killed else "el residente murió o dejó de dar latido") + \
                     "; residente matado (el servidor sigue vivo) y relanzado"
            _res_kill(reason)
            (RES_DIR / "inbox" / name).unlink(missing_ok=True)
            relaunch = ""
            if alive(st):
                try:
                    ok2, relaunch = _res_launch(st)
                    relaunch = "" if ok2 else f" (no pude relanzarlo: {relaunch})"
                except Exception as e:  # noqa: BLE001
                    relaunch = f" (no pude relanzarlo: {e})"
            pos, new = _copy_live(log, pos, out_file)
            acc += new
            err = reason + relaunch
        else:
            err = ""
            pos, new = _copy_live(log, pos, out_file)
            acc += new
            if res is not None:
                head, _, rest = res.partition("\n")
                if head.startswith("ERR"):
                    err = rest.strip() or "error en el residente"
            if res is not None and not err:  # el latido y el stdout van por canales distintos: espera el AGENT_END
                end = time.time() + 3
                while _between(acc, tag) is None and time.time() < end:
                    time.sleep(0.1)
                    pos, new = _copy_live(log, pos, out_file)
                    acc += new
    finally:
        pidf.unlink(missing_ok=True)
    rc = 0 if not (killed or died or err) else (-1 if killed else 1)
    r = _inject_result(st, src, tag, t0, rc, err, killed, out_file, expect_death)
    if died:
        r["resident_died"] = True
    if err and not killed and not died and _between(acc, tag) is None and not r.get("exception"):
        # error de compilación (o de ejecución antes de AGENT_BEGIN): lo devuelve el propio residente
        r["exception"] = "el cliente residente no pudo compilar/ejecutar la macro: " + err.strip().splitlines()[0][:300]
        r["trace"] = _redact(err[-3000:])
    if (killed or died) and not r.get("exception"):
        r["exception"] = err
    return r, ""


def _inject_local(st: dict, src: Path, timeout: float) -> tuple[int, str, bool, Path]:
    killed = False
    cmd = _starccm_cmd() + ["-host", f"localhost:{st['port']}", "-batch", str(src), "-noexit"] + _lic_flags()
    # El stdout del cliente va EN VIVO a <macro>.out (el panel lo sigue mientras itera);
    # una macro está en marcha mientras su .out no contiene AGENT_END.
    out_file = src.with_suffix(".out")
    with open(out_file, "w", encoding="utf-8", errors="replace") as fo:
        p = subprocess.Popen(cmd, stdout=fo, stderr=subprocess.PIPE, text=True, cwd=str(MACROS),
                             encoding="utf-8", errors="replace")
        pidf = src.with_suffix(".pid")  # lo lee macro_running() desde otros procesos
        pidf.write_text(str(p.pid))
        try:
            _, err = p.communicate(timeout=timeout)
            rc = p.returncode
        except subprocess.TimeoutExpired:
            # subprocess.run solo mataba cmd.exe y esperaba al java hijo: se mata el árbol entero
            if p.poll() is None:
                _kill_tree(p.pid)
            try:
                _, e2 = p.communicate(timeout=15)
            except Exception:  # noqa: BLE001
                e2 = ""
            rc, err = -1, f"timeout {timeout}s: cliente batch (pid {p.pid}) matado; la salida puede estar incompleta. {e2 or ''}"
            killed = True
        finally:
            pidf.unlink(missing_ok=True)
    return rc, err, killed, out_file


def _inject_result(st: dict, src: Path, tag: str, t0: float, rc: int, err: str, killed: bool, out_file: Path,
                   expect_death: bool = False) -> dict:
    out = out_file.read_text(encoding="utf-8", errors="replace")
    secs = round(time.time() - t0, 2)
    # Salida de la macro: entre marcadores, del stdout del cliente; si no, del log del servidor.
    text = _between(out, tag)
    if text is None:
        text = _between(_tail_log(st), tag)
    err_match = re.search(r"AGENT_ERROR: (.*)", text or out)
    # sin AGENT_END el .out se cierra con «--- stderr» (así macro_running() lo da por terminado)
    out_file.write_text(_redact(out + ("\n--- stderr\n" + err if (err or text is None) else "")), encoding="utf-8")
    _prune_macros()
    _touch()
    died = text is None and not killed and not expect_death and not alive(st)
    if died:
        _log(f"servidor {st.get('pid')} murió durante la macro {src.name} (rc {rc})")
    return {
        "ok": rc == 0 and not err_match and text is not None,
        "stdout": _redact(text) if text is not None else _redact(out[-4000:]),
        "stderr": _redact(err[-2000:]) if err else "",
        "exception": _redact(err_match.group(1).strip()) if err_match else (
            "El servidor de STAR-CCM+ murió o dejó de responder durante la macro; lo no guardado se perdió. "
            "Relánzalo con server_start()." if died else None),
        "trace": _redact(_trace(text)) if err_match else (_redact(_tail_log(st, 3000)) if died else None),
        "seconds": secs,
        "macro": str(src),
        "returncode": rc,
        **({"client_killed": True} if killed else {}),
        **({"server_died": True} if died else {}),
    }


def _between(out: str, tag: str) -> str | None:
    m = re.search(rf"AGENT_BEGIN {tag}\r?\n(.*?)AGENT_END {tag}", out or "", re.S)
    return m.group(1).strip() if m else None


def _trace(text: str) -> str:
    m = re.search(r"AGENT_ERROR: .*?\n(.*)", text or "", re.S)
    return (m.group(1).strip()[:3000]) if m else ""


def _tail_log(st: dict, n: int = 200_000) -> str:
    p = Path(st.get("log") or "")
    if not p.is_file():
        return ""
    with open(p, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - n))
        return f.read().decode("utf-8", "replace")


# ----------------------------------------------------------------------------- MCP
if "--vigilante" in sys.argv:  # el vigilante no sirve MCP: no paga el import (~1,7 s)
    class FastMCP:  # type: ignore
        def __init__(self, *a, **k):
            pass

        def tool(self, *a, **k):
            return lambda fn: fn
else:
    try:
        from mcp.server.fastmcp import FastMCP  # mcp 1.x
    except ImportError:  # mcp 2.x
        from mcp.server.mcpserver import MCPServer as FastMCP  # type: ignore

mcp = FastMCP("starccm")


_ctx = threading.local()  # contexto de la tool en curso en este hilo: profundidad, vías usadas, motivos de fallback


def _note_via(via: str, reason: str | None = None) -> None:
    seen = getattr(_ctx, "seen", None)
    if seen is not None:
        seen.append(via)
        if reason:
            _ctx.reasons.append(reason)


def _record_time(tool: str, ms: int, via: str, ok, n: int) -> None:
    """Una fila por tool (solo la de primer nivel) en WORK/tiempos.csv, para comparar residente y batch."""
    try:
        TIEMPOS.parent.mkdir(parents=True, exist_ok=True)
        new = not TIEMPOS.exists()
        with open(TIEMPOS, "a", encoding="utf-8", newline="") as f:
            if new:
                f.write("fecha,herramienta,ms,via,ok,inyecciones\n")
            f.write(f"{datetime.now():%Y-%m-%dT%H:%M:%S},{tool},{ms},{via},{ok},{n}\n")
    except OSError:
        pass


def _tool(fn):
    """Registra la tool en el MCP. Todo lo que devuelve pasa por _scrub (la clave PoD nunca sale) y, en la tool de primer
    nivel, lleva `ms` (total) y `via` (residente | batch | mixto | ninguno); una lista o un texto se envuelven en
    {"resultado": ...}. La fila va a tiempos.csv. Las tools que llaman a otras (pump_case -> server_start) no duplican."""
    @functools.wraps(fn)
    def wrapper(*a, **k):
        depth = getattr(_ctx, "depth", 0)
        if depth == 0:
            _ctx.seen, _ctx.reasons = [], []
        _ctx.depth = depth + 1
        t0 = time.perf_counter()
        try:
            res = _scrub(fn(*a, **k))
        except Exception:
            if depth == 0:
                _record_time(fn.__name__, round((time.perf_counter() - t0) * 1000), _via_label(), False, len(_ctx.seen))
            raise
        finally:
            _ctx.depth = depth
        if depth:
            return res
        ms = round((time.perf_counter() - t0) * 1000)
        if not isinstance(res, dict):
            res = {"resultado": res}
        via = _via_label()
        res = {**res, "ms": ms, "via": via}
        if _ctx.reasons:
            res["via_fallback"] = "; ".join(sorted(set(_ctx.reasons)))
        _record_time(fn.__name__, ms, via, res.get("ok", "error" not in res), len(_ctx.seen))
        return res
    mcp.tool()(wrapper)
    return wrapper


def _via_label() -> str:
    v = set(_ctx.seen)
    return "ninguno" if not v else v.pop() if len(v) == 1 else "mixto"


_INIT_AUTOSAVE = ("AutoSave as = sim.getSimulationIterator().getAutoSave();\n"
                  "as.setAutoSaveBatch(false); as.setAutoSaveMesh(false);\n"
                  # el AutoExport del 3D en vivo arranca apagado (una receta puede activarlo, con frecuencia >= 100)
                  "try { sim.getSimulationIterator().getAutoExport().setEnabled(false); } catch (Throwable e) { }\n"
                  'sim.println("INIT autosave off, autoexport off");\n')


def _run_init(st: dict) -> None:
    """Primera macro tras arrancar: apaga el autoguardado en batch (en 2410 viene activo y deja <sim>@meshed.sim y
    @NNNNN.sim, gigas en una bomba). Su resultado queda en st["init"]; no tumba el arranque."""
    _write_state({**st, "init": "en curso"})  # (la inyección de abajo vuelve a pasar por _inject_locked: no repetir)
    try:
        res = _inject(_INIT_AUTOSAVE, timeout=600, tool="server_start")["stdout"][-200:]
    except Exception as e:  # noqa: BLE001
        res = f"init falló: {e}"
    cur = _state()
    if cur.get("pid") == st.get("pid"):
        _write_state({**cur, "init": res})
        _log(f"init: {res}")


def _save_macro(sp: str) -> str:
    """Java que guarda el .sim en `sp` (ya escapado con _jstr) y lo anuncia."""
    return f'sim.saveState("{sp}");\nsim.println("SAVED " + "{sp}");\n'


def _try_save(sp: str) -> tuple[bool, str | None]:
    """El guardado va en su propia macro: si falla, el servidor sigue vivo y el trabajo no se pierde."""
    try:
        r = _inject(_save_macro(sp), timeout=1800, tool="save")
        return bool(r["ok"] and "SAVED" in (r["stdout"] or "")), r.get("exception") or r.get("stderr") or r.get("stdout")
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def _save_failed(err) -> dict:
    _log(f"server_stop: guardado falló, servidor sigue vivo: {err}")
    return {"stopped": False, "saved": None, "save_error": str(err)[:500],
            "reason": "no se pudo guardar; el servidor sigue vivo (save=False apaga sin guardar)"}


@_tool
def server_start(sim_path: str = "", name: str = "") -> dict:
    """Levanta el servidor de STAR-CCM+ (o reutiliza el vivo). sim_path: .sim a abrir; se copia a
    sims/<name>.sim y se trabaja sobre la copia, nunca sobre el original. Vacío = simulación nueva.
    Devuelve host:port, pid, sim y segundos de arranque. Un solo servidor a la vez."""
    with _lock:
        _touch()
        # un nombre con separadores, comillas o espacios no puede ser el de un archivo ni ir a un literal Java
        name = re.sub(r'[\\/:*?"<>|\s]+', "_", name.strip()).strip("._") if name else ""
        st = _state()
        if st.get("pid") and alive(st):
            want = (name or (Path(sim_path).stem if sim_path else "")).strip()
            have = Path(st["sim"]).stem if st.get("sim") else ""
            if want and want != have:  # otro caso cargado: no trabajar sobre la simulación equivocada
                raise RuntimeError(f"Hay un servidor vivo con otro .sim cargado ({have or 'sin .sim'}). "
                                   f"Guarda y apágalo con server_stop() antes de abrir {want}.")
            _write_panel_estado()
            vig = _vigilante_ensure(int(st["port"])).get("vigilante")  # un solver vivo de antes también queda vigilado
            return {"reused": True, **st, "vigilante": vig}
        if st.get("pid"):
            if _pid_alive(st["pid"]) and _pid_is_ours(st["pid"]):
                # proceso vivo con el puerto cerrado: otro proceso (panel o chat) lo está arrancando, o está colgado
                try:
                    age = (datetime.now() - datetime.fromisoformat(st["started"])).total_seconds()
                except (KeyError, ValueError, TypeError):
                    age = 1e9
                if age < 600:
                    raise RuntimeError(f"Otro proceso está arrancando el servidor (pid {st['pid']}, hace {age:.0f} s): "
                                       "espera a que abra el puerto y vuelve a llamar a server_start.")
                _log(f"servidor {st['pid']} colgado (puerto cerrado tras {age:.0f} s); lo mato")
                _kill_tree(st["pid"])
            _log(f"servidor {st.get('pid')} muerto; limpio .server.json")
            STATE_FILE.unlink(missing_ok=True)
        if not EXE.lower().endswith(".py") and not (Path(EXE).is_file() or shutil.which(EXE)):
            raise RuntimeError(f"No encuentro el lanzador de STAR-CCM+ ({EXE}); define STARCCM_HOME o STARCCM_EXE en .env")
        if _port_open(PORT, timeout=0.25):  # antes de copiar el .sim (158 MB): no se copia para nada (un puerto libre en Windows tarda todo el timeout)
            raise RuntimeError(f"Puerto {PORT} ocupado por otro proceso (¿un solver huérfano? mira `netstat -ano | findstr :{PORT}`); "
                               "cambia STARCCM_PORT o libéralo.")
        _res_kill("servidor nuevo")  # un residente de un servidor anterior no sirve
        SIMS.mkdir(parents=True, exist_ok=True)
        sim = None
        if sim_path:
            srcp = Path(sim_path)
            if not srcp.is_file():
                raise FileNotFoundError(f"No existe {sim_path}")
            dst = SIMS / f"{name or srcp.stem}.sim"
            if srcp.resolve() != dst.resolve():
                shutil.copy2(srcp, dst)
            sim = dst
        elif name:
            sim = SIMS / f"{name}.sim"  # se creará con save()
        log = SIMS / f"{(sim.stem if sim else 'nuevo')}.server.log"
        cmd = _starccm_cmd() + ["-server", "-port", str(PORT), "-np", str(NP)] + _lic_flags()
        if sim and sim.is_file():
            cmd += ["-load", str(sim)]
        t0 = time.time()
        sp = _spawn_detached(cmd, log=log, cwd=SIMS)  # fuera del árbol de la CLI: «Detener» del panel no lo alcanza
        spid = sp["pid"]
        st = {"pid": spid, "port": PORT, "host": "localhost", "sim": str(sim) if sim else None,
              "log": str(log), "started": datetime.now().isoformat(timespec="seconds"), "np": NP,
              "cmd": _redact(" ".join(cmd))}
        _write_state(st)
        _write_panel_estado()
        deadline = t0 + 600
        while time.time() < deadline:
            if not _pid_alive(spid):
                tail = _redact(_tail_log(st, 6000))
                STATE_FILE.unlink(missing_ok=True)
                hint = "\nFalta STARCCM_PODKEY en .env (sin clave pide licencia a flex y aborta)." if not os.environ.get("STARCCM_PODKEY") else ""
                raise RuntimeError(f"El servidor terminó a los {time.time()-t0:.0f}s. Cola del log:\n{tail[-3000:]}{hint}")
            if _port_open(PORT) and "Server::start" in _tail_log(st, 20000):
                break
            time.sleep(min(POLL_S, 0.25))
        else:
            _kill_tree(spid)
            STATE_FILE.unlink(missing_ok=True)
            raise RuntimeError("El servidor no abrió el puerto en 600 s")
        st["startup_seconds"] = round(time.time() - t0, 1)
        # El residente se lanza y NO se espera (~10 s en serie antes): la primera inyección espera su latido si hace falta y
        # corre antes la macro de init (autoguardado y AutoExport apagados). (Simulation no tiene setPresentationName: lo da save().)
        st["init"] = "pendiente"
        _write_state(st)
        if RES_ON and time.time() >= _res_off_until:
            try:
                _res_spawn(st)
            except Exception as e:  # noqa: BLE001
                _log(f"residente: no pude lanzarlo en server_start: {e}")
        st["vigilante"] = _vigilante_ensure(PORT).get("vigilante")
        if not alive(st):
            raise RuntimeError(f"El servidor murió justo al arrancar. Cola del log:\n{_redact(_tail_log(st, 3000))}")
        _log(f"servidor pid={st['pid']} puerto={PORT} sim={sim} en {st['startup_seconds']}s; residente lanzado sin esperar")
        return st


@_tool
def server_stop(save: bool = True) -> dict:
    """Guarda (si save) y apaga el servidor. Limpia .server.json. Si hay GUI conectada AVISA (gui_warning) pero apaga:
    pregunta antes al usuario."""
    with _turn("server_stop") as busy:
        if busy:
            return {"stopped": False, **_busy_only(busy)}
        _touch()
        st = _state()
        if not st.get("pid"):
            _res_stop(3.0)
            return {"stopped": False, "reason": "no había servidor"}
        result = {"stopped": True, "saved": None}
        if _pid_alive(st["pid"]) and _port_open(st["port"]):
            g = _gui()
            if g["connected"]:
                result["gui_warning"] = (f"había GUI conectada (pid {g['pids']}): al apagar el servidor la GUI se desconecta; "
                                         "el usuario debe elegir «desconectar»")
            if save:  # sin .sim registrado (simulación nueva sin nombre) se guarda aparte: apagar no pierde el trabajo
                sim = st.get("sim") or str(SIMS / f"nuevo_{datetime.now():%Y%m%d_%H%M%S}.sim")
                ok, err = _try_save(_jstr(_jpath(sim)))
                if not ok:
                    return {**_save_failed(err), **({"gui_warning": result["gui_warning"]} if "gui_warning" in result else {})}
                result["saved"] = sim
                if (cp := mirror_sim(sim)):
                    result["copia"] = cp
            try:
                r = _inject("sim.kill();\n", timeout=1800, expect_death=True, tool="server_stop")
                result["stdout"] = r["stdout"]
            except Exception as e:  # noqa: BLE001
                result["kill_error"] = str(e)
            for _ in range(60):
                if not _pid_alive(st["pid"]):
                    break
                time.sleep(POLL_S)
        if _res_stop(3.0):  # el residente (archivo STOP) no sobrevive al servidor
            result["residente"] = "detenido"
        if _pid_alive(st["pid"]):
            if _pid_is_ours(st["pid"]):
                _kill_tree(st["pid"])
                result["forced"] = True
            else:  # PID reasignado a otro proceso: no se toca, solo se limpia el estado
                result["pid_reused"] = True
        STATE_FILE.unlink(missing_ok=True)
        _log(f"servidor {st['pid']} apagado {result}")
        return result


@_tool
def server_status() -> dict:
    """Estado del servidor: pid, puerto, sim, vivo o no, minutos de inactividad, GUI conectada, turno (quién tiene
    el solver) e idle_warning si lo hay."""
    st = _state()
    a = alive(st)
    out = {"alive": a, "idle_min": round(idle_seconds() / 60, 1), "turno": lock_holder(), **st}
    if a:
        g = _gui()
        out["gui"] = g["connected"]
        out["gui_pids"] = g["pids"]
    return out


@_tool
def gui_status() -> dict:
    """¿Hay una GUI de STAR-CCM+ conectada al servidor? (proceso starccm+ con -host localhost:PORT y sin -batch)."""
    st = _state()
    return {**_gui(fresh=True), "server_alive": alive(st)}


@_tool
def gui_attach(wait_s: int = 0) -> dict:
    """Abre la GUI real de STAR-CCM+ conectada al servidor vivo (starccm+ -host localhost:PORT, proceso separado,
    sin -podkey ni licencia propia). El árbol y el panel Output muestran lo que hace el agente. Nunca cierra una GUI.
    wait_s: segundos que espera a verla conectada (la GUI tarda ~30 s en abrir)."""
    st = _state()
    if not alive(st):
        return {"ok": False, "error": "no hay servidor vivo: usa server_start primero"}
    g = _gui(fresh=True)
    if g["connected"]:
        return {"ok": True, "already": True, **g}
    cmd = _starccm_cmd() + ["-host", f"localhost:{st['port']}"]  # sin _lic_flags(): la GUI no reserva licencia
    SIMS.mkdir(parents=True, exist_ok=True)
    gpid = _spawn_detached(cmd, cwd=SIMS)["pid"]  # fuera del árbol de la CLI: la GUI del usuario sobrevive a «Detener»
    _log(f"gui_attach: lanzada pid {gpid}: {_redact(' '.join(cmd))}")
    deadline = time.time() + max(0, wait_s)
    g = {"connected": False, "pids": []}
    while time.time() < deadline:
        time.sleep(POLL_S)
        g = _gui(fresh=True)
        if g["connected"]:
            break
    return {"ok": True, "launched": gpid, "connected": g["connected"], "pids": g["pids"],
            "note": "" if g["connected"] else "la GUI tarda ~30 s en conectar; confirma con gui_status()"}


def _jpath(p: str | os.PathLike) -> str:
    return str(p).replace("\\", "/")


def _jstr(s: str) -> str:
    """Escapa texto para un literal Java entre comillas (no se puede cerrar el string ni meter saltos)."""
    return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")


@_tool
def run_java(body: str, imports: str = "", timeout_s: int = 3600, wait_s: int = -1) -> dict:
    """Ejecuta el cuerpo de execute() en el servidor. `sim` (Simulation) ya existe. Lo que se imprime
    con sim.println vuelve en stdout. imports: extra separados por coma (p.ej. "star.energy.*").
    Si falla, devuelve exception y trace. Si otro proceso tiene el turno del solver: espera wait_s (-1 = el
    default, 120 s) y devuelve {"ok": false, "busy": ...}. Antes de inventar Java: recipe_search y docs_search."""
    return _inject(body, imports, timeout=timeout_s, wait_s=None if wait_s < 0 else wait_s, tool="run_java")


# ----------------------------------------------------------------------------- run_steps
def _jval(v) -> str:
    return ("true" if v else "false") if isinstance(v, bool) else str(v)


def _subst(body: str, params: dict) -> str:
    """Sustituye ${nombre} SOLO para las claves de params (las expresiones de STAR-CCM+ también usan ${Flujo},
    ${HReport}…: esas no se tocan). Los valores van tal cual: un texto para un literal Java va ya escapado."""
    for k, v in (params or {}).items():
        body = body.replace("${" + str(k) + "}", _jval(v))
    return body


def _recipe_file(name: str) -> Path | None:
    stem = _safe(name.removesuffix(".java"))
    for cand in (stem, stem.lower()):
        p = RECIPES / f"{cand}.java"
        if p.is_file():
            return p
    return None


def _build_steps(steps: list, stop_on_error: bool) -> tuple[str, list[str], list[str], str | None]:
    """(cuerpo Java, etiquetas, avisos, error). Cada paso va en su bloque y su try/catch con STEP_BEGIN/STEP_END."""
    if not isinstance(steps, list) or not steps:
        return "", [], [], "steps debe ser una lista no vacía de {recipe, params} o {java}"
    labels, warns, parts = [], [], ["boolean _stop = false;"]
    for i, s in enumerate(steps):
        if not isinstance(s, dict) or ("recipe" in s) == ("java" in s):
            return "", [], [], f"paso {i}: debe tener exactamente uno de «recipe» o «java»"
        params = s.get("params") or {}
        if "recipe" in s:
            p = _recipe_file(str(s["recipe"]))
            if not p:
                return "", [], [], f"paso {i}: no existe la receta «{s['recipe']}» en {RECIPES}"
            body, label = p.read_text(encoding="utf-8", errors="replace"), p.name
            if "no verificado" in body.lower():
                warns.append(f"paso {i}: la receta {p.name} está marcada «no verificado»")
            m = re.search(r"^//\s*PARAMS:\s*(.+)$", body, re.M)  # parámetros obligatorios declarados en la cabecera
            need = [x for x in re.split(r"[,\s]+", m.group(1).strip()) if x] if m else []
            miss = [x for x in need if x not in params]
            if miss:
                return "", [], [], f"paso {i} ({p.name}): faltan parámetros {miss}"
        else:
            body, label = str(s["java"]), str(s.get("name") or "java")
        unused = [k for k in params if "${" + str(k) + "}" not in body]
        if unused:
            warns.append(f"paso {i} ({label}): parámetros sin uso en el cuerpo: {unused}")
        body = _subst(body, params)
        labels.append(label)
        parts.append(f"""// ---- paso {i}: {label}
if (_stop) {{ sim.println("STEP_SKIPPED {i}"); }} else {{
  long _t{i} = System.currentTimeMillis();
  sim.println("STEP_BEGIN {i}");
  try {{
    {{
{body}
    }}
    sim.println("STEP_OK {i}");
  }} catch (Throwable _e{i}) {{
    StringWriter _sw{i} = new StringWriter();
    _e{i}.printStackTrace(new PrintWriter(_sw{i}));
    sim.println("STEP_ERROR {i}: " + _e{i});
    sim.println(_sw{i}.toString());
    _stop = {'true' if stop_on_error else 'false'};
  }}
  sim.println("STEP_END {i} " + (System.currentTimeMillis() - _t{i}));
}}""")
    return "\n".join(parts), labels, warns, None


def _parse_steps(stdout: str, labels: list[str] | None = None) -> list[dict]:
    """Lee STEP_* de la salida de una macro: [{i, name, ok, skipped, stdout, error, trace, seconds}]."""
    steps: dict[int, dict] = {}
    cur = None
    in_trace = False
    for line in (stdout or "").splitlines():
        m = re.match(r"STEP_(BEGIN|OK|ERROR|END|SKIPPED) (\d+)(?::| |$)(.*)", line)
        if m:
            kind, i, rest = m.group(1), int(m.group(2)), m.group(3).strip()
            name = labels[i] if labels and i < len(labels) else f"paso {i}"
            if kind == "SKIPPED":
                steps[i] = {"i": i, "name": name, "ok": None, "skipped": True}
            elif kind == "BEGIN":
                cur = steps[i] = {"i": i, "name": name, "ok": None, "skipped": False, "stdout": "", "_open": True}
                in_trace = False
            elif cur and cur["i"] == i:
                if kind == "OK":
                    cur["ok"] = True
                elif kind == "ERROR":
                    cur.update(ok=False, error=rest, trace="")
                    in_trace = True
                elif kind == "END":
                    cur["_open"] = False
                    if cur["ok"] is None:
                        cur["ok"] = True  # cerró sin STEP_ERROR (los pipelines pueden no imprimir STEP_OK)
                    if rest.isdigit():
                        cur["seconds"] = round(int(rest) / 1000, 2)
                    cur = None
                    in_trace = False
            continue
        if cur is not None:
            if in_trace:
                cur["trace"] = (cur["trace"] + "\n" + line).strip()[:2000]
            else:
                cur["stdout"] = (cur["stdout"] + "\n" + line).strip()
    out = []
    for i in range(max([*steps, len(labels or []) - 1], default=-1) + 1):
        s = steps.get(i) or {"i": i, "name": labels[i] if labels and i < len(labels) else f"paso {i}", "ok": None,
                             "skipped": True, "not_run": True}
        if s.pop("_open", False):
            s.update(ok=False, error=s.get("error") or "la macro se cortó antes del STEP_END")
        out.append(s)
    return out


@_tool
def run_steps(steps: list[dict], stop_on_error: bool = True, timeout_s: int = 3600, imports: str = "",
              wait_s: int = -1) -> dict:
    """Varios pasos en UNA macro (ahorra los 8-10 s de arranque del cliente Java por paso). steps = lista de
    {"recipe": "bomba-regiones", "params": {"rpm": 1450}} o {"java": "cuerpo", "name": "etiqueta", "params": {...}}.
    En el cuerpo, ${nombre} se sustituye por params[nombre] (solo las claves dadas; ${Flujo} y otras expresiones de
    STAR-CCM+ quedan intactas; una receta puede exigir parámetros con «// PARAMS: a, b» en su cabecera). Cada paso va
    en su try/catch y devuelve ok/stdout/error/seconds. stop_on_error: tras un fallo el resto se salta
    (skipped); False los intenta todos. ponytail: un error de javac no dice qué paso; los pasos comparten un solo
    método Java (límite de 64 KB de bytecode)."""
    body, labels, warns, err = _build_steps(steps, stop_on_error)
    if err:
        return {"ok": False, "error": err}
    with _turn("run_steps", None if wait_s < 0 else wait_s) as busy:
        if busy:
            return _busy_only(busy)
        r = _inject(body, imports, timeout=timeout_s, tool="run_steps")
    res = _parse_steps(r["stdout"], labels)
    failed = next((s["i"] for s in res if s.get("ok") is False), None)
    return {"ok": bool(r["ok"]) and failed is None and all(s.get("ok") for s in res if not s.get("skipped")),
            "steps": res, "failed_at": failed, "seconds": r["seconds"], "macro": r["macro"],
            **({"warnings": warns} if warns else {}),
            **({"exception": r["exception"], "trace": r["trace"]} if r["exception"] else {}),
            **({"stdout": r["stdout"][-1500:]} if not res or all(s.get("not_run") for s in res) else {})}


_INSPECT = {
    "regions": """
for (Region r : sim.getRegionManager().getRegions()) {
  sim.println("REGION\\t" + r.getPresentationName() + "\\t" + r.getRegionType().getPresentationName());
  for (Boundary b : r.getBoundaryManager().getBoundaries())
    sim.println("BOUNDARY\\t" + r.getPresentationName() + "/" + b.getPresentationName() + "\\t" + b.getBoundaryType().getPresentationName());
}
for (Interface i : sim.getInterfaceManager().getObjects())
  sim.println("INTERFACE\\t" + i.getPresentationName() + "\\t" + i.getClass().getSimpleName());
""",
    "physics": """
for (Continuum c : sim.getContinuumManager().getObjects()) {
  sim.println("CONTINUUM\\t" + c.getPresentationName() + "\\t" + c.getClass().getSimpleName());
  if (c instanceof PhysicsContinuum)
    for (Model m : ((PhysicsContinuum) c).getModelManager().getObjects())
      sim.println("MODEL\\t" + c.getPresentationName() + "/" + m.getPresentationName() + "\\t" + m.getClass().getSimpleName());
}
""",
    "mesh_ops": """
for (MeshOperation op : sim.get(MeshOperationManager.class).getObjects())
  sim.println("MESH_OP\\t" + op.getPresentationName() + "\\t" + op.getClass().getSimpleName());
""",
    "parts": """
for (GeometryPart p : sim.get(SimulationPartManager.class).getParts()) {
  sim.println("PART\\t" + p.getPresentationName() + "\\t" + p.getClass().getSimpleName());
  for (PartSurface s : p.getPartSurfaces())
    sim.println("SURFACE\\t" + p.getPresentationName() + "/" + s.getPresentationName() + "\\t");
}
""",
    "reports": """
for (Report rep : sim.getReportManager().getObjects()) {
  String v;
  try { v = String.valueOf(rep.getReportMonitorValue()); } catch (Exception e) { v = "n/a"; }
  sim.println("REPORT\\t" + rep.getPresentationName() + "\\t" + rep.getClass().getSimpleName() + "\\t" + v);
}
""",
    "monitors": """
for (Monitor m : sim.getMonitorManager().getObjects())
  sim.println("MONITOR\\t" + m.getPresentationName() + "\\t" + m.getClass().getSimpleName());
""",
    "scenes": """
for (Scene s : sim.getSceneManager().getScenes())
  sim.println("SCENE\\t" + s.getPresentationName() + "\\t");
""",
    "solver": """
sim.println("ITERATION\\t" + sim.getSimulationIterator().getCurrentIteration() + "\\t");
sim.println("SIM\\t" + sim.getPresentationName() + "\\t" + sim.getSessionPath());
for (Solver s : sim.getSolverManager().getObjects())
  sim.println("SOLVER\\t" + s.getPresentationName() + "\\t" + s.getClass().getSimpleName());
""",
}
# Cada sección en su propio try/catch: un gestor ausente (p.ej. MeshOperationManager en una
# simulación vacía) avisa con WARN y no tumba el resto.
_INSPECT = {k: "try {\n" + v + "\n} catch (Exception e) { sim.println(\"WARN\\t" + k
            + "\\t\" + String.valueOf(e.getMessage()).replace('\\n', ' ')); }"
            for k, v in _INSPECT.items()}
_INSPECT["tree"] = "\n".join(_INSPECT[k] for k in ("solver", "parts", "regions", "physics", "mesh_ops", "reports", "monitors", "scenes"))




@_tool
def inspect(path: str = "tree") -> dict:
    """Lee un nodo del árbol de la simulación: tree (todo), regions, physics, mesh_ops, parts,
    reports, monitors, scenes, solver. JSON con nombres, tipos y valores principales."""
    key = (path or "tree").strip().lower()
    if key not in _INSPECT:
        return {"error": f"nodo desconocido '{path}'", "nodos": sorted(_INSPECT)}
    r = _inject(_INSPECT[key], tool="inspect")
    items: dict[str, list] = {}
    for line in r["stdout"].splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].isupper():
            items.setdefault(parts[0].lower(), []).append(
                {"name": parts[1], "type": parts[2] if len(parts) > 2 else "",
                 **({"value": parts[3]} if len(parts) > 3 else {})})
    return {"ok": r["ok"], "path": key, "seconds": r["seconds"], **items, **_err(r)}


def _err(r: dict) -> dict:
    """Campos de error/ocupado de un resultado de _inject, para copiarlos a la respuesta de una tool."""
    return {**({"busy": r["busy"]} if r.get("busy") else {}),
            **({"exception": r["exception"], "trace": r["trace"]} if r.get("exception") else {})}


# ----------------------------------------------------------------------------- iterate
_iter_hist: list[dict] = []  # [{it, vals: {reporte: valor}}] muestras de fin de bloque; se reinicia si cambia el servidor
_iter_pid = None


def _iter_block(k: int, timeout_s: float) -> dict:
    """Corre k iteraciones y lee reportes y residuales. Devuelve {ok, iteration, reports, residuals, res_rows, ...}."""
    # Residuales: exportando el plot "Residuals" a CSV (MonitorManager.export falla en batch 2410:
    # "StarUpdate unexpectedly missing"). Reportes: valor directo de cada Report.
    csv = MACROS / "residuals.csv"
    csv.unlink(missing_ok=True)
    body = f"""
sim.getSimulationIterator().step({int(k)});
sim.println("ITERATION " + sim.getSimulationIterator().getCurrentIteration());
try {{
  StarPlot rp = sim.getPlotManager().getPlot("Residuals");
  rp.export("{_jpath(csv)}", ",");
  sim.println("RESIDUALS_CSV ok");
}} catch (Exception e) {{ sim.println("RESIDUALS_CSV n/a " + e.getMessage()); }}
for (Report rep : sim.getReportManager().getObjects()) {{
  try {{ sim.println("REPORT\\t" + rep.getPresentationName() + "\\t" + rep.getReportMonitorValue()); }}
  catch (Exception e) {{ sim.println("REPORT\\t" + rep.getPresentationName() + "\\tn/a"); }}
}}
"""
    r = _inject(body, timeout=timeout_s, tool="iterate")
    out = {"ok": r["ok"], "seconds": r["seconds"], "stdout": r["stdout"], **_err(r)}
    m = re.search(r"ITERATION (\d+)", r["stdout"])
    if m:
        out["iteration"] = int(m.group(1))
    reports = {}
    for line in r["stdout"].splitlines():
        parts = line.split("\t")
        if parts[0] == "REPORT" and len(parts) == 3:
            try:
                reports[parts[1]] = float(parts[2])
            except ValueError:
                reports[parts[1]] = parts[2]
    out["reports"] = reports
    out["residuals"], out["res_rows"] = None, []
    if csv.is_file():
        rows = [ln.split(",") for ln in csv.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
        if len(rows) >= 2:
            hdr = [h.strip().strip('"') for h in rows[0]]
            res = {}
            for h, v in zip(hdr, rows[-1]):
                try:
                    res[h] = float(v)
                except ValueError:
                    res[h] = v.strip()
            out["residuals"] = res
            for idx, row in enumerate(rows[1:], 1):  # (iteración, mayor residual) por fila
                vals = []
                for v in row[1:]:
                    try:
                        vals.append(abs(float(v)))
                    except ValueError:
                        pass
                try:
                    it = float(row[0]) if hdr[0].lower().startswith("iter") else idx
                except ValueError:
                    it = idx
                if vals:
                    out["res_rows"].append((it, max(vals)))
    return out


def _variation(samples: list[float]) -> float:
    """(máx - mín) / |media| de las muestras."""
    mean = sum(samples) / len(samples)
    return (max(samples) - min(samples)) / max(abs(mean), 1e-30)


def _judge(hist: list[dict], names: list[str], window: float, tol: float, res_rows: list, plateau_its: float):
    """('stable'|'plateau'|None, variación por reporte). Estable: cada reporte varía < tol en `window` iteraciones
    (ponytail: sobre muestras de fin de bloque, no iteración a iteración). Plateau: estable, pero el mayor residual
    no bajó un 10 % en plateau_its iteraciones."""
    last = hist[-1]
    if hist[0]["it"] > last["it"] - window:
        return None, {}  # aún no hay `window` iteraciones de historia
    recent = [h for h in hist if h["it"] >= last["it"] - window]
    var = {n: _variation([h["vals"][n] for h in recent]) for n in names}
    if any(v >= tol for v in var.values()):
        return None, var
    old = [(it, r) for it, r in res_rows if it <= last["it"] - plateau_its]
    if old and res_rows and res_rows[-1][1] >= 0.9 * old[-1][1]:
        return "plateau", var
    return "stable", var


def _short(hist: list[dict], n: int = 12) -> list[dict]:
    pts = hist if len(hist) <= n else [hist[round(i * (len(hist) - 1) / (n - 1))] for i in range(n)]
    return [{"it": h["it"], **{k: round(v, 6) for k, v in h["vals"].items()}, **({"res": h["res"]} if h.get("res") else {})}
            for h in pts]


@_tool
def iterate(n: int = 0, until: str = "stable", reports: list[str] = ["H", "M"], window: int = 100,
            tol: float = 0.005, block: int = 50, timeout_s: int = 7200, plateau_its: int = 300) -> dict:
    """Corre hasta n iteraciones por bloques de `block`. until="stable": para cuando cada reporte de `reports`
    (nombres de Report del .sim) varía < tol (0,5 %) en `window` iteraciones; stopped="plateau" si además el mayor
    residual no bajó un 10 % en plateau_its (300); stopped="max" si llega a n. n=0 (por defecto): 1000 con
    until="stable" (con 100 nunca da «stable»: pide ≥ window + block) y 100 con until="n". until="n": un solo bloque
    de n, sin criterio. Devuelve stopped, iterations_run, iteration, reports y residuals (última fila), variation, history
    (corta) y segundos. timeout_s es por bloque."""
    global _iter_hist, _iter_pid
    n = int(n) if int(n) > 0 else (1000 if until == "stable" else 100)
    block = max(1, int(block))
    with _turn("iterate") as busy:
        if busy:
            return _busy_only(busy)
        pid = _state().get("pid")
        if pid != _iter_pid:
            _iter_hist, _iter_pid = [], pid
        done, t0, blocks, last = 0, time.time(), 0, {}
        stopped, var, names = "max", {}, list(reports or [])
        while done < n:
            k = min(block, n - done) if until == "stable" else n
            last = _iter_block(k, timeout_s)
            blocks += 1
            if not last["ok"] or "iteration" not in last:
                return {"ok": False, "stopped": "error", "iterations_run": done, "blocks": blocks,
                        "stdout": last["stdout"][-1500:], **_err(last)}
            done += k
            it = last["iteration"]
            if _iter_hist and it <= _iter_hist[-1]["it"]:
                _iter_hist = []  # el .sim volvió atrás (otra simulación): historia nueva
            if until != "stable":
                break
            low = {r.lower(): (r, v) for r, v in last["reports"].items()}
            miss = [x for x in names if x.lower() not in low]
            bad = [x for x in names if x.lower() in low and not isinstance(low[x.lower()][1], float)]
            if miss or bad:
                return {"ok": False, "stopped": "error", "iterations_run": done, "iteration": it, "blocks": blocks,
                        "error": f"reportes {miss or bad} sin valor; hay {sorted(last['reports'])}. "
                                 "Pasa reports=[...] con nombres existentes o until='n'",
                        "reports": last["reports"]}
            res_rows = last["res_rows"]
            _iter_hist.append({"it": it, "vals": {x: low[x.lower()][1] for x in names},
                               "res": res_rows[-1][1] if res_rows else None})
            reason, var = _judge(_iter_hist, names, window, tol, res_rows, plateau_its)
            if reason:
                stopped = reason
                break
        out = {"ok": True, "stopped": stopped, "iterations_run": done, "iteration": last.get("iteration"), "blocks": blocks,
               "seconds": round(time.time() - t0, 1), "reports": last.get("reports", {}),
               "variation_pct": {k: round(v * 100, 3) for k, v in var.items()}, "history": _short(_iter_hist)}
        if last.get("residuals"):
            out["residuals"] = last["residuals"]
        else:
            out["stdout"] = last.get("stdout", "")[-1500:]
        return out


# ----------------------------------------------------------------------------- pipeline de bomba
def _pump_imports(mod) -> str:
    """IMPORTS del pipeline como texto para _wrap. Es una LISTA: se une con comas (str(lista) daba «import 'star.x.*';»,
    visto con el solver real). pump_case y el selftest usan esta misma función."""
    imp = getattr(mod, "IMPORTS", "") or ""
    return ",".join(imp) if isinstance(imp, (list, tuple)) else str(imp)


def _parse_pump(stdout: str) -> dict:
    """Líneas de la macro de la bomba -> dict. RESULT<TAB>nombre<TAB>valor, PIPE fase segundos, BALANCE_MASA<TAB>N %,
    AVISO<TAB>texto, EULER_OK|EULER_MAL<TAB>detalle (y cualquier EULER_*), más CELLS, MESH_SECONDS<TAB>zona<TAB>s,
    ITER_STOP, YPLUS, PLANOS, POST_SECONDS y PNG. Las claves vacías no salen."""
    def num(v):
        try:
            return float(v.replace(",", "."))
        except ValueError:
            return v
    out: dict = {"RESULT": {}, "PIPE": {}, "AVISO": [], "EULER": {}, "MESH_SECONDS": {}, "PNG": []}
    for raw in (stdout or "").splitlines():
        line = raw.strip("\r")
        if not line.strip():
            continue
        tag, _, rest = line.partition("\t")
        tag = tag.strip()
        if tag == "RESULT":
            k, _, v = rest.partition("\t")
            out["RESULT"][k.strip()] = num(v.strip())
        elif tag.startswith("PIPE "):
            m = re.match(r"PIPE (.+?) ([0-9.eE+-]+)\s*$", line.strip())
            if m:
                out["PIPE"][m.group(1)] = num(m.group(2))
        elif tag == "BALANCE_MASA":
            m = re.match(r"\s*([0-9.eE+-]+)\s*%?(?:\t(.*))?", rest)
            if m:
                out["BALANCE_MASA"] = {"pct": num(m.group(1)), **({"aviso": m.group(2).strip()} if m.group(2) else {})}
                if m.group(2):
                    out["AVISO"].append(f"BALANCE_MASA {m.group(1)} %: {m.group(2).strip()}")
        elif tag == "AVISO":
            out["AVISO"].append(rest.strip())
        elif tag.startswith("EULER_"):
            out["EULER"][tag] = rest.strip()
        elif tag == "CELLS":
            v = num(rest.strip())
            out["CELLS"] = int(v) if isinstance(v, float) else v
        elif tag == "MESH_SECONDS":
            k, _, v = rest.partition("\t")
            out["MESH_SECONDS"][k.strip()] = num(v.strip())
        elif tag == "POST_SECONDS":
            out[tag] = num(rest.strip())
        elif tag in ("ITER_STOP", "YPLUS", "PLANOS"):
            out[tag] = rest.strip()
        elif tag == "PNG":
            out["PNG"].append(rest.strip())
    return {k: v for k, v in out.items() if v not in ({}, [])}


def _load_pipeline(path: Path | str | None = None):
    """Importa pipelines/bomba.py en cada llamada (así una edición se nota sin reiniciar el MCP)."""
    p = Path(path or PIPELINE_BOMBA)
    if not p.is_file():
        raise FileNotFoundError(f"No existe {p}: el pipeline de bomba (parse_ccm, macro_body) aún no está escrito")
    spec = importlib.util.spec_from_file_location("pipelines_bomba", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for f in ("parse_ccm", "macro_body"):
        if not callable(getattr(mod, f, None)):
            raise AttributeError(f"{p} no define {f}()")
    return mod


@_tool
def pump_case(case: str, cad_dir: str, mode: str = "rapida", ccm: str = "", params: dict | None = None,
              timeout_s: int = 3600, dry_run: bool = False, until: str = "", n: int = 1000,
              reports: list[str] = ["H", "M"], block: int = 50) -> dict:
    """Bomba estándar de CFturbo en UNA macro: importa los STEP de cad_dir y el .cfturbo-ccm (rpm, caudal, eje,
    nombres), regiones, interfaces, malla (mode "rapida" o "normal"), física, MRF, fronteras y reportes (H, M, eta,
    Euler), vía pipelines/bomba.py. Arranca el servidor con el nombre del caso si no hay uno. ccm vacío = el
    *.cfturbo-ccm de cad_dir. params se pasa tal cual al pipeline. dry_run: solo genera la macro y la describe.
    Va por el cliente residente en UNA macro. until="stable" (o "n"): tras la macro, si salió bien, itera hasta n con
    el criterio de iterate (bloques de `block`, reportes `reports`) en el mismo turno, sin crear un cliente por bloque.
    Devuelve `parsed` (RESULT, PIPE, BALANCE_MASA, AVISO, EULER, CELLS, MESH_SECONDS, ITER_STOP, YPLUS, PNG ya leídos de la
    salida), la cola de stdout y `salida_completa` = casos/<caso>/salida_pump.txt con toda la salida."""
    if mode not in ("rapida", "normal"):
        return {"ok": False, "error": f"mode debe ser 'rapida' o 'normal', no '{mode}'"}
    cad = Path(cad_dir)
    if not cad.is_dir():
        return {"ok": False, "error": f"no existe la carpeta de CAD {cad_dir}"}
    ccm_path = Path(ccm) if ccm else next(iter(sorted(cad.glob("*.cfturbo-ccm")) or sorted(cad.rglob("*.cfturbo-ccm"))), None)
    if not ccm_path or not ccm_path.is_file():
        return {"ok": False, "error": f"no encuentro el .cfturbo-ccm (en {cad_dir} o en ccm=)"}
    try:
        mod = _load_pipeline()
        ccm_d = mod.parse_ccm(str(ccm_path))
        case_dir = CASES / _safe(case)
        case_dir.mkdir(parents=True, exist_ok=True)
        body = mod.macro_body(_jpath(case_dir), _jpath(cad), ccm_d, mode, dict(params or {}))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"pipeline de bomba: {type(e).__name__}: {e}"}
    imports = _pump_imports(mod)
    info = {"case": case, "mode": mode, "ccm_file": str(ccm_path), "ccm": ccm_d, "macro_chars": len(body)}
    if dry_run:
        return {"ok": True, "dry_run": True, **info, "macro_head": body[:600]}
    with _turn("pump_case") as busy:
        if busy:
            return _busy_only(busy)
        try:
            server_start(name=case)  # reutiliza el vivo; se niega si hay otro .sim cargado
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)[:1500], **info}
        r = _inject(body, imports, timeout=timeout_s, tool="pump_case")
        steps = _parse_steps(r["stdout"])
        failed = next((s["i"] for s in steps if s.get("ok") is False), None)
        # salida COMPLETA a casos/<caso>/salida_pump.txt (la cola de 3000 car. pierde PIPE, CELLS y AVISO del principio)
        salida = case_dir / "salida_pump.txt"
        try:
            salida.write_text(r["stdout"] + (f"\n--- error\n{r.get('exception')}\n{r.get('trace') or ''}" if not r["ok"] else "") + "\n",
                              encoding="utf-8")
        except OSError:
            salida = None
        out = {**info, "ok": bool(r["ok"]) and failed is None, "steps": steps, "failed_at": failed, "seconds": r["seconds"],
               "macro": r["macro"], "parsed": _parse_pump(r["stdout"]), "salida_completa": str(salida) if salida else None,
               "stdout": r["stdout"][-3000:], **_err(r)}
        if until in ("stable", "n") and out["ok"]:
            it = iterate(n=n, until=until, reports=list(reports), block=block)  # mismo turno (re-entrante)
            out["iterate"] = {k: it.get(k) for k in ("ok", "stopped", "iterations_run", "iteration", "blocks", "seconds",
                                                    "reports", "variation_pct", "error", "exception", "busy") if k in it}
            out["ok"] = bool(it.get("ok"))
        elif until and until not in ("stable", "n"):
            out["warnings"] = [f"until={until!r} no es «stable» ni «n»: no se iteró"]
        return out


@_tool
def export_scene(name: str, path: str = "", width: int = 1600, height: int = 1000) -> dict:
    """Exporta la escena `name` a PNG. path vacío = casos/_scenes/<name>.png."""
    out = Path(path) if path else CASES / "_scenes" / f"{_safe(name)}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    nm, outp = _jstr(name), _jstr(_jpath(out))
    # createScene("X") puede dejar la escena como "X 1": se acepta nombre exacto o prefijo.
    body = f"""
Scene sc = null;
for (Scene s : sim.getSceneManager().getScenes())
  if (s.getPresentationName().equals("{nm}")) sc = s;
if (sc == null)
  for (Scene s : sim.getSceneManager().getScenes())
    if (s.getPresentationName().startsWith("{nm}")) {{ sc = s; break; }}
if (sc == null) throw new RuntimeException("No hay escena '{nm}'");
sim.println("SCENE " + sc.getPresentationName());
sc.printAndWait(resolvePath("{outp}"), 1, {int(width)}, {int(height)}, true, false);
sim.println("PNG " + "{outp}");
"""
    r = _inject(body, tool="export_scene")
    return {"ok": r["ok"] and out.is_file(), "png": str(out), "seconds": r["seconds"], **_err(r)}


@_tool
def save(path: str = "") -> dict:
    """Guarda el .sim (en sims/ por defecto, o en path). Actualiza .server.json."""
    st = _state()
    target = Path(path) if path else (Path(st["sim"]) if st.get("sim") else SIMS / "nuevo.sim")
    if not target.suffix:
        target = target.with_suffix(".sim")
    if not target.is_absolute():
        target = SIMS / target
    target.parent.mkdir(parents=True, exist_ok=True)
    with _turn("save") as busy:
        if busy:
            return {"sim": str(target), **_busy_only(busy)}
        r = _inject(_save_macro(_jstr(_jpath(target))), timeout=1800, tool="save")
        if r["ok"]:
            st["sim"] = str(target)
            _write_state(st)
    mirror = mirror_sim(target) if r["ok"] else None
    return {"ok": r["ok"], "sim": str(target), "seconds": r["seconds"], **({"copia": mirror} if mirror else {}), **_err(r)}


def sim_destino(case: str) -> str | None:
    """Ruta donde el usuario quiere tener el .sim del caso (casos/<caso>/caso.json → sim_destino), o None."""
    f = CASES / case / "caso.json"
    try:
        d = json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}
    except ValueError:
        return None
    p = d.get("sim_destino") if isinstance(d, dict) else None
    return p if isinstance(p, str) and p.lower().endswith(".sim") else None


def mirror_sim(sim: Path | str) -> dict | None:
    """Tras cada guardado, copia el .sim de trabajo (sims/<caso>.sim) a la ruta elegida en caso.json. Copia a un
    temporal y lo renombra: un corte a mitad no deja un .sim roto en el destino."""
    sim = Path(sim)
    dest = sim_destino(sim.stem)
    if not dest or not sim.is_file() or Path(dest).resolve() == sim.resolve():
        return None
    d = Path(dest)
    tmp = d.with_name(d.name + ".copiando")
    try:
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sim, tmp)
        os.replace(tmp, d)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        _log(f"copia del .sim a {d} falló: {e}")
        return {"ok": False, "dest": str(d), "error": str(e)}
    _log(f"copia del .sim: {sim} -> {d}")
    return {"ok": True, "dest": str(d)}


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", s).strip("_") or "x"


# ----------------------------------------------------------------------------- memoria
@_tool
def recipe_search(query: str = "") -> dict:
    """Busca en conocimiento/recetas/ (snippets Java verificados). Devuelve nombre, cabecera y coincidencias.
    Vacío = lista todas (en `resultado`). Úsalas con run_java o run_steps."""
    _touch()
    terms = [t.lower() for t in re.findall(r"\w+", query)]
    hits = []
    for p in sorted(RECIPES.glob("*.java")) + sorted(RECIPES.glob("*.md")):
        if p.name.lower() == "readme.md":
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        low = (p.name + "\n" + text).lower()
        score = sum(low.count(t) for t in terms) if terms else 1
        if score:
            head = [ln.strip() for ln in text.splitlines()[:4] if ln.strip()]
            hits.append({"name": p.name, "score": score, "head": head,
                         "unverified": "no verificado" in text.lower()})
    hits.sort(key=lambda h: -h["score"])
    return hits[:20]


@_tool
def recipe_save(name: str, body: str, note: str = "") -> dict:
    """Guarda un snippet que funcionó como conocimiento/recetas/<verbo>-<objeto>.java con cabecera QUE/VERSION/FECHA.
    Para que run_steps lo parametrice: ${nombre} en el cuerpo y «// PARAMS: nombre» en la cabecera.
    Guardar tras CADA run_java que logre algo nuevo. La ruta del archivo va en `resultado`."""
    _touch()
    RECIPES.mkdir(parents=True, exist_ok=True)
    fname = _safe(name.removesuffix(".java")).lower() + ".java"
    header = f"// QUE: {note or name}\n// VERSION: {_version_tag()}\n// FECHA: {datetime.now():%Y-%m-%d}\n"
    p = RECIPES / fname
    p.write_text(header + body.rstrip() + "\n", encoding="utf-8")
    return str(p)


def _version_tag() -> str:
    return HOME.name.replace("STAR-CCM+", "") if HOME else "?"


_doc_index: list[tuple[str, Path]] | None = None


def _docs() -> list[tuple[str, Path]]:
    global _doc_index
    if _doc_index is None:
        _doc_index = []
        if DOC_DIR and DOC_DIR.is_dir():
            for p in DOC_DIR.rglob("*.html"):
                rel = p.relative_to(DOC_DIR).as_posix()
                if "/" in rel and "class-use/" not in rel and not p.name.startswith(("index", "overview", "package-")):
                    _doc_index.append((rel[:-5].replace("/", "."), p))
    return _doc_index


_member_idx: dict[str, list[str]] | None = None


def _members() -> dict[str, list[str]]:
    """clase calificada -> miembros, de member-search-index.js (0,1 s; sustituye al rastreo de las 22 k páginas)."""
    global _member_idx
    if _member_idx is None:
        _member_idx = {}
        f = DOC_DIR / "member-search-index.js" if DOC_DIR else None
        if f and f.is_file():
            txt = f.read_text(encoding="utf-8", errors="replace")
            for m in json.loads(txt[txt.index("["):txt.rindex("]") + 1]):
                _member_idx.setdefault(f"{m['p']}.{m['c']}", []).append(m["l"])
    return _member_idx


def _html_text(p: Path) -> str:
    t = p.read_text(encoding="utf-8", errors="replace")
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(t)
    t = re.sub(r"[ \t\xa0]+", " ", t)
    # firmas con la lista de argumentos partida en varias líneas -> una sola línea
    return re.sub(r"\(\s*([^()]*?)\s*\)", lambda m: "(" + " ".join(m.group(1).split()) + ")", t, flags=re.S)


@_tool
def docs_search(query: str, max_classes: int = 5) -> dict:
    """Busca en el javadoc de STAR-CCM+ ($STARCCM_HOME/doc/client/html): clases cuyo nombre
    coincide y, dentro, las firmas de métodos que contienen los términos. Fuente de verdad de
    nombres de clases y métodos."""
    _touch()
    terms = [t for t in re.findall(r"\w+", query) if len(t) > 1]
    if not terms:
        return {"error": "query vacía"}
    docs = _docs()
    if not docs:
        return {"error": f"No encuentro el javadoc en {DOC_DIR}"}
    low = [t.lower() for t in terms]
    scored = []
    for fq, p in docs:
        name = fq.rsplit(".", 1)[-1].lower()
        s = sum(3 for t in low if t in name) + sum(1 for t in low if t in fq.lower())
        if s:
            scored.append((s, -len(name), fq, p))
    scored.sort(reverse=True)
    result = {"query": query, "classes": []}
    for s, _, fq, p in scored[:max_classes]:
        text = _html_text(p)
        sigs = []
        for line in re.split(r"\n+", text):
            l = line.strip()
            if ("(" in l and ")" in l and len(l) < 220 and any(t in l.lower() for t in low)) \
                    or l.startswith(("public ", "class ", "interface ")):
                if l not in sigs:
                    sigs.append(l)
        result["classes"].append({"class": fq, "file": str(p), "matches": sigs[:25]})
    if not result["classes"]:
        # ningún nombre de clase coincide: se busca entre los 146 k miembros del índice del javadoc
        hits, members = [], {}
        for fq, ms in _members().items():
            joined = " ".join(ms).lower()
            if all(t in joined for t in low):
                exact = [m for m in ms if all(t in m.lower() for t in low)]
                hits.append((0 if exact else 1, fq))
                members[fq] = (exact or ms)[:5]
        hits.sort()
        result["content_hits"] = [fq for _, fq in hits[:max_classes]]
        result["members"] = {fq: members[fq] for fq in result["content_hits"]}
    return result




# ----------------------------------------------------------------------------- selftest
_HOLD = ("import json,os,sys,time;p=sys.argv[1];"
         "open(p,'w').write(json.dumps({'pid':os.getpid(),'tool':'panel_demo','start':'x','t':time.time()}));"
         "print('HELD',flush=True);time.sleep(float(sys.argv[2]));os.path.exists(p) and os.remove(p)")

_FAKE_BOMBA = '''
IMPORTS = ["star.common.*", "star.meshing.*"]
EXTRA = r"""
sim.println("RESULT\\tH\\t16.5");
sim.println("RESULT\\tM\\t8.4");
sim.println("PIPE total 12.5");
sim.println("BALANCE_MASA\\t0.0002 %");
sim.println("AVISO\\talgo raro");
sim.println("EULER_OK\\tH_Euler 18 m, H 16 m");
sim.println("CELLS\\t1234");
"""
def parse_ccm(path):
    return {"rpm": 1450, "caudal_m3h": 36.0, "nombre": "bomba falsa"}
def macro_body(case_dir, cad_dir, ccm, mode, params):
    fail = 'sim.println("AGENT_FAKE_FAIL");' if params.get("fallar") else ""
    return ('sim.println("STEP_BEGIN 0");\\nsim.println("importado %s %s");\\nsim.println("STEP_END 0");\\n'
            'sim.println("STEP_BEGIN 1");\\n%s\\nsim.println("regiones listas");\\nsim.println("STEP_END 1");'
            % (ccm["rpm"], mode, fail)) + EXTRA
'''


def _selftest() -> int:
    global EXE, PORT, SIMS, MACROS, CASES, RECIPES, STATE_FILE, LOCK_FILE, AGENT_LOG, IDLE_MIN, LOCK_WAIT
    global PIPELINE_BOMBA, GUI_TTL, POLL_S, _inject_local, _gui_cache, _list_procs, _iter_hist, _iter_pid
    global WORK, RES_DIR, TIEMPOS, RES_ON, RES_HB_STALE, RES_START_WAIT, _res_off_until, _javac, _run_javac, _precompile
    global PANEL_ESTADO, VIGILANTE_ON, VIGILANTE_S
    import tempfile
    t_all = time.time()
    VIGILANTE_ON = False  # el vigilante real solo se prueba en su sección (es un proceso aparte con su propio entorno)
    fake = HERE / "tests" / "fake_starccm.py"
    os.environ["STARCCM_EXE"] = str(fake)
    EXE = str(fake)
    KEY = "FAKEKEY-selftest-9f3a7c"
    os.environ["STARCCM_PODKEY"] = KEY  # clave de mentira: se comprueba que NUNCA sale; la real no se toca
    with socket.socket() as sk:  # puerto libre: dos selftests a la vez no se pisan
        sk.bind(("127.0.0.1", 0))
        PORT = sk.getsockname()[1]
    tmp = Path(tempfile.mkdtemp(prefix="starccm-selftest-"))
    # Todo lo que escribe el falso va al temporal: ni sims/, ni macros/, ni .server.json, ni .inject.lock reales
    # (un selftest antiguo borró el .server.json de un solver vivo de otra sesión).
    real_lock, real_state, real_panel = LOCK_FILE, STATE_FILE, PANEL_ESTADO
    real_panel_before = real_panel.stat().st_mtime_ns if real_panel.is_file() else None
    real = (SIMS, MACROS, STATE_FILE, RES_DIR)
    real_work = _work_dir()
    _real_files = lambda: {(str(p), p.stat().st_mtime_ns) for d in real for p in ([d] if d.is_file() else d.glob("*") if d.is_dir() else [])}
    real_before = _real_files()
    WORK = tmp / "work"
    SIMS, MACROS, RES_DIR, TIEMPOS = WORK / "sims", WORK / "macros", WORK / "residente", WORK / "tiempos.csv"
    STATE_FILE, AGENT_LOG, PANEL_ESTADO = estado_dir() / ".server.json", tmp / "agent.log", tmp / "panel_estado.json"
    RES_HB_STALE, RES_START_WAIT = 2.0, 8.0
    LOCK_FILE, CASES, RECIPES = estado_dir() / ".inject.lock", tmp / "casos", tmp / "recetas"
    RECIPES.mkdir()
    (RECIPES / "paso-ok.java").write_text('// QUE: paso de prueba\n// PARAMS: nombre\nsim.println("hola ${nombre} y ${Flujo}");\n', encoding="utf-8")
    (RECIPES / "paso-falla.java").write_text('sim.println("AGENT_FAKE_FAIL");\nsim.println("no llega");\n', encoding="utf-8")
    scen = tmp / "scen.txt"
    os.environ["FAKE_SCEN_FILE"] = str(scen)
    LOCK_WAIT, POLL_S, GUI_TTL = 0.5, 0.05, 1e9
    real_list = _list_procs
    seen_rows: list = []
    _list_procs = lambda: []  # sin PowerShell salvo en la sección de la GUI
    fails = []
    gui_pid = None
    sleepers = []

    def check(cond, msg):
        print(f"  {time.time() - t_all:5.1f}s " + ("ok   " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    def wlock(pid, tool="otro", age_s=0):
        LOCK_FILE.write_text(json.dumps({"pid": pid, "tool": tool, "start": "x", "t": time.time() - age_s}), encoding="utf-8")
        if age_s:
            os.utime(LOCK_FILE, (time.time() - age_s, time.time() - age_s))

    # --- pruebas lentas e independientes del resto: en hilos, para no sumar al tiempo del selftest
    bg: dict = {}
    jc_real = _javac(real=True)  # (se resuelve ya: más abajo se sustituyen _javac y _run_javac)

    def bg_detached() -> None:
        """Procesos fuera del árbol de la CLI: un padre falso lanza un destino con _spawn_detached y otro hijo normal; un
        `taskkill /T /F` REAL sobre el padre mata al hijo normal y NO al desacoplado."""
        sleeper = "import time;time.sleep(120)"
        code = (f"import sys,time,subprocess; sys.path.insert(0, {str(HERE)!r}); import mcp_server as m; "
                f"d=m._spawn_detached([sys.executable,'-I','-c',{sleeper!r}]); "
                f"c=subprocess.Popen([sys.executable,'-I','-c',{sleeper!r}]); "
                "print(d['pid'], c.pid, d['breakaway'], flush=True); time.sleep(120)")
        par = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        try:
            line = par.stdout.readline().split()
            dpid, cpid = int(line[0]), int(line[1])
            time.sleep(0.4)
            before = (_pid_alive(dpid), _pid_alive(cpid), _pid_alive(par.pid))
            subprocess.run(["taskkill", "/PID", str(par.pid), "/T", "/F"], capture_output=True, timeout=30)
            time.sleep(0.6)
            bg["detached"] = (before, _pid_alive(par.pid), _pid_alive(cpid), _pid_alive(dpid), line[2])
            _kill_tree(dpid)
            # un .bat desacoplado que lanza un hijo de consola (como starccm+.bat): su salida redirigida tiene que llegar al log
            bat, blog = tmp / "hijo.bat", tmp / "hijo.log"
            crlf = chr(13) + chr(10)
            bat.write_bytes(("@echo off" + crlf + "echo bat" + crlf + '"' + sys.executable + '" -I -c "print(1234)"' + crlf + "echo fin" + crlf).encode())
            _spawn_detached([str(bat)], log=blog, cwd=tmp)
            t_end = time.time() + 15
            while time.time() < t_end and "fin" not in (blog.read_text(errors="replace") if blog.is_file() else ""):
                time.sleep(0.1)
            bg["bat"] = blog.read_text(errors="replace") if blog.is_file() else ""
        except Exception as e:  # noqa: BLE001
            bg["detached"] = e
            _kill_tree(par.pid)

    def bg_regresion() -> None:
        """La macro REAL de pipelines/bomba.py con el MISMO camino de imports que pump_case, compilada con el javac de STAR-CCM+."""
        cad = Path.home() / "OneDrive" / "Documentos" / "cfturbo-agent" / "versions" / "BombaRapida_v1"
        ccms = sorted(cad.glob("*.cfturbo-ccm")) if cad.is_dir() else []
        if not ccms or not jc_real:
            bg["regresion"] = "SKIP: " + ("no hay .cfturbo-ccm de BombaRapida_v1" if not ccms else "no hay javac de STAR-CCM+")
            return
        res = []
        try:
            mod = _load_pipeline(HERE / "pipelines" / "bomba.py")
            ccm_d = mod.parse_ccm(str(ccms[0]))
            d = tmp / "regresion"
            d.mkdir()
            for nombre, params in (("rapida", {}), ("iterar+post", {"iterar": True, "post": True})):
                body = mod.macro_body(_jpath(d), _jpath(cad), ccm_d, "rapida", dict(params))
                src = d / f"Agent_{_safe(nombre)}.java"
                text = _wrap(body, _pump_imports(mod), _safe(nombre))
                src.write_text(text, encoding="utf-8")
                bad = [ln for ln in text.splitlines() if ln.startswith("import ") and re.search(r"['\[\]]", ln)]
                (d / "out").mkdir(exist_ok=True)
                r = subprocess.run([jc_real[0], "-nowarn", "-proc:none", "-cp", jc_real[1], "-d", str(d / "out"), str(src)],
                                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
                res.append((nombre, bad, r.returncode, (r.stderr or r.stdout)[-400:]))
            bg["regresion"] = res
        except Exception as e:  # noqa: BLE001
            bg["regresion"] = e

    def bg_docs() -> None:
        """Calienta el índice del javadoc (~3,5 s la primera vez); las comprobaciones de docs_search lo esperan."""
        try:
            _docs()
            _members()
        except Exception:  # noqa: BLE001
            pass

    docs_thread = threading.Thread(target=bg_docs, daemon=True)
    docs_thread.start()
    threads = [threading.Thread(target=bg_regresion, daemon=True)]
    if os.name == "nt":
        threads.append(threading.Thread(target=bg_detached, daemon=True))
    for t in threads:
        t.start()

    print(f"selftest: starccm+ falso = {fake} · escribe solo en {tmp}")
    try:
        r = subprocess.run(_starccm_cmd() + ["-version"], capture_output=True, text=True)
        check("FAKE" in r.stdout, "-version del falso")
        os.environ["FAKE_RESIDENT_DELAY"] = "0.6"  # el residente tarda 0,6 s en dar su primer latido: server_start no debe esperarlo
        t_ss = time.time()
        st = server_start()
        t_ss = time.time() - t_ss
        os.environ.pop("FAKE_RESIDENT_DELAY")
        check(st.get("pid") and _port_open(PORT), f"server_start -> pid {st.get('pid')} puerto {PORT} en {st.get('startup_seconds')}s")
        ri0 = _res_info()
        check(ri0.get("pid") and _pid_alive(ri0["pid"]) and not (RES_DIR / "heartbeat").exists() and _res_waits == 0
              and _state().get("init") == "pendiente" and st.get("init") == "pendiente",
              f"server_start lanza el residente y vuelve SIN esperar su latido ({t_ss:.1f} s; init pendiente)")
        check(Path(_state()["log"]).parent == SIMS and ri0.get("breakaway") in (True, False, None) and "vigilante" in st,
              "el servidor y el residente salen del lanzador desacoplado (doble lanzamiento)")
        check(KEY not in json.dumps(st) and KEY not in STATE_FILE.read_text(encoding="utf-8"), "la clave PoD no sale de server_start ni de .server.json")
        st2 = server_start()
        check(st2.get("reused"), "server_start reutiliza el servidor vivo")
        r = run_java('sim.println("OK");')
        check(r["ok"] and r["stdout"].strip() == "OK", f"run_java OK ({r['seconds']}s)")
        check(r["via"] == "residente" and _res_waits == 1 and (RES_DIR / "heartbeat").is_file()
              and "INIT autosave off" in (_state().get("init") or ""),
              f"la primera inyección espera el latido del residente y corre antes la macro de init ({_state().get('init')!r})")
        check(not LOCK_FILE.exists(), "el turno se libera al terminar la tool")
        r = run_java('sim.println("AGENT_FAKE_FAIL");\nsim.println("no llega");')
        check(not r["ok"] and r["exception"] and "fallo simulado" in r["exception"], "run_java captura excepción")
        r = inspect("regions")
        check(r["ok"] and r["path"] == "regions", "inspect(regions) responde")
        r = inspect("nodo_que_no_existe")
        check("error" in r, "inspect rechaza nodo desconocido")
        p = recipe_save("probar-selftest", 'sim.println("hola");', "receta de prueba del selftest")
        p = p["resultado"]
        check(Path(p).is_file() and Path(p).parent == RECIPES, "recipe_save (en la carpeta de recetas)")
        hits = recipe_search("selftest hola")["resultado"]
        check(any(h["name"] == "probar-selftest.java" for h in hits), "recipe_search encuentra la receta")
        check(RECIPES.parent == tmp and (HERE / "conocimiento" / "recetas").is_dir(), "las recetas reales viven en conocimiento/recetas")
        docs_thread.join(timeout=30)
        d = docs_search("Simulation saveState")
        check(("classes" in d and d["classes"]) or "error" in d, "docs_search responde (con o sin javadoc)")
        if DOC_DIR and DOC_DIR.is_dir():
            check(any(c["class"].endswith("Simulation") for c in d.get("classes", [])), "docs_search encuentra star.common.Simulation")
        r = save(str(tmp / "prueba.sim"))
        check(r["ok"], "save")
        if DOC_DIR and DOC_DIR.is_dir() and (DOC_DIR / "member-search-index.js").is_file():
            d2 = docs_search("setRelativeToModelLength")
            check(any(c.endswith("GlyphSettings") for c in d2.get("content_hits", [])), "docs_search encuentra un método por el índice de miembros")
        check(_jstr('a"b' + chr(92) + 'c' + chr(10) + 'd') == 'a' + chr(92) + '"b' + chr(92) * 2 + 'c' + chr(92) + 'nd',
              "_jstr escapa comillas, barras y saltos")
        r = export_scene('x"); System.exit(0); //')
        macs = sorted(MACROS.glob("Agent_*.java"), key=lambda q: q.stat().st_mtime)
        check('equals("x\\"); System.exit(0); //")' in macs[-1].read_text(encoding="utf-8"), "export_scene escapa el nombre en el Java")
        check(idle_seconds() < 60, "last_activity en disco ve la actividad reciente")

        # --- redacción de la clave
        r = run_java(f'sim.println("clave={KEY}");')
        outs = "".join(q.read_text(encoding="utf-8", errors="replace") for q in MACROS.glob("Agent_*.out"))
        check(KEY not in json.dumps(r) and "***PODKEY***" in r["stdout"], "run_java: la clave impresa por una macro sale redactada")
        check(KEY not in outs and KEY not in AGENT_LOG.read_text(encoding="utf-8"), "la clave no queda en macros/*.out ni en agent.log")
        check(_redact(f"a {KEY} b") == "a ***PODKEY*** b" and KEY not in json.dumps(_scrub({"x": [f"k={KEY}", {"y": KEY}]})),
              "_redact y _scrub")

        # --- carpetas de trabajo fuera de OneDrive
        check(_work_dir({"COPILOTO_WORK": str(tmp / "w")}) == tmp / "w"
              and _work_dir({"LOCALAPPDATA": "C:/X"}) == Path("C:/X") / "starccm-copiloto",
              "_work_dir: COPILOTO_WORK manda; si no, %LOCALAPPDATA%/starccm-copiloto")
        check(bool(os.environ.get("COPILOTO_WORK")) or (HERE not in real_work.parents and "onedrive" not in str(real_work).lower()),
              f"carpeta de trabajo real fuera del repo y de OneDrive ({real_work})")
        check(all(tmp in q.parents for q in (SIMS, MACROS, RES_DIR, TIEMPOS)) and Path(_state()["log"]).parent == SIMS
              and CASES.parent == tmp and (HERE / "casos").is_dir(),
              "sims, macros, residente, tiempos.csv y log del solver cuelgan de la carpeta de trabajo; casos/ se queda en el repo")
        check(estado_dir() == WORK and STATE_FILE.parent == WORK and LOCK_FILE.parent == WORK and STATE_FILE.is_file(),
              "estado_dir(): .server.json e .inject.lock cuelgan de la carpeta de trabajo")
        check(real_state.parent == real_work and real_lock.parent == real_work
              and (bool(os.environ.get("COPILOTO_WORK")) or "onedrive" not in str(real_state).lower()),
              f"los .server.json/.inject.lock REALES quedan fuera del repo y de OneDrive ({real_state.parent})")
        pe = json.loads(PANEL_ESTADO.read_text(encoding="utf-8")) if PANEL_ESTADO.is_file() else {}
        check(pe.get("estado_dir") == str(WORK) and pe.get("state_file") == str(STATE_FILE) and pe.get("lock_file") == str(LOCK_FILE),
              "panel_estado.json dice dónde están .server.json e .inject.lock (lo escribe server_start)")
        old_d, new_d = tmp / "viejo", tmp / "nuevo"
        old_d.mkdir()
        (old_d / ".server.json").write_text('{"pid": 1}', encoding="utf-8")
        (old_d / ".inject.lock").write_text("{}", encoding="utf-8")
        moved = _migrate_legacy(old_d, new_d / ".server.json", new_d / ".inject.lock")
        (old_d / ".server.json").write_text('{"pid": 2}', encoding="utf-8")
        moved2 = _migrate_legacy(old_d, new_d / ".server.json", new_d / ".inject.lock")
        check(sorted(moved) == [".inject.lock", ".server.json"] and (new_d / ".server.json").read_text(encoding="utf-8") == '{"pid": 1}'
              and not moved2, "migración: el estado viejo del repo se mueve a la carpeta de estado y nunca pisa uno existente")

        # --- cliente residente (vía por defecto)
        ri = _res_info()
        rpid = ri.get("pid")
        n_launch = _res_launches
        check(rpid and _pid_alive(rpid) and (RES_DIR / "heartbeat").is_file() and (RES_DIR / "Residente.java").is_file()
              and ri.get("server_pid") == _state().get("pid"), f"server_start dejó un residente vivo con latido (pid {rpid})")
        pc_calls = []
        real_pc = _precompile
        _precompile = lambda *a, **k: (pc_calls.append(a), None)[1]
        r = run_java('sim.println("res1");')
        r2 = run_java('sim.println("res2");')
        _precompile = real_pc
        check(r["ok"] and r["stdout"] == "res1" and r2["stdout"] == "res2" and r["via"] == "residente" and r2["via"] == "residente"
              and isinstance(r["ms"], int) and _res_info()["pid"] == rpid and _res_launches == n_launch,
              f"run_java por el residente: via, ms y el MISMO residente ({r['ms']} ms, {r2['ms']} ms)")
        check(not pc_calls, "la vía residente no precompila con javac")
        check("AGENT_END" in Path(r["macro"]).with_suffix(".out").read_text(encoding="utf-8")
              and "AGENT_BEGIN" in Path(_res_info()["log"]).read_text(encoding="utf-8") and not macro_running(),
              "la salida va al log del residente y al .out de la macro; macro_running no la da por viva")
        r = run_java('sim.println("AGENT_FAKE_COMPILE_ERROR");')
        check(r["ok"] is False and "compilar" in (r["exception"] or "") and "cannot find symbol" in (r["trace"] or "")
              and r["via"] == "residente" and _res_info()["pid"] == rpid, "error de compilación: lo devuelve el residente y sigue vivo")
        r = run_java('sim.println("tras el error");')
        check(r["ok"] and r["stdout"] == "tras el error" and _res_info()["pid"] == rpid, "tras el error de compilación el residente sigue sirviendo")
        t0 = time.time()
        r = _inject('sim.println("AGENT_FAKE_HANG");', timeout=0.4, tool="selftest")
        npid = _res_info().get("pid")
        check(r["ok"] is False and r.get("client_killed") and "timeout" in (r["exception"] or "") and not _pid_alive(rpid)
              and npid and npid != rpid and _pid_alive(npid) and alive() and time.time() - t0 < 12,
              f"macro colgada: se mata el residente (no el servidor), se relanza y responde error ({time.time() - t0:.1f}s)")
        rpid = npid
        r = run_java('sim.println("tras el cuelgue");')
        check(r["ok"] and r["via"] == "residente" and _res_info()["pid"] == rpid, "tras el cuelgue el residente nuevo sirve")
        r = run_java('sim.println("AGENT_FAKE_DIE");')
        npid = _res_info().get("pid")
        check(r["ok"] is False and r.get("resident_died") and not _pid_alive(rpid) and npid and npid != rpid and alive(),
              "residente muerto a mitad de macro: error claro (sin repetir la macro por batch) y relanzado")
        rpid = npid
        _kill_tree(rpid)
        r = run_java('sim.println("revive");')
        check(r["ok"] and r["via"] == "residente" and _res_info()["pid"] != rpid, "residente muerto entre macros: se relanza solo (vía residente)")
        rpid = _res_info()["pid"]
        os.environ["FAKE_RESIDENT_MODE"] = "broken"
        _kill_tree(rpid)
        n_launch = _res_launches
        r = run_java('sim.println("por batch");')
        check(r["ok"] and r["stdout"] == "por batch" and r["via"] == "batch" and "terminó" in (r.get("via_fallback") or "")
              and _res_launches == n_launch + 2 and not _res_info(),
              f"residente que no arranca (relanzado una vez): cae al batch y lo dice ({str(r.get('via_fallback'))[:60]})")
        r = run_java('sim.println("batch 2");')
        check(r["ok"] and r["via"] == "batch" and _res_launches == n_launch + 2 and "pausa" in (r.get("via_fallback") or ""),
              "tras fallar dos veces el batch sigue sin reintentar el residente cada macro (pausa)")
        os.environ.pop("FAKE_RESIDENT_MODE")
        _res_off_until = 0.0
        r = run_java('sim.println("de vuelta");')
        check(r["ok"] and r["via"] == "residente" and _pid_alive(_res_info()["pid"]), "al volver a estar sano el residente vuelve a ser la vía")
        # guardas de AutoExport
        b, w = _guard_autoexport("getAutoExport();\nae.getStarUpdate().getIterationUpdateFrequency().setIterations(25);")
        b2, w2 = _guard_autoexport("getAutoExport();\nae.getStarUpdate().getIterationUpdateFrequency().setIterations(250);")
        check("setIterations(100)" in b and w and "setIterations(250)" in b2 and not w2
              and _guard_autoexport("x.setIterations(5);") == ("x.setIterations(5);", []), "AutoExport: frecuencia < 100 sube a 100")
        r = run_java('sim.getSimulationIterator().getAutoExport(); ae.getIterationUpdateFrequency().setIterations(10);')
        check("setIterations(100)" in Path(r["macro"]).read_text(encoding="utf-8") and r.get("warnings"), "run_java aplica la guarda de AutoExport")
        check("setEnabled(false)" in _INIT_AUTOSAVE and "Math.max(100, CADA)" in (HERE / "conocimiento" / "recetas" / "activar-autoexport.java").read_text(encoding="utf-8"),
              "AutoExport apagado al arrancar y la receta nunca baja de 100")
        # caché de javac por hash del cuerpo (solo vía batch)
        calls = []
        real_javac, real_run = _javac, _run_javac
        _javac = lambda: ("javac", "cp")
        _run_javac = lambda cmd: (calls.append(cmd), subprocess.CompletedProcess(cmd, 0, "", ""))[1]
        fj = MACROS / "Agent_x.java"
        fj.write_text("x", encoding="utf-8")
        e = [_precompile(fj, "hashA"), _precompile(fj, "hashA"), _precompile(fj, "hashB")]
        _run_javac = lambda cmd: (calls.append(cmd), subprocess.CompletedProcess(cmd, 1, "", "boom"))[1]
        e += [_precompile(fj, "hashC"), _precompile(fj, "hashC")]
        _javac, _run_javac = real_javac, real_run
        check(e[:3] == [None] * 3 and e[3:] == ["boom", "boom"] and len(calls) == 4, "precompilación batch: caché por hash (los errores no se cachean)")
        fj.unlink()
        # tiempos.csv
        rows = [ln.split(",") for ln in TIEMPOS.read_text(encoding="utf-8").splitlines()]
        check(rows[0] == ["fecha", "herramienta", "ms", "via", "ok", "inyecciones"]
              and any(x[1] == "run_java" and x[3] == "residente" and x[2].isdigit() for x in rows[1:])
              and any(x[1] == "run_java" and x[3] == "batch" for x in rows[1:]), f"tiempos.csv con herramienta, ms y via ({len(rows) - 1} filas)")
        ri = recipe_search("selftest")
        check(ri["via"] == "ninguno" and isinstance(ri["ms"], int), "toda respuesta trae ms y via (también las que no inyectan)")

        # --- turnos entre procesos (.inject.lock)
        child = subprocess.Popen([sys.executable, "-I", "-c", _HOLD, str(LOCK_FILE), "2.2"], stdout=subprocess.PIPE, text=True)
        check(child.stdout.readline().strip() == "HELD", "otro proceso toma el turno")
        t0 = time.time()
        r = run_java('sim.println("x");', wait_s=1)
        check(r["ok"] is False and "panel_demo" in (r.get("busy") or "") and 0.8 < time.time() - t0 < 4,
              f"run_java con el turno ocupado espera y devuelve busy ({r.get('busy')})")
        sstat = server_status()
        check((sstat.get("turno") or {}).get("tool") == "panel_demo", "server_status dice quién tiene el turno")
        r = server_stop()
        check(r["stopped"] is False and r.get("busy") and alive(), "server_stop con el turno ocupado no apaga")
        r = run_java('sim.println("tras la espera");', wait_s=10)
        check(r["ok"] and r["stdout"] == "tras la espera", "run_java espera el turno y corre al liberarse")
        child.wait(timeout=10)
        check(not LOCK_FILE.exists(), "turno liberado tras la espera")
        wlock(999999, "muerto")
        r = run_java('sim.println("1");')
        check(r["ok"] and not LOCK_FILE.exists(), "candado huérfano (pid muerto) se limpia")
        sl = subprocess.Popen([sys.executable, "-I", "-c", "import time;time.sleep(60)"])
        sleepers.append(sl)
        wlock(sl.pid, "viejo", age_s=3 * 3600)
        r = run_java('sim.println("2");')
        check(r["ok"], "candado sin latido en 2 h se limpia aunque el pid viva")
        wlock(os.getpid(), "mio-roto")
        r = run_java('sim.println("3");')
        check(r["ok"], "candado propio sin uso (resto de un fallo) se limpia")
        wlock(sl.pid, "vivo")
        r = run_java('sim.println("4");', wait_s=0)
        check(r["ok"] is False and "vivo" in (r.get("busy") or ""), "candado vivo y reciente da busy")
        LOCK_FILE.unlink()
        _kill_tree(sl.pid)

        # --- run_steps
        r = run_steps([{"recipe": "paso-ok", "params": {"nombre": "mundo"}}, {"recipe": "paso-falla"},
                       {"java": 'sim.println("tercero");', "name": "mi-java"}], stop_on_error=False)
        s = r.get("steps", [])
        check(len(s) == 3 and s[0]["ok"] is True and s[0]["stdout"] == "hola mundo y ${Flujo}", f"run_steps: paso 0 ok con params y ${{Flujo}} intacto ({s[0] if s else r})")
        check(s[1]["ok"] is False and "fallo simulado" in s[1].get("error", ""), "run_steps: el paso que falla trae su error")
        check(s[2]["ok"] is True and s[2]["stdout"] == "tercero" and s[2]["name"] == "mi-java", "run_steps: con stop_on_error=False el siguiente corre")
        check(r["ok"] is False and r["failed_at"] == 1, "run_steps: ok False y failed_at=1")
        check("// ---- paso 1: paso-falla.java" in Path(r["macro"]).read_text(encoding="utf-8"), "run_steps: UNA macro con los tres pasos")
        r = run_steps([{"recipe": "paso-ok", "params": {"nombre": "x"}}, {"recipe": "paso-falla"}, {"java": 'sim.println("nunca");'}])
        s = r["steps"]
        check(s[1]["ok"] is False and s[2]["skipped"] is True and s[2]["ok"] is None, "run_steps: stop_on_error salta el resto")
        r = run_steps([{"recipe": "paso-ok"}])
        check(r["ok"] is False and "faltan parámetros" in r["error"], "run_steps: exige los PARAMS de la receta")
        r = run_steps([{"recipe": "no-existe"}])
        check(r["ok"] is False and "no existe la receta" in r["error"], "run_steps: receta inexistente")
        r = run_steps([{"recipe": "paso-ok", "java": "x"}])
        check(r["ok"] is False and "exactamente uno" in r["error"], "run_steps: paso mal formado")
        r = run_steps([{"recipe": "paso-ok", "params": {"nombre": "a", "sobra": 1}}])
        check(r["ok"] and any("sin uso" in w for w in r.get("warnings", [])), "run_steps avisa de parámetros sin uso")

        # --- iterate con criterio de parada
        scen.write_text("stable")
        r = iterate(n=1000, block=100)
        check(r["ok"] and r["stopped"] == "stable" and 200 <= r["iteration"] <= 600 and r["reports"].get("H"),
              f"iterate until=stable para al asentarse: {r.get('stopped')} en {r.get('iteration')} it, var {r.get('variation_pct')}")
        check(r["iterations_run"] < 1000 and r["history"] and "residuals" in r and "res" in r["history"][-1], "iterate: historia corta y residuales")
        scen.write_text("plateau")
        r = iterate(n=1000, block=100)
        check(r["ok"] and r["stopped"] == "plateau" and r["iteration"] >= 300, f"iterate marca plateau: {r.get('stopped')} en {r.get('iteration')} it")
        scen.write_text("drift")
        r = iterate(n=100, block=100)
        check(r["ok"] and r["stopped"] == "max" and r["iterations_run"] == 100, "iterate llega a n sin estabilizarse: stopped=max")
        r = iterate(block=250, window=1000)
        check(r["ok"] and r["stopped"] == "max" and r["iterations_run"] == 1000,
              f"iterate until=stable sin n usa 1000 por defecto ({r.get('iterations_run')} it)")
        scen.write_text("stable")
        r = iterate(n=30, until="n")
        check(r["ok"] and r["blocks"] == 1 and r["stopped"] == "max" and r["iteration"] == 30 and r["residuals"], "iterate until='n' es un solo bloque")
        r = iterate(n=50, reports=["Q_no_existe"])
        check(r["ok"] is False and "Q_no_existe" in r.get("error", ""), "iterate con un reporte inexistente falla con mensaje claro")

        # --- vigilante: macro en marcha, y apagado por inactividad (sin GUI)
        real_sim = _state().get("sim")
        _write_state({**_state(), "sim": str(tmp / "AGENT_FAKE_FAIL.sim")})
        r = server_stop(save=True)
        check(r["stopped"] is False and r.get("save_error") and _port_open(PORT), "server_stop con guardado fallido no apaga ni dice «saved»")
        _write_state({**_state(), "sim": real_sim})
        check(server_status()["alive"], "server_status vivo")
        IDLE_MIN = 0.001  # 0,06 s
        fake_m = MACROS / "Agent_19990101_000000_000"
        fake_m.with_suffix(".java").write_text("// macro callada", encoding="utf-8")
        fake_m.with_suffix(".out").write_text("AGENT_BEGIN 19990101_000000_000\n", encoding="utf-8")
        check(macro_running() and idle_seconds() == 0, "macro_running ve un .out sin AGENT_END")
        _idle_stop()
        check(_port_open(PORT) and STATE_FILE.exists(), "watchdog no apaga con una macro en marcha")
        fake_m.with_suffix(".pid").write_text("999999")  # su cliente murió: macro abandonada
        check(not macro_running(), "macro con cliente muerto no cuenta como en marcha")
        fake_m.with_suffix(".pid").unlink()
        fake_m.with_suffix(".out").write_text("AGENT_BEGIN x\nAGENT_END x\n", encoding="utf-8")
        check(not macro_running(), "macro con AGENT_END ya no cuenta")
        time.sleep(0.2)
        res_idle = _idle_stop()
        check(not _port_open(PORT) and not STATE_FILE.exists(), f"watchdog apaga el solver inactivo (y guarda) cuando no hay GUI ({res_idle})")
        IDLE_MIN = 30
        # puerto ocupado por otro proceso: se niega sin copiar el .sim
        srcsim = tmp / "otra.sim"
        srcsim.write_bytes(b"x")
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen(1)
            real_port, PORT = PORT, busy.getsockname()[1]
            try:
                server_start(sim_path=str(srcsim))
                msg = ""
            except RuntimeError as e:
                msg = str(e)
            PORT = real_port
        check("ocupado" in msg and not (SIMS / "otra.sim").exists(), "puerto ocupado: error claro y sin copiar el .sim")
        # otro proceso lo está arrancando (pid vivo, puerto cerrado, reciente): no se lanza un segundo
        with socket.socket() as sk2:
            sk2.bind(("127.0.0.1", 0))
            free = sk2.getsockname()[1]
        _write_state({"pid": os.getpid(), "port": free, "started": datetime.now().isoformat(timespec="seconds")})
        try:
            server_start()
            msg = ""
        except RuntimeError as e:
            msg = str(e)
        check("arrancando" in msg and STATE_FILE.exists(), "server_start no pisa un arranque en curso de otro proceso")
        # .server.json viejo (pid muerto): server_start limpia y relanza; un nombre raro no sale de sims/ ni rompe el Java
        _write_state({"pid": 999999, "port": PORT, "sim": None})
        st = server_start(name='../mi caso"x')
        check(st.get("pid") and st["pid"] != 999999 and _port_open(PORT), "server_start con .server.json viejo relanza")
        check(Path(st["sim"]).parent == SIMS and Path(st["sim"]).name == "mi_caso_x.sim", f"nombre raro saneado -> {Path(st['sim']).name}")

        # --- vigilante independiente del MCP: UN proceso aparte por puerto, con su propio entorno
        procs_f = tmp / "procs.json"
        procs_f.write_text(json.dumps([{"pid": 77, "name": "starccm+.exe", "cmd": f'"C:\\x\\starccm+.exe" -host localhost:{PORT}'}]),
                           encoding="utf-8")
        venv_env = {"COPILOTO_WORK": str(WORK), "COPILOTO_AGENT_LOG": str(AGENT_LOG), "COPILOTO_VIGILANTE_S": "0.25",
                    "COPILOTO_VIG_GONE_S": "1", "STARCCM_TEST_PROCS": str(procs_f), "STARCCM_IDLE_MIN": "0.012"}
        saved_env = {k: os.environ.get(k) for k in venv_env}
        os.environ.update(venv_env)
        VIGILANTE_ON, VIGILANTE_S = True, 0.25
        try:
            v = _vigilante_ensure(PORT)
            end = time.time() + 15
            while time.time() < end and not _state().get("idle_warning"):
                time.sleep(0.2)
            st, vpid = _state(), _vigilante_pid(PORT)
            check(v.get("nuevo") and vpid and _pid_alive(vpid) and v["vigilante"] != os.getpid(),
                  f"_vigilante_ensure lanza el vigilante como proceso aparte (pid {vpid})")
            iw = st.get("idle_warning")
            check(st.get("gui") is True and isinstance(iw, str) and "sin actividad" in iw and "GUI conectada: no se apaga" in iw
                  and isinstance(st.get("idle_min"), (int, float)) and st.get("idle_checked") and alive(),
                  f"vigilante con GUI: escribe gui, idle_min e idle_warning como TEXTO y no apaga ({iw!r})")
            check(time.time() - STATE_FILE.stat().st_mtime > 0.5, "lo que escribe el vigilante (cada 0,25 s) no cuenta como actividad: el mtime no se mueve")
            v2 = _vigilante_ensure(PORT)
            p2 = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--vigilante", str(PORT)],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            procs_f.write_text("[]", encoding="utf-8")  # el usuario cierra la GUI: sin GUI y pasado el límite, guarda y apaga
            end = time.time() + 15
            while time.time() < end and (_port_open(PORT) or STATE_FILE.exists()):
                time.sleep(0.2)
            check(not _port_open(PORT) and not STATE_FILE.exists() and "vigilante: solver apagado por" in AGENT_LOG.read_text(encoding="utf-8"),
                  "vigilante sin GUI: guarda y apaga el solver inactivo (el MCP no hace nada)")
            end = time.time() + 12
            while time.time() < end and _pid_alive(vpid):
                time.sleep(0.2)
            check(not _pid_alive(vpid) and not _vig_file(PORT).exists(), "el vigilante termina cuando el servidor muere")
            check(v2.get("vigilante") == vpid and not v2.get("nuevo") and p2.wait(timeout=30) == 0,
                  "un solo vigilante por puerto (un segundo, lanzado a mano o por server_start, sale sin sustituir al primero)")
        finally:
            for k, val in saved_env.items():
                os.environ.pop(k, None) if val is None else os.environ.__setitem__(k, val)
            VIGILANTE_ON, VIGILANTE_S = False, 30.0
            vp = _vig_info(PORT).get("pid")
            if vp:
                _kill_tree(vp)
        st = server_start(name="mi_caso_x")  # el resto del selftest sigue con un servidor vivo
        check(st.get("pid") and alive(st), "tras el apagado del vigilante, server_start arranca otro servidor")

        # --- GUI conectada
        rows = [{"pid": 1, "name": "starccm+.exe", "cmd": f'"C:\\x\\starccm+.exe" -host localhost:{PORT}'},
                {"pid": 2, "name": "starccm+.exe", "cmd": f'"C:\\x\\starccm+.exe" -host localhost:{PORT} -batch M.java -noexit'},
                {"pid": 3, "name": "starccm+.exe", "cmd": f"starccm+.exe -server -port {PORT} -host localhost:{PORT}"},
                {"pid": 4, "name": "starccm+.exe", "cmd": '"C:\\x\\starccm+.exe" -host localhost:1'},
                {"pid": 5, "name": "powershell.exe", "cmd": f"powershell ... starccm -host localhost:{PORT}"}]
        check(_gui_clients(rows, PORT) == [1], "_gui_clients: solo la GUI (sin -batch, sin -server, mismo puerto)")
        g = gui_status()
        check(g["connected"] is False and g["server_alive"], "gui_status sin GUI: no conectada")
        sl2 = subprocess.Popen([sys.executable, "-I", "-c", "import time;time.sleep(60)", "-host", "localhost:1"])
        sleepers.append(sl2)
        time.sleep(0.4)
        t_n = time.time()
        rows_n = _procs_native("-host") if os.name == "nt" else []
        t_n = time.time() - t_n
        check(os.name != "nt" or (any(x["pid"] == sl2.pid and "-host localhost:1" in x["cmd"] for x in rows_n) and t_n < 1.0),
              f"detección de GUI sin PowerShell: líneas de comandos leídas del PEB en {t_n * 1000:.0f} ms ({len(rows_n)} procesos con -host)")

        def spy():
            res = real_list()
            seen_rows.append(res)
            return res
        _list_procs = spy  # desde aquí el listado de procesos es el real (PowerShell, ~1,5 s por llamada)
        GUI_TTL = 60
        t0 = time.time()
        r = gui_attach(wait_s=20)
        gui_pid = r.get("launched")
        mine = [x for x in (seen_rows[-1] if seen_rows else []) if x["pid"] in r.get("pids", [])]
        check(r["ok"] and r["connected"] and mine, f"gui_attach lanza la GUI y se la ve conectada ({time.time()-t0:.1f}s, pids {r.get('pids')})")
        check(mine and "-podkey" not in mine[0]["cmd"] and KEY not in mine[0]["cmd"] and f"-host localhost:{PORT}" in mine[0]["cmd"],
              "la GUI se lanza con -host localhost:PORT y sin -podkey")
        _list_procs = lambda: list(seen_rows[-1])  # sin PowerShell: la misma foto
        check(gui_attach().get("already") is True, "gui_attach con GUI conectada no lanza otra")
        st = _state()
        check(server_status().get("gui") is True and st.get("port") == PORT, "server_status informa gui=True")
        # vigilante CON GUI: no apaga, deja idle_warning y eso no cuenta como actividad
        IDLE_MIN = 0.001
        time.sleep(0.2)
        _gui_cache = None
        _idle_stop()
        st = _state()
        check(alive() and st.get("gui") is True and isinstance(st.get("idle_warning"), str)
              and "GUI conectada: no se apaga" in st["idle_warning"] and "sin actividad" in st["idle_warning"],
              f"vigilante con GUI: no apaga y deja idle_warning como texto ({st.get('idle_warning')!r})")
        check(idle_seconds() > IDLE_MIN * 60, "escribir idle_warning no cuenta como actividad")
        IDLE_MIN = 30
        run_java('sim.println("vuelvo");')
        _idle_stop()
        check("idle_warning" not in _state(), "al haber actividad se borra idle_warning")
        # server_stop con GUI: avisa pero apaga (simulación nueva sin nombre: guarda aparte)
        _write_state({**_state(), "sim": None})
        rpid = _res_info().get("pid")
        r = server_stop(save=True)
        check(rpid and not _pid_alive(rpid) and not _res_info() and r.get("residente") == "detenido", "server_stop detiene el residente")
        check(r["stopped"] and r.get("gui_warning") and r.get("saved") and Path(r["saved"]).parent == SIMS and not STATE_FILE.exists(),
              "server_stop con GUI avisa (gui_warning), guarda en sims/nuevo_*.sim y apaga")
        _kill_tree(gui_pid)
        gui_pid = None
        _list_procs, GUI_TTL = (lambda: []), 10.0

        # --- pump_case con la bomba falsa
        PIPELINE_BOMBA = tmp / "bomba_falsa.py"
        cad = tmp / "cad"
        cad.mkdir()
        (tmp / "vacia").mkdir()
        (cad / "bomba.cfturbo-ccm").write_text("<ccm/>", encoding="utf-8")
        r = pump_case("BombaFalsa", str(cad))
        check(r["ok"] is False and "No existe" in r.get("error", ""), "pump_case sin pipelines/bomba.py: error claro")
        PIPELINE_BOMBA.write_text(_FAKE_BOMBA, encoding="utf-8")
        r = pump_case("BombaFalsa", str(cad), dry_run=True)
        check(r["ok"] and r["dry_run"] and r["ccm"]["rpm"] == 1450 and r["macro_chars"] > 50, "pump_case dry_run: genera la macro con la bomba falsa")
        check(pump_case("B", str(cad), mode="lenta")["ok"] is False and pump_case("B", str(tmp / "nada"))["ok"] is False
              and pump_case("B", str(tmp / "vacia"))["ok"] is False, "pump_case valida mode, cad_dir y .cfturbo-ccm")
        r = pump_case("BombaFalsa", str(cad), mode="rapida")
        check(r["ok"] and [s["ok"] for s in r["steps"]] == [True, True] and "importado 1450 rapida" in r["steps"][0]["stdout"]
              and (CASES / "BombaFalsa").is_dir() and Path(_state()["sim"]).stem == "BombaFalsa",
              f"pump_case arranca el servidor del caso e inyecta UNA macro ({r.get('seconds')}s; {r.get('error') or r.get('exception') or r.get('steps')})")
        pp = r.get("parsed", {})
        sal = CASES / "BombaFalsa" / "salida_pump.txt"
        check(pp.get("RESULT") == {"H": 16.5, "M": 8.4} and pp.get("PIPE") == {"total": 12.5} and pp.get("BALANCE_MASA", {}).get("pct") == 0.0002
              and pp.get("AVISO") == ["algo raro"] and pp.get("EULER", {}).get("EULER_OK", "").startswith("H_Euler 18") and pp.get("CELLS") == 1234,
              f"pump_case devuelve RESULT, PIPE, BALANCE_MASA, AVISO, EULER y CELLS ya leídos ({pp})")
        check(sal.is_file() and "RESULT\tH\t16.5" in sal.read_text(encoding="utf-8") and r.get("salida_completa") == str(sal)
              and "RESULT" in r["stdout"], "pump_case deja la salida completa en casos/<caso>/salida_pump.txt y sigue dando la cola de stdout")
        check(_pump_imports(type("M", (), {"IMPORTS": ["a.*", "b.*"]})) == "a.*,b.*" and _pump_imports(type("M", (), {})) == ""
              and "import 'a.*'" not in _wrap("", str(["a.*", "b.*"]), "x"),
              "imports del pipeline: la lista se une con comas (y _wrap sanea el str(lista) del fallo real)")
        e2e = HERE / "casos" / "BombaRapida_e2e_174159" / "e2e_stdout.txt"
        if e2e.is_file():
            q = _parse_pump(e2e.read_text(encoding="utf-8", errors="replace"))
            check(len(q.get("RESULT", {})) >= 9 and abs(q["RESULT"]["H"] - 16.738) < 0.01 and "iterar" in q.get("PIPE", {})
                  and q.get("BALANCE_MASA", {}).get("pct", 1) < 1e-3 and len(q.get("PNG", [])) == 3,
                  f"_parse_pump lee la salida REAL de la corrida e2e ({len(q.get('RESULT', {}))} resultados, {len(q.get('PIPE', {}))} fases)")
        r = pump_case("BombaFalsa", str(cad), mode="normal", params={"fallar": True})
        check(r["ok"] is False and r["failed_at"] == 1 and r["steps"][0]["ok"] is True, "pump_case: un paso que falla se reporta con su índice")
        scen.write_text("stable")
        n_launch = _res_launches
        r = pump_case("BombaFalsa", str(cad), until="stable", n=600, block=100)
        check(r["ok"] and r["via"] == "residente" and r["iterate"]["stopped"] == "stable" and r["iterate"]["blocks"] >= 2
              and _res_launches == n_launch, f"pump_case until=stable: una macro y luego bloques por el MISMO residente ({r.get('iterate')})")
        r = pump_case("OtroCaso", str(cad))
        check(r["ok"] is False and "otro .sim" in r.get("error", ""), "pump_case se niega si hay otro .sim cargado")

        # --- el servidor muere a mitad de una macro: error claro, no un «ok» vacío
        real_inject = _inject_local

        def dying(st_, src, timeout):
            _kill_tree(st_["pid"])
            time.sleep(0.5)
            out = src.with_suffix(".out")
            out.write_text("AGENT_BEGIN medio\n", encoding="utf-8")
            return 1, "conexión perdida", False, out
        _inject_local, RES_ON = dying, False
        try:
            r = run_java('sim.println("nunca");')
        finally:
            _inject_local, RES_ON = real_inject, True
        check(not r["ok"] and r.get("server_died") and "murió" in (r["exception"] or ""), "servidor muerto a mitad de macro: server_died + mensaje claro")
        check(not macro_running() and not LOCK_FILE.exists(), "tras el fallo, su .out queda cerrado y el turno liberado")
        check(not server_status()["alive"], "server_status: no vivo tras morir")
        r2 = server_stop()
        check(r2["stopped"] is False or r2.get("saved") is None, "server_stop con el servidor muerto no falla")
        check(KEY not in AGENT_LOG.read_text(encoding="utf-8"), "agent.log sin la clave")
        # otra sesión puede estar usando el solver real mientras tanto: solo miramos que lo nuevo no sea del falso ni nuestro
        new = [Path(p) for p, _ in _real_files() - real_before]
        check(not any(p.suffix in (".log", ".out", ".json") and "FAKE" in p.read_text(encoding="utf-8", errors="replace")[:400] for p in new if p.is_file()),
              "sims/, macros/ y .server.json reales sin rastro del starccm+ falso")
        check((real_panel.stat().st_mtime_ns if real_panel.is_file() else None) == real_panel_before, "panel_estado.json real intacto")
        mine_real = real_lock.is_file() and f'"pid": {os.getpid()}' in real_lock.read_text(encoding="utf-8", errors="replace")
        check(not mine_real, ".inject.lock real intacto")
        for t in threads:
            t.join(timeout=60)
        dt = bg.get("detached")
        if os.name == "nt":
            ok_det = isinstance(dt, tuple) and dt[0] == (True, True, True) and dt[1] is False and dt[2] is False and dt[3] is True
            check(ok_det, f"taskkill /T /F real sobre el padre: muere el hijo normal y sobrevive el lanzado con _spawn_detached "
                          f"(vivos antes/después padre,hijo,desacoplado = {dt if not isinstance(dt, tuple) else (dt[0], dt[1:4])}; breakaway {dt[4] if isinstance(dt, tuple) else '?'})")
        if os.name == "nt":
            check("1234" in str(bg.get("bat")), f"un .bat lanzado con _spawn_detached conserva la salida de sus hijos de consola ({str(bg.get('bat'))!r})")
        rg = bg.get("regresion")
        if isinstance(rg, str):
            print(f"  {time.time() - t_all:5.1f}s SKIP {rg}: macro real de pipelines/bomba.py con javac")
        else:
            check(isinstance(rg, list) and all(not bad and rc == 0 for _, bad, rc, _ in rg) and len(rg) == 2,
                  "macro REAL de pipelines/bomba.py (rapida e iterar+post) envuelta con _wrap y los imports de pump_case: compila con el javac de STAR-CCM+"
                  + ("" if isinstance(rg, list) else f" ({rg!r})") + (f" {[(n, b, e) for n, b, rc, e in rg if b or rc]}" if isinstance(rg, list) else ""))
        secs = time.time() - t_all
        check(secs < 45, f"el selftest tarda {secs:.1f} s (< 45 s)")
    finally:
        for pid in [gui_pid] + [s.pid for s in sleepers]:
            if pid:
                _kill_tree(pid)
        st = _state()
        if st.get("pid") and _pid_alive(st["pid"]):
            _kill_tree(st["pid"])
        _res_kill("selftest")
        for vf in WORK.glob("vigilante-*.json"):
            try:
                _kill_tree(int(json.loads(vf.read_text(encoding="utf-8")).get("pid") or 0))
            except (OSError, ValueError):
                pass
        os.environ.pop("FAKE_RESIDENT_MODE", None)
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"selftest: {len(fails)} fallos" if fails else "selftest: todo en verde")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    if "--vigilante" in sys.argv:  # proceso independiente del MCP: python mcp_server.py --vigilante <puerto>
        sys.exit(_vigilante_main(int(sys.argv[sys.argv.index("--vigilante") + 1])))
    _migrate_legacy(HERE, STATE_FILE, LOCK_FILE)  # .server.json/.inject.lock de cuando vivían en el repo (OneDrive)
    _write_panel_estado()
    start_watchdog()
    mcp.run()

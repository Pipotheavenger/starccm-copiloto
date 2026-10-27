"""MCP para operar Simcenter STAR-CCM+ (2410, Windows) desde Claude Code y desde el panel de chat.

Arquitectura: UN servidor de STAR-CCM+ vivo con el .sim en memoria (starccm+ -server -port P). Cada
tool inyecta una macro Java con un cliente batch (`starccm+ -host localhost:P -batch M.java -noexit`)
y lee lo que la macro imprimió con sim.println entre los marcadores AGENT_BEGIN/AGENT_END.
El usuario puede mirar con la GUI real (`gui_attach`: starccm+ -host localhost:P, sin licencia propia).
No se recarga el .sim entre pasos. Claude nunca resuelve física a mano: STAR-CCM+ es el solver.

Tools (16): server_start, server_stop, server_status, gui_attach, gui_status, cancel, inspect, run_java, run_steps,
iterate, pump_case, export_scene, save, recipe_search, recipe_save, docs_search.

Progreso en vivo: mientras iterate o pump_case corren, un hilo escribe estado_dir()/progreso.json (cada ~1 s: fase, iteración, H, M,
eta, continuidad, transcurrido y lo que falta) leyendo el .out de la macro; al terminar deja fase "terminado" con el resultado.
cancel(): crea el archivo ABORT (criterio «Stop File» de STAR-CCM+, en sims/) y cancel.json; iterate/pump_case lo ven y paran limpio.

Turnos: toda inyección toma el candado de archivo `.inject.lock` (pid, herramienta, inicio). Si otro proceso
vivo lo tiene, se espera `wait_s` y si no se devuelve {"ok": false, "busy": "..."}. Huérfano (pid muerto o sin
latido en 2 h) se limpia solo.

Uso:   python mcp_server.py            (stdio, lo lanza Claude Code desde .mcp.json)
       python mcp_server.py --selftest (starccm+ falso de tests/, sin licencia ni solver, < 45 s)
       python mcp_server.py --vigilante <puerto>  (lo lanza server_start; no se llama a mano)
       python mcp_server.py --cli <herramienta> [json-args]  (acción directa SIN modelo: gui_attach, gui_status, server_stop,
                        server_status, cancel; escribe el resultado JSON por stdout; respeta el candado de turnos)

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
                   macros/, residente/ (inbox, out, latido, log del residente), tiempos.csv y agent.log.
  COPILOTO_CANCEL_GRACE_S  s mínimos SIN avance de la macro, tras un cancel, para darla por colgada y abandonarla (120; nunca menos de
                   3 bloques de 50 it según ritmo_it_s). Mientras avance se espera el fin del bloque: matar el residente a mitad de step() tumba el servidor.

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
AGENT_LOG = Path(os.environ.get("COPILOTO_AGENT_LOG") or WORK / "agent.log")  # en la carpeta de trabajo (no en OneDrive)
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
MAX_HORAS = float(os.environ.get("STARCCM_MAX_HORAS", "0") or 0)  # tope DURO de horas del solver encendido (0 = sin tope): el vigilante guarda y apaga, con o sin GUI
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


def _replace_retry(tmp: Path, dst: Path, tries: int = 8) -> None:
    """os.replace con reintentos: en Windows falla con WinError 5 si otro proceso tiene `dst` abierto un instante (visto con el
    vigilante, que moría en su primer latido y dejaba el solver sin apagado por inactividad)."""
    for i in range(tries):
        try:
            os.replace(tmp, dst)
            return
        except PermissionError:
            if i == tries - 1:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise
            time.sleep(0.03 * (i + 1))


def _write_state(d: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")  # un tmp por proceso: el vigilante y el MCP escriben a la vez (visto: WinError 5)
    tmp.write_text(json.dumps(d, indent=1, ensure_ascii=False), encoding="utf-8")
    _replace_retry(tmp, STATE_FILE)


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


def _proc_ctime(pid: int) -> float | None:
    """Hora de creación (epoch s) del proceso `pid`, o None si no existe o no se puede leer. Windows: GetProcessTimes por ctypes
    (microsegundos, sin lanzar procesos); otros SO: None (ahí el PID reasignado no se comprueba por hora)."""
    if os.name != "nt" or not pid:
        return None
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    k32.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    h = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        c, e, k, u = (wintypes.FILETIME() for _ in range(4))
        if not k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
            return None
        return ((c.dwHighDateTime << 32 | c.dwLowDateTime) - 116444736000000000) / 1e7
    finally:
        k32.CloseHandle(h)


def _proc_exe(pid: int) -> str | None:
    """Nombre del ejecutable del proceso `pid` (QueryFullProcessImageNameW: sin lanzar `tasklist`, ~1 ms en vez de 0,3-1 s). None si no se puede."""
    if os.name != "nt" or not pid:
        return None
    import ctypes
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    k32.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32)]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    h = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        buf, n = ctypes.create_unicode_buffer(1024), ctypes.c_uint32(1024)
        return os.path.basename(buf.value) if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)) else None
    finally:
        k32.CloseHandle(h)


_MY_CTIME: list = []  # hora de creación de ESTE proceso (se calcula una vez)


def _my_ctime() -> float | None:
    if not _MY_CTIME:
        _MY_CTIME.append(_proc_ctime(os.getpid()))
    return _MY_CTIME[0]


def _same_proc(pid: int, t_create: float | None, tol: float = 2.0) -> bool:
    """¿El proceso `pid` es el que guardamos? Compara la hora de creación (con tolerancia). Sin hora guardada o sin poder leerla
    se da por bueno (el que llama ya comprobó `_pid_alive`); otra hora = Windows reasignó el PID a otro proceso."""
    if not t_create:
        return True
    now = _proc_ctime(pid)
    return now is None or abs(now - float(t_create)) < tol


def _pid_is_ours(pid: int, t_create: float | None = None, t_ref: float | None = None) -> bool:
    """Evita matar un proceso ajeno si Windows reasignó el PID guardado (el PID es el del .bat/starccm+). Con `t_create` (hora de
    creación guardada al lanzarlo) manda ella: otra hora = otro proceso. Sin ella (registro viejo), se mira el nombre del exe y,
    con `t_ref` (hora en que se guardó el registro), que el proceso NO sea más nuevo que el registro: un PID reasignado después
    de guardarlo es un proceso creado después."""
    if os.name != "nt":
        return True
    if t_create:
        now = _proc_ctime(pid)
        if now is not None:
            return abs(now - float(t_create)) < 2.0
    elif t_ref:
        now = _proc_ctime(pid)
        if now is not None and now > float(t_ref) + 2.0:
            return False
    exe = _proc_exe(pid)
    if exe is None:  # sin acceso al proceso: el listado de tareas
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=30).stdout
        m = re.match(r'"([^"]+)"', out.strip())
        exe = m.group(1) if m else None
    return bool(exe) and re.search(r"cmd|star|java|python|mpi|ccm|hydra|smpd", exe, re.I) is not None


PORT_PROBE_S = 1.0  # sondeo por defecto de _port_open. Un puerto cerrado en Windows bloquea TODO el timeout (medido), y un servidor ocupado puede tardar en aceptar: por eso el bucle de arranque usa uno corto (reintenta)


def _started_ts(st: dict) -> float | None:
    """Epoch de `started` (ISO) de .server.json: referencia para _pid_is_ours cuando no hay t_create."""
    try:
        return datetime.fromisoformat(st["started"]).timestamp()
    except (KeyError, ValueError, TypeError):
        return None


def _port_open(port: int, host: str = "127.0.0.1", timeout: float | None = None) -> bool:
    try:
        with socket.create_connection((host, port), timeout=PORT_PROBE_S if timeout is None else timeout):
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
    py = getattr(sys, "_base_executable", None)  # el intermedio solo usa la biblioteca estándar: el intérprete base se salta el redirector del venv (un proceso menos, ~0,1 s)
    py = py if py and os.path.isfile(py) else sys.executable
    p = subprocess.Popen([py, "-I", "-c", _LAUNCHER], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
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
         "work": str(WORK), "residente_dir": str(RES_DIR), "vigilante_file": str(_vig_file(PORT))}  # latido (t) del vigilante
    try:
        if json.loads(PANEL_ESTADO.read_text(encoding="utf-8")) == d:
            return
    except (OSError, ValueError):
        pass
    try:
        tmp = PANEL_ESTADO.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(d, indent=1, ensure_ascii=False), encoding="utf-8")
        _replace_retry(tmp, PANEL_ESTADO)
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
    return (not _pid_alive(pid)) or not _same_proc(pid, info.get("t_create")) or age > 2 * 3600


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
                json.dump({"pid": os.getpid(), "t_create": _my_ctime(), "tool": tool, "start": datetime.now().isoformat(timespec="seconds"),
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
        if pf.is_file() and (RES_DIR / "out" / (o.stem + ".java.res")).is_file():
            continue  # la macro del residente ya terminó (el dueño pudo morir sin cerrar el .out: cancel con dueño muerto)
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
    if _vig_self:  # solo el proceso vigilante deja su pid (real) y los de los procesos reales del servidor y del residente
        cur["vigilante"] = _vig_self
        ri = _res_info()
        rp = ri.get("pid") if ri.get("server_pid") == cur.get("pid") and ri.get("pid") and _pid_alive(ri["pid"]) else None
        cur["residente"] = rp
        try:
            cur["pid_real"] = _real_pid(cur["pid"], "-server") if os.name == "nt" else cur["pid"]
            cur["residente_real"] = _real_pid(rp, "Residente.java") if rp else None
        except Exception:  # noqa: BLE001
            pass
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
    lo que ven todos los procesos. Devuelve "sin_servidor" | "ok" | "aviso" | "apagado" | "apagado_tope" (STARCCM_MAX_HORAS) | "no_apagado"."""
    st = _state()
    if not (st.get("pid") and alive(st)):
        return "sin_servidor"
    _cancel_stale_cleanup()
    if MAX_HORAS > 0:  # tope duro (PoD cobra por hora; una GUI olvidada no tiene otro límite): guarda y apaga aunque haya GUI o actividad
        t0 = _started_ts(st)
        if t0 and time.time() - t0 > MAX_HORAS * 3600:
            horas = (time.time() - t0) / 3600
            with _lock:
                r = server_stop(save=True)
            if r.get("save_error") or r.get("busy"):
                _log(f"vigilante: tope de {MAX_HORAS:g} h superado ({horas:.2f} h) pero no apago: {r.get('save_error') or r.get('busy')}; reintento")
                return "no_apagado"
            _log(f"vigilante: TOPE DURO STARCCM_MAX_HORAS={MAX_HORAS:g} h superado ({horas:.2f} h encendido): solver apagado (guardado: {r.get('saved')})")
            return "apagado_tope"
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
_vig_self: int | None = None  # pid de ESTE proceso si es el vigilante (lo publica en .server.json)
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
    real, t_end = None, time.time() + 3.0  # el pid del lanzador puede ser el redirector del venv: el vigilante da el suyo en su archivo
    while time.time() < t_end:
        real = _vigilante_pid(port)
        if real:
            break
        time.sleep(0.05)
    _log(f"vigilante lanzado pid {real or sp['pid']} (puerto {port}, breakaway {sp.get('breakaway')}"
         + (f", lanzador {sp['pid']}" if real and real != sp["pid"] else "") + ")")
    return {"vigilante": real or sp["pid"], "nuevo": True}


def _vigilante_main(port: int) -> int:
    global _vig_self
    me = _vig_self = os.getpid()
    if _vigilante_pid(port):
        return 0  # ya hay uno
    f = _vig_file(port)
    parar = threading.Event()

    def beat() -> None:
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(f".{threading.get_ident()}.tmp")  # un tmp por hilo: el latido y el chequeo escriben a la vez
        tmp.write_text(json.dumps({"pid": me, "port": port, "t": time.time(), "every_s": VIGILANTE_S}), encoding="utf-8")
        _replace_retry(tmp, f)

    def latido() -> None:  # hilo aparte: un _idle_stop → server_stop largo (guardar un .sim grande) no puede hacerlo pasar por muerto
        while not parar.wait(min(5.0, VIGILANTE_S)):
            try:
                beat()
            except OSError:
                pass

    try:
        beat()
    except OSError as e:  # el latido del hilo lo reintenta en 5 s: no morir por un archivo ocupado
        _log(f"vigilante {me}: primer latido falló ({e}); sigo")
    threading.Thread(target=latido, daemon=True).start()
    _log(f"vigilante {me} vigila el puerto {port} (cada {VIGILANTE_S:g} s, apaga a los {IDLE_MIN:g} min sin GUI)")
    last, gone_since = 0.0, None
    hold = float(os.environ.get("COPILOTO_TEST_VIG_BLOCK_S") or 0)  # gancho de pruebas: el primer chequeo tarda tanto (un server_stop largo)
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
                    if hold:
                        time.sleep(hold)
                        hold = 0.0
                    _idle_stop()
                except Exception as e:  # noqa: BLE001
                    _log(f"vigilante: {type(e).__name__}: {e}")
            time.sleep(min(1.0, VIGILANTE_S))
    finally:
        parar.set()
        time.sleep(0.05)
        if _vig_info(port).get("pid") == me:
            f.unlink(missing_ok=True)
    _log(f"vigilante {me} termina: el servidor del puerto {port} ya no está")
    return 0


def vigilante_relanzar() -> dict:
    """Relanza el vigilante del servidor vivo si está muerto (el panel lo llama por `--cli vigilante_relanzar`). No hace nada si ya
    hay uno vivo o no hay servidor. Devuelve vigilante (pid), nuevo y vigilante_vivo."""
    st = _state()
    if not (st.get("pid") and alive(st)):
        return {"ok": True, "vigilante": None, "vigilante_vivo": False, "reason": "no hay servidor vivo"}
    r = _vigilante_ensure(int(st["port"]))
    return {"ok": bool(r.get("vigilante")), **r, "vigilante_vivo": bool(_vigilante_pid(int(st["port"])) or r.get("nuevo"))}


def start_watchdog() -> None:
    """Al arrancar el MCP: si ya hay un solver vivo (de otra sesión o de antes), asegura su vigilante."""
    try:
        st = _state()
        if st.get("pid") and alive(st):
            _vigilante_ensure(int(st["port"]))
    except Exception as e:  # noqa: BLE001
        _log(f"watchdog: {e}")


def _log(msg: str) -> None:
    try:
        AGENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
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
            pid, name, ppid = pe.th32ProcessID, pe.szExeFile, pe.th32ParentProcessID
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
                    rows.append({"pid": pid, "name": name, "cmd": cmd, "ppid": ppid})
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


def _real_pid(root: int | None, needle: str) -> int | None:
    """Pid del proceso REAL lanzado desde `root`: el descendiente más profundo cuya línea de comandos contiene `needle`. Un python
    de un venv de Windows es un redirector que lanza al intérprete de verdad como hijo, y un .bat lanza a cmd y luego al solver:
    el pid que devuelve el lanzador es el del intermediario. Sin descendiente que coincida (o fuera de Windows) vuelve `root`."""
    if not root or os.name != "nt":
        return root
    try:
        rows = _procs_native(needle)
    except Exception:  # noqa: BLE001
        return root
    kids: dict[int, list] = {}
    for r in rows:
        kids.setdefault(r.get("ppid"), []).append(r)
    best, depth, stack = root, 0, [(root, 0)]
    while stack:
        pid, d = stack.pop()
        for k in kids.get(pid, []):
            if str(k.get("name", "")).lower().startswith("conhost"):
                continue
            if d + 1 > depth:
                best, depth = k["pid"], d + 1
            stack.append((k["pid"], d + 1))
    return best


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


KEEP_RES_LOGS = 3   # residente-*.log que se conservan (los más recientes)
PRUNE_DAYS = 7      # macros/.classes y macros/.cache más viejos que esto se borran


def _prune_work() -> dict:
    """Mantenimiento de la carpeta de trabajo: deja los KEEP_RES_LOGS últimos residente-*.log y borra de macros/.classes y
    macros/.cache lo de más de PRUNE_DAYS días. Devuelve lo borrado."""
    out = {"residente_logs": 0, "classes": 0}
    try:
        logs = sorted(RES_DIR.glob("residente-*.log"), key=lambda q: q.stat().st_mtime, reverse=True)
        for q in logs[KEEP_RES_LOGS:]:
            try:
                q.unlink()
                out["residente_logs"] += 1
            except OSError:
                pass
    except OSError:
        pass
    cutoff = time.time() - PRUNE_DAYS * 86400
    for d in (MACROS / ".classes", MACROS / ".cache"):
        if not d.is_dir():
            continue
        try:
            for f in d.rglob("*"):
                try:
                    if f.is_file() and f.stat().st_mtime < cutoff:
                        f.unlink()
                        out["classes"] += 1
                except OSError:
                    pass
            for sub in sorted((x for x in d.rglob("*") if x.is_dir()), key=lambda x: len(x.parts), reverse=True):
                try:
                    sub.rmdir()  # solo las vacías
                except OSError:
                    pass
        except OSError:
            pass
    return out


def _migrate_agent_log() -> bool:
    """agent.log vivía en el repo (OneDrive); ahora en la carpeta de trabajo. Si el viejo existe y el nuevo no, se mueve."""
    old = HERE / "agent.log"
    try:
        if not os.environ.get("COPILOTO_AGENT_LOG") and old.is_file() and old.resolve() != AGENT_LOG.resolve() and not AGENT_LOG.exists():
            AGENT_LOG.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old), str(AGENT_LOG))
            return True
    except OSError:
        pass
    return False


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
    if not _cancel_requested():
        _clear_abort()  # un ABORT que quedó de otra corrida pararía esta al instante
        if getattr(_ctx, "t_start", None) is not None:  # (y su cancel.json: la macro de iterar también lo mira)
            try:
                cancel_file().unlink(missing_ok=True)
            except OSError:
                pass
    _prog.watch(src.with_suffix(".out"))
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


def _cancel_stall_s() -> float:
    """s sin avance de la macro, tras un cancel, antes de abandonarla: max(CANCEL_GRACE_S, CANCEL_BLOCK_X × tiempo de un bloque de 50 it)."""
    try:
        rate = _prog._rate()
    except Exception:  # noqa: BLE001
        rate = None
    return max(CANCEL_GRACE_S, CANCEL_BLOCK_X * 50.0 / rate if rate and rate > 0 else 0.0)


def _hb_age() -> float:
    try:
        return time.time() - (RES_DIR / "heartbeat").stat().st_mtime
    except OSError:
        return 1e9


def _res_healthy(st: dict) -> bool:
    ri = _res_info()
    return (bool(ri.get("pid")) and ri.get("server_pid") == st.get("pid") and _pid_alive(ri["pid"])
            and _same_proc(ri["pid"], ri.get("t_create")) and _hb_age() < RES_HB_STALE)


def _res_kill(why: str) -> None:
    """Mata el residente (nunca el servidor) y borra su estado."""
    ri = _res_info()
    if ri.get("pid") and _pid_alive(ri["pid"]):
        if _pid_is_ours(ri["pid"], ri.get("t_create"), ri.get("t")):
            _log(f"residente {ri['pid']} matado: {why}")
            _kill_tree(ri["pid"])
        else:
            _log(f"residente {ri['pid']}: el PID ya es de otro proceso (hora de creación distinta); no lo mato")
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
    info = {"pid": sp["pid"], "t_create": _proc_ctime(sp["pid"]), "server_pid": st.get("pid"), "port": st.get("port"), "log": str(log), "t": t_launch,
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
    t_cancel, cancelled, last_grow, stall = None, False, time.time(), 0.0
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
                if new:
                    last_grow = now  # la macro avanza (ITER por bloque): no está colgada
                _touch_lock()
                if expect_death and not alive(st):  # (cada 0,4 s)
                    break  # sim.kill(): el servidor se fue, no habrá resultado
                if _cancel_requested():  # cancel(): ABORT ya puesto; si la macro no acaba sola en CANCEL_GRACE_S se abandona
                    _prog.mark_cancel()
                    if t_cancel is None:
                        t_cancel = now
                    elif getattr(_ctx, "cancel_kill", False) and now - max(t_cancel, last_grow) > _cancel_stall_s():
                        # ponytail: solo con la macro sin sacar nada > max(CANCEL_GRACE_S, 3 bloques); matar a mitad de step() tumba el servidor
                        stall = now - max(t_cancel, last_grow)
                        killed = cancelled = True
                        break
            time.sleep(RES_POLL)
        if killed or died:
            reason = (f"cancelado por el usuario: macro COLGADA (sin avance {stall:.1f} s tras el ABORT) y abandonada; se perdió el bloque en curso" if cancelled else
                      f"timeout {timeout}s" if killed else "el residente murió o dejó de dar latido") + \
                     "; residente matado (el servidor sigue vivo) y relanzado"
            _res_kill(reason)
            (RES_DIR / "inbox" / name).unlink(missing_ok=True)
            relaunch = ""
            if alive(st):
                try:
                    if cancelled:  # sin esperar su latido: la siguiente inyección lo espera
                        _res_spawn(st)
                        ok2, relaunch = True, ""
                    else:
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
    if cancelled:
        r["cancelled"] = True
        r["abandoned"] = True  # solo si la macro quedó colgada tras el cancel (nunca por tardar un bloque)
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


# ----------------------------------------------------------------------------- progreso en vivo y cancelación
# Mientras iterate o pump_case corren, un hilo del proceso dueño del turno escribe estado_dir()/progreso.json cada ~1 s leyendo el
# .out de la macro en curso (que el cliente residente va llenando en vivo): marcadores PIPE (fases), ITER, ITERATION/REPORT y la
# tabla de monitores del solver (Iteration, Continuity, …, M, H). El panel solo lee ese archivo.
PROG_TOOLS = ("iterate", "pump_case")
PROG_EVERY_S = 1.0
CANCEL_GRACE_S = float(os.environ.get("COPILOTO_CANCEL_GRACE_S", "120"))  # tras cancel: mínimo de s SIN avance (salida de la macro) para darla por colgada. Matar el residente a mitad de step() TUMBA el servidor (2026-10-26): por defecto se espera el fin del bloque
CANCEL_BLOCK_X = 3.0  # ...y nunca menos de CANCEL_BLOCK_X × lo que tarda un bloque de 50 it según ritmo_it_s (0 lo desactiva: selftest)
FASES = ["importar", "regiones", "fisica", "mrf", "fronteras", "reportes", "malla", "euler", "iterar", "post"]  # orden de los PIPE
_FASE_NOMBRE = {"malla": "mallar"}
_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_ROW = re.compile(rf"^\s*(\d+)((?:\s+(?:{_NUM}|NaN|-?Infinity))+)\s*$")
_VAL_KEYS = {"h": "H", "m": "M", "eta": "eta", "continuity": "continuidad"}


def progreso_file() -> Path:
    return estado_dir() / "progreso.json"


def cancel_file() -> Path:
    return estado_dir() / "cancel.json"


def abort_file() -> Path:
    """Archivo ABORT que vigila el criterio «Stop File» de STAR-CCM+ (por defecto «ABORT» en el directorio de trabajo del servidor,
    que server_start fija en sims/). Doc: Stopping Criteria > Setting a Stop File."""
    return SIMS / "ABORT"


def _write_json_atomic(path: Path, d: dict) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.{threading.get_ident()}.tmp")
    for _ in range(4):  # (Windows: replace falla un instante si el panel lo tiene abierto)
        try:
            tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
            _replace_retry(tmp, path, tries=3)
            return True
        except OSError:
            time.sleep(0.05)
    try:
        path.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        return True
    except OSError:
        return False


def _clean(o):
    """Sin NaN/Infinity (no son JSON válido para el panel)."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t).isoformat(timespec="seconds")


def _read_json(path: Path) -> dict:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


class _Progreso:
    """Estado de la corrida en curso + hilo que lo vuelca a progreso.json. Una sola corrida a la vez (el turno del solver)."""

    def __init__(self):
        self.lk = threading.RLock()
        self.active = False
        self.owner = None
        self.gen = 0
        self._reset("", None)

    def _reset(self, tool, caso):
        self.tool, self.caso = tool, caso
        self.t0 = self.t_fase = time.time()
        self.fkey = "arrancar"
        self.hechas: dict = {}
        self.it = self.it0 = self.t_it = self.r0 = None  # r0 = (iteración, t) de la primera lectura: base del ritmo
        self.vals: dict = {}
        self.hist: list = []  # (it, H, M)
        self.cols = None
        self.iter_max, self.window, self.tol, self.criterio = None, 100, 0.005, "stable"
        self.cancel = False
        self._eta_prev = None  # (falta_s, t) de la muestra anterior: suaviza la ETA
        self.out, self.pos, self.buf = None, 0, ""
        self.ref: dict = {}

    # --- ciclo de vida
    def begin(self, tool: str, caso=None, fase: str = "arrancar", **conf) -> bool:
        """True si esta llamada toma la corrida (pump_case llama luego a iterate: la anidada no la reinicia, solo la configura)."""
        with self.lk:
            if self.active:
                self.configure(**conf)
                return False
            self._reset(tool, caso)
            self.ref = _read_json(estado_dir() / "fases_ref.json")
            self.fkey = fase
            self.configure(**conf)
            self.active = True
            self.owner = threading.get_ident()
            self.gen += 1
            gen = self.gen
            self._write()
        threading.Thread(target=self._loop, args=(gen,), daemon=True).start()
        return True

    def configure(self, iter_max=None, window=None, tol=None, criterio=None, caso=None) -> None:
        with self.lk:
            if iter_max is not None:
                self.iter_max = int(iter_max)
            if window is not None:
                self.window = max(1, int(window))
            if tol is not None:
                self.tol = float(tol)
            if criterio is not None:
                self.criterio = criterio
            if caso:
                self.caso = caso

    def set_fase(self, key: str) -> None:
        with self.lk:
            if self.active:
                self._fase(key)
                self._write()

    def watch(self, out_path) -> None:
        """Pasa a leer el .out de la macro que empieza (antes termina de leer el de la anterior)."""
        with self.lk:
            if not self.active:
                return
            self.feed(final=True)
            self.out, self.pos, self.buf = Path(out_path), 0, ""

    def mark_cancel(self) -> None:
        with self.lk:
            if self.active and not self.cancel:
                self.cancel = True
                self._write()

    def update(self, it: int, done: int, reports: dict) -> None:
        """Lo que sabe iterate al final de un bloque (respaldo de la lectura del .out)."""
        with self.lk:
            if not self.active:
                return
            self.it0 = it - done + 1
            self._iter(it)
            for k, v in (reports or {}).items():
                if isinstance(v, (int, float)):
                    self._val(k, float(v))
            self._hist(it)
            self._write()

    def end(self, res=None, err=None) -> None:
        with self.lk:
            if not self.active or self.owner != threading.get_ident():  # solo termina quien la empezó
                return
            self.feed(final=True)
            self.active = False
            self.gen += 1
            resultado = self._resumen(res, err)
            self._write(terminado=True, resultado=resultado)
            if isinstance(res, dict) and isinstance((res.get("parsed") or {}).get("PIPE"), dict):
                ref = {k: v for k, v in res["parsed"]["PIPE"].items() if k in FASES and isinstance(v, (int, float))}
                if ref:
                    _write_json_atomic(estado_dir() / "fases_ref.json", {**self.ref, **ref})

    @staticmethod
    def _resumen(res, err) -> dict:
        if err:
            return {"ok": False, "error": str(err)[:500]}
        r = res if isinstance(res, dict) else {}
        out = {k: r[k] for k in ("ok", "stopped", "iterations_run", "iteration", "seconds", "cancelled", "error", "exception", "busy",
                                 "failed_at") if k in r}
        it = r.get("iterate")
        if isinstance(it, dict):
            out["iterate"] = {k: it[k] for k in ("ok", "stopped", "iterations_run", "iteration", "reports", "variation_pct") if k in it}
        rr = (r.get("parsed") or {}).get("RESULT")
        if rr:
            out["RESULT"] = rr
        elif isinstance(r.get("reports"), dict):
            out["reports"] = r["reports"]
        return out

    # --- lectura del .out
    def feed(self, final: bool = False) -> None:
        p = self.out
        if p:
            try:
                sz = p.stat().st_size
                if sz < self.pos:
                    self.pos, self.buf = 0, ""
                if sz > self.pos:
                    with open(p, "rb") as f:
                        f.seek(self.pos)
                        data = f.read(4_000_000)
                    self.pos += len(data)
                    self.buf += data.decode("utf-8", "replace")
            except OSError:
                pass
        lines = self.buf.split("\n")
        self.buf = "" if final else lines.pop()
        for ln in lines:
            self._line(ln)

    def _fase(self, key: str) -> None:
        if key != self.fkey:
            self.fkey, self.t_fase = key, time.time()

    def _val(self, name: str, v: float) -> None:
        k = _VAL_KEYS.get(re.sub(r"\s*\(.*\)\s*$", "", str(name)).strip().lower())
        if k:
            self.vals[k] = v

    def _iter(self, it: int) -> None:
        now = time.time()
        if self.fkey != "post":
            self._fase("iterar")
        if self.it0 is None:
            self.it0 = it
        if self.r0 is None:
            self.r0 = (it, now)
        self.it, self.t_it = it, now

    def _hist(self, it: int) -> None:
        if not self.hist or it > self.hist[-1][0]:
            self.hist.append((it, self.vals.get("H"), self.vals.get("M")))
            if len(self.hist) > 20000:
                del self.hist[:5000]

    def _line(self, ln: str) -> None:
        s = ln.strip()
        if not s:
            return
        if s.startswith("PIPE "):
            m = re.match(rf"PIPE (\S+) ({_NUM})$", s)
            if m:
                nm = m.group(1)
                self.hechas[nm] = float(m.group(2))
                if nm in FASES and nm != "post":
                    self._fase(FASES[FASES.index(nm) + 1])
            return
        if s.startswith("ITER_STOP"):
            if self.fkey == "iterar":
                self._fase("post")
            return
        if s.startswith("ITER\t"):
            parts = s.split("\t")
            try:
                it = int(parts[1])
            except (ValueError, IndexError):
                return
            self._iter(it)
            for p in parts[2:]:
                m = re.match(rf"(\w+) ({_NUM}|NaN)$", p.strip())
                if m:
                    self._val(m.group(1), float(m.group(2)))
            self._hist(it)
            return
        m = re.match(r"ITERATION (\d+)$", s)
        if m:
            self._iter(int(m.group(1)))
            return
        if s.startswith("REPORT\t"):
            parts = s.split("\t")
            if len(parts) == 3:
                try:
                    self._val(parts[1], float(parts[2]))
                except ValueError:
                    pass
            return
        if s.startswith("Iteration"):  # cabecera de la tabla de monitores
            cols = re.split(r"\s{2,}", s)
            if len(cols) >= 3:
                self.cols = cols
            return
        if self.cols:
            m = _ROW.match(ln)
            if m:
                vals = [m.group(1)] + m.group(2).split()
                if len(vals) == len(self.cols):
                    it = int(vals[0])
                    self._iter(it)
                    for c, v in zip(self.cols[1:], vals[1:]):
                        try:
                            self._val(c, float(v))
                        except ValueError:
                            pass
                    self._hist(it)

    # --- estimación
    def _var(self, end: float, w: int):
        """(máx − mín)/|media| de H y M en las `w` iteraciones que acaban en `end`; None si aún no hay tanta historia."""
        if not self.hist or self.hist[0][0] > end - w:
            return None
        worst = None
        for idx in (1, 2):
            xs = [h[idx] for h in self.hist if end - w <= h[0] <= end and h[idx] is not None and math.isfinite(h[idx])]
            if len(xs) >= 2:
                v = (max(xs) - min(xs)) / max(abs(sum(xs) / len(xs)), 1e-30)
                worst = v if worst is None else max(worst, v)
        return worst

    def _rate(self):
        if self.r0 and self.it is not None and self.t_it and self.it > self.r0[0] and self.t_it > self.r0[1]:
            return (self.it - self.r0[0]) / (self.t_it - self.r0[1])
        return None

    def _its_hasta_estable(self) -> float | None:
        """Iteraciones que faltan para tener `ventana` iteraciones seguidas con variación < tol (el criterio «stable»), extrapolando la
        tendencia de la variación en MEDIAS ventanas (h = ventana/2): vh = variación de las últimas h iteraciones y r = vh / vh de las h
        anteriores (sin tendencia medible, se supone la mitad por h). La variación en h baja de tol en h·ln(tol/vh)/ln(r) iteraciones y
        entonces aún hace falta llenar el resto de la ventana (ventana − h). None si aún no hay h iteraciones de historia."""
        w = self.window
        h = max(2, w // 2)
        vh = self._var(self.it, h)
        if vh is None:
            return None
        if vh <= self.tol:
            return max(0.0, w - h) * 0.5  # ya está quieto: lo que quede de ventana por llenar (aprox. la mitad de lo que falta)
        vhp = self._var(self.it - h, h)
        r = vh / vhp if vhp and vhp > vh else 0.5
        r = min(max(r, 0.05), 0.95)
        return h * math.log(self.tol / vh) / math.log(r) + (w - h)

    def _falta(self, now: float) -> dict:
        none = {"falta_s": None, "falta_max_s": None, "falta_its": None, "falta_fuente": None}
        if self.fkey != "iterar":
            self._eta_prev = None  # la suavización solo vale dentro de una racha de iterar
        if self.fkey == "iterar" and self.it is not None and self.iter_max:
            rate = self._rate()
            if not rate:
                return none
            n_done = self.it - (self.it0 if self.it0 is not None else self.it) + 1
            left_max = max(0, self.iter_max - n_done)
            if self.criterio == "n":
                rem = left_max
            else:
                # contra la VENTANA de estabilidad (no contra el tope): lo que falta para tener `ventana` iteraciones con variación < tol,
                # extrapolando la tendencia de la variación; el tope va aparte (falta_max_s). Misma fórmula antes y después de llenarse
                # la ventana: antes, (w − span) + lo que tarde en asentarse; así no salta al pasar la iteración `ventana`.
                w = self.window
                span = (self.hist[-1][0] - self.hist[0][0]) if self.hist else 0
                est = self._its_hasta_estable()
                rem = (w - span) + w * 0.5 if est is None else est  # sin media ventana de historia: llenar la ventana y asentarse
                if span < w:
                    rem = max(rem, w - span)  # nunca menos de lo que falta para llenar la ventana
            rem = min(max(rem, 0), left_max)
            falta = rem / rate
            prev = getattr(self, "_eta_prev", None)
            if prev is not None:  # sin saltos de más de ×2 entre muestras seguidas
                falta = min(max(falta, prev[0] / 2.0), max(prev[0] * 2.0, 10.0))
            self._eta_prev = (falta, now)
            return {"falta_s": round(falta, 1), "falta_max_s": round(left_max / rate, 1), "falta_its": round(falta * rate),
                    "falta_fuente": "ritmo"}
        if self.fkey in FASES and self.ref.get(self.fkey) is not None:  # fases anteriores a iterar: lo que tardó la última corrida
            idx = FASES.index(self.fkey)
            total = sum(float(self.ref.get(k, 0) or 0) for k in FASES[idx + 1:]) + max(0.0, float(self.ref[self.fkey]) - (now - self.t_fase))
            return {"falta_s": round(total, 1), "falta_max_s": None, "falta_its": None, "falta_fuente": "referencia"}
        return none

    def snapshot(self, terminado: bool = False, resultado=None) -> dict:
        now = time.time()
        v = self.vals
        var = self._var(self.it, self.window) if self.it is not None else None
        d = {"schema": 1, "tool": self.tool, "caso": self.caso,
             "fase": "terminado" if terminado else _FASE_NOMBRE.get(self.fkey, self.fkey),
             "fases_s": {k: round(x, 2) for k, x in self.hechas.items()},
             "iteracion": self.it, "iter_max": self.iter_max, "criterio": self.criterio,
             "H": v.get("H"), "M": v.get("M"), "eta": v.get("eta"), "continuidad": v.get("continuidad"),
             "variacion_pct": None if var is None else round(var * 100, 3), "ritmo_it_s": None,
             "inicio": _iso(self.t0), "transcurrido_s": round(now - self.t0, 1),
             "falta_s": None, "falta_max_s": None, "falta_its": None, "falta_fuente": None,
             "cancelando": self.cancel, "terminado": terminado, "resultado": resultado,
             "actualizado": _iso(now), "t": round(now, 2), "pid": os.getpid(), "t_create": _my_ctime(), "fase_falta_s": None}
        if not terminado and self.fkey in FASES and self.fkey != "iterar" and self.ref.get(self.fkey) is not None:  # lo que queda de la fase actual (cancel lo usa)
            d["fase_falta_s"] = round(max(0.0, float(self.ref[self.fkey]) - (now - self.t_fase)), 1)
        rate = self._rate()
        if rate:
            d["ritmo_it_s"] = round(rate, 3)
        if terminado:
            d["falta_s"] = 0.0
        else:
            d.update(self._falta(now))
        return _clean(d)

    def _write(self, terminado: bool = False, resultado=None) -> None:
        _write_json_atomic(progreso_file(), self.snapshot(terminado, resultado))

    def _loop(self, gen: int) -> None:
        while True:
            time.sleep(PROG_EVERY_S)
            with self.lk:
                if not self.active or self.gen != gen:
                    return
                try:
                    self.feed()
                    self._write()
                except Exception:  # noqa: BLE001
                    pass


_prog = _Progreso()


def read_progreso() -> dict:
    """progreso.json tal cual (lo lee el panel y cancel). {} si no hay."""
    return _read_json(progreso_file())


def _progreso_vivo(p: dict | None = None, max_age: float = 10.0) -> bool:
    """¿Hay una corrida escribiendo progreso ahora (no terminada, latido reciente, proceso dueño vivo)?"""
    p = read_progreso() if p is None else p
    return bool(p) and not p.get("terminado") and time.time() - float(p.get("t") or 0) < max_age and _pid_alive(int(p.get("pid") or 0))


def _cancel_requested() -> bool:
    """¿Alguien llamó a cancel() DESPUÉS de empezar la tool de primer nivel de este hilo? (cancel.json trae la hora)"""
    t0 = getattr(_ctx, "t_start", None)
    if t0 is None:
        return False
    d = _read_json(cancel_file())
    if d and float(d.get("t") or 0) >= t0 - 1.0:
        _ctx.cancel_seen = True
        _ctx.cancel_kill = bool(d.get("kill", False))  # solo con cancel(forzar=True)
        return True
    return False


def _clear_abort() -> None:
    try:
        abort_file().unlink(missing_ok=True)
    except OSError:
        pass


def _cancel_cleanup() -> None:
    """Fin de la tool que vio el cancel: se borra ABORT (si no, la próxima corrida pararía al instante) y cancel.json."""
    _clear_abort()
    try:
        cancel_file().unlink(missing_ok=True)
    except OSError:
        pass


def _cancel_stale_cleanup() -> bool:
    """Red de seguridad (la corre el vigilante): un cancel.json/ABORT de más de 15 s sin macro en marcha ni dueño del turno es un resto
    (cancel con dueño muerto cuyo wait_s venció); si se queda, pararía al instante la próxima macro de iterar."""
    d = _read_json(cancel_file())
    if (d or abort_file().exists()) and not macro_running() and lock_holder() is None:
        if time.time() - float((d or {}).get("t") or 0) > 15 and (not d or time.time() - cancel_file().stat().st_mtime > 15):
            _cancel_cleanup()
            _log("vigilante: borro un ABORT/cancel.json que quedó de un cancel")
            _cerrar_progreso_huerfano()
            return True
    return False


def _cerrar_progreso_huerfano() -> bool:
    """Quien ve el fin de una macro huérfana cancelada (cancel() si esperó, si no el vigilante) deja progreso.json como terminado:
    el dueño murió y nadie más lo cerrará. No toca uno ya terminado ni de un dueño vivo."""
    p = read_progreso()
    if not p or p.get("terminado") or macro_running() or (p.get("pid") and _owner_alive(p)):
        return False
    _write_json_atomic(progreso_file(), {**p, "terminado": True, "cancelando": False, "falta_s": 0.0, "fase": "terminado",
                                         "resultado": {"cancelled": True, "stopped": "cancelled", "huerfana": True}, "t": round(time.time(), 2)})
    return True


def _owner_alive(p: dict) -> bool:
    """¿Vive el proceso dueño de progreso.json (pid + hora de creación)?"""
    try:
        pid = int(p.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    return pid == os.getpid() or (pid > 0 and _pid_alive(pid) and _same_proc(pid, p.get("t_create")))


def _turn_owner_dead(p: dict) -> bool:
    """¿Murió el dueño de la corrida? Dueño de progreso.json muerto, o dueño del candado de turnos muerto (pid distinto o
    hora de creación distinta: PID reasignado)."""
    if p.get("pid") and not _owner_alive(p):
        return True
    exists, info, _ = _read_lock()
    if exists and info and info.get("pid"):
        pid = int(info["pid"])
        return pid != os.getpid() and (not _pid_alive(pid) or not _same_proc(pid, info.get("t_create")))
    return False


def _abort_enable_java() -> str:
    """Java que deja activo el criterio «Stop File» con la ruta absoluta de abort_file() (el nodo existe solo con un modelo de
    tiempo/estacionario). Mudo y a prueba de fallos: si no existe, vale el ABORT por defecto del directorio de trabajo."""
    p = _jstr(_jpath(abort_file()))
    return ("try { for (SolverStoppingCriterion sc : sim.getSolverStoppingCriterionManager().getObjects()) {\n"
            "  if (sc instanceof AbortFileStoppingCriterion) { AbortFileStoppingCriterion af = (AbortFileStoppingCriterion) sc;\n"
            f'    af.setAbortFilePath("{p}"); af.setIsUsed(true); }}\n'
            "} } catch (Throwable e) { }\n")


# Sin respaldo de stop() desde otro cliente: con el solver real (2410, 2026-10-26) un segundo cliente batch que llama a
# getSimulationIterator().stop() mientras el residente está en step() TUMBA el servidor (dos de dos pruebas: «Connection reset»).
# El bucle de la macro mira el ABORT/cancel.json entre bloques y para solo.


# ----------------------------------------------------------------------------- MCP
if "--vigilante" in sys.argv or "--cli" in sys.argv:  # el vigilante y --cli no sirven MCP: no pagan el import (~1,7 s)
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
            _ctx.t_start, _ctx.cancel_seen, _ctx.cancel_kill = time.time(), False, False
        _ctx.depth = depth + 1
        t0 = time.perf_counter()
        try:
            res = _scrub(fn(*a, **k))
        except Exception as e:
            if depth == 0:
                _record_time(fn.__name__, round((time.perf_counter() - t0) * 1000), _via_label(), False, len(_ctx.seen))
                _prog.end(None, f"{type(e).__name__}: {e}")
                if _ctx.cancel_seen:
                    _cancel_cleanup()
            raise
        finally:
            _ctx.depth = depth
        if depth:
            return res
        _prog.end(res)  # (no-op si esta tool no tomó la corrida)
        if _ctx.cancel_seen:
            _cancel_cleanup()
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
        res = _inject(_abort_enable_java() + _INIT_AUTOSAVE, timeout=600, tool="server_start")["stdout"][-200:]
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
            if _pid_alive(st["pid"]) and _pid_is_ours(st["pid"], st.get("t_create"), _started_ts(st)):
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
        if _port_open(PORT, timeout=0.1):  # antes de copiar el .sim (158 MB): no se copia para nada (un puerto libre en Windows tarda todo el timeout)
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
        st = {"pid": spid, "t_create": _proc_ctime(spid), "port": PORT, "host": "localhost", "sim": str(sim) if sim else None,
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
            if _port_open(PORT, timeout=0.1) and "Server::start" in _tail_log(st, 20000):  # aún cerrado es lo normal: sondeo corto (un cerrado bloquea todo el timeout en Windows) y reintento
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
        try:
            st["pid_real"] = _real_pid(spid, "-server")  # `pid` es el del lanzador (.bat o redirector del venv); este, el proceso real
        except Exception:  # noqa: BLE001
            st["pid_real"] = spid
        _write_state({**_state(), "pid_real": st["pid_real"], "vigilante": st["vigilante"]})
        _prune_work()
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
        tm = {}  # segundos por fase (guardar, apagar, residente): para ver dónde se va el tiempo
        ri0 = _res_info()
        res_vivo = bool(ri0.get("pid")) and _pid_alive(ri0["pid"])  # (el STOP anticipado de abajo puede hacerlo salir antes de _res_stop)
        t_s = time.time()
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
                tm["guardar"] = round(time.time() - t_s, 2)
            t_k = time.time()
            try:
                r = _inject("sim.kill();\n", timeout=1800, expect_death=True, tool="server_stop")
                result["stdout"] = r["stdout"]
            except Exception as e:  # noqa: BLE001
                result["kill_error"] = str(e)
            try:  # el residente recibe STOP ya, mientras el servidor termina de morir (antes se esperaba a uno y luego al otro)
                (RES_DIR / "inbox").mkdir(parents=True, exist_ok=True)
                (RES_DIR / "inbox" / "STOP").write_text("", encoding="utf-8")
            except OSError:
                pass
            end = time.time() + 60
            while time.time() < end and _pid_alive(st["pid"]):  # sondeo de 0,1 s: sin esperas fijas de POLL_S
                time.sleep(0.1)
            tm["apagar"] = round(time.time() - t_k, 2)
        t_r = time.time()
        if _res_stop(3.0) or res_vivo:  # el residente (archivo STOP) no sobrevive al servidor
            result["residente"] = "detenido"
        tm["residente"] = round(time.time() - t_r, 2)
        if _pid_alive(st["pid"]):
            if _pid_is_ours(st["pid"], st.get("t_create"), _started_ts(st)):
                _kill_tree(st["pid"])
                result["forced"] = True
            else:  # PID reasignado a otro proceso: no se toca, solo se limpia el estado
                result["pid_reused"] = True
        STATE_FILE.unlink(missing_ok=True)
        tm["total"] = round(time.time() - t_s, 2)
        result["timings_s"] = tm
        _log(f"servidor {st['pid']} apagado {result}")
        return result


@_tool
def server_status() -> dict:
    """Estado del servidor: pid, puerto, sim, vivo o no, minutos de inactividad, GUI conectada, turno (quién tiene
    el solver) e idle_warning si lo hay."""
    st = _state()
    a = alive(st)
    out = {"alive": a, "idle_min": round(idle_seconds() / 60, 1), "turno": lock_holder(), **st}
    if a and VIGILANTE_ON:  # vigilante_muerto: el panel puede relanzarlo con `--cli vigilante_relanzar`
        vp = _vigilante_pid(int(st.get("port") or PORT))
        out["vigilante_vivo"] = bool(vp)
        out["vigilante_muerto"] = not vp
        if vp:
            out["vigilante"] = vp
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


@_tool
def cancel(wait_s: int = 45, forzar: bool = False) -> dict:
    """Detiene LIMPIO la corrida en curso (iterate o cualquier fase de pump_case): crea el archivo ABORT que vigila el criterio
    «Stop File» de STAR-CCM+ (sims/ABORT) y cancel.json; la macro de bomba los mira antes de importar, entre fases y antes de cada
    bloque de iteraciones, y sale sin post (la tool que itera deja de pedir bloques, borra ABORT y devuelve stopped="cancelled"
    con la solución hasta la última iteración). En 2410 el Stop File NO corta un step() en curso: en iterar la parada llega al
    final del bloque (≤ 50 iteraciones); importar y mallar NO son interrumpibles: se para al terminar la fase (no se pierde nada, pero
    puede tardar minutos: si wait_s vence devuelve cancelled=False, pendiente=True y parara_en_s). Nunca manda stop() desde otro
    cliente (tumba el servidor) y NO MATA el residente por defecto, ni con la macro colgada: eso exige forzar=True, y aun así solo
    si la macro lleva > max(120 s, 3 bloques) SIN salida tras el cancel (nunca a mitad de step() mientras avanza; matar a mitad de
    step() tumba el servidor y se pierde el bloque). Sin forzar, una macro colgada sigue «parando» y se avisa (pendiente).
    Macro huérfana (el dueño murió con «Detener» y la macro sigue en el solver; se decide con macro_running() + dueño muerto, no
    por la fase de un progreso.json que ya nadie actualiza): cancela igual en cualquier fase (duenio_muerto), espera el fin de la
    macro y limpia ABORT/cancel.json él; si wait_s vence, lo limpia el vigilante cuando la macro acaba. NO toma el turno (es de
    quien itera). Sin corrida en curso no crea ABORT (pararía la próxima) y no toca nada: {"iteracion_en_curso": false}.
    Devuelve cancelled (¿paró?), pendiente, parara_en_s, fase, iteracion_en_curso, iteracion, metodo y turno."""
    t0 = time.time()
    st = _state()
    if not alive(st):
        return {"ok": True, "cancelled": False, "iteracion_en_curso": False, "reason": "no hay servidor vivo"}
    p = read_progreso()
    vivo = _progreso_vivo(p)
    owner = p.get("pid")
    fase = p.get("fase")
    # dueño muerto (el panel mató la CLI con «Detener») pero la macro sigue en el residente: nadie actualiza progreso.json (t viejo)
    # y nadie borrará ABORT/cancel.json; se cancela igual, en CUALQUIER fase, y esta llamada espera el fin y limpia ella.
    huerfana = (not vivo and bool(p) and not p.get("terminado") and macro_running() and _turn_owner_dead(p))
    holder = lock_holder()
    if not (vivo or huerfana) or fase in ("post", "terminado"):
        en_post = fase == "post" and (vivo or huerfana)
        return {"ok": True, "cancelled": False, "iteracion_en_curso": False, "pendiente": False, "fase": fase if (vivo or huerfana) else None, "turno": holder,
                "reason": "no hay corrida en curso" + (" (la macro está en el post-proceso: no se interrumpe y ya casi termina; usa server_stop)" if en_post else "")}
    it_at = p.get("iteracion")
    _write_json_atomic(cancel_file(), {"t": time.time(), "by": os.getpid(), "kill": bool(forzar) and not huerfana, "iteracion": it_at, "tool": p.get("tool"),
                                       "fase": fase, **({"huerfana": True} if huerfana else {})})
    try:
        abort_file().parent.mkdir(parents=True, exist_ok=True)
        abort_file().write_text("cancelado " + datetime.now().isoformat(timespec="seconds"), encoding="utf-8")
    except OSError as e:
        _cancel_cleanup()
        return {"ok": False, "cancelled": False, "iteracion_en_curso": True, "error": f"no pude crear {abort_file()}: {e}"}
    _log(f"cancel: ABORT creado ({abort_file()}); fase {fase}, iteración {it_at}, dueño pid {owner}{' MUERTO (macro huérfana)' if huerfana else ''}{' (forzar)' if forzar else ''}")
    if huerfana:  # que el panel vea que se está cancelando aunque nadie más escriba progreso.json
        _write_json_atomic(progreso_file(), {**p, "cancelando": True, "t": round(time.time(), 2), "actualizado": _iso(time.time())})
    # cuánto falta para que la macro vea el ABORT: iterar = el bloque en curso; antes de iterar = lo que quede de la fase actual
    if fase == "iterar":
        parara_en = round(50.0 / float(p["ritmo_it_s"])) if p.get("ritmo_it_s") else None
    elif p.get("fase_falta_s") is not None:
        parara_en = max(0, round(float(p["fase_falta_s"]) - (time.time() - float(p.get("t") or time.time()))))
    else:
        parara_en = None
    stopped, method = False, "ABORT"
    end = time.time() + max(0.3, float(wait_s))
    while time.time() < end:
        time.sleep(0.1)
        if huerfana:
            if not macro_running():
                stopped = True
                break
            continue
        pp = read_progreso()
        if lock_holder() is None or pp.get("terminado") or pp.get("pid") != owner:
            stopped = True
            break
    if huerfana and stopped:  # la macro paró: el cierre (borrar ABORT/cancel.json, marcar terminado) lo hace quien lo ve
        _cerrar_progreso_huerfano()
    if (huerfana and stopped) or (not huerfana and lock_holder() is None):  # nadie es dueño: ABORT y cancel.json se limpian aquí (si no, el dueño lo hace al terminar)
        _cancel_cleanup()
    out = {"ok": True, "cancelled": stopped, "pendiente": not stopped, "iteracion_en_curso": fase == "iterar" or fase is None, "fase": fase,
           "iteracion": it_at, "metodo": method, "turno": lock_holder(), "segundos": round(time.time() - t0, 1), "abort_pendiente": abort_file().exists(),
           **({"duenio_muerto": True} if huerfana else {}), **({"forzar": True} if forzar else {})}
    if not stopped:
        donde = (f"parará al final del bloque en curso (hasta 50 it, ~{parara_en} s)" if fase == "iterar" else
                 f"parará al terminar la fase «{fase}» (no se interrumpe), ~{parara_en} s" if parara_en is not None else
                 f"parará al terminar la fase «{fase}» (no se interrumpe)")
        out["parara_en_s"] = parara_en
        out["reason"] = (f"ABORT enviado; {donde}. "
                         + ("El dueño había muerto: vuelve a llamar a cancel o espera; el vigilante limpia ABORT/cancel.json y cierra el progreso al final. "
                            if huerfana else "Quien itera lo borra al acabar. ")
                         + ("" if forzar else "No se mata el residente (cancel(forzar=True) solo si la macro lleva > 120 s sin salida)."))
    return out


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
    body = _abort_enable_java() + f"""
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
    out = {"ok": r["ok"], "seconds": r["seconds"], "stdout": r["stdout"], **_err(r), **({"abandoned": True} if r.get("abandoned") else {})}
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
        _prog.begin("iterate", caso=Path(_state().get("sim") or "").stem or None, fase="iterar")
        _prog.configure(iter_max=n, window=window, tol=tol, criterio="stable" if until == "stable" else "n")
        _prog.set_fase("iterar")
        done, t0, blocks, last, prev_it = 0, time.time(), 0, {}, None
        stopped, var, names = "max", {}, list(reports or [])
        while done < n:
            if _cancel_requested():
                stopped = "cancelled"
                break
            k = min(block, n - done) if until == "stable" else n
            last = _iter_block(k, timeout_s)
            blocks += 1
            if not last["ok"] or "iteration" not in last:
                if _cancel_requested():  # la macro se abandonó por el cancel (no es un fallo)
                    return {"ok": True, "stopped": "cancelled", "cancelled": True, "iterations_run": done, "iteration": _prog.it,
                            "blocks": blocks, "seconds": round(time.time() - t0, 1), "reports": {},
                            **({"macro_abandonada": last.get("exception")} if last.get("abandoned") else {})}
                return {"ok": False, "stopped": "error", "iterations_run": done, "blocks": blocks,
                        "stdout": last["stdout"][-1500:], **_err(last)}
            it = last["iteration"]
            blk = k
            if _cancel_requested():  # el bloque pudo parar antes por el ABORT: las iteraciones hechas son las que dice el solver
                blk = (it - prev_it) if prev_it is not None else ((it - _prog.it0 + 1) if _prog.cols and _prog.it0 is not None else k)
                blk = max(0, min(k, blk))
            done += blk
            prev_it = it
            _prog.update(it, done, last["reports"])
            if _iter_hist and it <= _iter_hist[-1]["it"]:
                _iter_hist = []  # el .sim volvió atrás (otra simulación): historia nueva
            if until != "stable":
                if _cancel_requested():
                    stopped = "cancelled"
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
            if _cancel_requested():
                stopped = "cancelled"
                break
        out = {"ok": True, "stopped": stopped, **({"cancelled": True} if stopped == "cancelled" else {}), "iterations_run": done, "iteration": last.get("iteration"), "blocks": blocks,
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
    salida), la cola de stdout y `salida_completa` = casos/<caso>/salida_pump.txt con toda la salida.
    Cancelado (cancel()): `ok` False y `cancelled` True, `stopped` "cancelled", `resultados` = los RESULT de la solución PARCIAL (H, M, eta,
    H_Euler… hasta la última iteración; también en `parsed`) y sin post ni iterate; quien lea `ok` no cree que acabó. Si la macro quedó
    colgada y hubo que abandonarla viene `macro_abandonada` y no hay resultados."""
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
        body = mod.macro_body(_jpath(case_dir), _jpath(cad), ccm_d, mode, {"abort_file": _jpath(abort_file()), "cancel_file": _jpath(cancel_file()), **dict(params or {})})
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"pipeline de bomba: {type(e).__name__}: {e}"}
    imports = _pump_imports(mod)
    info = {"case": case, "mode": mode, "ccm_file": str(ccm_path), "ccm": ccm_d, "macro_chars": len(body)}
    if dry_run:
        return {"ok": True, "dry_run": True, **info, "macro_head": body[:600]}
    with _turn("pump_case") as busy:
        if busy:
            return _busy_only(busy)
        pr = dict(params or {})
        itp = pr.get("iterar")
        mx = (pr.get("max_iter") or (itp if isinstance(itp, int) and not isinstance(itp, bool) and itp > 1 else 1000)) if itp \
            else (n if until in ("stable", "n") else None)
        _prog.begin("pump_case", caso=case, fase="arrancar", iter_max=mx, window=pr.get("ventana", 100), tol=pr.get("tol", 0.005),
                    criterio="n" if until == "n" else "stable")
        try:
            server_start(name=case)  # reutiliza el vivo; se niega si hay otro .sim cargado
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)[:1500], **info}
        _prog.set_fase("importar")
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
        if r.get("cancelled") or "ITER_STOP\tcancelled" in r["stdout"]:  # la macro vio el ABORT y paró sola: sin post, servidor intacto
            out.update(ok=False, cancelled=True, stopped="cancelled", resultados=dict(out["parsed"].get("RESULT") or {}))
            if r.get("abandoned"):
                out["macro_abandonada"] = r.get("exception")
            if "ITER_STOP\tcancelled" in r["stdout"]:
                _cancel_cleanup()
        if until in ("stable", "n") and out["ok"] and not out.get("cancelled") and not _cancel_requested():
            it = iterate(n=n, until=until, reports=list(reports), block=block)  # mismo turno (re-entrante)
            out["iterate"] = {k: it.get(k) for k in ("ok", "stopped", "iterations_run", "iteration", "blocks", "seconds",
                                                    "reports", "variation_pct", "error", "exception", "busy") if k in it}
            out["ok"] = bool(it.get("ok"))
            if it.get("cancelled"):  # cancelado en el iterate encadenado: mismo contrato que arriba, con los reportes parciales
                out.update(ok=False, cancelled=True, stopped="cancelled", resultados=dict(it.get("reports") or {}))
                if it.get("macro_abandonada"):
                    out["macro_abandonada"] = it["macro_abandonada"]
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


CLI_TOOLS = ("gui_attach", "gui_status", "server_stop", "server_status", "cancel", "vigilante_relanzar")


def _cli(argv: list[str]) -> int:
    """`python mcp_server.py --cli <herramienta> [json-args]`: ejecuta UNA herramienta sin modelo y escribe su resultado JSON por stdout
    (UTF-8, una línea). Respeta el candado de turnos: espera como mucho COPILOTO_CLI_LOCK_WAIT_S (3) y devuelve {"busy": ...}.
    Código de salida: 0 si la herramienta respondió (aunque diga ok:false), 1 si lanzó, 2 si el uso es incorrecto."""
    global LOCK_WAIT
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    name = argv[0] if argv else ""
    rc, res = 0, None
    try:
        args = json.loads(argv[1]) if len(argv) > 1 and argv[1].strip() else {}
        if name not in CLI_TOOLS or not isinstance(args, dict):
            raise ValueError(f"uso: --cli <{'|'.join(CLI_TOOLS)}> [json-args]")
    except ValueError as e:
        res, rc = {"ok": False, "error": str(e)}, 2
    if res is None:
        LOCK_WAIT = float(os.environ.get("COPILOTO_CLI_LOCK_WAIT_S", "3"))
        try:
            res = globals()[name](**args)
        except TypeError as e:
            res, rc = {"ok": False, "error": f"argumentos de {name}: {e}"}, 2
        except Exception as e:  # noqa: BLE001
            res, rc = {"ok": False, "error": f"{type(e).__name__}: {_redact(str(e))[:500]}"}, 1
    print(json.dumps(_clean(_scrub(res)), ensure_ascii=False), flush=True)
    return rc


if __name__ == "__main__":
    if "--cli" in sys.argv:  # acción directa sin modelo (panel): ver _cli
        sys.exit(_cli(sys.argv[sys.argv.index("--cli") + 1:]))
    if "--selftest" in sys.argv:
        sys.modules.setdefault("mcp_server", sys.modules["__main__"])  # el selftest prueba ESTE módulo, no una segunda copia
        sys.path.insert(0, str(HERE / "tests"))
        from selftest_mcp import _selftest  # import perezoso: ni el MCP ni --cli/--vigilante pagan su coste

        sys.exit(_selftest())
    if "--vigilante" in sys.argv:  # proceso independiente del MCP: python mcp_server.py --vigilante <puerto>
        sys.exit(_vigilante_main(int(sys.argv[sys.argv.index("--vigilante") + 1])))
    _migrate_legacy(HERE, STATE_FILE, LOCK_FILE)  # .server.json/.inject.lock de cuando vivían en el repo (OneDrive)
    _migrate_agent_log()
    _prune_work()
    _write_panel_estado()
    start_watchdog()
    mcp.run()

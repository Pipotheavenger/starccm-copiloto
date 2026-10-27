"""Selftest de mcp_server.py (sin licencia ni solver: usa tests/fake_starccm.py y solo temporales).

    .venv\Scripts\python mcp_server.py --selftest        (delega aquí)
    .venv\Scripts\python tests\selftest_mcp.py

Las globales que el selftest reasigna (SIMS, MACROS, STATE_FILE, CANCEL_GRACE_S...) se reasignan sobre el módulo
importado (`m.X = ...`) y las funciones de mcp_server las leen de su propio módulo, así que ven el cambio."""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import mcp_server as m  # noqa: E402

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
            % (ccm["rpm"], mode, fail)) + (CANCELADO if params.get("cancelado") else EXTRA)
CANCELADO = r"""
sim.println("RESULT\\tH\\t15.9");
sim.println("RESULT\\tM\\t8.3");
sim.println("ITER_STOP\\tcancelled\\titeraciones 50\\titeracion 150\\tvariacion_max_pct 1.2\\tmeseta false\\t3.0 s");
"""
'''


def _selftest() -> int:
    import tempfile
    t_all = time.time()
    agent_log_real = m.AGENT_LOG
    m.PROG_EVERY_S, m.CANCEL_GRACE_S = 0.2, 1.5
    m.VIGILANTE_ON = False  # el vigilante real solo se prueba en su sección (es un proceso aparte con su propio entorno)
    fake = m.HERE / "tests" / "fake_starccm.py"
    os.environ["STARCCM_EXE"] = str(fake)
    m.EXE = str(fake)
    KEY = "FAKEKEY-selftest-9f3a7c"
    os.environ["STARCCM_PODKEY"] = KEY  # clave de mentira: se comprueba que NUNCA sale; la real no se toca
    with socket.socket() as sk:  # puerto libre: dos selftests a la vez no se pisan
        sk.bind(("127.0.0.1", 0))
        m.PORT = sk.getsockname()[1]
    tmp = Path(tempfile.mkdtemp(prefix="starccm-selftest-"))
    # Todo lo que escribe el falso va al temporal: ni sims/, ni macros/, ni .server.json, ni .inject.lock reales
    # (un selftest antiguo borró el .server.json de un solver vivo de otra sesión).
    real_lock, real_state, real_panel = m.LOCK_FILE, m.STATE_FILE, m.PANEL_ESTADO
    real_panel_before = real_panel.stat().st_mtime_ns if real_panel.is_file() else None
    real = (m.SIMS, m.MACROS, m.STATE_FILE, m.RES_DIR)
    real_work = m._work_dir()
    _real_files = lambda: {(str(p), p.stat().st_mtime_ns) for d in real for p in ([d] if d.is_file() else d.glob("*") if d.is_dir() else [])}
    real_before = _real_files()
    m.WORK = tmp / "work"
    m.SIMS, m.MACROS, m.RES_DIR, m.TIEMPOS = m.WORK / "sims", m.WORK / "macros", m.WORK / "residente", m.WORK / "tiempos.csv"
    m.STATE_FILE, m.AGENT_LOG, m.PANEL_ESTADO = m.estado_dir() / ".server.json", tmp / "agent.log", tmp / "panel_estado.json"
    m.RES_HB_STALE, m.RES_START_WAIT = 2.0, 8.0
    m.LOCK_FILE, m.CASES, m.RECIPES = m.estado_dir() / ".inject.lock", tmp / "casos", tmp / "recetas"
    m.RECIPES.mkdir()
    (m.RECIPES / "paso-ok.java").write_text('// QUE: paso de prueba\n// PARAMS: nombre\nsim.println("hola ${nombre} y ${Flujo}");\n', encoding="utf-8")
    (m.RECIPES / "paso-falla.java").write_text('sim.println("AGENT_FAKE_FAIL");\nsim.println("no llega");\n', encoding="utf-8")
    scen = tmp / "scen.txt"
    os.environ["FAKE_SCEN_FILE"] = str(scen)
    ctl = tmp / "ctl.json"  # control en caliente del falso (iteración lenta, ignorar ABORT)
    os.environ["FAKE_CTL_FILE"] = str(ctl)
    m.LOCK_WAIT, m.POLL_S, m.GUI_TTL = 0.3, 0.05, 1e9
    real_list = m._list_procs
    seen_rows: list = []
    m._list_procs = lambda: []  # sin PowerShell salvo en la sección de la GUI
    fails = []
    gui_pid = None
    sleepers = []

    def check(cond, msg):
        print(f"  {time.time() - t_all:5.1f}s " + ("ok   " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    def wlock(pid, tool="otro", age_s=0):
        m.LOCK_FILE.write_text(json.dumps({"pid": pid, "tool": tool, "start": "x", "t": time.time() - age_s}), encoding="utf-8")
        if age_s:
            os.utime(m.LOCK_FILE, (time.time() - age_s, time.time() - age_s))

    # --- pruebas lentas e independientes del resto: en hilos, para no sumar al tiempo del selftest
    bg: dict = {}
    jc_real = m._javac(real=True)  # (se resuelve ya: más abajo se sustituyen _javac y _run_javac)

    def bg_detached() -> None:
        """Procesos fuera del árbol de la CLI: un padre falso lanza un destino con _spawn_detached y otro hijo normal; un
        `taskkill /T /F` REAL sobre el padre mata al hijo normal y NO al desacoplado."""
        sleeper = "import time;time.sleep(120)"
        code = (f"import sys,time,subprocess; sys.path.insert(0, {str(m.HERE)!r}); import mcp_server as m; "
                f"d=m._spawn_detached([sys.executable,'-I','-c',{sleeper!r}]); "
                f"c=subprocess.Popen([sys.executable,'-I','-c',{sleeper!r}]); "
                "print(d['pid'], c.pid, d['breakaway'], flush=True); time.sleep(120)")
        par = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        try:
            line = par.stdout.readline().split()
            dpid, cpid = int(line[0]), int(line[1])
            time.sleep(0.4)
            before = (m._pid_alive(dpid), m._pid_alive(cpid), m._pid_alive(par.pid))
            subprocess.run(["taskkill", "/PID", str(par.pid), "/T", "/F"], capture_output=True, timeout=30)
            time.sleep(0.6)
            bg["detached"] = (before, m._pid_alive(par.pid), m._pid_alive(cpid), m._pid_alive(dpid), line[2])
            m._kill_tree(dpid)
            # un .bat desacoplado que lanza un hijo de consola (como starccm+.bat): su salida redirigida tiene que llegar al log
            bat, blog = tmp / "hijo.bat", tmp / "hijo.log"
            crlf = chr(13) + chr(10)
            bat.write_bytes(("@echo off" + crlf + "echo bat" + crlf + '"' + sys.executable + '" -I -c "print(1234)"' + crlf + "echo fin" + crlf).encode())
            m._spawn_detached([str(bat)], log=blog, cwd=tmp)
            t_end = time.time() + 15
            while time.time() < t_end and "fin" not in (blog.read_text(errors="replace") if blog.is_file() else ""):
                time.sleep(0.1)
            bg["bat"] = blog.read_text(errors="replace") if blog.is_file() else ""
        except Exception as e:  # noqa: BLE001
            bg["detached"] = e
            m._kill_tree(par.pid)

    def bg_regresion() -> None:
        """La macro REAL de pipelines/bomba.py con el MISMO camino de imports que pump_case, compilada con el javac de STAR-CCM+."""
        cad = Path.home() / "OneDrive" / "Documentos" / "cfturbo-agent" / "versions" / "BombaRapida_v1"
        ccms = sorted(cad.glob("*.cfturbo-ccm")) if cad.is_dir() else []
        if not ccms or not jc_real:
            bg["regresion"] = "SKIP: " + ("no hay .cfturbo-ccm de BombaRapida_v1" if not ccms else "no hay javac de STAR-CCM+")
            return
        res = []
        try:
            mod = m._load_pipeline(m.HERE / "pipelines" / "bomba.py")
            ccm_d = mod.parse_ccm(str(ccms[0]))
            d = tmp / "regresion"
            d.mkdir()
            for nombre, params in (("rapida", {}), ("iterar+post", {"iterar": True, "post": True})):
                body = mod.macro_body(m._jpath(d), m._jpath(cad), ccm_d, "rapida", dict(params))
                src = d / f"Agent_{m._safe(nombre)}.java"
                text = m._wrap(body, m._pump_imports(mod), m._safe(nombre))
                src.write_text(text, encoding="utf-8")
                bad = [ln for ln in text.splitlines() if ln.startswith("import ") and re.search(r"['\[\]]", ln)]
                (d / "out").mkdir(exist_ok=True)
                r = subprocess.run([jc_real[0], "-nowarn", "-proc:none", "-cp", jc_real[1], "-d", str(d / "out"), str(src)],
                                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
                res.append((nombre, bad, r.returncode, (r.stderr or r.stdout)[-400:]))
            bg["regresion"] = res
        except Exception as e:  # noqa: BLE001
            bg["regresion"] = e

    def bg_java() -> None:
        """El Java nuevo (Stop File activo, init) compilado con el javac real de STAR-CCM+."""
        if not jc_real:
            bg["java"] = "SKIP: no hay javac de STAR-CCM+"
            return
        res = []
        try:
            d = tmp / "javanuevo"
            d.mkdir()
            (d / "out").mkdir()
            for nombre, body in (("init", m._abort_enable_java() + m._INIT_AUTOSAVE),
                                 ("iter", m._abort_enable_java() + "sim.getSimulationIterator().step(5);")):
                src = d / f"Agent_{nombre}.java"
                src.write_text(m._wrap(body, "", nombre), encoding="utf-8")
                r = subprocess.run([jc_real[0], "-nowarn", "-proc:none", "-cp", jc_real[1], "-d", str(d / "out"), str(src)],
                                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
                res.append((nombre, r.returncode, (r.stderr or r.stdout)[-300:]))
            bg["java"] = res
        except Exception as e:  # noqa: BLE001
            bg["java"] = e

    def bg_docs() -> None:
        """Calienta el índice del javadoc (~3,5 s la primera vez); las comprobaciones de docs_search lo esperan."""
        try:
            m._docs()
            m._members()
        except Exception:  # noqa: BLE001
            pass

    docs_thread = threading.Thread(target=bg_docs, daemon=True)
    docs_thread.start()
    threads = [threading.Thread(target=bg_regresion, daemon=True), threading.Thread(target=bg_java, daemon=True)]
    if os.name == "nt":
        threads.append(threading.Thread(target=bg_detached, daemon=True))
    for t in threads:
        t.start()

    print(f"selftest: starccm+ falso = {fake} · escribe solo en {tmp}")
    try:
        r = subprocess.run(m._starccm_cmd() + ["-version"], capture_output=True, text=True)
        check("FAKE" in r.stdout, "-version del falso")
        os.environ["FAKE_RESIDENT_DELAY"] = "0.6"  # el residente tarda 0,6 s en dar su primer latido: server_start no debe esperarlo
        t_ss = time.time()
        st = m.server_start()
        t_ss = time.time() - t_ss
        os.environ.pop("FAKE_RESIDENT_DELAY")
        check(st.get("pid") and m._port_open(m.PORT), f"server_start -> pid {st.get('pid')} puerto {m.PORT} en {st.get('startup_seconds')}s")
        ri0 = m._res_info()
        check(ri0.get("pid") and m._pid_alive(ri0["pid"]) and not (m.RES_DIR / "heartbeat").exists() and m._res_waits == 0
              and m._state().get("init") == "pendiente" and st.get("init") == "pendiente",
              f"server_start lanza el residente y vuelve SIN esperar su latido ({t_ss:.1f} s; init pendiente)")
        check(Path(m._state()["log"]).parent == m.SIMS and ri0.get("breakaway") in (True, False, None) and "vigilante" in st,
              "el servidor y el residente salen del lanzador desacoplado (doble lanzamiento)")
        check(KEY not in json.dumps(st) and KEY not in m.STATE_FILE.read_text(encoding="utf-8"), "la clave PoD no sale de server_start ni de .server.json")
        pr = st.get("pid_real")
        check(pr and m._pid_alive(pr) and (os.name != "nt" or (m._real_pid(st["pid"], "-server") == pr and any(x["pid"] == pr for x in m._procs_native("-server"))))
              and m._state().get("pid_real") == pr, f".server.json guarda el pid REAL del servidor ({pr}; lanzador {st['pid']})")
        st2 = m.server_start()
        check(st2.get("reused"), "server_start reutiliza el servidor vivo")
        r = m.run_java('sim.println("OK");')
        check(r["ok"] and r["stdout"].strip() == "OK", f"run_java OK ({r['seconds']}s)")
        check(r["via"] == "residente" and m._res_waits == 1 and (m.RES_DIR / "heartbeat").is_file()
              and "INIT autosave off" in (m._state().get("init") or ""),
              f"la primera inyección espera el latido del residente y corre antes la macro de init ({m._state().get('init')!r})")
        # PID reasignado: la hora de creación guardada al lanzar manda sobre el nombre del exe (un python ajeno se mataba con /T /F)
        me_ct = m._proc_ctime(os.getpid())
        t_pid = time.perf_counter()
        for _ in range(200):
            m._pid_is_ours(os.getpid(), me_ct)
        ms_pid = (time.perf_counter() - t_pid) * 5  # ms por llamada
        check(os.name != "nt" or (me_ct and 0 <= time.time() - me_ct < 7 * 86400 and m._pid_is_ours(os.getpid(), me_ct)
                                  and not m._pid_is_ours(os.getpid(), me_ct - 5000) and m._pid_is_ours(os.getpid()) and ms_pid < 5),
              f"_pid_is_ours compara la hora de creación guardada: el mismo proceso sí, un PID 'reasignado' no ({ms_pid:.3f} ms por sondeo)")
        check(os.name != "nt" or (isinstance(st.get("t_create"), float) and m._pid_is_ours(st["pid"], st["t_create"]) and m._state().get("t_create") == st["t_create"]),
              f".server.json guarda t_create (hora de creación del lanzador) y _pid_is_ours lo acepta ({st.get('t_create')})")
        check(not m.LOCK_FILE.exists(), "el turno se libera al terminar la tool")
        # F: hora de creación también en el residente, en el dueño del turno y en el fallback por nombre
        if os.name == "nt":
            sl_f = subprocess.Popen([sys.executable, "-I", "-c", "import time;time.sleep(60)"])
            sleepers.append(sl_f)
            ct_f = m._proc_ctime(sl_f.pid)
            ri_f = m._res_info()
            check(isinstance(ri_f.get("t_create"), float) and m._pid_is_ours(ri_f["pid"], ri_f["t_create"]),
                  f"residente.json guarda t_create (hora de creación del lanzador) ({ri_f.get('t_create')})")
            res_txt = (m.RES_DIR / "residente.json").read_text(encoding="utf-8")
            (m.RES_DIR / "residente.json").write_text(json.dumps({**ri_f, "pid": sl_f.pid, "t_create": ct_f - 5000}), encoding="utf-8")
            m._res_kill("selftest: PID reasignado")  # el PID guardado es ya de OTRO proceso (hora distinta): NO se mata
            vivo_f = m._pid_alive(sl_f.pid)
            (m.RES_DIR / "residente.json").write_text(res_txt, encoding="utf-8")
            check(vivo_f, "_res_kill no mata un PID reasignado (otra hora de creación)")
            wlock(sl_f.pid)
            holder_ok = m.lock_holder()
            m.LOCK_FILE.write_text(json.dumps({"pid": sl_f.pid, "t_create": ct_f - 5000, "tool": "otro", "start": "x", "t": time.time()}), encoding="utf-8")
            holder_re = m.lock_holder()
            m.LOCK_FILE.write_text(json.dumps({"pid": sl_f.pid, "t_create": ct_f, "tool": "otro", "start": "x", "t": time.time()}), encoding="utf-8")
            holder_ok2 = m.lock_holder()
            m.LOCK_FILE.unlink(missing_ok=True)
            check(holder_ok and holder_re is None and holder_ok2 and holder_ok2.get("pid") == sl_f.pid,
                  "dueño del turno: un PID reasignado (otra hora de creación) hace el candado huérfano; el mismo proceso sigue siendo dueño")
            me_t = m._proc_ctime(os.getpid())
            p_ok, p_re = {"pid": sl_f.pid, "t_create": ct_f}, {"pid": sl_f.pid, "t_create": ct_f - 5000}
            check(m._owner_alive(p_ok) and not m._owner_alive(p_re) and not m._turn_owner_dead(p_ok) and m._turn_owner_dead(p_re)
                  and isinstance(m._prog.snapshot().get("t_create"), float) and abs(m._prog.snapshot()["t_create"] - m._my_ctime()) < 1,
                  "dueño de la corrida: progreso.json guarda t_create y un PID reasignado (otra hora) cuenta como dueño muerto para cancel")
            check((m._proc_exe(os.getpid()) or "").lower().startswith("python") and m._proc_exe(99999999) is None,
                  f"_proc_exe: nombre del exe sin lanzar tasklist ({m._proc_exe(os.getpid())})")
            check(m._pid_is_ours(os.getpid(), None, time.time() + 10) and not m._pid_is_ours(os.getpid(), None, me_t - 100),
                  "fallback de _pid_is_ours (sin t_create): un proceso MÁS NUEVO que el registro no es el guardado")
        r = m.run_java('sim.println("AGENT_FAKE_FAIL");\nsim.println("no llega");')
        check(not r["ok"] and r["exception"] and "fallo simulado" in r["exception"], "run_java captura excepción")
        r = m.inspect("regions")
        check(r["ok"] and r["path"] == "regions", "inspect(regions) responde")
        r = m.inspect("nodo_que_no_existe")
        check("error" in r, "inspect rechaza nodo desconocido")
        p = m.recipe_save("probar-selftest", 'sim.println("hola");', "receta de prueba del selftest")
        p = p["resultado"]
        check(Path(p).is_file() and Path(p).parent == m.RECIPES, "recipe_save (en la carpeta de recetas)")
        hits = m.recipe_search("selftest hola")["resultado"]
        check(any(h["name"] == "probar-selftest.java" for h in hits), "recipe_search encuentra la receta")
        check(m.RECIPES.parent == tmp and (m.HERE / "conocimiento" / "recetas").is_dir(), "las recetas reales viven en conocimiento/recetas")
        docs_thread.join(timeout=30)
        d = m.docs_search("Simulation saveState")
        check(("classes" in d and d["classes"]) or "error" in d, "docs_search responde (con o sin javadoc)")
        if m.DOC_DIR and m.DOC_DIR.is_dir():
            check(any(c["class"].endswith("Simulation") for c in d.get("classes", [])), "docs_search encuentra star.common.Simulation")
        r = m.save(str(tmp / "prueba.sim"))
        check(r["ok"], "save")
        if m.DOC_DIR and m.DOC_DIR.is_dir() and (m.DOC_DIR / "member-search-index.js").is_file():
            d2 = m.docs_search("setRelativeToModelLength")
            check(any(c.endswith("GlyphSettings") for c in d2.get("content_hits", [])), "docs_search encuentra un método por el índice de miembros")
        check(m._jstr('a"b' + chr(92) + 'c' + chr(10) + 'd') == 'a' + chr(92) + '"b' + chr(92) * 2 + 'c' + chr(92) + 'nd',
              "_jstr escapa comillas, barras y saltos")
        r = m.export_scene('x"); System.exit(0); //')
        macs = sorted(m.MACROS.glob("Agent_*.java"), key=lambda q: q.stat().st_mtime)
        check('equals("x\\"); System.exit(0); //")' in macs[-1].read_text(encoding="utf-8"), "export_scene escapa el nombre en el Java")
        check(m.idle_seconds() < 60, "last_activity en disco ve la actividad reciente")

        # --- redacción de la clave
        r = m.run_java(f'sim.println("clave={KEY}");')
        outs = "".join(q.read_text(encoding="utf-8", errors="replace") for q in m.MACROS.glob("Agent_*.out"))
        check(KEY not in json.dumps(r) and "***PODKEY***" in r["stdout"], "run_java: la clave impresa por una macro sale redactada")
        check(KEY not in outs and KEY not in m.AGENT_LOG.read_text(encoding="utf-8"), "la clave no queda en macros/*.out ni en agent.log")
        check(m._redact(f"a {KEY} b") == "a ***PODKEY*** b" and KEY not in json.dumps(m._scrub({"x": [f"k={KEY}", {"y": KEY}]})),
              "_redact y _scrub")

        # --- carpetas de trabajo fuera de OneDrive
        check(m._work_dir({"COPILOTO_WORK": str(tmp / "w")}) == tmp / "w"
              and m._work_dir({"LOCALAPPDATA": "C:/X"}) == Path("C:/X") / "starccm-copiloto",
              "_work_dir: COPILOTO_WORK manda; si no, %LOCALAPPDATA%/starccm-copiloto")
        check(bool(os.environ.get("COPILOTO_WORK")) or (m.HERE not in real_work.parents and "onedrive" not in str(real_work).lower()),
              f"carpeta de trabajo real fuera del repo y de OneDrive ({real_work})")
        check(all(tmp in q.parents for q in (m.SIMS, m.MACROS, m.RES_DIR, m.TIEMPOS)) and Path(m._state()["log"]).parent == m.SIMS
              and m.CASES.parent == tmp and (m.HERE / "casos").is_dir(),
              "sims, macros, residente, tiempos.csv y log del solver cuelgan de la carpeta de trabajo; casos/ se queda en el repo")
        check(m.estado_dir() == m.WORK and m.STATE_FILE.parent == m.WORK and m.LOCK_FILE.parent == m.WORK and m.STATE_FILE.is_file(),
              "estado_dir(): .server.json e .inject.lock cuelgan de la carpeta de trabajo")
        check(real_state.parent == real_work and real_lock.parent == real_work
              and (bool(os.environ.get("COPILOTO_WORK")) or "onedrive" not in str(real_state).lower()),
              f"los .server.json/.inject.lock REALES quedan fuera del repo y de OneDrive ({real_state.parent})")
        pe = json.loads(m.PANEL_ESTADO.read_text(encoding="utf-8")) if m.PANEL_ESTADO.is_file() else {}
        check(pe.get("estado_dir") == str(m.WORK) and pe.get("state_file") == str(m.STATE_FILE) and pe.get("lock_file") == str(m.LOCK_FILE),
              "panel_estado.json dice dónde están .server.json e .inject.lock (lo escribe server_start)")
        old_d, new_d = tmp / "viejo", tmp / "nuevo"
        old_d.mkdir()
        (old_d / ".server.json").write_text('{"pid": 1}', encoding="utf-8")
        (old_d / ".inject.lock").write_text("{}", encoding="utf-8")
        moved = m._migrate_legacy(old_d, new_d / ".server.json", new_d / ".inject.lock")
        (old_d / ".server.json").write_text('{"pid": 2}', encoding="utf-8")
        moved2 = m._migrate_legacy(old_d, new_d / ".server.json", new_d / ".inject.lock")
        check(sorted(moved) == [".inject.lock", ".server.json"] and (new_d / ".server.json").read_text(encoding="utf-8") == '{"pid": 1}'
              and not moved2, "migración: el estado viejo del repo se mueve a la carpeta de estado y nunca pisa uno existente")

        # --- cliente residente (vía por defecto)
        ri = m._res_info()
        rpid = ri.get("pid")
        n_launch = m._res_launches
        check(rpid and m._pid_alive(rpid) and (m.RES_DIR / "heartbeat").is_file() and (m.RES_DIR / "Residente.java").is_file()
              and ri.get("server_pid") == m._state().get("pid"), f"server_start dejó un residente vivo con latido (pid {rpid})")
        pc_calls = []
        real_pc = m._precompile
        m._precompile = lambda *a, **k: (pc_calls.append(a), None)[1]
        r = m.run_java('sim.println("res1");')
        r2 = m.run_java('sim.println("res2");')
        m._precompile = real_pc
        check(r["ok"] and r["stdout"] == "res1" and r2["stdout"] == "res2" and r["via"] == "residente" and r2["via"] == "residente"
              and isinstance(r["ms"], int) and m._res_info()["pid"] == rpid and m._res_launches == n_launch,
              f"run_java por el residente: via, ms y el MISMO residente ({r['ms']} ms, {r2['ms']} ms)")
        check(not pc_calls, "la vía residente no precompila con javac")
        check("AGENT_END" in Path(r["macro"]).with_suffix(".out").read_text(encoding="utf-8")
              and "AGENT_BEGIN" in Path(m._res_info()["log"]).read_text(encoding="utf-8") and not m.macro_running(),
              "la salida va al log del residente y al .out de la macro; macro_running no la da por viva")
        r = m.run_java('sim.println("AGENT_FAKE_COMPILE_ERROR");')
        check(r["ok"] is False and "compilar" in (r["exception"] or "") and "cannot find symbol" in (r["trace"] or "")
              and r["via"] == "residente" and m._res_info()["pid"] == rpid, "error de compilación: lo devuelve el residente y sigue vivo")
        r = m.run_java('sim.println("tras el error");')
        check(r["ok"] and r["stdout"] == "tras el error" and m._res_info()["pid"] == rpid, "tras el error de compilación el residente sigue sirviendo")
        t0 = time.time()
        r = m._inject('sim.println("AGENT_FAKE_HANG");', timeout=0.4, tool="selftest")
        npid = m._res_info().get("pid")
        check(r["ok"] is False and r.get("client_killed") and "timeout" in (r["exception"] or "") and not m._pid_alive(rpid)
              and npid and npid != rpid and m._pid_alive(npid) and m.alive() and time.time() - t0 < 12,
              f"macro colgada: se mata el residente (no el servidor), se relanza y responde error ({time.time() - t0:.1f}s)")
        rpid = npid
        r = m.run_java('sim.println("tras el cuelgue");')
        check(r["ok"] and r["via"] == "residente" and m._res_info()["pid"] == rpid, "tras el cuelgue el residente nuevo sirve")
        r = m.run_java('sim.println("AGENT_FAKE_DIE");')
        npid = m._res_info().get("pid")
        check(r["ok"] is False and r.get("resident_died") and not m._pid_alive(rpid) and npid and npid != rpid and m.alive(),
              "residente muerto a mitad de macro: error claro (sin repetir la macro por batch) y relanzado")
        rpid = npid
        m._kill_tree(rpid)
        r = m.run_java('sim.println("revive");')
        check(r["ok"] and r["via"] == "residente" and m._res_info()["pid"] != rpid, "residente muerto entre macros: se relanza solo (vía residente)")
        rpid = m._res_info()["pid"]
        os.environ["FAKE_RESIDENT_MODE"] = "broken"
        m._kill_tree(rpid)
        n_launch = m._res_launches
        r = m.run_java('sim.println("por batch");')
        check(r["ok"] and r["stdout"] == "por batch" and r["via"] == "batch" and "terminó" in (r.get("via_fallback") or "")
              and m._res_launches == n_launch + 2 and not m._res_info(),
              f"residente que no arranca (relanzado una vez): cae al batch y lo dice ({str(r.get('via_fallback'))[:60]})")
        r = m.run_java('sim.println("batch 2");')
        check(r["ok"] and r["via"] == "batch" and m._res_launches == n_launch + 2 and "pausa" in (r.get("via_fallback") or ""),
              "tras fallar dos veces el batch sigue sin reintentar el residente cada macro (pausa)")
        os.environ.pop("FAKE_RESIDENT_MODE")
        m._res_off_until = 0.0
        r = m.run_java('sim.println("de vuelta");')
        check(r["ok"] and r["via"] == "residente" and m._pid_alive(m._res_info()["pid"]), "al volver a estar sano el residente vuelve a ser la vía")
        # guardas de AutoExport
        b, w = m._guard_autoexport("getAutoExport();\nae.getStarUpdate().getIterationUpdateFrequency().setIterations(25);")
        b2, w2 = m._guard_autoexport("getAutoExport();\nae.getStarUpdate().getIterationUpdateFrequency().setIterations(250);")
        check("setIterations(100)" in b and w and "setIterations(250)" in b2 and not w2
              and m._guard_autoexport("x.setIterations(5);") == ("x.setIterations(5);", []), "AutoExport: frecuencia < 100 sube a 100")
        r = m.run_java('sim.getSimulationIterator().getAutoExport(); ae.getIterationUpdateFrequency().setIterations(10);')
        check("setIterations(100)" in Path(r["macro"]).read_text(encoding="utf-8") and r.get("warnings"), "run_java aplica la guarda de AutoExport")
        check("setEnabled(false)" in m._INIT_AUTOSAVE and "Math.max(100, CADA)" in (m.HERE / "conocimiento" / "recetas" / "activar-autoexport.java").read_text(encoding="utf-8"),
              "AutoExport apagado al arrancar y la receta nunca baja de 100")
        # caché de javac por hash del cuerpo (solo vía batch)
        calls = []
        real_javac, real_run = m._javac, m._run_javac
        m._javac = lambda: ("javac", "cp")
        m._run_javac = lambda cmd: (calls.append(cmd), subprocess.CompletedProcess(cmd, 0, "", ""))[1]
        fj = m.MACROS / "Agent_x.java"
        fj.write_text("x", encoding="utf-8")
        e = [m._precompile(fj, "hashA"), m._precompile(fj, "hashA"), m._precompile(fj, "hashB")]
        m._run_javac = lambda cmd: (calls.append(cmd), subprocess.CompletedProcess(cmd, 1, "", "boom"))[1]
        e += [m._precompile(fj, "hashC"), m._precompile(fj, "hashC")]
        m._javac, m._run_javac = real_javac, real_run
        check(e[:3] == [None] * 3 and e[3:] == ["boom", "boom"] and len(calls) == 4, "precompilación batch: caché por hash (los errores no se cachean)")
        fj.unlink()
        # tiempos.csv
        rows = [ln.split(",") for ln in m.TIEMPOS.read_text(encoding="utf-8").splitlines()]
        check(rows[0] == ["fecha", "herramienta", "ms", "via", "ok", "inyecciones"]
              and any(x[1] == "run_java" and x[3] == "residente" and x[2].isdigit() for x in rows[1:])
              and any(x[1] == "run_java" and x[3] == "batch" for x in rows[1:]), f"tiempos.csv con herramienta, ms y via ({len(rows) - 1} filas)")
        ri = m.recipe_search("selftest")
        check(ri["via"] == "ninguno" and isinstance(ri["ms"], int), "toda respuesta trae ms y via (también las que no inyectan)")

        # --- turnos entre procesos (.inject.lock)
        child = subprocess.Popen([sys.executable, "-I", "-c", _HOLD, str(m.LOCK_FILE), "1.4"], stdout=subprocess.PIPE, text=True)
        check(child.stdout.readline().strip() == "HELD", "otro proceso toma el turno")
        t0 = time.time()
        r = m.run_java('sim.println("x");', wait_s=0.5)
        check(r["ok"] is False and "panel_demo" in (r.get("busy") or "") and 0.4 < time.time() - t0 < 4,
              f"run_java con el turno ocupado espera y devuelve busy ({r.get('busy')})")
        sstat = m.server_status()
        check((sstat.get("turno") or {}).get("tool") == "panel_demo", "server_status dice quién tiene el turno")
        r = m.server_stop()
        check(r["stopped"] is False and r.get("busy") and m.alive(), "server_stop con el turno ocupado no apaga")
        r = m.run_java('sim.println("tras la espera");', wait_s=10)
        check(r["ok"] and r["stdout"] == "tras la espera", "run_java espera el turno y corre al liberarse")
        child.wait(timeout=10)
        check(not m.LOCK_FILE.exists(), "turno liberado tras la espera")
        wlock(999999, "muerto")
        r = m.run_java('sim.println("1");')
        check(r["ok"] and not m.LOCK_FILE.exists(), "candado huérfano (pid muerto) se limpia")
        sl = subprocess.Popen([sys.executable, "-I", "-c", "import time;time.sleep(60)"])
        sleepers.append(sl)
        wlock(sl.pid, "viejo", age_s=3 * 3600)
        r = m.run_java('sim.println("2");')
        check(r["ok"], "candado sin latido en 2 h se limpia aunque el pid viva")
        wlock(os.getpid(), "mio-roto")
        r = m.run_java('sim.println("3");')
        check(r["ok"], "candado propio sin uso (resto de un fallo) se limpia")
        wlock(sl.pid, "vivo")
        r = m.run_java('sim.println("4");', wait_s=0)
        check(r["ok"] is False and "vivo" in (r.get("busy") or ""), "candado vivo y reciente da busy")
        m.LOCK_FILE.unlink()
        m._kill_tree(sl.pid)

        # --- run_steps
        r = m.run_steps([{"recipe": "paso-ok", "params": {"nombre": "mundo"}}, {"recipe": "paso-falla"},
                       {"java": 'sim.println("tercero");', "name": "mi-java"}], stop_on_error=False)
        s = r.get("steps", [])
        check(len(s) == 3 and s[0]["ok"] is True and s[0]["stdout"] == "hola mundo y ${Flujo}", f"run_steps: paso 0 ok con params y ${{Flujo}} intacto ({s[0] if s else r})")
        check(s[1]["ok"] is False and "fallo simulado" in s[1].get("error", ""), "run_steps: el paso que falla trae su error")
        check(s[2]["ok"] is True and s[2]["stdout"] == "tercero" and s[2]["name"] == "mi-java", "run_steps: con stop_on_error=False el siguiente corre")
        check(r["ok"] is False and r["failed_at"] == 1, "run_steps: ok False y failed_at=1")
        check("// ---- paso 1: paso-falla.java" in Path(r["macro"]).read_text(encoding="utf-8"), "run_steps: UNA macro con los tres pasos")
        r = m.run_steps([{"recipe": "paso-ok", "params": {"nombre": "x"}}, {"recipe": "paso-falla"}, {"java": 'sim.println("nunca");'}])
        s = r["steps"]
        check(s[1]["ok"] is False and s[2]["skipped"] is True and s[2]["ok"] is None, "run_steps: stop_on_error salta el resto")
        r = m.run_steps([{"recipe": "paso-ok"}])
        check(r["ok"] is False and "faltan parámetros" in r["error"], "run_steps: exige los PARAMS de la receta")
        r = m.run_steps([{"recipe": "no-existe"}])
        check(r["ok"] is False and "no existe la receta" in r["error"], "run_steps: receta inexistente")
        r = m.run_steps([{"recipe": "paso-ok", "java": "x"}])
        check(r["ok"] is False and "exactamente uno" in r["error"], "run_steps: paso mal formado")
        r = m.run_steps([{"recipe": "paso-ok", "params": {"nombre": "a", "sobra": 1}}])
        check(r["ok"] and any("sin uso" in w for w in r.get("warnings", [])), "run_steps avisa de parámetros sin uso")

        # --- iterate con criterio de parada
        scen.write_text("stable")
        r = m.iterate(n=1000, block=100)
        check(r["ok"] and r["stopped"] == "stable" and 200 <= r["iteration"] <= 600 and r["reports"].get("H"),
              f"iterate until=stable para al asentarse: {r.get('stopped')} en {r.get('iteration')} it, var {r.get('variation_pct')}")
        check(r["iterations_run"] < 1000 and r["history"] and "residuals" in r and "res" in r["history"][-1], "iterate: historia corta y residuales")
        scen.write_text("plateau")
        r = m.iterate(n=1000, block=100)
        check(r["ok"] and r["stopped"] == "plateau" and r["iteration"] >= 300, f"iterate marca plateau: {r.get('stopped')} en {r.get('iteration')} it")
        scen.write_text("drift")
        r = m.iterate(n=100, block=100)
        check(r["ok"] and r["stopped"] == "max" and r["iterations_run"] == 100, "iterate llega a n sin estabilizarse: stopped=max")
        r = m.iterate(block=250, window=1000)
        check(r["ok"] and r["stopped"] == "max" and r["iterations_run"] == 1000,
              f"iterate until=stable sin n usa 1000 por defecto ({r.get('iterations_run')} it)")
        scen.write_text("stable")
        r = m.iterate(n=30, until="n")
        check(r["ok"] and r["blocks"] == 1 and r["stopped"] == "max" and r["iteration"] == 30 and r["residuals"], "iterate until='n' es un solo bloque")
        pg = m.read_progreso()
        check(pg.get("terminado") is True and pg.get("fase") == "terminado" and pg.get("tool") == "iterate" and pg.get("iteracion") == 30
              and pg.get("H") is not None and pg.get("M") is not None and (pg.get("resultado") or {}).get("ok") is True
              and (pg.get("resultado") or {}).get("stopped") == "max" and pg.get("pid") == os.getpid() and pg.get("falta_s") == 0.0,
              f"progreso.json al terminar iterate: fase terminado con el resultado ({ {k: pg.get(k) for k in ('fase', 'iteracion', 'H', 'resultado')} })")
        r = m.iterate(n=50, reports=["Q_no_existe"])
        check(r["ok"] is False and "Q_no_existe" in r.get("error", ""), "iterate con un reporte inexistente falla con mensaje claro")

        # --- vigilante: macro en marcha, y apagado por inactividad (sin GUI)
        real_sim = m._state().get("sim")
        m._write_state({**m._state(), "sim": str(tmp / "AGENT_FAKE_FAIL.sim")})
        r = m.server_stop(save=True)
        check(r["stopped"] is False and r.get("save_error") and m._port_open(m.PORT), "server_stop con guardado fallido no apaga ni dice «saved»")
        m._write_state({**m._state(), "sim": real_sim})
        check(m.server_status()["alive"], "server_status vivo")
        m.IDLE_MIN = 0.001  # 0,06 s
        fake_m = m.MACROS / "Agent_19990101_000000_000"
        fake_m.with_suffix(".java").write_text("// macro callada", encoding="utf-8")
        fake_m.with_suffix(".out").write_text("AGENT_BEGIN 19990101_000000_000\n", encoding="utf-8")
        check(m.macro_running() and m.idle_seconds() == 0, "macro_running ve un .out sin AGENT_END")
        m._idle_stop()
        check(m._port_open(m.PORT) and m.STATE_FILE.exists(), "watchdog no apaga con una macro en marcha")
        fake_m.with_suffix(".pid").write_text("999999")  # su cliente murió: macro abandonada
        check(not m.macro_running(), "macro con cliente muerto no cuenta como en marcha")
        fake_m.with_suffix(".pid").unlink()
        fake_m.with_suffix(".out").write_text("AGENT_BEGIN x\nAGENT_END x\n", encoding="utf-8")
        check(not m.macro_running(), "macro con AGENT_END ya no cuenta")
        time.sleep(0.2)
        res_idle = m._idle_stop()
        check(not m._port_open(m.PORT) and not m.STATE_FILE.exists(), f"watchdog apaga el solver inactivo (y guarda) cuando no hay GUI ({res_idle})")
        m.IDLE_MIN = 30
        # puerto ocupado por otro proceso: se niega sin copiar el .sim
        srcsim = tmp / "otra.sim"
        srcsim.write_bytes(b"x")
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen(1)
            real_port, m.PORT = m.PORT, busy.getsockname()[1]
            try:
                m.server_start(sim_path=str(srcsim))
                msg = ""
            except RuntimeError as e:
                msg = str(e)
            m.PORT = real_port
        check("ocupado" in msg and not (m.SIMS / "otra.sim").exists(), "puerto ocupado: error claro y sin copiar el .sim")
        # otro proceso lo está arrancando (pid vivo, puerto cerrado, reciente): no se lanza un segundo
        with socket.socket() as sk2:
            sk2.bind(("127.0.0.1", 0))
            free = sk2.getsockname()[1]
        m._write_state({"pid": os.getpid(), "port": free, "started": datetime.now().isoformat(timespec="seconds")})
        try:
            m.server_start()
            msg = ""
        except RuntimeError as e:
            msg = str(e)
        check("arrancando" in msg and m.STATE_FILE.exists(), "server_start no pisa un arranque en curso de otro proceso")
        # .server.json viejo (pid muerto): server_start limpia y relanza; un nombre raro no sale de sims/ ni rompe el Java
        m._write_state({"pid": 999999, "port": m.PORT, "sim": None})
        st = m.server_start(name='../mi caso"x')
        check(st.get("pid") and st["pid"] != 999999 and m._port_open(m.PORT), "server_start con .server.json viejo relanza")
        check(Path(st["sim"]).parent == m.SIMS and Path(st["sim"]).name == "mi_caso_x.sim", f"nombre raro saneado -> {Path(st['sim']).name}")

        # --- vigilante independiente del MCP: UN proceso aparte por puerto, con su propio entorno
        procs_f = tmp / "procs.json"
        procs_f.write_text(json.dumps([{"pid": 77, "name": "starccm+.exe", "cmd": f'"C:\\x\\starccm+.exe" -host localhost:{m.PORT}'}]),
                           encoding="utf-8")
        venv_env = {"COPILOTO_WORK": str(m.WORK), "COPILOTO_AGENT_LOG": str(m.AGENT_LOG), "COPILOTO_VIGILANTE_S": "0.25",
                    "COPILOTO_VIG_GONE_S": "1", "STARCCM_TEST_PROCS": str(procs_f), "STARCCM_IDLE_MIN": "0.012",
                    "COPILOTO_TEST_VIG_BLOCK_S": "1.2"}  # el primer chequeo del vigilante dura 1,2 s (como un server_stop largo)
        saved_env = {k: os.environ.get(k) for k in venv_env}
        os.environ.update(venv_env)
        m.VIGILANTE_ON, m.VIGILANTE_S = True, 0.25
        try:
            ss0 = m.server_status()
            v = m.vigilante_relanzar()  # lo que hace el panel con `--cli vigilante_relanzar` cuando server_status dice vigilante_muerto
            ve0 = m._vig_info(m.PORT)
            time.sleep(0.8)  # el primer chequeo del vigilante está bloqueado (1,2 s): el latido sigue por su hilo
            ve1 = m._vig_info(m.PORT)
            ic_b = m._state().get("idle_checked")
            ss1 = m.server_status()
            check(ss0.get("vigilante_muerto") is True and ss0.get("vigilante_vivo") is False and ss1.get("vigilante_vivo") is True
                  and ss1.get("vigilante_muerto") is False and ss1.get("vigilante") == v["vigilante"],
                  f"server_status dice vigilante_muerto sin vigilante y vigilante_vivo tras vigilante_relanzar ({ss0.get('vigilante_muerto')}, {ss1.get('vigilante_vivo')})")
            check(ve0.get("pid") and ve1.get("t", 0) - ve0.get("t", 0) >= 0.5 and ic_b is None and m._vigilante_pid(m.PORT),
                  f"vigilante: el latido NO depende de terminar cada vuelta (hilo aparte): sigue latiendo con el chequeo bloqueado ({ve1.get('t', 0) - ve0.get('t', 0):.1f} s)")
            end = time.time() + 15
            while time.time() < end and not m._state().get("idle_warning"):
                time.sleep(0.05)
            st, vpid = m._state(), m._vigilante_pid(m.PORT)
            check(v.get("nuevo") and vpid and m._pid_alive(vpid) and v["vigilante"] != os.getpid(),
                  f"_vigilante_ensure lanza el vigilante como proceso aparte (pid {vpid})")
            iw = st.get("idle_warning")
            check(st.get("gui") is True and isinstance(iw, str) and "sin actividad" in iw and "GUI conectada: no se apaga" in iw
                  and isinstance(st.get("idle_min"), (int, float)) and st.get("idle_checked") and m.alive(),
                  f"vigilante con GUI: escribe gui, idle_min e idle_warning como TEXTO y no apaga ({iw!r})")
            check(time.time() - m.STATE_FILE.stat().st_mtime > 0.5, "lo que escribe el vigilante (cada 0,25 s) no cuenta como actividad: el mtime no se mueve")
            ic1 = m._state().get("idle_checked")
            t_ic = time.time()
            while time.time() - t_ic < 3 and m._state().get("idle_checked") == ic1:  # el sello tiene resolución de 1 s
                time.sleep(0.1)
            st = m._state()
            check(st.get("vigilante") == vpid == v["vigilante"] and st.get("idle_checked") and st.get("idle_checked") != ic1
                  and st.get("pid_real") and m._pid_alive(st["pid_real"]) and st.get("residente") and m._pid_alive(st["residente"]),
                  f"el vigilante deja su pid REAL (no el del lanzador) e idle_checked en cada chequeo en .server.json ({st.get('vigilante')}, {ic1} -> {st.get('idle_checked')})")
            v2 = m._vigilante_ensure(m.PORT)
            p2 = subprocess.Popen([sys.executable, str(Path(m.__file__).resolve()), "--vigilante", str(m.PORT)],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            procs_f.write_text("[]", encoding="utf-8")  # el usuario cierra la GUI: sin GUI y pasado el límite, guarda y apaga
            end = time.time() + 15
            while time.time() < end and (m._port_open(m.PORT) or m.STATE_FILE.exists()):
                time.sleep(0.05)
            check(not m._port_open(m.PORT) and not m.STATE_FILE.exists() and "vigilante: solver apagado por" in m.AGENT_LOG.read_text(encoding="utf-8"),
                  "vigilante sin GUI: guarda y apaga el solver inactivo (el MCP no hace nada)")
            end = time.time() + 12
            while time.time() < end and m._pid_alive(vpid):
                time.sleep(0.05)
            check(not m._pid_alive(vpid) and not m._vig_file(m.PORT).exists(), "el vigilante termina cuando el servidor muere")
            check(v2.get("vigilante") == vpid and not v2.get("nuevo") and p2.wait(timeout=30) == 0,
                  "un solo vigilante por puerto (un segundo, lanzado a mano o por server_start, sale sin sustituir al primero)")
        finally:
            for k, val in saved_env.items():
                os.environ.pop(k, None) if val is None else os.environ.__setitem__(k, val)
            m.VIGILANTE_ON, m.VIGILANTE_S = False, 30.0
            vp = m._vig_info(m.PORT).get("pid")
            if vp:
                m._kill_tree(vp)
        st = m.server_start(name="mi_caso_x")  # el resto del selftest sigue con un servidor vivo
        check(st.get("pid") and m.alive(st), "tras el apagado del vigilante, server_start arranca otro servidor")

        # --- GUI conectada
        rows = [{"pid": 1, "name": "starccm+.exe", "cmd": f'"C:\\x\\starccm+.exe" -host localhost:{m.PORT}'},
                {"pid": 2, "name": "starccm+.exe", "cmd": f'"C:\\x\\starccm+.exe" -host localhost:{m.PORT} -batch M.java -noexit'},
                {"pid": 3, "name": "starccm+.exe", "cmd": f"starccm+.exe -server -port {m.PORT} -host localhost:{m.PORT}"},
                {"pid": 4, "name": "starccm+.exe", "cmd": '"C:\\x\\starccm+.exe" -host localhost:1'},
                {"pid": 5, "name": "powershell.exe", "cmd": f"powershell ... starccm -host localhost:{m.PORT}"}]
        check(m._gui_clients(rows, m.PORT) == [1], "_gui_clients: solo la GUI (sin -batch, sin -server, mismo puerto)")
        g = m.gui_status()
        check(g["connected"] is False and g["server_alive"], "gui_status sin GUI: no conectada")
        sl2 = subprocess.Popen([sys.executable, "-I", "-c", "import time;time.sleep(60)", "-host", "localhost:1"])
        sleepers.append(sl2)
        time.sleep(0.4)
        t_n = time.time()
        rows_n = m._procs_native("-host") if os.name == "nt" else []
        t_n = time.time() - t_n
        check(os.name != "nt" or (any(x["pid"] == sl2.pid and "-host localhost:1" in x["cmd"] for x in rows_n) and t_n < 1.0),
              f"detección de GUI sin PowerShell: líneas de comandos leídas del PEB en {t_n * 1000:.0f} ms ({len(rows_n)} procesos con -host)")

        def spy():
            res = real_list()
            seen_rows.append(res)
            return res
        m._list_procs = spy  # desde aquí el listado de procesos es el real (PowerShell, ~1,5 s por llamada)
        m.GUI_TTL = 60
        t0 = time.time()
        r = m.gui_attach(wait_s=20)
        gui_pid = r.get("launched")
        mine = [x for x in (seen_rows[-1] if seen_rows else []) if x["pid"] in r.get("pids", [])]
        check(r["ok"] and r["connected"] and mine, f"gui_attach lanza la GUI y se la ve conectada ({time.time()-t0:.1f}s, pids {r.get('pids')})")
        check(mine and "-podkey" not in mine[0]["cmd"] and KEY not in mine[0]["cmd"] and f"-host localhost:{m.PORT}" in mine[0]["cmd"],
              "la GUI se lanza con -host localhost:PORT y sin -podkey")
        m._list_procs = lambda: list(seen_rows[-1])  # sin PowerShell: la misma foto
        check(m.gui_attach().get("already") is True, "gui_attach con GUI conectada no lanza otra")
        st = m._state()
        check(m.server_status().get("gui") is True and st.get("port") == m.PORT, "server_status informa gui=True")
        # vigilante CON GUI: no apaga, deja idle_warning y eso no cuenta como actividad
        m.IDLE_MIN = 0.001
        time.sleep(0.2)
        m._gui_cache = None
        m._idle_stop()
        st = m._state()
        check(m.alive() and st.get("gui") is True and isinstance(st.get("idle_warning"), str)
              and "GUI conectada: no se apaga" in st["idle_warning"] and "sin actividad" in st["idle_warning"],
              f"vigilante con GUI: no apaga y deja idle_warning como texto ({st.get('idle_warning')!r})")
        check(m.idle_seconds() > m.IDLE_MIN * 60, "escribir idle_warning no cuenta como actividad")
        m.IDLE_MIN = 30
        m.run_java('sim.println("vuelvo");')
        m._idle_stop()
        check("idle_warning" not in m._state(), "al haber actividad se borra idle_warning")
        # I: tope duro STARCCM_MAX_HORAS (por defecto 0 = desactivado): el vigilante guarda y apaga aunque haya GUI y actividad
        real_stop, real_sts, llamadas = m.server_stop, m._started_ts, []
        m.server_stop = lambda save=True: (llamadas.append(save), {"stopped": True, "saved": "x.sim"})[1]
        m._started_ts = lambda st_: time.time() - 7200  # lleva 2 h encendido
        try:
            r_off = m._idle_stop() if m.MAX_HORAS == 0 and not llamadas else None
            m.MAX_HORAS = 3.0
            r_ok = m._idle_stop()  # 2 h < 3 h: no
            m.MAX_HORAS = 1.0
            r_tope = m._idle_stop()  # 2 h > 1 h: guarda y apaga (con la GUI conectada)
            m.server_stop = lambda save=True: {"stopped": False, "save_error": "disco lleno"}
            r_fallo = m._idle_stop()
        finally:
            m.server_stop, m._started_ts, m.MAX_HORAS = real_stop, real_sts, 0.0
        check(r_off in ("ok", "aviso") and r_ok in ("ok", "aviso") and r_tope == "apagado_tope" and llamadas == [True] and r_fallo == "no_apagado"
              and "TOPE DURO STARCCM_MAX_HORAS=1 h" in m.AGENT_LOG.read_text(encoding="utf-8") and "reintento" in m.AGENT_LOG.read_text(encoding="utf-8"),
              f"tope duro de horas: desactivado por defecto ({r_off}), sin superarlo no apaga ({r_ok}), superado guarda y apaga con GUI conectada ({r_tope}) y si no puede guardar lo dice ({r_fallo})")
        # server_stop con GUI: avisa pero apaga (simulación nueva sin nombre: guarda aparte)
        m._write_state({**m._state(), "sim": None})
        rpid = m._res_info().get("pid")
        r = m.server_stop(save=True)
        check(rpid and not m._pid_alive(rpid) and not m._res_info() and r.get("residente") == "detenido", "server_stop detiene el residente")
        check(r["stopped"] and r.get("gui_warning") and r.get("saved") and Path(r["saved"]).parent == m.SIMS and not m.STATE_FILE.exists(),
              "server_stop con GUI avisa (gui_warning), guarda en sims/nuevo_*.sim y apaga")
        check(r.get("timings_s", {}).get("total", 99) < 4 and {"guardar", "apagar", "residente"} <= set(r.get("timings_s", {})),
              f"server_stop sin esperas fijas: sondeo de 0,1 s y STOP al residente a la vez ({r.get('timings_s')})")
        m._kill_tree(gui_pid)
        gui_pid = None
        m._list_procs, m.GUI_TTL = (lambda: []), 10.0

        # --- pump_case con la bomba falsa
        m.PIPELINE_BOMBA = tmp / "bomba_falsa.py"
        cad = tmp / "cad"
        cad.mkdir()
        (tmp / "vacia").mkdir()
        (cad / "bomba.cfturbo-ccm").write_text("<ccm/>", encoding="utf-8")
        r = m.pump_case("BombaFalsa", str(cad))
        check(r["ok"] is False and "No existe" in r.get("error", ""), "pump_case sin pipelines/bomba.py: error claro")
        m.PIPELINE_BOMBA.write_text(_FAKE_BOMBA, encoding="utf-8")
        r = m.pump_case("BombaFalsa", str(cad), dry_run=True)
        check(r["ok"] and r["dry_run"] and r["ccm"]["rpm"] == 1450 and r["macro_chars"] > 50, "pump_case dry_run: genera la macro con la bomba falsa")
        check(m.pump_case("B", str(cad), mode="lenta")["ok"] is False and m.pump_case("B", str(tmp / "nada"))["ok"] is False
              and m.pump_case("B", str(tmp / "vacia"))["ok"] is False, "pump_case valida mode, cad_dir y .cfturbo-ccm")
        r = m.pump_case("BombaFalsa", str(cad), mode="rapida")
        check(r["ok"] and [s["ok"] for s in r["steps"]] == [True, True] and "importado 1450 rapida" in r["steps"][0]["stdout"]
              and (m.CASES / "BombaFalsa").is_dir() and Path(m._state()["sim"]).stem == "BombaFalsa",
              f"pump_case arranca el servidor del caso e inyecta UNA macro ({r.get('seconds')}s; {r.get('error') or r.get('exception') or r.get('steps')})")
        pp = r.get("parsed", {})
        sal = m.CASES / "BombaFalsa" / "salida_pump.txt"
        check(pp.get("RESULT") == {"H": 16.5, "M": 8.4} and pp.get("PIPE") == {"total": 12.5} and pp.get("BALANCE_MASA", {}).get("pct") == 0.0002
              and pp.get("AVISO") == ["algo raro"] and pp.get("EULER", {}).get("EULER_OK", "").startswith("H_Euler 18") and pp.get("CELLS") == 1234,
              f"pump_case devuelve RESULT, PIPE, BALANCE_MASA, AVISO, EULER y CELLS ya leídos ({pp})")
        check(sal.is_file() and "RESULT\tH\t16.5" in sal.read_text(encoding="utf-8") and r.get("salida_completa") == str(sal)
              and "RESULT" in r["stdout"], "pump_case deja la salida completa en casos/<caso>/salida_pump.txt y sigue dando la cola de stdout")
        check(m._pump_imports(type("M", (), {"IMPORTS": ["a.*", "b.*"]})) == "a.*,b.*" and m._pump_imports(type("M", (), {})) == ""
              and "import 'a.*'" not in m._wrap("", str(["a.*", "b.*"]), "x"),
              "imports del pipeline: la lista se une con comas (y _wrap sanea el str(lista) del fallo real)")
        e2e = m.HERE / "casos" / "BombaRapida_e2e_174159" / "e2e_stdout.txt"
        if e2e.is_file():
            q = m._parse_pump(e2e.read_text(encoding="utf-8", errors="replace"))
            check(len(q.get("RESULT", {})) >= 9 and abs(q["RESULT"]["H"] - 16.738) < 0.01 and "iterar" in q.get("PIPE", {})
                  and q.get("BALANCE_MASA", {}).get("pct", 1) < 1e-3 and len(q.get("PNG", [])) == 3,
                  f"_parse_pump lee la salida REAL de la corrida e2e ({len(q.get('RESULT', {}))} resultados, {len(q.get('PIPE', {}))} fases)")
        r = m.pump_case("BombaFalsa", str(cad), mode="normal", params={"fallar": True})
        check(r["ok"] is False and r["failed_at"] == 1 and r["steps"][0]["ok"] is True, "pump_case: un paso que falla se reporta con su índice")
        scen.write_text("stable")
        n_launch = m._res_launches
        r = m.pump_case("BombaFalsa", str(cad), until="stable", n=600, block=100)
        check(r["ok"] and r["via"] == "residente" and r["iterate"]["stopped"] == "stable" and r["iterate"]["blocks"] >= 2
              and m._res_launches == n_launch, f"pump_case until=stable: una macro y luego bloques por el MISMO residente ({r.get('iterate')})")
        pg = m.read_progreso()
        check(pg.get("terminado") and pg.get("tool") == "pump_case" and pg.get("caso") == "BombaFalsa"
              and (pg.get("resultado") or {}).get("iterate", {}).get("stopped") == "stable" and (pg.get("resultado") or {}).get("RESULT", {}).get("H") == 16.5,
              f"progreso.json de pump_case con iterate: terminado, caso y resultado ({ (pg.get('resultado') or {}).get('iterate') })")
        r = m.pump_case("BombaFalsa", str(cad), params={"cancelado": True}, until="stable", n=600)
        check(r["ok"] is False and r.get("cancelled") is True and r.get("stopped") == "cancelled" and "iterate" not in r
              and r.get("resultados") == {"H": 15.9, "M": 8.3} and r["parsed"]["RESULT"] == {"H": 15.9, "M": 8.3}
              and "cancelled" in r["parsed"].get("ITER_STOP", "") and not r.get("macro_abandonada") and not m.abort_file().exists(),
              f"pump_case cancelado: ok False, cancelled True y los RESULT parciales (sin iterate ni post) ({r.get('resultados')}; ok {r['ok']})")
        r = m.pump_case("OtroCaso", str(cad))
        check(r["ok"] is False and "otro .sim" in r.get("error", ""), "pump_case se niega si hay otro .sim cargado")

        # --- progreso en vivo (parser), cancel y --cli
        pgx = m._Progreso()
        pgx._reset("pump_case", "X")
        pgx.iter_max = 1000
        for ln in ("PIPE importar 8.0", "PIPE regiones 0.2", "PIPE fisica 1.3", "PIPE mrf 0.2", "PIPE fronteras 0.2", "PIPE reportes 1.0"):
            pgx._line(ln)
        f1 = pgx.snapshot()["fase"]
        pgx._line("PIPE malla 50.1")
        f2 = pgx.snapshot()["fase"]
        for ln in ("     Iteration     Continuity     X-momentum     pt_in (Pa)        M (N-m)              H",
                   "            30   1.000857e-01   5.427665e-02   1.4e+05   8.453830e+00   1.529018e+01",
                   "            31   9.588507e-02   5.239161e-02   1.5e+05   8.497105e+00   1.553220e+01"):
            pgx._line(ln)
        sn = pgx.snapshot()
        pgx._line("ITER\t50\tH 15.886\tM 8.4846")
        pgx._line("ITER_STOP\tstable\titeraciones 50")
        check((f1, f2) == ("mallar", "euler") and sn["fase"] == "iterar" and sn["iteracion"] == 31 and abs(sn["H"] - 15.5322) < 1e-9
              and abs(sn["M"] - 8.497105) < 1e-9 and abs(sn["continuidad"] - 0.09588507) < 1e-12 and pgx.snapshot()["iteracion"] == 50
              and pgx.snapshot()["fase"] == "post" and pgx.snapshot()["fases_s"]["malla"] == 50.1,
              f"progreso: fases por los PIPE (importar..mallar..iterar..post), tabla de monitores e ITER ({f1}, {f2}, {sn['fase']})")
        e2e_out = m.HERE / "casos" / "BombaRapida_e2e_181510" / "salida_pump.txt"
        if e2e_out.is_file():
            pgr = m._Progreso()
            pgr._reset("pump_case", "real")
            for ln in e2e_out.read_text(encoding="utf-8", errors="replace").splitlines():
                pgr._line(ln)
            sr = pgr.snapshot()
            check(sr["iteracion"] == 250 and abs(sr["H"] - 16.7382) < 1e-3 and abs(sr["M"] - 8.4544) < 1e-3 and sr["fase"] == "post"
                  and sr["continuidad"] is not None and "importar" in sr["fases_s"] and abs(sr["fases_s"]["malla"] - 50.17) < 0.01,
                  f"progreso: la salida REAL de la corrida e2e da iteración 250, H, M, continuidad y las fases ({sr['iteracion']}, {sr['H']}, {sr['fase']})")
        nan_s = m._Progreso()
        nan_s._reset("iterate", "n")
        nan_s._line("ITER\t10\tH NaN\tM 1.5")
        check(json.loads(json.dumps(nan_s.snapshot(), allow_nan=False))["H"] is None, "progreso: NaN sale como null (JSON válido)")

        # ETA: contra la ventana de estabilidad (no contra el tope de 1000 it): sin saltos al pasar la iteración 100 (18 s -> 232 s medido con solver real)
        e2e_j = m.HERE / "casos" / "BombaRapida_e2e_193651" / "e2e.json"
        if e2e_j.is_file():
            ms_ = [x for x in json.loads(e2e_j.read_text(encoding="utf-8")).get("progreso_muestras", [])
                   if x.get("fase") == "iterar" and x.get("iteracion") and x.get("H") is not None and x.get("ritmo_it_s")]
            pe_ = m._Progreso()
            pe_._reset("pump_case", "eta")
            pe_.fkey, pe_.iter_max, pe_.window, pe_.tol, pe_.criterio, pe_.it0 = "iterar", 1000, 100, 0.005, "stable", 1
            serie = []
            for k_, x_ in enumerate(ms_):
                pe_.it = x_["iteracion"]
                pe_.hist.append((pe_.it, x_["H"], x_["M"]))
                pe_._rate = lambda r_=x_["ritmo_it_s"]: r_
                f_ = pe_._falta(1000.0 + 5.1 * k_)
                serie.append((x_["iteracion"], f_["falta_s"], f_["falta_max_s"]))
            vals_ = [v for _, v, _ in serie]
            saltos = [max(a, b) / max(min(a, b), 1e-9) for a, b in zip(vals_, vals_[1:]) if max(a, b) >= 10]
            i109 = next((i for i, (it_, _, _) in enumerate(serie) if it_ >= 100), None)
            check(len(serie) >= 12 and saltos and max(saltos) <= 2.05 and i109 and 10 <= serie[i109][1] <= 150 and serie[i109][2] > 2 * serie[i109][1]
                  and all(mx > v for _, v, mx in serie if v is not None and v > 10) and vals_[-1] < 20,
                  f"ETA con los datos REALES de 193651: sin saltos > x2 entre muestras ({max(saltos or [0]):.2f}), sin escalón al pasar la it. 100 "
                  f"({serie[i109 - 1][1] if i109 else None} -> {serie[i109][1] if i109 else None} s), falta_max_s aparte ({serie[i109][2] if i109 else None} s)")
        else:
            print(f"  {time.time() - t_all:5.1f}s SKIP ETA: falta {e2e_j}")
        # cancel sin iteración en curso: no crea ABORT (pararía la próxima corrida) ni toca nada
        t_c = time.time()
        r = m.cancel()
        check(r["ok"] and r["iteracion_en_curso"] is False and r["cancelled"] is False and not m.abort_file().exists()
              and not m.cancel_file().exists() and time.time() - t_c < 2, "cancel sin iteración en curso: no crea ABORT y responde al instante")

        # cancel de una iteración en marcha: ABORT, la tool ve el cancel, para limpia y borra ABORT y cancel.json
        def lanzar(**kw):
            box = {}

            def run():
                try:
                    box["r"] = m.iterate(**kw)
                except Exception as e:  # noqa: BLE001
                    box["r"] = {"error": repr(e)}
            th_ = threading.Thread(target=run, daemon=True)
            th_.start()
            return th_, box

        def esperar_iteracion(minimo, hasta=20.0):
            t_end = time.time() + hasta
            while time.time() < t_end:
                q = m.read_progreso()
                if q.get("fase") == "iterar" and (q.get("iteracion") or 0) >= minimo and not q.get("terminado") and q.get("H") is not None:
                    return q
                time.sleep(0.1)
            return m.read_progreso()

        scen.write_text("stable")
        ctl.write_text(json.dumps({"delay": 0.02}))
        base_it = m.read_progreso().get("iteracion") or 0
        th_c, box = lanzar(n=2000, until="n")
        live = esperar_iteracion(base_it + 15)
        check(live.get("fase") == "iterar" and not live.get("terminado") and live.get("H") is not None and live.get("M") is not None
              and live.get("continuidad") is not None and (live.get("ritmo_it_s") or 0) > 0 and live.get("falta_s") is not None
              and live.get("transcurrido_s") is not None and live.get("iter_max") == 2000 and live.get("pid") == os.getpid(),
              f"progreso en vivo mientras itera: fase, iteración, H, M, continuidad, ritmo y lo que falta ({ {k: live.get(k) for k in ('fase', 'iteracion', 'H', 'continuidad', 'ritmo_it_s', 'falta_s')} })")
        rc_ = m.cancel()
        th_c.join(timeout=20)
        ri_ = box.get("r", {})
        check(rc_["ok"] and rc_["cancelled"] is True and rc_["iteracion_en_curso"] and rc_["metodo"] == "ABORT"
              and ri_.get("stopped") == "cancelled" and ri_.get("cancelled") is True and ri_.get("ok") is True
              and 0 < (ri_.get("iterations_run") or 0) < 2000 and ri_.get("iteration") < 2000,
              f"cancel detiene las iteraciones en curso por el archivo ABORT ({rc_.get('segundos')} s; parada en {ri_.get('iteration')} de 2000; {rc_.get('metodo')})")
        check(not m.abort_file().exists() and not m.cancel_file().exists() and not m.LOCK_FILE.exists() and m.alive(),
              "tras el cancel se borran ABORT y cancel.json y se libera el turno (el servidor sigue vivo)")
        pgc = m.read_progreso()
        check(pgc.get("terminado") and (pgc.get("resultado") or {}).get("cancelled") is True and (pgc.get("resultado") or {}).get("stopped") == "cancelled",
              "progreso.json deja el resultado cancelado")
        ctl.write_text(json.dumps({"delay": 0}))
        r = m.iterate(n=20, until="n")
        check(r["ok"] and r["stopped"] == "max" and r["iterations_run"] == 20, "tras un cancel la siguiente corrida itera con normalidad (sin ABORT viejo)")
        m.abort_file().write_text("viejo", encoding="utf-8")  # un ABORT que quedó de otra corrida no la para
        m._write_json_atomic(m.cancel_file(), {"t": time.time() - 100})  # ni su cancel.json (la macro de iterar también lo mira)
        r = m.iterate(n=20, until="n")
        check(r["ok"] and r["stopped"] == "max" and r["iterations_run"] == 20 and not m.abort_file().exists() and not m.cancel_file().exists(),
              "un ABORT y un cancel.json viejos se borran al empezar una corrida")

        # una macro que NO obedece el ABORT pero AVANZA: no se abandona ni se mata el residente (matar a mitad de step() tumba el
        # servidor real); se espera el fin del bloque. Con la gracia a 0,8 s el comportamiento viejo la habría matado a los 0,8 s
        ctl.write_text(json.dumps({"delay": 0.02, "ignore_abort": True}))
        n_launch = m._res_launches
        m.CANCEL_GRACE_S, m.CANCEL_BLOCK_X = 0.5, 0.0
        base_it = m.read_progreso().get("iteracion") or 0
        th_g, box_g = lanzar(n=50, until="n")
        esperar_iteracion(base_it + 5)
        t_g = time.time()
        rg_ = m.cancel(wait_s=10)
        th_g.join(timeout=20)
        ri_g = box_g.get("r", {})
        check(rg_["cancelled"] is True and ri_g.get("stopped") == "cancelled" and ri_g.get("ok") is True and (ri_g.get("iterations_run") or 0) >= 40
              and not ri_g.get("macro_abandonada") and m.alive() and m._res_launches == n_launch and not m.abort_file().exists() and not m.cancel_file().exists(),
              f"cancel de una macro que ignora el ABORT pero avanza: se espera el fin del bloque, NO se mata el residente ({time.time() - t_g:.1f} s; {rg_.get('cancelled')}; {ri_g.get('iterations_run')} it; lanzamientos {m._res_launches - n_launch})")
        # macro COLGADA (sin sacar nada tras el cancel durante CANCEL_GRACE_S): única que se abandona, y el resultado lo dice
        ctl.write_text(json.dumps({"delay": 0.02, "ignore_abort": True}))
        n_launch = m._res_launches
        m.CANCEL_GRACE_S = 0.3
        base_it = m.read_progreso().get("iteracion") or 0
        th_h, box_h = lanzar(n=60, until="n")
        esperar_iteracion(base_it + 3)
        ctl.write_text(json.dumps({"delay": 0.02, "ignore_abort": True, "hang": True}))  # la macro se queda muda
        time.sleep(0.2)
        t_h = time.time()
        rh0_ = m.cancel(wait_s=0.4)  # sin forzar: NO se mata el residente aunque la macro lleve más que la gracia sin salida
        check(rh0_["ok"] and rh0_["cancelled"] is False and rh0_.get("pendiente") is True and m._res_launches == n_launch and m.alive()
              and "forzar" in (rh0_.get("reason") or "") and m._pid_alive(m._res_info().get("pid") or 0),
              f"cancel SIN forzar sobre una macro colgada: sigue parando (pendiente), no mata el residente ({rh0_.get('reason')})")
        rh_ = m.cancel(wait_s=10, forzar=True)
        th_h.join(timeout=20)
        ctl.write_text(json.dumps({"delay": 0.02, "ignore_abort": True}))  # suelta lo que quede del servidor falso (<= 1,2 s)
        ri_h = box_h.get("r", {})
        check(rh_["cancelled"] is True and ri_h.get("stopped") == "cancelled" and ri_h.get("ok") is True and "COLGADA" in (ri_h.get("macro_abandonada") or "")
              and m.alive() and m._res_launches == n_launch + 1 and not m.abort_file().exists() and not m.cancel_file().exists(),
              f"cancel(forzar=True) de una macro colgada: se abandona tras la gracia y el resultado lo dice ({time.time() - t_h:.1f} s; {ri_h.get('macro_abandonada')}; lanzamientos {m._res_launches - n_launch})")
        ctl.write_text(json.dumps({"delay": 0}))
        t_w = time.time()
        while time.time() - t_w < 8 and not m._port_open(m.PORT):  # el falso (un solo hilo) acaba lo que le quede de la macro colgada antes de volver a aceptar
            pass
        r = m.run_java('sim.println("tras el cancel duro");')
        check(r["ok"] and r["stdout"] == "tras el cancel duro", "tras abandonar la macro colgada el solver sigue sirviendo por el residente nuevo")
        # la gracia: max(CANCEL_GRACE_S, 3 bloques de 50 it según ritmo_it_s)
        m.CANCEL_GRACE_S, m.CANCEL_BLOCK_X, rate0 = 120.0, 3.0, m._prog._rate
        try:
            m._prog._rate = lambda: 0.1  # 0,1 it/s: un bloque de 50 it tarda 500 s
            lento = m._cancel_stall_s()
            m._prog._rate = lambda: 10.0
            rapido = m._cancel_stall_s()
            m._prog._rate = lambda: None
            sin_ritmo = m._cancel_stall_s()
        finally:
            m._prog._rate = rate0
        check(lento == 1500.0 and rapido == 120.0 and sin_ritmo == 120.0,
              f"gracia del cancel = max(120 s, 3 bloques de 50 it según ritmo_it_s) ({lento}, {rapido}, {sin_ritmo})")
        m.CANCEL_GRACE_S, m.CANCEL_BLOCK_X = 1.5, 3.0

        # cancel con el DUEÑO MUERTO (el panel mató la CLI con «Detener»): la macro sigue iterando en el residente, nadie escribe
        # progreso.json ni borrará ABORT/cancel.json; cancel() cancela igual, espera el fin de la macro y limpia él
        ctl.write_text(json.dumps({"delay": 0.02}))
        muerto = subprocess.Popen([sys.executable, "-c", "pass"])
        muerto.wait()
        body_o = m._abort_enable_java() + 'sim.getSimulationIterator().step(3000);\nsim.println("ITERATION " + sim.getSimulationIterator().getCurrentIteration());\n'
        box_o: dict = {}
        th_o = threading.Thread(target=lambda: box_o.update(r=m._inject(body_o, timeout=60, tool="iterate")), daemon=True)
        th_o.start()
        t_end = time.time() + 10
        while time.time() < t_end and not m.macro_running():
            time.sleep(0.05)
        m._write_json_atomic(m.progreso_file(), {"schema": 1, "tool": "pump_case", "fase": "iterar", "iteracion": 100, "terminado": False,
                                             "pid": muerto.pid, "t": round(time.time(), 2)})
        sin_dueno = not m._progreso_vivo()
        t_o = time.time()
        ro_ = m.cancel(wait_s=10)
        th_o.join(timeout=15)
        pgo = m.read_progreso()
        check(sin_dueno and ro_["ok"] and ro_["cancelled"] is True and ro_.get("duenio_muerto") is True and ro_["iteracion_en_curso"]
              and not th_o.is_alive() and (box_o.get("r") or {}).get("ok") is True and not m.abort_file().exists() and not m.cancel_file().exists()
              and pgo.get("terminado") is True and (pgo.get("resultado") or {}).get("huerfana") is True and m.alive() and not m.macro_running(),
              f"cancel con el dueño muerto y la macro iterando: escribe ABORT, espera el fin, limpia ABORT/cancel.json y deja progreso terminado ({time.time() - t_o:.1f} s; {ro_})")
        ctl.write_text(json.dumps({"delay": 0}))
        m._write_json_atomic(m.progreso_file(), {"schema": 1, "tool": "pump_case", "fase": "iterar", "terminado": False, "pid": muerto.pid, "t": round(time.time(), 2)})
        rn_ = m.cancel()
        check(rn_["ok"] and rn_["cancelled"] is False and rn_["iteracion_en_curso"] is False and not m.abort_file().exists() and not m.cancel_file().exists(),
              "cancel con dueño muerto pero SIN macro en marcha: no crea ABORT (pararía la próxima corrida)")
        # dueño muerto con la macro en una fase NO interrumpible (mallar): se decide con macro_running() + dueño muerto, no con la fase del
        # progreso.json viejo; ABORT va igual y la macro (que mira ABORT tras la malla, antes del primer step()) sale sin iterar ni post
        ctl.write_text(json.dumps({"delay": 0.02}))
        n_launch_m = m._res_launches
        body_m = ('// AGENT_FAKE_MALLA 1.2\nsim.println("PIPE malla 1.2");\nsim.getSimulationIterator().step(3000);\n'
                  'sim.println("ITERATION " + sim.getSimulationIterator().getCurrentIteration());\n')
        box_m: dict = {}
        th_m = threading.Thread(target=lambda: box_m.update(r=m._inject(body_m, timeout=60, tool="pump_case")), daemon=True)
        th_m.start()
        t_end = time.time() + 10
        while time.time() < t_end and not m.macro_running():
            time.sleep(0.05)
        m._write_json_atomic(m.progreso_file(), {"schema": 1, "tool": "pump_case", "fase": "mallar", "iteracion": None, "terminado": False, "pid": muerto.pid,
                                             "fase_falta_s": 40.0, "t": round(time.time(), 2)})
        t_m = time.time()
        rm1 = m.cancel(wait_s=0.4)  # la malla dura 1,2 s: no ha terminado
        check(rm1["ok"] and rm1["cancelled"] is False and rm1.get("pendiente") is True and rm1.get("duenio_muerto") is True and rm1.get("fase") == "mallar"
              and isinstance(rm1.get("parara_en_s"), int) and 30 <= rm1["parara_en_s"] <= 40 and "mallar" in (rm1.get("reason") or "") and "no se interrumpe" in (rm1.get("reason") or "")
              and m.abort_file().exists() and m.cancel_file().exists() and m.macro_running(),
              f"cancel con dueño muerto y fase mallar: pone ABORT igual y responde pendiente con el texto de cuánto falta ({rm1.get('parara_en_s')} s; {rm1.get('reason')})")
        rm2 = m.cancel(wait_s=10)  # segunda llamada: espera el fin de la malla, ve el fin y limpia
        th_m.join(timeout=15)
        pgm = m.read_progreso()
        stm = (box_m.get("r") or {}).get("stdout") or ""
        check(rm2["ok"] and rm2["cancelled"] is True and rm2.get("duenio_muerto") is True and "ITER_STOP\tcancelled" in stm and not any(x.strip()[:1].isdigit() for x in stm.splitlines())
              and not th_m.is_alive() and not m.abort_file().exists() and not m.cancel_file().exists() and not m.macro_running()
              and pgm.get("terminado") is True and (pgm.get("resultado") or {}).get("huerfana") is True and m.alive() and m._res_launches == n_launch_m,
              f"cancel con dueño muerto + malla: la macro para al terminarla (sin iterar), cancel limpia ABORT/cancel.json, cierra el progreso y no toca el residente ({time.time() - t_m:.1f} s; {rm2}; {pgm.get('resultado')})")
        ctl.write_text(json.dumps({"delay": 0}))
        # sin dueño muerto ni macro, la fase del progreso viejo no basta: no se crea ABORT
        m._write_json_atomic(m.progreso_file(), {"schema": 1, "tool": "pump_case", "fase": "mallar", "terminado": False, "pid": muerto.pid, "t": round(time.time(), 2)})
        rm3 = m.cancel()
        check(rm3["ok"] and rm3["cancelled"] is False and rm3["iteracion_en_curso"] is False and not m.abort_file().exists(),
              "cancel con dueño muerto, fase mallar y SIN macro en marcha: no crea ABORT")
        # el vigilante cierra el progreso huérfano si cancel() no esperó (cancel.json/ABORT viejos sin macro ni dueño)
        m._write_json_atomic(m.progreso_file(), {"schema": 1, "tool": "pump_case", "fase": "mallar", "terminado": False, "pid": muerto.pid, "cancelando": True, "t": round(time.time() - 60, 2)})
        m._write_json_atomic(m.cancel_file(), {"t": time.time() - 60, "huerfana": True})
        os.utime(m.cancel_file(), (time.time() - 60, time.time() - 60))
        m.abort_file().write_text("viejo", encoding="utf-8")
        vc_ = m._cancel_stale_cleanup()
        pgv = m.read_progreso()
        check(vc_ is True and pgv.get("terminado") is True and (pgv.get("resultado") or {}).get("huerfana") is True and not m.abort_file().exists() and not m.cancel_file().exists(),
              "el vigilante, al ver el fin de una macro huérfana cancelada, limpia ABORT/cancel.json y deja el progreso terminado")
        # red de seguridad del vigilante: un ABORT/cancel.json viejo sin macro ni dueño se borra; uno reciente no
        m._write_json_atomic(m.cancel_file(), {"t": time.time() - 60})
        os.utime(m.cancel_file(), (time.time() - 60, time.time() - 60))
        m.abort_file().write_text("viejo", encoding="utf-8")
        viejo_ok = m._cancel_stale_cleanup()
        m._write_json_atomic(m.cancel_file(), {"t": time.time()})
        fresco = m._cancel_stale_cleanup()
        check(viejo_ok is True and fresco is False and m.cancel_file().exists() and not m.abort_file().exists(), "vigilante: borra un cancel.json/ABORT huérfano de más de 15 s y respeta uno reciente")
        m._cancel_cleanup()

        # --cli: acciones directas sin modelo (otro proceso, sin importar mcp)
        cli_env = {**os.environ, "COPILOTO_WORK": str(m.WORK), "COPILOTO_AGENT_LOG": str(m.AGENT_LOG), "STARCCM_EXE": str(fake),
                   "STARCCM_PORT": str(m.PORT), "COPILOTO_CLI_LOCK_WAIT_S": "0.3", "STARCCM_VIGILANTE": "0", "FAKE_SCEN_FILE": str(scen)}

        def cli(*args, timeout=30):
            t_ = time.time()
            q = subprocess.run([sys.executable, str(Path(m.__file__).resolve()), "--cli", *args], capture_output=True, text=True,
                               encoding="utf-8", env=cli_env, timeout=timeout)
            try:
                js = json.loads(q.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                js = {"_raw": q.stdout[-300:], "_err": q.stderr[-300:]}
            return q.returncode, js, time.time() - t_

        rc1, j1, t1 = cli("server_status")
        rc2, j2, t2 = cli("gui_status")
        rc3, j3, t3 = cli("cancel")
        rc4, j4, t4 = cli("nada")
        rc5, j5, _ = cli("gui_status", "{no es json")
        check(rc1 == rc2 == rc3 == 0 and j1.get("alive") is True and j1.get("pid") == m._state().get("pid") and j2.get("connected") is False
              and j2.get("server_alive") is True and j3.get("iteracion_en_curso") is False and max(t1, t2, t3) < 2.0,
              f"--cli server_status / gui_status / cancel: JSON por stdout en {max(t1, t2, t3):.2f} s sin modelo ({t1:.2f}, {t2:.2f}, {t3:.2f}; {j1.get('_err') or ''})")
        check(rc4 == 2 and j4.get("ok") is False and "uso" in j4.get("error", "") and rc5 == 2, "--cli: herramienta desconocida o JSON roto -> error en JSON y código 2")
        hold = subprocess.Popen([sys.executable, "-I", "-c", _HOLD, str(m.LOCK_FILE), "1.2"], stdout=subprocess.PIPE, text=True)
        hold.stdout.readline()
        rc6, j6, t6 = cli("server_stop", '{"save": false}')
        check(rc6 == 0 and j6.get("stopped") is False and "panel_demo" in (j6.get("busy") or "") and m.alive() and t6 < 3,
              f"--cli server_stop respeta el candado de turnos: busy en {t6:.1f} s y el solver sigue ({j6.get('busy')})")
        hold.wait(timeout=10)
        try:
            m.LOCK_FILE.unlink()
        except OSError:
            pass
        rc7, j7, t7 = cli("server_stop", '{"save": false}', timeout=60)
        check(rc7 == 0 and j7.get("stopped") is True and not m.alive() and not m.STATE_FILE.exists() and "timings_s" in j7,
              f"--cli server_stop apaga el solver ({t7:.1f} s; fases {j7.get('timings_s')})")
        st = m.server_start(name="BombaFalsa")  # el resto del selftest necesita un servidor vivo
        check(st.get("pid") and m.alive(st) and m.run_java('sim.println("init");')["ok"], "server_start tras el --cli server_stop")

        # --- el servidor muere a mitad de una macro: error claro, no un «ok» vacío
        real_inject = m._inject_local

        def dying(st_, src, timeout):
            m._kill_tree(st_["pid"])
            time.sleep(0.5)
            out = src.with_suffix(".out")
            out.write_text("AGENT_BEGIN medio\n", encoding="utf-8")
            return 1, "conexión perdida", False, out
        m._inject_local, m.RES_ON = dying, False
        try:
            r = m.run_java('sim.println("nunca");')
        finally:
            m._inject_local, m.RES_ON = real_inject, True
        check(not r["ok"] and r.get("server_died") and "murió" in (r["exception"] or ""), "servidor muerto a mitad de macro: server_died + mensaje claro")
        check(not m.macro_running() and not m.LOCK_FILE.exists(), "tras el fallo, su .out queda cerrado y el turno liberado")
        check(not m.server_status()["alive"], "server_status: no vivo tras morir")
        r2 = m.server_stop()
        check(r2["stopped"] is False or r2.get("saved") is None, "server_stop con el servidor muerto no falla")
        check(KEY not in m.AGENT_LOG.read_text(encoding="utf-8"), "agent.log sin la clave")
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
        # --- mantenimiento: residente-*.log (3 últimos) y macros/.classes de más de 7 días
        for q in list(m.RES_DIR.glob("residente-*.log")):
            q.unlink()
        now_ = time.time()
        for i in range(1, 7):
            lf = m.RES_DIR / f"residente-9-{i}.log"
            lf.write_text("x", encoding="utf-8")
            os.utime(lf, (now_ + i, now_ + i))
        cls = m.MACROS / ".classes" / "viejo"
        cls.mkdir(parents=True, exist_ok=True)
        (cls / "A.class").write_text("x", encoding="utf-8")
        (m.MACROS / ".classes" / "nuevo.class").write_text("x", encoding="utf-8")
        os.utime(cls / "A.class", (now_ - 8 * 86400, now_ - 8 * 86400))
        borrado = m._prune_work()
        left = sorted(q.name for q in m.RES_DIR.glob("residente-*.log"))
        check(left == ["residente-9-4.log", "residente-9-5.log", "residente-9-6.log"] and borrado["residente_logs"] == 3,
              f"mantenimiento: residente-*.log deja los 3 últimos ({left})")
        check(not (cls / "A.class").exists() and not cls.exists() and (m.MACROS / ".classes" / "nuevo.class").exists() and borrado["classes"] == 1,
              "mantenimiento: macros/.classes borra lo de más de 7 días (y las carpetas vacías) y deja lo reciente")
        check(agent_log_real == (Path(os.environ["COPILOTO_AGENT_LOG"]) if os.environ.get("COPILOTO_AGENT_LOG") else real_work / "agent.log"),
              f"agent.log va a la carpeta de trabajo ({agent_log_real})")
        jv = bg.get("java")
        if isinstance(jv, str):
            print(f"  {time.time() - t_all:5.1f}s SKIP {jv}")
        else:
            check(isinstance(jv, list) and len(jv) == 2 and all(rc == 0 for _, rc, _ in jv),
                  "Java nuevo (Stop File activo con ruta absoluta, init) compila con el javac de STAR-CCM+"
                  + ("" if isinstance(jv, list) else f" ({jv!r})") + (f" {[(n, e) for n, rc, e in jv if rc]}" if isinstance(jv, list) else ""))
        secs = time.time() - t_all
        check(secs < 45, f"el selftest tarda {secs:.1f} s (< 45 s)")
    finally:
        for pid in [gui_pid] + [s.pid for s in sleepers]:
            if pid:
                m._kill_tree(pid)
        st = m._state()
        if st.get("pid") and m._pid_alive(st["pid"]):
            m._kill_tree(st["pid"])
        m._res_kill("selftest")
        for vf in m.WORK.glob("vigilante-*.json"):
            try:
                m._kill_tree(int(json.loads(vf.read_text(encoding="utf-8")).get("pid") or 0))
            except (OSError, ValueError):
                pass
        os.environ.pop("FAKE_RESIDENT_MODE", None)
        shutil.rmtree(tmp, ignore_errors=True)
    # la documentación cuenta y nombra las tools: que no se quede atrás del código
    src = Path(m.__file__).read_text(encoding="utf-8")
    tools = re.findall(r"^@_tool\s*\ndef (\w+)", src, re.M)
    readme = (Path(m.__file__).parent / "README.md").read_text(encoding="utf-8")
    falta = [t for t in tools if f"`{t}`" not in readme]
    check(f"## Tools ({len(tools)})" in readme and not falta, f"README lista las {len(tools)} tools del código" + (f" (faltan {falta})" if falta else ""))
    print(f"selftest: {len(fails)} fallos" if fails else "selftest: todo en verde")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(_selftest())

"""starccm+ falso para `python mcp_server.py --selftest`. No necesita licencia ni STAR-CCM+.

Imita lo justo del CLI real:
  -version                          imprime una línea de versión
  -server -port P [...] [sim]       abre un socket TCP en P, imprime "Server::start" y atiende
                                    clientes hasta recibir una macro con sim.kill()
  -host localhost:P -batch M.java   manda el texto de la macro al servidor; el servidor "ejecuta"
                                    la macro: imprime cada literal de sim.println("...") (con
                                    marcadores AGENT_BEGIN/END incluidos) en su stdout y lo devuelve
                                    al cliente, que lo reimprime. `AGENT_FAKE_FAIL` en la macro
                                    simula una excepción (rama catch del wrapper o del paso de run_steps).
  -host localhost:P (sin -batch)    «cliente GUI» falso: queda vivo hasta que lo matan (máx. 120 s)
  -host ... -batch Residente.java   «cliente residente» falso: vigila <base>/inbox (la ruta sale del propio
                                    Residente.java), manda cada .java al servidor falso, imprime su salida por stdout
                                    (el log del residente), escribe <base>/out/<macro>.res y late en <base>/heartbeat
                                    cada 0,3 s. Termina con <base>/inbox/STOP. Simulaciones por texto de la macro:
                                    AGENT_FAKE_COMPILE_ERROR (error de compilación, sin AGENT_BEGIN), AGENT_FAKE_HANG
                                    (se cuelga sin resultado), AGENT_FAKE_DIE (el residente muere). Por entorno
                                    FAKE_RESIDENT_MODE: broken (sale al arrancar) | silent (vive pero no late). FAKE_RESIDENT_DELAY=<s>: tarda esos
                    segundos en dar el primer latido.
Entiende además los marcadores de run_steps (STEP_BEGIN/OK/ERROR/END/SKIPPED, `_stop = true;`) y simula iterate:
`.step(N)` avanza una iteración falsa, el plot Residuals se exporta a CSV y cada Report imprime su valor. La
curva sale de la escena que dice el archivo $FAKE_SCEN_FILE: stable | plateau | drift (cambiar de escena
reinicia las iteraciones).
Iteración lenta y cancelable: FAKE_STEP_DELAY=<s> hace que cada iteración de `.step(N)` tarde esos segundos, imprima en vivo la
tabla de monitores (Iteration, Continuity, X-momentum, M (N-m), H) y se detenga si existe el archivo FAKE_ABORT_FILE (por defecto
«ABORT» en el directorio de trabajo del servidor: el criterio «Stop File» de STAR-CCM+). FAKE_IGNORE_ABORT=1 lo ignora (para probar
la macro que no obedece). Con el servidor ya lanzado se controlan por el JSON de $FAKE_CTL_FILE: {"delay": s, "ignore_abort": bool, "hang": bool}. Con hang la macro se queda muda (como una colgada) hasta que vuelve a false. El servidor emite esa salida por el socket a medida que sale y el residente la reimprime en vivo.
Se ejecuta con `python -I tests/fake_starccm.py ...` (ver STARCCM_EXE en mcp_server.py).
"""
import math
import os
import re
import socket
import sys
import time

END = b"\n<<END>>\n"


def main(argv):
    if "-version" in argv:
        print("Simcenter STAR-CCM+ FAKE 0.0 (selftest, sin licencia)")
        return 0
    if "-server" in argv:
        port = int(argv[argv.index("-port") + 1])
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(5)
        print(f"Simcenter STAR-CCM+ FAKE server\nServer::start -host localhost:{port}", flush=True)
        srv.settimeout(5)
        t_end = time.time() + 900  # si un selftest muere sin limpiar (los procesos salen del árbol de la CLI), no queda para siempre
        while True:
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                if time.time() > t_end:
                    return 0
                continue
            conn.settimeout(5)
            data = b""
            try:
                while not data.endswith(END):
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
            except socket.timeout:
                pass
            macro = data.decode("utf-8", "replace").replace(END.decode(), "")
            def emit(txt, conn=conn):
                print(txt, flush=True)
                try:
                    conn.sendall((txt + chr(10)).encode("utf-8"))
                except OSError:
                    pass
            _EMIT[0] = emit
            try:
                out = run_macro(macro)
            except Exception as e:  # noqa: BLE001
                out = f"FAKE_INTERNAL_ERROR: {e!r}"
            _EMIT[0] = None
            print(out, flush=True)
            try:
                conn.sendall(out.encode("utf-8"))
            except OSError:
                pass
            finally:
                conn.close()
            if ".kill()" in macro:
                time.sleep(0.2)
                return 0
    if "-host" in argv and "-batch" in argv:
        host, port = argv[argv.index("-host") + 1].split(":")
        macro_path = argv[argv.index("-batch") + 1]
        with open(macro_path, encoding="utf-8") as f:
            text = f.read()
        if os.path.basename(macro_path).startswith("Residente"):
            return resident(host, int(port), text)
        c = socket.create_connection(("127.0.0.1" if host == "localhost" else host, int(port)), timeout=10)  # localhost -> ::1 primero tardaba 2 s en Windows
        c.sendall(text.encode("utf-8") + END)
        c.shutdown(socket.SHUT_WR)
        out = b""
        while True:
            chunk = c.recv(65536)
            if not chunk:
                break
            out += chunk
        print(out.decode("utf-8", "replace"), flush=True)
        return 0
    if "-host" in argv:  # GUI falsa: no hace nada, solo existe como proceso con -host y sin -batch
        t0 = time.time()
        while time.time() - t0 < 120:
            time.sleep(0.5)
        return 0
    print("fake_starccm: argumentos no soportados: " + " ".join(argv), file=sys.stderr)
    return 2


def send_macro(host, port, text, live=False):
    """Manda una macro al servidor falso y devuelve su salida (lo que hace el cliente batch de un solo uso). live: la va
    imprimiendo a medida que llega (el log del residente la ve en vivo, como el real)."""
    c = socket.create_connection(("127.0.0.1" if host == "localhost" else host, port), timeout=10)
    c.settimeout(600)
    c.sendall(text.encode("utf-8") + END)
    c.shutdown(socket.SHUT_WR)
    out = b""
    while True:
        chunk = c.recv(65536)
        if not chunk:
            break
        out += chunk
        if live:
            print(chunk.decode("utf-8", "replace"), end="", flush=True)
    c.close()
    return out.decode("utf-8", "replace")


def resident(host, port, text):
    import threading
    mode = os.environ.get("FAKE_RESIDENT_MODE", "")
    if mode == "broken":
        print("fake residente roto: no arranca", flush=True)
        return 1
    base = re.search(r'new File\("([^"]+)"\)', text).group(1)
    time.sleep(float(os.environ.get("FAKE_RESIDENT_DELAY", "0") or 0))  # arranque lento: server_start no debe esperarlo
    inbox, out, hb = os.path.join(base, "inbox"), os.path.join(base, "out"), os.path.join(base, "heartbeat")
    run = [True]

    def beat():
        while run[0] and mode != "silent":
            with open(hb, "w") as f:
                f.write(str(time.time()))
            time.sleep(0.3)
    threading.Thread(target=beat, daemon=True).start()
    print("RESIDENTE listo", flush=True)
    t_end = time.time() + 900  # tope por si un selftest muere sin limpiar
    while time.time() < t_end:
        if os.path.exists(os.path.join(inbox, "STOP")):
            break
        for name in sorted(n for n in os.listdir(inbox) if n.endswith(".java")):
            p = os.path.join(inbox, name)
            with open(p, encoding="utf-8") as f:
                macro = f.read()
            err = ""
            if "AGENT_FAKE_DIE" in macro:
                os._exit(3)
            if "AGENT_FAKE_HANG" in macro:
                time.sleep(300)
            if "AGENT_FAKE_COMPILE_ERROR" in macro:
                err = "java.lang.RuntimeException: compilation failed\n\tAgent.java:7: error: cannot find symbol (fake)"
            else:
                try:
                    send_macro(host, port, macro, live=True)
                    print("", flush=True)
                except Exception as e:  # noqa: BLE001
                    err = f"conexión con el servidor: {e!r}"
            print("RESIDENTE_FIN " + name, flush=True)
            os.remove(p)
            tmp = os.path.join(out, name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(("ERR " if err else "OK ") + "1.0\n" + err)
            os.replace(tmp, os.path.join(out, name + ".res"))
        time.sleep(0.02)
    run[0] = False
    print("RESIDENTE fin", flush=True)
    return 0


_LITERAL = re.compile(r'^"((?:[^"\\]|\\.)*)"$')
_HEAD = re.compile(r'^"((?:[^"\\]|\\.)*)"')
_IT = [0]
_SCEN = [None]
_EMIT = [None]


def _scen():
    p = os.environ.get("FAKE_SCEN_FILE")
    name = "stable"
    if p and os.path.isfile(p):
        with open(p, encoding="utf-8") as f:
            name = f.read().strip() or name
    if name != _SCEN[0]:
        _SCEN[0], _IT[0] = name, 0
    return name


def _ctl():
    """Control en caliente (el servidor ya está lanzado): JSON {"delay": s, "ignore_abort": bool, "hang": bool} en $FAKE_CTL_FILE."""
    p = os.environ.get("FAKE_CTL_FILE")
    if p and os.path.isfile(p):
        try:
            import json
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    return {}


def _series(name, it):
    """(reportes, residual) de la iteración `it`."""
    if name == "plateau":  # los reportes se asientan, el residual no baja
        return ({"H": 15 + 5 * math.exp(-it / 50), "M": 2 + math.exp(-it / 50), "eta": 0.8},
                1e-3 * (1 + 0.01 * math.sin(it)))
    if name == "drift":  # nunca se estabiliza
        return {"H": 15 + 0.01 * it, "M": 2 + 0.001 * it, "eta": 0.8}, 1e-3
    return ({"H": 15 + 5 * math.exp(-it / 50), "M": 2 + math.exp(-it / 50), "eta": 0.8},
            10 ** (-1 - it / 100))  # stable: se asienta y el residual baja


def run_macro(text):
    """'Ejecuta' una macro: cada sim.println("literal") se imprime en orden. Las líneas con
    expresiones (no literales) se resumen. AGENT_FAKE_FAIL fuerza la rama catch del wrapper."""
    lines = []
    failed = False
    in_catch = False
    stopped = False  # run_steps con stop_on_error: tras un paso fallido, los siguientes salen como SKIPPED
    suppress = False
    skip_next = 0
    stop_flag = "_stop = true;" in text
    scen = _scen()
    m = re.search(r"\.step\((\d+)\)", text)
    skip_begin = False
    ctl = _ctl()
    delay = float(ctl.get("delay", os.environ.get("FAKE_STEP_DELAY", "0")) or 0)
    if m and delay > 0 and _EMIT[0]:
        b = re.search(r'sim\.println\("(AGENT_BEGIN [^"]+)"\)', text)
        if b:
            _EMIT[0](b.group(1))
            skip_begin = True
        abort = os.environ.get("FAKE_ABORT_FILE", "ABORT")
        ignore = bool(ctl.get("ignore_abort")) or os.environ.get("FAKE_IGNORE_ABORT") == "1"
        it0 = _IT[0]
        for i in range(int(m.group(1))):
            while _ctl().get("hang"):  # macro colgada: no emite nada hasta que el test lo suelte
                time.sleep(0.05)
            if not ignore and os.path.exists(abort):
                break
            time.sleep(delay)
            _IT[0] = it0 + i + 1
            if i % 10 == 0:
                _EMIT[0]("     Iteration     Continuity     X-momentum     M (N-m)              H")
            rep_, res_ = _series(scen, _IT[0])
            _EMIT[0](f"{_IT[0]:13d}   {res_:.6e}   {3 * res_:.6e}   {rep_['M']:.6e}   {rep_['H']:.6e}")
    elif m:
        _IT[0] += int(m.group(1))
    m = re.search(r'rp\.export\("([^"]+)"', text)
    if m:
        rows = ['"Iteration","Continuity","X-momentum"']
        for i in range(1, _IT[0] + 1):
            r = _series(scen, i)[1]
            rows.append(f"{i},{r:.6e},{3 * r:.6e}")
        with open(m.group(1), "w", encoding="utf-8") as f:
            f.write("\n".join(rows) + "\n")
    for m in re.finditer(r"sim\.println\((.*?)\);", text, re.S):
        arg = m.group(1).strip()
        if skip_begin and arg.startswith('"AGENT_BEGIN'):
            continue
        if "STEP_SKIPPED" in arg:
            if stopped:
                lines.append(_LITERAL.match(arg).group(1))
                suppress = True
            else:
                suppress = False
            continue
        if "AGENT_ERROR" in arg:  # bloque catch del wrapper: solo sale si "falló"
            in_catch = True
            if failed:
                lines.append("AGENT_ERROR: java.lang.RuntimeException: fallo simulado (AGENT_FAKE_FAIL)")
                lines.append("\tat Agent.execute(Agent.java:1)")
            continue
        if "AGENT_END" in arg:
            failed = in_catch = suppress = False  # el marcador final siempre sale
        if suppress:
            continue
        if "STEP_ERROR" in arg:  # catch del paso: sale solo si el paso falló; la línea siguiente es el trace
            if failed:
                n = re.search(r"STEP_ERROR (\d+)", arg).group(1)
                lines.append(f"STEP_ERROR {n}: java.lang.RuntimeException: fallo simulado (AGENT_FAKE_FAIL)")
                stopped = stopped or stop_flag
                failed = False
            skip_next = 1
            continue
        if skip_next:
            skip_next -= 1
            continue
        if failed or in_catch:
            continue
        if "AGENT_FAKE_FAIL" in arg:
            failed = True
            continue
        if "n/a" in arg:  # ramas catch de iterate
            continue
        if arg.startswith('"ITERATION '):
            lines.append(f"ITERATION {_IT[0]}")
            continue
        if arg.startswith('"REPORT'):
            for k, v in _series(scen, _IT[0])[0].items():
                lines.append(f"REPORT\t{k}\t{v}")
            continue
        lit = _LITERAL.match(arg)
        if lit:
            lines.append(lit.group(1).encode("utf-8").decode("unicode_escape"))
        elif arg.startswith('"'):
            lines.append(_HEAD.match(arg).group(1) + "<fake>")
        else:
            lines.append("<fake:" + arg[:40] + ">")
    if ".kill()" in text:
        lines.append("FAKE: servidor apagado por sim.kill()")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

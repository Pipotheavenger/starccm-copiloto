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
Entiende además los marcadores de run_steps (STEP_BEGIN/OK/ERROR/END/SKIPPED, `_stop = true;`) y simula iterate:
`.step(N)` avanza una iteración falsa, el plot Residuals se exporta a CSV y cada Report imprime su valor. La
curva sale de la escena que dice el archivo $FAKE_SCEN_FILE: stable | plateau | drift (cambiar de escena
reinicia las iteraciones).
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
        while True:
            conn, _ = srv.accept()
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
            try:
                out = run_macro(macro)
            except Exception as e:  # noqa: BLE001
                out = f"FAKE_INTERNAL_ERROR: {e!r}"
            print(out, flush=True)
            try:
                conn.sendall(out.encode("utf-8"))
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


_LITERAL = re.compile(r'^"((?:[^"\\]|\\.)*)"$')
_HEAD = re.compile(r'^"((?:[^"\\]|\\.)*)"')
_IT = [0]
_SCEN = [None]


def _scen():
    p = os.environ.get("FAKE_SCEN_FILE")
    name = "stable"
    if p and os.path.isfile(p):
        with open(p, encoding="utf-8") as f:
            name = f.read().strip() or name
    if name != _SCEN[0]:
        _SCEN[0], _IT[0] = name, 0
    return name


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
    if m:
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

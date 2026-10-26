"""starccm+ falso para `python mcp_server.py --selftest`. No necesita licencia ni STAR-CCM+.

Imita lo justo del CLI real:
  -version                          imprime una línea de versión
  -server -port P [...] [sim]       abre un socket TCP en P, imprime "Server::start" y atiende
                                    clientes hasta recibir una macro con sim.kill()
  -host localhost:P -batch M.java   manda el texto de la macro al servidor; el servidor "ejecuta"
                                    la macro: imprime cada literal de sim.println("...") (con
                                    marcadores AGENT_BEGIN/END incluidos) en su stdout y lo devuelve
                                    al cliente, que lo reimprime. `AGENT_FAKE_FAIL` en la macro
                                    simula una excepción (rama catch del wrapper).
Se ejecuta con `python -I tests/fake_starccm.py ...` (ver STARCCM_EXE en mcp_server.py).
"""
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
        c = socket.create_connection((host, int(port)), timeout=10)
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
    print("fake_starccm: argumentos no soportados: " + " ".join(argv), file=sys.stderr)
    return 2


_LITERAL = re.compile(r'^"((?:[^"\\]|\\.)*)"$')
_HEAD = re.compile(r'^"((?:[^"\\]|\\.)*)"')


def run_macro(text):
    """'Ejecuta' una macro: cada sim.println("literal") se imprime en orden. Las líneas con
    expresiones (no literales) se resumen. AGENT_FAKE_FAIL fuerza la rama catch del wrapper."""
    lines = []
    failed = False
    in_catch = False
    for m in re.finditer(r"sim\.println\((.*?)\);", text, re.S):
        arg = m.group(1).strip()
        if "AGENT_ERROR" in arg:  # bloque catch del wrapper: solo sale si "falló"
            in_catch = True
            if failed:
                lines.append("AGENT_ERROR: java.lang.RuntimeException: fallo simulado (AGENT_FAKE_FAIL)")
                lines.append("\tat Agent.execute(Agent.java:1)")
            continue
        if "AGENT_END" in arg:
            failed = in_catch = False  # el marcador final siempre sale
        if failed or in_catch:
            continue
        if "AGENT_FAKE_FAIL" in arg:
            failed = True
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

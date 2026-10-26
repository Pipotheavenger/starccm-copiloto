"""CLI de Claude Code falsa para el selftest de ui/: imprime stream-json como `claude -p --output-format stream-json`
(init, un texto, una tool run_java con su resultado, otro texto y el resultado), con pausas para probar el reenganche."""
import json
import sys
import time

pause = float(sys.argv[sys.argv.index("--fake-pause") + 1]) if "--fake-pause" in sys.argv else 0.6


def out(o):
    print(json.dumps(o), flush=True)


out({"type": "system", "subtype": "init", "session_id": "fake-0001", "model": "fake", "mcp_servers": []})
prompt = sys.argv[sys.argv.index("-p") + 1] if "-p" in sys.argv else ""
if "PREGUNTA_PRUEBA" in prompt:  # la página debe pintar una tarjeta con opciones y abrir Fronteras con dos caras dudosas
    q = {"pregunta": "¿Cuál es la entrada de la bomba?", "opciones": ["estator/inlet (recomendado)", "rotor/Inflow", "voluta/outlet"],
         "multiple": False, "ver": "fronteras", "caras": ["estator/inlet", "rotor/Inflow"]}
    txt = "No tengo clara la entrada: hay dos caras candidatas.\n```pregunta\n" + json.dumps(q, ensure_ascii=False) + "\n```"
    out({"type": "assistant", "message": {"content": [{"type": "text", "text": txt}]}})
    out({"type": "result", "subtype": "success", "session_id": "fake-0001", "num_turns": 1})
    sys.exit(0)
if "MENSAJE DEL USUARIO MIENTRAS TRABAJABAS" in prompt:  # la página retomó la conversación con un mensaje en espera
    out({"type": "assistant", "message": {"content": [{"type": "text", "text": "Recibido: " + prompt.rsplit("\n", 1)[-1]}]}})
    out({"type": "result", "subtype": "success", "session_id": "fake-0001", "num_turns": 1})
    sys.exit(0)
out({"type": "assistant", "message": {"content": [{"type": "text", "text": "Importo la geometría."}]}})
time.sleep(pause)
out({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "mcp__starccm__run_java",
                                                   "input": {"body": "// QUE: importar los STEP del caso\npim.importCadPart(x);"}}]}})
time.sleep(pause)
out({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}})
time.sleep(pause)
out({"type": "assistant", "message": {"content": [{"type": "text", "text": "Listo: 3 partes importadas."}]}})
out({"type": "result", "subtype": "success", "session_id": "fake-0001", "num_turns": 2})

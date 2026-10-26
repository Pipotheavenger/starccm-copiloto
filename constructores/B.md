# Constructor B: panel/chat_panel.py

Entregado: `panel/chat_panel.py`, `panel/abrir_panel.ps1`, `.mcp.json.example`; `.gitignore` +`panel/.estado.json`; `tests/fake_claude.py` lee `FAKE_PAUSE` (env) para ir lento.

- Selftest `python panel/chat_panel.py --selftest`: 20 checks, 1,7 s (traducción de tools, parseo, comando con --resume, CLI falsa, sesión, Detener, estado, candado, ventana con withdraw).
- Ventana: 420x640 al borde derecho, -topmost con casilla «Encima» y Ctrl+T, Enter envía, Shift+Enter salto, ↑ recupera el último, fuente 10 pt.
- Estado cada 3 s de `.server.json` (pid vivo, `sim`, mtime = inactividad, `idle_warning`, `gui` o «?») y `.inject.lock` (JSON con `pid` y `tool`; ignora el propio y los > 2 h).
- Abrir GUI / Apagar solver mandan mensaje fijo al chat; Apagar y Nueva conversación piden un segundo clic en 4 s.
- Supuesto para el MCP (constructor A): `.inject.lock` JSON con `pid` y `tool`; `.server.json` con `gui` (bool) opcional.
- Sin probar: CLI real de claude (no lanzada). Captura visual: la captura de ventana salió mal (DPI y se coló otra ventana), descartada y borrada; el aspecto no está revisado a ojo.
- ponytail: mientras la CLI trabaja no hay cola de mensajes (Enviar espera o Detener); el tiempo de cada tool se pone al terminar, no en vivo.

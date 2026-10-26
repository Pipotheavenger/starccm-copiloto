# Lanza el panel de chat sin consola (pythonw del .venv).
$repo = Split-Path -Parent $PSScriptRoot
Start-Process -FilePath (Join-Path $repo ".venv\Scripts\pythonw.exe") -ArgumentList "`"$PSScriptRoot\chat_panel.py`"" -WorkingDirectory $repo


# Testy NESMÍ psát do ostrého stavu mostu. 2. 9. 2026 zapsaly do ~/.local/state/agent2telegram
# testovací `last_tmux_sid` = "s-42" (auto-obnova by po restartu založila prázdnou session)
# a dva falešné přepisy hlasovek. `_state_dir()` čte tuhle proměnnou, tak ji nastavíme dřív,
# než se cokoli z balíku importuje.
import os as _os, tempfile as _tempfile
_os.environ["AGENT2TELEGRAM_STATE"] = _tempfile.mkdtemp(prefix="a2t-tests-")

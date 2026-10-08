import ctypes
import sys
import traceback

from version import APP_NAME, data_home

try:
    from gui import main
    raise SystemExit(main())
except Exception as error:
    if isinstance(error, ModuleNotFoundError) and error.name.startswith("PySide6"):
        message = (f"A interface do {APP_NAME} precisa do PySide6.\n\nReinstale com: python install.py\n\n"
                   "O modo texto continua disponível em launcher.py.")
    else:
        message = f"Não foi possível abrir o {APP_NAME}.\n\n{error}"
        try:
            destination = data_home() / "gui-error.log"
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(traceback.format_exc(), encoding="utf-8")
            message += f"\n\nDetalhes: {destination}"
        except OSError:
            pass
    if sys.platform == "win32":
        ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 16)
    else:
        print(message, file=sys.stderr)

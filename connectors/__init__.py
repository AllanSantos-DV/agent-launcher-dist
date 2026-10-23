"""Fachada: conectores do usuário em ~/.agent-launcher/connectors fazem `from connectors._http import
gateway` e `connectors.ConnectorError`. O código mora em agent_launcher.connectors; os submódulos são registrados com o
mesmo objeto, para não existirem duas classes ConnectorError."""
import sys

import agent_launcher.connectors as _package
from agent_launcher.connectors import _http  # noqa: F401

sys.modules[__name__] = _package
for _name, _module in list(sys.modules.items()):
    if _name.startswith("agent_launcher.connectors."):
        sys.modules["connectors." + _name.split(".", 2)[2]] = _module

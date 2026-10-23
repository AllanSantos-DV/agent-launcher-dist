"""Fachada: conectores do usuário importam `catalog.ModelInfo`. O código mora em agent_launcher.domain.catalog."""
from agent_launcher._facade import alias  # noqa: E402

alias(__name__, "agent_launcher.domain.catalog")

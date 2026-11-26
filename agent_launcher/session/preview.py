"""Resumo do que uma abertura de sessão vai iniciar, sem abrir nada: o mesmo plano do `--dry-run` do modo texto e do
resumo da tela, com a chave do host escondida."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from agent_launcher.domain.catalog import Catalog, token_label
from agent_launcher.domain.conversation_usage import resume_window_warning
from agent_launcher.domain.errors import LauncherError
from agent_launcher.harnesses import registry
from agent_launcher.session import powershell
from agent_launcher.session.conversations import conversations_for
from agent_launcher.session.plan import LaunchPlan, build_plan

REPORT_KEYS = ("cli", "project", "model", "connection", "connector", "targets", "resume", "command", "controlled",
               "controlled_codex", "environment_overrides", "catalog_status", "input_limit", "context_window",
               "auto_compact_token_limit", "claude_families", "warnings")


def planned(home: Path, gateway, project_folder: str, cli: str, model: str, resume: bool, catalog: Catalog,
            controlled: bool = False, conversation: str | None = None, worktree: agent_launcher.session.worktree.Worktree | None = None,
            fork: bool = False) -> LaunchPlan:
    """O plano da sessão terminal; ao retomar, o aviso de conversa maior que a janela do modelo entra nos avisos. Com
    `worktree`, a sessão é a do worktree (veja build_plan)."""
    plan = build_plan(gateway, cli, project_folder, model, resume, catalog, controlled=controlled,
                      conversation=conversation, worktree=worktree, fork=fork)
    if resume:
        warning = resume_window_warning(home, conversations_for(home, registry.get(cli), plan.directory), conversation,
                                        catalog.find(plan.model), catalog.verified)
        if warning:
            plan.warnings.append(warning)
    return plan


def model_catalog(plan: LaunchPlan, catalog: Catalog) -> dict | None:
    """Janela e preço por 1M de tokens (US$) que o host informou para o modelo do plano; None se não informou nada, para o
    relatório não trazer zero nem janela inventados."""
    record = catalog.find(plan.model)
    if record is None:
        return None

    def per_million(value: float | None) -> float | None:
        return None if value is None else round(value * 1_000_000, 6)

    info = {"context_window": record.max_input_tokens, "input_per_million": per_million(record.input_cost_per_token),
            "output_per_million": per_million(record.output_cost_per_token), "confirmed": catalog.verified}
    return info if any(value is not None for key, value in info.items() if key != "confirmed") else None


def plan_report(plan: LaunchPlan, gateway, resume: bool, catalog: Catalog) -> dict:
    """O plano como dicionário legível: a chave do host aparece como <chave> no comando e no ambiente."""
    controlled_commands = registry.get(plan.cli).dry_run_extra(plan) or None
    hidden = {gateway.token} - {""}
    command = ["<chave>" if part in hidden else part for part in plan.command]
    report = {"cli": plan.cli, "project": str(plan.directory), "model": plan.model, "connection": gateway.name,
              "connector": gateway.module.NAME, "targets": gateway.targets, "resume": resume, "command": command,
              "controlled": plan.controlled, "controlled_codex": controlled_commands,
              "environment_overrides": plan.public_overrides, "catalog_status": catalog.status,
              "input_limit": plan.input_limit, "context_window": plan.context_window,
              "auto_compact_token_limit": plan.auto_compact_limit, "claude_families": plan.families,
              "warnings": plan.warnings}
    if plan.endpoint:
        report["endpoint"] = plan.endpoint.url
    if info := model_catalog(plan, catalog):
        report["model_catalog"] = info
    if plan.worktree:
        report["worktree"] = plan.worktree.as_json()
    return report


def catalog_line(info: dict | None) -> str:
    """A linha do resumo com a janela e o preço do `model_catalog` do relatório; traço onde o host não informou."""
    info = info or {}
    window = info.get("context_window")
    shown = "—" if window is None else token_label(window) + ("" if info.get("confirmed", True) else " (não confirmada)")
    prices = [info.get("input_per_million"), info.get("output_per_million")]
    price = "preço —" if all(value is None for value in prices) else (
        "US$ " + "/".join("?" if value is None else f"{value:g}" for value in prices) + " por 1M (entrada/saída)")
    return f"Janela e preço: janela {shown} • {price}"


def plan_text(report: dict) -> str:
    """O resumo em linhas para a tela: o que abre, onde, com qual conexão e modelo, o comando e o que muda no ambiente."""
    label = registry.get(report["cli"]).LABEL if registry.find(report["cli"]) else report["cli"]
    lines = [f"{label} · {'retomar' if report['resume'] else 'nova sessão'}", "",
             f"Pasta:    {report['project']}", f"Modelo:   {report['model']}",
             f"Conexão:  {report['connection']} ({report['connector']})", f"Catálogo: {report['catalog_status']}",
             f"Entrada informada: {token_label(report['input_limit'])}", catalog_line(report.get("model_catalog"))]
    if report.get("worktree"):
        found = report["worktree"]
        lines.insert(3, f"Worktree: {found['branch']} ({'reaproveitado' if found['exists'] else 'novo'})")
    if report.get("endpoint"):
        lines.append(f"Endpoint local: {report['endpoint']} (a chave da conexão fica fora do ambiente da sessão)")
    if report.get("context_window") is not None:
        lines.append(f"Contexto configurado: {token_label(report['context_window'])}")
    if report.get("auto_compact_token_limit") is not None:
        lines.append(f"Compactação nativa: até {token_label(report['auto_compact_token_limit'])} tokens de contexto")
    for family, family_model in (report.get("claude_families") or {}).items():
        lines.append(f"{family.title()}: {family_model}")
    lines += ["", "Comando:", subprocess.list2cmdline(report["command"])]
    if report["environment_overrides"]:
        lines += ["", "Ambiente desta sessão:"]
        lines += [f"{name}={value}" for name, value in report["environment_overrides"].items()]
    if report["warnings"]:
        lines += ["", "Avisos:"]
        lines += [f"- {warning}" for warning in report["warnings"]]
    return "\n".join(lines)


RESUME_SHELLS = ("cmd", "powershell")
_SIMPLE_NAME = re.compile(r"[A-Za-z_]\w*")
REMINDER = "<sua chave>"
_CMD_OPERATORS = "&|<>^"


def _cmd_safe(line: str) -> str:
    """A linha de comando para colar no cmd: `& | < > ^` fora de aspas ganham `^`, para chegarem ao programa como texto. O
    cmd conta cada `"` (também a `\\"` de um argumento) para saber o que está entre aspas, e um `|` dentro de um JSON
    entre `\\"` ficaria de fora."""
    quoted, escaped = False, []
    for char in line:
        if char == '"':
            quoted = not quoted
        elif char in _CMD_OPERATORS and not quoted:
            escaped.append("^")
        escaped.append(char)
    return "".join(escaped)


def resume_command_text(report: dict, shell: str = "cmd", scrubbed: tuple[str, ...] = ()) -> str:
    """O plano de uma sessão (nova ou de retomada) em linhas para colar no `shell` (cmd ou powershell): vai à pasta, limpa as variáveis que o
    launcher tira do ambiente das sessões (`scrubbed`, menos as que a sessão define de novo), define o ambiente da
    sessão e roda o comando. A chave nunca está no relatório; o lembrete `<sua chave>` ocupa o lugar dela. No cmd,
    aspas em `set` mantêm `&` e `<` como texto."""
    if shell not in RESUME_SHELLS:
        raise LauncherError(f"Shell {shell!r} desconhecido; use {' ou '.join(RESUME_SHELLS)}.")
    command = [REMINDER if part == "<chave>" else part for part in report["command"]]
    overrides = {name: REMINDER if value == "<chave>" else value for name, value in report["environment_overrides"].items()}
    cleared = [name for name in scrubbed if name.upper() not in {key.upper() for key in overrides}]
    if shell == "cmd":
        lines = [f'cd /d "{report["project"]}"']
        lines += [f'set "{name}="' for name in cleared]
        lines += [f'set "{name}={value}"' for name, value in overrides.items()]
        lines.append(_cmd_safe(subprocess.list2cmdline(command)))
        return "\n".join(lines)
    lines = [f"Set-Location -LiteralPath {powershell.quote(str(report['project']))}"]
    lines += [f"Remove-Item Env:{name} -ErrorAction SilentlyContinue" if _SIMPLE_NAME.fullmatch(name) else
              f"Remove-Item -LiteralPath {powershell.quote('Env:' + name)} -ErrorAction SilentlyContinue"
              for name in cleared]
    for name in overrides:
        if not _SIMPLE_NAME.fullmatch(name):
            raise LauncherError(f"A variável {name!r} não tem forma de $env: no PowerShell; use --shell cmd.")
    lines += [f"$env:{name}={powershell.quote(value)}" for name, value in overrides.items()]
    lines.append(powershell.native_command(command))
    return "\n".join(lines)


def plan_command(plan: LaunchPlan, gateway, resume: bool, catalog: Catalog, shell: str = "cmd") -> str:
    """O plano da sessão (nova ou de retomada) como texto para colar no `shell`, com a chave escondida."""
    return resume_command_text(plan_report(plan, gateway, resume, catalog), shell, plan.scrubbed)


def resume_command(home: Path, gateway, project_folder: str, cli: str, model: str, catalog: Catalog,
                   conversation: str, shell: str = "cmd") -> str:
    """O comando que continua a conversa fora do launcher, no `shell`: o mesmo plano da retomada pelo botão e do
    `launch --resume --conversation --dry-run`."""
    plan = planned(home, gateway, project_folder, cli, model, True, catalog, conversation=conversation)
    if not plan.resume:
        raise LauncherError("; ".join(plan.warnings) or f"Não há conversa {conversation} do {cli} nesta pasta.")
    return plan_command(plan, gateway, True, catalog, shell)


def new_session_command(home: Path, gateway, project_folder: str, cli: str, model: str, catalog: Catalog,
                        shell: str = "cmd") -> str:
    """O comando que abre uma sessão nova fora do launcher, no `shell`: o mesmo plano do botão Nova sessão e do
    `launch --dry-run`."""
    return plan_command(planned(home, gateway, project_folder, cli, model, False, catalog), gateway, False, catalog,
                        shell)

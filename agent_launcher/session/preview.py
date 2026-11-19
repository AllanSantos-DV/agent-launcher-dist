"""Resumo do que uma abertura de sessão vai iniciar, sem abrir nada: o mesmo plano do `--dry-run` do modo texto e do
resumo da tela, com a chave do host escondida."""
from __future__ import annotations

import subprocess
from pathlib import Path

from agent_launcher.domain.catalog import Catalog, token_label
from agent_launcher.domain.conversation_usage import resume_window_warning
from agent_launcher.harnesses import registry
from agent_launcher.session.conversations import conversations_for
from agent_launcher.session.plan import LaunchPlan, build_plan

REPORT_KEYS = ("cli", "project", "model", "connection", "connector", "targets", "resume", "command", "controlled",
               "controlled_codex", "environment_overrides", "catalog_status", "input_limit", "context_window",
               "auto_compact_token_limit", "claude_families", "warnings")


def planned(home: Path, gateway, project_folder: str, cli: str, model: str, resume: bool, catalog: Catalog,
            controlled: bool = False, conversation: str | None = None) -> LaunchPlan:
    """O plano da sessão terminal; ao retomar, o aviso de conversa maior que a janela do modelo entra nos avisos."""
    plan = build_plan(gateway, cli, project_folder, model, resume, catalog, controlled=controlled,
                      conversation=conversation)
    if resume:
        warning = resume_window_warning(home, conversations_for(home, registry.get(cli), plan.directory), conversation,
                                        catalog.find(plan.model), catalog.verified)
        if warning:
            plan.warnings.append(warning)
    return plan


def plan_report(plan: LaunchPlan, gateway, resume: bool, catalog: Catalog) -> dict:
    """O plano como dicionário legível: a chave do host aparece como <chave> no comando e no ambiente."""
    controlled_commands = registry.get(plan.cli).dry_run_extra(plan) or None
    hidden = {gateway.token} - {""}
    command = ["<chave>" if part in hidden else part for part in plan.command]
    return {"cli": plan.cli, "project": str(plan.directory), "model": plan.model, "connection": gateway.name,
            "connector": gateway.module.NAME, "targets": gateway.targets, "resume": resume, "command": command,
            "controlled": plan.controlled, "controlled_codex": controlled_commands,
            "environment_overrides": plan.public_overrides, "catalog_status": catalog.status,
            "input_limit": plan.input_limit, "context_window": plan.context_window,
            "auto_compact_token_limit": plan.auto_compact_limit, "claude_families": plan.families,
            "warnings": plan.warnings}


def plan_text(report: dict) -> str:
    """O resumo em linhas para a tela: o que abre, onde, com qual conexão e modelo, o comando e o que muda no ambiente."""
    label = registry.get(report["cli"]).LABEL if registry.find(report["cli"]) else report["cli"]
    lines = [f"{label} · {'retomar' if report['resume'] else 'nova sessão'}", "",
             f"Pasta:    {report['project']}", f"Modelo:   {report['model']}",
             f"Conexão:  {report['connection']} ({report['connector']})", f"Catálogo: {report['catalog_status']}",
             f"Entrada informada: {token_label(report['input_limit'])}"]
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

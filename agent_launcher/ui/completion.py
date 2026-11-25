"""Completar com Tab no PowerShell: o script nasce do próprio parser da linha de comando, então comando, opção e valor
fixo novos aparecem sem lista paralela; projetos e conexões são consultados na hora, pelo próprio launcher."""
import argparse

NAMES_BY_OPTION = {"--project": "projects", "--connection": "connections"}

SCRIPT = """Register-ArgumentCompleter -Native -CommandName launcher,launcher.cmd -ScriptBlock {
    param($wordToComplete, $commandAst, $cursorPosition)
    $tree = @{
%(tree)s
    }
    $choices = @{
%(choices)s
    }
    $valued = '%(valued)s' -split ' '
    $names = @{ '--project' = 'projects'; '--connection' = 'connections' }
    $launcher = $commandAst.CommandElements[0].ToString()
    $words = @($commandAst.CommandElements | Select-Object -Skip 1 | ForEach-Object { $_.ToString() })
    if ($wordToComplete -ne '' -and $words.Count -gt 0) { $words = @($words | Select-Object -SkipLast 1) }
    $key = ''
    $skip = $false
    foreach ($word in $words) {
        if ($skip) { $skip = $false; continue }
        if ($word.StartsWith('-')) { $skip = $valued -contains $word; continue }
        $next = ($key + ' ' + $word).Trim()
        if ($tree.ContainsKey($next)) { $key = $next } else { break }
    }
    $previous = if ($words.Count -gt 0) { $words[-1] } else { '' }
    if ($choices.ContainsKey("$key $previous")) { $candidates = $choices["$key $previous"] -split ' ' }
    elseif ($names.ContainsKey($previous)) { $candidates = @(& $launcher completion $names[$previous]) }
    elseif ($tree.ContainsKey($key)) { $candidates = $tree[$key] -split ' ' }
    else { $candidates = @() }
    $candidates | Where-Object { $_ -like "$wordToComplete*" } | ForEach-Object {
        $text = if ($_ -match '\\s') { "'$_'" } else { $_ }
        [System.Management.Automation.CompletionResult]::new($text, $_, 'ParameterValue', $_)
    }
}
"""


def _subparsers(parser: argparse.ArgumentParser):
    return next((action for action in parser._actions if isinstance(action, argparse._SubParsersAction)), None)


def _walk(parser: argparse.ArgumentParser, path: tuple, tree: dict, choices: dict, valued: set) -> None:
    words = []
    for action in parser._actions:
        if isinstance(action, (argparse._HelpAction, argparse._SubParsersAction)):
            continue
        for option in action.option_strings:
            words.append(option)
            if action.nargs != 0:
                valued.add(option)
            if action.choices:
                choices[" ".join((*path, option))] = " ".join(str(item) for item in action.choices)
    sub = _subparsers(parser)
    if sub:
        words = list(sub.choices) + words
        for name, child in sub.choices.items():
            _walk(child, (*path, name), tree, choices, valued)
    tree[" ".join(path)] = " ".join(words)


def powershell_script(parser: argparse.ArgumentParser) -> str:
    """O script que registra o completador de Tab do `launcher` e do `launcher.cmd`, montado do `parser`."""
    tree, choices, valued = {}, {}, set()
    _walk(parser, (), tree, choices, valued)
    lines = lambda table: "\n".join(f"        '{key}' = '{value}'" for key, value in table.items())  # noqa: E731
    return SCRIPT % {"tree": lines(tree), "choices": lines(choices), "valued": " ".join(sorted(valued))}

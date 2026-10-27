#!/usr/bin/env python3
"""Instala, atualiza ou desinstala o Agent Launcher (mesmo processo do install.py do Smart Tool).

Testes antes; cópia com manifesto, backup e rollback para %LOCALAPPDATA%\\Programs\\AgentLauncher; ambiente virtual
próprio com PySide6; atalhos no Menu Iniciar e na Área de Trabalho abrindo a interface sem console.
Os dados do usuário (~/.agent-launcher: projetos, conexões, chaves cifradas) nunca são tocados.
"""
import argparse
import filecmp
import json
import os
import shutil
import subprocess
import sys
import time
import venv
from pathlib import Path

import clis
import terminal
from version import APP_NAME, APP_USER_MODEL_ID, VERSION, data_home

SOURCE = Path(__file__).resolve().parent
DEFAULT_TARGET = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "Programs" / "AgentLauncher"
MANIFEST = ".install-manifest.json"
# Fora da instalação: fonte de desenvolvimento (testes, docs internos, página, release, CI) e, no pacote npm, o
# bootstrap Node (bin/, package.json). terminal/ e .venv/ são da instalação e nunca são apagados por obsolescência.
BLOCKED = {"__pycache__", ".git", ".github", ".claude", ".vscode", ".memory", ".token-guard", ".venv", "terminal",
           "terminal.old", "tests", "docs", "site", "release", "npm", "dist", "bin", "node_modules",
           "AGENTS.md", "CLAUDE.md", "install.py", "package.json", ".gitignore", ".gitattributes", MANIFEST}
MIN_PYTHON = (3, 10)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # instalador iniciado por processo sem console não abre janela
SHORTCUT = f"{APP_NAME}.lnk"
# Pacote de perfil (.alprofile): associação por usuário, sem admin.
CLASSES = r"Software\Classes"
FILE_EXTENSION = ".alprofile"
FILE_TYPE = "AgentLauncher.Profile"
# Caminhos passados por variável de ambiente: nada do usuário é interpolado no script PowerShell.
# System.AppUserModel.ID no atalho: o mesmo id que o app declara ao abrir (gui.create_application). Assim a barra de
# tarefas mostra o ícone do Agent Launcher, e fixar o app fixa o atalho (não o pythonw.exe). WScript.Shell não grava
# essa propriedade; é preciso o IPropertyStore do próprio .lnk.
_APP_ID_TYPE = r"""
using System; using System.Runtime.InteropServices;
public static class LinkAppId {
  [ComImport, Guid("00021401-0000-0000-C000-000000000046")] class ShellLink {}
  [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown), Guid("0000010b-0000-0000-C000-000000000046")]
  interface IPersistFile { void GetClassID(out Guid id); [PreserveSig] int IsDirty();
    void Load([MarshalAs(UnmanagedType.LPWStr)] string file, uint mode);
    void Save([MarshalAs(UnmanagedType.LPWStr)] string file, bool remember);
    void SaveCompleted([MarshalAs(UnmanagedType.LPWStr)] string file);
    void GetCurFile([MarshalAs(UnmanagedType.LPWStr)] out string file); }
  [StructLayout(LayoutKind.Sequential, Pack = 4)] struct PropertyKey { public Guid Format; public uint Id; }
  [StructLayout(LayoutKind.Explicit)] struct PropVariant { [FieldOffset(0)] public ushort Type; [FieldOffset(8)] public IntPtr Value; }
  [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown), Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")]
  interface IPropertyStore { void GetCount(out uint count); void GetAt(uint index, out PropertyKey key);
    void GetValue(ref PropertyKey key, out PropVariant value); void SetValue(ref PropertyKey key, ref PropVariant value);
    void Commit(); }
  public static void Set(string path, string appId) {
    var link = new ShellLink();
    ((IPersistFile)link).Load(path, 2);
    var key = new PropertyKey { Format = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), Id = 5 };
    var value = new PropVariant { Type = 31, Value = Marshal.StringToCoTaskMemUni(appId) };
    try { var store = (IPropertyStore)link; store.SetValue(ref key, ref value); store.Commit(); }
    finally { Marshal.FreeCoTaskMem(value.Value); }
    ((IPersistFile)link).Save(path, true);
    Marshal.ReleaseComObject(link);
  }
}
"""
_SHORTCUTS = (
    "Add-Type -TypeDefinition $env:AL_TYPE; "
    "$shell = New-Object -ComObject WScript.Shell; "
    "foreach ($folder in @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Desktop'))) { "
    "  $path = Join-Path $folder $env:AL_NAME; $link = $shell.CreateShortcut($path); "
    "  $link.TargetPath = $env:AL_PYTHONW; $link.Arguments = '\"' + $env:AL_SCRIPT + '\"'; "
    "  $link.WorkingDirectory = $env:AL_DIR; $link.IconLocation = $env:AL_ICON; "
    "  $link.Description = $env:AL_DESCRIPTION; $link.Save(); [LinkAppId]::Set($path, $env:AL_APPID) }"
)
_REMOVE_SHORTCUTS = (
    "foreach ($folder in @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Desktop'))) { "
    "  $path = Join-Path $folder $env:AL_NAME; if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path } }"
)


def ships(rel: str) -> bool:
    """O arquivo (caminho relativo à fonte) faz parte do produto instalado? Usado também por release/build_package.py."""
    parts = Path(rel).parts
    return bool(parts) and not any(part in BLOCKED for part in parts) and not rel.endswith((".log", ".pyc"))


def source_files():
    files = []
    for current, dirs, names in os.walk(SOURCE):
        dirs[:] = sorted(d for d in dirs if d not in BLOCKED)
        for name in sorted(names):
            rel = (Path(current) / name).relative_to(SOURCE).as_posix()
            if ships(rel):
                files.append(rel)
    return files


def python_paths(target):
    scripts = target / ".venv" / "Scripts"
    return scripts / "python.exe", scripts / "pythonw.exe"


def read_manifest(target):
    """Lê o manifesto antes de tocar em qualquer arquivo; manifesto inválido aborta a instalação."""
    path = target / MANIFEST
    if not path.is_file():
        return set()
    try:
        files = json.loads(path.read_text(encoding="utf-8"))["files"]
        if not isinstance(files, list) or not all(isinstance(rel, str) for rel in files):
            raise ValueError("campo files inválido")
    except (ValueError, KeyError, TypeError) as error:
        raise SystemExit(f"Manifesto inválido em {path} ({error}); nada foi alterado. Corrija ou remova o arquivo.")
    return set(files)


def removable(target, rel):
    """Só remove por obsolescência arquivos dentro do destino e fora das pastas protegidas (.venv etc.)."""
    parts = Path(rel).parts
    if Path(rel).is_absolute() or ".." in parts or any(part in BLOCKED for part in parts):
        return False
    return (target / rel).resolve().is_relative_to(target)


def copy_files(target, files, backup, changed, removed, previous):
    if (target / MANIFEST).is_file():
        shutil.copy2(target / MANIFEST, backup / MANIFEST)
    for rel in files:
        source, dest = SOURCE / rel, target / rel
        if dest.is_file() and filecmp.cmp(source, dest, shallow=False):
            continue
        if dest.is_file():
            (backup / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dest, backup / rel)
        changed.append(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        os.utime(dest)  # o npm grava todo arquivo com a mesma data fixa: sem isto o .pyc antigo continuaria valendo
    for rel in sorted(previous - set(files)):
        if not removable(target, rel):
            print(f"  ! ignorado no manifesto (fora do destino ou protegido): {rel}")
            continue
        if (target / rel).is_file():
            (backup / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(target / rel, backup / rel)
            removed.append(rel)
    (target / MANIFEST).write_text(json.dumps({"installed_at": time.time(), "version": VERSION, "source": str(SOURCE),
                                               "files": files}, indent=1), encoding="utf-8")


def clear_bytecode(target):
    """Apaga os __pycache__ da instalação (fora .venv e terminal/). O Python reaproveita o .pyc quando data e tamanho
    da fonte batem; com a data fixa do pacote npm, um arquivo que muda sem mudar de tamanho (ex.: VERSION 1.0.7 →
    1.0.8) seguiria rodando o código antigo."""
    for cache in list(Path(target).rglob("__pycache__")):
        if not any(part in (".venv", "terminal", "terminal.old") for part in cache.relative_to(target).parts):
            shutil.rmtree(cache, ignore_errors=True)


def restore(target, backup, changed, removed):
    for rel in changed:
        if (backup / rel).is_file():
            shutil.copy2(backup / rel, target / rel)
        else:
            (target / rel).unlink(missing_ok=True)
    for rel in removed:
        if (backup / rel).is_file():
            (target / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(backup / rel, target / rel)
    if (backup / MANIFEST).is_file():
        shutil.copy2(backup / MANIFEST, target / MANIFEST)
    else:
        (target / MANIFEST).unlink(missing_ok=True)
    clear_bytecode(target)


def run_tests(python, root):
    tests = subprocess.run([str(python), str(root / "tests" / "run.py")], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    output = (tests.stdout + tests.stderr).strip()
    print(output[-4000:])
    return tests.returncode == 0


def healthy(python) -> bool:
    """O Python da venv roda? A venv guarda só um redirecionador para o Python base (pyvenv.cfg): se o base foi movido
    ou removido, o python.exe continua no disco e não executa."""
    if not python.is_file():
        return False
    try:
        return subprocess.run([str(python), "-c", "import sys"], capture_output=True, timeout=60,
                              creationflags=NO_WINDOW).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def install_dependencies(target, python):
    if not healthy(python):
        if python.is_file():
            print("Ambiente virtual quebrado (o Python de origem sumiu ou mudou de lugar); recriando…")
            shutil.rmtree(target / ".venv")
        else:
            print("Criando ambiente virtual…")
        venv.create(target / ".venv", with_pip=True)
    result = subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r",
                             str(target / "requirements.txt")], capture_output=True, text=True, encoding="utf-8",
                            errors="replace", creationflags=NO_WINDOW)
    if result.returncode:  # sem console visível, a saída do pip só chega por aqui
        raise RuntimeError("pip install falhou: " + (result.stderr or result.stdout).strip()[-600:])


def powershell(script, env):
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                            env={**os.environ, **env}, capture_output=True, text=True, timeout=60,
                            creationflags=NO_WINDOW)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip()[-600:] or f"PowerShell saiu com {result.returncode}")


def create_shortcuts(target, pythonw):
    powershell(_SHORTCUTS, {"AL_NAME": SHORTCUT, "AL_PYTHONW": str(pythonw), "AL_SCRIPT": str(target / "launcher.pyw"),
                            "AL_DIR": str(target), "AL_ICON": str(target / "assets" / "launcher.ico"),
                            "AL_DESCRIPTION": f"{APP_NAME}: Claude Code e Codex em qualquer provedor",
                            "AL_APPID": APP_USER_MODEL_ID, "AL_TYPE": _APP_ID_TYPE})


class WindowsRegistry:
    """HKEY_CURRENT_USER pelo winreg (sem admin). Os testes passam um registro falso com a mesma interface."""

    def set(self, path: str, name: str, value: str) -> None:
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)

    def get(self, path: str, name: str = "") -> str | None:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
                return winreg.QueryValueEx(key, name)[0]
        except OSError:
            return None

    def delete_tree(self, path: str) -> None:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_ALL_ACCESS) as key:
                while True:
                    try:
                        child = winreg.EnumKey(key, 0)
                    except OSError:
                        break
                    self.delete_tree(f"{path}\\{child}")
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
        except FileNotFoundError:
            pass

    def notify(self) -> None:
        """Avisa o Explorer (SHChangeNotify SHCNE_ASSOCCHANGED) para o ícone e o duplo clique valerem já."""
        try:
            import ctypes
            ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)
        except (AttributeError, OSError):
            pass


def register_file_type(target, pythonw, registry=None):
    """Duplo clique num .alprofile abre o launcher e importa o perfil (HKCU\\Software\\Classes, só deste usuário)."""
    registry = registry or WindowsRegistry()
    registry.set(f"{CLASSES}\\{FILE_EXTENSION}", "", FILE_TYPE)
    registry.set(f"{CLASSES}\\{FILE_TYPE}", "", f"Perfil do {APP_NAME}")
    registry.set(f"{CLASSES}\\{FILE_TYPE}\\DefaultIcon", "", f'"{target / "assets" / "launcher.ico"}"')
    registry.set(f"{CLASSES}\\{FILE_TYPE}\\shell\\open\\command", "",
                 f'"{pythonw}" "{target / "launcher.pyw"}" --import "%1"')
    registry.notify()


def unregister_file_type(registry=None):
    """Tira a associação, mas só a nossa: .alprofile apontando para outro tipo fica como está."""
    registry = registry or WindowsRegistry()
    if registry.get(f"{CLASSES}\\{FILE_EXTENSION}") == FILE_TYPE:
        registry.delete_tree(f"{CLASSES}\\{FILE_EXTENSION}")
    registry.delete_tree(f"{CLASSES}\\{FILE_TYPE}")
    registry.notify()


def uninstall(target):
    if not (target / MANIFEST).is_file():
        raise SystemExit(f"{target} não contém uma instalação do {APP_NAME} (sem {MANIFEST}); nada foi removido.")
    powershell(_REMOVE_SHORTCUTS, {"AL_NAME": SHORTCUT})
    try:
        unregister_file_type()
    except OSError as error:
        print(f"Aviso: associação do arquivo {FILE_EXTENSION} não removida ({error}).", file=sys.stderr)
    shutil.rmtree(target)
    print(f"{APP_NAME} removido de {target}. Seus dados continuam em {data_home()}; apague a pasta se não quiser mantê-los.")


def main():
    if sys.version_info < MIN_PYTHON:
        raise SystemExit(f"{APP_NAME} precisa de Python {'.'.join(map(str, MIN_PYTHON))} ou superior; este é "
                         f"{sys.version.split()[0]} ({sys.executable}).")
    parser = argparse.ArgumentParser(description=f"Instala, atualiza ou desinstala o {APP_NAME}.")
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    parser.add_argument("--skip-tests", action="store_true", help="instala sem rodar tests/run.py (só emergência)")
    parser.add_argument("--skip-deps", action="store_true", help="não cria o ambiente virtual nem roda pip")
    parser.add_argument("--no-shortcuts", action="store_true", help="não cria atalhos no Menu Iniciar e na Área de Trabalho")
    parser.add_argument("--no-file-association", action="store_true",
                        help=f"não associa os arquivos {FILE_EXTENSION} (perfil) ao launcher")
    parser.add_argument("--no-terminal", action="store_true", help="não instala o Windows Terminal quando ele falta")
    parser.add_argument("--no-clis", action="store_true", help="não instala Claude Code e Codex quando faltam")
    parser.add_argument("--terminal-zip", type=Path,
                        help="ZIP do Windows Terminal entregue pela TI (sem acesso ao GitHub); instala em modo portátil")
    parser.add_argument("--uninstall", action="store_true", help="remove a instalação e os atalhos (mantém os dados)")
    args = parser.parse_args()
    target = args.target.resolve()
    if target == SOURCE or target.is_relative_to(SOURCE) or SOURCE.is_relative_to(target):
        raise SystemExit("O destino não pode ser a pasta de origem, ficar dentro dela nem contê-la.")
    if args.uninstall:
        return uninstall(target)
    python, pythonw = python_paths(target)
    if args.skip_tests:
        print("Testes pulados por --skip-tests.")
    elif not run_tests(python if healthy(python) else Path(sys.executable), SOURCE):
        raise SystemExit("Testes falharam; instalação abortada sem tocar na versão instalada.")
    files = source_files()
    previous = read_manifest(target)
    existed = (target / "launcher.pyw").is_file()
    print(("Atualizando" if existed else "Instalando") + f" {APP_NAME} {VERSION} em {target} ({len(files)} arquivos)")
    target.mkdir(parents=True, exist_ok=True)
    backup = target.parent / f".agent-launcher-backup-{time.strftime('%Y%m%d-%H%M%S')}"
    backup.mkdir()
    changed, removed = [], []
    try:
        copy_files(target, files, backup, changed, removed, previous)
        clear_bytecode(target)
        print(f"Arquivos alterados: {len(changed)}; removidos por obsolescência: {len(removed)}")
        if not args.skip_deps:
            install_dependencies(target, python)
        interpreter = python if healthy(python) else Path(sys.executable)
        imports = "import core, catalog, connectors, desktop, launcher" + ("" if args.skip_deps else ", gui")
        compiled = subprocess.run([str(interpreter), "-c", imports], cwd=target,
                                  capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  creationflags=NO_WINDOW)
        if compiled.returncode:
            raise RuntimeError("versão instalada não importa: " + compiled.stderr.strip()[-600:])
        if not args.no_shortcuts:
            if not pythonw.is_file():
                raise RuntimeError("atalhos exigem o ambiente virtual (não use --skip-deps junto com atalhos)")
            create_shortcuts(target, pythonw)
        # Independente dos atalhos: a atualização automática roda com --no-shortcuts e também precisa do duplo clique.
        if not (args.no_file_association or os.environ.get("AGENT_LAUNCHER_NO_FILE_ASSOCIATION")) and pythonw.is_file():
            register_file_type(target, pythonw)
    except BaseException as exc:
        print(f"Falha: {exc}. Restaurando a versão anterior.", file=sys.stderr)
        try:
            restore(target, backup, changed, removed)
        except Exception as error:
            print(f"Rollback incompleto; backup preservado em {backup}: {error}", file=sys.stderr)
            raise SystemExit(1)
        shutil.rmtree(backup, ignore_errors=True)
        raise SystemExit(1)
    try:
        shutil.rmtree(backup)
    except OSError as error:
        print(f"Aviso: instalação concluída, mas o backup não pôde ser apagado ({error}): {backup}", file=sys.stderr)
    if not args.no_terminal:
        # Etapa à parte: sem Terminal o launcher funciona (consoles separados), então a falha avisa e não desfaz nada.
        terminal.ensure(target, args.terminal_zip.resolve() if args.terminal_zip else None)
    if not args.no_clis:
        # Instaladores oficiais por usuário; como o Terminal, a falha avisa e não desfaz a instalação do launcher.
        for cli in ("claude", "codex"):  # os outros destinos (Copilot CLI, VS Code...) são opcionais: pela interface
            clis.ensure(cli)
    print(f"{APP_NAME} pronto." + ("" if args.no_shortcuts else f" Abra pelo atalho '{APP_NAME}' no Menu Iniciar ou na Área de Trabalho."))


if __name__ == "__main__":
    main()

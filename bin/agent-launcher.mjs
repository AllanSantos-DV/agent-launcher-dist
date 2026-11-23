#!/usr/bin/env node
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { closeSync, existsSync, mkdirSync, openSync, readFileSync, realpathSync, renameSync, rmSync, statSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { childEnv, describeError, fetchFromGitHub, relaunchArgs } from "./rede.mjs";

const PACKAGE_DIR = join(dirname(fileURLToPath(import.meta.url)), "..");
const RUNTIME_DIR = join(homedir(), ".agent-launcher", "runtime");
const UV_VERSION = "0.12.23";
const UV_ASSET = "uv-x86_64-pc-windows-msvc.zip";
const UV_SHA256 = "75d05de6762778c31ee183398de7dd15093fad0ed90b1f236d8205ea5ec00c90";
const UV_DIR = join(RUNTIME_DIR, `uv-${UV_VERSION}`);
export const UV_EXE = join(UV_DIR, "uv.exe");
// A mesma pasta estável por versão do launcher (%LOCALAPPDATA%\\AgentLauncher\\cache\\<versão>), nunca um nome novo por execução.
const PACKAGE_VERSION = JSON.parse(readFileSync(join(PACKAGE_DIR, "package.json"), "utf8")).version;
export const UV_SCRATCH = join(process.env.LOCALAPPDATA || join(homedir(), "AppData", "Local"), "AgentLauncher", "cache", PACKAGE_VERSION, "uv");
const LOCK_WAIT_MS = 60_000;
const LOCK_STALE_MS = 15 * 60_000;
const PYTHON_VERSION = "3.12";
const PYTHON_DIR = join(RUNTIME_DIR, "python");
const USAGE = `Uso: npx @allansantos-dev/agent-launcher@latest install [opções]

Instala ou atualiza o Agent Launcher para este usuário do Windows (sem admin). Rode de novo com @latest para atualizar.
Instala só o launcher; cada ferramenta (Claude Code, Codex, Copilot CLI, VS Code, Copilot app) se instala pelo cartão dela.
Opções: --with-clis [claude,codex,copilot], --no-terminal, --terminal-zip CAMINHO, --no-shortcuts, --target PASTA, --uninstall`;

class Failure extends Error {}

// Nada de exit explícito: com o fetch ainda fechando, o Node 24 no Windows aborta (Assertion failed: UV_HANDLE_CLOSING).
// A falha sobe até main(), que grava process.exitCode e deixa o processo terminar sozinho.
function fail(message) {
  throw new Failure(message);
}

function run(command, args, options = {}) {
  const result = spawnSync(command, args, { encoding: "utf8", windowsHide: true, ...options });
  if (result.error) fail(`${command} did not start: ${result.error.message}`);
  if (result.status !== 0) fail(`${command} ${args.join(" ")} exited with ${result.status}: ${(result.stderr || result.stdout || "").trim().slice(-2000)}`);
  return (result.stdout || "").trim();
}

/**
 * Exclusão entre execuções do npx: o arquivo de trava nasce exclusivo (`wx`) e some ao fim; o de um processo morto vale
 * como velho depois de LOCK_STALE_MS.
 * @param {string} lockPath arquivo de trava
 * @param {() => Promise<*>} action roda com a trava; falha em LOCK_WAIT_MS se outra execução a segura
 * @returns {Promise<*>} o que `action` devolve
 */
async function withLock(lockPath, action) {
  mkdirSync(dirname(lockPath), { recursive: true });
  const deadline = Date.now() + LOCK_WAIT_MS;
  for (;;) {
    try {
      closeSync(openSync(lockPath, "wx"));
      break;
    } catch (error) {
      if (error.code !== "EEXIST") throw error;
      let age;
      try {
        age = Date.now() - statSync(lockPath).mtimeMs;
      } catch {
        continue;
      }
      if (age > LOCK_STALE_MS) rmSync(lockPath, { force: true });
      else if (Date.now() >= deadline) fail(`${lockPath} is held by another run; try again.`);
      else await new Promise((resolve) => setTimeout(resolve, 100));
    }
  }
  try {
    return await action();
  } finally {
    rmSync(lockPath, { force: true });
  }
}

/**
 * Baixa o zip do uv e confere o SHA-256 fixado.
 * @returns {Promise<Buffer>} o arquivo baixado
 */
async function downloadUv() {
  const url = `https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/${UV_ASSET}`;
  console.log(`Baixando uv ${UV_VERSION} (gerenciador de Python)...`);
  let response;
  try {
    response = await fetchFromGitHub(url);
  } catch (error) {
    fail(`download of ${url} failed: ${describeError(error)}`);
  }
  if (!response.ok) fail(`download of ${url} failed: HTTP ${response.status}`);
  const archive = Buffer.from(await response.arrayBuffer());
  const digest = createHash("sha256").update(archive).digest("hex");
  if (digest !== UV_SHA256) fail(`${UV_ASSET} checksum mismatch (expected ${UV_SHA256}, got ${digest}).`);
  return archive;
}

/**
 * Extrai o zip com o tar.exe do Windows.
 * @param {string} zip arquivo
 * @param {string} directory pasta de destino
 */
function extractZip(zip, directory) {
  run(join(process.env.SystemRoot || "C:\\Windows", "System32", "tar.exe"), ["-xf", zip, "-C", directory]);
}

/**
 * Grava `data` em `path` por um temporário `.parte` na mesma pasta seguido de rename: quem lê nunca vê o arquivo pela
 * metade, e uma falha não deixa o temporário.
 * @param {string} path destino
 * @param {Buffer|string} data conteúdo
 */
export function writeAtomic(path, data) {
  const partial = `${path}.parte`;
  try {
    writeFileSync(partial, data);
    renameSync(partial, path);
  } catch (error) {
    rmSync(partial, { force: true });
    throw error;
  }
}

/**
 * Garante o uv.exe em RUNTIME_DIR; baixa e extrai pela pasta de trabalho fixa da versão (UV_SCRATCH), esvaziada ao fim.
 * @param {{download?: () => Promise<Buffer>, extract?: (zip: string, directory: string) => void}} steps
 */
export async function ensureUv({ download = downloadUv, extract = extractZip } = {}) {
  if (existsSync(UV_EXE)) return;
  await withLock(`${UV_SCRATCH}.lock`, async () => {
    if (existsSync(UV_EXE)) return;
    const archive = await download();
    rmSync(UV_SCRATCH, { recursive: true, force: true });
    mkdirSync(UV_SCRATCH, { recursive: true });
    try {
      const zip = join(UV_SCRATCH, UV_ASSET);
      writeAtomic(zip, archive);
      mkdirSync(UV_DIR, { recursive: true });
      extract(zip, UV_DIR);
    } finally {
      rmSync(UV_SCRATCH, { recursive: true, force: true });
    }
  });
  if (!existsSync(UV_EXE)) fail(`uv.exe not found in ${UV_DIR} after extracting ${UV_ASSET}.`);
}

function ensurePython() {
  const env = { ...childEnv(process.env), UV_PYTHON_INSTALL_DIR: PYTHON_DIR };
  console.log(`Preparando Python ${PYTHON_VERSION} em ${PYTHON_DIR}...`);
  run(UV_EXE, ["python", "install", PYTHON_VERSION, "--no-bin", "--no-registry"], { env });
  const python = run(UV_EXE, ["python", "find", "--managed-python", "--no-python-downloads", PYTHON_VERSION], { env });
  if (!python.toLowerCase().startsWith(PYTHON_DIR.toLowerCase())) fail(`uv returned a Python outside ${PYTHON_DIR}: ${python}`);
  return python;
}

async function main() {
  const [command, ...rest] = process.argv.slice(2);
  if (command === "--version" || command === "-v") {
    console.log(JSON.parse(readFileSync(join(PACKAGE_DIR, "package.json"), "utf8")).version);
    return;
  }
  if (command !== "install") {
    console.log(USAGE);
    process.exitCode = command === undefined || command === "--help" || command === "-h" ? 0 : 1;
    return;
  }
  if (process.platform !== "win32" || process.arch !== "x64") fail(`Agent Launcher runs on Windows x64 only (this is ${process.platform}-${process.arch}).`);
  const flags = relaunchArgs({ version: process.version, execArgv: process.execArgv, env: process.env });
  if (flags) {
    // A flag só vale na partida do Node: reexecuta este script com o repositório de certificados do Windows ligado.
    const again = spawnSync(process.execPath, [...flags, fileURLToPath(import.meta.url), command, ...rest], {
      windowsHide: true,
      stdio: "inherit",
      env: childEnv(process.env),
    });
    if (again.error) fail(`could not restart node with ${flags.join(" ")}: ${again.error.message}`);
    process.exitCode = again.status ?? 1;
    return;
  }
  await ensureUv();
  const python = ensurePython();
  const installer = spawnSync(python, [join(PACKAGE_DIR, "install.py"), "--skip-tests", ...rest], {
    windowsHide: true,
    stdio: "inherit",
    cwd: PACKAGE_DIR,
    env: { ...childEnv(process.env), PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8" },
  });
  if (installer.error) fail(`the installer did not start: ${installer.error.message}`);
  process.exitCode = installer.status ?? 1;
}

if (process.argv[1] && realpathSync(process.argv[1]) === realpathSync(fileURLToPath(import.meta.url))) {
  main().catch((error) => {
    console.error(`agent-launcher: ${error instanceof Failure ? error.message : error.stack || describeError(error)}`);
    process.exitCode = 1;
  });
}

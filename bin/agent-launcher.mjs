#!/usr/bin/env node
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { childEnv, describeError, relaunchArgs } from "./rede.mjs";

const PACKAGE_DIR = join(dirname(fileURLToPath(import.meta.url)), "..");
const RUNTIME_DIR = join(homedir(), ".agent-launcher", "runtime");
const UV_VERSION = "0.12.23";
const UV_ASSET = "uv-x86_64-pc-windows-msvc.zip";
const UV_SHA256 = "75d05de6762778c31ee183398de7dd15093fad0ed90b1f236d8205ea5ec00c90";
const UV_DIR = join(RUNTIME_DIR, `uv-${UV_VERSION}`);
const UV_EXE = join(UV_DIR, "uv.exe");
const PYTHON_VERSION = "3.12";
const PYTHON_DIR = join(RUNTIME_DIR, "python");
const USAGE = `Uso: npx @allansantos-dev/agent-launcher@latest install [opções]

Instala ou atualiza o Agent Launcher para este usuário do Windows (sem admin). Rode de novo com @latest para atualizar.
Opções: --no-clis, --no-terminal, --terminal-zip CAMINHO, --no-shortcuts, --target PASTA, --uninstall`;

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

async function ensureUv() {
  if (existsSync(UV_EXE)) return;
  const url = `https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/${UV_ASSET}`;
  console.log(`Baixando uv ${UV_VERSION} (gerenciador de Python)...`);
  let response;
  try {
    response = await fetch(url);
  } catch (error) {
    fail(`download of ${url} failed: ${describeError(error)}`);
  }
  if (!response.ok) fail(`download of ${url} failed: HTTP ${response.status}`);
  const archive = Buffer.from(await response.arrayBuffer());
  const digest = createHash("sha256").update(archive).digest("hex");
  if (digest !== UV_SHA256) fail(`${UV_ASSET} checksum mismatch (expected ${UV_SHA256}, got ${digest}).`);
  const scratch = mkdtempSync(join(tmpdir(), "agent-launcher-uv-"));
  try {
    const zip = join(scratch, UV_ASSET);
    writeFileSync(zip, archive);
    mkdirSync(UV_DIR, { recursive: true });
    run(join(process.env.SystemRoot || "C:\\Windows", "System32", "tar.exe"), ["-xf", zip, "-C", UV_DIR]);
  } finally {
    rmSync(scratch, { recursive: true, force: true });
  }
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

main().catch((error) => {
  console.error(`agent-launcher: ${error instanceof Failure ? error.message : error.stack || describeError(error)}`);
  process.exitCode = 1;
});

#!/usr/bin/env node
// Confere um pacote do Agent Launcher publicado no engine-registry, no contrato ed25519-sha256-raw do kit do registry
// (kit/signature.mjs): <asset>.sha256 com o sha256 do asset e <asset>.sig com 64 bytes crus da assinatura Ed25519 do
// sha256, contra a chave pública (32 bytes em hex) que quem chama fixou. O Python da instalação não tem Ed25519 na
// biblioteca padrão; o Node já é requisito do instalador.
//
//   node verificar.mjs <asset> <chave-publica-hex>
//
// Saída: uma linha JSON {"ok":true} ou {"ok":false,"reason":"..."}; código 0 só quando ok. Nunca aceita na dúvida.
import { readFileSync } from "node:fs";
import { basename } from "node:path";
import { createHash, createPublicKey, verify } from "node:crypto";

const SPKI_PREFIX = Buffer.from("302a300506032b6570032100", "hex"); // envelope DER fixo de uma chave Ed25519 crua
const SIGNATURE_BYTES = 64;

/** Confere sha256 e assinatura de `asset` contra `publicKeyHex`; devolve {ok:true} ou {ok:false, reason}, sem lançar. */
function check(asset, publicKeyHex) {
  if (!/^[0-9a-fA-F]{64}$/.test(publicKeyHex ?? "")) {
    return { ok: false, reason: "chave pública malformada (esperado 64 caracteres hex)" };
  }
  let blob, sidecar, signature;
  try {
    blob = readFileSync(asset);
    sidecar = readFileSync(`${asset}.sha256`, "utf8");
    signature = readFileSync(`${asset}.sig`);
  } catch (error) {
    return { ok: false, reason: `arquivo ausente: ${error.message}` };
  }
  const match = /^([0-9a-f]{64})(?:\s+\*?(\S+))?\s*$/.exec(sidecar.trim());
  if (!match) {
    return { ok: false, reason: `${basename(asset)}.sha256 malformado` };
  }
  if (match[2] && match[2] !== basename(asset)) {
    return { ok: false, reason: `${basename(asset)}.sha256 é de outro arquivo (${match[2]})` };
  }
  const digest = createHash("sha256").update(blob).digest();
  if (digest.toString("hex") !== match[1]) {
    return { ok: false, reason: "sha256 do pacote não confere com o .sha256" };
  }
  if (signature.length !== SIGNATURE_BYTES) {
    return { ok: false, reason: `.sig com ${signature.length} bytes (esperado ${SIGNATURE_BYTES} crus)` };
  }
  try {
    const key = createPublicKey({ key: Buffer.concat([SPKI_PREFIX, Buffer.from(publicKeyHex, "hex")]), format: "der", type: "spki" });
    if (verify(null, digest, key, signature)) {
      return { ok: true };
    }
    return { ok: false, reason: "assinatura Ed25519 não confere com a chave do Agent Launcher" };
  } catch (error) {
    return { ok: false, reason: `verificação da assinatura falhou: ${error.message}` };
  }
}

const [asset, publicKeyHex] = process.argv.slice(2);
const result = asset ? check(asset, publicKeyHex) : { ok: false, reason: "uso: node verificar.mjs <asset> <chave-publica-hex>" };
console.log(JSON.stringify(result));
process.exitCode = result.ok ? 0 : 1;

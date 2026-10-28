// Rede corporativa com inspeção de TLS: a CA da empresa está no repositório de certificados do Windows, mas o Node
// só olha as CAs embutidas. Daqui saem a flag que liga o repositório do sistema, o ambiente dos processos filhos
// (uv, npm) e a descrição de erro com a causa. NODE_EXTRA_CA_CERTS, SSL_CERT_FILE, REQUESTS_CA_BUNDLE e UV_NATIVE_TLS
// são do usuário: nunca são criadas nem alteradas aqui.

const SYSTEM_CA_FLAG = "--use-system-ca";

// --use-system-ca: Node 22.15+, 23.8+ e 24+ (Windows e macOS); antes disso a flag é opção inválida.
export function supportsSystemCa(version) {
  const [major, minor] = version.replace(/^v/, "").split(".").map(Number);
  return major >= 24 || (major === 23 && minor >= 8) || (major === 22 && minor >= 15);
}

// Argumentos de node a acrescentar ao reexecutar o próprio script, ou null se não é preciso (já ligado, o usuário
// desligou com NODE_USE_SYSTEM_CA=0, ou este Node não conhece a flag).
export function relaunchArgs({ version, execArgv, env }) {
  if (!supportsSystemCa(version)) return null;
  const configured = [...execArgv, ...(env.NODE_OPTIONS || "").split(/\s+/)];
  if (configured.includes(SYSTEM_CA_FLAG) || env.NODE_USE_SYSTEM_CA !== undefined) return null;
  return [SYSTEM_CA_FLAG];
}

// Ambiente dos filhos: repositório do sistema ligado no Node (npm que o instalador roda) e no uv, só onde o usuário
// não decidiu nada.
export function childEnv(env) {
  const result = { ...env };
  if (result.NODE_USE_SYSTEM_CA === undefined) result.NODE_USE_SYSTEM_CA = "1";
  if (result.UV_SYSTEM_CERTS === undefined && result.UV_NATIVE_TLS === undefined) result.UV_SYSTEM_CERTS = "true";
  return result;
}

const CERTIFICATE_CODES = /CERT|SELF_SIGNED|UNABLE_TO_(GET|VERIFY)|ISSUER/;

// "fetch failed" sozinho não diz nada: a causa real (código e mensagem) está em error.cause.
export function describeError(error) {
  const parts = [];
  let certificate = false;
  for (let current = error, depth = 0; current && depth < 5; current = current.cause, depth++) {
    const code = current.code ? `${current.code}: ` : "";
    parts.push(`${code}${current.message ?? String(current)}`);
    certificate ||= CERTIFICATE_CODES.test(current.code ?? "");
  }
  const text = parts.join(" <- ");
  return certificate
    ? `${text}\n  O certificado da cadeia não é confiável para o Node (inspeção de TLS da rede?). Use Node 22.15+ com --use-system-ca,` +
      " ou aponte NODE_EXTRA_CA_CERTS para a CA da empresa."
    : text;
}

// Download do GitHub: um redirecionamento só pode ir (https) para os hosts que o GitHub usa para servir release.
const GITHUB_REDIRECT_HOSTS = ["github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"];

export async function fetchFromGitHub(url, { fetchImpl = fetch, hosts = GITHUB_REDIRECT_HOSTS, redirects = 5 } = {}) {
  const response = await fetchImpl(url, { redirect: "manual" });
  if (response.status < 300 || response.status >= 400) return response;
  const location = response.headers.get("location");
  if (!location || redirects === 0) throw new Error(`download of ${url} redirected without a usable Location`);
  const next = new URL(location, url);
  if (next.protocol !== "https:" || !hosts.includes(next.hostname)) {
    throw new Error(`download of ${url} redirected to ${next.href}, outside the GitHub download hosts; refused`);
  }
  return fetchFromGitHub(next.href, { fetchImpl, hosts, redirects: redirects - 1 });
}

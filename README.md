# Agent Launcher

Abre **Claude Code** e **Codex** nos seus projetos usando o provedor de modelos que você escolher: um proxy LiteLLM
ou host próprio (inclusive por túnel/VPN), roteadores como **9router**, **OmniRoute** e **OpenRouter**, as APIs da
**Anthropic** e da **OpenAI**, ou qualquer host compatível com Anthropic Messages ou OpenAI Responses.

A lista de modelos vem do próprio host. O launcher lê o que cada host informa (disponibilidade, janela de entrada,
saída máxima, ferramentas) e configura cada sessão só com o que foi confirmado. Nada de catálogo fixo no código.

Interface gráfica Windows (PySide6) e modo texto (biblioteca padrão). **Status:** 1.0.13, Windows 10/11.

## Requisitos

- Windows 10 ou 11 (x64) e Node.js 18 ou superior (para o `npx`). Não precisa de Python nem de administrador: o
  instalador prepara um Python só para o Agent Launcher, isolado do resto da máquina.
- Claude Code e Codex são instalados automaticamente se faltarem (instaladores oficiais, por usuário). O Codex precisa
  de um host com a API **Responses**: ele não aceita `/chat/completions`.

## Instalar

```powershell
npx @allansantos-dev/agent-launcher@latest install
```

O instalador copia o app para `%LOCALAPPDATA%\Programs\AgentLauncher`, cria o ambiente com PySide6, instala o que
faltar (Windows Terminal, Claude Code, Codex) e coloca o atalho **Agent Launcher** no Menu Iniciar e na Área de
Trabalho. O atalho abre a interface direto, sem janela de console. Opções úteis: `--no-clis`, `--no-terminal`,
`--terminal-zip CAMINHO`, `--no-shortcuts`.

### Atualização automática

A partir da 1.0.5, a interface confere o npm a cada abertura e, havendo versão nova, se atualiza sozinha em segundo
plano. O pacote só é aceito se vier do próprio registro e bater com o sha512 publicado nele. A troca é feita pelo
instalador da versão nova, com backup e rollback se algo falhar. Em seguida a interface se reinicia sozinha em 5 s,
porque a janela aberta segue com o código antigo na memória. As sessões abertas continuam. Se houver uma caixa de
diálogo aberta ou uma instalação em curso, ela espera e mostra **reinicie**. Janela que fica aberta por dias também
confere o npm a cada 4 horas. Sem rede, nada muda: o resultado da última verificação aparece
em Diagnóstico e em `~/.agent-launcher/update.log`. Para desligar, use **Atualizar automaticamente** no menu de
opções, `launcher preferences --auto-update off` ou `AGENT_LAUNCHER_NO_AUTO_UPDATE=1`. Para atualizar na hora:
`launcher update` (`--check` só consulta). Quem está na 1.0.4 ou anterior atualiza uma última vez à mão, rodando de
novo o comando de instalação acima.

Desinstalar: `npx @allansantos-dev/agent-launcher@latest install --uninstall`. Remove o app e os atalhos; seus dados
ficam em `~/.agent-launcher`.

### Claude Code e Codex

Quando um dos dois falta, o instalador usa o instalador oficial do fabricante, por usuário e sem admin
(`claude.ai/install.ps1` e `chatgpt.com/codex/install.ps1`, ambos conferem o SHA-256 do binário). Se ele for
bloqueado, tenta `npm install -g`. Depois disso a interface também oferece **Instalar** ao lado do CLI que faltar, e
o modo texto tem `launcher cli --install claude|codex|all`. Não é preciso fazer login nos CLIs: a chave vem da conexão.

### Windows Terminal

As sessões abrem como abas do Windows Terminal (é nele que Shift+Enter quebra linha no Claude Code e no Codex).
Se a máquina não tem o Terminal, o instalador resolve **sem admin**, nesta ordem:

1. Usa o Windows Terminal já instalado.
2. Baixa a distribuição ZIP oficial (github.com/microsoft/terminal) para `AgentLauncher\terminal`, em modo
   portátil: não passa pela Store, por pacote MSIX nem por winget. O download só é aceito se o SHA-256 bater com o
   publicado na release e o `wt.exe` tiver assinatura válida da Microsoft. Exige Windows 10 2004 ou superior.
3. Tenta o winget, a menos que a política da empresa bloqueie winget ou pacotes MSIX por usuário.

Vale igual para a instalação pelo npm. A interface também confere o Terminal ao abrir. Ela não depende só do PATH:
procura o alias da Store em `%LOCALAPPDATA%\Microsoft\WindowsApps\wt.exe`, porque um processo aberto por outro com
PATH reduzido não enxerga essa pasta. Se ele faltar, a interface instala sozinha em segundo plano, pela mesma ordem.
Só quando as três formas são bloqueadas o launcher segue sem o Terminal e avisa, com o botão **Copiar erro**, que
copia o aviso e o contexto (versões, Windows, PATH, Terminal) para colar num chat de IA. Cada ocorrência fica em
`~/.agent-launcher/terminal.log`. As sessões abrem então em consoles separados (Ctrl+J continua quebrando linha).
Em empresa sem
acesso ao GitHub, a TI entrega o ZIP: `npx @allansantos-dev/agent-launcher@latest install --terminal-zip C:\caminho\Microsoft.WindowsTerminal_x64.zip`.
`launcher terminal` mostra o que foi encontrado e os bloqueios corporativos detectados (máquina em domínio
ou Entra ID, Store removida ou restrita, MSIX por usuário bloqueado, winget desativado, AppLocker, SRP);
`launcher terminal --install [--zip ...]` instala depois. `--no-terminal` pula a etapa.
 Remove o app e os atalhos; seus dados ficam em `~/.agent-launcher`.

## Conexões

Na interface, use **Gerenciar conexões** → **Adicionar**, escolha o tipo e preencha os campos. **Testar e salvar**
consulta a lista de modelos antes de gravar: conexão que não responde não é salva. Pode haver várias conexões; cada
projeto lembra a última conexão, o CLI e o modelo usados.

| Conector | Claude Code | Codex | Modelos | Contexto |
| --- | --- | --- | --- | --- |
| LiteLLM / host próprio | `{endereço}` | `{endereço}/v1` | só `claude-*` / só `gpt-*`, `o*` (ou qualquer um, opcional) | `/v1/model/info` |
| 9router | `{endereço}` | `{endereço}/v1` | qualquer modelo nos dois | `capabilities` |
| OmniRoute | `{endereço}` | `{endereço}/v1` | qualquer modelo nos dois | `context_length`, `max_input_tokens` |
| OpenRouter | `https://openrouter.ai/api` | `https://openrouter.ai/api/v1` | qualquer modelo com ferramentas | `context_length` |
| Anthropic API | `https://api.anthropic.com` (x-api-key) | — | Claude | `max_input_tokens` |
| OpenAI API | — | `https://api.openai.com/v1` | GPT/o | não informado |
| Compatível com Anthropic | `{endereço}` | — | o que o host listar + extras | se o host informar |
| Compatível com OpenAI | — | `{endereço}` (Responses) | o que o host listar + extras | se o host informar |

Conexões que levam o Claude Code a hosts de terceiros (LiteLLM, 9router, OmniRoute, OpenRouter, compatíveis com
Anthropic) têm a opção **Tool search de MCP no Claude Code**: fora da Anthropic o Claude Code carrega todas as
ferramentas MCP de uma vez, e com a opção ligada elas passam a carregar sob demanda (`ENABLE_TOOL_SEARCH=true`), com
bem menos contexto. Só ligue se o host suportar `tool_reference`; sem esse suporte as requisições falham. O teste da
conexão do 9router também confere a chave (o `/v1/models` dele responde sem chave).

Modelos de imagem, áudio, embeddings e os que o host declara sem ferramentas não entram nas listas. Para hosts que não
listam tudo, os conectores compatíveis aceitam **Modelos extras** (IDs separados por vírgula).

No modo texto:

```powershell
launcher connectors
launcher connections add --name "Meu túnel" --connector litellm --set base_url=http://10.0.0.5
$env:OR_KEY = "sk-or-..."; launcher connections add --name OpenRouter --connector openrouter --secret-env api_key=OR_KEY
launcher connections default "Meu túnel"
launcher connections test OpenRouter
```

Chaves nunca vão na linha de comando: `--secret-env` lê de uma variável de ambiente, e sem ela o comando pergunta de
forma oculta.

## Sessões

Selecione projeto, conexão, CLI e modelo; **Nova sessão** ou **Retomar última**. **Retomar última** só fica habilitado
quando há conversa daquele CLI na pasta: as do terminal e do app contam; as de `claude -p` e do SDK não, porque o
`--continue` as ignora. Sem conversa anterior (inclusive numa sessão aberta pelo orquestrador), a retomada vira
sessão nova, com um aviso na janela, em vez de parar em "No conversation found to continue". Com o Windows Terminal instalado, cada sessão vira
uma aba da janela **agent-launcher** (laranja para Claude Code, verde para Codex), com o título
**Projeto — Claude Code/Codex**; sem ele, cada sessão abre num console próprio. A sessão confere a conexão antes de iniciar. Fechar o launcher não encerra as
sessões. A lista de projetos fica do mais recente para o mais antigo e se reordena a cada abertura; o botão
direito em um projeto edita (nome e pasta) ou remove o cadastro. `Ctrl+K` busca projetos, `Ctrl+F` modelos, `Ctrl+N` adiciona projeto e `F5` atualiza o catálogo.

Cada sessão recebe a configuração só no próprio processo; `~/.claude/settings.json` e `~/.codex/config.toml` não são
alterados.

- **Claude Code:** `ANTHROPIC_BASE_URL` do conector. A chave vai em `ANTHROPIC_AUTH_TOKEN` (Bearer) ou em
  `ANTHROPIC_API_KEY` (x-api-key, API da Anthropic), e só no ambiente do processo; a outra variável vai vazia no
  `--settings` inline para que settings do usuário não a reativem. Os aliases Opus/Sonnet/Haiku apontam para a maior
  versão disponível de cada família, preferindo o mesmo provider do modelo escolhido. Família ausente no host usa o
  modelo principal, com aviso, em vez de cair num ID que o host não tem.
- **Codex:** provider `agent_launcher` por `-c` (`base_url`, `env_key=AGENT_LAUNCHER_API_KEY`, `wire_api=responses`).

### Contexto

- Para um modelo de outro fornecedor no Claude Code (via roteador), o limite informado pelo host vai em
  `CLAUDE_CODE_MAX_CONTEXT_TOKENS`; sem isso o Claude Code assumiria a janela padrão para um ID que não reconhece.
- `[1m]` é acrescentado a um modelo **Claude** (e a cada alias elegível) só quando o host informa entrada de pelo menos
  1.000.000 tokens para aquele ID. Sem metadados confirmados, 1M fica desligado na sessão.
- No Codex, o limite informado vira `model_context_window`, e a compactação automática fica no menor entre 90% dele e o
  limite menos a reserva de saída (`max_output_tokens`, limitada a 25%). Essa margem é política do launcher. Para IDs
  com prefixo de provider, o launcher gera um catálogo nativo temporário a partir de `codex debug models --bundled`;
  se o Codex não conhece o modelo (outro fornecedor via roteador), a sessão abre com aviso de que a janela efetiva pode
  ser menor.
- Cache de catálogo: seis horas por conexão. Cache vencido ou catálogo sem confirmação nunca ativa aumentos de contexto.

### Bypass de permissões

Desligado por padrão. A caixa **Bypass de permissões** vale só para a próxima abertura; o padrão fica em
**Opções (⋯) → Bypass marcado por padrão** (ou `launcher.py preferences --bypass-default on`). No Claude acrescenta
`--dangerously-skip-permissions`; no Codex, `--dangerously-bypass-approvals-and-sandbox`, que **também desliga o sandbox**.

### Sessão controlada por orquestrador

`desktop.py --controlled` ou `launcher.py launch --controlled` abre uma sessão para um orquestrador externo coordenar.
Implica bypass e:

- **Claude:** `--append-system-prompt` com a nota de delegação. Só valem mensagens entre sessões que tragam o código de
  delegação secreto de `~/.cli-launcher/orchestrador-delegacao.token` (criado na primeira vez e lido pelo orquestrador).
- **Codex:** um app-server próprio (`app-server --listen ws://127.0.0.1:<porta>`, autenticado por token aleatório) e a
  TUI conectada a ele. O registro `~/.cli-launcher/codex-controlled/<porta>.json` é como o orquestrador encontra a
  sessão. O app-server fica num Job Object do Windows e morre junto com a janela.

Sessão controlada não pode parar esperando clique (o orquestrador já validou o projeto antes de cadastrar). Só nela,
e só para a pasta do projeto cadastrado:

- **Claude:** antes de abrir, grava `hasTrustDialogAccepted` daquela pasta no `~/.claude.json` (troca atômica, refeita
  se outra sessão gravou no meio).
- **Codex:** `-c check_for_update_on_startup=false`, confiança da pasta só neste processo
  (`-c projects={"<pasta>"={trust_level="trusted"}}`, nada gravado em disco) e, antes de abrir (conversa nova ou
  retomada), a confiança dos hooks novos ou alterados daquela pasta gravada pelo app-server do Codex (o mesmo "Trust
  all" da revisão de hooks, em `hooks.state`). Os hooks do projeto ficam confiados e ativos de verdade, sem
  `--dangerously-bypass-hook-trust`. Se o Codex recusar a confiança, ou algum hook continuar sem ela, a sessão não abre
  e o erro aparece na janela.

Fora do modo controlado (abrir pela interface ou pelo modo texto sem `--controlled`), os CLIs continuam pedindo essas
confirmações normalmente.

Retomar (`--continue`) uma conversa grande (mais de 100 mil tokens) parada há mais de 70 minutos faz o Claude Code
oferecer o diálogo "Resume from summary", que ninguém responde numa sessão sem humano, e a sessão trava. Nas
sessões controladas o launcher eleva os limites desse diálogo (`CLAUDE_CODE_RESUME_THRESHOLD_MINUTES` e
`CLAUDE_CODE_RESUME_TOKEN_THRESHOLD`) só no processo da sessão. A retomada segue com a conversa inteira, e a
configuração global e as sessões normais continuam com a recomendação.

O orquestrador também fecha a sessão de verdade: ele grava `~/.cli-launcher/close-requests/<pid da janela>` e encerra
o CLI; a janela do launcher vê o pedido, sai com código 0 (sem pedir ENTER) e o Windows Terminal fecha a aba.

### Janelas: segundo plano, sempre por cima e abrir por cima

Sessão controlada abre **em segundo plano**: o Windows Terminal ativa a janela ao abrir uma aba (não há opção para não
ativar), então o launcher devolve o foco do teclado a quem estava na frente logo em seguida. Pela interface, ou com
`background=False`, a sessão abre na frente como antes.

```python
from desktop import start_session
start_session(store, projeto, "claude", modelo, controlled=True)                    # segundo plano (padrão)
start_session(store, projeto, "claude", modelo, controlled=True, background=False)  # primeiro plano, a pedido
```

**Sempre por cima:** uma janela marcada pelo título (ex.: a tela do orquestrador em modo app) fica `HWND_TOPMOST`, e
tudo o que abrir depois fica por baixo dela. Um vigia oculto (um só por usuário, sem console) reaplica se ela perder o
estado ou for recriada; ele só mexe na ordem Z, nunca no foco.

```powershell
launcher topmost on --title "Jarvis"   # marca e mantém
launcher topmost off                   # desmarca (o vigia sai)
launcher topmost status                # {"enabled", "title", "found", "topmost", "front_until"}
```

**Abrir por cima** (só quando o usuário pede, ex.: "abre o PDF"): a janela marcada dá lugar, o item abre na frente
pelo shell, e o "sempre por cima" volta sozinho quando o usuário clica na janela marcada, passa do item para outra
janela, o tempo acaba (padrão 300 s) ou pede para voltar (`topmost resume`, ou Ctrl+Alt+J em qualquer lugar).

```powershell
launcher front "C:\docs\relatorio.pdf" --seconds 600
launcher topmost resume                # "voltar": encerra o abrir por cima agora
```

Em Python: `focus.topmost_on(titulo)`, `focus.topmost_off()`, `focus.topmost_resume()` e
`focus.open_front(alvo, segundos)`. Estado em `~/.cli-launcher/topmost.json`.

**Monitor das sessões:** `launcher preferences --sessions-monitor other` leva as sessões em segundo plano para o
monitor que não tem a janela marcada (ou para um não principal, sem janela marcada), se houver mais de um.
`--sessions-monitor primary` volta ao padrão (ficam onde abrirem).

## Escrever um conector

Um conector é um arquivo `.py` em `~/.agent-launcher/connectors/`. Ele aparece na interface ao lado dos embutidos;
um arquivo com erro é listado com a mensagem e não afeta os outros.

```python
from connectors._http import gateway

NAME = "meu_host"                  # id único
LABEL = "Meu host"                 # nome exibido
HINT = "Quando usar este conector."
FIELDS = (                         # viram campos do formulário; "secret" fica cifrado com DPAPI
    {"name": "base_url", "label": "Endereço", "required": True, "placeholder": "http://10.0.0.5:8080"},
    {"name": "api_key", "label": "Chave", "secret": True},
)
# Fábrica pronta para hosts que servem /v1/models, /v1/messages e /v1/responses na mesma base.
targets, verify, models = gateway("http://10.0.0.5:8080")
```

Para um host diferente, implemente as três funções: `targets(options)` devolve
`{"claude": url_ou_None, "codex": url_ou_None, "any_model": bool, "claude_auth": "bearer" | "api_key"}`;
`verify(options)` levanta `connectors.ConnectorError` com um motivo acionável; `models(options)` devolve
`(lista de catalog.ModelInfo, contexto_confirmado)`. O contrato completo está em `connectors/__init__.py`.

## Comandos

O modo texto fica em `%LOCALAPPDATA%\Programs\AgentLauncher\launcher.cmd`; nos exemplos, `launcher` é esse
caminho.

```powershell
launcher                       # menu de texto
launcher doctor                # CLIs + todas as conexões, sem gerar tokens
launcher projects add "C:\meus projetos\app" --name "Meu app"
launcher projects edit "Meu app" --name "App" --path "D:\app"   # só o cadastro muda
launcher projects import "C:\caminho\config.json"   # só nomes e caminhos
launcher models --connection OpenRouter --cli claude --search sonnet --refresh
launcher launch --project "Meu app" --connection OpenRouter --cli codex --model "openai/gpt-6" --dry-run
launcher launch --project "Meu app" --resume
launcher update                # atualiza agora pelo npm (--check só consulta)
launcher topmost on --title "Jarvis"   # janela sempre por cima (off, resume, status)
launcher front "C:\docs\relatorio.pdf" # abrir por cima, a pedido do usuário
launcher preferences --sessions-monitor other   # sessões em segundo plano no outro monitor
```

`--dry-run` mostra o plano (comando, overrides, contexto, avisos) com a chave mascarada, sem rede e sem salvar nada.

## Dados e segurança

- `~/.agent-launcher/config.json`: projetos, conexões (sem chaves) e preferências. `AGENT_LAUNCHER_HOME` escolhe
  outra pasta. Gravação atômica; configuração inválida é preservada, nunca sobrescrita.
- `~/.agent-launcher/secrets.json`: chaves das conexões, cada uma cifrada com DPAPI para o seu usuário do Windows.
  Uma cópia do arquivo não serve em outra conta ou máquina. Isso não protege contra programas rodando como você.
- `~/.agent-launcher/cache/`: catálogo por conexão (IDs e limites numéricos; nunca chaves nem payloads brutos).
- As consultas não seguem redirecionamentos (a chave não vaza para outro host). Hosts locais e privados não passam
  pelo proxy HTTP do sistema.
- A abertura dos CLIs usa argumentos separados, sem `cmd /c` nem scripts temporários. Wrappers `.cmd` desconhecidos
  são recusados em vez de executados.
- Remover um projeto apaga só o cadastro, nunca a pasta.

## Licença

MIT. Veja `LICENSE`.

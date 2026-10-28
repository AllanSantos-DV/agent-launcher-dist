# Agent Launcher

Abre **Claude Code**, **Codex** e **GitHub Copilot CLI** nos seus projetos, e configura o chat do Copilot no
**VS Code** e o **GitHub Copilot app**, usando o provedor de modelos que você escolher: um proxy LiteLLM ou host próprio
(inclusive por túnel/VPN), roteadores como **9router**, **OmniRoute** e **OpenRouter**, as APIs da **Anthropic** e da
**OpenAI**, ou qualquer host compatível com Anthropic Messages, OpenAI Responses ou Chat Completions.

A lista de modelos vem do próprio host. O launcher lê o que cada host informa (disponibilidade, janela de entrada,
saída máxima, ferramentas) e configura cada sessão só com o que foi confirmado. Nada de catálogo fixo no código.

Interface gráfica Windows (PySide6) e modo texto (biblioteca padrão). **Status:** 1.1.0, Windows 10/11.

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

A partir da 1.0.5, a interface confere a cada abertura o manifest do engine-registry (a partir da versão publicada com a entrada do
launcher lá) e, havendo versão nova, se atualiza sozinha em segundo plano. O pacote só é aceito se vier da release
`agent-launcher-vX.Y.Z` do registry e a assinatura Ed25519 dele conferir com a chave fixada no código do launcher (precisa
do Node.js); qualquer recusa é erro visível e nada é aplicado, sem queda para o npm. A troca é feita pelo
instalador da versão nova, com backup e rollback se algo falhar. Em seguida a interface se reinicia sozinha em 5 s,
porque a janela aberta segue com o código antigo na memória. As sessões abertas continuam. Se houver uma caixa de
diálogo aberta ou uma instalação em curso, ela espera e mostra **reinicie**. Janela que fica aberta por dias também
confere o npm a cada 4 horas. Sem a interface aberta (modo texto, ou sessões abertas por outro programa como um
orquestrador), o launcher confere no máximo a cada 4 horas num processo oculto que não atrasa o comando; a versão nova
vale para os próximos comandos e sessões. Sem rede, nada muda: o resultado da última verificação aparece
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

**Atualização das CLIs.** Cada botão de CLI mostra a versão instalada (`--version`) e, quando o registro do npm tem uma mais
nova, a faixa de avisos (informativa, separada da de erros) diz "Codex 0.164.0 disponível (instalada 0.162.1)" e aparece **Atualizar Codex 0.162.1 → 0.164.0**.
Com **Atualizar as CLIs automaticamente** ligado (padrão), o launcher dispara o `update` da própria CLI (ou o
`npm install -g …@latest` de quem a instalou pelo npm) e avisa "Codex atualizado: 0.162.1 → 0.164.0". Sessão
aberta não impede: o Windows deixa renomear um .exe em execução, então a sessão segue na versão antiga e a nova vale na próxima
abertura (a faixa diz quantas sessões estão nessa situação); só quando a tentativa bate num arquivo preso (npm com a CLI
rodando) a atualização fica adiada, com o nome do processo e o PID, e tenta de novo na próxima verificação. Falha vira
aviso, nunca derruba o launcher, e no npm uma CLI que deixou de executar volta à versão anterior. Para desligar: o item do
menu de opções, `launcher preferences --cli-auto-update off` ou `AGENT_LAUNCHER_NO_CLI_UPDATE=1`. Modo texto: `launcher cli`
mostra as versões e `launcher cli --update claude|codex|copilot|all` atualiza na hora. VS Code e o app do Copilot ficam de
fora: cada um se atualiza sozinho.

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

## Destinos

| Destino | Como abre | Protocolos | Login no GitHub |
| --- | --- | --- | --- |
| Claude Code | aba do Windows Terminal | Anthropic Messages | — |
| Codex | aba do Windows Terminal | OpenAI Responses | — |
| GitHub Copilot CLI | aba do Windows Terminal | Messages (Claude), Responses (GPT-5 em diante), Chat Completions (os outros) | não precisa |
| VS Code | janela do VS Code no perfil **Agent Launcher** | os três, pelo endpoint local | não precisa |
| GitHub Copilot app | o app, com o provider **Agent Launcher — conexão** | Responses (GPT-5 em diante) e Chat Completions, pelo endpoint local | o app pede conta GitHub |

Os três de terminal recebem a conexão só no ambiente do processo da sessão: nada é gravado na configuração deles. O
Copilot CLI e o Copilot app não vêm no instalador; a interface oferece **Instalar** para o Copilot CLI (npm ou winget,
por usuário). Claude Code e Codex têm sessão controlada pelo orquestrador; o Copilot CLI ainda não.

**VS Code e Copilot app guardam a configuração**, então nunca recebem a chave nem o endereço real do host. Eles
apontam para o **endpoint local** da conexão, um processo oculto do launcher que escuta só em `127.0.0.1` (porta fixa
por conexão, a partir de 47910), descarta qualquer credencial que o destino mande, acrescenta a da conexão e repassa
ao host com streaming. Trocar a chave ou o host no launcher vale na próxima requisição, sem mexer no destino. O endpoint
sobe ao abrir o destino e encerra sozinho após 4 h sem uso; `launcher endpoint autostart --connection NOME` faz ele
subir com o Windows (sem janela, sem admin), para o VS Code e o app acharem a conexão com o launcher fechado. O log
(`~/.agent-launcher/endpoint.log`) tem rota, modelo, status e duração; nunca corpo, prompt ou chave.

- **VS Code:** o launcher abre o projeto num perfil próprio, **Agent Launcher** (criado pelo VS Code na primeira
  vez), e grava nele os modelos da conexão (`chatLanguageModels.json`, provider Custom Endpoint) e o modelo escolhido
  como padrão do chat. O seu perfil e as suas extensões não são tocados. Escolha o modelo no chat do Copilot.
- **Copilot app:** o launcher grava o provider **Agent Launcher — conexão** no banco do app
  (`~/.copilot/data.db`), com backup antes, sem mexer nos seus outros providers. Se já estiver certo, não faz nada; se
  faltar ou divergir, pergunta, fecha o app (inclusive o ícone da bandeja), grava e abre de novo. Esse banco é interno
  e sem documentação: se o formato mudar, o launcher não grava e mostra os dados para cadastrar à mão em Settings →
  Model providers.

## Perfis

Para quem atende clientes diferentes, cada um com a sua LLM autorizada: **perfis** separam projetos e conexões. No
topo da interface, escolha o perfil ou **Novo perfil…**; trocar mostra só os projetos e as conexões dele. As chaves de
cada perfil ficam num arquivo próprio. Quem não cria perfil continua no **Padrão**, sem nada diferente.

- A sessão abre com o perfil do projeto; fora do Padrão, o nome do perfil aparece no título da aba e no nome do
  provider no VS Code e no Copilot app (`Agent Launcher — Cliente A — Minha conexão`).
- A mesma pasta em dois perfis é permitida, com aviso: os CLIs guardam o histórico por pasta, então **Retomar
  última** usa só as conversas abertas neste perfil (a interface avisa, sem perguntar).
- Conectores do usuário (`~/.agent-launcher/connectors`) valem para todos os perfis.
- **Importar perfil…** (ou duplo clique num arquivo `.alprofile`) cria o perfil pronto, com conexões e conectores;
  **Exportar perfil…** gera esse arquivo. O arquivo nunca leva chave: se a conexão pede uma, o launcher pergunta.
- **Remover perfil** (ou conexão) oferece tirar também o provider do Copilot app e do VS Code e parar o endpoint local.
- **Uso do mês…**: tokens e custo estimado por destino e modelo, lidos das conversas e do endpoint deste perfil.
  É estimativa local; o custo usa o preço que o host informa no catálogo.
- Fora do Padrão, a aba do Terminal usa a cor do perfil.
- Modo texto: `launcher profiles [list|add NOME|rename|remove|use|import ARQUIVO|export REF ARQUIVO]`,
  `launcher usage [--month AAAA-MM]` e `launcher --profile NOME <comando>`.
- Os perfis extras ficam em `~/.agent-launcher/profiles/<id>/`; o orquestrador lista e abre projetos de todos.

## Conexões

Na interface, use **Gerenciar conexões** → **Adicionar**, escolha o tipo e preencha os campos. **Testar e salvar**
consulta a lista de modelos antes de gravar: conexão que não responde não é salva. Pode haver várias conexões; cada
projeto lembra a última conexão, o CLI e o modelo usados.
Conexão usada pelo VS Code ou pelo Copilot app mostra o endpoint local dela: porta, rodando ou parado, requisições,
**Iniciar/Parar** e **Iniciar com o Windows**.

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

A conexão LiteLLM (e conectores que a embrulham) tem o campo opcional **Nome da chave no host**: num host que guarda
a chave de vários clientes, cada conexão (ou perfil por cliente) informa o nome com que a chave foi registrada, e todas
as URLs da conexão passam a ir para `{endereço}/k/<nome>/...` (catálogo, teste e os destinos de cada CLI). Antes de
usar o nome, o launcher exige que o `/healthz` do host anuncie `"key_routing": true`; host que não separa chaves é
recusado em vez de usar a chave padrão. Vazio, nada muda.

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
quando há conversa daquele CLI na pasta, conferida ao selecionar o projeto: as do terminal e do app contam; as de
`claude -p`, do SDK e do `codex exec` não, porque o `--continue` e o `codex resume --last` as ignoram. A seta ao lado abre **Escolher conversa…**: a lista das
conversas do projeto (data, título, mensagens) para retomar qualquer uma, não só a última. Em modo texto:
`launcher conversations --project X` e `launch --resume --conversation ID`. Sem conversa anterior (inclusive numa sessão aberta pelo orquestrador), a retomada vira
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

Sessão controlada abre **minimizada, em segundo plano**: o Windows Terminal ativa e mostra a janela ao abrir uma aba
(não há opção para não ativar nem para abrir minimizado), então o launcher a minimiza assim que ela aparece (e de novo
se o Terminal a reabrir nos primeiros segundos) e devolve o foco do teclado a quem estava na frente. Ela fica na barra
de tarefas até o usuário abri-la. Se a janela das sessões já estava à vista (outra aba aberta antes), ela não é
minimizada: só volta para trás de quem estava na frente. Pela interface, ou com `background=False`, a sessão abre na
frente como antes.

A minimização grava o retângulo normal da janela junto (`SetWindowPlacement`) e confere se ele é são. Antes, o
`ShowWindow` repetido enquanto o Terminal se restaurava deixava a janela em -32000 com 480x103: ela não restaurava nem
maximizava e as abas (Codex ou Claude) nasciam com 48x2. Janela do Terminal já nesse estado é consertada antes de
abrir a aba (`focus.heal_terminal_windows`) e depois dela (`focus.repair_placement`).

```python
from desktop import start_session
start_session(store, projeto, "claude", modelo, controlled=True)                    # minimizada (padrão)
start_session(store, projeto, "claude", modelo, controlled=True, minimized=False)   # atrás, mas na tela
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

### Responder um diálogo aberto: `launcher keys`

Sessão controlada pode parar num diálogo já aberto (pergunta, pedido de permissão). `launcher keys` envia teclas ao
console da sessão sem trazer a janela para a frente: funciona com a janela minimizada, em outro monitor ou sem foco.
Cada janela de sessão fica registrada em `~/.cli-launcher/sessions/<pid>.json` enquanto existir.

```
launcher keys [TECLA ...] (--project PROJETO [--cli claude|codex] | --pid PID) [--screen]
launcher keys --list
```

| Parâmetro | Significado |
| --- | --- |
| `TECLA` | `enter esc up down left right tab shift+tab space backspace ctrl+c home end pageup pagedown delete`, um caractere (`1`, `y`) ou `text:ALGO` (cola ALGO de uma vez, entre os marcadores de colagem, sem tocar na área de transferência; 20 mil caracteres em poucos segundos; a linha de comando do Windows limita o texto a cerca de 30 mil caracteres). Enviadas em ordem; o `enter` que vem depois do `text:` é uma tecla à parte. |
| `--project` | Nome, ID ou caminho do projeto da sessão. `--cli` desempata Claude e Codex. |
| `--pid` | pid da janela da sessão (de `--list`); dispensa `--project`. |
| `--screen` | Imprime o que a sessão mostra agora (antes das teclas, se houver), para decidir o que enviar. |
| `--list` | JSON com as sessões abertas: `pid, project, project_id, cli, title, controlled, startedAt`. |

Saída ao enviar: `{"ok": true, "pid": 123, "title": "Projeto — Claude Code", "sent": ["down", "enter"]}`, código 0.
Falha (tecla desconhecida, sessão inexistente ou ambígua, console inacessível): `Erro: ...` no stderr e código 1;
nada é enviado quando alguma tecla é inválida.

```powershell
launcher keys --project "Meu app" --screen                 # ver o diálogo
launcher keys --project "Meu app" --cli claude down enter  # escolher a 2ª opção
launcher keys --project "Meu app" 1                        # opção numerada
launcher keys --pid 4242 esc                               # dispensar
```

Em Python: `keys.sessions()`, `keys.resolve(ids_do_projeto, cli, pid)`, `keys.send(pid, tokens)`,
`keys.read_screen(pid)`.

**Monitor das sessões:** `launcher preferences --sessions-monitor other` leva as sessões em segundo plano para o
monitor que não tem a janela marcada (ou para um não principal, sem janela marcada), se houver mais de um.
`--sessions-monitor primary` volta ao padrão (ficam onde abrirem).

### Administrador sem UAC

Para projetos que instalam programas, mexem em firewall ou em pastas protegidas, `launcher elevated enable PROJETO...`
cria uma vez a tarefa agendada `AgentLauncher-Elevated` (RunLevel Highest, só sob demanda). Isso exige **um** clique de
UAC, na configuração. Depois, `launcher elevated run -- COMANDO` executa o comando como administrador, na pasta do
projeto, sem prompt, e devolve a saída e o código de saída (`--cmd` usa o cmd, `--timeout` limita o tempo). As sessões
desses projetos recebem no prompt a instrução de usar esse comando para tudo que exigiria administrador. A sessão em si
continua comum: um Claude elevado não recebe mensagens do orquestrador nem teclas de `launcher keys`.

`launcher elevated status` mostra a tarefa e os projetos marcados, `launcher elevated disable PROJETO...` desmarca (outro
clique) e `launcher elevated probe` prova, sem janela, que a tarefa roda com token elevado.

A lista dos marcados e o runner ficam em `%ProgramData%\agent-launcher\elevated`, onde só administrador grava. Risco: o
UAC deixa de proteger esses projetos; qualquer programa do usuário pode disparar a tarefa e mandar um comando de um
projeto marcado. Marque só quem precisa.

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

Para um host com várias chaves, `connectors._http` traz o campo pronto `KEY_NAME` (ponha em
`FIELDS`), `with_key_name(url, options)` (acrescenta `/k/<nome>` à raiz quando há `key_name`) e
`require_key_routing(raiz_com_prefixo, headers)` (exige `key_routing: true` no `/healthz`). Quem embrulha o conector
`litellm` só precisa repassar `key_name` nas opções: ele já aplica o prefixo e a checagem.

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
launcher launch --project "Meu app" --cli copilot --model "vertex_ai.anthropic.claude-sonnet-5-5"
launcher launch --project "Meu app" --cli vscode --model "azure.gpt-5.4-mini"   # ou --cli copilot_app
launcher endpoint                # endpoint local de cada conexão (start, stop, autostart [--off])
launcher cli --install copilot   # Copilot CLI por usuário (npm ou winget)
launcher cli --update all        # atualiza as CLIs instaladas (adia com sessão aberta)
launcher update                # atualiza agora pelo engine-registry (--check só consulta)
launcher topmost on --title "Jarvis"   # janela sempre por cima (off, resume, status)
launcher front "C:\docs\relatorio.pdf" # abrir por cima, a pedido do usuário
launcher keys --project "Meu app" --screen      # ver e responder um diálogo aberto (keys ... down enter)
launcher preferences --sessions-monitor other   # sessões em segundo plano no outro monitor
```

`--dry-run` mostra o plano (comando, overrides, contexto, avisos) com a chave mascarada, sem rede e sem salvar nada.

## Dados e segurança

- `~/.agent-launcher/config.json`: projetos, conexões (sem chaves) e preferências. `AGENT_LAUNCHER_HOME` escolhe
  outra pasta. Gravação atômica; configuração inválida é preservada, nunca sobrescrita. Fica fora da pasta instalada:
  instalar, atualizar ou desinstalar não toca nela.
- `~/.agent-launcher/backups/`: cópia do `config.json` de cada dia com projetos (os 30 dias mais recentes). Se o
  `config.json` sumir ou aparecer com a lista vazia sem que você tenha removido os projetos, o app volta a cópia mais
  recente e avisa. Sem cópia nenhuma, importa os projetos da instalação
  anterior (`%LOCALAPPDATA%\Programs\*\launcher-v2\.data`).
- `~/.agent-launcher/secrets.json`: chaves das conexões, cada uma cifrada com DPAPI para o seu usuário do Windows.
  Uma cópia do arquivo não serve em outra conta ou máquina. Isso não protege contra programas rodando como você.
- `~/.agent-launcher/cache/`: catálogo por conexão (IDs e limites numéricos; nunca chaves nem payloads brutos).
- As consultas não seguem redirecionamentos (a chave não vaza para outro host). Hosts locais e privados não passam
  pelo proxy HTTP do sistema.
- A abertura dos CLIs usa argumentos separados, sem `cmd /c` nem scripts temporários. Wrappers `.cmd` desconhecidos
  são recusados em vez de executados.
- Remover um projeto apaga só o cadastro, nunca a pasta.
- `~/.agent-launcher/endpoints.json`: porta fixa de cada endpoint local. O endpoint escuta só em `127.0.0.1`; qualquer
  programa seu pode usá-lo enquanto ele roda, do mesmo jeito que pode ler as chaves cifradas para o seu usuário.
- `~/.agent-launcher/harnesses/`: adaptadores de destino do usuário (um `.py` com `HARNESS`), no mesmo modelo dos
  conectores.

## Licença

MIT. Veja `LICENSE`.

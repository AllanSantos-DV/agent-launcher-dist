# Agent Launcher

Abre **Claude Code**, **Codex** e **GitHub Copilot CLI** nos seus projetos, e configura o chat do Copilot no
**VS Code** e o **GitHub Copilot app**, usando o provedor de modelos que você escolher: um proxy LiteLLM ou host próprio
(inclusive na rede ou por VPN), roteadores como **9router**, **OmniRoute** e **OpenRouter**, as APIs da **Anthropic** e da
**OpenAI**, ou qualquer host compatível com Anthropic Messages, OpenAI Responses ou Chat Completions.

A lista de modelos vem do próprio host. O launcher lê o que cada host informa (disponibilidade, janela de entrada,
saída máxima, ferramentas, entrada de imagem, áudio e vídeo) e configura cada sessão só com o que foi confirmado. Nada
de catálogo fixo no código. A lista mostra, para cada modelo, se ele aceita imagem (Visão), áudio e vídeo: ícone quando
o host diz que aceita, ícone riscado quando diz que não e traço quando o conector não informou.
O filtro "Entrada" lista só os modelos com visão, áudio ou vídeo; o modelo escolhido que o host diz não aceitar imagem
ganha um aviso; **Ordenar** põe a lista por nome (a ordem de sempre), por contexto (maior primeiro) ou por preço de entrada
(menor primeiro), com o que o host não informou sempre no fim; **Copiar lista** copia a lista filtrada, separada por tabulação; o botão direito num modelo oferece **Copiar ID**, que copia o ID completo dele, e **Testar modelo (1 token)…**, que mostra o custo em tokens e só faz a chamada real de 1 token depois de **Testar** (Cancelar é o padrão; nunca roda sozinho). O que o host informa sobre imagem também
vai para o chat do VS Code (anexar imagem) e para o Codex (modelo declarado só de texto não recebe imagem); `launcher models`
mostra visão, áudio e vídeo só do que o host informou, e `launcher models --sort name|context|price` ordena como o **Ordenar** (o que o
host não informou vai para o fim; `name` é família e versão mais nova primeiro; sem a opção a lista sai na ordem do
catálogo, por ID).
A lista também traz a saída máxima e, na dica e na cópia, o preço por 1M de tokens (traço quando o host não informa). A lista de
conversas para retomar mostra o contexto e o custo estimado de cada conversa (Claude Code e Codex) e o launcher avisa ao
retomar uma conversa que já enche a janela do modelo escolhido.

Interface gráfica Windows (PySide6) e modo texto (biblioteca padrão). Para Windows 10/11.

## Requisitos

- Windows 10 ou 11 (x64) e Node.js 18 ou superior (para o `npx`). Não precisa de Python nem de administrador: o
  instalador prepara um Python só para o Agent Launcher, isolado do resto da máquina.
- O pacote leva só o launcher. Cada ferramenta (Claude Code, Codex, Copilot CLI, VS Code, Copilot app) se instala sob
  demanda, pelo cartão dela na tela, por usuário e sem admin. O Codex precisa de um host com a API **Responses**: ele
  não aceita `/chat/completions`.

## Instalar

```powershell
npx @allansantos-dev/agent-launcher@latest install
```

O instalador copia o app para `%LOCALAPPDATA%\Programs\AgentLauncher`, cria o ambiente com PySide6, instala o Windows
Terminal se faltar e coloca o atalho **Agent Launcher** no Menu Iniciar e na Área de Trabalho. O atalho abre a interface
direto, sem janela de console. Nenhuma ferramenta é instalada sem pedido: quem quiser os CLIs já na instalação usa
`--with-clis` (Claude Code e Codex) ou `--with-clis claude,codex,copilot`. Outras opções: `--no-terminal`,
`--terminal-zip CAMINHO`, `--no-shortcuts`. Quem já usa o launcher não perde nada: as ferramentas instaladas continuam
lá e aparecem prontas.

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
`launcher update` (`--check` só consulta). O Diagnóstico mostra também quanto levou cada fase da última abertura (import, janela, primeira
pintura, catálogo e versões das CLIs), gravada em `~/.agent-launcher/abertura.log`. Quem está na 1.0.4 ou anterior atualiza uma última vez à mão, rodando de
novo o comando de instalação acima.

**O que mudou:** o pacote de cada versão leva a nota dela (`docs/release/RELEASE-X.Y.Z.md`, copiada pelo
`release/build_package.py` para `agent_launcher/ui/novidades.md`; sem a nota o pacote não é montado). Na primeira abertura
da interface depois de atualizar, o launcher mostra essa nota uma vez, numa janela dele (a mesma das instruções), e guarda
a versão vista em `~/.agent-launcher/novidades-vistas.txt`. Uma instalação feita do checkout, sem o pacote, não tem a nota
e não mostra nada.

Desinstalar: `npx @allansantos-dev/agent-launcher@latest install --uninstall`. Remove o app e os atalhos; seus dados
ficam em `~/.agent-launcher`.

### Instalar as ferramentas

Cartão de ferramenta que ainda não está instalada aparece desligado, com o ícone claro e "Não instalado · clique para
instalar". Um clique instala, por usuário e sem admin, uma ferramenta por vez:

| Ferramenta | Caminho de instalação | Progresso |
| --- | --- | --- |
| Claude Code | instalador oficial (`claude.ai/install.ps1`), depois `npm install -g` | indeterminado até concluir |
| Codex | instalador oficial (`chatgpt.com/codex/install.ps1`), depois `npm install -g` | indeterminado até concluir |
| Copilot CLI | `npm install -g @github/copilot`, depois winget | indeterminado até concluir |
| VS Code | instalador de usuário baixado da API de update da Microsoft, com SHA-256 e assinatura conferidos, depois winget | proporcional no download |
| Copilot app | instalador da última release de `github/app`, com SHA-256 e assinatura conferidos, depois winget | proporcional no download |

Durante a instalação a barra do cartão cresce e o ícone vai sendo pintado. Onde o próprio launcher baixa o instalador,
a barra acompanha os bytes; onde o instalador oficial roda em silêncio, não há porcentagem e a barra corre de um lado a
outro até o fim. Falha aparece na faixa de erros com o motivo e fica em `~/.agent-launcher/cli-install.log`. No modo
texto: `launcher cli --install claude|codex|copilot|vscode|copilot_app|all` (`all` = os três CLIs de terminal), e
abrir sessão de uma ferramenta que falta pergunta se instala. Não é preciso fazer login nos CLIs: a chave vem da conexão.

**Atualização das CLIs.** Cada botão de CLI mostra a versão instalada (`--version`) e, quando o registro do npm tem uma mais
nova, a faixa de avisos (informativa, separada da de erros) diz "Codex 0.164.0 disponível (instalada 0.162.1)" e aparece **Atualizar Codex 0.162.1 → 0.164.0**.
Com **Atualizar as CLIs automaticamente** ligado (padrão), o launcher dispara o `update` da própria CLI (ou o
`npm install -g …@<versão publicada> --prefix <prefixo>` na instalação do npm que o launcher abre, recusando o shim
sem pacote do npm ao lado, como os do pnpm e do yarn) e avisa "Codex atualizado: 0.162.1 → 0.164.0". Sessão
aberta não impede: o Windows deixa renomear um .exe em execução, então a sessão segue na versão antiga e a nova vale na próxima
abertura (a faixa diz quantas sessões estão nessa situação); só quando a falha da tentativa cita arquivo preso da própria
instalação (o executável da CLI ou a pasta do pacote dela) a atualização fica adiada, com o nome do processo e o PID quando
há um, e o cartão diz a causa (sessões abertas, arquivo em uso ou outra janela atualizando); tenta de novo na próxima
verificação. A trava de atualização guarda o PID de quem atualiza; se ele morreu, deixa de bloquear a abertura de sessão e a próxima atualização a assume na hora (só essa trava, nunca outro arquivo); trava de PID vivo ou sem PID vale pela idade (30 min).
Qualquer outra falha (cache do npm sem permissão, rede, prefixo alheio) vai para `cli-install.log`. Falha vira
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

Os três de terminal recebem a conexão só no ambiente do processo da sessão: nada é gravado na configuração deles. Todos
se instalam pelo cartão (veja [Instalar as ferramentas](#instalar-as-ferramentas)). Claude Code e Codex têm sessão
controlada pelo orquestrador; o Copilot CLI ainda não.

**VS Code e Copilot app guardam a configuração**, então nunca recebem a chave nem o endereço real do host. Eles
apontam para o **endpoint local** da conexão, um processo oculto do launcher que escuta só em `127.0.0.1` (porta fixa
por conexão, a partir de 47910), descarta qualquer credencial que o destino mande, acrescenta a da conexão e repassa
ao host com streaming. Trocar a chave ou o host no launcher vale na próxima requisição, sem mexer no destino. O endpoint
sobe ao abrir o destino e encerra sozinho após 4 h sem uso; `launcher endpoint autostart --connection NOME` faz ele
subir com o Windows (sem janela, sem admin), para o VS Code e o app acharem a conexão com o launcher fechado. O log
(`~/.agent-launcher/endpoint.log`) tem rota, modelo, status e duração; nunca corpo, prompt ou chave; `launcher endpoint logs [--lines N] [--summary]` mostra o fim dele (com `--summary`, o resumo: pedidos, erros por modelo, failovers e latência). Além de repassar, o endpoint
responde `GET /usage` (o uso do mês do perfil em JSON, só números; `?month=AAAA-MM` escolhe o mês e `?all=1` soma os
perfis; `?limit=1` acrescenta `limit_status`, o gasto frente ao teto mensal do perfil, nulo com o teto desligado) e
`GET /v1/models?capability=vision|audio|video|tools`, que lista só os modelos com a capacidade confirmada pelo
catálogo (valor repetido ou separado por vírgula exige todas; valor desconhecido dá 400). `?search=` filtra pelo trecho
do ID e `?source=` pela fonte inteira, sem diferença de maiúscula, como `launcher models --search/--source`; os filtros
se combinam, e a conexão cujo conector não declara fontes não lista nada com `?source=`. Cada item de `/v1/models`
traz `source`, o nome da fonte exatamente como o conector declarou, só quando o conector da conexão declara fontes e o
modelo tem uma (sem fonte, o campo não vem). `GET /quota` diz se a conexão atendida e a reserva estão marcadas com cota
esgotada (`quota_active`, `quota_mark_at`); sem reserva, `fallback` vem nulo.
`GET /status` devolve o mesmo JSON do `launcher status` (aceita `?month=AAAA-MM`), sem chaves, inclusive o `activity` de cada sessão aberta. Como o `launcher status`, ele lê o perfil e os registros de sessão, e pode descartar o registro de uma sessão cujo processo já terminou.

**Conexão reserva.** Uma conexão pode ter outra como reserva. Quando a conexão atendida responde cota ou crédito
esgotado (HTTP 429 com código de cota, ou 402 de pagamento, a mesma detecção do teste de conexão) antes de entregar
qualquer byte da resposta, o endpoint refaz o mesmo pedido, uma vez só, na reserva (com a chave dela) e marca a primeira
como "cota esgotada". Enquanto a marca vale (10 minutos, contados da hora gravada nela), os pedidos vão direto à reserva,
sem tocar na primeira; passado o prazo, o pedido seguinte tenta a primeira de novo: se ela ainda está sem cota a marca
recomeça o prazo, se respondeu a marca sai. Um teste bem-sucedido da conexão também a tira (o endpoint vê a mudança em poucos segundos). Reserva que também
responde sem cota é marcada do mesmo jeito e devolve o erro dela (com as duas marcadas, tenta-se a primeira como antes);
resposta em streaming já iniciada nunca é refeita; sem reserva, o erro do host é repassado como sempre. O `endpoint.log`
ganha uma linha `failover <de> -> <para> motivo=<429|402>` (ou `motivo=cota marcada` no pedido direto), sem corpo nem chave. A reserva se define em **Gerenciar conexões** (seletor
**Conexão reserva** e **Salvar**) ou por `launcher connections --set-fallback NOME --to RESERVA` (`--clear-fallback NOME`
tira); a própria conexão, um ciclo ou uma conexão inexistente são recusados.

**Sessões de terminal pelo endpoint local (opt-in, por conexão).** Por padrão o Claude Code e o Codex vão direto ao
host da conexão, com a chave no ambiente da sessão, como sempre. Com a preferência ligada (caixa **Abrir sessões de
terminal pelo endpoint local**, desmarcada por padrão, logo abaixo do painel do endpoint em **Gerenciar conexões**, ou `launcher connections --endpoint-sessions
on --connection NOME`; `off` volta ao caminho direto) as duas CLIs passam a falar com o endpoint local da conexão:
o launcher o sobe ao abrir a sessão, aponta o Claude Code para o endereço sem `/v1` e o Codex para o endereço com `/v1`, e
a chave que vai no ambiente é um texto fixo sem valor (o endpoint acrescenta a credencial real). Com isso o `endpoint.log`
e a **Conexão reserva** (failover por cota esgotada) passam a valer também para a sessão. Se o endpoint não sobe, a
abertura é recusada com o motivo, sem cair para o host às escondidas. O resumo do plano (olho) e `launch --dry-run` mostram o
endereço do endpoint local; o Copilot CLI segue direto. O uso da sessão vem das conversas, como antes: o endpoint reconhece
o Claude Code e o Codex pelo `User-Agent` e o Uso do mês não soma essas linhas de novo, então a sessão conta uma vez só (VS Code
e Copilot app continuam somando do endpoint). `launcher connections` marca com "sessões pelo endpoint" a conexão que tem a preferência ligada.

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
- **Achar projetos no histórico…** (menu ⋯ ao lado do projeto) lista, marcadas, as pastas do histórico de conversas do
  Claude Code, do Codex e do Copilot CLI que `launcher projects discover --add` cadastraria; **Cadastrar marcados** grava só as
  que continuam marcadas, com o nome da pasta. Pasta inexistente, de usuário, do sistema, worktree do launcher ou de nome já usado
  fica de fora, e a janela diz quantas.
- **Importar perfil…** (ou duplo clique num arquivo `.alprofile`) cria o perfil pronto, com conexões e conectores;
  **Exportar perfil…** gera esse arquivo. O arquivo nunca leva chave: se a conexão pede uma, o launcher pergunta.
- **Compartilhar sem chave…** (painel Conexões) grava só a conexão selecionada num arquivo `.alprofile` (o mesmo formato do perfil,
  sem chave nem valor de chave); no modo texto, `launcher connections export CONEXAO ARQUIVO` e `launcher connections import ARQUIVO`
  (nome já usado é recusado, a chave fica por preencher).
- **Remover perfil** (ou conexão) oferece tirar também o provider do Copilot app e do VS Code e parar o endpoint local.
- **Uso do mês…**: tokens e custo estimado por destino e modelo, lidos das conversas e do endpoint deste perfil. Com mais de
  um perfil, **Somar todos os perfis** junta o uso de todos numa tabela só.
  É estimativa local; o custo usa o preço que o host informa no catálogo. **Copiar relatório** copia a tabela com os
  valores separados por tabulação: colada numa planilha, cada valor cai na sua coluna. A caixa **Visão** troca a tabela
  por **Por dia** (os últimos 14 dias) ou **Blocos de 5 h** (os últimos 8 dias, com o bloco ativo em negrito e o tempo
  restante), os mesmos números de `launcher usage --days` e `--blocks`; nessas visões o mês e **Somar todos os perfis**
  ficam desligados, e **Copiar relatório** e **Salvar em arquivo…** seguem a visão. O padrão continua **Por modelo**.
- **Limite mensal** (campo no **Uso do mês…**, com **Salvar**): teto de gasto do mês, por perfil, em US$. A faixa de
  avisos da janela (âmbar) avisa ao chegar a 80% e a 100% dele. Nasce desligado (campo vazio, ou `off`) e só avisa:
  nada é bloqueado. O gasto é o custo estimado do mês; modelo sem preço fica fora da conta e o aviso diz **soma parcial**.
  O valor fica no `config.json` do perfil (`preferences.monthly_limit`); sem o campo, o limite está desligado.
  Cada nível também vira um balão na bandeja do Windows, com o mesmo texto da faixa, uma vez por nível em cada mês e mesmo com
  a janela minimizada ou sem foco (o balão não puxa o foco nem abre janela). Mudar o limite no mês recomeça os avisos:
  o nível já avisado vale só para o limite em que foi dado. Nível e limite avisados de cada mês ficam só no
  `selections.json` do perfil.
  Se a conexão da janela tem um conector que declara fontes e oferece a rota `costs` (veja o contrato em
  `connectors/__init__.py`) e uma fonte traz `limit` maior que zero, cada fonte também avisa por balão a 80% e a 100% do
  limite dela, com o nome da fonte, o custo, o limite, a porcentagem e a moeda do conector (`Limite da fonte X do mês perto do
  limite: US$ 80,00 de US$ 100,00 (80%).`; com mais de uma conexão no perfil o texto nomeia também a conexão, `Limite da fonte
  X (conexão Y) do mês perto do limite: ...`). O custo e o limite vêm só do conector: o launcher não calcula nada por fonte. A
  leitura roda em segundo plano, na mesma checagem do teto (a cada 10 minutos) e de novo na hora em que se troca de conexão ou
  de perfil na janela; falha do conector aparece como aviso na faixa âmbar com a mensagem dele, nunca como balão. É
  independente do limite mensal do perfil, que segue igual e pode estar desligado. O nível avisado vale por conexão, fonte e
  limite: duas conexões com a mesma fonte avisam cada uma o seu, e mudar o limite recomeça o aviso só daquele par; os níveis
  ficam no mesmo registro do `selections.json`. Sem a rota, sem a declaração de fontes ou sem `limit` não há balão novo.
- A aba do Terminal usa a cor do perfil (**Cor do perfil…**, também no Padrão). Sem cor escolhida, o Padrão fica com
  a cor de cada CLI (laranja no Claude).
- Modo texto: `launcher profiles [list|add NOME|rename|remove|use|import ARQUIVO|export REF ARQUIVO]`,
  `launcher usage [--month AAAA-MM] [--all] [--tsv] [--by-source [--connection X]]` (`--all` soma os perfis; `--tsv` imprime o mesmo texto do
  **Copiar relatório**; `--by-source` imprime o custo do mês por fonte que o conector da conexão informa pela rota `costs`, na
  moeda dele, e falha se o conector não declara fontes ou não oferece a rota), `launcher usage --limit VALOR|off` (grava ou desliga o limite mensal do perfil; com o limite
  ligado, o JSON de `usage` e o de `status` ganham `limit` com valor, gasto, percentual, nível e se a soma é parcial),
  `launcher usage --days N` (o uso de cada um dos N últimos dias, de 1 a 366, do de hoje para trás: tokens e custo estimado
  por dia, pelos mesmos leitores do Claude Code, Codex e Copilot CLI e pelo endpoint local; dia sem uso não aparece; JSON,
  ou `--tsv` em tabela; não combina com `--month`, `--months`, `--by-source`, `--all` nem `--limit`; guarda o cache em
  `cache/usage-days.json`, sem mexer no do mês),
  `launcher usage --blocks [--active]` (os blocos de 5 horas de uso dos últimos 8 dias, como `ccusage blocks`: cada bloco
  começa na hora cheia, **no fuso local da máquina** (o ccusage usa UTC), da primeira requisição e dura 5 h; uma requisição no
  fim do bloco ou depois dele abre o seguinte. Por bloco: início, fim, se está ativo, o tempo restante do ativo, tokens e custo,
  com a mesma regra de custo e de `cost_complete` do `--days`, pelos leitores do Claude Code, Codex e Copilot CLI e pelo
  endpoint local; JSON, ou `--tsv` em tabela; `--active` deixa só o bloco em andamento. **O custo é o equivalente de API, não o
  da assinatura.** O bloco sai do carimbo de tempo de cada requisição, com resolução de um minuto no primeiro e no último pedido do bloco (Claude: de cada resposta;
  Codex: de cada evento `token_count`; Copilot CLI: de cada `session.usage_record`, e, só nas sessões sem esses registros, do
  encerramento da sessão), e não do reset que a assinatura informa: o `rate_limits` do Codex traz a janela semanal ou nada,
  nunca a de 5 h, e o Claude não grava o limite nas conversas. Uso fora do launcher (Claude Desktop, outra máquina) não
  aparece. Não combina com `--month`, `--months`, `--by-source`, `--all`, `--limit` nem `--days`; guarda o cache em
  `cache/usage-blocks.json`, sem mexer nos outros),
  `launcher status` e `launcher --profile NOME <comando>`.
- Os perfis extras ficam em `~/.agent-launcher/profiles/<id>/`; o orquestrador lista e abre projetos de todos.

## Conexões

Na interface, use **Gerenciar conexões** → **Adicionar**, escolha o tipo e preencha os campos. **Testar e salvar**
consulta a lista de modelos antes de gravar: conexão que não responde não é salva. Pode haver várias conexões; cada
projeto lembra a última conexão, o CLI e o modelo usados.
**Duplicar** abre o formulário preenchido com os campos da conexão selecionada e o nome "(cópia)", para cadastrar
outra conta do mesmo provedor sem redigitar endereço e opções; a chave não é copiada e é pedida de novo.
**Testar todas** testa as conexões uma depois da outra, em segundo plano, pela mesma conferência do **Testar**, e a lista mostra
o resultado de cada uma (ok, chave recusada, host fora do ar, cota esgotada, aguarda login ou o erro); uma falha não interrompe
as outras. **Copiar lista** copia nome, conector, endereço, padrão e estado de cada conexão, separados por tabulação; a chave
nunca vai junto.
Conexão usada pelo VS Code ou pelo Copilot app mostra o endpoint local dela: porta, rodando ou parado, requisições,
**Iniciar/Parar** e **Iniciar com o Windows**; **Copiar endereço** copia o endereço base do endpoint (sem chave: o endpoint acrescenta a credencial) para colar no campo de endereço de outra ferramenta, e fica desabilitado com o endpoint parado; **Copiar configuração** (menu **PowerShell** ou **cmd**, também desabilitado com o endpoint parado) copia as variáveis de ambiente para outra ferramenta usar o endpoint, só dos protocolos que a conexão serve: `ANTHROPIC_BASE_URL` com o endereço sem `/v1` e `ANTHROPIC_API_KEY` (Messages), `OPENAI_BASE_URL` com o endereço terminado em `/v1` e `OPENAI_API_KEY` (Responses e Chat Completions); a chave é um texto fixo sem valor (o endpoint descarta a do cliente e acrescenta a da conexão), e o endereço copiado antes continua igual; os dois botões desabilitados dizem na dica "Inicie o endpoint"; **Iniciar e copiar** (menu **PowerShell** ou **cmd**, sempre habilitado fora de uma ação em andamento) sobe o endpoint se ele estiver parado e copia a mesma configuração, e com o endpoint rodando só copia; se o endpoint não subir, a faixa do painel mostra o motivo e nada é copiado; **Ver log** abre o fim do `endpoint.log` do perfil, sem corpo nem chave, com Atualizar e Copiar, e um **resumo** acima do log (pedidos, erros 4xx e 5xx, failovers feitos e não feitos, latência média e maior, e os cinco modelos mais usados com pedidos, erros e latência), calculado só sobre as linhas mostradas: o log tem teto de tamanho e guarda só o fim, então o resumo não cobre o que já saiu dele; o texto do log e o Copiar não mudam. Abaixo da lista, **Conexão reserva** escolhe a conexão que o endpoint local
usa quando a selecionada fica sem cota; salvar grava na hora, e ao reabrir a tela o seletor mostra o valor gravado.

| Conector | Claude Code | Codex | Modelos | Contexto |
| --- | --- | --- | --- | --- |
| LiteLLM / host próprio | `{endereço}` | `{endereço}/v1` | só `claude-*` / só `gpt-*`, `o*` (ou qualquer um, opcional) | `/v1/model/info` ou `/model/info`; sem elas, `mode`, `max_input_tokens` e `max_output_tokens` do `/v1/models` |
| 9router | `{endereço}` | `{endereço}/v1` | qualquer modelo nos dois | `capabilities` |
| OmniRoute | `{endereço}` | `{endereço}/v1` | qualquer modelo nos dois | `context_length`, `max_input_tokens` |
| OpenRouter | `https://openrouter.ai/api` | `https://openrouter.ai/api/v1` | qualquer modelo com ferramentas | `context_length` |
| Anthropic API | `https://api.anthropic.com` (x-api-key) | — | Claude | `max_input_tokens` |
| OpenAI API | — | `https://api.openai.com/v1` | GPT/o | não informado |
| Compatível com Anthropic | `{endereço}` | — | o que o host listar + extras | se o host informar (grafias abaixo) |
| Compatível com OpenAI | — | `{endereço}` (Responses) | o que o host listar + extras | se o host informar (grafias abaixo) |

Os conectores, exceto o LiteLLM, leem de cada item do `/v1/models` o primeiro valor que vier, nesta ordem:

| Capacidade | Grafias aceitas |
| --- | --- |
| Entrada (contexto) | `max_input_tokens`, `context_length`, `top_provider.context_length`, `capabilities.contextWindow`, `context_window`, `limits.input`, `max_model_len` |
| Saída | `max_output_tokens`, `max_tokens`, `max_completion_tokens`, `top_provider.max_completion_tokens`, `capabilities.maxOutput`, `limits.output` |
| Ferramentas | `capabilities.tools`, `capabilities.tool_calling`, `tools` em `supported_parameters` |
| Visão | `capabilities.vision`; `image` em `architecture.input_modalities` |
| Áudio, vídeo | `audio`, `video` em `architecture.input_modalities` |

Com `architecture.input_modalities` presente, o que não está na lista conta como "não aceita"; sem ela, "não informado".
O LiteLLM lê `supports_vision`, `supports_audio_input` e `supports_video_input` do `model_info`.

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
launcher connections add --name "Meu proxy" --connector litellm --set base_url=http://10.0.0.5
$env:OR_KEY = "sk-or-..."; launcher connections add --name OpenRouter --connector openrouter --secret-env api_key=OR_KEY
launcher connections default "Meu proxy"
launcher connections test OpenRouter
launcher connections test   # sem o nome, testa todas as conexões, uma a uma, e diz o resultado de cada; código 1 se alguma falhou
launcher connections --set-fallback "Meu proxy" --to OpenRouter   # reserva usada pelo endpoint local se a cota acabar
launcher connections --clear-fallback "Meu proxy"
launcher connections --endpoint-sessions on --connection "Meu proxy"   # Claude Code e Codex pelo endpoint local (off volta ao direto)
```

Chaves nunca vão na linha de comando: `--secret-env` lê de uma variável de ambiente, e sem ela o comando pergunta de
forma oculta.

## Sessões

Selecione projeto, conexão, CLI e modelo; **Nova sessão** ou **Retomar última**. **Retomar última** só fica habilitado
quando há conversa daquele CLI na pasta, conferida ao selecionar o projeto: as do terminal e do app contam; as de
`claude -p`, do SDK e do `codex exec` não, porque o `--continue` e o `codex resume --last` as ignoram. No Codex só contam
as conversas abertas pelo launcher: as da conta do usuário no próprio Codex (o app, a assinatura) ficam com ela, e o Codex
também não as mistura com as do launcher. A última conversa do Codex é retomada pelo id, sempre a da pasta do projeto;
se ela estiver aberta em outra janela ou programa, a sessão não abre e o aviso diz onde ela está (uma sessão que já
fechou e deixou o Codex preso é liberada sozinha). A seta ao lado abre **Escolher conversa…**: a lista das
conversas do projeto (data, título, mensagens, contexto e custo estimado) para retomar qualquer uma, não só a última; o campo
acima da lista filtra por título, data ou CLI e **Copiar lista** copia as linhas à vista, separadas por tabulação (`launcher conversations --tsv` imprime esse mesmo texto, com traço no
custo desconhecido). Esse filtro olha só título, data e CLI; a caixa **Só o ramo X** (desligada por padrão; só aparece quando a pasta é um repositório com ramo) esconde as
conversas gravadas em outro ramo, sem mexer no texto do filtro; a caixa **Buscar também no texto das mensagens** (desligada
por padrão) faz o Enter no campo ler as mensagens e somar à lista as conversas que têm o texto, com a mesma busca (e os mesmos tetos,
avisados na janela) do `launcher conversations --grep TEXTO`, que procura dentro das mensagens procura dentro das mensagens
(a sua e a do modelo, sem distinguir maiúsculas) das conversas de Claude Code, Codex e Copilot CLI, e deixa de fora metadados,
resultados de ferramenta, raciocínio e o contexto de ambiente. Só lê os transcripts, nunca grava nada neles; a busca para em 30 s
no total, lê no máximo 64 MiB de cada conversa e pula linha acima de 16 MiB, e o que ficou sem ler aparece como `[AVISO]` no stderr.
Valor vazio é erro. Com `--command`, a posição conta a lista já filtrada pelo `--grep`.
**Copiar comando de retomada** copia, para a conversa selecionada, o comando do cmd que a continua num terminal seu (SSH,
outro shell): vai à pasta do projeto, limpa do shell as variáveis que o launcher tira do ambiente das sessões, define o
ambiente da conexão e roda o CLI com o modelo escolhido na tela; é o mesmo plano do botão **Retomar** e do `--dry-run`, que
segue como está. **Copiar para PowerShell**, ao lado, copia o mesmo comando escrito para o PowerShell (5.1 ou 7). A chave
nunca vai no texto: no lugar dela fica `<sua chave>`, na variável de ambiente que o CLI lê. Vale para Claude Code, Codex e
Copilot CLI. Em modo texto:
`launcher conversations --project X` e `launch --resume --conversation ID`.
**Retomar como cópia** (Claude Code e Codex), na mesma janela, abre a conversa selecionada numa conversa nova copiada dela
(`--fork-session` do Claude Code, `codex fork ID`): a original fica como está, e no Codex a cópia não espera a trava de escritor
da conversa original. Em modo texto, `launch --resume --conversation ID --fork`; o Copilot CLI e a sessão controlada do Codex
recusam com o motivo. O id da conversa nova é do CLI (o launcher não o escolhe), então a cópia não entra no registro de
conversas por perfil: numa pasta compartilhada entre perfis ela não aparece na lista do perfil, e o **Reabrir sessões
recentes…** não a reabre.
**Reabrir sessões recentes…**, no mesmo menu da seta (e em `Ctrl+Shift+H`, que vale mesmo sem conversa no projeto selecionado), serve para depois de reiniciar o Windows: lista as sessões abertas nas últimas 24 horas que não estão abertas agora, uma por projeto e CLI (a mais recente), e reabre as que você marcar, em sequência, cada uma na própria conversa, como aba da mesma janela do Terminal e sem tirar o foco de quem está trabalhando; o resumo diz quantas abriram e, de cada uma que falhou, o motivo, e uma falha não impede as outras. O Claude Code retoma pelo id exato da conversa. O Codex e o Copilot CLI não guardam o id na abertura: vale a conversa que começou até 10 minutos depois dela, e só quando nenhuma outra abertura da pasta disputa essa conversa. Sem essa certeza a linha vem desmarcada e desabilitada, com o motivo ("conversa não identificada"), e nunca retoma outra conversa; o mesmo vale para o Claude Code aberto por **Retomar última** (sem id gravado). Cada sessão reabre com a conexão e o modelo da abertura, sem bypass. **Retomar última** e **Escolher conversa…** não mudam. Sem conversa anterior (inclusive numa sessão aberta pelo orquestrador), a retomada vira
sessão nova, com um aviso na janela, em vez de parar em "No conversation found to continue". Com o Windows Terminal instalado, cada sessão vira
uma aba da janela **agent-launcher** (laranja para Claude Code, verde para Codex), com o título
**Projeto — Claude Code/Codex**; sem ele, cada sessão abre num console próprio. A sessão confere a conexão antes de iniciar. Com a cota da conexão esgotada a sessão abre mesmo assim, e o aviso diz qual é a **Conexão reserva** configurada, se houver: é só o aviso, a sessão não troca de conexão sozinha (para usar a reserva, abra a sessão por ela) e a reserva segue valendo apenas para o endpoint local. Se o modelo escolhido, como o lembrado do projeto, não está mais no catálogo da conexão, o plano traz o aviso "não está no catálogo" (também no olho e no `--dry-run`); a sessão abre como antes, e hosts que aceitam qualquer ID não o mostram. Fechar o launcher não encerra as
sessões. Quando uma sessão termina com o launcher minimizado ou sem foco, o Windows mostra um aviso na bandeja com a mesma
mensagem do rodapé; o ícone da bandeja só existe enquanto há sessão aberta ou aviso a mostrar. A lista de projetos fica do mais
recente para o mais antigo e se reordena a cada abertura, com os projetos fixados no topo; projeto com sessão
aberta agora (por esta ou por outra janela do launcher) aparece com "· aberta" ou "· 2 abertas", e a dica diz quais CLIs.
O botão direito em um projeto abre a pasta, abre no VS Code (só com o VS Code instalado, no seu perfil normal), abre uma **nova sessão em worktree…** (veja **Sessão em worktree**), mostra
**a tela da sessão aberta** (a tela é só leitura e a janela dela não vem para a frente; o botão **Enviar**, ao lado do campo **Teclas a enviar**, responde um diálogo parado da sessão com as mesmas fichas do `launcher keys`, e só envia quando você clica: nunca ao abrir, ao atualizar nem por timer), edita (nome e pasta),
**fixa no topo** (ou desafixa; o fixado aparece com um alfinete no lugar da pasta) ou remove o cadastro. O botão do olho ao lado de **Retomar última** mostra, antes de abrir, o que a nova sessão vai iniciar
(comando, conexão, modelo, janela e preço por 1M do modelo, com traço onde o host não informou, ambiente e avisos, sem a chave); **Copiar comando** e **Copiar para PowerShell**, nessa janela, copiam o comando que abre essa mesma sessão nova num terminal seu (SSH, outro shell), como o comando de retomada: pasta, limpeza do ambiente, variáveis e o CLI com o modelo escolhido, com `<sua chave>` no lugar da chave (só sessões de terminal; **Copiar resumo** segue igual). `Ctrl+K` busca projetos, `Ctrl+F` modelos, `Ctrl+N` adiciona
projeto, `F5` atualiza o catálogo, `Ctrl+R` retoma a última conversa, `Ctrl+H` escolhe a conversa, `Ctrl+Shift+H` reabre sessões recentes, `Ctrl+U` abre o uso do mês e `Ctrl+Enter` abre a Nova sessão.
No menu **…**, **Tema da interface…** escolhe Claro, Escuro ou Seguir o Windows e a cor de destaque (Verde, Azul, Roxo
ou Grafite); a troca vale na hora, inclusive nas janelas abertas, e fica gravada para todos os perfis.

Cada sessão recebe a configuração só no próprio processo; `~/.claude/settings.json` e `~/.codex/config.toml` não são
alterados.

- **Claude Code:** `ANTHROPIC_BASE_URL` do conector. A chave vai em `ANTHROPIC_AUTH_TOKEN` (Bearer) ou em
  `ANTHROPIC_API_KEY` (x-api-key, API da Anthropic), e só no ambiente do processo; a outra variável vai vazia no
  `--settings` inline para que settings do usuário não a reativem. Os aliases Opus/Sonnet/Haiku apontam para a maior
  versão disponível de cada família, preferindo o mesmo provider do modelo escolhido. Família ausente no host usa o
  modelo principal, com aviso, em vez de cair num ID que o host não tem.
- **Codex:** provider `agent_launcher` por `-c` (`base_url`, `env_key=AGENT_LAUNCHER_API_KEY`, `wire_api=responses`).

### Sessão em worktree

Duas sessões abertas na mesma pasta (a lista mostra "· 2 abertas") podem sobrescrever a edição uma da outra. `launcher launch
--worktree [NOME]` abre a sessão num **worktree git** do projeto: uma cópia de trabalho própria, com branch própria, que divide
o histórico do repositório com o projeto. Sem a opção, nada muda: a sessão abre na pasta do projeto, como sempre.

- **Onde fica:** na pasta irmã do projeto, `<pai do projeto>/<nome do projeto>.worktrees/NOME`, na branch nova `launcher/NOME`
  (a partir do HEAD do projeto). Fora da pasta do projeto, o `git status` dele não muda. Sem NOME, o nome é
  `sessao-AAAAMMDD-HHMMSS`. O NOME serve de pasta e de trecho de branch: até 64 letras, números, `.`, `-` ou `_`, começando por
  letra ou número, sem `..`, sem terminar em `.` ou `.lock`, sem nome reservado do Windows (`con`, `nul`...).
- **Reaproveitar:** repetir o mesmo NOME abre a sessão no worktree que o launcher criou, se ele está na branch `launcher/NOME`; o
  trabalho que está lá fica como está, e retomar (`--resume`) acha as conversas dele. O launcher só reaproveita o que ele mesmo
  terminou de criar: depois do `git worktree add` com sucesso grava o marcador `launcher-pronto` na pasta administrativa do
  worktree (`.git/worktrees/<nome>/`, fora do working tree). Worktree sem o marcador (o `git worktree add` passou de 60 s e foi
  encerrado, foi interrompido, ou você o criou à mão) é recusado como incompleto, com o comando para conferir
  (`git -C <worktree> status`) e para recomeçar (`git worktree remove --force <pasta>` e `git branch -D launcher/NOME`, por você).
  Qualquer outra coisa no lugar (pasta que não é worktree deste projeto, worktree em outra branch, branch `launcher/NOME` que já
  existe sem worktree) também é erro com o que fazer, e nada é sobrescrito.
- **Exige:** o git instalado (sem rede), e a pasta do projeto ser a **raiz** de um repositório git com ao menos um commit; fora
  disso o erro diz o que falta. O worktree traz só o que está versionado: `node_modules`, `.env` e outros arquivos ignorados
  não são copiados.
- **Limite conhecido:** a criação roda o git na thread da interface; num repositório muito grande ou preso, a tela pode ficar
  parada por até 60 s (o teto do git) antes de o erro aparecer.
- **Não remove nada:** o launcher nunca apaga worktree nem branch, para não perder trabalho. Para remover, quando a sessão
  acabar: `git worktree remove <pasta do worktree>` e, se quiser, `git branch -d launcher/NOME` (um `git worktree prune` limpa o
  registro de uma pasta que você apagou à mão).
- **Listar:** `launcher worktrees --project "Meu app"` (só leitura; sem `--project` vale o projeto da pasta atual) mostra os
  worktrees que ficam na pasta irmã `<projeto>.worktrees` — nome, branch, pasta, se tem o marcador `launcher-pronto` e se a
  pasta existe —, e `--json` entrega a mesma lista em JSON (`name`, `branch`, `path`, `exists`, `ready`; `branch` é null com a
  cabeça solta e `ready` é null quando a pasta sumiu). O worktree principal e os que você criou fora dessa pasta não entram.
  O comando nunca cria, altera nem remove nada: a remoção é `git worktree remove`, por você.
- **Plano:** `launch --worktree NOME --dry-run` mostra o plano com `project` apontando para o worktree e uma chave nova
  `worktree` (`name`, `branch`, `path`, `project`, `exists`), só presente com a opção; as demais chaves ficam como eram. O
  `--dry-run` não cria nada (`exists` diz se o worktree já existe). `--command` escreve o `cd /d` na pasta do worktree. A janela
  do olho (**O que vai abrir**) ganha a linha `Worktree:` só nesse caso.
- **Registro:** a sessão continua cadastrada no projeto (a marca "· aberta" da lista e o `launcher keys` não mudam); o registro
  de aberturas do perfil guarda a pasta do worktree, e **Reabrir sessões recentes…** (`sessions --reopen`) reabre a sessão no
  worktree dela, com o nome na linha.
- **Quais destinos:** Claude Code, Codex e Copilot CLI, igual para os três: só muda a pasta em que a sessão roda. VS Code e
  GitHub Copilot app abrem numa pasta própria e recusam a opção com uma mensagem.
- **Pela interface:** botão direito no projeto, **Nova sessão em worktree…** (só quando o destino escolhido é de terminal):
  pergunta o NOME (vazio gera um) e abre com o CLI, o modelo e a conexão escolhidos na tela.

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
- Novidades do catálogo: os modelos que entraram e saíram desde a última lista que você viu aparecem na faixa
  informativa da tela e numa linha logo depois do estado em `launcher models` ("Novidades do catálogo: 2 modelos novos
  (…); 1 removido (…)."; com `--json` a linha vai para o stderr). A lista vista fica em `models-<id>.json` e só muda
  quando a tela ou `launcher models` mostra a novidade: se outra rotina (como o `doctor`) atualizar o cache antes, a
  diferença se acumula até alguém mostrá-la, e mostrada uma vez ela não volta. É só informação: sem lista vista (primeira
  leitura ou cache de versão anterior) não há comparação.

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

- **Claude:** antes de abrir, grava `hasTrustDialogAccepted` daquela pasta no `~/.claude.json`, inclusive na chave com
  barra normal (`C:/pasta`) que o Claude Code lê no Windows (troca atômica, refeita se outra sessão gravou no meio).
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
launcher keys [TECLA ...] (--project PROJETO [--cli claude|codex] | --pid PID) [--screen] [--wait TEXTO [--timeout S]]
launcher keys --list
```

| Parâmetro | Significado |
| --- | --- |
| `TECLA` | `enter esc up down left right tab shift+tab space backspace ctrl+c home end pageup pagedown delete`, um caractere (`1`, `y`) ou `text:ALGO` (cola ALGO de uma vez, entre os marcadores de colagem, sem tocar na área de transferência; no Codex, que no Windows lê a entrada como teclas, vai sem os marcadores e cada quebra de linha vira Ctrl+J, nova linha sem enviar; 20 mil caracteres em poucos segundos; a linha de comando do Windows limita o texto a cerca de 30 mil caracteres). Enviadas em ordem; o `enter` que vem depois do `text:` é uma tecla à parte. |
| `--project` | Nome, ID ou caminho do projeto da sessão. `--cli` desempata Claude e Codex. |
| `--pid` | pid da janela da sessão (de `--list`); dispensa `--project`. |
| `--screen` | Imprime o que a sessão mostra agora (antes das teclas, se houver), para decidir o que enviar. |
| `--wait` | Espera `TEXTO` aparecer na tela da sessão (lida a cada segundo) antes de ler a tela e enviar as teclas; espaços e quebras de linha valem como um só, porque o console quebra a linha na largura. Com `--screen`, imprime a tela em que o texto apareceu; sem teclas, confirma com `{"ok": true, "pid": ..., "title": ..., "waited": "TEXTO"}`. |
| `--timeout` | Com `--wait`: prazo da espera em segundos (padrão 30). Sem o texto até o prazo, `Erro: ...` no stderr, código 1 e nenhuma tecla é enviada; sem `--wait`, o comando é o de sempre. |
| `--list` | JSON com as sessões abertas: `pid, project, project_id, cli, title, controlled, startedAt`. |

Saída ao enviar: `{"ok": true, "pid": 123, "title": "Projeto — Claude Code", "sent": ["down", "enter"]}`, código 0 (com
`--wait`, o JSON ganha `"waited": "TEXTO"`).
Falha (tecla desconhecida, sessão inexistente ou ambígua, console inacessível, texto que não apareceu no prazo): `Erro: ...`
no stderr e código 1; nada é enviado quando alguma tecla é inválida.

```powershell
launcher keys --project "Meu app" --screen                 # ver o diálogo
launcher keys --project "Meu app" --cli claude down enter  # escolher a 2ª opção
launcher keys --project "Meu app" 1                        # opção numerada
launcher keys --pid 4242 esc                               # dispensar
launcher keys --project "Meu app" --wait "Do you want to proceed" --timeout 120 1   # esperar o diálogo e então responder
```

Em Python: `keys.sessions()`, `keys.resolve(ids_do_projeto, cli, pid)`, `keys.send(pid, tokens)`,
`keys.read_screen(pid)`, `keys.wait_for_text(pid, texto, prazo)`.

**Estado e última atividade das sessões:** cada sessão de `sessions` no `launcher status` (e no `GET /status`) traz um
objeto novo `activity` com `last_activity` (carimbo ISO, com fuso, da última gravação do transcript da sessão) e `state`:
`working` se essa gravação tem menos de 30 s (`WORKING_WINDOW_S` em `session/activity.py`: os três CLIs gravam a cada
mensagem e a cada ferramenta, e 30 s cobre o intervalo comum entre elas), `idle` se passou disso e `unknown` (com
`last_activity` nulo) quando não há transcript seguro para a sessão. Não diz que o CLI espera por você: é só a idade da
última gravação, então uma geração longa ou uma ferramenta lenta pode aparecer como `idle`. Os campos que já existiam
não mudam, e `launcher keys --list` segue igual. O launcher só lê o transcript (Claude `~/.claude/projects`, Codex
`~/.codex/sessions`, Copilot CLI `~/.copilot/session-state`), nunca o grava, e ignora arquivo ausente ou linha inválida.
Como acha o transcript: cada abertura do registro do perfil (`sessions.jsonl`), na pasta do projeto ou na do worktree dele,
vai para a sessão aberta do mesmo projeto e CLI que registrou mais recentemente antes dela, e a sessão só vale com
exatamente uma abertura (a conversa é procurada na pasta em que a abertura aconteceu); daí vale a mesma regra do
**Retomar**: no Claude o id da conversa que o launcher passou (`--session-id`, ou a retomada por id) e, no Codex e no
Copilot CLI, a primeira conversa que começou depois da abertura, se nenhuma outra abertura da pasta a reivindica. Duas
sessões do mesmo projeto e CLI abertas em instantes diferentes se resolvem cada uma na sua conversa; em qualquer dúvida
(duas abertas juntas sem id, Claude retomado com `--continue` sem id, conversa que ainda não tem transcript, projeto de
outro perfil) as duas ficam `unknown`, nunca um palpite; o mesmo vale para todas as sessões de um projeto e CLI que têm
aberturas na pasta do projeto e em worktree quando alguma delas não pode ser atribuída com certeza. A janela principal não mostra esse estado: resolver o transcript
varre a pasta de conversas do CLI e o laço de 1,5 s da lista de projetos roda na thread da interface.

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

Um conector pode declarar, com o atributo opcional `SEGREGATES_SOURCES = True` no módulo, que segrega os modelos por
**fonte** (o caminho de acesso: serviço compartilhado, Azure, GCP, GitHub...; não é o provider). Cada `ModelInfo` traz então
`source` com o nome exato que o conector quer ver, e um modelo pertence a uma só fonte: o mesmo modelo por duas fontes são
dois ids, escolhidos pelo conector (um id com 2+ fontes é recusado com `ConnectorError`). Só com a declaração o launcher
guarda e mostra a fonte (filtro e coluna **Fonte** na tabela de modelos, `models --source`, campo `source` do `/v1/models`);
sem ela, a fonte é descartada, a tabela fica sem o filtro e sem a coluna e `models --source` recusa. O launcher não cadastra,
junta nem renomeia fontes.

Com a declaração, o conector também pode oferecer a função opcional `costs(options, month)` (`month` é `AAAA-MM`), que devolve
`{"currency": "USD", "sources": [{"source": nome, "cost": número, "limit": número ou None, "tokens_in": inteiro ou None,
"tokens_out": inteiro ou None}]}`: o custo e o limite de cada fonte, como o conector os conhece. O launcher só confere o
formato (fonte repetida, custo não numérico ou negativo viram `ConnectorError`) e mostra o que veio, sem estimar nada:
`launcher usage --by-source` e a seção **Custo por fonte** do diálogo **Uso do mês**, que só existem quando a conexão tem a
rota.
Para um host com várias chaves, `connectors._http` traz o campo pronto `KEY_NAME` (ponha em
`FIELDS`), `with_key_name(url, options)` (acrescenta `/k/<nome>` à raiz quando há `key_name`) e
`require_key_routing(raiz_com_prefixo, headers)` (exige `key_routing: true` no `/healthz`). Quem embrulha o conector
`litellm` só precisa repassar `key_name` nas opções: ele já aplica o prefixo e a checagem.

## Comandos

O modo texto fica em `%LOCALAPPDATA%\Programs\AgentLauncher\launcher.cmd`; nos exemplos, `launcher` é esse
caminho.

```powershell
launcher                       # menu de texto
launcher doctor                # ferramentas + todas as conexões, sem gerar tokens (nenhuma ferramenta é normal)
launcher doctor --json         # as mesmas verificações em JSON: nome, estado (ok, aviso ou erro) e mensagem, mais o campo ok
launcher projects add "C:\meus projetos\app" --name "Meu app"
launcher projects edit "Meu app" --name "App" --path "D:\app"   # só o cadastro muda
launcher projects import "C:\caminho\config.json"   # só nomes e caminhos
launcher projects export "D:\projetos.json"   # o par do import: grava só nomes e pastas (nunca chaves), sem sobrescrever arquivo existente
launcher projects list         # JSON: id, nome, pasta, fixado, último uso, pasta disponível e, no fim, branch (o ramo git lido do .git/HEAD, sem chamar o git, também no arquivo .git de worktree; null na cabeça solta; sem a chave quando a pasta não é repositório git ou o HEAD não diz o ramo, como no armazenamento reftable); o menu de texto mostra "ramo X" ou "cabeça solta" na linha do projeto; e o que o projeto lembra da última abertura (last_cli, last_connection, last_models: {id da conexão: {destino: modelo}}; o `status` traz os mesmos campos)
launcher projects open "Meu app" --folder   # abre a pasta do projeto no Explorer, como o "Abrir pasta" do menu do projeto (--vscode abre no VS Code, no seu perfil normal, só com o VS Code instalado); sem janela de console, e erro claro se o projeto não existe, a pasta sumiu ou o VS Code não está instalado
launcher projects discover [--add] [--cli claude|codex|copilot]   # JSON das pastas de projeto achadas no histórico de conversas do Claude Code, do Codex e do Copilot CLI: nome sugerido (o da pasta), caminho, CLIs de origem, nº de conversas, a última e se já está cadastrada; sem --add é só leitura (nada é gravado); --add cadastra, com o nome da pasta, as que existem, ainda não estão cadastradas e não são raiz de disco, pasta de usuário, pasta do sistema nem worktree do launcher (as mesmas recusas do --add-here; nome repetido de outra pasta fica de fora com o que fazer) e repetir não muda nada. O nome da pasta do histórico do Claude troca todo caractere não alfanumérico por '-' e não volta ao caminho: só vale o cwd que as linhas do transcript trazem e que, codificado, dá exatamente o nome da pasta; sem isso o item sai com "caminho não recuperado" e nunca é cadastrado. Conta as mesmas conversas que o 'conversations' lista (Codex só as interativas deste launcher; Copilot CLI, as 400 sessões mais recentes)
launcher projects restore --date 2026-11-20   # devolve os removidos a partir da cópia diária daquele dia
launcher models --connection OpenRouter --cli claude --search sonnet --refresh
launcher models --source "Azure Foundry" # só modelos dessa fonte, com o nome que o conector declarou; só vale em conector que declara fontes
launcher models --capability vision --capability audio   # só os que confirmam as capacidades (vision, audio, video, tools; a mesma regra do endpoint)
launcher models --json                   # os itens do /v1/models do endpoint local (com a fonte, quando o conector declara)
launcher models --sort context           # a ordem do "Ordenar" da interface: name (família e versão mais nova primeiro), context (maior entrada primeiro) ou price (menor preço primeiro); o que o host não informou vai para o fim, em texto e em --json
launcher models test openai/gpt-6 --connection OpenRouter [--yes]   # chamada real de 1 token, opt-in: sem --yes só mostra o custo em tokens e o preço ("sem preço" quando o host não informou) e não envia nada (código 2); com --yes (ou "s" no console) chama pelo endpoint local, entra no uso do mês e imprime OK com latência e tokens do host, ou o erro real (código 1)
launcher launch --project "Meu app" --connection OpenRouter --cli codex --model "openai/gpt-6" --dry-run
launcher launch --project "Meu app" --resume
launcher session open --project "Meu app" --cli claude [--connection OpenRouter] [--model ID] [--resume [--conversation ID]]   # abre a janela da sessão (aba da janela única do Windows Terminal) pelo mesmo caminho dos botões Nova sessão e Retomar da interface, grava a última escolha do projeto (se a gravação falhar com a janela já aberta, o JSON segue com opened true e o motivo em warnings) e devolve JSON; sem --cli/--connection/--model usa o que o projeto lembra; só Claude Code, Codex e Copilot CLI
launcher session open --project "Meu app" --cli claude --dry-run   # as mesmas validações e o plano em JSON, sem abrir nada e sem gravar
launcher launch --project "Meu app" --worktree feature-a   # a sessão abre no worktree git ../Meu app.worktrees/feature-a (branch launcher/feature-a), criado ou reaproveitado; sem NOME, sessao-AAAAMMDD-HHMMSS; nunca remove nada
launcher worktrees --project "Meu app" [--json]   # só leitura: os worktrees do launcher no projeto (nome, branch, pasta, marcador launcher-pronto, pasta existe); remover é 'git worktree remove', nunca o launcher
launcher conversations --project "Meu app" --cli claude                        # JSON das conversas, mais recente primeiro; no fim de cada uma, branch: o ramo git que o CLI gravou ao abrir a conversa (Codex na session_meta, Claude em gitBranch, Copilot CLI no workspace.yaml; sem a chave quando o CLI não grava ou a conversa abriu fora de um repositório)
cd "C:\caminho\do\projeto"; launcher conversations   # sem --project (em conversations e launch) vale o projeto cadastrado da pasta atual
cd "C:\caminho\do\projeto"; launcher launch --add-here --cli claude --model "ID completo"   # --add-here (em launch e conversations): pasta atual ainda não cadastrada vira projeto, com o nome da pasta, e o comando segue; --project, se vier, tem de ser o nome ou o caminho da pasta atual; recusa raiz de disco, pasta do sistema e pasta de usuário (a do seu perfil, a que a contém e as pastas de outros perfis, Public, Default e All Users; as subpastas delas servem); projeto já cadastrado não é regravado; o cadastro só é gravado depois que o pedido inteiro passou nas validações (conexão, modelo e combinação de opções), então pedido recusado não cadastra nada; com --dry-run cadastra só na memória. Sem a flag o erro "Projeto não cadastrado" é o de sempre
launcher conversations --search janela   # só as conversas com o texto no título, no CLI ou na data (sem distinguir maiúsculas)
launcher conversations --grep "erro de cache"   # só as conversas com o texto em alguma mensagem (a sua ou a do modelo, sem distinguir maiúsculas); combina com --search, --cli, --tsv e --command
launcher conversations --tsv             # em vez do JSON, o texto do "Copiar lista" da janela de conversas: tabulação, traço no custo desconhecido (não combina com --command); ganha a coluna final Ramo (traço no que não se sabe) só quando alguma conversa da lista tem ramo gravado
launcher sessions --reopen --dry-run [--hours 48]   # JSON do que reabriria (padrão 24 h): projeto, CLI, conversa, e o motivo da linha que não abre
launcher sessions --reopen [--hours 48]   # reabre todas as que têm conversa identificada, como abas em segundo plano; código 1 se alguma aba falhar
launcher conversations --project "Meu app" --command 1 --model "vertex_ai.anthropic.claude-opus-5-5"   # comando do cmd que retoma a conversa 1 (ou o ID), sem a chave
launcher launch --project "Meu app" --cli copilot --model "vertex_ai.anthropic.claude-sonnet-5-5"
launcher launch --project "Meu app" --cli vscode --model "azure.gpt-5.4-mini"   # ou --cli copilot_app
launcher endpoint                # endpoint local de cada conexão (start, stop, autostart [--off])
launcher endpoint logs --connection OpenRouter --lines 100   # fim do endpoint.log (padrão 50 linhas; o log é único por perfil, --connection só confere o nome e não filtra)
launcher usage --all --tsv --month 2026-11   # uso do mês de todos os perfis, texto de planilha
launcher usage --months 6   # o total de cada um dos 6 últimos meses (1 a 24), do corrente para trás (JSON; com --all soma os perfis)
launcher usage --days 7   # tokens e custo de cada um dos 7 últimos dias (1 a 366), sem os dias sem uso (JSON; --tsv em tabela)
launcher usage --blocks   # blocos de 5 h de uso dos últimos 8 dias, do mais recente para trás, com o tempo restante do ativo (custo = equivalente de API; hora local; --tsv em tabela, --active só o ativo)
launcher usage --by-source --connection Central --month 2026-11 --tsv   # custo do mês por fonte, como o conector informa (JSON sem --tsv); só em conector com a rota costs
launcher usage --limit 100,50    # teto de gasto do mês deste perfil (US$); off desliga; a janela só avisa a 80% e 100%
launcher status                # um JSON: CLIs, conexões (cota), endpoint, sessões abertas (cada uma com `activity`: `state` working|idle|unknown e `last_activity`, ver abaixo), projetos e uso do mês; sem chaves
launcher completion powershell >> $PROFILE   # Tab completa comandos, opções, CLIs, projetos e conexões no PowerShell
launcher cli --install vscode    # qualquer ferramenta por usuário: claude, codex, copilot, vscode, copilot_app
launcher cli --update all        # atualiza as CLIs instaladas (adia com sessão aberta)
launcher update                # atualiza agora pelo engine-registry (--check só consulta)
launcher topmost on --title "Jarvis"   # janela sempre por cima (off, resume, status)
launcher front "C:\docs\relatorio.pdf" # abrir por cima, a pedido do usuário
launcher keys --project "Meu app" --screen      # ver e responder um diálogo aberto (keys ... down enter)
launcher preferences --sessions-monitor other   # sessões em segundo plano no outro monitor
```

`--dry-run` mostra o plano (comando, overrides, contexto, avisos) com a chave mascarada, sem rede e sem salvar nada. Quando o host informou algo do modelo, o JSON ganha a chave `model_catalog` (`context_window`, `input_per_million` e `output_per_million` em US$ por 1M de tokens, `confirmed`), igual para Claude Code, Codex e Copilot CLI; o que o host não informou vem `null` (nunca zero) e as demais chaves ficam como eram. É só informação: não bloqueia a abertura.

`conversations --command ID_OU_POSIÇÃO` imprime, em vez da lista, as linhas para colar no cmd: `cd /d` na pasta, um
`set "NOME="` por variável que o launcher tira do ambiente das sessões (`ANTHROPIC_*`, `CLAUDE_CODE_OAUTH*`,
`CLAUDE_CODE_USE_*`, `OPENAI_API_KEY` e as demais de `SCRUB`, menos as que a sessão define de novo), um `set` por variável de
ambiente da sessão e o comando com `--resume`/`resume` e o modelo. `--shell powershell` troca a forma: `Set-Location
-LiteralPath '...'`, `Remove-Item Env:NOME -ErrorAction SilentlyContinue`, `$env:NOME='valor'` (aspas simples, apóstrofo
dobrado) e o comando num bloco `& { $PSNativeCommandArgumentPassing = 'Legacy'; & 'programa' 'arg' ... }`: o 5.1 não escapa as
aspas duplas de um argumento nativo e o 7.3+ as escapa de outro jeito, então cada argumento já vai no padrão do Windows e o
7.3+ fica no modo `Legacy` só dentro do bloco, sem mudar o seu shell. O JSON do `--settings` chega com o mesmo valor (pode ganhar
um espaço depois da `{`) e um valor `chave="a b"` do `-c` do Codex com espaço vira `chave="a\u0020b"`, o mesmo valor TOML;
argumento vazio ou com espaço e barra invertida no fim o 5.1 não passa e o comando falha dizendo para usar `--shell cmd`.
`--shell cmd` é o padrão; nele, `&`, `|`, `<`, `>` e `^` fora de aspas ganham `^` (o cmd conta cada `"`, também a `\"` de um
argumento, e um `|` dentro do JSON do `--settings` quebraria o comando). A posição conta a lista como `conversations` a mostra (1 é a mais recente; `--cli` filtra antes de
contar). `--connection` e `--model` escolhem a conexão e o modelo; omitidos, valem os últimos do projeto, como no `launch`. A
chave sai como `<sua chave>`; o valor das variáveis do seu ambiente nunca aparece no texto.

`launch --dry-run --command [--shell cmd|powershell]` imprime, em vez do JSON do plano, o mesmo texto para a sessão **nova** (ou
para a retomada, com `--resume`): é o que **Copiar comando** copia no olho. Só vale para as sessões de terminal (Claude Code,
Codex, Copilot CLI); `--command` sem `--dry-run`, `--shell` sem `--command` e um destino que não é de terminal falham com
mensagem, e o `--dry-run` sem `--command` continua imprimindo o JSON. O Claude Code ganha um `--session-id` novo a cada plano, então o
texto muda a cada vez nesse trecho.

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

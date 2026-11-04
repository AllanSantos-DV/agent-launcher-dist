# Agent Launcher — instruções e comandos

O Agent Launcher abre sessões do Claude Code e do Codex em abas do Windows Terminal, apontadas para a conexão (LLM) que
você escolher. Este texto cobre o uso do dia a dia.

## Instalar

```powershell
npx @allansantos-dev/agent-launcher@latest install
```

O instalador copia o app para `%LOCALAPPDATA%\Programs\AgentLauncher`, instala o que faltar (Windows Terminal, Claude
Code, Codex) e cria o atalho **Agent Launcher** no Menu Iniciar e na Área de Trabalho. Opções úteis: `--no-clis`,
`--no-terminal`, `--terminal-zip CAMINHO`, `--no-shortcuts`.

- **Atualização automática.** A interface confere a versão a cada abertura e, havendo uma nova, se atualiza sozinha e
  reinicia em seguida. As sessões abertas continuam. Para desligar, use **Atualizar automaticamente** no menu de opções.
  Para atualizar na hora: `launcher update` (`--check` só consulta).
- **CLIs.** Cada botão de CLI mostra a versão instalada e oferece **Instalar** ou **Atualizar** quando for o caso. Não
  é preciso fazer login no Claude Code nem no Codex: a chave vem da conexão. Modo texto: `launcher cli`.
- **Windows Terminal.** Se a máquina não tem o Terminal, o instalador o instala sem admin. Se tudo for bloqueado, as
  sessões abrem em consoles separados. `launcher terminal` mostra o que foi encontrado.
- **Desinstalar:** `npx @allansantos-dev/agent-launcher@latest install --uninstall`. Remove o app e os atalhos; seus
  dados ficam em `~/.agent-launcher`.

## Conexões

Uma conexão diz ao launcher para onde mandar as requisições do CLI: um host próprio (LiteLLM, 9router, OmniRoute),
OpenRouter, a API da Anthropic, a API da OpenAI ou qualquer host compatível com Anthropic ou OpenAI.

1. Abra **Gerenciar conexões** e clique em **Adicionar**.
2. Escolha o tipo e preencha os campos.
3. **Testar e salvar** consulta a lista de modelos antes de gravar: conexão que não responde não é salva.

Pode haver várias conexões; cada projeto lembra a última conexão, o CLI e o modelo usados. Na lista, **Testar**
confere uma conexão já salva.

**Cota esgotada.** Quando o provedor avisa que a cota ou o crédito acabou, a conexão é salva com a marca
**cota esgotada** em vez de ser recusada. A marca não impede nada: abrir uma sessão nela só mostra um aviso, porque a
cota pode ter voltado. Um teste bem-sucedido tira a marca. Diagnóstico e a importação de perfil também a mencionam.

No modo texto:

```powershell
launcher connectors
launcher connections add --name "Meu túnel" --connector litellm --set base_url=http://10.0.0.5
launcher connections default "Meu túnel"
launcher connections test "Meu túnel"
```

Chaves nunca vão na linha de comando: `--secret-env VARIÁVEL` lê de uma variável de ambiente, e sem ela o comando
pergunta de forma oculta.

## Sessões

Escolha o projeto, a conexão, o CLI e o modelo; depois **Nova sessão** ou **Retomar última**.

- **Retomar última** só fica habilitado quando há conversa daquele CLI na pasta. A seta ao lado abre **Escolher
  conversa…**, com a lista das conversas do projeto. Sem conversa anterior, a retomada vira sessão nova, com aviso.
- Cada sessão vira uma aba da janela **agent-launcher** (laranja para Claude Code, verde para Codex). Fechar o launcher
  não encerra as sessões.
- A sessão recebe a configuração só no próprio processo: `~/.claude/settings.json` e `~/.codex/config.toml` não são
  alterados.
- **Bypass de permissões** vem desligado. A caixa vale só para a próxima abertura; o padrão fica em **Opções (⋯)**.
  No Codex ele também desliga o sandbox.
- Atalhos: `Ctrl+K` busca projetos, `Ctrl+F` modelos, `Ctrl+N` adiciona projeto e `F5` atualiza o catálogo.
- **Perfis** separam projetos e conexões (por cliente, por exemplo). Escolha o perfil no topo da interface ou
  **Novo perfil…**; **Importar perfil…** e **Exportar perfil…** levam um perfil sem chaves de um lugar para outro.

## Comandos

```powershell
launcher                                  # abre a interface
launcher projects                         # lista os projetos
launcher launch --project NOME --cli claude
launcher launch --project NOME --cli codex --resume
launcher conversations --project NOME     # conversas do projeto
launcher connections test NOME            # testa uma conexão
launcher doctor                           # confere a instalação e as conexões
launcher cli --update all                 # atualiza o Claude Code e o Codex
launcher update --check                   # só consulta a versão nova
launcher profiles list
launcher usage --month 2026-11            # tokens e custo estimados do mês
```

**Copiar comandos**, nesta janela, copia todos os blocos de comando deste texto.

## Dados e privacidade

- Seus dados ficam em `~/.agent-launcher`: projetos, conexões e preferências; perfis extras em
  `~/.agent-launcher/profiles/<id>/`.
- As chaves ficam no arquivo de segredos do perfil, nunca na linha de comando nem em arquivos de projeto. O arquivo
  de perfil exportado nunca leva chave.
- Os registros de problema ficam em `~/.agent-launcher`: `update.log` (atualização), `terminal.log` e `cli-install.log`.
- **Copiar erro** e **Copiar diagnóstico** copiam o texto para você colar num chat de IA ou num pedido de suporte;
  chaves não entram.
- Remover o app não apaga `~/.agent-launcher`.

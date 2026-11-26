# Contribuindo

Repositório privado do Agent Launcher. O estado atual e a estrutura estão em `docs/STATE.md`; testes e release em
`docs/DEVELOPMENT.md`; regras do projeto (paridade Claude, Codex e Copilot, janela, interface, CI) em `CLAUDE.md`.

## Fluxo

1. Trabalhe na `develop` (ou numa branch dela). Nunca crie tag fora da `main`: a tag só nasce na `main`, depois do
   fast-forward da `develop`, com a suíte verde e o gate limpo.
2. Escreva o teste de regressão primeiro e veja-o falhar; só então implemente. Um comportamento por caso, e o nome do teste
   diz a garantia.
3. Rode a suíte pelo runner isolado, nunca `python -m unittest` direto:
   `python tests/sem_janela.py python tests/run.py [modulo]`. Teste não abre janela, não toca áudio e não usa o perfil nem os
   serviços do usuário (pasta temporária própria, porta livre).
4. Uma função, um commit, com README e docs atualizados no mesmo commit. Mensagem em português, sem trailer de coautor.

## Código

- Função nova aditiva não muda o que já existe: sem a opção nova, a saída e os arquivos são os de antes.
- Paridade: o que vale para um CLI vale para Claude, Codex e Copilot CLI, ou a diferença é documentada.
- Reuse antes de criar: procure o que já existe antes de escrever um helper.
- Falhe alto: erro de uso vira `LauncherError` com mensagem acionável, nunca resposta plausível-falsa.
- Comentários mínimos, só o porquê; documentação em docstring. Sem telemetria, sem caminho de perfil real nem e-mail em
  nada que o produto entrega.
- Arquivos de texto do repositório usam a terminação de linha que já têm (CRLF em vários): preserve ao editar.

## Release

Segue `docs/DEVELOPMENT.md`: pacote com `release/build_package.py`, instalação local silenciosa com backup, exercício do
programa instalado, e só então a publicação, uma por ciclo.

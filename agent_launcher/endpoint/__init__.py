"""Endpoint local da conexão: VS Code e Copilot app apontam para 127.0.0.1 e nunca recebem host nem chave.

server.py   o processo que escuta e repassa (biblioteca padrão, streaming sem buffer)
registry.py porta fixa por conexão, estado, subir/parar, autostart no logon
"""

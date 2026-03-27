# Contexto persistente do Quimera

Este arquivo deve ser carregado no inicio de toda execucao do `quimera.py`.
Ele existe para preservar decisoes estaveis sem reinjetar historico bruto no prompt.

## Regras operacionais

- Carregar sempre este arquivo como contexto inicial.
- Nao carregar `quimera_conversa_inicial.txt` nem outros logs brutos como prompt recorrente.
- O historico bruto deve ficar apenas para consulta humana e auditoria.
- Este arquivo deve permanecer curto e curado, com alvo de 80 a 120 linhas no maximo.
- Quando uma decisao ficar obsoleta, substituir a anterior em vez de acumular versoes.

## Decisoes atuais

- O contexto recorrente oficial fica em `quimera_context.md`.
- Transcricoes completas ficam separadas do contexto recorrente.
- O objetivo do contexto persistente e evitar reexplicacao, sem aumentar custo, latencia e ruido.
- Claude e Codex podem usar este arquivo como base para continuar decisoes anteriores.

## Arquivos relacionados

- `quimera.py`: script principal do chat multiagente.
- `quimera_conversa_inicial.txt`: historico salvo da conversa inicial, apenas referencia.
- `logs/`: diretorio recomendado para transcricoes de sessoes futuras, quando existir.

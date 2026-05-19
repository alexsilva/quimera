# quimera.evidence

Este pacote centraliza evidências extraídas da execução de agentes para reutilização entre turnos. A ideia é persistir achados curtos e objetivos sobre raciocínio (`think`), leituras de arquivo e edições, para que próximos prompts consigam recuperar contexto sem depender de toda a saída bruta anterior.

## Propósito

- Compartilhar achados úteis entre turnos de uma mesma sessão.
- Registrar o que já foi inspecionado por agentes: pensamentos resumidos, arquivos lidos e arquivos editados.
- Permitir reuso de contexto por meio de uma camada pequena: parser, store e formatter.

## Fluxo da arquitetura

```text
output bruto do agente
        |
        v
   OutputParser
        |
        v
EvidenceNormalizer
        |
        v
  EvidenceStore
        |
        v
EvidenceFormatter
        |
        v
prompt seguinte
```

### Responsabilidade de cada etapa

- `OutputParser`: recebe o output bruto do agente e aciona os extratores registrados.
- `EvidenceNormalizer`: etapa conceitual que transforma saídas heterogêneas em instâncias coerentes de `Evidence`. Hoje essa normalização está distribuída dentro dos próprios extratores em `quimera/evidence/parser.py`.
- `EvidenceStore`: persiste e consulta evidências por sessão em JSONL.
- `EvidenceFormatter`: resume evidências em uma tag semântica enxuta para reinjeção no prompt seguinte.

## Contrato da interface

### `Evidence`

A dataclass `Evidence` em `quimera/evidence/models.py` é o contrato comum entre parser, store e formatter.

Campos:

- `ts`: timestamp ISO da evidência.
- `path`: caminho do arquivo relacionado, quando existir.
- `digest`: hash associado ao artefato, quando existir.
- `type`: tipo lógico da evidência, como `think_summary`, `file_read` ou `file_edit`.
- `summary`: resumo textual curto, usado principalmente para `think_summary`.
- `agent`: identificador do agente que produziu a evidência.
- `session_id`: sessão à qual a evidência pertence.

### `EvidenceStore`

A API de `EvidenceStore` em `quimera/evidence/store.py` é deliberadamente pequena:

- `append(evidence: Evidence) -> None`: grava uma evidência em JSONL.
- `query(session_id: str, since_ts: str | None = None) -> list[Evidence]`: lê evidências de uma sessão, opcionalmente filtrando por timestamp mínimo.
- `is_valid(path: Path, digest: str) -> bool`: recalcula o SHA-1 do arquivo para validar se a evidência ainda corresponde ao conteúdo atual.
- `close() -> None`: fecha o handle aberto.
- `with EvidenceStore(...) as store`: uso suportado via context manager.

### `EvidenceFormatter`

A API de `EvidenceFormatter` em `quimera/evidence/formatter.py` expõe:

- `format(evidences: list[Evidence], max_chars: int = 2000) -> str`

Comportamento:

- agrupa leituras e edições como "Arquivos visitados";
- resume ferramentas executadas como "Execução recente";
- inclui resumos de pensamento como "Pensamentos";
- deduplica caminhos repetidos;
- respeita o limite de tamanho do texto final;
- envolve a saída em `<evidence_context title="Contexto Compartilhado de Evidências">` com um título explicativo para facilitar leitura e parsing.

## Como adicionar um novo extrator

Novos extratores devem seguir o protocolo `PatternExtractor`, isto é, implementar:

```python
def extract(self, output: str, agent: str, session_id: str) -> list[Evidence]:
    ...
```

Passos mínimos:

1. Criar uma classe com método `extract(...)`.
2. Produzir instâncias de `Evidence` já normalizadas.
3. Registrar a implementação no registry:

```python
from quimera.evidence.parser import PatternRegistry

PatternRegistry.register("meu_extrator", MeuExtrator())
```

O ponto de extensão oficial é `PatternRegistry.register(...)` em `quimera/evidence/parser.py`. A inicialização padrão atual usa `ThinkExtractor`, `FileReadExtractor` e `FileEditExtractor`.

## Fluxo end-to-end: geração até injeção no prompt

```text
1. Agente executa tarefa (ex: codex, claude, gemini)
        |
        v
2. OutputParser extrai padrões do output bruto
   - ThinkExtractor: captura <thinking>...</thinking>
   - FileReadExtractor: detecta "Read file: <caminho>"
   - FileEditExtractor: detecta "Edit <caminho>"
        |
        v
3. EvidenceNormalizer (dentro dos extratores)
   - Transforma em instâncias Evidence normalizadas
   - Gera digest SHA-1 do arquivo (se aplicável)
        |
        v
4. EvidenceStore.append() → JSONL em .quimera/evidence/<session_id>.jsonl
        |
        v
5. Próximo turno: EvidenceStore.query() recupera evidências
        |
        v
6. EvidenceFormatter.format() → tag <evidence_context>
        |
        v
7. PromptBuilder injeta no final do prompt antes do user input
```

### Integração com PromptBuilder

O `PromptBuilder` em `quimera/prompt.py`调用 `_build_evidence_section()`:
1. Lê `session_id` do `shared_state`
2. Consulta `EvidenceStore` para recuperar evidências da sessão
3. Formata via `EvidenceFormatter` e concatena antes da seção de mensagens do usuário

## Por que isso ajuda agentes a evitar releitura redundante

**Problema**: Cada novo agente em uma sessão reinspeciona arquivos já lidos por agentes anteriores, desperdiçando tokens e tempo.

**Solução via evidence pipeline**:
- **Compartilhamento de contexto**: O `evidence_context` informa ao próximo agente exatamente quais arquivos já foram visitados (`### Arquivos visitados`), quais ferramentas já rodaram (`### Execução recente`) e quais raciocínios foram úteis (`### Pensamentos`).
- **Decisão informada**: O agente pode pular re-leitura de arquivos listados ou pedir confirmação antes de editar um arquivo cujo digest mudou.
- **Transparência**: A tag `<evidence_context>` é explícita, permitindo que o agente ignore ou respeite o contexto conforme sua estratégia.

**Exemplo prático**:
- Sessão começa → codex lê `src/main.py`, `src/config.py`
- Próximo turno → gemini recebe prompt com `### Arquivos visitados\n- src/main.py\n- src/config.py`
- Gemini sabe que não precisa reler esses arquivos, focando em onde它们 foram modificados

## Limites do sistema atual

| Limite | Valor | Observação |
|--------|-------|-------------|
| `max_chars` no formatter | 2000 (padrão) | Truncamento severo em sessões longas |
| Tamanho do summary (think) | 200 chars | Truncamento no formatter |
| Persistência | JSONL por sessão | Sem rotação/limpeza automática |
| Validação de digest | SHA-1 por arquivo | Útil para detectar mudanças |
| Extratores registrados | ThinkExtractor, FileReadExtractor, FileEditExtractor | Extensível via PatternRegistry |

**Limitações conhecidas**:
- Não há deduplicação temporal (mesma evidência pode ser registrada múltiplas vezes)
- Sem TTL ou política de limpeza de evidências antigas
- O truncamento pode perder informações críticas em sessões longas
- Não há fallback se o arquivo de store não existir

## Como validar manualmente a tag no preview

### Passo a passo para verificar se `<evidence_context>` aparece

1. **Gere uma sessão com evidências**: Execute um agente que produza output com `<thinking>`, `Read file:` ou `Edit`.

2. **Verifique o JSONL**: O arquivo `.quimera/evidence/<session_id>.jsonl` deve conter registros:
   ```json
   {"ts":"2026-05-18T10:00:00Z","path":"src/main.py","digest":"abc123","type":"file_read","summary":"","agent":"codex","session_id":"sess-123"}
   ```

3. **Inspecione o prompt renderizado**: No preview ou no log do driver, procure:
   ```xml
   <evidence_context title="Contexto Compartilhado de Evidências">
   Estas evidências resumem arquivos já inspecionados e raciocínios úteis desta sessão.

   ### Arquivos visitados
   - src/main.py

   ### Execução recente
   - exec_command: ok | cmd: rg "PromptBuilder" quimera

   ### Pensamentos
   - Preciso verificar o parser.
   </evidence_context>
   ```

4. **Se a tag NÃO aparece**:
   - Verifique se `session_id` está presente no `shared_state`
   - Confirme que `.quimera/` existe no `workspace_tmp_root`
   - Valide que há ao menos uma evidência no JSONL

### Debugging rápido

```bash
# Listar evidências de uma sessão
cat .quimera/evidence/sess-123.jsonl | jq '.'

# Verificar se há resumos de pensamento
grep "think_summary" .quimera/evidence/sess-123.jsonl

# Simular formatação
python -c "
from pathlib import Path
from quimera.evidence import EvidenceStore, EvidenceFormatter
store = EvidenceStore(Path('.quimera'), 'sess-123')
ev = store.query('sess-123')
print(EvidenceFormatter.format(ev))
"
```

## Exemplos: evidência útil vs. evidência ruim

### ✅ Evidência Útil

| Tipo | Exemplo | Por que funciona |
|------|---------|------------------|
| `think_summary` | "Identificado bug na linha 42: off-by-one error em parser" | Resume decisão-chave sem necessidade de reler todo o thinking |
| `file_read` | `src/api/handler.py` | Próximo agente sabe exatamente o que já foi inspecionado |
| `file_edit` | `tests/test_parser.py` | Indica que arquivo foi modificado, útil para validação |

**Boas práticas**:
- Resumo de pensamento deve ser ≤200 chars e conter insight real, não apenas repetição
- Caminho de arquivo deve ser completo e válido
- Digest permite verificar se o arquivo mudou desde a última leitura

### ❌ Evidência Ruim

| Tipo | Exemplo | Problema |
|------|---------|----------|
| `think_summary` | "vou verificar o código" | Sem utilidade, não diz o que descobriu |
| `think_summary` | (texto de 1000+ chars) | Ultrapassa limite, será truncado e perdido |
| `file_read` | `../relative/path/../file.py` | Caminho relativo não resolve corretamente |
| `file_edit` | (sem path, apenas descrição) | Não permite correlacionar com arquivo |

**Armadilhas comuns**:
- Extrator capturando output de depuração (log verbose) como think_summary
- Arquivos binários gerando digest mas sem usefulness
- Muitas evidências de leitura sem contexto de por que foram lidas

## Ainda não integrado

Esta primeira fase entrega o pacote base, mas a integração fim a fim ainda não foi conectada nos seguintes pontos:

- `quimera/spy_output_presenter.py`
- `quimera/runtime/drivers/openai_compat.py`
- `prompt.py`, caso a próxima fase opte por injetar o contexto formatado diretamente na montagem de prompt

Ou seja: parser, store e formatter já existem, mas a coleta automática e a reinjeção no prompt ainda são trabalho da próxima fase.

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

## Exemplo mínimo de uso

```python
from pathlib import Path

from quimera.evidence import EvidenceFormatter, EvidenceStore, PatternRegistry

output = """
<thinking>Preciso verificar o parser.</thinking>
Read file: quimera/evidence/parser.py
Edit quimera/evidence/README.md
"""

PatternRegistry._extractors = {}
PatternRegistry.default()
evidences = PatternRegistry.extract_all(output, agent="codex", session_id="sess-123")

with EvidenceStore(Path(".quimera"), "sess-123") as store:
    for evidence in evidences:
        store.append(evidence)
    recovered = store.query("sess-123")

prompt_context = EvidenceFormatter.format(recovered)
```

## Ainda não integrado

Esta primeira fase entrega o pacote base, mas a integração fim a fim ainda não foi conectada nos seguintes pontos:

- `quimera/spy_output_presenter.py`
- `quimera/runtime/drivers/openai_compat.py`
- `prompt.py`, caso a próxima fase opte por injetar o contexto formatado diretamente na montagem de prompt

Ou seja: parser, store e formatter já existem, mas a coleta automática e a reinjeção no prompt ainda são trabalho da próxima fase.

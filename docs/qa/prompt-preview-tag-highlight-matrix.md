# Matriz de Validação Visual — Preview com Destaque de Tags

## Objetivo
Validar que a melhoria dehighlight de tags no prompt preview está legível, sem ruído e sem regressão nos logs de render.

---

## Critérios de Verificação

### 1. Tela (terminal com Rich)

| Item | Verificar | Sinal de regressão |
|------|-----------|---------------------|
| Tags visuais | Tags `{{variable}}`, `{{#block}}`, `{{/block}}` aparecem com cor/stilo diferente do texto comum | Tags ignoradas, mesma cor do texto |
| Legibilidade | Contraste suficiente entre tags e conteúdo | Texto ilegível por excesso de cor |
| Estrutura | Blocos aninhados visualmente distinguíveis | Quebra de layout, overflow |
| Escape correto | Caracteres especiais (`{`, `}`, `|`) escapados quando não são tags | Render broken, caracteres inválidos |

### 2. Arquivo .ansi

| Item | Verificar | Sinal de regressão |
|------|-----------|---------------------|
| Códigos ANSI presentes | Códigos de cor (ex: `\x1b[36m`, `\x1b[1m`) para tags | Arquivo sem ANSI, tudo plain |
| Sequências balanceadas | Todo `\x1b[...m` tem `\x1b[0m` de reset | ANSI residue, cor vazada |
| Encoding válido | UTF-8 sem caracteres inválidos | `UnicodeDecodeError` ao ler |
| Tamanho razoável | Arquivo não inflateado desnecessariamente por ANSI redundante | Crescimento >50% vs baseline |

### 3. Arquivo .jsonl

| Item | Verificar | Sinal de regressão |
|------|-----------|---------------------|
| Campo `preview` existe | Entrada com `event: "print"` contém campo `preview` | Campo ausente |
| Sem ANSI no preview | `preview` é texto puro (sem escape codes) | ANSI visível no JSON |
| JSON válido | Cada linha é JSON parseável | JSON inválido, linhas quebradas |
| Estrutura intacta | Campos `timestamp`, `event`, `session_id` presentes | Schema quebrado |

---

## Procedimento de Teste

```bash
# 1. Gerar sessão com /prompt
cd /home/alex/PycharmProjects/quimera
python -m Quimera --debug /prompt "sua query aqui"

# 2. Capturar arquivos de render
SESSION_ID=$(ls -t workspace/render-logs/ | head -1 | cut -d'-' -f2 | cut -d'.' -f1)
cat workspace/render-logs/render-${SESSION_ID}.ansi > /tmp/test.ansi
cat workspace/render-logs/render-${SESSION_ID}.jsonl > /tmp/test.jsonl

# 3. Validar ANSI
hexdump -C /tmp/test.ansi | grep -E "1b\[" | head -5  # deve ter códigos
cat /tmp/test.ansi | strip-ansi | wc -c  # menor que original

# 4. Validar JSONL
python -c "import json; [json.loads(l) for l in open('/tmp/test.jsonl')]" && echo "OK"
grep '"preview"' /tmp/test.jsonl | head -1 | strip-ansi  # sem códigos
```

---

## Baseline de Referência

- **Antes**: Prompt preview com `markup_escape()` — todo texto plano, sem highlight
- **Depois**: Preview com Rich markup — tags coloridas/destacadas
- **Esperado**: Melhora visual sem alteração no schema do .jsonl

---

## Riscos Conhecidos

| Risco | Mitigação |
|-------|-----------|
| Escape mal feito quebra Rich | Validar `markup_escape()` aplicado apenas onde necessário |
| ANSI vazando no .jsonl | Garantir `strip_ansi()` antes de logger |
| Performance em logs grandes | Limitar profundidade de highlight (ex: 3 níveis) |
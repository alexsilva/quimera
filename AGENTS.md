# AGENTS

## File References In Chat

When referencing files in chat responses for this repository, use this format:

`/absolute/path/to/file:N`

Optional:
`snippet: short unique text`

Rules:
- Put the absolute path with `:line` on its own line when a specific line is relevant.
- Use the plain absolute path without `:line` only when pointing to the file in general.
- Do not use Markdown links for file references.
- Include a short searchable snippet on the next line only when useful.
- Prefer this format over renderer-specific link syntaxes, since absolute paths with a `:line` suffix are confirmed to open reliably in this workspace.

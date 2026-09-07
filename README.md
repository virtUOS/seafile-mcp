# Seafile-mcp

An MCP server for [Seafile](https://www.seafile.com/), designed for **multi-user
deployments**: one server instance serves many people, and each user supplies their own
Seafile API token as the MCP API key.

## How authentication works

The Seafile token *is* the API key. There is no separate account, no registration, and no
credential storage — "authenticated" simply means "Seafile accepted this token."

Two kinds of Seafile token work, and the server auto-detects which one you gave it:

| Token | How to get it | Scope |
|---|---|---|
| **Library API token** (recommended) | In Seafile: library → Advanced → API Token, choose read-only or read-write | One library. Cannot delete, move, or copy — Seafile itself offers no such endpoints for these tokens. |
| **Account token** | `POST /api2/auth-token/` with your username and password | Your whole account, all libraries. Needed for search, move, copy, and delete. |

Prefer a **read-only library token** unless you specifically need more. It is the single
most effective way to limit what an assistant can do on your behalf.

The server is a stateless pass-through: it stores no credentials, keeps no user database,
and retains nothing between requests. Isolation between users is structural — each request
carries its own token and no per-user state exists to leak.

## Safety

Deployments choose a mode via `SEAFILE_MCP_MODE`; tools outside the active tier are never
registered, so a model cannot call what it cannot see.

- `read_only` — no mutating tools at all.
- `safe_write` — **default.** Write, upload, create directory, rename, move, copy. Every
  one is reversible via Seafile's file history or an inverse operation.
- `full` — additionally exposes delete.

Nothing this server exposes can irreversibly destroy data: deletes go to the library trash,
and overwrites create a new version in file history. The trash purge endpoint is
deliberately not wrapped as a tool.

## Reading documents

(Under Development)

`seafile_read_file` extracts real text from PDF, Word, PowerPoint, and Excel files
instead of returning their raw bytes as mojibake. There is no OCR anywhere — a
scanned/image-only PDF or a textless slide/sheet comes back with an explicit notice
instead of an error or garbage — and formatting, layout, and images are never
preserved.

PDF pages and PowerPoint slides are chunkable: a bare call returns the whole
document/deck, except that one longer than a configurable threshold (15 pages / 20
slides by default) is previewed instead (first 2 pages / first 3 slides), to keep a
single call cheap. Pass `start_page`/`end_page` or `start_slide`/`end_slide`
(1-indexed, inclusive) to read a specific range instead. Excel workbooks are chunked
by sheet rather than by number: a bare call returns every sheet if there aren't many
(5 by default), or just the first if there are — pass `sheet_name` to read one
specific sheet in full. In every case the response's `notice` field states the true
total (pages/slides/sheets) and whether what you got was a preview or everything, so
the two are never ambiguous. Word documents are always extracted in full — Word
stores no page boundaries in the file itself, so there's no natural unit to chunk by
yet.

A legacy pre-2007 binary Office file (`.doc`/`.xls`/`.ppt`) or a password-protected
`.docx`/`.xlsx`/`.pptx` can't be parsed at all and raises a clear error rather than
falling through to mojibake.

System admins can tune or disable each preview threshold independently:

```bash
SEAFILE_MCP_PDF_PREVIEW_THRESHOLD_PAGES=30      # preview only past 30 pages
SEAFILE_MCP_PDF_PREVIEW_THRESHOLD_PAGES=all     # never preview; always extract the whole PDF
SEAFILE_MCP_PPTX_PREVIEW_THRESHOLD_SLIDES=30    # same idea, for PowerPoint slides
SEAFILE_MCP_XLSX_PREVIEW_THRESHOLD_SHEETS=10    # same idea, for Excel sheet counts
```

## Agent skill

[`skills/seafile-mcp-tools`](skills/seafile-mcp-tools) is a
skill documenting the tool-selection pitfalls above plus two things this README
doesn't cover: saving *new* PDF/Word/Excel/PowerPoint files, and editing ones that
already exist in the library. Both require real code — `seafile_write_file` will
silently corrupt any of them, since it only ever sends UTF-8 text. The skill bundles
a tested generator script per format for creating new files; editing an existing one
has no bundled script (the change is different every time), but the skill documents
the download → edit-with-the-real-library → re-upload round trip.

**This MCP server only ever moves file bytes in and out — it never parses, generates,
or edits document structure itself.** All of that work happens in the agent's own code,
not in this server. Concretely: **producing *or* modifying a PDF/Word/Excel/PowerPoint
file requires the agent to have a sandbox or other environment where it can execute
code (a shell and Python), in addition to this MCP server** — without one, these
document formats can be neither produced nor modified through this server at all, no
matter which tools are enabled.

## Running it

See [docs/clients.md](docs/clients.md) for client configuration (LibreChat, Claude Desktop,
Claude Code, generic HTTP, and local stdio) and [docs/deploy.md](docs/deploy.md) for the
Docker + Caddy deployment.

```bash
# local, single user
export SEAFILE_SERVER_URL=https://seafile.example.org
export SEAFILE_API_TOKEN=your_token
uvx seafile-mcp --transport stdio
```

## License

MIT

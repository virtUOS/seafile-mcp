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
| **Account token, confined to one library** | Take an account token and append `:repo_id:` plus the library id — `abc123…:repo_id:8f2c…` | That one library only, through every tool. Search still works, unlike a library token. |

Prefer a **read-only library token** unless you specifically need more. It is the single
most effective way to limit what an assistant can do on your behalf.

### Confining an account token to one library

Search is the main reason to use an account token: Seafile offers no search endpoint for
library tokens, so a library token cannot search at all. If you want search but not
account-wide reach, append `:repo_id:` and the library id to your account token:

```
abc123def456…:repo_id:8f2c9b10-4d3e-4a7f-9c21-5e6a7b8c9d01
```

The server splits this apart, sends only the token to Seafile, and confines **every**
tool to that library. The assistant can then omit `repo_id` entirely, and naming a
different library is refused — so an instruction hidden inside a document it reads
("now search my whole account for…") cannot widen the scope.

One thing to be clear about: this confines the *assistant*, not the *token*. The string
still contains your full account token, so treat it with exactly the care you would treat
the bare token — anyone who obtains it and drops the suffix has your whole account. It is
no worse than supplying the bare token (which is what you would otherwise paste), but it
is not a restricted credential in the way a library token is.

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

`seafile_read_file` extracts real text from PDF, Word, PowerPoint, Excel and
OpenDocument (`.odt`/`.ods`/`.odp`) files instead of returning their raw bytes as
mojibake. There is no OCR anywhere — a
scanned/image-only PDF or a textless slide/sheet comes back with an explicit notice
instead of an error or garbage — and formatting, layout, and embedded images are
never preserved.

**Image files are the exception**: they are not converted to text at all. A
`.png`/`.jpeg`/`.gif`/`.webp`/`.tiff`/`.bmp`/`.heic` is returned as an MCP *image
block*, so a vision-capable model simply looks at it — a photo, a screenshot, a
diagram, a scan — with no sandbox, no filesystem, and no download step. Large images
are downscaled to fit the limits below, and the accompanying text gives the original
dimensions and says when detail may have been lost, so the model can tell "I cannot
read this label" from "this label is blank". Still no OCR: nothing is transcribed,
the picture is simply shown.

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

LibreOffice files are read the same way as their Microsoft counterparts and share
their settings: `.ods` chunks by `sheet_name` like `.xlsx`, `.odp` by
`start_slide`/`end_slide` like `.pptx`, and `.odt` has no page range for the same
reason `.docx` doesn't — neither format stores page boundaries. In `.odt`, headings
are marked with a leading `#` and tables stay inline where they appear.

A legacy pre-2007 binary Office file (`.doc`/`.xls`/`.ppt`), or a password-protected
Office or OpenDocument file, can't be parsed at all and raises a clear error rather
than falling through to mojibake.

### Plain text, encodings, and long files

Text files are read as UTF-8 unless a byte-order mark says otherwise. A BOM is an
explicit declaration inside the file, so honouring it is not encoding *detection* —
this server never guesses a charset. What it does instead is count: any bytes that
could not be decoded become U+FFFD, and `decode_replacements` plus the `notice` say
how many, so a model knows not to quote that passage as the document's wording. This
matters most for a German `.csv` exported by Excel, which is Windows-1252 by default
and whose every umlaut would otherwise be silently destroyed.

Any result cut short by the size limit carries `next_offset`; pass it back as
`offset` to read on. That is the only way to reach the rest of a long Word, `.odt`,
CSV or log file, none of which have a page or sheet to chunk by.

System admins can tune or disable each preview threshold independently:

```bash
SEAFILE_MCP_PDF_PREVIEW_THRESHOLD_PAGES=30      # preview only past 30 pages
SEAFILE_MCP_PDF_PREVIEW_THRESHOLD_PAGES=all     # never preview; always extract the whole PDF
SEAFILE_MCP_PPTX_PREVIEW_THRESHOLD_SLIDES=30    # same idea, for PowerPoint slides
SEAFILE_MCP_XLSX_PREVIEW_THRESHOLD_SHEETS=10    # same idea, for Excel sheet counts
SEAFILE_MCP_MAX_DOWNLOAD_MB=30                  # bytes of one file held in memory
SEAFILE_MCP_MAX_IMAGE_EDGE_PX=1568              # long edge an image is scaled down to
SEAFILE_MCP_MAX_IMAGE_MB=5                      # ceiling on the re-encoded image
SEAFILE_MCP_IMAGE_READS=false                   # don't return images at all
```

`SEAFILE_MCP_MAX_DOWNLOAD_MB` is a safety limit rather than a tuning knob: one
process serves every user, so an unbounded read is an availability problem, not a
slow call. Over the limit, text comes back as a prefix flagged `truncated`, while a
PDF or Office document raises — their structure lives at the end of the file, so a
prefix cannot be parsed at all — and so does an image, since a prefix of a PNG is an
undecodable fragment rather than a smaller picture.

The image limits bound the *model's context*, not just bandwidth: a tool result goes
straight into the context window, and base64 inflates the bytes by a third. Set
`SEAFILE_MCP_IMAGE_READS=false` for a client that cannot render image blocks or a
model without vision — images then come back as a short text description and a
pointer to `seafile_get_download_link`, rather than a block the host silently drops.
See [docs/deploy.md](docs/deploy.md).

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

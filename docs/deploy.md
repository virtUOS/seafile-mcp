# Deploying

One container behind Caddy. Users authenticate with their own Seafile tokens, so there is no
user database to provision and no credential store to protect.

## Quick start

```bash
cp .env.example .env
# edit SEAFILE_SERVER_URL and MCP_DOMAIN
docker compose up --build -d
```

Point `MCP_DOMAIN`'s DNS record at the host first — Caddy obtains a certificate on startup
and needs the name to resolve. Users then connect to `https://$MCP_DOMAIN/mcp`; see
[clients.md](clients.md).

## What the deployment guarantees

- The MCP container publishes **no port**. The only route in is through Caddy over the
  internal compose network.
- Caddy terminates TLS. This matters more than usual here: every request carries a live
  Seafile token in a header.
- Access logs omit headers, and the application redacts token-shaped strings from all log
  output including tracebacks.
- Nothing is persisted. Restarting the container discards all in-memory state; there is no
  volume holding user data or credentials.

## Choosing a mode

`SEAFILE_MCP_MODE` decides which tools exist. Tools outside the tier are never registered,
so a model cannot call what it cannot see.

| Mode | Tools | When |
|---|---|---|
| `read_only` | browsing and reading only | First rollout, or read-only assistants |
| `safe_write` | **default** — write, upload, mkdir, rename, move, copy | Recommended for shared deployments |
| `full` | adds `seafile_delete` | Only when you need it |

The default is `safe_write` for a specific reason: most chat clients (LibreChat included)
cannot show an interactive confirmation prompt, so there is no way for a human to approve a
deletion at the moment it happens. A `confirm` argument supplied by the model is not a
substitute — anything a model can set, text injected into a document can also set. Deleting
from the Seafile web interface keeps a real person in the loop.

If your users connect only with clients that support MCP elicitation (Claude Desktop, for
example), `full` is more defensible, because the server will ask the user directly.

## Restricting who may use the server

Any valid token for your Seafile instance works by default, which mirrors the fact that
those users could already reach Seafile directly. To narrow it:

```bash
SEAFILE_MCP_ALLOWED_EMAIL_DOMAINS=uni-osnabrueck.de,example.org
```

This applies to account tokens, where the server can read the account's email. Library
tokens are anonymous by design and are unaffected.

## PDF preview length

Most libraries are mostly PDFs, so `seafile_read_file` extracts their text layer. A bare
call on a document longer than `SEAFILE_MCP_PDF_PREVIEW_THRESHOLD_PAGES` (default 15)
returns only a short preview, to keep a single call cheap; the model is expected to
follow up with `start_page`/`end_page` for a specific range. Set it to `all` to disable
previewing entirely and always extract the whole document:

```bash
SEAFILE_MCP_PDF_PREVIEW_THRESHOLD_PAGES=all
```

## Download size limit

`SEAFILE_MCP_MAX_DOWNLOAD_MB` (default 30) bounds how much of one file this server
will hold in memory. It is separate from `SEAFILE_MCP_MAX_FILE_READ_KB`, which caps
how much *text* a caller gets back and says nothing about the size of the file
behind it. One process serves every user, so this protects everyone's
session, not just the caller's.

A file over the limit is not simply refused:

- **Text** (Markdown, CSV, logs, source) comes back as its first
  `MAX_DOWNLOAD_MB`, flagged `truncated`, with a notice pointing at
  `seafile_get_download_link` for the rest.
- **PDF, `.docx`/`.xlsx`/`.pptx` and `.odt`/`.ods`/`.odp`** raise instead. Their structure — a PDF's
  cross-reference table, a ZIP's central directory — sits at the *end* of the file,
  so a prefix cannot be opened at all and returning one would only produce a parse
  error later. The format is recognised from the first kilobyte, so an oversized
  PDF is abandoned after one chunk rather than after a full 30 MB.
- **Images** raise for a related reason: a prefix of a PNG or JPEG is not a smaller
  picture, it is an undecodable fragment — and a decoder lenient enough to limp
  through one hands a vision model a half-grey image it will then describe with
  confidence. That is a wrong answer, not a partial one.

## Images

`seafile_read_file` returns an image file as an MCP image block rather than as text,
which is the only way an agent with no sandbox can see one. Two limits bound it:

```bash
SEAFILE_MCP_MAX_IMAGE_EDGE_PX=1568   # long edge the image is scaled down to
SEAFILE_MCP_MAX_IMAGE_MB=5           # ceiling on the re-encoded image
```

These are **context-budget knobs, not bandwidth knobs**. A tool result goes straight
into the model's context window, and base64 inflates the bytes by a third, so one
unbounded image read could consume a whole context. 1568 px is roughly where
mainstream vision models downsample anyway, so raising it usually costs tokens
without buying detail.

Images are re-encoded, never passed through: oriented per EXIF (phone photos are
stored rotated), flattened to RGB, and written as JPEG — or PNG where transparency
would otherwise be composited away, which is what makes dark-text screenshots
unreadable. Only the first frame of an animation is sent, and the notice says so.
An image declaring more than 50 megapixels is refused before it is decoded: the
compressed size of a decompression bomb says nothing about its decoded size, so
`MAX_DOWNLOAD_MB` does not bound it.

If your client cannot render image blocks, or your model has no vision, turn it off:

```bash
SEAFILE_MCP_IMAGE_READS=false
```

An image then comes back as an ordinary text result naming its format and dimensions
and pointing at `seafile_get_download_link` — a worse answer than the picture, but a
much better one than a block the host silently drops, which leaves the model holding
a notice about an image it cannot see and no way to tell that is what happened.

**Verify this against your own client before relying on it.** Image-block support
varies, and this server cannot detect what the host does with what it sends.

## OCR (optional)

Off by default. When you configure an OCR model, scanned documents become readable
in **one call**: `seafile_read_file` transcribes a PDF's scanned pages itself, and an
extra tool, `seafile_ocr_file`, is registered for direct use. Without that
configuration neither exists and nothing else changes.

What it does, and what it deliberately does not do:

- **`seafile_read_file` transcribes only what has no text.** It extracts the text
  layer as always; only pages without one go to the OCR model, and come back marked
  `(OCR)` in the same result. A typed PDF reads byte for byte as before and costs no
  GPU time. One read transcribes at most `SEAFILE_MCP_OCR_MAX_PAGES` pages; the long
  document preview still applies, so a bare read of a 200-page scan transcribes only
  its first 2 pages, and the notice says how to continue.
- **A read never fails because OCR did.** If the OCR host is down or a page times
  out, the typed pages still come back and the failed pages are marked with how to
  retry. An outage — a timeout, a refused or dropped connection, or HTTP
  429/502/503/504 — costs a read at most the requests already in flight, so one
  timeout rather than one per page. For the next 60 s no OCR request is sent at all,
  by either tool or any user: reads return their typed pages at once and mark
  scanned ones "temporarily unavailable". After that, the next scanned page tries
  again, so a restarted OCR server is back in use within a minute.
- **Blank pages are not sent** — the empty backs of a duplex scan, a solid colour. The
  check counts ink pixels that touch other ink, so scanner speckle is ignored while a
  single small word is enough to send a page; it errs towards sending.
  `SEAFILE_MCP_OCR_SKIP_BLANK_PAGES=false` switches it off.
- **`seafile_ocr_file` is for direct use**: an image's text (`seafile_read_file`
  still shows images to a vision model as pictures), a copier "searchable PDF" whose
  hidden text layer is garbled, or a file the agent already knows is a scan. It skips
  pages with a text layer and blank pages too; `force_ocr=true` sends them anyway.
  That flag is set by the model, so it is guidance rather than a lock — the page and
  concurrency limits still apply, and the log line records `forced=True`. The pages
  of a multi-page TIFF are treated as pages.
- **Models without vision** (`SEAFILE_MCP_IMAGE_READS=false`): `seafile_read_file`
  returns an image's transcription instead of "cannot be shown".

### Enabling it

The OCR model runs on a GPU host of its own, behind any **OpenAI-compatible
chat-completions API**. The reference setup is vLLM serving
[LightOnOCR](https://github.com/lightonai/LightOnOCR):

```bash
# On the GPU host
vllm serve lightonai/LightOnOCR-2-1B \
  --limit-mm-per-prompt '{"image": 1}' \
  --mm-processor-cache-gb 0 --no-enable-prefix-caching \
  --port 8010 --api-key "$OCR_API_KEY"
```

LightOnOCR's own README pins the vLLM and `transformers` versions it was tested
with. At the time of writing it warns that one vLLM release made every model loop,
so use their pin rather than the latest release.

Then, in this server's `.env`:

```bash
SEAFILE_MCP_OCR_URL=https://gpu.example.org:8010/v1   # the base URL, ending in /v1
SEAFILE_MCP_OCR_MODEL=lightonai/LightOnOCR-2-1B        # exactly as the server lists it
SEAFILE_MCP_OCR_API_KEY=...                            # vLLM's --api-key, if set
```

Rebuild once, since OCR adds a dependency (`pypdfium2`, which renders PDF pages):
`docker compose up --build -d`. The boot log then shows `ocr=on(<model>)`, followed by
one line from a startup check against `GET /v1/models`:

- `OCR service reachable and serving …`: ready.
- `does not serve model …`: `SEAFILE_MCP_OCR_MODEL` doesn't match what the OCR server
  lists. The log line shows what it does serve.
- `did not answer at startup`: the OCR host is down or still loading the model, which
  takes vLLM minutes. **The tool stays registered anyway** and reports the service
  unavailable until it answers. The check never turns OCR off.

Setting only one of `OCR_URL` and `OCR_MODEL` is a startup error. An admin who set
one clearly meant OCR to be on, and a server that silently ran without it would hide
the mistake.

### Other models

The protocol is generic, so a different model is a settings change:

| Model | Change |
|---|---|
| LightOnOCR-3-0.8B / -3-4B | `SEAFILE_MCP_OCR_MAX_EDGE_PX=2048`, and `SEAFILE_MCP_OCR_EXTRA_BODY={"chat_template_kwargs":{"enable_thinking":false}}`, unless vLLM already sets that with `--default-chat-template-kwargs` |
| A model that needs an instruction (olmOCR, Qwen-VL and other general vision models) | `SEAFILE_MCP_OCR_PROMPT` set to that model's documented OCR prompt. LightOnOCR takes no prompt; an empty prompt is its plain mode |
| Ollama, LM Studio, SGLang | Their OpenAI-compatible base URL (Ollama: `http://host:11434/v1`) and the model name they list |

`SEAFILE_MCP_OCR_EXTRA_BODY` is a JSON object merged into every request, for
model-specific knobs. In `.env`, single-quote it so its inner double quotes survive.
It may not set `model`, `messages`, `stream`, `max_tokens` or `temperature`: this
server owns those, and the last two have their own settings.

### All OCR settings

| Variable | Default | Meaning |
|---|---|---|
| `SEAFILE_MCP_OCR_URL` | unset | Base URL of the OpenAI-compatible API. Unset means OCR is off |
| `SEAFILE_MCP_OCR_MODEL` | unset | Model name, required with the URL |
| `SEAFILE_MCP_OCR_API_KEY` | unset | Sent as `Authorization: Bearer` to the OCR host only |
| `SEAFILE_MCP_OCR_PROMPT` | empty | Text sent with each page image |
| `SEAFILE_MCP_OCR_MAX_EDGE_PX` | 1540 | Long edge a page is rendered or scaled to (max 4096) |
| `SEAFILE_MCP_OCR_MAX_PAGES` | 10 | Pages one call may transcribe, in either tool |
| `SEAFILE_MCP_OCR_CONCURRENCY` | 4 | OCR requests in flight across **all** users of this server |
| `SEAFILE_MCP_OCR_TIMEOUT_S` | 120 | Per page |
| `SEAFILE_MCP_OCR_MAX_TOKENS` | 4096 | Output per page; a page that reaches it is flagged as possibly cut short or looping |
| `SEAFILE_MCP_OCR_TEMPERATURE` | 0.1 | LightOnOCR's recommendation |
| `SEAFILE_MCP_OCR_EXTRA_BODY` | `{}` | JSON object merged into each request |
| `SEAFILE_MCP_OCR_SKIP_BLANK_PAGES` | `true` | Don't send pages that look blank |

### What leaves this host

Page images of library files are sent to the OCR host. Treat it with the same
data-protection standing as Seafile itself: run it on infrastructure covered by the
same rules, not on a public API unless that has been approved. Across hosts, use HTTPS
or a private network, and set vLLM's `--api-key`.

The OCR host never receives a Seafile token. OCR uses an HTTP client of its own,
separate from the Seafile one, and sends only `SEAFILE_MCP_OCR_API_KEY`. The OCR
endpoint is fixed by this configuration, never by a caller, for the same reason the
Seafile URL is.

Nothing is cached, and transcriptions are never logged, only page counts and timings.
Asking for the same page twice transcribes it twice.

### Capacity and client timeouts

Each page is one request. `SEAFILE_MCP_OCR_CONCURRENCY` caps how many are in flight
for the whole server; vLLM batches them on the GPU. A call that transcribes takes
roughly `OCR_MAX_PAGES / OCR_CONCURRENCY` × the per-page latency, and longer while
other users are transcribing too. **This now applies to `seafile_read_file` as well**:
reading a scanned PDF waits for its transcription, while a typed PDF is as fast as
ever. Measure the per-page latency on your GPU. Your MCP client's tool timeout must be
longer than that worst case. In LibreChat that is `timeout` (milliseconds) on the
server's entry in `librechat.yaml`. Both tools send a progress notification after
each page, which some clients use to keep the call alive.

The worst case during an outage is different: one `SEAFILE_MCP_OCR_TIMEOUT_S`
(120 s by default), for the read that discovers it. If your client gives up sooner,
that one read times out on the user's side — typed pages included — even though the
server would have returned them. Either raise the client's timeout above it, or
lower `SEAFILE_MCP_OCR_TIMEOUT_S` to fit, keeping it comfortably above the slowest
page you have measured.

There is no per-user OCR quota. The global concurrency cap keeps the GPU from being
flooded, and waiting requests are served in arrival order, so users interleave rather
than queue behind one long document.

## Text encoding

Plain-text files are decoded as UTF-8 unless a byte-order mark declares otherwise.
There is deliberately **no charset detection** here: a guessing dependency that is
wrong produces confident nonsense, which is worse than visible damage. A BOM is not a
guess — it is the file declaring its own encoding in band, the same kind of evidence
as `%PDF-` — so it is honoured, which is also what rescues UTF-16 files. Left as
UTF-8 those decode without error into NUL-interleaved text that looks fine and is
unusable.

Anything that still fails to decode becomes U+FFFD and is *counted*: the response
carries `decode_replacements` and the notice tells the model not to quote the
affected passage. A Windows-1252 CSV — Excel's default export on a German Windows
machine — is the common case.

One consequence for operators: the whole downloaded buffer is now decoded, not just
the first `MAX_FILE_READ_KB`. Decoded text costs between roughly 1× and 4× the byte
count depending on the script (German is the cheapest case, at about 1 byte per
character), so `MAX_DOWNLOAD_MB` at 30 implies up to ~120 MB of transient string in
the worst case, and ~26 MB for ordinary German text.

## Reading past the size limit

`MAX_FILE_READ_KB` is now a **page size rather than a cliff**, and it counts
characters rather than bytes. A result cut short carries `next_offset`, and passing
it back as `offset` returns the next page. This is what makes a long Word document,
`.odt`, CSV or log file reachable to its end — they have no page, slide or sheet to
chunk by.

Because the server is stateless by design, each page re-downloads and re-extracts the
file. Walking a very large file this way is expensive for the instance, and the
`total_chars` field is what lets a model notice that and use
`seafile_get_download_link` instead.

## Search

Seafile's file search is a **Professional-edition** feature. On startup the server probes
`/api2/search/` without credentials — URL routing happens before authentication, so a
missing feature answers `404` while a present one answers `401`. If it is absent,
`seafile_search` is not registered and the boot log says so. Override with
`SEAFILE_MCP_ENABLE_SEARCH=true|false`.

## Operating notes

- **Logs**: `docker compose logs -f seafile-mcp`. Mutating calls are audit-logged with the
  tool, a token fingerprint, the library, the path, and the outcome — never the token.
- **Rate limiting**: writes are capped per token per minute (`mutation_rate_limit`), which
  bounds a runaway or injected loop.
- **Upgrades**: `docker compose up --build -d`. Certificates persist in the `caddy_data`
  volume; do not delete it casually or you will re-request certificates and may hit issuance
  rate limits.

## LibreChat administrator fallback

Prefer having users add the server themselves ([clients.md](clients.md)). If your LibreChat
version does not expose an API-key field when a user adds a server, declare it once in
`librechat.yaml`. The file holds only a placeholder — each user still enters their own token
through the LibreChat UI, and LibreChat stores it encrypted per user.

```yaml
mcpServers:
  seafile:
    type: streamable-http
    url: 'https://seafile-mcp.example.org/mcp'
    headers:
      Authorization: 'Token {{SEAFILE_TOKEN}}'
    customUserVars:
      SEAFILE_TOKEN:
        title: 'Seafile API Token'
        description: 'Library API Token (library -> Advanced -> API Token), or an account token.'
```

Restart LibreChat afterwards. Users will find the token field via the gear icon next to the
server in the tool picker, or in the MCP Settings panel.

## Verifying a deployment

```bash
# 1. Unauthenticated requests must be refused.
curl -i https://$MCP_DOMAIN/mcp            # expect 401 + WWW-Authenticate

# 2. A real token must be accepted.
curl -i -H "Authorization: Token $YOUR_TOKEN" https://$MCP_DOMAIN/mcp

# 3. Confirm the advertised tool list matches the mode you configured.
docker compose logs seafile-mcp | grep starting
```

Then connect from two different accounts and confirm each sees only its own libraries.

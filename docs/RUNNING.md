# Running Novel Crawler

Verified against the source code and local configuration on September 11, 2026. Examples use PowerShell on Windows from `D:\Project\NovelCrawler`.

## 1. Quick start

```powershell
Set-Location -LiteralPath 'D:\Project\NovelCrawler'
.\start.cmd status
.\start.cmd
```

By default, `start.cmd` runs the entire pipeline: **crawl → translate → edit → export the final EPUB → process Kindle delivery**. Run the same command again to resume saved progress. No cronjob is needed. The process exits when no more work can be done in the current invocation; it does not wait for the next day to resume automatically.

**Local configuration at the time of writing:**

| Setting | Local value | Meaning |
|---|---|---|
| Book | `han-men-bai-jia-zi`, `enabled = false` | The previous book is paused while a new book is being selected. Starting the app does not enable it automatically. |
| Primary agent | `provider = "agy"` | Gemini 3.8 Flash high through Antigravity. |
| Gemini policy refusal | `policy_fallback_provider = "codex_cli"` | Routes that chapter to Terra medium. This is not a fallback for Gemini quota exhaustion. |
| Contextual glossary review | `contextual_glossary_review = true` | May make an additional small review call for ambiguous two-character names. |
| Aggregate call/token budgets | All four daily/total ceilings are `0` | Those ceilings are disabled. Call intervals, timeouts and per-chapter/stage attempt limits still apply. |
| Batch sizes | Crawl `5`, translate `1`, edit `1` | Limits each batch, not the entire invocation. |
| Kindle delivery | `enabled = true`, `transport = "gmail_connector"` | The script prepares an outbox. An external connector must send the email. |

To resume the previous book, set `enabled = true` in that book's `[[books]]` block, save the file and restart. To switch books, configure a **new ID and verified sources** as described in section 9. Do not replace only the title under an existing ID.

## 2. Requirements and setup

- Python **3.11 or later**, with `python` available on PATH.
- Network access for initial dependency installation, source requests and agent calls.
- The selected provider CLI installed and authenticated under the Windows account running the app: `agy` for Antigravity, `codex` for Terra, or `gemini` for the older Gemini CLI adapter.

The launcher creates `.venv` when needed and installs missing dependencies. It uses that environment's Python directly; manual activation is unnecessary. If `config.toml` does not exist, the CLI copies `config.example.toml`. **The example defaults differ from the current local configuration.**

If PowerShell blocks direct `.ps1` execution, use `.\start.cmd`. It starts PowerShell with ExecutionPolicy Bypass for the launcher process without changing the machine-wide policy.

Check CLI paths without requesting a translation:

```powershell
Get-Command agy, codex -ErrorAction SilentlyContinue
.\.venv\Scripts\python.exe -m novel_crawler doctor
```

`doctor` currently searches only for `codex` and `gemini`. It **does not check `agy`**, authentication, quota or model access. The app verifies the Antigravity model when initializing the agent for actual work.

## 3. Launcher options

```powershell
.\start.cmd [mode]
# Alternatively:
.\start.ps1 -Mode status
```

The launcher accepts **one mode**. It does not forward arbitrary Python options such as `--book` or `--config`. Use the Python CLI in section 4 for those options.

| Mode | Example | Behavior |
|---|---|---|
| `run` — default | `.\start.cmd` or `.\start.cmd run` | Runs the manual pipeline for all enabled books, repeating available batches and attempting final export/delivery when eligible. |
| `once` | `.\start.cmd once` | Runs at most one batch per crawl, translate and edit stage, with concurrent workers. Final export/delivery may still occur if the book is complete. |
| `status` | `.\start.cmd status` | Prints the saved progress report, including while the runner is active. Makes no agent calls. |
| `sources` | `.\start.cmd sources` | Fetches and aligns configured source tables of contents for enabled books, saving mappings to the DB. Makes HTTP requests and changes the DB; does not translate chapter text. |
| `stop` | `.\start.cmd stop` | Writes `stop.request`. For the manual runner, this requests early cancellation and may interrupt an active agent call. |
| `test` | `.\start.cmd test` | Runs the offline pytest suite with fake agents and delivery services; installs pytest if missing. |
| `demo` | `.\start.cmd demo` | Builds a two-chapter sample EPUB using `data/demo.db`. Makes no agent calls and sends no email. |
| `background` — legacy | `.\start.cmd background` | Starts the older scheduler in the background. Stores its PID in `data/runner.json` and console output in `data/runner-console.log`. This is not a background version of the new manual pipeline. |

`once` **does not mean exactly one chapter or one agent call**. The crawl batch can contain multiple chapters, translation and editing are separate stages, and repairs or glossary reviews may require additional calls. A stage may process no chapters if its inputs are not ready.

## 4. Python CLI command reference

General syntax:

```powershell
.\.venv\Scripts\python.exe -m novel_crawler [--config CONFIG] COMMAND [OPTIONS]
```

- `--config CONFIG`: TOML path, default `config.toml`. Place it **before** the command name.
- `-h`, `--help`: show general or command-specific help without running the pipeline.
- Omitting `COMMAND` is equivalent to `start`.
- `--book` takes the **configured book ID**, such as `han-men-bai-jia-zi`, rather than its title.
- TOML paths such as `database`, `output_dir` and `glossary` resolve relative to the directory containing the TOML file.

| Command | Command-specific options | Behavior |
|---|---|---|
| `start` | Optional `--once` | Runs the two-worker pipeline; equivalent to launcher `run`/`once`. Does not accept `--book`. |
| `init` | None | Creates/opens the DB, initializes its schema and registers configured books. Does not crawl or translate. |
| `status` | None | Reads `status.txt` beside the DB. Check its timestamp: the report may be stale after the runner stops. |
| `doctor` | None | Checks CLI paths as described in section 2. |
| `sources` | None | Discovers source tables of contents for enabled books. Does not accept `--book`. |
| `run` | `JOB` and required `--book ID` | Runs a single stage for one enabled book. See the JOB table below. |
| `preview` | Required `--book ID` | Exports a local preview from consecutive edited chapters starting at chapter 1. Does not send it to Kindle. |
| `retry` | Required `--book ID --chapter N` | Acknowledges a chapter failure and releases its state for a later run. The command itself does not translate. |
| `manual-import` | Required `--file PATH` | Validates and imports a reviewed manual translation/edit. |
| `tick` — legacy | None | Runs one sequential pass of jobs due in the schedule table. Not equivalent to `start --once`. |
| `schedule` — legacy | None | Repeats `tick`, checking every 5 seconds and using the configured `*_interval_seconds` values. |

Supported `JOB` values:

| JOB | Example command suffix | Scope |
|---|---|---|
| `crawl` | `run crawl --book han-men-bai-jia-zi` | One `crawl_batch`; does not automatically translate. |
| `translate` | `run translate --book han-men-bai-jia-zi` | One `translate_batch` of eligible chapters; does not automatically edit. |
| `edit` | `run edit --book han-men-bai-jia-zi` | One `edit_batch` of eligible drafts. |
| `export` | `run export --book han-men-bai-jia-zi` | Exports the final EPUB when all chapters are edited or validly recorded as missing. |
| `send` | `run send --book han-men-bai-jia-zi` | Validates/exports the final edition, then sends through SMTP or prepares an outbox according to `kindle.transport`. |

Complete command examples:

```powershell
.\.venv\Scripts\python.exe -m novel_crawler --help
.\.venv\Scripts\python.exe -m novel_crawler run --help
.\.venv\Scripts\python.exe -m novel_crawler run edit --book han-men-bai-jia-zi
.\.venv\Scripts\python.exe -m novel_crawler --config config.other.toml start --once
```

**Stop the runner before executing commands that write to the DB**, including `run`, `retry`, `preview`, `sources`, `init` and `manual-import`. The runner holds its DB lock throughout the invocation; `status` remains readable. `start` also performs recovery, imports ready manual submissions, creates backups and reports progress. A standalone `run JOB` does not replace that full lifecycle and currently does not refresh the progress report after its job.

Standalone jobs are not dry runs. `run translate`/`run edit` invoke agents when eligible, and `run send` can send real email when SMTP is enabled. The automatic agent enable/disable switch is checked by `start`; do not treat an explicit `run translate` as harmless merely because `[agent] enabled=false`.

## 5. Workers, progress and stopping

The crawl worker runs concurrently with the language worker. Translation and editing share the language worker's queue; **the app does not run separate translation and editing agents concurrently**.

When the draft backlog reaches `edit_priority_backlog`, editing takes priority. If editing fails or makes no progress, the worker can return to translation. This threshold is not a hard limit on the number of outstanding drafts. Account/quota failures may affect both stages; unfinished edits still block the final EPUB.

Monitor progress in a second PowerShell window:

```powershell
.\start.cmd status
Get-Content -LiteralPath '.\data\pipeline.log' -Tail 60 -Wait
```

Ctrl+C in a window running only `Get-Content -Wait` stops log monitoring, not the pipeline.

### Stop after saving the current response

Use this before changing configuration:

```powershell
Set-Content -LiteralPath '.\data\drain.request' -Value 'Stop after saving the current response.' -Encoding utf8
```

The manual runner checks this request between agent calls, allowing the current response to be processed and saved before stopping. The crawl worker also stops once the runner signals it. There is no `start.cmd drain` option; create the file as shown. Drain checks live in the language worker: when agents are disabled and only crawling is active, use `stop` instead.

### Stop early

```powershell
.\start.cmd stop
```

Alternatively, press Ctrl+C in the runner window. This may interrupt an active agent call. Committed progress remains saved, but an unfinished call may require recovery or a cooldown; its recorded cost is retained. The legacy scheduler checks the stop file between cycles. The stop script's “finish current batch” message does not accurately describe early cancellation in the new manual runner.

### Resume

Wait for the previous runner to exit, then run `.\start.cmd`. A new `start` clears both `stop.request` and `drain.request`, checks saved responses and resumes. Do not delete the DB or reset chapters. With a custom DB, stop/drain/log/status files live **beside that DB**; launcher `stop` reads only the default `config.toml`.

## 6. Configuration reference

Edit `config.toml`, then restart the manual runner to apply changes. `start` reads configuration at startup and **does not reload TOML during an invocation**. Only the legacy scheduler reloads it between cycles.

### Paths

| Key | Meaning |
|---|---|
| `database` | SQLite storage for chapters, context, glossary, calls and delivery state. Default: `data/library.db`. |
| `output_dir` | EPUB directory. Default: `data/epub`. |

### `[agent]`

| Key | Meaning |
|---|---|
| `enabled` | Enables/disables automatic translation and editing in `start`; crawling can continue. |
| `provider` | `agy`, `codex_cli` or `gemini_cli`. |
| `policy_fallback_provider` | Supports only `"codex_cli"` with `agy` primary: routes chapters explicitly refused by Gemini for policy reasons. |
| `fallback_provider` | Supports only `"agy"` with `codex_cli` primary: switches when Terra exhausts quota. |
| `contextual_glossary_review` | Enables a small metered review for some ambiguous two-character names that may be ordinary words. Cached decisions do not replace canonical registry names. |
| `style_revision` | Style version identifier. Changing it archives existing edits and queues them for editing again. Do not change it for routine configuration experiments. A frozen final EPUB cannot be restyled in place. |
| `timeout_seconds` | Per-call processing timeout; currently `600` seconds locally. |
| `max_prompt_bytes` | Maximum UTF-8 prompt size; currently `200000` bytes locally. |
| `max_output_bytes` | Maximum response size; currently `200000` bytes locally. Also applies to manual submission files. |
| `max_output_tokens` | Used for budget reservations. The `gemini_cli` adapter also passes an output limit. This is not a server-side hard token cap for Codex/Antigravity. |
| `quota_cooldown_seconds` | Provider pause after quota exhaustion; currently `3600`. Does not guarantee account quota will recover at that time. |
| `retry_cooldown_seconds` | Wait for transient failures/interrupted calls; currently `60`. |

**Models and efforts are fixed in `novel_crawler/agents.py`. There is no app `--model` option or TOML `model = ...` override:**

| Provider | Model | Effort |
|---|---|---|
| `agy` | `gemini-3.8-flash` | `high` |
| `codex_cli` | `gpt-5.6-terra` | `medium` |
| `gemini_cli` | `gemini-3.8-flash` | `high` |

Gemini primary, Terra on policy refusal — current setup:

```toml
[agent]
provider = "agy"
policy_fallback_provider = "codex_cli"
```

Terra primary, Gemini when Terra exhausts quota:

```toml
[agent]
provider = "codex_cli"
fallback_provider = "agy"
```

These snippets show lines to **edit in the existing `[agent]` block**, not complete minimal configurations. Remove the previous mode's fallback key when switching modes. Do not keep both fallback keys: the loader rejects unsupported combinations. To use only one provider, remove both fallback keys.

Gemini policy fallback requires an explicit content refusal. Quota exhaustion, authentication, network and glossary errors do not trigger it. If Terra translated a refused chapter, Terra also edits that chapter; the next chapter returns to Gemini. In Terra quota fallback mode, Gemini remains active for the rest of that invocation. A new invocation tries Terra again when recovery conditions permit.

### `[limits]`

| Key | Meaning | Current local value |
|---|---|---:|
| `calls_per_day` | Calls per UTC day, shared across books and stages. | `0` |
| `calls_total` | Calls across the entire DB. | `0` |
| `reserved_tokens_per_day` | Reserved tokens per UTC day. | `0` |
| `reserved_tokens_total` | Reserved tokens across the entire DB. | `0` |
| `min_call_interval_seconds` | Minimum interval between calls. | `30` |
| `max_attempts_per_chapter_stage` | Content attempts scoped to chapter/stage, provider/model, style revision and source. | `2` |

Only the **four aggregate daily/total ceilings** accept `0` to disable them. Other limits must be positive. Failed/interrupted calls remain in the cost history; retry does not refund tokens. Reserved tokens in status are conservative application accounting, not the account's remaining quota. `config.example.toml` still defaults to 48 calls/day, 4000 total calls, 2 million tokens/day and 180 million total tokens.

### `[jobs]`

| Key | Meaning |
|---|---|
| `crawl_batch` | Maximum chapters per crawl batch; currently `5`. |
| `translate_batch` | Maximum chapters per translation batch; currently `1`. |
| `edit_batch` | Maximum chapters per editing batch; currently `1`. |
| `edit_priority_backlog` | Draft backlog at which editing takes priority; currently `8`. |
| `max_unedited_chapters` | Legacy name accepted as the priority threshold when `edit_priority_backlog` is absent. Not a hard ceiling. |
| `crawl_interval_seconds`, `translate_interval_seconds`, `edit_interval_seconds` | Job intervals for legacy `tick`/`schedule`; do not control the `start` loop. |
| `export_interval_seconds`, `send_interval_seconds` | Also used only by the legacy scheduler. |

`start` has no `--until-chapter 20`, `--chapters 1-20`, `--max-calls` or `--no-send` option. Disable automatic delivery with `[kindle] enabled = false`. Use `once` and small batches for a small trial. There is no built-in command to “edit exactly 20 chapters, then send them to Kindle.”

### `[crawler]`

| Key | Meaning |
|---|---|
| `request_interval_seconds` | Interval between HTTP requests; example default: `3` seconds. |
| `timeout_seconds` | Request timeout; example default: `20` seconds. |
| `max_response_bytes` | HTTP response size limit; example default: `2000000` bytes. |
| `min_chapter_characters`, `max_chapter_characters` | Length thresholds for detecting unusually short/long chapters; example defaults: `500`–`30000`. |
| `user_agent` | User-Agent string; empty uses HTTPX's default identification. |

Each source/candidate has persisted attempt counts and retry times. “All sources exhausted” means **all configured sources**, not an unlimited Internet search. A chapter still unavailable after that process becomes `missing`, receives a note at its position in the EPUB, and later chapters continue. An agent policy refusal is not treated as a missing source chapter.

## 7. Preview EPUBs and quality review

Export consecutive edited chapters starting at chapter 1:

```powershell
.\.venv\Scripts\python.exe -m novel_crawler preview --book han-men-bai-jia-zi
```

Default output: `data/epub/han-men-bai-jia-zi.preview.epub`. The preview stops at the first chapter that is not `edited`, including a `missing` chapter. It does not collect isolated edited chapters beyond that point. Its title includes the chapter range, and its book identifier differs from the final edition. The command sends no email and does not freeze a final artifact. There is currently no option to choose the preview's ending chapter.

Export selected chapters for reading with optional side-by-side Chinese source text:

```powershell
.\.venv\Scripts\python.exe scripts/review_samples.py --book han-men-bai-jia-zi --chapters 1 8 12
```

| `review_samples.py` option | Default | Meaning |
|---|---|---|
| `--database` | `data/library.db` | Database to read. |
| `--book` | `han-men-bai-jia-zi` | Book ID. |
| `--chapters` | `1 8 12` | One or more chapter numbers separated by spaces. Each chapter must have an edited result. |
| `--output` | `data/review/ban-dich-mau.html` | HTML output path; also creates a Markdown file with the same base name. |

Review samples do not enter the delivery outbox automatically. EPUB output uses reflowable layout, serif text, justified paragraphs, a 1.2em first-line indent, 1.4 line height and 2% horizontal margins. Font size is not locked, so readers can adjust it on Kindle. CSS lives in `novel_crawler/epub.py`; font and margin controls are not exposed as TOML/CLI options.

## 8. Kindle delivery

### Final edition requirements

`completed = true` means **the source novel is complete**, not that the app has finished translating it. The app freezes a final edition only when canonical chapter numbers cover 1 through `expected_chapters`, and every chapter is edited or validly marked missing. Outstanding translation/editing failures prevent final publication.

`start` checks these conditions automatically. To run the steps separately:

```powershell
.\.venv\Scripts\python.exe -m novel_crawler run export --book han-men-bai-jia-zi
.\.venv\Scripts\python.exe -m novel_crawler run send --book han-men-bai-jia-zi
```

`run send` does not accept an arbitrary EPUB path and **does not send previews**.

### `[kindle]` settings

| Key | Meaning |
|---|---|
| `enabled` | Enables delivery processing. Setting it to `false` still allows EPUB export. |
| `transport` | `smtp` sends directly from the script; `gmail_connector` prepares an outbox. |
| `sender` | Sender email; the configured account is `trquoctoann@gmail.com`. |
| `recipient` | Kindle address: `trquoctoann_DSsROK@kindle.com`. |
| `smtp_host`, `smtp_port` | SMTP server and STARTTLS port; default port: `587`. |
| `username_env`, `password_env` | Names of environment variables containing SMTP credentials; defaults: `KINDLE_SMTP_USERNAME`, `KINDLE_SMTP_PASSWORD`. |
| `max_message_bytes` | Maximum complete MIME message size, including base64 overhead; example default: `25000000` bytes. |

With the local **`gmail_connector`** configuration, the script writes an EPUB snapshot and JSON manifest to `data/outbox/` and records delivery as `ready`. An external Gmail connector must upload and send the file. Being signed into Gmail in the app does not authenticate Python's SMTP client.

For an independent script run that sends to Kindle automatically, configure `smtp`, its host/port and both credential environment variables for the script's process. The code does not load `.env` automatically. Do not store passwords in TOML or documentation. The sender must be allowed by the recipient's Kindle account.

Delivery states: `ready` means waiting for the connector; `sending` means transmission has started; `submitted` means the service accepted the email, not that the physical Kindle received it; `unknown` means the outcome is uncertain. A recorded delivery is not resent automatically, even if `send` is run again. Check the previous delivery before considering any change to its record.

Previously requested manual Kindle snapshots are recorded separately in `data/kindle-snapshots/`. There is no launcher command to automatically create/send a snapshot every 20 chapters.

## 9. Configuring a new book and its sources

Each book is a `[[books]]` block; subsequent `[[books.sources]]` blocks belong to that book. Use `config.example.toml` as the structural template and supply verified book/source information.

| Key in `[[books]]` | Meaning |
|---|---|
| `id` | Unique ID using lowercase letters, digits and hyphens, up to 80 characters. Use a new ID for a different book or edition. |
| `title`, `original_title`, `author` | Vietnamese title, original title and author. |
| `enabled` | Whether to include the book in the automatic pipeline. |
| `completed` | Whether the source novel is complete. Must be `true` for final publication. |
| `expected_chapters` | Positive canonical chapter count. Do not shorten it just to send a sample. |
| `source_url` | Canonical table-of-contents URL used for book identity and chapter numbering. |
| `toc_selector` | CSS selector for chapter links in the canonical table of contents. |
| `content_selector` | Content CSS selector for a single-source configuration. |
| `encoding` | Canonical source encoding, such as `utf-8` or `gb18030`. |
| `glossary` | This book's glossary JSON file. Start with `{}` if no terms have been approved. |
| `rendering_aliases` | Optional file of verified equivalent renderings. Do not copy another book's aliases into the new book. |
| `contextual_terms` | Optional list of Chinese phrases that should remain contextual vocabulary guidance. |

| Key in `[[books.sources]]` | Meaning |
|---|---|
| `id` | Source ID unique within the book. |
| `source_url`, `toc_selector`, `encoding` | This source's table-of-contents URL, chapter link selector and encoding. |
| `adapter` | `css` by default, or `piaotia` for the existing specialized adapter. |
| `content_selector` | Required for the `css` adapter. |
| `title_selector` | Optional chapter title selector for verification. |

Secondary sources are aligned using titles, sequence and neighboring-chapter evidence. Equal chapter numbers on two sites do not prove equal content. When `books.sources` is absent, the loader creates a `primary` source from the book's single-source settings.

To switch books: stop the runner → disable the old book → add a new ID, configuration and glossary JSON → run `sources` and inspect the mappings → run `once` for an initial batch → review sample output → run the full pipeline. Source discovery requires the new book to be enabled, but does not translate. The DB rejects changes to the canonical URL or chapter count under an existing registered ID to prevent mixing editions.

## 10. Troubleshooting and manual submissions

| Symptom | What to check or do |
|---|---|
| The app exits quickly without processing chapters | Check `books.enabled`, the status timestamp, eligible chapters and cooldowns. An invocation ending does not mean the whole book is finished. |
| `crawled` grows but `translated` does not | Inspect logs and each worker's progress. The worker may be editing, waiting for an agent, out of quota or blocked in translation. |
| A state counter decreases | Counters show chapters **currently in that state**, not lifetime totals. Chapters move from crawled to translated to edited. |
| Glossary/editing error | Inspect the actual finding and chapter. The app repairs/reviews within its limits; remaining defects need their cause addressed before retry. Do not delete canonical names merely to pass validation. |
| Quota exhausted | Crawling may continue while inference pauses. Fallback depends on the mode in section 6. Restart after quota recovers rather than running a continuous retry loop. |
| Crash/timeout | Restart with `start` to recover saved responses first. An uncertain call may need to wait for timeout plus cooldown. |
| Cannot acquire the DB lock | Another runner is using the DB. Wait for it to stop. A `.lock` file's presence alone does not prove a process is active. |
| Delivery is `ready`, but no book appears on Kindle | Gmail outbox mode is waiting for the connector to send the email. |

After fixing the cause and stopping the runner:

```powershell
.\.venv\Scripts\python.exe -m novel_crawler retry --book han-men-bai-jia-zi --chapter 150
.\start.cmd
```

Replace `150` with the chapter that actually failed. `retry` acknowledges that chapter's failed/reserved calls and releases its state for another run. It does not change text, translate immediately, erase cost history, raise aggregate budgets or clear provider cooldowns. **It also does not reset per-chapter/stage attempt counts.** If `max_attempts_per_chapter_stage` is exhausted, acknowledgement alone will not permit another call; inspect the cause and decide how to address the limit/configuration. Do not use `retry` to reopen `missing` chapters or bypass a manual submission requirement.

### When both Gemini and Terra refuse for policy reasons

1. Open the `submission.json` path reported by `status`, under `data/manual-review/<book>/chapter-.../`.
2. Read `source.json` and `README.txt` in that directory. Fill `result.title`, `result.continuity` and every item in `result.paragraphs` with the complete, manually edited translation.
3. Preserve metadata, hashes and paragraph IDs. Do not insert translation commentary into the reader-facing prose.
4. After reviewing the complete chapter, set the JSON fields `ready` and `reviewed` to the boolean value `true`.
5. Run `start` again to validate and import automatically. Alternatively, while the runner is stopped:

```powershell
.\.venv\Scripts\python.exe -m novel_crawler manual-import --file 'D:\actual-submission-directory\submission.json'
```

Replace the example path with the app-generated file. Do not invent a new request at an arbitrary path. The submission is the final human-translated and human-edited chapter; it is not sent back to an agent. Structure, paragraph coverage, names, numbers and source revision are still validated. Partially completed submissions survive restarts. Later translations may wait for this chapter's context; the pipeline does not invent or summarize content to skip it.

## 11. Files and technical utilities

| Default path | Contents |
|---|---|
| `data/library.db` | Main data and call/delivery history. SQLite WAL may also use `-wal` and `-shm` files. |
| `data/status.txt`, `data/status.json` | Progress snapshots and manual submission requests. |
| `data/pipeline.log` | Rotating pipeline log. |
| `data/missing-chapters.json` | Missing source chapters and their reasons. |
| `data/backups/` | SQLite backups by run date. |
| `data/agent-workspaces/agy/` | Antigravity request metadata and `events.jsonl` for recovery/investigation. |
| `data/manual-review/` | Manual translation/editing requests. |
| `data/epub/`, `data/outbox/` | EPUBs and the Gmail outbox. |
| `data/review/` | HTML/Markdown reading samples. |

Do not back up only `library.db` while the runner is writing to WAL. Use the app's SQLite backups or stop cleanly before copying data. Resuming does not require deleting data.

These utilities support testing/integration rather than normal pipeline operation:

- `python -m scripts.demo`: equivalent to launcher `demo`, with no additional options.
- `python -m scripts.background [stop|worker]`: legacy background launcher; `worker` is an internal entry point. Not needed for manual operation.
- `.\.venv\Scripts\python.exe -m scripts.smoke_agents --provider codex_cli` or `--provider gemini_cli`: makes **real agent calls** and records costs in the main DB. Does not support `agy` or replace the offline suite. Do not run alongside the pipeline.
- `.\.venv\Scripts\python.exe -m scripts.gmail_outbox ACTION`: connector bridge; **does not send email itself**. Actions are `status` (inspect delivery records), `claim --book ID` (mark transmission started), `chunk --book ID --offset N` (read a base64 EPUB chunk; byte offset must be divisible by 6144 and defaults to 0), `ack --book ID --message-id ID` (record the actual sent message ID), and `unknown --book ID` (record an uncertain outcome). Use `ack` only with an actual message ID after the connector sends the email. These actions can change duplicate-delivery protection state. Invoke them as modules with `-m` from the project directory so they can import `novel_crawler` even without an editable installation.

Use `--help` and `.\start.cmd test` to check the installation without agent calls or email delivery. Offline tests verify software behavior, not account quota/model access or arrival on a physical Kindle.

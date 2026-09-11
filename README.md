# Novel Crawler

**Operating guide:** [Running the app: commands, options and configuration](docs/RUNNING.md). The guide is verified against the current code, distinguishes local settings from example defaults, and covers stopping/resuming, model fallback, manual submissions and the Gmail outbox.

A local Python 3.11+ application: **crawl Chinese novels → translate into Vietnamese → edit against the source → export EPUB → deliver to Kindle**. SQLite WAL stores source text, translations, context, terminology, chapter provenance, agent calls and delivery state.

## Run everything with one script

Double-click **start.cmd**, or run `./start.cmd` in PowerShell. The script prepares .venv, dependencies and config.toml when missing, then runs in the foreground. No cronjob or heartbeat is required. Each invocation processes available work within its budget and exits when only quota, cooldowns or issues requiring attention remain. Run the same script again to resume. Books must have `enabled = true`. With `kindle.transport = "gmail_connector"`, the script only prepares an outbox; a connector must send the email.

| Command | Purpose |
|---|---|
| `start.cmd` | Run the full pipeline in the current terminal |
| `start.cmd once` | Run one crawl batch and one batch for each translation/editing stage |
| `start.cmd stop` | Request cancellation of the running process |
| `start.cmd status` | Show progress, missing chapters, budgets and delivery status |
| `start.cmd test` | Run offline tests with fake agents and SMTP |
| `start.cmd demo` | Generate a sample EPUB from original sample text without delivery |
| `start.cmd sources` | Align configured source tables of contents |

`python -m novel_crawler` also runs the full manual pipeline. Ctrl+C preserves committed progress; a Windows Job Object terminates the agent process tree. The older schedule/background commands remain available only when explicitly requested and are not used by the default launcher.

## Two independent workers

- The crawl worker processes consecutive batches, fills gaps from alternative sources and saves each chapter immediately. It can continue when agents encounter errors or exhaust quota.
- The language worker translates and edits eligible chapters. Those stages share one agent queue to control costs; crawling and language work run concurrently.
- Each thread has its own SQLite connection. An OS lock is held throughout the invocation to prevent competing sessions from calling agents or sending books. Network requests and inference do not hold database transactions.
- `data/status.txt` and `data/status.json` record worker progress; `data/missing-chapters.json` records missing chapters; `data/pipeline.log` rotates. SQLite backups are created at most once per run date in `data/backups/`.

## Sources and missing chapters

The example configuration is **Hàn Môn Bại Gia Tử / 寒门败家子**, by **寻北仪**, with 1,597 chapters. It uses [Fanqie](https://fanqienovel.com/page/7143311019090119711) as the canonical table of contents and [Piaotian](https://www.piaotia.com/html/15/15289/) as the preferred content source. In the project's source survey, Piaotian was readable over HTTP but lacked chapter 1,264; Fanqie had a complete table of contents but many full-text pages were restricted or used font encoding. A free source containing verified full text for the entire book has not been established. See the [operating guide](docs/RUNNING.md) for source configuration and discovery.

Canonical chapter IDs come from the main table of contents. Alternative sources are matched by normalized title, order and neighboring chapters. For example, canonical chapter 2 may use chapter 102 from another source when the evidence matches. Equal numbers alone are insufficient; the crawler does not guess across split chapters or different editions. It checks page titles, length, duplicated/merged content, corrupted characters and access-restriction pages.

Each table of contents or content candidate allows at most three HTTP attempts. Attempt counts and `retry_at` are saved **before** requests. A chapter remains `source_waiting` while a source has a pending retry. Once **all sources in books.sources** are exhausted and no valid candidates or pending attempts remain, the runner marks the chapter `missing`, records the reason in JSON and inserts a Vietnamese missing-chapter note at the correct EPUB position. Later translation uses the nearest available context and known gaps. No replacement content is invented.

“All sources exhausted” means all configured and verified sources, not an unlimited Internet search. Adding sources before finalization can reopen `source_review`. A finalized missing chapter does not automatically reopen after later chapters have been translated; subsequent additions require a new edition to avoid context inconsistencies and duplicate delivery.

## Agents and quality

The example default is **codex_cli → gpt-5.6-terra, medium**, using Codex CLI account authentication. Other supported adapters are **agy → gemini-3.8-flash, high** through Antigravity and **gemini_cli → gemini-3.8-flash, high** when the account has access. The current local configuration uses Gemini with Terra policy fallback; see the operating guide for supported routing combinations. The app does not purchase quota or silently switch to paid API credentials. Offline tests do not prove account access to a model.

Prompts include translation style rules, glossary entries, continuity and the preceding chapter's ending. New names proposed by a draft are stored as candidates and become protected only after editing confirms source/translation evidence. Ordinary vocabulary remains contextual guidance. The registry supplies previously approved names when they appear again. Editing receives the source, draft and findings. Results must preserve all paragraph IDs in order. Unresolved translation defects involving identities, numbers, untranslated Chinese, markup or blocking editing issues prevent publication; source-only notes remain advisory.

## Limits and recovery

The registry separates `entity` entries (names, places, organizations and named techniques) from `preferred` vocabulary (ordinary words, titles and descriptions). Entity checks protect the actual name while allowing source-backed geographic grammar. Preferred entries guide contextual translation. Matching selects the longest Chinese mention and respects Vietnamese word boundaries, without locking a one-character surname inside another word. `rendering-aliases.json` stores verified alternatives, normalized only when the corresponding Chinese term occurs in the paragraph. Raw responses remain available for audit.

| Example configuration limit | Value |
|---|---:|
| Calls per UTC day / entire DB | 48 / 4,000 |
| Reserved tokens per day / total | 2 million / 180 million |
| Minimum call interval | 30 seconds |
| Content attempts per chapter/stage | 2 |
| Timeout / prompt / output | 180 seconds / 50 KB / 200 KB |
| Reserved output tokens | 12,000 |
| Provider quota cooldown | 3,600 seconds |

Calls and reservations are not refunded after errors, timeouts, crashes or restarts. Quota failures do not consume content attempts for the chapter, but **still count toward aggregate daily/total call and token budgets**. The app does not raise ceilings automatically. Local settings may explicitly disable aggregate ceilings as described below.

The CLI adapters do not provide a universal server-side hard token cap. The app limits call launches and prompt/output bytes, reserves tokens conservatively and records actual usage when available. Internal CLI reasoning/retries may consume additional tokens. Oversized prompts stop before inference; excessively long chapters require splitting/review rather than unlimited resubmission.

1. A valid response saved before the chapter commit is validated and committed on restart without another agent call.
2. Interrupted calls without a response and transient network failures can reopen after cooldown, subject to attempt limits. Crash recovery waits for the reserved call's timeout plus cooldown before retrying.
3. Quota exhaustion creates a provider-specific pause while crawling can continue. Configured quota fallback may switch providers; otherwise restart after quota recovery/cooldown. The manual runner does not wait in a loop for the next day.
4. Structurally complete drafts with glossary, numeric or untranslated-Chinese findings are saved with paragraph-level warnings for editing against the source. Final edits must pass validation. Glossary findings are aggregated for bounded repair or contextual review. Invalid structures, missing paragraphs, unresolved defects and authentication failures retain their responses/errors for inspection. `python -m novel_crawler retry --book BOOK_ID --chapter N` acknowledges a failure after its cause is addressed; it does not erase costs, raise budgets or reset per-chapter/stage attempt counts.

## EPUB and Kindle Paperwhite 5

Output is reflowable EPUB 3 with NFC-normalized Vietnamese, a table-of-contents entry/anchor per chapter and grouped XHTML suitable for long books. Defaults are serif text, justified paragraphs, a 1.2em first-line indent, 1.4 line height and relative 2% horizontal margins. Font size is not fixed and fonts are not embedded, allowing Kindle adjustments. Chapters start on a new page; missing-chapter notes are italic and unindented.

The final edition is created/delivered only when all canonical chapters are **edited or explicitly marked missing under the rules above**. Its hash is stored and checked; subsequent runs reuse the same final file. Incremental editions are not sent automatically. The explicit preview utility creates a local sample outside the final delivery flow.

For automatic delivery in the same invocation, configure `kindle.transport="smtp"`, `enabled=true`, `smtp_host`, `sender`, a personal `...@kindle.com` recipient, and the `KINDLE_SMTP_USERNAME` / `KINDLE_SMTP_PASSWORD` environment variables. Add the sender to the Amazon account's approved email list. Keep passwords out of the repository. Without SMTP setup, crawling, translation and EPUB export can proceed, but SMTP delivery cannot.

SMTP connection/authentication failures can be retried on a later run. Once transmission starts, `sending`/`unknown` states prevent automatic resending after a crash or uncertain result; check the Kindle library before resolving them. `submitted` means SMTP accepted the message, not that it arrived on the device. `gmail_connector` only creates a `ready` outbox and **does not send email from the script**.

Offline tests cover the simulated full pipeline, different source chapter numbering, missing chapters, concurrent workers, quota/resume, persistent terminology, process-tree termination, duplicate-delivery protection and a 1,597-chapter EPUB. ZIP/XML, manifest, spine and anchors are checked. These checks do not replace testing on a physical Kindle or human assessment of translation quality.

## Implementation notes

Glossary matching follows the longest Chinese mention, Unicode-normalized Vietnamese word boundaries and source-backed geographic grammar (for example 九山郡城 → quận thành Cửu Sơn). It never fuzzy-matches a changed name. Single-character dynamic entries are vocabulary guidance, not substring locks. Draft entity proposals remain `candidate` until an editor approves them. An optional conflicting metadata proposal is discarded without overwriting the registry or rejecting correct prose.

Each agent call stores its glossary snapshot; later names cannot retroactively change its recovery checks. Final glossary findings are collected across all paragraphs and sent back with the saved editor response for a bounded automatic repair. All calls and usage remain accounted; the per-chapter attempt cap still prevents endless retries. A remaining real identity mismatch blocks final publication.

Set any of `calls_per_day`, `calls_total`, `reserved_tokens_per_day`, `reserved_tokens_total` to `0` to disable that aggregate ceiling explicitly. Defaults remain bounded. Provider quota, attempt bounds, timeouts and byte guards still apply. The local runtime configuration can differ from these defaults.

For local quality review: `python scripts/review_samples.py --chapters 1 8 12` exports the saved edited prose and optional aligned Chinese source to `data/review/ban-dich-mau.html`, plus Markdown. These samples never enter the final EPUB or delivery outbox.


The current translation style uses natural Vietnamese for ordinary nouns, narration, gestures and dialogue. Sino-Vietnamese is reserved for proper names and established historical/specialized terms whose distinction matters. Existing preferred vocabulary never overrides fluency. The editor must revise stiff older drafts throughout, not merely fix spelling.

An explicit `[agent] style_revision = "natural-vi-v1"` identifies this editing pass. Changing that revision archives existing edited prose in `edited_history`, reuses it as a draft and queues every previously edited chapter for a new pass. Calls retain their revision and all historical usage; attempt caps apply within each revision. A completed artifact cannot be restyled in place. `books.contextual_terms` lists reviewed ordinary/descriptive terms that must remain vocabulary guidance. Restart the manual pipeline after changing the style revision.


User-selected style update: keep Hán–Việt forms of address and self-designation (phu quân, nương tử, tiểu hữu, lão phu, tại hạ, etc.) and established ta/ngươi dialogue. Natural Vietnamese applies to ordinary narration, descriptions and vocabulary, not modernization of address. The active revision is `natural-vi-hanviet-address-v2`; the previous style's few edited samples must pass editing again to restore any modernized address.


Source-only inconsistencies (prices, arithmetic, sibling names, chronology or plot logic) are advisory. Translate the supplied statements faithfully without repairing the author. Source notes stay in raw/internal records and are omitted from every EPUB. Only unresolved translation defects belong in blocking issues; explicitly SOURCE:-prefixed concerns do not block. Paragraph coverage, named identities, source-relative numeric values and untranslated Chinese remain checked. Audited response recovery remains tied to source/output hashes.

The manual language worker prioritizes editing when the draft backlog reaches `jobs.edit_priority_backlog` (default 8). This is a priority threshold, not a queue ceiling. A failed or blocked edit falls through to translation, which can continue through every available chapter. Local programming failures do not trip the provider circuit for the other stage; account quota and unresolved transport failures still pause inference. Failed edits remain pending review and prevent final EPUB delivery. The old `max_unedited_chapters` key is accepted as a priority threshold for compatibility. Explicitly disabled aggregate call/token ceilings remain disabled.


Numeric validation distinguishes 两 as the unit lạng after an amount (五十两 = 50 lạng) from 两 as the digit two (两百 = 200). It compares canonical Chinese integer values through 亿, Vietnamese scale words such as vạn/triệu, and unambiguous dot-separated thousands when converting Chinese numerals. Existing Arabic literals remain exact; uncertain ranges, changed magnitude, duplicated or reordered numeric values still fail validation. Paragraph-level numeric and untranslated-Chinese findings are aggregated and sent back to the editor together, using the saved response and the same persisted per-style attempt cap as glossary repairs. Stale machine warnings are recomputed before editing; schema, coverage and unresolved material issues remain blocking.


Unprefixed editor issues describe genuine unresolved translation defects and enter bounded repair. Source-logic concerns use internal source_notes or SOURCE:-prefixed issues and do not trigger retries. No visible translator explanation is required for an original inconsistency or clear contextual typo. Raw responses and usage remain retained.


Optional source-note quotations and references are no longer publication checks because these notes are not reader-facing. Untranslated Chinese in chapter prose is still rejected. Successful recovery acknowledges prior failed attempts while retaining their raw responses and accounting.

Whole-document source deduplication requires matching complete copies with identical meaningful characters and paragraph boundaries; individual repeated phrases are never removed. Calls carry a source input hash, so a verified source correction permits fresh bounded attempts and cannot recover or reuse an editor response for the old input. Historical reservations remain in aggregate usage. Existing source repairs must preserve the original source, draft, paragraph mapping and provenance in an audit backup.

Family-title grammar is source-aware: 刘家大少爷 can render as “Lưu đại thiếu gia” because the original contains that title. The family-name stem, title and rank must match exactly; a bare surname or a title absent from the source is not accepted. Such contextual forms never overwrite the canonical family glossary entry. Recovery revalidates saved responses with these checks without paying for a new translation.


Glossary matching uses a single source-mention resolver for prompts, paragraph validation, alias normalization and term admission. The pinned Jieba dictionary (HMM disabled) supplies lexical evidence, not a final identity decision. A short match crossing two multi-character words is excluded (小小/山贼 does not name 小山). A single uncertain boundary is preserved: names such as 大虎 in 大虎将钱箱 or 王渊 remain checked even when the tokenizer splits them incorrectly.

Short homonyms that may have an ordinary sense carry `ordinary_reading_allowed` in paragraph guidance. An editor can return `glossary_readings` with the exact paragraph, character position, verbatim full source quote, actual Vietnamese phrase and contextual reason. These are auditable model judgments, not a deterministic guarantee of semantics. They apply only to that occurrence; they cannot waive full names, family/place identities, another occurrence, numbers, coverage or unresolved issues, and never update the canonical registry. Draft judgments must be independently reconsidered in editing. They are retained in saved JSON but not printed as technical notes in the EPUB. Older saved responses without this optional field still validate.

Run `.venv\Scripts\python.exe -m novel_crawler.glossary_audit` to replay the complete source corpus and saved edits without inference or library changes. The report in `data/audit/glossary-audit.json` records excluded matches, existing edited failures and failed responses that can now be recovered. This is an optional diagnostic; `start.cmd` remains the single entry point for the full pipeline.


Antigravity integration: set `[agent] provider = "agy"` to run Gemini 3.8 Flash with explicit `--effort high` through the installed official `agy` CLI and its existing Google login. The adapter checks `/model` before inference, sends one NDJSON user message through stdin, selects a workspace-local text-only agent, requests plain JSON and validates it against the chapter contract in the application. CLI 1.1.28 schema mode caused repeated forced invocations in the real chapter test, so the adapter deliberately does not pass `--json-schema`. It checks stream model/agent identity, terminal status and token usage. It never extracts credentials or silently selects another model. See [Google headless CLI documentation](https://www.antigravity.google/docs/cli/headless/).

Completed Antigravity responses and their token usage are saved before parsing chapter JSON. Invalid JSON pauses its chapter/stage without blocking the other stage; incomplete streams and uncertain provider calls still require recovery. The parser can remove duplicate or trailing object commas identified by the strict JSON parser, without changing string contents, inventing values or filling array gaps. Original responses remain in the ledger and event files. This repair also applies when recovering saved results and requires no additional agent call. The text-only agent has no command task, so normal translation/editing needs no shell permission prompt.

AGY workspaces and event output live under `data/agent-workspaces/agy/`. Each request records its call ID, prompt hash, model and effort; stdout is written there during inference, including partial output on timeout. On the next manual run, recovery reuses a complete successful stream only when its call ID, prompt hash and model match; chapter source hashes and all content validators still apply. A single enclosing JSON Markdown fence is accepted without altering its payload. The runtime configuration allows 600 seconds for a full chapter. These files are retained because the CLI service can keep directory handles open after the foreground process exits; cleanup must not turn a successful paid response into WinError 32. All timeout, cancellation, response-size and accounting checks still apply. Attempt limits are scoped to provider/model, style and source; global accounting includes every provider. Explicitly abandoned setup tests remain in the ledger and audit backup, with their reservations preserved, but do not exhaust future quality-repair attempts.

Ordinary glossary readings still require a valid source occurrence and cannot waive protected identities. Their optional Vietnamese commentary may differ in wording/order from the prose; idiomatic paraphrases are not rejected for substring mismatch.

With `provider = "codex_cli"` and `fallback_provider = "agy"`, the manual `start.cmd` language worker uses Terra medium first and switches translation and editing to Gemini 3.8 Flash high when Terra reports quota exhaustion or has a persisted quota pause. The switch is logged and the status report shows the active and primary providers. Existing reservations remain accounted for; only quota failures without returned content release their chapter for fallback. Transport failures, authentication failures, editing findings and configured budget ceilings never trigger fallback. Gemini remains active for the rest of that invocation; a fresh manual invocation tries Terra again after its persisted cooldown. If both providers exhaust quota, inference pauses without a retry loop. Explicitly rolled-back trial outputs stay in the audit ledger but cannot be recovered into the book or exhaust future stage attempts.

Explicit person/place names must retain their accepted rendering. When `join_previous=true` joins fragments across an unfinished source sentence, glossary, numeric and term-evidence checks use those fragments together, allowing natural Vietnamese word order across IDs. A source sentence-ending punctuation mark keeps checks separate even if the output requests a layout join. Source IDs and contextual-reading offsets remain unchanged; missing or incorrect names and amounts still fail validation. Natural prose must not replace explicit identities with pronouns or surname-only family references; source pronouns remain pronouns. To stop a manual runner after its current agent response has been saved, create `data/drain.request`. This differs from the immediate cancellation file `data/stop.request`; a fresh `start.cmd` clears either request.

Completed-chapter preview EPUBs have a range-specific title and identifier distinct from the final book. Explicit user-requested Kindle snapshots are recorded separately under data/kindle-snapshots/; they do not freeze or mark delivery of the eventual final book.

The current reader style is `natural-vi-hanviet-han-layout-v3`: Vương Uyên is hắn in narration; historical forms of address remain unchanged. Source IDs remain aligned for coverage, while paragraph `join_previous` flags join website-fragmented sentences in the EPUB. Double newlines within an item separate reading paragraphs; single newlines are unwrapped. Separate complete quoted utterances receive distinct reading paragraphs. Existing edits are archived and queued for a new pass when the explicit style revision changes.

Gemini-first mode uses `[agent] provider = "agy"` and `policy_fallback_provider = "codex_cli"` (remove the separate `fallback_provider` quota option). Gemini 3.8 Flash high handles normal chapters. An explicit policy refusal routes that chapter to Terra medium; if translation needed Terra, its edit also uses Terra. The next chapter returns to Gemini. Routing is recovered from calls scoped to chapter, stage, source hash and style revision. Known refusals never pause the other provider/stage; quota, authentication, transport and validation failures retain their existing handling and do not trigger policy routing. Terminal refusal usage is recorded even when no publishable text was returned. Provider restrictions remain in force; prompts do not ask models to bypass them.

If Terra also refuses, `status.txt` shows a manual request under `data/manual-review/<book>/chapter-…/`. `source.json` contains the Chinese paragraphs and glossary; fill the Vietnamese `result` in `submission.json` while keeping metadata and paragraph IDs intact. This submission is the final human-translated and human-edited chapter: set `ready=true` and `reviewed=true` only after reviewing the complete text. Run the same `start.cmd` to validate and import it before agents resume. Alternatively use `.venv\Scripts\python.exe -m novel_crawler manual-import --file "<absolute path to submission.json>"` while the runner is stopped. Imports check source/style identity, coverage, glossary, numeric fidelity and the normal final-edit contract. Accepted submissions are audited in SQLite, import once without model calls, and do not erase agent costs. A draft submission is never overwritten during restart. Failed validation is reported without publishing the chapter. Ready earlier edits continue while a later chapter awaits manual input; dependent later translations wait for the missing context. Policy refusals are never recorded as missing source chapters or silently replaced with summaries.

With `contextual_glossary_review=true`, an edit rejected only for glossary gets a separate small contextual review of ambiguous two-character matches. The reviewer distinguishes an actual entity from a common word/idiom/positional phrase and may return uncertain. For example 马前 names Mã Tiền in 主薄马前, but denotes “in front of the horse” in 王渊马前跪下. Dictionary segmentation alone cannot reliably resolve this. Review decisions require the exact source occurrence and a matching Vietnamese phrase; approved ordinary readings are cached against the source hash and do not change canonical names or prose. Longer names and family/place identities keep their existing checks. A cached decision cannot waive another occurrence or a changed source/rendering. `glossary_reviews` links each parent edit to its metered review call; `glossary_decisions` retains the evidence. There is at most one review per parent edit/provider, within the existing chapter-stage and aggregate budgets. Gemini review policy refusals may use the configured Terra fallback. Completed paid review responses are reused on restart; uncertainty or malformed evidence does not trigger repeated whole-chapter rewrites. Actual name errors retain the bounded editing-repair path. Contextual review is a model judgment, not a guarantee of perfect semantic accuracy. The chapter still passes all coverage, numeric and final-edit checks before acceptance.

Malformed optional `glossary_readings` from the translating/editing agent are discarded from the accepted result. Their original contents remain in `calls.response`. Discarding an annotation grants no waiver: the normal name checks still run, and any remaining ambiguous match is eligible for the independent contextual review. Thus an incorrect position or partial quote cannot alone reject correct prose, hide a wrong name, or prevent a targeted review. Names split by source line wrapping are included in glossary guidance; validation joins only explicit continuations of unfinished source sentences.

---
name: paper-fetch
description: Batch download academic papers from DOI links and convert PDFs to Markdown. Use this skill when a user asks to download papers, fetch references, build a local paper library, or convert PDFs to readable text for evidence review.
---

# Paper Fetch

A generic tool for downloading academic papers by DOI and converting them to high-quality Markdown. Works with single papers or batch lists. Supports paywalled publishers (via institutional cookies) and open-access journals.

**When to use**: the user asks to download a paper, fetch references, build a paper library, or convert PDFs to Markdown — in any project context.

## How it works

1. You give the script one or more DOIs
2. It fetches metadata from **CrossRef** (authors, year, title)
3. Downloads the PDF using publisher-specific URL rules
4. Converts to Markdown via **Datalab API** (balanced mode, ~$0.004/page), or falls back to **pypdf** text extraction if no API key
5. Files are saved as: `Author et al. (YYYY) Title.pdf` / `.md`

## Skill directory layout

All config and state live in this skill directory (`paper-fetch/`):

```
paper-fetch/
  SKILL.md              # this file
  fetch_papers.py       # the CLI script (agent runs directly)
  .env                  # DATALAB_API_KEY + PAPERS_DIR (user configures)
  .cookies.json         # exported browser cookies (auto-generated)
  .browser-data/        # Playwright persistent context (auto-generated)
```

Papers are saved to the directory specified by `PAPERS_DIR` in `.env`.

## Workflow

**IMPORTANT: Follow steps in strict order. Do NOT skip ahead.**

### Step 0 — Install dependencies and preflight (MUST run first)

The script manages its own dependencies. Run these before anything else:

```bash
// turbo
python paper-fetch/fetch_papers.py --install-deps
```

Then verify everything is ready:

```bash
// turbo
python paper-fetch/fetch_papers.py --preflight
```

If preflight reports failures, resolve them before proceeding:
- **Missing packages** → re-run `--install-deps`
- **Missing DATALAB_API_KEY** → **STOP and ask the user** one of:
  1. Get a free key ($5 credit) at https://www.datalab.to/app/keys → write to `.env`
  2. Skip high-quality conversion, use pypdf fallback (add `--no-convert` is NOT needed; fallback is automatic)
  3. Download only with `--no-convert`
- **Missing PAPERS_DIR** → **Ask the user** where to save papers. Tell them the `.env` path (`paper-fetch/.env`) so they can set `PAPERS_DIR` permanently. If the user confirms a directory, write `PAPERS_DIR=<path>` to `.env` for them, or pass `--dir <path>` on the command line. The agent may also suggest a sensible default (e.g. current workspace) and set it if the user agrees.
- **Missing cookies** → proceed to Step 1 if paywalled papers are needed
- **Never write API keys into chat or committed files.** Keys live only in `.env`.

### Step 1 — Ensure cookies (if paywalled papers needed)

If the user needs paywalled papers (Springer, Wiley, Elsevier, etc.):

```bash
python paper-fetch/fetch_papers.py --login
```

**Never run `--login` as agent** — it opens a GUI browser. Ask the user to run it.

OA papers (Copernicus, MDPI, SpringerOpen) work without cookies.

### Step 2 — Fetch papers

Single paper:
```bash
python paper-fetch/fetch_papers.py --fetch 10.1007/s10291-022-01243-1
```

Multiple papers:
```bash
python paper-fetch/fetch_papers.py --fetch 10.1007/s10291-022-01243-1 10.5194/amt-9-5965-2016
```

Download only (no conversion):
```bash
python paper-fetch/fetch_papers.py --fetch DOI --no-convert
```

Override save directory:
```bash
python paper-fetch/fetch_papers.py --fetch DOI --dir /custom/path
```

The script will:
- Query CrossRef for metadata → build filename
- Download PDF → validate `%PDF-` header
- Convert to Markdown (Datalab if API key set, pypdf fallback otherwise)

Check output:
- `✓ PDF saved` — download success
- `✓` after Converting — conversion success
- `⚠ using pypdf fallback` — no API key, basic text extraction used
- `✗ not a PDF` — cookies expired → ask user to re-run `--login`
- `✗ unknown publisher` — add a rule to `_guess_pdf_url` in the script
- `_failures.json` — written to papers dir if any DOIs fail

### Step 3 — Convert existing PDFs (optional)

To convert PDFs that were placed manually:

```bash
python paper-fetch/fetch_papers.py --convert
python paper-fetch/fetch_papers.py --convert --force    # reconvert all
python paper-fetch/fetch_papers.py --convert --dir /path  # specific directory
```

## Supported publishers

Springer, Wiley (incl. AGU), Copernicus, Elsevier, Cambridge University Press, Taylor & Francis, MDPI, IEEE. Add new publishers by editing `_guess_pdf_url()` in the script.

## Error recovery

- **Cookies expired** (downloads return HTML): ask user to re-run `--login`.
- **API failure mid-batch**: safe to re-run — existing files are skipped.
- **Unknown publisher**: check the landing URL domain, add a rule to `_guess_pdf_url`.
- **Manual PDFs**: place files in the papers directory and run `--convert`.

## Constraints

- **Never run `--login` as agent.** It requires a GUI browser window.
- **Never expose API keys** in chat or committed files. Keys live only in `.env`.
- **Never create temporary or intermediate files** (e.g. manual PDF downloads). All downloads MUST go through `--fetch`. If you need to rename or move files, rename in-place — do not copy then delete.
- `--preflight`, `--install-deps`, `--fetch` and `--convert` are safe for agent execution.
- Downloaded files are validated by the `%PDF-` magic header.
- 1-second delay between downloads to avoid rate limiting.

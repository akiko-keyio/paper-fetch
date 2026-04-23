"""
Paper Fetch — Download academic papers by DOI and convert to Markdown.

Usage:
  python fetch_papers.py --login                         # one-time institutional login
  python fetch_papers.py --fetch DOI [DOI ...]           # download + convert
  python fetch_papers.py --fetch DOI --no-convert        # download only
  python fetch_papers.py --convert [--dir PATH] [--force]  # convert existing PDFs
"""

import argparse
import json
import os
import re
import asyncio
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
ENV_FILE = SCRIPT_DIR / ".env"
BROWSER_DATA = SCRIPT_DIR / ".browser-data"
COOKIES_FILE = SCRIPT_DIR / ".cookies.json"


# ── Dependency management ──────────────────────────────────────

REQUIRED_PACKAGES = {
    "httpx": "httpx",
    "playwright": "playwright",
    "datalab_sdk": "datalab-python-sdk",
}


def _check_package(import_name: str) -> bool:
    try:
        __import__(import_name)
        return True
    except ImportError:
        return False


def _check_chromium() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            path = p.chromium.executable_path
            return path and os.path.exists(path)
    except Exception:
        return False


def install_deps():
    """Install all required packages and Playwright chromium."""
    missing = [pip_name for imp, pip_name in REQUIRED_PACKAGES.items()
               if not _check_package(imp)]
    if missing:
        print(f"Installing: {', '.join(missing)}")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "-U"] + missing)
    else:
        print("✓ All Python packages installed")

    if not _check_chromium():
        print("Installing Playwright chromium...")
        subprocess.check_call(
            [sys.executable, "-m", "playwright", "install", "chromium"])
    else:
        print("✓ Playwright chromium ready")


def preflight() -> int:
    """Check all prerequisites. Returns 0 if all OK, 1 otherwise."""
    ok = True
    print("Preflight checks:\n")

    # Packages
    for imp, pip_name in REQUIRED_PACKAGES.items():
        if _check_package(imp):
            print(f"  ✓ {pip_name}")
        else:
            print(f"  ✗ {pip_name} — run --install-deps")
            ok = False

    # Chromium
    if _check_package("playwright") and _check_chromium():
        print("  ✓ chromium")
    else:
        print("  ✗ chromium — run --install-deps")
        ok = False

    # .env / API key
    env = _load_env()
    if env.get("DATALAB_API_KEY"):
        print("  ✓ DATALAB_API_KEY")
    else:
        print("  ⚠ DATALAB_API_KEY not set — conversion requires it")
        print("    Get free key ($5 credit): https://www.datalab.to/app/keys")
        print("    Or use --no-convert for download-only mode")

    # Cookies
    if COOKIES_FILE.exists():
        print("  ✓ cookies")
    else:
        print("  ⚠ no cookies — run --login for paywalled papers")

    print()
    return 0 if ok else 1


# ── Config ─────────────────────────────────────────────────────

def _load_env() -> dict:
    """Load key=value pairs from .env file."""
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip("\"'")
    return env


def _get_papers_dir(cli_dir: str | None = None) -> Path:
    """Resolve papers directory: CLI arg > .env PAPERS_DIR > error."""
    if cli_dir:
        return Path(cli_dir)
    env = _load_env()
    d = env.get("PAPERS_DIR")
    if d:
        return Path(d)
    raise FileNotFoundError(
        "PAPERS_DIR not configured.\n"
        f"Set PAPERS_DIR in {ENV_FILE}, or pass --dir on the command line."
    )


def _get_api_key() -> str:
    """Load Datalab API key from environment variable or .env file."""
    key = os.environ.get("DATALAB_API_KEY")
    if key:
        return key
    env = _load_env()
    key = env.get("DATALAB_API_KEY")
    if key:
        return key
    raise FileNotFoundError(
        "DATALAB_API_KEY not found.\n"
        f"Set it in {ENV_FILE} as: DATALAB_API_KEY=your_key_here\n"
        "Get a key at https://www.datalab.to/app/keys ($5 free credit)"
    )


# ── CrossRef metadata ─────────────────────────────────────────

def _fetch_metadata(doi: str) -> dict | None:
    """Fetch paper metadata (authors, year, title) from CrossRef API."""
    import httpx
    try:
        resp = httpx.get(
            f"https://api.crossref.org/works/{doi}",
            timeout=15,
            headers={"User-Agent": "paper-fetch/1.0 (https://github.com)"},
        )
        if resp.status_code != 200:
            return None
        data = resp.json()["message"]

        # Authors
        authors = data.get("author", [])
        if len(authors) == 0:
            author_str = "Unknown"
        elif len(authors) == 1:
            author_str = authors[0].get("family", "Unknown")
        elif len(authors) == 2:
            author_str = (f"{authors[0].get('family', '')} & "
                          f"{authors[1].get('family', '')}")
        else:
            author_str = f"{authors[0].get('family', '')} et al."

        # Year
        year = None
        for date_key in ("published-print", "published-online", "created"):
            if date_key in data and "date-parts" in data[date_key]:
                year = data[date_key]["date-parts"][0][0]
                break

        # Title
        titles = data.get("title", [])
        title = titles[0] if titles else "Untitled"

        return {"authors": author_str, "year": year, "title": title, "doi": doi}
    except Exception:
        return None


def _build_filename(meta: dict) -> str:
    """Build sanitized filename: 'Author et al. (YYYY) Title'."""
    author = meta.get("authors", "Unknown")
    year = meta.get("year") or "YYYY"
    title = meta.get("title", "Untitled")

    # Truncate title to avoid filesystem path limits
    if len(title) > 120:
        title = title[:120].rsplit(" ", 1)[0] + "..."

    name = f"{author} ({year}) {title}"
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    name = name.replace('\n', ' ').replace('\r', '').strip()
    return name


# ── Publisher PDF URL patterns ─────────────────────────────────

def _copernicus_pdf_url(doi: str) -> str | None:
    """Build PDF URL for Copernicus OA journals (AMT, NPG, HESS, etc.)."""
    m = re.match(r'10\.5194/(\w+)-(\d+)-(\d+)-(\d{4})', doi)
    if m:
        journal, vol, page, year = m.groups()
        return (f"https://{journal}.copernicus.org/articles/"
                f"{vol}/{page}/{year}/{journal}-{vol}-{page}-{year}.pdf")
    return None


def _guess_pdf_url(landing_url: str, doi: str) -> str | None:
    """Guess direct PDF URL based on publisher landing-page domain."""
    from urllib.parse import urlparse
    domain = urlparse(landing_url).netloc

    # Springer (all flavors: link.springer.com, springeropen, biomedcentral)
    if "springer" in domain or "biomedcentral" in domain:
        return f"https://link.springer.com/content/pdf/{doi}.pdf"

    # Wiley (including AGU journals)
    if "wiley.com" in domain:
        return f"https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}"

    # Copernicus (OA: AMT, NPG, HESS, ACP, etc.)
    if "copernicus.org" in domain:
        return _copernicus_pdf_url(doi)

    # Elsevier / ScienceDirect
    if "sciencedirect.com" in domain or "elsevier.com" in domain:
        return landing_url.rstrip("/") + "/pdfft?isDTMRedir=true&download=true"

    # Cambridge University Press
    if "cambridge.org" in domain:
        parts = landing_url.rstrip("/").split("/")
        article_id = parts[-1]
        return (f"https://www.cambridge.org/core/services/"
                f"aop-cambridge-core/content/view/{article_id}")

    # Taylor & Francis
    if "tandfonline.com" in domain:
        return f"https://www.tandfonline.com/doi/pdf/{doi}"

    # MDPI (OA)
    if "mdpi.com" in domain:
        return f"https://www.mdpi.com/{doi.split('/')[-1]}/pdf"

    # IEEE Xplore
    if "ieee.org" in domain:
        m = re.search(r'/document/(\d+)', landing_url)
        if m:
            return (f"https://ieeexplore.ieee.org/stampPDF/"
                    f"getPDF.jsp?tp=&arnumber={m.group(1)}")

    return None


# ── Login (needs GUI, user runs once) ──────────────────────────

async def login_session():
    """Open browser → user logs in → export cookies to JSON."""
    from playwright.async_api import async_playwright

    sample_url = "https://link.springer.com/article/10.1007/s10291-024-01641-7"
    print("Opening browser for institutional login...")
    print(f"URL: {sample_url}\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(
            str(BROWSER_DATA), headless=False, accept_downloads=True,
        )
        page = await browser.new_page()
        await page.goto(sample_url)

        print('1. Click "Log in via an institution" → complete SSO login.')
        print("2. Verify you can see the PDF download button.")
        print("3. Come back here and press Enter.\n")
        input(">>> Press Enter after login is complete...")

        cookies = await browser.cookies()
        COOKIES_FILE.write_text(json.dumps(cookies, indent=2), encoding="utf-8")
        print(f"\n✓ Exported {len(cookies)} cookies → {COOKIES_FILE.name}")
        await browser.close()


# ── Download helpers ───────────────────────────────────────────

def _load_cookies_for_httpx():
    """Load saved cookies. Returns None if no cookies file (OA-only mode)."""
    import httpx
    if not COOKIES_FILE.exists():
        return None
    raw = json.loads(COOKIES_FILE.read_text(encoding="utf-8"))
    jar = httpx.Cookies()
    for c in raw:
        jar.set(c["name"], c["value"], domain=c.get("domain", ""))
    return jar


def _resolve_doi(client, doi: str) -> str | None:
    """Follow DOI redirect to get the landing page URL."""
    try:
        resp = client.get(f"https://doi.org/{doi}", follow_redirects=True, timeout=20)
        return str(resp.url)
    except Exception:
        return None


# ── Fetch (download + convert) ─────────────────────────────────

def fetch_papers(dois: list[str], papers_dir: Path,
                 convert: bool = True, force: bool = False,
                 workers: int = 3):
    """Download papers by DOI, optionally convert to Markdown."""
    import httpx

    papers_dir.mkdir(parents=True, exist_ok=True)

    cookies = _load_cookies_for_httpx()
    if not cookies:
        print("⚠ No cookies found — only OA papers can be downloaded.")
        print("  Run --login first for paywalled papers.\n")

    # Prepare Datalab client if converting
    datalab_client = None
    datalab_options = None
    use_fallback = False
    if convert:
        try:
            api_key = _get_api_key()
            os.environ["DATALAB_API_KEY"] = api_key
            from datalab_sdk import DatalabClient, ConvertOptions
            datalab_client = DatalabClient()
            datalab_options = ConvertOptions(
                mode="balanced",
                output_format="markdown",
                disable_image_extraction=True,
                disable_image_captions=True,
            )
        except (FileNotFoundError, ImportError):
            # Fallback: use pypdf for basic text extraction
            if _check_package("pypdf"):
                print("⚠ DATALAB_API_KEY not set — using pypdf fallback conversion")
                use_fallback = True
            else:
                print("⚠ DATALAB_API_KEY not set and pypdf not installed.")
                print("  Skipping conversion. Get key: https://www.datalab.to/app/keys\n")
                convert = False

    results = {"ok": [], "fail": []}
    results_lock = threading.Lock()
    print_lock = threading.Lock()
    total = len(dois)
    t0 = time.time()

    def _log(msg: str):
        with print_lock:
            print(msg, flush=True)

    def _process_one(doi: str, index: int):
        """Process a single DOI: metadata → download → convert."""
        doi = doi.strip()
        if not doi:
            return

        _log(f"\n[{index}/{total}] {doi}")

        # Each thread gets its own httpx client (thread-safe alternative)
        with httpx.Client(
            cookies=cookies,
            follow_redirects=True,
            timeout=30,
            headers={
                "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36")
            },
        ) as client:
            # Step 1: Fetch metadata
            meta = _fetch_metadata(doi)
            if meta:
                filename = _build_filename(meta)
                _log(f"  [{index}] ✓ {filename}")
            else:
                _log(f"  [{index}] ✗ CrossRef failed, using DOI as filename")
                filename = doi.replace("/", "_")
                meta = {"doi": doi, "authors": "?", "year": "?",
                        "title": filename}

            pdf_path = papers_dir / f"{filename}.pdf"
            md_path = papers_dir / f"{filename}.md"

            # Step 2: Download PDF
            if pdf_path.exists() and not force:
                _log(f"  [{index}] [skip] PDF already exists")
            else:
                landing = _resolve_doi(client, doi)
                if not landing:
                    _log(f"  [{index}] ✗ DOI redirect failed")
                    meta["error"] = "DOI redirect failed"
                    with results_lock:
                        results["fail"].append(meta)
                    return

                pdf_url = _guess_pdf_url(landing, doi)
                if not pdf_url:
                    _log(f"  [{index}] ✗ unknown publisher: {landing}")
                    meta["error"] = f"unknown publisher: {landing}"
                    with results_lock:
                        results["fail"].append(meta)
                    return

                resp = client.get(pdf_url)
                body = resp.content

                if body[:5] == b"%PDF-":
                    pdf_path.write_bytes(body)
                    _log(f"  [{index}] ✓ PDF saved ({len(body) // 1024} KB)")
                else:
                    ct = resp.headers.get("content-type", "unknown")
                    _log(f"  [{index}] ✗ not a PDF (type={ct}, {len(body)//1024} KB)")
                    meta["error"] = f"not a PDF ({ct})"
                    with results_lock:
                        results["fail"].append(meta)
                    return

            # Step 3: Convert to Markdown
            if convert and pdf_path.exists():
                if md_path.exists() and not force:
                    _log(f"  [{index}] [skip] Markdown already exists")
                elif use_fallback:
                    try:
                        _fallback_convert(pdf_path, md_path)
                        _log(f"  [{index}] ✓ Converted (pypdf fallback)")
                    except Exception as e:
                        _log(f"  [{index}] ✗ Convert failed: {e}")
                elif datalab_client:
                    try:
                        result = datalab_client.convert(
                            str(pdf_path), options=datalab_options)
                        if result.status == "complete":
                            md_path.write_text(
                                result.markdown, encoding="utf-8")
                            _log(f"  [{index}] ✓ Converted ({len(result.markdown)//1000}k chars)")
                        else:
                            _log(f"  [{index}] ✗ Convert status={result.status}")
                    except Exception as e:
                        _log(f"  [{index}] ✗ Convert failed: {e}")

            with results_lock:
                results["ok"].append(meta)

    # Run in parallel
    effective_workers = min(workers, len(dois))
    if effective_workers > 1:
        _log(f"Parallel mode: {effective_workers} workers")

    with ThreadPoolExecutor(max_workers=effective_workers) as pool:
        futures = {
            pool.submit(_process_one, doi, i): doi
            for i, doi in enumerate(dois, 1)
        }
        for future in as_completed(futures):
            exc = future.exception()
            if exc:
                _log(f"  ✗ Unexpected error for {futures[future]}: {exc}")

    # Summary
    elapsed = time.time() - t0
    print(f"\n{'=' * 50}")
    print(f"OK: {len(results['ok'])}, Failed: {len(results['fail'])}")
    print(f"Time: {elapsed:.0f}s")
    print(f"Directory: {papers_dir}")

    if results["fail"]:
        print("\nFailed papers:")
        for m in results["fail"]:
            print(f"  {m['doi']}: {m.get('error', 'unknown')}")
        report = papers_dir / "_failures.json"
        report.write_text(
            json.dumps(results["fail"], indent=2, ensure_ascii=False),
            encoding="utf-8")
        print(f"  → {report}")

    return results


# ── Convert standalone ─────────────────────────────────────────

def convert_pdfs(papers_dir: Path, force: bool = False):
    """Convert all PDFs in a directory to Markdown using Datalab API."""
    api_key = _get_api_key()
    os.environ["DATALAB_API_KEY"] = api_key

    try:
        from datalab_sdk import DatalabClient, ConvertOptions
    except ImportError:
        print("Install: pip install datalab-python-sdk")
        return

    client = DatalabClient()
    options = ConvertOptions(
        mode="balanced",
        output_format="markdown",
        disable_image_extraction=True,
        disable_image_captions=True,
    )

    pdf_files = sorted(papers_dir.rglob("*.pdf"))
    print(f"Found {len(pdf_files)} PDFs to convert\n")

    done, skipped, failed = 0, 0, 0
    t0 = time.time()

    for i, pdf_path in enumerate(pdf_files, 1):
        md_path = pdf_path.with_suffix(".md")
        if md_path.exists() and not force:
            print(f"  [{i}/{len(pdf_files)}] [skip] {pdf_path.name}")
            skipped += 1
            continue

        print(f"  [{i}/{len(pdf_files)}] {pdf_path.name}...", end=" ", flush=True)
        try:
            result = client.convert(str(pdf_path), options=options)
            if result.status == "complete":
                md_path.write_text(result.markdown, encoding="utf-8")
                print(f"✓ {len(result.markdown) // 1000}k chars")
                done += 1
            else:
                print(f"✗ status={result.status}")
                failed += 1
        except Exception as e:
            print(f"✗ {e}")
            failed += 1

    elapsed = time.time() - t0
    print(f"\nConverted: {done}, Skipped: {skipped}, Failed: {failed}")
    print(f"Total time: {elapsed:.0f}s")


# ── Fallback conversion ───────────────────────────────────────

def _fallback_convert(pdf_path: Path, md_path: Path):
    """Basic PDF→Markdown using pypdf (text extraction only)."""
    from pypdf import PdfReader
    reader = PdfReader(str(pdf_path))
    title = pdf_path.stem
    parts = [f"# {title}", ""]
    for i, page in enumerate(reader.pages, 1):
        text = page.extract_text() or ""
        parts.append(f"\n\n## Page {i}\n\n{text.strip()}")
    md_path.write_text("\n".join(parts).strip() + "\n", encoding="utf-8")


# ── CLI ────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Download academic papers by DOI and convert to Markdown")
    parser.add_argument("--preflight", action="store_true",
                        help="[AGENT-OK] Check all prerequisites")
    parser.add_argument("--install-deps", action="store_true",
                        help="[AGENT-OK] Install required packages")
    parser.add_argument("--login", action="store_true",
                        help="[USER] Open browser for institutional login")
    parser.add_argument("--fetch", nargs="+", metavar="DOI",
                        help="[AGENT-OK] Download + convert papers by DOI")
    parser.add_argument("--convert", action="store_true",
                        help="[AGENT-OK] Convert existing PDFs to Markdown")
    parser.add_argument("--no-convert", action="store_true",
                        help="Download only, skip Markdown conversion")
    parser.add_argument("--force", action="store_true",
                        help="Re-download/reconvert even if files exist")
    parser.add_argument("--workers", type=int, default=3,
                        help="Parallel workers for download+convert (default: 3)")
    parser.add_argument("--dir", type=str, default=None,
                        help="Override papers directory (default: from .env)")
    args = parser.parse_args()

    if not any([args.preflight, args.install_deps, args.login,
                args.fetch, args.convert]):
        parser.print_help()
        return

    if args.install_deps:
        install_deps()
        if not args.fetch and not args.convert:
            return

    if args.preflight:
        sys.exit(preflight())

    if args.login:
        asyncio.run(login_session())
        if not args.fetch and not args.convert:
            return

    if args.fetch:
        papers_dir = _get_papers_dir(args.dir)
        fetch_papers(
            dois=args.fetch,
            papers_dir=papers_dir,
            convert=not args.no_convert,
            force=args.force,
            workers=args.workers,
        )

    if args.convert:
        papers_dir = _get_papers_dir(args.dir)
        convert_pdfs(papers_dir, force=args.force)


if __name__ == "__main__":
    main()

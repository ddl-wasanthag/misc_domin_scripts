#!/usr/bin/env python3
"""
report_low_usage_workspaces.py

Lists all Domino workspaces whose project volume usage is below a given
threshold (%) and writes results to a CSV file.

Usage:
    python report_low_usage_workspaces.py <threshold_percent> [output.csv]

Examples:
    python report_low_usage_workspaces.py 50
    python report_low_usage_workspaces.py 20 low_usage.csv

Requirements:
    - Run inside a Domino workspace (uses $DOMINO_API_PROXY env var)
    - Python 3.7+
    - requests library (pip install requests  — already present in Domino conda envs)
"""

import csv
import logging
import math
import os
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    print("ERROR: 'requests' library not found. Run: pip install requests", file=sys.stderr)
    sys.exit(1)


# ── Constants ─────────────────────────────────────────────────────────────────
PAGE_SIZE     = 50
SORT_FIELD    = "LastStarted"
SORT_ORDER    = "desc"
BYTES_PER_GIB = 1073741824  # 2^30

# Retry / timeout config
CONNECT_TIMEOUT_SEC  = 10   # time to establish connection
READ_TIMEOUT_SEC     = 60   # time to wait for response data
MAX_RETRIES          = 5    # total retry attempts on transient failures
RETRY_BACKOFF_FACTOR = 2    # urllib3 backoff: {factor} * (2 ** (retry - 1)) seconds
# HTTP status codes that urllib3 will automatically retry
RETRYABLE_HTTP_CODES = [429, 500, 502, 503, 504]

CSV_FIELDS = [
    "workspaceId",
    "name",
    "projectId",
    "ownerId",
    "ownerName",
    "state",
    "stateUpdatedAt",
    "volumeSizeGiB",
    "usedGiB",
    "maxDiskUsageGiB",
    "hasHighDiskUsage",
    "pctUsed",
]


# ── Logging setup ─────────────────────────────────────────────────────────────

def setup_logging(log_path: Path) -> logging.Logger:
    """Write INFO+ to console and DEBUG+ to a timestamped log file."""
    logger = logging.getLogger("workspace_report")
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)-8s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    return logger


# ── HTTP session ──────────────────────────────────────────────────────────────

def build_session() -> requests.Session:
    """
    Create a requests.Session with:
      - curl-compatible headers (User-Agent, Accept, Connection)
      - urllib3 retry on connection errors and retryable HTTP status codes
      - exponential backoff between retries
    """
    session = requests.Session()

    # Match the headers curl sends by default so the proxy accepts the connection
    session.headers.update({
        "User-Agent": "curl/7.88.1",
        "Accept":     "*/*",
        "Connection": "keep-alive",
    })

    retry = Retry(
        total=MAX_RETRIES,
        backoff_factor=RETRY_BACKOFF_FACTOR,   # waits 2s, 4s, 8s, 16s, 32s
        status_forcelist=RETRYABLE_HTTP_CODES,
        allowed_methods=["GET"],
        raise_on_status=False,                 # we check status ourselves
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://",  adapter)
    session.mount("https://", adapter)

    return session


# ── API helpers ───────────────────────────────────────────────────────────────

def build_url(base: str, page_number: int) -> str:
    return (
        f"{base}?pageSize={PAGE_SIZE}"
        f"&pageNumber={page_number}"
        f"&sortField={SORT_FIELD}"
        f"&sortOrder={SORT_ORDER}"
    )


def fetch_page(
    session: requests.Session,
    base_url: str,
    page_number: int,
    logger: logging.Logger,
) -> dict:
    """
    Fetch one page with retry + exponential backoff.

    urllib3's Retry handles:
      - Connection errors / RemoteDisconnected / resets
      - HTTP 429 and 5xx responses

    This function adds application-level logging around each attempt.
    """
    url = build_url(base_url, page_number)
    logger.debug("GET %s", url)

    timeout = (CONNECT_TIMEOUT_SEC, READ_TIMEOUT_SEC)

    for attempt in range(1, MAX_RETRIES + 2):   # +1 for the initial attempt
        try:
            t0   = time.monotonic()
            resp = session.get(url, timeout=timeout)
            elapsed = time.monotonic() - t0

            logger.debug(
                "Page %d — HTTP %d, %.0f bytes, %.2fs (attempt %d)",
                page_number, resp.status_code, len(resp.content), elapsed, attempt,
            )

            if resp.status_code in RETRYABLE_HTTP_CODES:
                wait = min(RETRY_BACKOFF_FACTOR ** attempt, 60)
                logger.warning(
                    "Page %d — HTTP %d (attempt %d/%d). Retrying in %.0fs...",
                    page_number, resp.status_code, attempt, MAX_RETRIES, wait,
                )
                time.sleep(wait)
                continue

            if not resp.ok:
                logger.error(
                    "Page %d — Unrecoverable HTTP %d: %s",
                    page_number, resp.status_code, resp.text[:200],
                )
                sys.exit(1)

            data = resp.json()
            logger.debug(
                "Page %d — top-level keys: %s",
                page_number, list(data.keys()) if isinstance(data, dict) else type(data).__name__,
            )
            return data

        except requests.exceptions.ConnectionError as exc:
            wait = min(RETRY_BACKOFF_FACTOR ** attempt, 60)
            logger.warning(
                "Page %d — Connection error (attempt %d/%d): %s. Retrying in %.0fs...",
                page_number, attempt, MAX_RETRIES, exc, wait,
            )
            if attempt > MAX_RETRIES:
                break
            time.sleep(wait)

        except requests.exceptions.Timeout as exc:
            wait = min(RETRY_BACKOFF_FACTOR ** attempt, 60)
            logger.warning(
                "Page %d — Timeout after %ds/%ds (attempt %d/%d). Retrying in %.0fs...",
                page_number, CONNECT_TIMEOUT_SEC, READ_TIMEOUT_SEC,
                attempt, MAX_RETRIES, wait,
            )
            if attempt > MAX_RETRIES:
                break
            time.sleep(wait)

        except requests.exceptions.JSONDecodeError as exc:
            logger.error("Page %d — Failed to parse JSON: %s", page_number, exc)
            sys.exit(1)

        except requests.exceptions.RequestException as exc:
            logger.error("Page %d — Unexpected request error: %s", page_number, exc)
            sys.exit(1)

    logger.error("Page %d — Giving up after %d failed attempts.", page_number, MAX_RETRIES)
    sys.exit(1)


# ── Workspace parsing ─────────────────────────────────────────────────────────

def bytes_to_gib(value: float) -> float:
    return value / BYTES_PER_GIB


# Candidate keys the API might use for the workspace list
_WORKSPACE_ARRAY_KEYS = [
    "workspaces",
    "items",
    "data",
    "results",
    "provisionedWorkspaces",
    "userWorkspaces",
]


def extract_workspaces(page: dict, logger: logging.Logger) -> list:
    """
    Pull the workspace list out of a page response regardless of the key name.
    Tries known candidate keys first, then falls back to the first list-valued key.
    """
    for key in _WORKSPACE_ARRAY_KEYS:
        if key in page and isinstance(page[key], list):
            logger.debug("Workspace array key found: '%s' (%d items)", key, len(page[key]))
            return page[key]

    # Generic fallback: first key whose value is a non-empty list
    for key, val in page.items():
        if isinstance(val, list) and val:
            logger.warning(
                "Workspace array key not in known list — using first list key found: '%s'", key
            )
            return val

    logger.error(
        "Could not find workspace array in response. Top-level keys: %s", list(page.keys())
    )
    return []


def parse_workspace(ws: dict, logger: logging.Logger) -> dict:
    """Extract and compute all fields needed for the CSV row."""
    ws_id = ws.get("id", "unknown")

    # Volume size (bytes → GiB)
    vol_bytes  = ws.get("initConfig", {}).get("volumeSize", {}).get("value", 0)
    volume_gib = bytes_to_gib(vol_bytes)
    logger.debug(
        "Workspace %s — volumeSize raw=%d bytes (%.4f GiB)", ws_id, vol_bytes, volume_gib
    )

    # diskUsage lives inside mostRecentSession
    session    = ws.get("mostRecentSession") or {}
    disk_usage = session.get("diskUsage") or ws.get("diskUsage") or {}

    # Use the API-provided percentage directly when available
    disk_usage_pct = disk_usage.get("diskUsagePercent")          # e.g. 0.0171 means 0.0171 %
    disk_usage_gib = disk_usage.get("diskUsageGiB", 0.0)
    max_disk_gib   = disk_usage.get("maxDiskUsageGib", 0.0)
    has_high_disk  = disk_usage.get("hasHighDiskUsage", False)

    # Derive pctUsed: prefer API field, fall back to computing from GiB values
    if disk_usage_pct is not None:
        pct_used     = float(disk_usage_pct)
        effective_used = disk_usage_gib or max_disk_gib
    else:
        effective_used = disk_usage_gib or max_disk_gib
        pct_used = (effective_used / volume_gib * 100) if volume_gib > 0 else 0.0

    logger.debug(
        "Workspace %s — diskUsageGiB=%.6f  maxDiskGiB=%.6f  volumeGiB=%.6f  pctUsed=%.6f%%",
        ws_id, effective_used, max_disk_gib, volume_gib, pct_used,
    )

    return {
        "workspaceId":      ws_id,
        "name":             ws.get("name", ""),
        "projectId":        ws.get("projectId", ""),
        "ownerId":          ws.get("ownerId", ""),
        "ownerName":        ws.get("ownerName", ""),
        "state":            ws.get("state", ""),
        "stateUpdatedAt":   ws.get("stateUpdatedAt", ""),
        "volumeSizeGiB":    round(volume_gib, 6),
        "usedGiB":          round(effective_used, 6),
        "maxDiskUsageGiB":  round(max_disk_gib, 6),
        "hasHighDiskUsage": has_high_disk,
        "pctUsed":          round(pct_used, 6),
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    timestamp  = datetime.now().strftime("%Y%m%d_%H%M%S")
    script_dir = Path(__file__).resolve().parent
    log_file   = script_dir / f"workspace_report_{timestamp}.log"
    logger     = setup_logging(log_file)

    logger.info("=" * 60)
    logger.info("Domino Workspace Low-Usage Report")
    logger.info("Log file : %s", log_file)
    logger.info("=" * 60)

    # ── Args
    if len(sys.argv) < 2:
        logger.error(
            "Usage: python %s <threshold_percent> [output.csv]",
            Path(__file__).name,
        )
        sys.exit(1)

    try:
        threshold = float(sys.argv[1])
        if not (0 <= threshold <= 100):
            raise ValueError("out of range")
    except ValueError:
        logger.error("threshold must be a number between 0 and 100. Got: %s", sys.argv[1])
        sys.exit(1)

    output_file = (
        Path(sys.argv[2]) if len(sys.argv) > 2
        else script_dir / f"low_usage_workspaces_{timestamp}.csv"
    )

    logger.info("Threshold  : %.2f%%", threshold)
    logger.info("Output CSV : %s", output_file)

    # ── Environment check
    api_proxy = os.environ.get("DOMINO_API_PROXY", "")
    if not api_proxy:
        logger.error("DOMINO_API_PROXY is not set. Run inside a Domino workspace.")
        sys.exit(1)

    logger.info("API proxy  : %s", api_proxy)
    base_url = f"{api_proxy}/v4/workspace"

    # ── Build HTTP session
    session = build_session()
    logger.debug(
        "HTTP session: connect_timeout=%ds read_timeout=%ds max_retries=%d backoff_factor=%d",
        CONNECT_TIMEOUT_SEC, READ_TIMEOUT_SEC, MAX_RETRIES, RETRY_BACKOFF_FACTOR,
    )

    # ── Fetch page 1 to discover totalCount
    logger.info("Fetching page 1 to determine total workspace count...")
    t0         = time.monotonic()
    first_page = fetch_page(session, base_url, 1, logger)
    elapsed    = time.monotonic() - t0

    total_count = first_page.get("totalCount", 0)
    total_pages = math.ceil(total_count / PAGE_SIZE) if total_count else 1

    first_batch = extract_workspaces(first_page, logger)
    logger.info(
        "Page 1/%d fetched in %.2fs — %d record(s) on page, %d total",
        total_pages, elapsed, len(first_batch), total_count,
    )

    # ── Collect remaining pages
    all_workspaces = list(first_batch)

    for page in range(2, total_pages + 1):
        logger.info("Fetching page %d/%d...", page, total_pages)
        t0      = time.monotonic()
        data    = fetch_page(session, base_url, page, logger)
        elapsed = time.monotonic() - t0

        batch = extract_workspaces(data, logger)
        all_workspaces.extend(batch)
        logger.info(
            "Page %d/%d fetched in %.2fs — %d record(s) | running total: %d",
            page, total_pages, elapsed, len(batch), len(all_workspaces),
        )

    logger.info(
        "All pages fetched — collected %d workspace(s) (API reported %d)",
        len(all_workspaces), total_count,
    )
    if len(all_workspaces) != total_count:
        logger.warning(
            "Record count mismatch — collected %d but API reported %d. "
            "Some records may be missing.",
            len(all_workspaces), total_count,
        )

    # ── Parse, filter, write CSV
    logger.info("Parsing records and applying %.2f%% threshold...", threshold)
    rows    = []
    skipped = 0

    for ws in all_workspaces:
        try:
            parsed = parse_workspace(ws, logger)
        except Exception as exc:
            logger.warning(
                "Skipping workspace %s — parse error: %s", ws.get("id", "unknown"), exc
            )
            skipped += 1
            continue

        if parsed["pctUsed"] < threshold:
            rows.append(parsed)
            logger.debug(
                "INCLUDE %s (%s) — %.4f%%", parsed["workspaceId"], parsed["name"], parsed["pctUsed"]
            )
        else:
            logger.debug(
                "EXCLUDE %s (%s) — %.4f%% >= threshold", parsed["workspaceId"], parsed["name"], parsed["pctUsed"]
            )

    logger.info(
        "Filter complete — %d match (< %.2f%%), %d excluded, %d skipped on parse error",
        len(rows), threshold, len(all_workspaces) - len(rows) - skipped, skipped,
    )

    logger.info("Writing CSV → %s", output_file)
    try:
        with open(output_file, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        logger.info("CSV written — %d row(s).", len(rows))
    except OSError as exc:
        logger.error("Failed to write CSV: %s", exc)
        sys.exit(1)

    logger.info("=" * 60)
    logger.info("Done.")
    logger.info("  CSV : %s", output_file)
    logger.info("  Log : %s", log_file)
    logger.info("=" * 60)


if __name__ == "__main__":
    main()

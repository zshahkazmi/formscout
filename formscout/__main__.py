"""Command-line interface for formscout.

Usage:
    python -m formscout domains.txt -o results.csv
    python -m formscout domains.csv -o results.csv --workers 10 --timeout 20

Input:  .txt (one domain per line) or .csv (a 'domain' column, or the
        first column if no header matches).
Output: CSV with one row per domain — contact URL, form info, emails,
        phones, and notes (password-gated, unreachable, ...).
"""

from __future__ import annotations

import argparse
import csv
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import __version__
from .discover import DiscoveryResult, discover


def read_domains(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if path.suffix.lower() == ".csv":
        # Try a 'domain' column first, else fall back to the first column.
        rows = list(csv.reader(lines))
        if not rows:
            return []
        header = [c.strip().lower() for c in rows[0]]
        if "domain" in header:
            idx = header.index("domain")
            return [r[idx].strip() for r in rows[1:] if len(r) > idx and r[idx].strip()]
        return [r[0].strip() for r in rows[1:] if r and r[0].strip()]
    # Plain text: one domain per line; skip comments/blanks.
    return [ln for ln in lines if not ln.startswith("#")]


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "submit":
        from .submit import run_cli

        return run_cli(argv[1:])
    ap = argparse.ArgumentParser(
        prog="formscout",
        description="Find contact pages and forms across a list of domains.",
    )
    ap.add_argument("input", help="File with domains (.txt one-per-line, or .csv)")
    ap.add_argument("-o", "--output", default="formscout-results.csv",
                    help="Output CSV path (default: formscout-results.csv)")
    ap.add_argument("--workers", type=int, default=8,
                    help="Parallel workers (default: 8; be polite, keep it low)")
    ap.add_argument("--timeout", type=float, default=15,
                    help="Seconds per HTTP request (default: 15)")
    ap.add_argument("-v", "--version", action="version", version=f"%(prog)s {__version__}")
    args = ap.parse_args(argv)

    in_path = Path(args.input)
    if not in_path.exists():
        print(f"error: input file not found: {in_path}", file=sys.stderr)
        return 2

    domains = read_domains(in_path)
    # De-duplicate, preserve order.
    seen, unique = set(), []
    for d in domains:
        key = d.strip().lower()
        if key and key not in seen:
            seen.add(key)
            unique.append(d.strip())
    if not unique:
        print("error: no domains found in input", file=sys.stderr)
        return 2

    print(f"formscout v{__version__}: scanning {len(unique)} domains "
          f"({args.workers} workers)…", file=sys.stderr)

    results: list[DiscoveryResult] = []
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        future_map = {pool.submit(discover, d, args.timeout): d for d in unique}
        for fut in as_completed(future_map):
            domain = future_map[fut]
            try:
                results.append(fut.result())
            except Exception as exc:  # never let one domain kill the run
                r = DiscoveryResult(domain=domain, notes=f"error: {exc}")
                results.append(r)
            done += 1
            if done % 10 == 0 or done == len(unique):
                print(f"  …{done}/{len(unique)}", file=sys.stderr)

    # Keep input order in the output.
    order = {d: i for i, d in enumerate(unique)}
    results.sort(key=lambda r: order.get(r.domain, 0))

    out_path = Path(args.output)
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(results[0].as_row().keys()))
        writer.writeheader()
        for r in results:
            writer.writerow(r.as_row())

    with_forms = sum(1 for r in results if r.has_form)
    print(f"done: {len(results)} domains, {with_forms} with forms → {out_path}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

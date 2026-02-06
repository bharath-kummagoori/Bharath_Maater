#!/usr/bin/env python3
"""
SPDX Source Downloader - Interactive Executable

Prompts the user for:
  1. Input SPDX JSON file path
  2. Output directory for downloaded source packages

Then downloads source code for all components found in the SPDX file.

Usage:
    chmod +x spdx_download.py
    ./spdx_download.py
"""

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

# Add project root to path so imports work when run standalone
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from spdx_downloader.spdx_parser import SPDXParser, PackageInfo
from spdx_downloader.registry_resolver import RegistryResolver
from spdx_downloader.fallback_resolver import FallbackResolver
from spdx_downloader.downloader import SourceDownloader

logger = logging.getLogger("spdx_downloader")

BANNER = r"""
============================================================
     SPDX Source Code Downloader
============================================================
  Supports: Maven, NPM, PyPI, NuGet, Go, Cargo, RubyGems,
  Composer, Pub, Conan, Conda, CocoaPods, Swift, Hex,
  Hackage, CRAN, CPAN, GitHub, Bitbucket, Debian, RPM,
  Alpine, Docker  (23 ecosystems)
============================================================
"""


def setup_logging(verbose: bool = False, log_file: str = ""):
    """Configure logging."""
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    handlers = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, mode="w"))

    logging.basicConfig(level=level, format=fmt, datefmt=datefmt, handlers=handlers)


def prompt_input():
    """Interactively prompt user for input file and output directory."""
    print(BANNER)

    # Prompt for input SPDX file
    while True:
        spdx_file = input("  Enter SPDX JSON file path: ").strip()
        if not spdx_file:
            print("  [ERROR] File path cannot be empty. Please try again.\n")
            continue
        # Expand ~ and resolve path
        spdx_file = str(Path(spdx_file).expanduser().resolve())
        if not Path(spdx_file).exists():
            print(f"  [ERROR] File not found: {spdx_file}")
            print("  Please check the path and try again.\n")
            continue
        if not spdx_file.lower().endswith(".json"):
            print(f"  [WARNING] File does not have .json extension: {spdx_file}")
            confirm = input("  Continue anyway? (y/n): ").strip().lower()
            if confirm != "y":
                continue
        break

    print()

    # Prompt for output directory
    while True:
        output_dir = input("  Enter output directory for downloads [./downloads]: ").strip()
        if not output_dir:
            output_dir = "./downloads"
        # Expand ~ and resolve path
        output_dir = str(Path(output_dir).expanduser().resolve())
        try:
            Path(output_dir).mkdir(parents=True, exist_ok=True)
            break
        except OSError as e:
            print(f"  [ERROR] Cannot create directory: {e}")
            print("  Please try a different path.\n")

    print()

    # Optional: GitHub token
    github_token = ""
    use_token = input("  Do you have a GitHub token? (y/n) [n]: ").strip().lower()
    if use_token == "y":
        github_token = input("  Enter GitHub token: ").strip()

    print()

    # Optional: Dry run
    dry_run = False
    mode = input("  Run mode - (d)ownload or (r)esolve only? [d]: ").strip().lower()
    if mode == "r":
        dry_run = True

    print()

    # Optional: Verbose
    verbose = False
    verb = input("  Enable verbose logging? (y/n) [n]: ").strip().lower()
    if verb == "y":
        verbose = True

    print()

    # Optional: Log file
    log_file = ""
    save_log = input("  Save logs to file? (y/n) [n]: ").strip().lower()
    if save_log == "y":
        log_file = input("  Enter log file path [spdx_download.log]: ").strip()
        if not log_file:
            log_file = "spdx_download.log"

    return {
        "spdx_file": spdx_file,
        "output_dir": output_dir,
        "github_token": github_token,
        "dry_run": dry_run,
        "verbose": verbose,
        "log_file": log_file,
    }


def process_package(
    pkg: PackageInfo,
    resolver: RegistryResolver,
    fallback: FallbackResolver,
    downloader: SourceDownloader,
    dry_run: bool = False,
) -> dict:
    """Process a single package: resolve URL and download source."""
    status = {
        "name": pkg.name,
        "version": pkg.version,
        "ecosystem": pkg.ecosystem,
        "method": None,
        "url": None,
        "downloaded": False,
        "error": None,
    }

    download_url = None

    # Strategy 1: Use downloadLocation from SPDX
    if pkg.has_download_link:
        download_url = pkg.download_location
        status["method"] = "direct"
        logger.info(f"[DIRECT] {pkg.name}@{pkg.version}: {download_url}")

    # Strategy 2: Resolve via PURL and registry API
    if not download_url and pkg.purl:
        download_url = resolver.resolve(pkg.purl)
        if download_url:
            status["method"] = "registry"
            logger.info(f"[REGISTRY] {pkg.name}@{pkg.version}: {download_url}")

    # Strategy 3: Resolve via name + version + ecosystem
    if not download_url and pkg.ecosystem:
        download_url = resolver.resolve_by_name(pkg.name, pkg.version, pkg.ecosystem)
        if download_url:
            status["method"] = "registry"
            logger.info(f"[REGISTRY] {pkg.name}@{pkg.version}: {download_url}")

    # Strategy 4: Fallback strategies
    if not download_url:
        download_url = fallback.resolve(
            name=pkg.name,
            version=pkg.version,
            cpe=pkg.cpe,
            homepage=pkg.homepage,
        )
        if download_url:
            status["method"] = "fallback"
            logger.info(f"[FALLBACK] {pkg.name}@{pkg.version}: {download_url}")

    if not download_url:
        status["error"] = "No download URL found"
        logger.warning(f"[SKIP] {pkg.name}@{pkg.version}: No download URL found")
        return status

    status["url"] = download_url

    # Download
    if dry_run:
        logger.info(f"[DRY-RUN] Would download: {pkg.name}@{pkg.version} from {download_url}")
        status["downloaded"] = False
        return status

    result = downloader.download(
        url=download_url,
        package_name=pkg.name,
        version=pkg.version,
        expected_checksums=pkg.checksums if pkg.checksums else None,
        source=status["method"],
    )

    status["downloaded"] = result.success
    if not result.success:
        status["error"] = result.error

    return status


def print_report(results, downloader, elapsed, dry_run, output_dir):
    """Print the final processing report."""
    logger.info(f"\n{'=' * 60}")
    logger.info("DOWNLOAD REPORT")
    logger.info(f"{'=' * 60}")

    total = len(results)
    direct = [r for r in results if r["method"] == "direct"]
    registry = [r for r in results if r["method"] == "registry"]
    fallback_list = [r for r in results if r["method"] == "fallback"]
    failed = [r for r in results if r["error"]]
    downloaded = [r for r in results if r["downloaded"]]

    logger.info(f"\nTotal packages:     {total}")
    logger.info(f"  Direct download:  {len(direct)}")
    logger.info(f"  Registry resolve: {len(registry)}")
    logger.info(f"  Fallback resolve: {len(fallback_list)}")

    if not dry_run:
        dl_summary = downloader.get_summary()
        logger.info(f"\nDownloaded:         {len(downloaded)}/{total}")
        logger.info(f"Total size:         {dl_summary['total_size_mb']} MB")
        logger.info(f"Output directory:   {output_dir}")

    # Per-package details with download links
    logger.info(f"\n{'─' * 60}")
    logger.info("PACKAGE DETAILS")
    logger.info(f"{'─' * 60}")
    for i, r in enumerate(results, 1):
        status_icon = "OK" if r.get("downloaded") or (dry_run and r.get("url")) else "FAILED"
        logger.info(f"\n  [{i}] {r['name']}@{r['version']}")
        logger.info(f"      Ecosystem : {r.get('ecosystem') or 'unknown'}")
        logger.info(f"      Method    : {r.get('method') or 'none'}")
        logger.info(f"      URL       : {r.get('url') or 'N/A'}")
        logger.info(f"      Status    : {status_icon}")
        if r.get("error"):
            logger.info(f"      Error     : {r['error']}")

    if failed:
        logger.info(f"\n{'─' * 60}")
        logger.info(f"FAILED PACKAGES ({len(failed)}):")
        logger.info(f"{'─' * 60}")
        for r in failed:
            logger.info(f"  - {r['name']}@{r['version']}: {r['error']}")
            logger.info(f"    URL: {r.get('url') or 'N/A'}")

    logger.info(f"\nTime elapsed: {elapsed:.1f}s")
    logger.info(f"{'=' * 60}")

    # Write report to JSON
    report_path = Path(output_dir) / "download_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w") as f:
        json.dump(
            {
                "summary": {
                    "total": total,
                    "direct": len(direct),
                    "registry": len(registry),
                    "fallback": len(fallback_list),
                    "downloaded": len(downloaded),
                    "failed": len(failed),
                    "elapsed_seconds": round(elapsed, 1),
                },
                "packages": results,
            },
            f,
            indent=2,
        )
    logger.info(f"Report saved to: {report_path}")


def run(config: dict):
    """Main execution flow."""
    start_time = time.time()

    spdx_file = config["spdx_file"]
    output_dir = config["output_dir"]
    dry_run = config["dry_run"]

    logger.info(f"{'=' * 60}")
    logger.info("SPDX Source Downloader")
    logger.info(f"{'=' * 60}")
    logger.info(f"Input:  {spdx_file}")
    logger.info(f"Output: {output_dir}")
    if dry_run:
        logger.info("Mode:   DRY RUN (resolve only, no downloads)")
    else:
        logger.info("Mode:   DOWNLOAD")
    logger.info(f"{'=' * 60}")

    # Parse SPDX file
    parser = SPDXParser(spdx_file)
    packages = parser.parse()

    if not packages:
        logger.warning("No packages found in SPDX file.")
        return

    # Print summary
    summary = parser.get_summary()
    logger.info(f"\nDocument: {summary['document_name']}")
    logger.info(f"Total packages: {summary['total_packages']}")
    logger.info(f"With download link: {summary['with_download_link']}")
    logger.info(f"Without download link: {summary['without_download_link']}")
    logger.info(f"Ecosystems: {json.dumps(summary['ecosystems'], indent=2)}")
    logger.info("")

    # Initialize components
    resolver = RegistryResolver(github_token=config["github_token"])
    fallback = FallbackResolver(github_token=config["github_token"])
    downloader = SourceDownloader(output_dir=output_dir)

    logger.info(f"Supported ecosystems: {', '.join(resolver.get_supported_ecosystems())}")
    logger.info("")

    # Process each package
    results = []
    total = len(packages)
    for i, pkg in enumerate(packages, 1):
        logger.info(f"\n[{i}/{total}] Processing: {pkg}")
        logger.info("-" * 40)

        status = process_package(
            pkg=pkg,
            resolver=resolver,
            fallback=fallback,
            downloader=downloader,
            dry_run=dry_run,
        )
        results.append(status)

        # Rate limiting between packages
        if i < total and not dry_run:
            time.sleep(0.5)

    # Print final report
    elapsed = time.time() - start_time
    print_report(results, downloader, elapsed, dry_run, output_dir)


def main():
    """Entry point - supports both interactive and CLI modes."""
    # If arguments are passed, use CLI mode
    if len(sys.argv) > 1 and not sys.argv[1].startswith("--interactive"):
        cli_parser = argparse.ArgumentParser(
            description="SPDX Source Downloader - Download source code for SPDX components",
        )
        cli_parser.add_argument("spdx_file", help="Path to SPDX JSON file")
        cli_parser.add_argument("-o", "--output-dir", default="downloads", help="Output directory")
        cli_parser.add_argument("--github-token", default="", help="GitHub token")
        cli_parser.add_argument("--dry-run", action="store_true", help="Resolve only")
        cli_parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
        cli_parser.add_argument("--log-file", default="", help="Log file path")

        args = cli_parser.parse_args()

        if not Path(args.spdx_file).exists():
            print(f"Error: File not found: {args.spdx_file}", file=sys.stderr)
            sys.exit(1)

        config = {
            "spdx_file": args.spdx_file,
            "output_dir": args.output_dir,
            "github_token": args.github_token,
            "dry_run": args.dry_run,
            "verbose": args.verbose,
            "log_file": args.log_file,
        }
    else:
        # Interactive mode
        try:
            config = prompt_input()
        except KeyboardInterrupt:
            print("\n\n  Cancelled by user.")
            sys.exit(0)

    setup_logging(verbose=config["verbose"], log_file=config["log_file"])

    print(f"\n  Starting {'resolve' if config['dry_run'] else 'download'}...\n")

    try:
        run(config)
    except KeyboardInterrupt:
        logger.info("\nInterrupted by user.")
        sys.exit(130)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()

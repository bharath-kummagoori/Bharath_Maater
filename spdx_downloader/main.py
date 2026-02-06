#!/usr/bin/env python3
"""
SPDX Source Downloader - Main entry point.

Downloads source code for all components listed in an SPDX JSON file.
Supports 20+ package ecosystems and includes fallback strategies
for when download links are not available in the SPDX document.

Usage:
    python -m spdx_downloader.main <spdx_file.json> [options]

Example:
    python -m spdx_downloader.main bom.spdx.json -o ./sources --github-token ghp_xxx
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from .spdx_parser import SPDXParser, PackageInfo
from .registry_resolver import RegistryResolver
from .fallback_resolver import FallbackResolver
from .downloader import SourceDownloader

logger = logging.getLogger("spdx_downloader")


def setup_logging(verbose: bool = False, log_file: str = ""):
    """Configure logging with console and optional file output."""
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    handlers = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, mode="w"))

    logging.basicConfig(level=level, format=fmt, datefmt=datefmt, handlers=handlers)


def process_package(
    pkg: PackageInfo,
    resolver: RegistryResolver,
    fallback: FallbackResolver,
    downloader: SourceDownloader,
    dry_run: bool = False,
) -> dict:
    """Process a single package: resolve URL and download source.

    Returns a dict with processing status.
    """
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


def run(args: argparse.Namespace):
    """Main execution flow."""
    start_time = time.time()

    # Parse SPDX file
    logger.info(f"{'=' * 60}")
    logger.info(f"SPDX Source Downloader")
    logger.info(f"{'=' * 60}")
    logger.info(f"Input: {args.spdx_file}")
    logger.info(f"Output: {args.output_dir}")
    if args.dry_run:
        logger.info("Mode: DRY RUN (no downloads)")
    logger.info(f"{'=' * 60}")

    parser = SPDXParser(args.spdx_file)
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
    resolver = RegistryResolver(github_token=args.github_token)
    fallback = FallbackResolver(github_token=args.github_token)
    downloader = SourceDownloader(output_dir=args.output_dir)

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
            dry_run=args.dry_run,
        )
        results.append(status)

        # Rate limiting between packages
        if i < total and not args.dry_run:
            time.sleep(0.5)

    # Print final report
    elapsed = time.time() - start_time
    _print_report(results, downloader, elapsed, args.dry_run, args.output_dir)


def _print_report(
    results: list,
    downloader: SourceDownloader,
    elapsed: float,
    dry_run: bool,
    output_dir: str,
):
    """Print the final processing report."""
    logger.info(f"\n{'=' * 60}")
    logger.info("DOWNLOAD REPORT")
    logger.info(f"{'=' * 60}")

    total = len(results)
    direct = [r for r in results if r["method"] == "direct"]
    registry = [r for r in results if r["method"] == "registry"]
    fallback = [r for r in results if r["method"] == "fallback"]
    failed = [r for r in results if r["error"]]
    downloaded = [r for r in results if r["downloaded"]]

    logger.info(f"\nTotal packages:     {total}")
    logger.info(f"  Direct download:  {len(direct)}")
    logger.info(f"  Registry resolve: {len(registry)}")
    logger.info(f"  Fallback resolve: {len(fallback)}")

    if not dry_run:
        dl_summary = downloader.get_summary()
        logger.info(f"\nDownloaded:         {len(downloaded)}/{total}")
        logger.info(f"Total size:         {dl_summary['total_size_mb']} MB")
        logger.info(f"Output directory:   {output_dir}")

    if failed:
        logger.info(f"\nFailed ({len(failed)}):")
        for r in failed:
            logger.info(f"  - {r['name']}@{r['version']}: {r['error']}")

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
                    "fallback": len(fallback),
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


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Download source code for components in an SPDX JSON file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s scan.spdx.json
  %(prog)s scan.spdx.json -o ./sources -v
  %(prog)s scan.spdx.json --dry-run
  %(prog)s scan.spdx.json --github-token ghp_xxxx

Supported ecosystems:
  Maven, NPM, PyPI, NuGet, Go, Cargo, RubyGems, Composer,
  Pub, Conan, Conda, CocoaPods, Swift, Hex, Hackage, CRAN,
  CPAN, GitHub, Bitbucket, Debian, RPM, Alpine, Docker
        """,
    )

    parser.add_argument(
        "spdx_file",
        help="Path to SPDX JSON file",
    )
    parser.add_argument(
        "-o", "--output-dir",
        default="downloads",
        help="Output directory for downloaded sources (default: downloads)",
    )
    parser.add_argument(
        "--github-token",
        default="",
        help="GitHub personal access token (for higher API rate limits)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only resolve URLs, do not download",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose/debug logging",
    )
    parser.add_argument(
        "--log-file",
        default="",
        help="Write logs to file",
    )

    args = parser.parse_args()

    # Validate input file
    if not Path(args.spdx_file).exists():
        print(f"Error: File not found: {args.spdx_file}", file=sys.stderr)
        sys.exit(1)

    setup_logging(verbose=args.verbose, log_file=args.log_file)

    try:
        run(args)
    except KeyboardInterrupt:
        logger.info("\nInterrupted by user.")
        sys.exit(130)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()

"""
Source Code Downloader - Downloads source archives from resolved URLs.

Handles:
- Direct downloads from SPDX downloadLocation
- Downloads from registry-resolved URLs
- Checksum verification
- Rate limiting and retries
- Progress tracking
"""

import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, unquote

import requests

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 120  # seconds
MAX_RETRIES = 3
RETRY_BACKOFF = 2  # seconds
CHUNK_SIZE = 8192  # bytes


class DownloadResult:
    """Result of a download operation."""

    def __init__(self, package_name: str, version: str):
        self.package_name = package_name
        self.version = version
        self.success = False
        self.file_path: Optional[str] = None
        self.url: Optional[str] = None
        self.error: Optional[str] = None
        self.file_size: int = 0
        self.checksum_verified: bool = False
        self.source: str = ""  # "direct", "registry", "fallback"

    def __str__(self) -> str:
        status = "OK" if self.success else "FAILED"
        size_mb = self.file_size / (1024 * 1024) if self.file_size else 0
        return (
            f"[{status}] {self.package_name}@{self.version} "
            f"({size_mb:.2f} MB) via {self.source}"
        )


class SourceDownloader:
    """Downloads source code archives with retry logic and verification."""

    def __init__(self, output_dir: str = "downloads", timeout: int = DEFAULT_TIMEOUT):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "SPDX-Source-Downloader/1.0",
        })
        self.results: list[DownloadResult] = []

    def download(
        self,
        url: str,
        package_name: str,
        version: str,
        expected_checksums: Optional[dict] = None,
        source: str = "direct",
    ) -> DownloadResult:
        """Download a source archive from a URL.

        Args:
            url: Download URL.
            package_name: Name of the package.
            version: Version string.
            expected_checksums: Dict of {algorithm: value} for verification.
            source: Source type ("direct", "registry", "fallback").

        Returns:
            DownloadResult with download status and file info.
        """
        result = DownloadResult(package_name, version)
        result.url = url
        result.source = source

        # Determine output filename
        filename = self._determine_filename(url, package_name, version)
        # Organize by package name
        pkg_dir = self.output_dir / self._sanitize_name(package_name)
        pkg_dir.mkdir(parents=True, exist_ok=True)
        file_path = pkg_dir / filename

        # Skip if already downloaded
        if file_path.exists() and file_path.stat().st_size > 0:
            logger.info(f"Already downloaded: {file_path}")
            result.success = True
            result.file_path = str(file_path)
            result.file_size = file_path.stat().st_size
            self.results.append(result)
            return result

        # Download with retries
        for attempt in range(MAX_RETRIES):
            try:
                logger.info(
                    f"Downloading {package_name}@{version} from {url} "
                    f"(attempt {attempt + 1}/{MAX_RETRIES})"
                )
                resp = self.session.get(url, stream=True, timeout=self.timeout)
                resp.raise_for_status()

                total_size = int(resp.headers.get("content-length", 0))
                downloaded = 0
                hash_algos = {}

                # Prepare checksum calculators
                if expected_checksums:
                    for algo in expected_checksums:
                        algo_name = algo.replace("-", "").lower()
                        if algo_name in hashlib.algorithms_available:
                            hash_algos[algo] = hashlib.new(algo_name)

                # Write to file
                with open(file_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            for h in hash_algos.values():
                                h.update(chunk)

                # Verify checksums
                if expected_checksums and hash_algos:
                    for algo, expected in expected_checksums.items():
                        if algo in hash_algos:
                            computed = hash_algos[algo].hexdigest()
                            if computed.lower() == expected.lower():
                                result.checksum_verified = True
                                logger.info(f"Checksum verified ({algo})")
                            else:
                                logger.warning(
                                    f"Checksum mismatch for {algo}: "
                                    f"expected {expected}, got {computed}"
                                )

                result.success = True
                result.file_path = str(file_path)
                result.file_size = downloaded
                logger.info(f"Downloaded: {file_path} ({downloaded} bytes)")
                break

            except requests.HTTPError as e:
                status = e.response.status_code if e.response else "unknown"
                logger.warning(f"HTTP error {status} downloading {url}")
                if status in (404, 410):
                    result.error = f"HTTP {status}: Not found"
                    break
                if attempt < MAX_RETRIES - 1:
                    wait = RETRY_BACKOFF * (2 ** attempt)
                    logger.info(f"Retrying in {wait}s...")
                    time.sleep(wait)
                else:
                    result.error = f"HTTP {status} after {MAX_RETRIES} retries"

            except requests.RequestException as e:
                logger.warning(f"Request error: {e}")
                if attempt < MAX_RETRIES - 1:
                    wait = RETRY_BACKOFF * (2 ** attempt)
                    logger.info(f"Retrying in {wait}s...")
                    time.sleep(wait)
                else:
                    result.error = f"Download failed: {e}"

            except OSError as e:
                result.error = f"File write error: {e}"
                break

        if not result.success and file_path.exists():
            file_path.unlink()  # Clean up partial download

        self.results.append(result)
        return result

    def _determine_filename(self, url: str, package_name: str, version: str) -> str:
        """Determine the output filename from URL or package info."""
        parsed = urlparse(url)
        path = unquote(parsed.path)
        url_filename = os.path.basename(path)

        if url_filename and "." in url_filename:
            return url_filename

        # Construct filename from package info
        safe_name = self._sanitize_name(package_name)
        safe_version = self._sanitize_name(version)

        # Try to guess extension from URL
        if any(ext in url.lower() for ext in [".tar.gz", ".tgz"]):
            return f"{safe_name}-{safe_version}.tar.gz"
        elif ".zip" in url.lower():
            return f"{safe_name}-{safe_version}.zip"
        elif ".jar" in url.lower():
            return f"{safe_name}-{safe_version}.jar"
        elif ".whl" in url.lower():
            return f"{safe_name}-{safe_version}.whl"
        elif ".gem" in url.lower():
            return f"{safe_name}-{safe_version}.gem"
        elif ".nupkg" in url.lower():
            return f"{safe_name}-{safe_version}.nupkg"
        else:
            return f"{safe_name}-{safe_version}.tar.gz"

    @staticmethod
    def _sanitize_name(name: str) -> str:
        """Sanitize a string for use in file/directory names."""
        return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)

    def get_summary(self) -> dict:
        """Return a summary of all download results."""
        successful = [r for r in self.results if r.success]
        failed = [r for r in self.results if not r.success]
        total_size = sum(r.file_size for r in successful)

        return {
            "total": len(self.results),
            "successful": len(successful),
            "failed": len(failed),
            "total_size_mb": round(total_size / (1024 * 1024), 2),
            "by_source": {
                "direct": len([r for r in successful if r.source == "direct"]),
                "registry": len([r for r in successful if r.source == "registry"]),
                "fallback": len([r for r in successful if r.source == "fallback"]),
            },
            "failed_packages": [
                {"name": r.package_name, "version": r.version, "error": r.error}
                for r in failed
            ],
        }

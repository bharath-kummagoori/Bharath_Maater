"""
Fallback Resolver - Finds source download URLs when SPDX and registry lookups fail.

Strategies:
1. GitHub search by package name + version
2. Source forge search
3. CPE-based lookup via NVD
4. Homepage URL scraping
5. Known URL pattern matching
"""

import logging
import re
import time
from typing import Optional
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

API_TIMEOUT = 30


class FallbackResolver:
    """Attempts to find source download URLs using fallback strategies."""

    def __init__(self, github_token: str = ""):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "SPDX-Source-Downloader/1.0",
            "Accept": "application/json",
        })
        self.github_token = github_token
        if github_token:
            self.session.headers["Authorization"] = f"token {github_token}"

    def resolve(
        self, name: str, version: str, cpe: str = "", homepage: str = ""
    ) -> Optional[str]:
        """Try all fallback strategies to find a source download URL.

        Args:
            name: Package name.
            version: Package version.
            cpe: CPE identifier (if available).
            homepage: Package homepage URL (if available).

        Returns:
            Source download URL or None.
        """
        strategies = [
            ("GitHub search", lambda: self._search_github(name, version)),
            ("Homepage archive", lambda: self._resolve_from_homepage(homepage, version)),
            ("Known patterns", lambda: self._try_known_patterns(name, version)),
            ("SourceForge", lambda: self._search_sourceforge(name, version)),
        ]

        for strategy_name, strategy_fn in strategies:
            try:
                logger.info(f"Trying fallback: {strategy_name} for {name}@{version}")
                url = strategy_fn()
                if url:
                    logger.info(f"Fallback resolved via {strategy_name}: {url}")
                    return url
            except Exception as e:
                logger.debug(f"Fallback {strategy_name} failed: {e}")
                continue

        logger.warning(f"All fallback strategies failed for {name}@{version}")
        return None

    def _search_github(self, name: str, version: str) -> Optional[str]:
        """Search GitHub for the package repository."""
        query = f"{name} in:name"
        url = f"https://api.github.com/search/repositories?q={quote(query)}&sort=stars&per_page=5"

        headers = {"Accept": "application/vnd.github.v3+json"}
        if self.github_token:
            headers["Authorization"] = f"token {self.github_token}"

        try:
            resp = self.session.get(url, headers=headers, timeout=API_TIMEOUT)
            if resp.status_code == 403:
                logger.warning("GitHub API rate limit reached")
                return None
            if resp.status_code != 200:
                return None

            data = resp.json()
            items = data.get("items", [])

            for item in items:
                repo_name = item.get("name", "").lower()
                full_name = item.get("full_name", "")

                # Check if repo name matches package name
                if self._names_match(name, repo_name):
                    # Try to find the version tag
                    tag_url = self._find_version_tag(full_name, version)
                    if tag_url:
                        return tag_url

                    # Fall back to tag-based URL
                    if version:
                        for tag_prefix in ["v", "", "release-", "rel-"]:
                            tag = f"{tag_prefix}{version}"
                            archive_url = (
                                f"https://github.com/{full_name}"
                                f"/archive/refs/tags/{quote(tag)}.tar.gz"
                            )
                            try:
                                check = self.session.head(
                                    archive_url, timeout=10, allow_redirects=True
                                )
                                if check.status_code == 200:
                                    return archive_url
                            except requests.RequestException:
                                continue

                    # Default to main branch
                    return f"https://github.com/{full_name}/archive/refs/heads/main.tar.gz"

        except requests.RequestException as e:
            logger.debug(f"GitHub search error: {e}")

        return None

    def _find_version_tag(self, full_name: str, version: str) -> Optional[str]:
        """Find a matching version tag in a GitHub repository."""
        if not version:
            return None

        url = f"https://api.github.com/repos/{full_name}/tags?per_page=100"
        headers = {"Accept": "application/vnd.github.v3+json"}
        if self.github_token:
            headers["Authorization"] = f"token {self.github_token}"

        try:
            resp = self.session.get(url, headers=headers, timeout=API_TIMEOUT)
            if resp.status_code != 200:
                return None

            tags = resp.json()
            clean_version = version.lstrip("v")

            for tag in tags:
                tag_name = tag.get("name", "")
                clean_tag = tag_name.lstrip("v").lstrip("release-").lstrip("rel-")

                if clean_tag == clean_version:
                    return (
                        f"https://github.com/{full_name}"
                        f"/archive/refs/tags/{quote(tag_name)}.tar.gz"
                    )

        except requests.RequestException:
            pass

        return None

    def _resolve_from_homepage(self, homepage: str, version: str) -> Optional[str]:
        """Try to derive a source download URL from the homepage."""
        if not homepage or homepage in ("NOASSERTION", "NONE", ""):
            return None

        # Check if it's a GitHub URL
        github_match = re.match(
            r"https?://github\.com/([^/]+)/([^/]+)", homepage
        )
        if github_match:
            owner, repo = github_match.groups()
            repo = repo.rstrip(".git")
            if version:
                for prefix in ["v", "", "release-"]:
                    tag = f"{prefix}{version}"
                    archive_url = (
                        f"https://github.com/{owner}/{repo}"
                        f"/archive/refs/tags/{quote(tag)}.tar.gz"
                    )
                    try:
                        check = self.session.head(
                            archive_url, timeout=10, allow_redirects=True
                        )
                        if check.status_code == 200:
                            return archive_url
                    except requests.RequestException:
                        continue
            return f"https://github.com/{owner}/{repo}/archive/refs/heads/main.tar.gz"

        # Check if it's a GitLab URL
        gitlab_match = re.match(
            r"https?://gitlab\.com/([^/]+)/([^/]+)", homepage
        )
        if gitlab_match:
            owner, repo = gitlab_match.groups()
            repo = repo.rstrip(".git")
            if version:
                return (
                    f"https://gitlab.com/{owner}/{repo}"
                    f"/-/archive/{quote(version)}/{repo}-{quote(version)}.tar.gz"
                )
            return f"https://gitlab.com/{owner}/{repo}/-/archive/main/{repo}-main.tar.gz"

        return None

    def _try_known_patterns(self, name: str, version: str) -> Optional[str]:
        """Try known URL patterns for well-known projects."""
        known_patterns = {
            "openssl": "https://www.openssl.org/source/openssl-{version}.tar.gz",
            "curl": "https://curl.se/download/curl-{version}.tar.gz",
            "zlib": "https://zlib.net/zlib-{version}.tar.gz",
            "libpng": "https://download.sourceforge.net/libpng/libpng-{version}.tar.gz",
            "libjpeg": "https://www.ijg.org/files/jpegsrc.v{version}.tar.gz",
            "sqlite": "https://www.sqlite.org/src/tarball/sqlite-src-{version}.tar.gz",
            "expat": "https://github.com/libexpat/libexpat/releases/download/R_{version_underscore}/expat-{version}.tar.gz",
            "libxml2": "https://download.gnome.org/sources/libxml2/{version_major_minor}/libxml2-{version}.tar.xz",
            "boost": "https://boostorg.jfrog.io/artifactory/main/release/{version}/source/boost_{version_underscore}.tar.gz",
            "linux": "https://cdn.kernel.org/pub/linux/kernel/v{version_major}.x/linux-{version}.tar.xz",
            "busybox": "https://busybox.net/downloads/busybox-{version}.tar.bz2",
            "glibc": "https://ftp.gnu.org/gnu/glibc/glibc-{version}.tar.gz",
            "binutils": "https://ftp.gnu.org/gnu/binutils/binutils-{version}.tar.gz",
            "gcc": "https://ftp.gnu.org/gnu/gcc/gcc-{version}/gcc-{version}.tar.gz",
            "bash": "https://ftp.gnu.org/gnu/bash/bash-{version}.tar.gz",
            "coreutils": "https://ftp.gnu.org/gnu/coreutils/coreutils-{version}.tar.xz",
            "gzip": "https://ftp.gnu.org/gnu/gzip/gzip-{version}.tar.gz",
            "tar": "https://ftp.gnu.org/gnu/tar/tar-{version}.tar.gz",
            "wget": "https://ftp.gnu.org/gnu/wget/wget-{version}.tar.gz",
        }

        if not version:
            return None

        name_lower = name.lower().strip()

        # Check exact match
        if name_lower in known_patterns:
            pattern = known_patterns[name_lower]
            version_parts = version.split(".")
            url = pattern.format(
                version=version,
                version_underscore=version.replace(".", "_"),
                version_major=version_parts[0] if version_parts else version,
                version_major_minor=".".join(version_parts[:2]) if len(version_parts) >= 2 else version,
            )
            try:
                check = self.session.head(url, timeout=10, allow_redirects=True)
                if check.status_code == 200:
                    return url
            except requests.RequestException:
                pass

        # Try GNU FTP pattern
        gnu_url = f"https://ftp.gnu.org/gnu/{name_lower}/{name_lower}-{version}.tar.gz"
        try:
            check = self.session.head(gnu_url, timeout=10, allow_redirects=True)
            if check.status_code == 200:
                return gnu_url
        except requests.RequestException:
            pass

        return None

    def _search_sourceforge(self, name: str, version: str) -> Optional[str]:
        """Search SourceForge for the package."""
        url = f"https://sourceforge.net/projects/{quote(name.lower())}/files/"
        try:
            resp = self.session.head(url, timeout=10, allow_redirects=True)
            if resp.status_code == 200:
                if version:
                    return f"{url}{name}-{version}.tar.gz/download"
                return url
        except requests.RequestException:
            pass
        return None

    @staticmethod
    def _names_match(pkg_name: str, repo_name: str) -> bool:
        """Check if a package name matches a repository name (fuzzy)."""
        def normalize(s):
            return re.sub(r"[-_.\s]+", "", s.lower())

        return normalize(pkg_name) == normalize(repo_name)

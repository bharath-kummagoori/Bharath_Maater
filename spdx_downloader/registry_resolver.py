"""
Registry Resolver - Resolves download URLs from package manager registries.

Supports 20+ package ecosystems by querying their public APIs
to find source code download links when SPDX downloadLocation is missing.
"""

import logging
import re
import time
from typing import Optional
from urllib.parse import quote, unquote

import requests

logger = logging.getLogger(__name__)

# Default timeout for registry API calls (seconds)
API_TIMEOUT = 30
MAX_RETRIES = 3
RETRY_BACKOFF = 2  # seconds


class RegistryResolver:
    """Resolves source download URLs from package registry APIs."""

    def __init__(self, github_token: str = ""):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "SPDX-Source-Downloader/1.0",
            "Accept": "application/json",
        })
        self.github_token = github_token
        if github_token:
            self.session.headers["Authorization"] = f"token {github_token}"

        # Map ecosystem types to resolver methods
        self._resolvers = {
            "maven": self._resolve_maven,
            "npm": self._resolve_npm,
            "pypi": self._resolve_pypi,
            "nuget": self._resolve_nuget,
            "golang": self._resolve_golang,
            "cargo": self._resolve_cargo,
            "gem": self._resolve_rubygems,
            "composer": self._resolve_composer,
            "pub": self._resolve_pub,
            "conan": self._resolve_conan,
            "conda": self._resolve_conda,
            "cocoapods": self._resolve_cocoapods,
            "swift": self._resolve_swift,
            "hex": self._resolve_hex,
            "hackage": self._resolve_hackage,
            "cran": self._resolve_cran,
            "cpan": self._resolve_cpan,
            "github": self._resolve_github,
            "bitbucket": self._resolve_bitbucket,
            "deb": self._resolve_debian,
            "rpm": self._resolve_rpm,
            "apk": self._resolve_alpine,
            "docker": self._resolve_docker,
        }

    def resolve(self, purl: str) -> Optional[str]:
        """Resolve a PURL to a source download URL."""
        parsed = self._parse_purl(purl)
        if not parsed:
            logger.warning(f"Failed to parse PURL: {purl}")
            return None

        pkg_type = parsed["type"]
        resolver = self._resolvers.get(pkg_type)

        if not resolver:
            logger.warning(f"No resolver for ecosystem: {pkg_type}")
            return None

        logger.info(f"Resolving {pkg_type} package: {parsed['name']}@{parsed['version']}")

        try:
            url = resolver(parsed)
            if url:
                logger.info(f"Resolved download URL: {url}")
            else:
                logger.warning(f"Could not resolve URL for {purl}")
            return url
        except Exception as e:
            logger.error(f"Error resolving {purl}: {e}")
            return None

    def resolve_by_name(self, name: str, version: str, ecosystem: str) -> Optional[str]:
        """Resolve a download URL using package name, version, and ecosystem."""
        resolver = self._resolvers.get(ecosystem.lower())
        if not resolver:
            logger.warning(f"No resolver for ecosystem: {ecosystem}")
            return None

        parsed = {
            "type": ecosystem.lower(),
            "namespace": "",
            "name": name,
            "version": version,
            "qualifiers": {},
            "subpath": "",
        }

        # Try to infer namespace for Maven-style packages
        if ecosystem.lower() == "maven" and ":" in name:
            parts = name.split(":")
            parsed["namespace"] = parts[0]
            parsed["name"] = parts[1]

        try:
            return resolver(parsed)
        except Exception as e:
            logger.error(f"Error resolving {name}@{version} ({ecosystem}): {e}")
            return None

    def _api_get(self, url: str, **kwargs) -> Optional[requests.Response]:
        """Make an API GET request with retry logic."""
        for attempt in range(MAX_RETRIES):
            try:
                resp = self.session.get(url, timeout=API_TIMEOUT, **kwargs)
                if resp.status_code == 200:
                    return resp
                if resp.status_code == 429:
                    wait = RETRY_BACKOFF * (2 ** attempt)
                    logger.warning(f"Rate limited, waiting {wait}s...")
                    time.sleep(wait)
                    continue
                if resp.status_code == 404:
                    logger.debug(f"Not found: {url}")
                    return None
                logger.warning(f"HTTP {resp.status_code} for {url}")
                return None
            except requests.RequestException as e:
                if attempt < MAX_RETRIES - 1:
                    wait = RETRY_BACKOFF * (2 ** attempt)
                    logger.warning(f"Request failed, retrying in {wait}s: {e}")
                    time.sleep(wait)
                else:
                    logger.error(f"Request failed after {MAX_RETRIES} retries: {e}")
        return None

    @staticmethod
    def _parse_purl(purl: str) -> Optional[dict]:
        """Parse a Package URL (PURL) into its components.

        Format: pkg:<type>/<namespace>/<name>@<version>?<qualifiers>#<subpath>
        """
        if not purl or not purl.startswith("pkg:"):
            return None

        try:
            body = purl[4:]  # Remove 'pkg:'

            # Split subpath
            subpath = ""
            if "#" in body:
                body, subpath = body.rsplit("#", 1)

            # Split qualifiers
            qualifiers = {}
            if "?" in body:
                body, qs = body.split("?", 1)
                for pair in qs.split("&"):
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        qualifiers[k] = unquote(v)

            # Split version
            version = ""
            if "@" in body:
                body, version = body.rsplit("@", 1)

            # Split type
            pkg_type, rest = body.split("/", 1)

            # Split namespace and name
            parts = rest.split("/")
            if len(parts) > 1:
                namespace = "/".join(parts[:-1])
                name = parts[-1]
            else:
                namespace = ""
                name = parts[0]

            return {
                "type": unquote(pkg_type).lower(),
                "namespace": unquote(namespace),
                "name": unquote(name),
                "version": unquote(version),
                "qualifiers": qualifiers,
                "subpath": unquote(subpath),
            }
        except (ValueError, IndexError) as e:
            logger.error(f"PURL parse error for '{purl}': {e}")
            return None

    # =========================================================================
    # Package Manager Resolvers
    # =========================================================================

    def _resolve_maven(self, parsed: dict) -> Optional[str]:
        """Resolve Maven/Gradle package from Maven Central."""
        group_id = parsed["namespace"]
        artifact_id = parsed["name"]
        version = parsed["version"]

        if not all([group_id, artifact_id, version]):
            return None

        group_path = group_id.replace(".", "/")

        # Try sources JAR first
        sources_url = (
            f"https://repo1.maven.org/maven2/{group_path}/{artifact_id}"
            f"/{version}/{artifact_id}-{version}-sources.jar"
        )
        resp = self._api_get(sources_url, stream=True)
        if resp:
            resp.close()
            return sources_url

        # Fall back to regular JAR
        jar_url = (
            f"https://repo1.maven.org/maven2/{group_path}/{artifact_id}"
            f"/{version}/{artifact_id}-{version}.jar"
        )
        return jar_url

    def _resolve_npm(self, parsed: dict) -> Optional[str]:
        """Resolve NPM package from npmjs.org registry."""
        name = parsed["name"]
        if parsed["namespace"]:
            name = f"@{parsed['namespace']}/{name}"
        version = parsed["version"]

        url = f"https://registry.npmjs.org/{quote(name, safe='@/')}"
        resp = self._api_get(url)
        if not resp:
            return None

        data = resp.json()

        # Try to get specific version tarball
        if version and version in data.get("versions", {}):
            dist = data["versions"][version].get("dist", {})
            tarball = dist.get("tarball")
            if tarball:
                return tarball

        # Try the latest version if no specific version
        dist_tags = data.get("dist-tags", {})
        latest = dist_tags.get("latest", "")
        if latest and latest in data.get("versions", {}):
            dist = data["versions"][latest].get("dist", {})
            return dist.get("tarball")

        return None

    def _resolve_pypi(self, parsed: dict) -> Optional[str]:
        """Resolve Python package from PyPI."""
        name = parsed["name"]
        version = parsed["version"]

        url = f"https://pypi.org/pypi/{quote(name)}/{quote(version)}/json" if version else f"https://pypi.org/pypi/{quote(name)}/json"
        resp = self._api_get(url)
        if not resp:
            return None

        data = resp.json()
        urls = data.get("urls", [])

        # Prefer sdist (source distribution)
        for u in urls:
            if u.get("packagetype") == "sdist":
                return u.get("url")

        # Fall back to first available
        if urls:
            return urls[0].get("url")

        return None

    def _resolve_nuget(self, parsed: dict) -> Optional[str]:
        """Resolve .NET package from NuGet.org."""
        name = parsed["name"].lower()
        version = parsed["version"].lower()

        if not version:
            # Get latest version
            url = f"https://api.nuget.org/v3/registration5-gz-semver2/{name}/index.json"
            resp = self._api_get(url)
            if resp:
                data = resp.json()
                items = data.get("items", [])
                if items:
                    last_page = items[-1]
                    page_items = last_page.get("items", [])
                    if page_items:
                        version = page_items[-1].get("catalogEntry", {}).get("version", "")

        if version:
            return (
                f"https://api.nuget.org/v3-flatcontainer/{name}/{version}"
                f"/{name}.{version}.nupkg"
            )
        return None

    def _resolve_golang(self, parsed: dict) -> Optional[str]:
        """Resolve Go module from Go Module Proxy."""
        module_path = parsed["name"]
        if parsed["namespace"]:
            module_path = f"{parsed['namespace']}/{module_path}"
        version = parsed["version"]

        if not version:
            # Get latest version
            url = f"https://proxy.golang.org/{quote(module_path, safe='')}/@latest"
            resp = self._api_get(url)
            if resp:
                version = resp.json().get("Version", "")

        if version:
            # Ensure version starts with 'v'
            if not version.startswith("v"):
                version = f"v{version}"
            return (
                f"https://proxy.golang.org/{quote(module_path, safe='')}"
                f"/@v/{quote(version, safe='')}.zip"
            )
        return None

    def _resolve_cargo(self, parsed: dict) -> Optional[str]:
        """Resolve Rust crate from crates.io."""
        name = parsed["name"]
        version = parsed["version"]

        url = f"https://crates.io/api/v1/crates/{quote(name)}"
        if version:
            url += f"/{quote(version)}"

        resp = self._api_get(url)
        if not resp:
            return None

        data = resp.json()

        if version:
            dl_path = data.get("version", {}).get("dl_path")
            if dl_path:
                return f"https://crates.io{dl_path}"
        else:
            crate = data.get("crate", {})
            max_version = crate.get("max_version", "")
            if max_version:
                return (
                    f"https://crates.io/api/v1/crates/{quote(name)}"
                    f"/{quote(max_version)}/download"
                )

        return None

    def _resolve_rubygems(self, parsed: dict) -> Optional[str]:
        """Resolve Ruby gem from RubyGems.org."""
        name = parsed["name"]
        version = parsed["version"]

        if version:
            return f"https://rubygems.org/downloads/{name}-{version}.gem"

        url = f"https://rubygems.org/api/v1/gems/{quote(name)}.json"
        resp = self._api_get(url)
        if resp:
            data = resp.json()
            ver = data.get("version", "")
            if ver:
                return f"https://rubygems.org/downloads/{name}-{ver}.gem"
        return None

    def _resolve_composer(self, parsed: dict) -> Optional[str]:
        """Resolve PHP package from Packagist."""
        vendor = parsed["namespace"]
        name = parsed["name"]
        version = parsed["version"]

        if not vendor:
            return None

        url = f"https://repo.packagist.org/p2/{quote(vendor)}/{quote(name)}.json"
        resp = self._api_get(url)
        if not resp:
            return None

        data = resp.json()
        packages = data.get("packages", {}).get(f"{vendor}/{name}", [])

        for pkg in packages:
            pkg_version = pkg.get("version", "").lstrip("v")
            target_version = version.lstrip("v") if version else ""

            if not target_version or pkg_version == target_version:
                dist = pkg.get("dist", {})
                dist_url = dist.get("url")
                if dist_url:
                    return dist_url

        return None

    def _resolve_pub(self, parsed: dict) -> Optional[str]:
        """Resolve Dart/Flutter package from pub.dev."""
        name = parsed["name"]
        version = parsed["version"]

        if version:
            return f"https://pub.dev/api/archives/{quote(name)}-{quote(version)}.tar.gz"

        url = f"https://pub.dev/api/packages/{quote(name)}"
        resp = self._api_get(url)
        if resp:
            data = resp.json()
            latest = data.get("latest", {}).get("version", "")
            if latest:
                return f"https://pub.dev/api/archives/{quote(name)}-{quote(latest)}.tar.gz"
        return None

    def _resolve_conan(self, parsed: dict) -> Optional[str]:
        """Resolve C/C++ Conan package from ConanCenter."""
        name = parsed["name"]
        version = parsed["version"]

        # Conan Center Index on GitHub is the main source
        if version:
            return (
                f"https://github.com/conan-io/conan-center-index/archive/"
                f"refs/heads/master.zip"
            )
        return None

    def _resolve_conda(self, parsed: dict) -> Optional[str]:
        """Resolve Conda package from Anaconda.org."""
        name = parsed["name"]
        version = parsed["version"]
        channel = parsed["namespace"] or "conda-forge"

        url = f"https://api.anaconda.org/package/{quote(channel)}/{quote(name)}"
        resp = self._api_get(url)
        if not resp:
            return None

        data = resp.json()
        source_url = data.get("source_git_url") or data.get("dev_url")
        if source_url:
            return source_url

        # Try to get specific file
        files = data.get("files", [])
        for f in files:
            if version and version in f.get("version", ""):
                download_url = f.get("download_url")
                if download_url:
                    return f"https://anaconda.org{download_url}"

        return None

    def _resolve_cocoapods(self, parsed: dict) -> Optional[str]:
        """Resolve iOS/macOS CocoaPod."""
        name = parsed["name"]
        version = parsed["version"]

        url = f"https://cdn.cocoapods.org/all_pods_versions_{name[0].lower()}.txt"
        # CocoaPods doesn't have a straightforward API; use specs repo
        specs_url = f"https://raw.githubusercontent.com/CocoaPods/Specs/master/Specs/{name[0].lower()}/{name}/{version}/{name}.podspec.json"
        resp = self._api_get(specs_url)
        if resp:
            data = resp.json()
            source = data.get("source", {})
            git_url = source.get("git", "")
            tag = source.get("tag", version)
            if git_url:
                # Convert to archive URL if GitHub
                if "github.com" in git_url:
                    git_url = git_url.rstrip(".git")
                    return f"{git_url}/archive/refs/tags/{quote(tag)}.tar.gz"
                return git_url
        return None

    def _resolve_swift(self, parsed: dict) -> Optional[str]:
        """Resolve Swift package (typically GitHub-based)."""
        namespace = parsed["namespace"]
        name = parsed["name"]
        version = parsed["version"]

        if namespace:
            base = f"https://github.com/{namespace}/{name}"
            if version:
                return f"{base}/archive/refs/tags/{quote(version)}.tar.gz"
            return base
        return None

    def _resolve_hex(self, parsed: dict) -> Optional[str]:
        """Resolve Erlang/Elixir package from Hex.pm."""
        name = parsed["name"]
        version = parsed["version"]

        if version:
            return f"https://repo.hex.pm/tarballs/{name}-{version}.tar"

        url = f"https://hex.pm/api/packages/{quote(name)}"
        resp = self._api_get(url)
        if resp:
            data = resp.json()
            releases = data.get("releases", [])
            if releases:
                latest = releases[0].get("version", "")
                if latest:
                    return f"https://repo.hex.pm/tarballs/{name}-{latest}.tar"
        return None

    def _resolve_hackage(self, parsed: dict) -> Optional[str]:
        """Resolve Haskell package from Hackage."""
        name = parsed["name"]
        version = parsed["version"]

        if version:
            return f"https://hackage.haskell.org/package/{name}-{version}/{name}-{version}.tar.gz"

        url = f"https://hackage.haskell.org/package/{quote(name)}/preferred.json"
        resp = self._api_get(url)
        if resp:
            data = resp.json()
            versions = data.get("normal-version", [])
            if versions:
                latest = versions[0]
                return f"https://hackage.haskell.org/package/{name}-{latest}/{name}-{latest}.tar.gz"
        return None

    def _resolve_cran(self, parsed: dict) -> Optional[str]:
        """Resolve R package from CRAN."""
        name = parsed["name"]
        version = parsed["version"]

        if version:
            return f"https://cran.r-project.org/src/contrib/{name}_{version}.tar.gz"

        # Try current version
        return f"https://cran.r-project.org/src/contrib/{name}_latest.tar.gz"

    def _resolve_cpan(self, parsed: dict) -> Optional[str]:
        """Resolve Perl module from CPAN/MetaCPAN."""
        name = parsed["name"]
        version = parsed["version"]

        # MetaCPAN API
        module_name = name.replace("-", "::")
        url = f"https://fastapi.metacpan.org/release/{quote(name)}"
        resp = self._api_get(url)
        if resp:
            data = resp.json()
            download_url = data.get("download_url")
            if download_url:
                return download_url
        return None

    def _resolve_github(self, parsed: dict) -> Optional[str]:
        """Resolve GitHub repository source archive."""
        owner = parsed["namespace"]
        repo = parsed["name"]
        version = parsed["version"]

        if not owner or not repo:
            return None

        if version:
            return f"https://github.com/{owner}/{repo}/archive/refs/tags/{quote(version)}.tar.gz"
        return f"https://github.com/{owner}/{repo}/archive/refs/heads/main.tar.gz"

    def _resolve_bitbucket(self, parsed: dict) -> Optional[str]:
        """Resolve Bitbucket repository source archive."""
        owner = parsed["namespace"]
        repo = parsed["name"]
        version = parsed["version"]

        if not owner or not repo:
            return None

        if version:
            return f"https://bitbucket.org/{owner}/{repo}/get/{quote(version)}.tar.gz"
        return f"https://bitbucket.org/{owner}/{repo}/get/main.tar.gz"

    def _resolve_debian(self, parsed: dict) -> Optional[str]:
        """Resolve Debian package source."""
        name = parsed["name"]
        version = parsed["version"]

        # Use Debian snapshot API
        if version:
            url = f"https://snapshot.debian.org/mr/package/{quote(name)}/{quote(version)}/srcfiles"
            resp = self._api_get(url)
            if resp:
                data = resp.json()
                result = data.get("result", [])
                for f in result:
                    file_hash = f.get("hash", "")
                    if file_hash:
                        return f"https://snapshot.debian.org/file/{file_hash}"

        # Fall back to current source
        prefix = name[:4] if name.startswith("lib") else name[0]
        return f"https://deb.debian.org/debian/pool/main/{prefix}/{name}/"

    def _resolve_rpm(self, parsed: dict) -> Optional[str]:
        """Resolve RPM package source."""
        name = parsed["name"]
        version = parsed["version"]

        # Fedora src.rpm
        if version:
            return (
                f"https://src.fedoraproject.org/rpms/{quote(name)}/archive/"
                f"{quote(version)}/{name}-{version}.tar.gz"
            )
        return f"https://src.fedoraproject.org/rpms/{quote(name)}"

    def _resolve_alpine(self, parsed: dict) -> Optional[str]:
        """Resolve Alpine Linux package source."""
        name = parsed["name"]
        return f"https://git.alpinelinux.org/aports/tree/main/{name}"

    def _resolve_docker(self, parsed: dict) -> Optional[str]:
        """Docker images don't have source downloads - return Dockerfile if available."""
        namespace = parsed["namespace"] or "library"
        name = parsed["name"]
        return f"https://hub.docker.com/r/{namespace}/{name}"

    def get_supported_ecosystems(self) -> list[str]:
        """Return a list of supported package ecosystems."""
        return sorted(self._resolvers.keys())

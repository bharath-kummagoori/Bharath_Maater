"""
SPDX JSON Parser - Extracts package information from SPDX 2.x JSON files.

Parses SPDX JSON (ISO/IEC 5962:2021) and extracts:
- Package name, version, supplier
- Download location
- External references (PURL, CPE, etc.)
- Checksums
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class PackageInfo:
    """Represents a parsed package from an SPDX document."""
    name: str
    version: str
    spdx_id: str = ""
    supplier: str = ""
    originator: str = ""
    download_location: str = ""
    homepage: str = ""
    purl: str = ""
    cpe: str = ""
    checksums: dict = field(default_factory=dict)
    license_concluded: str = ""
    license_declared: str = ""
    external_refs: list = field(default_factory=list)

    @property
    def has_download_link(self) -> bool:
        """Check if a valid download link is available."""
        invalid = {"NOASSERTION", "NONE", ""}
        return self.download_location not in invalid

    @property
    def ecosystem(self) -> str:
        """Detect the package ecosystem from PURL."""
        if self.purl:
            # purl format: pkg:<type>/<namespace>/<name>@<version>
            try:
                scheme_body = self.purl.split("pkg:")[1]
                pkg_type = scheme_body.split("/")[0]
                return pkg_type.lower()
            except (IndexError, AttributeError):
                pass
        return ""

    def __str__(self) -> str:
        return f"{self.name}@{self.version} [{self.ecosystem or 'unknown'}]"


class SPDXParser:
    """Parses SPDX 2.x JSON files and extracts package information."""

    def __init__(self, file_path: str):
        self.file_path = Path(file_path)
        self.document_name = ""
        self.document_namespace = ""
        self.spdx_version = ""
        self.packages: list[PackageInfo] = []

    def parse(self) -> list[PackageInfo]:
        """Parse the SPDX JSON file and return a list of PackageInfo objects."""
        if not self.file_path.exists():
            raise FileNotFoundError(f"SPDX file not found: {self.file_path}")

        if not self.file_path.suffix.lower() == ".json":
            raise ValueError(f"Expected JSON file, got: {self.file_path.suffix}")

        logger.info(f"Parsing SPDX file: {self.file_path}")

        with open(self.file_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        self._parse_document_info(data)
        self._parse_packages(data)

        logger.info(
            f"Parsed {len(self.packages)} packages from '{self.document_name}'"
        )
        return self.packages

    def _parse_document_info(self, data: dict):
        """Extract document-level metadata."""
        self.spdx_version = data.get("spdxVersion", "")
        self.document_name = data.get("name", "")
        self.document_namespace = data.get("documentNamespace", "")

        if not self.spdx_version.startswith("SPDX-2"):
            logger.warning(
                f"Unexpected SPDX version: {self.spdx_version}. "
                "This parser is designed for SPDX 2.x."
            )

    def _parse_packages(self, data: dict):
        """Extract all packages from the SPDX document."""
        packages_data = data.get("packages", [])

        for pkg_data in packages_data:
            pkg = self._parse_single_package(pkg_data)
            if pkg:
                self.packages.append(pkg)

    def _parse_single_package(self, pkg_data: dict) -> Optional[PackageInfo]:
        """Parse a single package entry from the SPDX JSON."""
        name = pkg_data.get("name", "")
        version = pkg_data.get("versionInfo", "")

        if not name:
            logger.warning("Skipping package with no name")
            return None

        pkg = PackageInfo(
            name=name,
            version=version,
            spdx_id=pkg_data.get("SPDXID", ""),
            supplier=pkg_data.get("supplier", ""),
            originator=pkg_data.get("originator", ""),
            download_location=pkg_data.get("downloadLocation", ""),
            homepage=pkg_data.get("homepage", ""),
            license_concluded=pkg_data.get("licenseConcluded", ""),
            license_declared=pkg_data.get("licenseDeclared", ""),
        )

        # Parse checksums
        for cs in pkg_data.get("checksums", []):
            algo = cs.get("algorithm", "").upper()
            value = cs.get("checksumValue", "")
            if algo and value:
                pkg.checksums[algo] = value

        # Parse external references (PURL, CPE, etc.)
        for ref in pkg_data.get("externalRefs", []):
            ref_type = ref.get("referenceType", "")
            ref_locator = ref.get("referenceLocator", "")
            pkg.external_refs.append(
                {"category": ref.get("referenceCategory", ""),
                 "type": ref_type,
                 "locator": ref_locator}
            )

            if ref_type == "purl":
                pkg.purl = ref_locator
            elif ref_type in ("cpe23Type", "cpe22Type"):
                pkg.cpe = ref_locator

        return pkg

    def get_summary(self) -> dict:
        """Return a summary of parsed packages."""
        ecosystems = {}
        with_download = 0
        without_download = 0

        for pkg in self.packages:
            eco = pkg.ecosystem or "unknown"
            ecosystems[eco] = ecosystems.get(eco, 0) + 1
            if pkg.has_download_link:
                with_download += 1
            else:
                without_download += 1

        return {
            "document_name": self.document_name,
            "total_packages": len(self.packages),
            "with_download_link": with_download,
            "without_download_link": without_download,
            "ecosystems": ecosystems,
        }

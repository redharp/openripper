from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

KNOWN_UHD_PRODUCTS = {
    "BP60NB10",
    "BP50NB40",
    "WP50NB40",
    "WH16NS60",
    "BU40N",
    "WH14NS40",
    "WH16NS40",
    "BW-16D1HT",
    "BW-16D1HT PRO",
    "BH16NS55",
    "BRUHD-PU3",
    "BDR-XD08UMB-S",
    "BDR-XD07UHD",
    "BDR-XD06JUHD",
    "BDR-S12UHT",
    "BDR-XS07UHD",
    "BDR-212UBK",
    "BDR-211UBK",
    "BDR-UD04",
    "BDR-S13U-X",
    "BDR-S13UBK",
}

# MediaTek MT1959 is the LG/ASUS/Buffalo chipset that LibreDrive unlocks for UHD.
UHD_PLATFORMS = {"MT1959"}


@dataclass(slots=True)
class CompatibilityInfo:
    manufacturer: str = ""
    product: str = ""
    revision: str = ""
    platform: str = ""
    firmware_version: str = ""
    libredrive_status: str = ""
    bd_raw_data_read: bool = False
    status: str = "unknown"
    message: str = "Compatibility has not been checked yet"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _yes(value: str) -> bool:
    return value.strip().casefold() == "yes"


def _parse_sdf_info(output: str) -> dict[str, str]:
    sections: dict[str, dict[str, str]] = {
        "drive": {},
        "identification": {},
    }
    section = ""
    pending_key = ""
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line == "[Drive Specific SDF] Embedded Info Strings:":
            section = "drive"
            pending_key = ""
            continue
        if line == "[Identification SDF] Embedded Info Strings:":
            section = "identification"
            pending_key = ""
            continue
        if line.startswith("["):
            section = ""
            pending_key = ""
            continue
        if not section:
            continue
        match = re.match(r"^(\d{4})?:(.*)$", line)
        if not match:
            continue
        code, value = match.groups()
        value = value.strip()
        if code and code.startswith("80"):
            pending_key = "" if code == "8000" else value.casefold()
        elif pending_key and ((code and code.startswith("81")) or not code):
            sections[section][pending_key] = value
            pending_key = ""
    return sections["drive"] or sections["identification"]


def parse_compatibility_info(
    output: str,
    *,
    manufacturer: str = "",
    product: str = "",
    revision: str = "",
) -> CompatibilityInfo:
    values: dict[str, str] = {}
    libre_values: dict[str, str] = {}
    in_libredrive = False
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.casefold().startswith("libredrive information"):
            in_libredrive = True
            continue
        if line.casefold().startswith(("disc information", "no disc inserted")):
            in_libredrive = False
        if ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        target = libre_values if in_libredrive else values
        target[key.casefold()] = value

    libre_values.update(_parse_sdf_info(output))
    info = CompatibilityInfo(
        manufacturer=values.get("manufacturer", manufacturer),
        product=values.get("product", product),
        revision=values.get("revision", revision),
        platform=libre_values.get("drive platform", ""),
        firmware_version=libre_values.get("firmware version", ""),
        libredrive_status=libre_values.get("status", ""),
        bd_raw_data_read=_yes(libre_values.get("bd raw data read", "")),
    )
    info.status, info.message = classify(info)
    return info


def classify(info: CompatibilityInfo) -> tuple[str, str]:
    """Summarize what a drive can rip. Every recognized drive rips Blu-ray and DVD."""
    product = info.product.upper()
    uhd_capable = info.platform in UHD_PLATFORMS or any(
        model in product for model in KNOWN_UHD_PRODUCTS
    )
    status = info.libredrive_status.casefold()
    enabled = "enabled" in status and "not yet enabled" not in status
    if uhd_capable and enabled and info.bd_raw_data_read:
        return "ready", "Rips 4K UHD, Blu-ray and DVD"
    if uhd_capable:
        return (
            "needs_firmware",
            "UHD-capable drive, but LibreDrive isn't enabled on this firmware. "
            "Blu-ray and DVD rip normally.",
        )
    if info.product or info.libredrive_status:
        return "standard", "Rips Blu-ray and DVD. Not a known 4K UHD drive."
    return "unknown", "MakeMKV didn't report enough about this drive"

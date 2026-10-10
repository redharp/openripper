from openripper.compatibility import parse_compatibility_info

LG_INFO = """
Drive Information
OS device name: /dev/sr0
Manufacturer: HL-DT-ST
Product: BD-RE WH16NS40
Revision: 1.05
Serial number: KLIJ123456
Firmware date: 2120-05-06 11:42
Bus encryption flags: 17

LibreDrive Information
Status: Possible, not yet enabled
Drive platform: MT1959
Firmware type: Original (unpatched)
Firmware version: 1.05
DVD all regions: Possible
BD raw data read: Possible
BD raw metadata read: Possible
Unrestricted read speed: Possible
"""

PIONEER_INFO = """
Drive Information
Manufacturer: PIONEER
Product: BD-RW BDR-XD08
Revision: 1.02
Firmware date: 2022-12-27

LibreDrive Information
Status: Enabled
Drive platform: RS9330
Firmware type: Original (unpatched)
Firmware version: 1.02
BD raw data read: Yes
BD raw metadata read: Yes
"""

SDF_TOOL_INFO = """
[Drive Specific SDF] Embedded Info Strings:
8000:LibreDrive Information
8013:Status
8105:Enabled
8001:Drive platform
:MT1959
8002:Firmware type
8107:Patched (microcode access re-enabled)
8003:Firmware version
:1.03
8006:BD raw data read
8100:Yes
8007:BD raw metadata read
8100:Yes
8009:Unrestricted read speed
8100:Yes

[Identification SDF] Embedded Info Strings:
8000:LibreDrive Information
8013:Status
8102:Possible, not yet enabled
8001:Drive platform
:MT1959
"""


def test_known_uhd_drive_without_libredrive_needs_firmware() -> None:
    info = parse_compatibility_info(LG_INFO)
    assert info.libredrive_status == "Possible, not yet enabled"
    assert info.firmware_version == "1.05"
    assert info.status == "needs_firmware"
    assert "Blu-ray and DVD rip normally" in info.message


def test_drive_outside_uhd_list_is_standard_even_with_libredrive() -> None:
    info = parse_compatibility_info(PIONEER_INFO)
    assert info.libredrive_status == "Enabled"
    assert info.status == "standard"


def test_sdf_output_prefers_drive_specific_section_and_reports_ready() -> None:
    info = parse_compatibility_info(
        SDF_TOOL_INFO,
        manufacturer="HL-DT-ST",
        product="BD-RE BU40N",
        revision="1.03",
    )
    assert info.firmware_version == "1.03"
    assert info.libredrive_status == "Enabled"
    assert info.bd_raw_data_read is True
    assert info.status == "ready"


def test_empty_output_is_unknown() -> None:
    assert parse_compatibility_info("").status == "unknown"


# Real Windows MakeMKV 1.18.3 output for a Buffalo (LG MT1959) drive: SDF strings only,
# no Manufacturer/Product lines.
BUFFALO_WINDOWS_INFO = """
SDF.bin version: 0x00A6

Drive Tool SDF present

Drive Specific SDF present

[Drive Specific SDF] Embedded Info Strings:
8000:LibreDrive Information

8013:Status
8105:Enabled

8001:Drive platform
:MT1959

8002:Firmware type
8107:Patched (microcode access re-enabled)

8003:Firmware version
:1.03

8006:BD raw data read
8100:Yes
"""


def test_mt1959_with_libredrive_is_ready_without_a_product_name() -> None:
    info = parse_compatibility_info(BUFFALO_WINDOWS_INFO)
    assert info.product == ""
    assert info.platform == "MT1959"
    assert info.status == "ready"


def test_not_yet_enabled_is_not_treated_as_enabled() -> None:
    info = parse_compatibility_info(
        LG_INFO.replace("BD raw data read: Possible", "BD raw data read: Yes")
    )
    assert info.status == "needs_firmware"

from openripper.discovery import parse_blkid_label, parse_windows_media


def test_parse_blkid_label() -> None:
    output = '/dev/sr0: LABEL="THE_BATMAN" TYPE="udf"\n'

    assert parse_blkid_label(output) == "THE_BATMAN"
    assert parse_blkid_label('/dev/sr2: TYPE="udf"\n') == ""


def test_parse_windows_media_keys_by_drive_letter() -> None:
    output = "F:|UltramanACE\r\ng:|\r\nnot a drive\r\n"

    assert parse_windows_media(output) == {"F:": "UltramanACE", "G:": "DISC_G"}

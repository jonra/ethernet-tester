from pathlib import Path

import pytest

from ethtester.config import Config, load_config


def test_missing_file_gives_defaults(tmp_path: Path):
    assert load_config(tmp_path / "nope.toml") == Config()


def test_values_override_defaults(tmp_path: Path):
    path = tmp_path / "c.toml"
    path.write_text('display = "V3"\nexpected_speed = 100\ninterval = 2\n')
    cfg = load_config(path)
    assert cfg.display == "V3"
    assert cfg.expected_speed == 100
    assert cfg.interval == 2
    assert cfg.interface == "eth0"


def test_unknown_keys_are_ignored(tmp_path: Path):
    path = tmp_path / "c.toml"
    path.write_text('colour = "red"\n')
    assert load_config(path) == Config()


def test_invalid_display_rejected(tmp_path: Path):
    path = tmp_path / "c.toml"
    path.write_text('display = "V9"\n')
    with pytest.raises(ValueError, match="display"):
        load_config(path)


def test_invalid_toml_is_value_error(tmp_path: Path):
    path = tmp_path / "c.toml"
    path.write_text('interval = \n')
    with pytest.raises(ValueError, match="invalid TOML"):
        load_config(path)


def test_wrong_type_names_the_key(tmp_path: Path):
    path = tmp_path / "c.toml"
    path.write_text('expected_speed = "fast"\n')
    with pytest.raises(ValueError, match="expected_speed"):
        load_config(path)


def test_list_for_string_rejected(tmp_path: Path):
    path = tmp_path / "c.toml"
    path.write_text('interface = ["eth0"]\n')
    with pytest.raises(ValueError, match="interface"):
        load_config(path)


def test_string_number_is_coerced(tmp_path: Path):
    path = tmp_path / "c.toml"
    path.write_text('expected_speed = "1000"\ninterval = 2\n')
    cfg = load_config(path)
    assert cfg.expected_speed == 1000 and isinstance(cfg.expected_speed, int)
    assert cfg.interval == 2.0 and isinstance(cfg.interval, float)


@pytest.mark.parametrize("line, key", [
    ("interval = -1", "interval"),
    ("interval = 0", "interval"),
    ("dhcp_timeout = 0", "dhcp_timeout"),
    ("ping_count = 0", "ping_count"),
    ("expected_speed = 0", "expected_speed"),
])
def test_out_of_range_values_rejected(tmp_path: Path, line, key):
    path = tmp_path / "c.toml"
    path.write_text(line + "\n")
    with pytest.raises(ValueError, match=key):
        load_config(path)


def test_utf8_bom_is_accepted(tmp_path: Path):
    path = tmp_path / "c.toml"
    path.write_bytes(b"\xef\xbb\xbf" + b'display = "V3"\n')
    assert load_config(path).display == "V3"


def test_mcp_clients_default_and_values(tmp_path: Path):
    assert Config().mcp_clients == "tailscale"
    path = tmp_path / "ethtester.toml"
    path.write_text('mcp_clients = "lan"\nmcp_bind = "wlan0"\n')
    cfg = load_config(path)
    assert cfg.mcp_clients == "lan" and cfg.mcp_bind == "wlan0"


def test_invalid_mcp_clients_rejected(tmp_path: Path):
    path = tmp_path / "ethtester.toml"
    path.write_text('mcp_clients = "world"\n')
    with pytest.raises(ValueError, match="mcp_clients"):
        load_config(path)


def test_activity_path_default(tmp_path: Path):
    assert Config().activity_path == "/run/ethtester/activity.json"
    path = tmp_path / "c.toml"
    path.write_text(f'activity_path = "{tmp_path / "a.json"}"\n')
    assert load_config(path).activity_path == str(tmp_path / "a.json")


def test_mcp_auth_default_and_values(tmp_path: Path):
    assert Config().mcp_auth == "token"
    path = tmp_path / "ethtester.toml"
    path.write_text('mcp_auth = "none"\n')
    assert load_config(path).mcp_auth == "none"


def test_invalid_mcp_auth_rejected(tmp_path: Path):
    path = tmp_path / "ethtester.toml"
    path.write_text('mcp_auth = "basic"\n')
    with pytest.raises(ValueError, match="mcp_auth"):
        load_config(path)

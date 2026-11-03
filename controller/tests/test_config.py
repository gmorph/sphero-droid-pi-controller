from pathlib import Path

import pytest

from droid_controller.config import Config, ConfigError, config_from_dict, load_config, parse_action


def test_missing_file_gives_defaults(tmp_path: Path) -> None:
    config = load_config(tmp_path / "missing.toml")
    assert config == Config()
    assert config.gamepad.dead_zone == 0.15
    assert config.drive.max_speed == 180
    assert config.drive.speed_exponent == 1.5
    assert config.drive.update_rate == 10.0
    assert config.droid.name_prefixes == ()


def test_partial_file_overrides_only_given_keys(tmp_path: Path) -> None:
    path = tmp_path / "droid.toml"
    path.write_text(
        """
        [drive]
        max_speed = 120
        update_rate = 5

        [droid]
        name_prefixes = ["D2-"]
        main_led = [255, 0, 0]

        [r2]
        a = "sound:R2_BURNOUT"

        [logging]
        level = "debug"
        """
    )
    config = load_config(path)
    assert config.drive.max_speed == 120
    assert config.drive.update_rate == 5.0
    assert config.drive.speed_exponent == 1.5
    assert config.droid.name_prefixes == ("D2-",)
    assert config.droid.main_led == (255, 0, 0)
    assert config.r2.a == "sound:R2_BURNOUT"
    assert config.r2.b == "animation:EMOTE_NO"
    assert config.logging.level == "debug"


def test_example_config_matches_defaults() -> None:
    example = Path(__file__).resolve().parent.parent / "droid-controller.example.toml"
    assert load_config(example) == Config()


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"gamepads": {}}, "unknown section"),
        ({"drive": {"max_sped": 1}}, "unknown key"),
        ({"drive": {"max_speed": 300}}, "max_speed"),
        ({"drive": {"max_speed": 1.5}}, "must be an integer"),
        ({"drive": {"update_rate": "fast"}}, "must be a number"),
        ({"gamepad": {"dead_zone": 1.0}}, "dead_zone"),
        ({"gamepad": {"preferred_names": "Xbox"}}, "must be an array"),
        ({"droid": {"main_led": [0, 0]}}, "main_led"),
        ({"droid": {"reconnect_backoff_min": 60.0}}, "reconnect_backoff_min"),
        ({"r2": {"a": "EMOTE_YES"}}, "animation:NAME"),
        ({"r2": {"a": "dance:EMOTE_YES"}}, "animation:NAME"),
        ({"logging": {"level": "LOUD"}}, "logging level"),
        ({"drive": "fast"}, "must be a table"),
    ],
)
def test_invalid_values_are_rejected(data: dict, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        config_from_dict(data)


def test_malformed_toml_is_a_config_error(tmp_path: Path) -> None:
    path = tmp_path / "bad.toml"
    path.write_text("[drive\nmax_speed = 1")
    with pytest.raises(ConfigError):
        load_config(path)


def test_parse_action() -> None:
    assert parse_action("animation:EMOTE_YES") == ("animation", "EMOTE_YES")
    assert parse_action("sound:R2_SCREAM") == ("sound", "R2_SCREAM")
    with pytest.raises(ConfigError):
        parse_action("sound:")

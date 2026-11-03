from droid_controller.droid import is_droid_name


def test_real_droid_names_pass() -> None:
    for name in ("D2-F5CA", "Q5-B8D7", "BB-E13C", "GB-1A2B"):
        assert is_droid_name(name), name


def test_address_lookalikes_and_strangers_are_rejected() -> None:
    for name in ("D2-C7-8C-3C-92-D9", "D2-", "BB-8", "SK-1234", "", None):
        assert not is_droid_name(name), name

from droid_controller.droid import choose_droid


def test_prefers_listed_names_in_order() -> None:
    seen = ["BB-1234", "D2-F5CA", "D2-AAAA"]
    assert choose_droid(seen, ["D2-AAAA", "D2-F5CA"], [], None) == "D2-AAAA"
    assert choose_droid(seen, ["D2-F5CA"], [], None) == "D2-F5CA"


def test_names_list_excludes_unlisted_droids() -> None:
    assert choose_droid(["BB-1234"], ["D2-F5CA"], [], None) is None


def test_prefix_filter_applies_without_names() -> None:
    assert choose_droid(["BB-1234", "D2-F5CA"], [], ["D2-"], None) == "D2-F5CA"
    assert choose_droid(["BB-1234", "D2-F5CA"], [], [], None) == "BB-1234"


def test_released_droid_is_skipped_when_others_exist() -> None:
    assert choose_droid(["D2-F5CA", "BB-1234"], [], [], "D2-F5CA") == "BB-1234"


def test_released_droid_is_chosen_when_alone() -> None:
    assert choose_droid(["D2-F5CA"], [], [], "D2-F5CA") == "D2-F5CA"


def test_nothing_seen() -> None:
    assert choose_droid([], [], [], None) is None

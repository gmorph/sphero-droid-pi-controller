from droid_controller.menu import SLEEP_ITEM, DroidMenu


def test_menu_starts_with_the_current_droid_and_sleep() -> None:
    menu = DroidMenu("D2-F5CA")
    assert menu.items == ["D2-F5CA", SLEEP_ITEM]
    assert menu.scanning
    assert menu.labels()[0] == "D2-F5CA (this one)"


def test_scan_results_merge_in_order_without_duplicates() -> None:
    menu = DroidMenu("D2-F5CA")
    menu.add_seen(["Q5-B8D7", "D2-F5CA", "BB-E13C"])
    menu.add_seen(["Q5-B8D7"])
    assert menu.items == ["D2-F5CA", "Q5-B8D7", "BB-E13C", SLEEP_ITEM]
    assert not menu.scanning


def test_cursor_wraps_both_ways() -> None:
    menu = DroidMenu("D2-F5CA")
    menu.add_seen(["Q5-B8D7"])
    menu.move(-1)
    assert menu.selected == SLEEP_ITEM
    menu.move(1)
    assert menu.selected == "D2-F5CA"
    menu.move(2)
    assert menu.selected == SLEEP_ITEM

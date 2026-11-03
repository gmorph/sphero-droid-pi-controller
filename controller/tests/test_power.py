from droid_controller.droid import PowerInfo


def test_charging_states_mean_the_cable_is_in() -> None:
    assert PowerInfo("CHARGED", 4.14).charging
    assert PowerInfo("CHARGING", 3.9).charging
    assert not PowerInfo("NOT_CHARGING", 3.9).charging
    assert not PowerInfo("OK", 3.9).charging
    assert not PowerInfo("UNKNOWN", None).charging


def test_battery_label() -> None:
    assert PowerInfo("CHARGED", 4.14).battery == "4.1V"
    assert PowerInfo("LOW", 3.5).battery == "LOW"
    assert PowerInfo("OK", None).battery == "OK"


class _V2Toy:
    """R2-D2 style: separate state enum and voltage commands."""

    name = "D2-F5CA"

    def get_battery_state(self):
        from spherov2.commands.power import BatteryStates

        return BatteryStates.CHARGING

    def get_battery_voltage(self):
        return 4.08


class _V1Toy:
    """BB-8 style: one power-state record with its own state numbering."""

    name = "BB-E13C"

    def get_power_state(self):
        from spherov2.listeners.core import PowerState, PowerStates

        return PowerState(record_version=1, state=PowerStates(4), voltage=3.41, number_of_charges=57,
                          time_since_last_charge=120)


def test_read_power_on_an_r2() -> None:
    from droid_controller.droid import read_power

    info = read_power(_V2Toy())
    assert (info.state, info.voltage) == ("CHARGING", 4.08)
    assert info.charging


def test_read_power_on_a_bb8_uses_the_power_state_record() -> None:
    from droid_controller.droid import read_power

    info = read_power(_V1Toy())
    assert (info.state, info.voltage) == ("CRITICAL", 3.41)
    assert info.battery == "CRITICAL"
    assert not info.charging


def test_read_power_on_a_bb8_sees_the_charger() -> None:
    from spherov2.listeners.core import PowerState, PowerStates

    from droid_controller.droid import read_power

    class Charging(_V1Toy):
        def get_power_state(self):
            return PowerState(1, PowerStates.CHARGING, 4.1, 57, 0)

    assert read_power(Charging()).charging


def test_droid_charger_wording_follows_the_model() -> None:
    import threading

    from spherov2.toy.bb8 import BB8
    from spherov2.toy.r2d2 import R2D2

    from droid_controller.droid import _droid_for_toy
    from droid_controller.status import CRADLE, USB_CABLE

    def make(toy_cls):
        return _droid_for_toy("X", object(), toy_cls, lambda a: None, lambda: PowerInfo("OK", 4.0), threading.Event())

    assert make(R2D2).charger is USB_CABLE
    assert make(BB8).charger is CRADLE

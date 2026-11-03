"""The droid chooser shown on the screen when Back is pressed. Pure state, no I/O."""

from dataclasses import dataclass, field

SLEEP_ITEM = "Sleep this droid"


@dataclass
class DroidMenu:
    """Droids to pick from: the current one first, then those a scan finds, then the sleep entry."""

    current: str
    names: list[str] = field(default_factory=list)
    index: int = 0
    scanning: bool = True

    def __post_init__(self) -> None:
        if self.current not in self.names:
            self.names.insert(0, self.current)

    @property
    def items(self) -> list[str]:
        return [*self.names, SLEEP_ITEM]

    @property
    def selected(self) -> str:
        return self.items[self.index]

    def add_seen(self, seen: list[str]) -> None:
        """Merge scan results, keeping the order droids were first seen in."""
        for name in seen:
            if name not in self.names:
                self.names.append(name)
        self.scanning = False

    def move(self, delta: int) -> None:
        self.index = (self.index + delta) % len(self.items)

    def labels(self) -> list[str]:
        """One label per item, marking the current droid."""
        return [f"{name} (this one)" if name == self.current else name for name in self.items]

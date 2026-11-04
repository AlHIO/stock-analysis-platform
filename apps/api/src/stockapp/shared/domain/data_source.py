from typing import Protocol, TypeVar

T_co = TypeVar("T_co", covariant=True)


class DataSource(Protocol[T_co]):
    """Vendor-neutral boundary; domain modules define typed requests/results later.

    Implementations live in infrastructure. No vendor is wired in Phase 0.
    """

    async def fetch(self, *, identifier: str) -> T_co: ...

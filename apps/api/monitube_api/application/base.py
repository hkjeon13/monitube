"""Dependencies shared by application use-case services."""

from ..cache import DerivedCache
from ..ports import CollectionRepository


class ApplicationService:
    def __init__(
        self,
        repository: CollectionRepository,
        *,
        runtime_config_id: str | None = None,
        derived_cache: DerivedCache | None = None,
    ) -> None:
        self.repository = repository
        self.runtime_config_id = runtime_config_id
        self.derived_cache = derived_cache

"""Pydantic models for virtual models (S-060)."""

from pydantic import BaseModel, ConfigDict, Field

NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"


class VirtualModelContract(BaseModel):
    """Declared guarantee a virtual model makes to its callers.

    ``context_size`` is the minimum context window (tokens) every satisfying
    target must report; ``capabilities`` lists capability strings (e.g.
    ``"multimodal"``) every satisfying target must advertise. An unset field
    means "no constraint on this axis".
    """

    context_size: int | None = Field(default=None, gt=0)
    capabilities: list[str] | None = None


class VirtualModelCreate(BaseModel):
    name: str
    targets: list[str] = Field(min_length=1)
    description: str | None = None
    contract: VirtualModelContract = Field(default_factory=VirtualModelContract)


class VirtualModelUpdate(BaseModel):
    targets: list[str] | None = None
    description: str | None = None
    contract: VirtualModelContract | None = None


class VirtualModelResponse(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    id: str
    name: str
    targets: list[str]
    description: str | None = None
    contract: VirtualModelContract
    created_at: str | None = None
    updated_at: str | None = None
    # Populated on create/update responses when a target is not (yet) in the
    # registry; always empty on plain reads.
    warnings: list[str] = Field(default_factory=list)
    # Populated on reads: target alias -> "satisfied" | "violating: <reason>"
    # | "missing", computed against the live registry.
    target_status: dict[str, str] | None = None

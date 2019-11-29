# Stubs for kubernetes.client.models.v1_owner_reference (Python 2)
#
# NOTE: This dynamically typed stub was automatically generated by stubgen.

from typing import Any, Optional

class V1OwnerReference:
    swagger_types: Any = ...
    attribute_map: Any = ...
    discriminator: Any = ...
    api_version: str = ...
    block_owner_deletion: Any = ...
    controller: Any = ...
    kind: str = ...
    name: Any = ...
    uid: Any = ...
    def __init__(self, api_version: Optional[Any] = ..., block_owner_deletion: Optional[Any] = ..., controller: Optional[Any] = ..., kind: Optional[Any] = ..., name: Optional[Any] = ..., uid: Optional[Any] = ...) -> None: ...
    @property
    def api_version(self) -> str: ...
    @api_version.setter
    def api_version(self, api_version: str) -> None: ...
    @property
    def block_owner_deletion(self): ...
    @block_owner_deletion.setter
    def block_owner_deletion(self, block_owner_deletion: Any) -> None: ...
    @property
    def controller(self): ...
    @controller.setter
    def controller(self, controller: Any) -> None: ...
    @property
    def kind(self) -> str: ...
    @kind.setter
    def kind(self, kind: str) -> None: ...
    @property
    def name(self): ...
    @name.setter
    def name(self, name: Any) -> None: ...
    @property
    def uid(self): ...
    @uid.setter
    def uid(self, uid: Any) -> None: ...
    def to_dict(self): ...
    def to_str(self): ...
    def __eq__(self, other: Any): ...
    def __ne__(self, other: Any): ...
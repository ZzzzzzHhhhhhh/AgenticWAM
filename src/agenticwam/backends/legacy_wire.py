"""OpenWAM/Vela v1 wire compatibility, kept outside the generic core."""

import hashlib

from agenticwam.core.types import Context, ContractError


class WamContext(Context):
    def to_dict(self):
        return {"schema": "wam.context/v1", "context_id": self.context_id, "revision": self.revision, "text": self.text}

    def verify_receipt(self, value):
        expected = {**self.to_dict(), "text_sha256": hashlib.sha256(self.text.encode("utf-8")).hexdigest()}
        if value != expected:
            raise ContractError("backend did not acknowledge the exact task context")

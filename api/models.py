# models.py
"""
Pydantic request/response payloads shared across routers.

Kept as its own top-level module (not moved into routers/) because
TransformPayload's shape is part of the public API contract the frontend
depends on, independent of which router happens to use it.
"""

from typing import List, Optional

from pydantic import BaseModel


class TransformPayload(BaseModel):
    prompt: str
    tables: List[int]
    sessionId: Optional[int] = None
    userId: int

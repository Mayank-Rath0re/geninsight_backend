# models.py

from pydantic import BaseModel
from typing import List, Optional

class TransformPayload(BaseModel):
    sessionId: Optional[int] = None
    userId: int
    prompt: str
    tables: List[int]
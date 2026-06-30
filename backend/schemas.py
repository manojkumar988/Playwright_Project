from __future__ import annotations

from pydantic import BaseModel, HttpUrl
from typing import Literal


class ScanRequest(BaseModel):
    url: HttpUrl
    mode: Literal["auto", "browser", "browser-fast", "http"] = "auto"
    headless: bool = False


class ScanResponse(BaseModel):
    report: str

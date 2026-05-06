from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class CookieInput(BaseModel):
    name: str
    value: str
    domain: Optional[str] = None
    path: Optional[str] = "/"
    secure: Optional[bool] = None
    httpOnly: Optional[bool] = None
    expires: Optional[float] = None


class ProxyConfig(BaseModel):
    url: str
    username: Optional[str] = None
    password: Optional[str] = None


class V1Request(BaseModel):
    cmd: str
    url: Optional[str] = None
    session: Optional[str] = None
    session_ttl_minutes: Optional[int] = None
    maxTimeout: int = 60000
    cookies: Optional[List[CookieInput]] = None
    postData: Optional[str] = None
    proxy: Optional[ProxyConfig] = None
    returnOnlyCookies: bool = False
    returnScreenshot: bool = False
    disableMedia: bool = False
    turnstile_input_name: str = "cf-turnstile-response"


class CookieOutput(BaseModel):
    name: str
    value: str
    domain: str
    path: str
    expires: Optional[float] = None
    size: Optional[int] = None
    httpOnly: bool = False
    secure: bool = False
    session: bool = False
    sameSite: Optional[str] = None


class SolutionResult(BaseModel):
    url: str
    status: int = 200
    headers: Dict[str, Any] = Field(default_factory=dict)
    response: Optional[str] = None
    cookies: List[CookieOutput] = Field(default_factory=list)
    userAgent: str = ""
    turnstile_token: Optional[str] = None


class V1Response(BaseModel):
    status: str
    message: str = ""
    startTimestamp: int = 0
    endTimestamp: int = 0
    version: str = ""
    solution: Optional[SolutionResult] = None

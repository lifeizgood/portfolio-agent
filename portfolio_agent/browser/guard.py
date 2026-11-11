"""Browser guardrails as plain functions (spec §9.2), so they can be unit-tested.

- Only registry domains may be navigated to.
- Controls that could act on the account are never clicked.
- CAPTCHA, unknown prompts and security questions stop the branch; we never
  try to get past bot detection.
"""

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

# Clicking any of these stops the branch and raises an alert.
BLOCKED_LABEL = re.compile(
    r"\b(transfer|transfers|trade|trades|trading|withdraw\w*|order|orders|delete|settings|"
    r"buy|sell|pay|payment|payments|send money|wire|close account)\b", re.I)
# Refused, but the navigator may keep looking.
AVOID_LABEL = re.compile(r"\b(sign ?out|log ?out|logoff|log off)\b", re.I)

CAPTCHA_TEXT = re.compile(r"(i'?m not a robot|verify (that )?you are (a )?human|captcha)", re.I)
OTP_TEXT = re.compile(r"((verification|security|one[- ]time|authorization|validation) code|"
                      r"enter (the )?code|passcode we sent|code (was )?sent)", re.I)
SECURITY_Q_TEXT = re.compile(r"(security question|challenge question)", re.I)


class FetchStopped(Exception):
    """A guardrail or prompt stopped this branch. `kind` decides the inbox item."""

    ALERT_KINDS = {"blocked_click", "off_allowlist"}

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass
class Trace:
    """Run trace (spec §5.10). Never record secrets or codes here."""

    steps: list = field(default_factory=list)

    def add(self, action: str, **info):
        self.steps.append({"action": action, **info})


def host_allowed(url: str, domains: list[str]) -> bool:
    u = urlparse(url)
    if u.scheme in ("about", "data", "blob", "chrome", "chrome-error"):
        return True
    if u.scheme not in ("http", "https"):
        return False
    host = (u.hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in domains)


def check_click(label: str, href: str, base_url: str, domains: list[str]) -> str | None:
    """None if the click is fine, 'avoid' to refuse it, 'blocked' to stop the branch."""
    text = f"{label} {href}"
    if BLOCKED_LABEL.search(text):
        return "blocked"
    if AVOID_LABEL.search(text):
        return "avoid"
    if href and href.startswith(("http://", "https://")) and not host_allowed(href, domains):
        return "blocked"
    return None


def classify_page(text: str, has_password: bool, has_otp_input: bool,
                  frame_urls: list[str]) -> str:
    """captcha | security_question | twofa | login | ok"""
    if any("captcha" in u.lower() for u in frame_urls) or CAPTCHA_TEXT.search(text):
        return "captcha"
    if SECURITY_Q_TEXT.search(text):
        return "security_question"
    if has_otp_input or OTP_TEXT.search(text):
        return "twofa"
    if has_password:
        return "login"
    return "ok"

"""Portal fetcher (spec §4): log in, reach the holdings page, save the evidence.

Playbook steps first (login selectors, saved holdings URL); the LLM navigator
only when no route is saved or the saved one fails. A route the navigator finds
comes back as a playbook proposal for the user to approve (spec D14).

The fetcher never extracts values. It hands the page text and evidence folder
to the extractor (spec C1).

Guardrails enforced here: domain allowlist on every main-frame navigation,
login form is the only form filled, login cap per night, CAPTCHA / security
question / unknown prompt stop the branch, kill switch, time limit.
"""

import json
import re
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Callable

from .. import config, registry, store
from ..timeutil import now, now_iso, to_iso
from .guard import FetchStopped, Trace, classify_page, host_allowed
from .navigator import navigate_to_holdings

USERNAME_IN_FORM = "input[type=email], input[type=text], input[type=tel], input:not([type])"
# Fields that are clearly a username / user ID, even with no password field on the page yet
# (two-step logins ask for the user ID first, then the password).
USERNAME_FIELDS = ("input[autocomplete='username'], input[name*='user' i], input[id*='user' i],"
                   " input[name*='onlineid' i], input[id*='onlineid' i], input[name*='login' i],"
                   " input[id*='login' i], input[type=email]")
# A code box is a text-like input: never a radio button or checkbox (a "how should we
# send it?" page often names its radios after the code, e.g. authCodeDelivery).
_TEXTLIKE = ":not([type=radio]):not([type=checkbox]):not([type=hidden]):not([type=submit]):not([type=button])"
OTP_INPUT = ", ".join(f"{sel}{_TEXTLIKE}" for sel in (
    "input[autocomplete='one-time-code']", "input[name*='otp' i]", "input[id*='otp' i]",
    "input[name*='code' i]", "input[id*='code' i]"))

_CLICKABLES_JS = """() => {
  const els = [...document.querySelectorAll(
    'a[href], button, [role=tab], [role=link], [role=button], [role=menuitem]')];
  const out = [];
  for (const e of els) {
    const r = e.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    const id = out.length;
    e.setAttribute('data-pa-id', String(id));
    out.push({id, label: (e.innerText || e.getAttribute('aria-label') || e.title || '')
                          .trim().replace(/\\s+/g, ' ').slice(0, 80),
              href: e.getAttribute('href') || ''});
    if (out.length >= 80) break;
  }
  return out;
}"""


@dataclass
class Evidence:
    text: str
    folder: str
    url: str
    captured_at: str
    proposal: dict | None = None     # playbook change for the user to approve
    trace: list = field(default_factory=list)


@dataclass
class FetchContext:
    conn: object
    run_kind: str                                   # nightly | refresh
    request_code: Callable[[str], str | None]       # asks the user via the inbox
    model_loader: Callable[[], object]              # LLM, loaded only if needed
    trace: Trace = field(default_factory=Trace)


class _PageAdapter:
    """What the navigator sees of a Playwright page."""

    def __init__(self, holder):
        self.h = holder

    def url(self):
        return self.h.page.url

    def title(self):
        try:
            return self.h.page.title()
        except Exception:
            return ""

    def text_excerpt(self, n):
        return _body_text(self.h.page)[:n]

    def clickables(self):
        return self.h.page.evaluate(_CLICKABLES_JS)

    def click(self, i):
        self._guarded(lambda: self.h.page.click(f'[data-pa-id="{i}"]', timeout=15000))

    def goto(self, url):
        self._guarded(lambda: self.h.page.goto(url, wait_until="domcontentloaded", timeout=60000))

    def _guarded(self, action):
        try:
            action()
        except Exception:
            self.h.check()   # a blocked navigation explains the failure better
            raise
        _settle(self.h.page)
        self.h.check()


class _Holder:
    """Current page plus the allowlist state shared with the route handler."""

    def __init__(self, page, deadline):
        self.page = page
        self.blocked_nav: str | None = None
        self.deadline = deadline

    def check(self):
        if self.blocked_nav:
            raise FetchStopped("off_allowlist", f"The site tried to send the browser to "
                               f"{self.blocked_nav}, outside the allowed domains. Stopped.")
        if config.KILL_FILE.exists():
            raise FetchStopped("kill", "Kill switch is on.")
        if time.monotonic() > self.deadline:
            raise FetchStopped("timeout", f"The fetch took longer than {config.FETCH_TIMEOUT_S}s.")


def _settle(page):
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass  # many portals keep connections open; DOM is enough


def _body_text(page) -> str:
    try:
        return page.inner_text("body", timeout=10000)
    except Exception:
        return ""


def _visible(locator) -> bool:
    try:
        return locator.count() > 0 and locator.first.is_visible()
    except Exception:
        return False


def fetch_portal(login: dict, creds: dict | None, ctx: FetchContext) -> Evidence:
    from playwright.sync_api import sync_playwright

    if config.KILL_FILE.exists():
        raise FetchStopped("kill", "Kill switch is on.")
    if not login.get("login_url"):
        raise FetchStopped("config", "No login page URL in Settings for this login.")
    domains = registry.domains(login)
    steps = registry.steps_for(ctx.conn, login["institution"])
    profile = config.BROWSER_PROFILES_DIR / _slug(login["institution"])
    profile.mkdir(parents=True, exist_ok=True)
    trace = ctx.trace

    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            str(profile), headless=config.HEADLESS, accept_downloads=False,
            viewport={"width": 1400, "height": 1000})
        holder = None
        try:
            holder = _Holder(browser.pages[0] if browser.pages else browser.new_page(),
                             time.monotonic() + config.FETCH_TIMEOUT_S)

            def guard_route(route, request):
                if (request.is_navigation_request() and request.frame.parent_frame is None
                        and not host_allowed(request.url, domains)):
                    holder.blocked_nav = request.url
                    trace.add("blocked_navigation", url=request.url)
                    return route.abort()
                return route.continue_()

            def watch_request(request):
                # Playwright doesn't route redirects, so check every main-frame
                # navigation request (including redirect hops) as it happens.
                try:
                    main = request.frame.parent_frame is None
                except Exception:
                    main = False
                if (request.is_navigation_request() and main
                        and not host_allowed(request.url, domains)):
                    holder.blocked_nav = holder.blocked_nav or request.url
                    trace.add("blocked_navigation", url=request.url)

            browser.route("**/*", guard_route)
            browser.on("request", watch_request)
            browser.on("page", lambda pg: setattr(holder, "page", pg))  # follow new tabs

            _login(holder, login, creds, steps, ctx)

            proposal = None
            pages: list[tuple] = []      # (url, text, screenshot-taker) per captured page
            saved = steps.get("holdings_urls") or []
            for n, url in enumerate(saved, 1):
                trace.add("goto_saved_holdings_url", url=url)
                _PageAdapter(holder).goto(url)
                state = _classify(holder.page)
                if state == "login":   # session expired between pages: log in once more
                    _login(holder, login, creds, steps, ctx)
                    _PageAdapter(holder).goto(url)
                    state = _classify(holder.page)
                if state != "ok":
                    trace.add("saved_route_failed", url=url, state=state)
                    pages = []
                    break
                pages.append(_capture_page(holder.page, n))
            if not pages:
                trace.add("llm_navigation_start")
                result = navigate_to_holdings(
                    ctx.model_loader(), _PageAdapter(holder), login["name"], domains,
                    trace, should_stop=lambda: (holder.check() or False))
                if not result.found:
                    raise FetchStopped("not_found", "Couldn't find the holdings page: "
                                       + (result.reason or "no reason given") + ". Add the "
                                       "holdings page URL(s) in Settings to skip this search.")
                pages = [_capture_page(holder.page, 1)]
                if result.url not in saved:
                    proposal = {"institution": login["institution"],
                                "change": {"holdings_urls": [result.url]},
                                "why": "The navigator found the holdings page here. If some "
                                       "accounts are on other pages, add those URLs in Settings."}
            return _save_evidence(pages, login, trace, proposal)
        except Exception as e:
            if holder is not None:   # keep the page it stopped on, for the Debug tab
                e.evidence_folder = _failure_evidence(holder.page, login, trace)
            raise
        finally:
            browser.close()


def _find_username(page, steps: dict):
    """The username field, or None. Saved selector first, then common names, then
    the text field in the same form as the password."""
    candidates = []
    if steps.get("username_selector"):
        candidates.append(page.locator(steps["username_selector"]))
    candidates.append(page.locator(USERNAME_FIELDS))
    pw = page.locator("input[type=password]")
    if _visible(pw):
        candidates.append(pw.first.locator("xpath=ancestor::form[1]").locator(USERNAME_IN_FORM))
    for loc in candidates:
        try:
            for i in range(min(loc.count(), 5)):
                el = loc.nth(i)
                if el.is_visible() and (el.get_attribute("type") or "text") not in (
                        "password", "hidden", "search", "checkbox", "radio", "submit"):
                    return el
        except Exception:
            continue
    return None


SUBMIT_BUTTONS = ("button[type=submit], input[type=submit], button:has-text('Continue'),"
                  " button:has-text('Next'), button:has-text('Sign in'), button:has-text('Log in'),"
                  " button:has-text('Login'), button:has-text('Submit'), button:has-text('Verify'),"
                  " [role=button]:has-text('Continue'), [role=button]:has-text('Submit'),"
                  " [role=button]:has-text('Log in'), [role=button]:has-text('Sign in')")


def _submit(page, field, steps: dict) -> None:
    """Submit the login step: saved button, else Enter, else the step's sign-in button."""
    if steps.get("submit_selector") and _visible(page.locator(steps["submit_selector"])):
        page.click(steps["submit_selector"], timeout=15000)
        return
    url = page.url
    field.press("Enter")
    try:
        page.wait_for_url(lambda u: u != url, timeout=3000)
        return
    except Exception:
        pass
    try:
        if not field.is_visible():
            return              # the page moved on without changing its URL
    except Exception:
        return
    buttons = page.locator(SUBMIT_BUTTONS)
    for i in range(min(buttons.count(), 5)):
        b = buttons.nth(i)
        if b.is_visible():
            b.click(timeout=15000)
            return


def _classify(page) -> str:
    has_pw = (_visible(page.locator("input[type=password]"))
              or _visible(page.locator(USERNAME_FIELDS)))   # username-first login page
    has_otp = _visible(page.locator("input[autocomplete='one-time-code']"))
    frames = [f.url for f in page.frames]
    return classify_page(_body_text(page)[:5000], has_pw, has_otp, frames)


def _login(holder: _Holder, login: dict, creds: dict | None, steps: dict,
           ctx: FetchContext) -> None:
    page, trace = holder.page, ctx.trace
    if page.url in ("", "about:blank") or not host_allowed(page.url, registry.domains(login)):
        trace.add("goto_login", url=login["login_url"])
        _PageAdapter(holder).goto(login["login_url"])
        page = holder.page

    counted = False
    for _ in range(5):   # user ID page, password page, then possibly a 2FA page
        state = _classify(page)
        trace.add("page_state", state=state, url=page.url)
        if state == "ok":
            return
        if state == "captcha":
            raise FetchStopped("captcha", "The portal is showing a CAPTCHA. Sign in once by hand "
                               "with `python -m portfolio_agent.cli open-browser "
                               f"{login['id']}`, then Refresh again.")
        if state == "security_question":
            raise FetchStopped("security_question", "The portal asked a security question. "
                               "Answer it by hand with `open-browser`, then Refresh again.")
        if state == "twofa":
            _two_factor(holder, login, ctx)
            page = holder.page
            continue
        # state == "login"
        if not creds:
            raise FetchStopped("no_credentials", "No username and password saved for this login. "
                               "Add them under Accounts & logins.")
        if not counted:     # one attempt, even when it spans a user-ID and a password page
            _check_login_cap(ctx, login)
            store.record_login(ctx.conn, login["id"], ctx.run_kind)
            counted = True
        pw_sel = steps.get("password_selector") or "input[type=password]"
        user = _find_username(page, steps)
        if user is not None:
            try:
                if not user.input_value():          # leave a remembered user ID alone
                    user.fill(creds["username"])
            except Exception:
                trace.add("username_fill_failed")
        else:
            trace.add("username_field_not_found")
        if not _visible(page.locator(pw_sel)):
            # Two-step login: submit the user ID, then wait for the password page.
            if user is None:
                raise FetchStopped("login_failed", "Couldn't find the username or password field. "
                                   "Add the field selectors under Accounts & logins → login steps.")
            trace.add("submit_username")
            _submit(page, user, steps)
            _settle(page)
            holder.check()
            page = holder.page
            try:
                page.locator(pw_sel).first.wait_for(state="visible", timeout=20000)
            except Exception:
                continue    # maybe a 2FA, CAPTCHA or other page: classify it next
        pw = page.locator(pw_sel).first
        pw.fill(creds["password"])
        trace.add("submit_login")     # values are never traced
        _submit(page, pw, steps)
        try:                          # give the portal time to move past the password page
            pw.wait_for(state="hidden", timeout=20000)
        except Exception:
            pass
        _settle(page)
        holder.check()
        page = holder.page
        if _classify(page) == "login":
            raise FetchStopped("login_failed", "The portal is still showing the login form after "
                               "signing in. Check the saved username and password. Not retrying, "
                               "to avoid a lockout.")
    raise FetchStopped("unexpected", "Login didn't finish after several pages.")


# "How would you like to receive it?" pages: pick text message, then the send button.
TEXT_OPTION = re.compile(r"text( message)?|sms", re.I)
SEND_BUTTON = re.compile(r"^\s*(next|send( code)?|continue|get code|text me|send text)\s*$", re.I)
CODE_FIELD_VISIBLE = ", ".join(f"{sel}:visible" for sel in OTP_INPUT.split(", "))


# "Remember this device" (checkbox or radio), so later runs skip the code entirely.
REMEMBER = re.compile(r"remember (this |my )?(device|computer|browser)|trust (this )?(device|browser)"
                      r"|don.?t ask (me )?again|save (this )?(device|browser)", re.I)
REMEMBER_YES = re.compile(r"^\s*(yes|yes, remember.*|remember( this)?( device| computer| browser)?"
                          r"|trust( this)?( device| browser)?|save( device)?)\s*$", re.I)


def _remember_device(page, trace: Trace) -> None:
    """Tick "remember this device" if the page offers it. Part of the login flow only."""
    for role in ("checkbox", "radio"):
        try:
            box = page.get_by_role(role, name=REMEMBER).first
            if box.count() and box.is_visible() and not box.is_checked():
                box.check(timeout=5000)
                trace.add("remember_device_checked")
                return
        except Exception:
            continue


def _answer_remember_page(page, trace: Trace) -> None:
    """A separate "Remember this device?" page after the code: answer yes."""
    try:
        if not REMEMBER.search(_body_text(page)[:3000]):
            return
        _remember_device(page, trace)
        yes = page.get_by_role("button", name=REMEMBER_YES).first
        if yes.count() and yes.is_visible():
            yes.click(timeout=10000)
            trace.add("remember_device_confirmed")
            _settle(page)
    except Exception:
        trace.add("remember_device_failed")


def _request_sms(page, trace: Trace) -> bool:
    """On a delivery-choice page, choose text message and press Next. True if done."""
    try:
        radio = page.get_by_role("radio", name=TEXT_OPTION).first
        if radio.count() and radio.is_visible():
            radio.check(timeout=5000)
        else:
            return False
        page.get_by_role("button", name=SEND_BUTTON).first.click(timeout=10000)
    except Exception:
        trace.add("sms_choice_failed")
        return False
    trace.add("sms_code_requested")
    try:
        page.locator(CODE_FIELD_VISIBLE).first.wait_for(state="visible", timeout=30000)
    except Exception:
        pass
    return True


def _two_factor(holder: _Holder, login: dict, ctx: FetchContext) -> None:
    """Get a 2FA code: ask the portal to text it, read it from Messages (code only,
    spec C12), and fall back to asking the user in the inbox with the reason."""
    from .. import sms
    from ..timeutil import now as utcnow

    method = login.get("twofa_method", "remembered_device")
    page, trace = holder.page, ctx.trace
    trace.add("twofa_requested", method=method)
    code, why = None, ""
    if method in ("sms", "remembered_device"):
        with sms.SMS_LOCK:            # one SMS code expected at a time (spec §9.2)
            since = utcnow()
            offers_text = _visible(page.get_by_role("radio", name=TEXT_OPTION))
            on_code_page = not offers_text and _visible(page.locator(CODE_FIELD_VISIBLE))
            if not on_code_page and not _request_sms(page, trace):
                why = "The page didn't offer a text-message option that could be selected. "
            else:
                try:
                    code = sms.wait_for_code(since - timedelta(seconds=30 if on_code_page else 2),
                                             should_stop=lambda: config.KILL_FILE.exists())
                    if code:
                        trace.add("sms_code_read")          # never the code itself
                    else:
                        why = f"No text with a code arrived within {config.SMS_WAIT_S} seconds. "
                except sms.SmsUnavailable as e:
                    why = str(e) + " "
    else:
        why = f"Automatic reading of {method} codes isn't built yet. "
    if not code:
        code = ctx.request_code(f"{login['institution']} is asking for a verification code for "
                                f"the {login['name']} login. {why}Enter the code you received.")
    if not code:
        raise FetchStopped("twofa_unavailable", "No verification code was entered in time. " + why)
    page = holder.page
    field = page.locator(CODE_FIELD_VISIBLE).first
    if not _visible(field):
        field = page.locator("input[type=text]:visible, input[type=tel]:visible, "
                             "input[type=number]:visible, input[type=password]:visible").first
    if not _visible(field):
        raise FetchStopped("twofa_unavailable", "Got a verification code but couldn't find the "
                           "box to type it into.")
    field.fill(code.strip())
    _remember_device(page, trace)
    _submit(page, field, {})
    trace.add("twofa_submitted")   # the code itself is never traced
    try:
        field.wait_for(state="hidden", timeout=20000)
    except Exception:
        pass
    _settle(page)
    holder.check()
    _answer_remember_page(holder.page, trace)
    holder.check()


def _check_login_cap(ctx: FetchContext, login: dict) -> None:
    if ctx.run_kind != "nightly":
        return   # Refresh limits are to be set over time (spec C15)
    since = to_iso(now() - timedelta(hours=12))
    n = store.logins_since(ctx.conn, login["id"], since, "nightly")
    if n >= config.MAX_LOGINS_PER_NIGHT:
        raise FetchStopped("login_cap", f"Already tried {n} logins tonight; the limit is "
                           f"{config.MAX_LOGINS_PER_NIGHT} per portal per night.")


def _capture_page(page, n: int) -> dict:
    _settle(page)
    return {"n": n, "url": page.url, "text": _body_text(page),
            "png": page.screenshot(full_page=True), "html": page.content()}


def _failure_evidence(page, login: dict, trace: Trace) -> str | None:
    """Screenshot and text of the page a run stopped on. Password fields show as dots."""
    try:
        folder = config.EVIDENCE_DIR / f"login-{login['id']}" / (now_iso().replace(":", "") + "-stopped")
        folder.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(folder / "stopped.png"), full_page=True, timeout=15000)
        (folder / "stopped.txt").write_text(_body_text(page), encoding="utf-8")
        (folder / "meta.json").write_text(json.dumps({"url": page.url, "stopped_at": now_iso()}))
        trace.add("failure_captured", url=page.url)
        return str(folder)
    except Exception:
        return None


def _save_evidence(pages: list[dict], login: dict, trace: Trace,
                   proposal: dict | None) -> Evidence:
    """Write every captured page to one evidence folder (View Source shows them all)."""
    captured_at = now_iso()
    folder = config.EVIDENCE_DIR / f"login-{login['id']}" / captured_at.replace(":", "")
    folder.mkdir(parents=True, exist_ok=True)
    texts = []
    for p in pages:
        stem = "page" if len(pages) == 1 else f"page-{p['n']}"
        (folder / f"{stem}.png").write_bytes(p["png"])
        (folder / f"{stem}.html").write_text(p["html"], encoding="utf-8")
        (folder / f"{stem}.txt").write_text(p["text"], encoding="utf-8")
        texts.append(p["text"] if len(pages) == 1 else f"=== Page {p['n']}: {p['url']} ===\n{p['text']}")
    (folder / "meta.json").write_text(json.dumps(
        {"pages": [p["url"] for p in pages], "captured_at": captured_at, "login": login["name"]},
        indent=2))
    trace.add("captured", pages=len(pages), folder=str(folder))
    text = "\n\n".join(texts)
    if len(text) > config.PAGE_MAX_CHARS:
        trace.add("page_text_truncated", chars=len(text))
        raise FetchStopped("page_too_long", f"The captured page text is {len(text):,} characters, "
                           f"over the {config.PAGE_MAX_CHARS:,} limit. Cutting it would drop "
                           "accounts, so nothing was read. Raise PA_PAGE_MAX_CHARS or save "
                           "narrower holdings pages under the login steps.")
    return Evidence(text=text, folder=str(folder), url=pages[0]["url"], captured_at=captured_at,
                    proposal=proposal, trace=trace.steps)


def open_browser_for_manual_login(login: dict) -> None:
    """Open the login's browser profile so the user can sign in by hand once
    (and tick "remember this device"). Nothing is read or saved by the agent."""
    from playwright.sync_api import sync_playwright

    profile = config.BROWSER_PROFILES_DIR / _slug(login["institution"])
    profile.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(str(profile), headless=False,
                                                       viewport={"width": 1400, "height": 1000})
        page = browser.pages[0] if browser.pages else browser.new_page()
        page.goto(login["login_url"])
        print("Sign in in the browser window, choose 'remember this device' if offered,"
              " then close the window.")
        try:
            page.wait_for_event("close", timeout=0)
        except Exception:
            pass
        browser.close()


def _slug(s: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in s.lower()).strip("_") or "portal"


__all__ = ["Evidence", "FetchContext", "FetchStopped", "fetch_portal",
           "open_browser_for_manual_login"]

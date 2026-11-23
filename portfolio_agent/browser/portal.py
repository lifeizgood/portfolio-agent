"""Portal fetcher (spec §4): log in, reach the holdings page, save the evidence.

Playbook steps first (saved holdings URL); the LLM navigator
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
from urllib.parse import urljoin
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Callable

from .. import config, registry, store
from ..timeutil import now, now_iso, to_iso
from .guard import FetchStopped, Trace, check_click, classify_page, host_allowed
from .navigator import navigate_to_holdings

USERNAME_IN_FORM = "input[type=email], input[type=text], input[type=tel], input:not([type])"
# Fields that are clearly a username / user ID, even with no password field on the page yet
# (two-step logins ask for the user ID first, then the password).
_EDITABLE = ":not([readonly]):not([disabled])"   # a profile page's read-only email isn't a form
USERNAME_FIELDS = ", ".join(f"{sel}{_EDITABLE}" for sel in (
    "input[autocomplete='username']", "input[name*='user' i]", "input[id*='user' i]",
    "input[name*='onlineid' i]", "input[id*='onlineid' i]", "input[name*='login' i]",
    "input[id*='login' i]", "input[type=email]"))
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
    # Account details pages opened: [{"label": link text, "suffix": last 4 read by code}]
    details: list = field(default_factory=list)
    # Statements downloaded from the portal's documents page: [{"name", "data": bytes}]
    documents: list = field(default_factory=list)


@dataclass
class FetchContext:
    conn: object
    run_kind: str                                   # nightly | refresh
    request_code: Callable[[str], str | None]       # asks the user via the inbox
    model_loader: Callable[[], object]              # LLM, loaded only if needed
    trace: Trace = field(default_factory=Trace)
    should_cancel: Callable[[], bool] = lambda: False   # the user pressed Cancel


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

    def __init__(self, page, deadline, should_cancel=lambda: False, domains=None):
        self.page = page
        self.nav_domains: list[str] = list(domains or [])   # where navigation may go now
        self.blocked_nav: str | None = None
        self.deadline = deadline
        self.should_cancel = should_cancel

    def check(self):
        if self.blocked_nav:
            raise FetchStopped("off_allowlist", f"The site tried to send the browser to "
                               f"{self.blocked_nav}, outside the allowed domains. Stopped.")
        if config.KILL_FILE.exists():
            raise FetchStopped("kill", "Kill switch is on.")
        if self.should_cancel():
            raise FetchStopped("cancelled", "Cancelled by you.")
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
    if ctx.should_cancel():
        raise FetchStopped("cancelled", "Cancelled by you.")
    if not login.get("login_url"):
        raise FetchStopped("config", "No login page URL in Settings for this login.")
    domains = registry.domains(login)
    steps = registry.steps_for(ctx.conn, login["institution"])
    trace = ctx.trace
    profile, channel = browser_profile(login)
    profile.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch_persistent_context(
                str(profile), headless=config.HEADLESS, channel=channel,
                accept_downloads=login.get("statement_kind") == "capital",
                viewport={"width": 1400, "height": 1000})
        except Exception as e:
            if PROFILE_IN_USE.search(str(e)):
                raise FetchStopped("profile_busy", profile_busy_message(login)) from None
            raise
        holder = None
        try:
            holder = _Holder(browser.pages[0] if browser.pages else browser.new_page(),
                             time.monotonic() + config.FETCH_TIMEOUT_S, ctx.should_cancel,
                             domains)

            def guard_route(route, request):
                if (request.is_navigation_request() and _top_level(request)
                        and not host_allowed(request.url, holder.nav_domains)):
                    holder.blocked_nav = request.url
                    trace.add("blocked_navigation", url=request.url)
                    return route.abort()
                return route.continue_()

            def watch_request(request):
                # Playwright doesn't route redirects, so check every main-frame
                # navigation request (including redirect hops) as it happens.
                if (request.is_navigation_request() and _top_level(request)
                        and not host_allowed(request.url, holder.nav_domains)):
                    holder.blocked_nav = holder.blocked_nav or request.url
                    trace.add("blocked_navigation", url=request.url)

            browser.route("**/*", guard_route)
            browser.on("request", watch_request)
            browser.on("page", lambda pg: setattr(holder, "page", pg))  # follow new tabs

            _login(holder, login, creds, steps, ctx)
            _wait_after_login(holder, steps, trace)

            if login.get("statement_kind") == "capital" and steps.get("document_name"):
                # each fund's own documents tab (e.g. Star Mountain)
                docs = _fetch_fund_documents(holder, browser, login, steps, domains, trace, ctx)
                ev = _save_evidence([_capture_page(holder.page, 1)], login, trace, None)
                ev.documents = docs
                return ev
            if login.get("statement_kind") == "capital" and steps.get("documents_tab"):
                # statements from the portal's documents page
                docs = _fetch_documents(holder, browser, login, steps, domains, trace, ctx)
                ev = _save_evidence([_capture_page(holder.page, 1)], login, trace, None)
                ev.documents = docs
                return ev

            proposal = None
            pages: list[tuple] = []      # (url, text, screenshot-taker) per captured page
            saved = steps.get("holdings_urls") or []
            for n, url in enumerate(saved, 1):
                trace.add("goto_saved_holdings_url", url=url)
                _PageAdapter(holder).goto(url)
                state = _state(holder.page, login, domains)
                if state == "login":   # session expired between pages: log in once more
                    _login(holder, login, creds, steps, ctx)
                    _PageAdapter(holder).goto(url)
                    state = _classify(holder.page, domains)
                if state != "ok":
                    trace.add("saved_route_failed", url=url, state=state)
                    pages = []
                    break
                pages.append(_capture_page(holder.page, n))
            if not pages and steps.get("read_landing_page"):   # the page after sign-in
                trace.add("landing_page_read")
                pages = [_capture_page(holder.page, 1)]
            if not pages:      # a page the institution's guide knows is already showing
                from ..statement_guides import guide_for
                guide = guide_for(login["institution"])
                if guide and guide.portal_marker and guide.portal_marker.lower() in \
                        _body_text(holder.page).lower():
                    trace.add("guide_page_found", marker=guide.portal_marker)
                    pages = [_capture_page(holder.page, 1)]
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
            if login.get("open_account_details"):
                known = [a["portal_name"] for a in registry.list_accounts(ctx.conn)
                         if a["login_id"] == login["id"] and a["status"] != "ignored"
                         and a.get("portal_name")]
                pages += _account_details(holder, pages, domains, trace, known,
                                          steps.get("account_number_link"))
            return _save_evidence(pages, login, trace, proposal)
        except Exception as e:
            if holder is not None:   # keep the page it stopped on, for the Debug tab
                e.evidence_folder = _failure_evidence(holder.page, login, trace)
            raise
        finally:
            browser.close()


# --- Statements from a portal's documents page (capital account statements) -----------

def _exact(label: str):
    return re.compile(rf"^\s*{re.escape(label.strip())}\s*$", re.I)


_IN_NAV = ("e => !!e.closest('nav, aside, header, [role=navigation], [role=banner], "
           "[class*=sidebar i], [class*=side-nav i], [class*=sidenav i], [id*=sidebar i]')")


def _find_control(page, label: str, roles=("tab", "link", "button", "menuitem")):
    """A visible control named exactly `label`, else one containing it. One in the page's
    content (a fund page's own Documents tab) wins over one in the side or top navigation."""
    for pattern in (_exact(label), re.compile(re.escape(label.strip()), re.I)):
        in_nav = None
        for role in roles:
            try:
                loc = page.get_by_role(role, name=pattern)
                for i in range(min(loc.count(), 8)):
                    el = loc.nth(i)
                    if not el.is_visible():
                        continue
                    try:
                        nav = el.evaluate(_IN_NAV)
                    except Exception:
                        nav = False
                    if not nav:
                        return el
                    in_nav = in_nav or el
            except Exception:
                continue
        if in_nav is not None:
            return in_nav
    return None


def _safe_click(el, page, domains, trace, what: str) -> None:
    label = ""
    try:
        label = (el.inner_text(timeout=2000) or "")[:80]
        href = el.get_attribute("href") or ""
    except Exception:
        href = ""
    verdict = check_click(label, urljoin(page.url, href) if href else "", page.url, domains)
    if verdict:
        raise FetchStopped("blocked_click", f"Refused to click {what} ({label!r}).")
    el.click(timeout=15000)
    trace.add("click", what=what)


def _apply_filter(page, label: str, domains, trace) -> bool:
    """Show only the statements: a dropdown option, a checkbox or a filter button."""
    for sel in page.locator("select").all()[:10]:
        try:
            options = [o.strip() for o in sel.locator("option").all_inner_texts()]
            match = [o for o in options if o.lower() == label.lower()] or \
                    [o for o in options if label.lower() in o.lower()]
            if match and sel.is_visible():
                sel.select_option(label=match[0])
                trace.add("filter_selected", how="dropdown")
                return True
        except Exception:
            continue
    for attempt in range(2):
        for role in ("checkbox", "radio"):
            try:
                box = page.get_by_role(role, name=re.compile(re.escape(label), re.I)).first
                if box.count() and box.is_visible():
                    if not box.is_checked():
                        box.check(timeout=10000)
                    trace.add("filter_selected", how=role)
                    return True
            except Exception:
                continue
        el = _find_control(page, label, ("option", "button", "tab", "link", "menuitem"))
        if el is not None:
            _safe_click(el, page, domains, trace, "the statement filter")
            return True
        # the choices may sit behind a "Filter" / "Document type" control
        opener = _find_control(page, "Document Type", ("button", "combobox")) or \
            _find_control(page, "Filter", ("button", "combobox"))
        if attempt or opener is None:
            break
        _safe_click(opener, page, domains, trace, "the filter menu")
        page.wait_for_timeout(800)
    return False


DOWNLOAD_NAME = re.compile(r"\bdownload\b|\.pdf\b|\bpdf\b|\bview\b", re.I)


_SEARCH_BOX = ("input[type=search], [role=searchbox], input[placeholder*='search' i],"
               " input[aria-label*='search' i], input[name*='search' i], input[id*='search' i]")


def _search(page, term: str, trace) -> bool:
    """Type the term into the documents page's own search box (on the login's site; this
    page was reached through the allowlist) and search. A search, never a login form."""
    box = page.locator(_SEARCH_BOX)
    for i in range(min(box.count(), 5)):
        el = box.nth(i)
        try:
            if el.is_visible() and (el.get_attribute("type") or "text") != "password":
                el.fill(term)
                el.press("Enter")
                trace.add("documents_searched")
                return True
        except Exception:
            continue
    return False


_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
_DATE_PATTERNS = [
    (re.compile(r"\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b"), lambda m: (int(m[1]), int(m[2]))),
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})\b"),
     lambda m: (int(m[3]) + (2000 if len(m[3]) == 2 else 0), int(m[1]))),
    (re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?[-\s]+(?:\d{1,2}(?:st|nd|rd|th)?[-,\s]+)?(\d{4})\b",
                re.I), lambda m: (int(m[2]), _MONTHS[m[1][:3].lower()])),
    (re.compile(r"\bQ([1-4])\s*[-']?\s*(\d{4})\b", re.I), lambda m: (int(m[2]), int(m[1]) * 3)),
    (re.compile(r"\b(\d{4})\s*Q([1-4])\b", re.I), lambda m: (int(m[1]), int(m[2]) * 3)),
]


def row_quarter(text: str) -> tuple[int, int] | None:
    """The latest (year, quarter) among the dates in a document row's text."""
    best = None
    for pattern, ym in _DATE_PATTERNS:
        for m in pattern.finditer(text or ""):
            try:
                y, mo = ym(m)
            except Exception:
                continue
            if 1990 <= y <= 2100 and 1 <= mo <= 12:
                q = (y, (mo - 1) // 3 + 1)
                best = q if best is None or q > best else best
    return best


def row_month(text: str) -> tuple[int, int] | None:
    """The latest (year, month) among the dates in a document row's text."""
    best = None
    for pattern, ym in _DATE_PATTERNS:
        for m in pattern.finditer(text or ""):
            try:
                y, mo = ym(m)
            except Exception:
                continue
            if 1990 <= y <= 2100 and 1 <= mo <= 12:
                best = (y, mo) if best is None or (y, mo) > best else best
    return best


def latest_rows(texts: list[str], monthly: bool = False) -> tuple[list[int], str]:
    """Which document rows to download: those dated in the newest quarter shown (several
    funds' statements for one quarter), or the newest month for a monthly login. With no
    dates at all, the first row only, since document lists show the newest first.
    Returns (indexes, how)."""
    periods = [(row_month if monthly else row_quarter)(t) for t in texts]
    dated = [q for q in periods if q]
    if not dated:
        return ([0] if texts else []), "first listed (no dates found)"
    top = max(dated)
    how = f"{top[0]}-{top[1]:02d}" if monthly else f"{top[0]} Q{top[1]}"
    return [i for i, q in enumerate(periods) if q == top], how


def _row_text(el) -> str:
    """The text of the list row or table row holding a document link."""
    for xp in ("xpath=ancestor::*[self::tr or self::li or @role='row'][1]",
               "xpath=..", "xpath=../..", "xpath=../../.."):
        try:
            row = el.locator(xp)
            if row.count():
                t = row.first.inner_text(timeout=2000)
                if row_quarter(t) or xp.endswith("../../.."):
                    return t
        except Exception:
            continue
    return ""


def _content_control(page, label: str):
    """A visible control named `label` in the page content, never in a sidebar or header."""
    for pattern in (_exact(label), re.compile(re.escape(label.strip()), re.I)):
        for role in ("tab", "link", "button", "menuitem"):
            try:
                loc = page.get_by_role(role, name=pattern)
                for i in range(min(loc.count(), 8)):
                    el = loc.nth(i)
                    if el.is_visible() and not el.evaluate(_IN_NAV):
                        return el
            except Exception:
                continue
    return None


def _wait_for_content_control(holder, label: str, seconds: int, trace):
    """Wait up to `seconds` for a `label` control in the page content (e.g. a fund page's
    Documents tab) to show up; then fall back to any `label` control, sidebar included."""
    holder.deadline += seconds          # waiting for a slow page isn't fetch time
    start = time.monotonic()
    while time.monotonic() - start < seconds:
        holder.check()
        page = _live_page(holder)
        el = _content_control(page, label)
        if el is not None:
            trace.add("page_ready", waited_s=round(time.monotonic() - start))
            return el
        page.wait_for_timeout(1000)
    trace.add("page_ready_timeout", waited_s=seconds)
    return _find_control(_live_page(holder), label)


def _wait_for_person(holder, login: dict, ctx, domains) -> None:
    """The portal shows a CAPTCHA. Only a person may answer it: bring the fetcher's window
    to the front, say so in the Inbox, and wait for the owner to solve it there. Nothing
    here clicks, types into or otherwise touches the CAPTCHA."""
    trace = ctx.trace
    if config.HEADLESS:
        raise FetchStopped("captcha", _sign_in_by_hand(
            login, "The portal is showing a CAPTCHA, and the browser window is hidden."))
    page = holder.page
    try:
        page.bring_to_front()
    except Exception:
        pass
    item = store.add_inbox_item(
        ctx.conn, "question", f"{login['name']}: the portal is showing a CAPTCHA. Solve it in "
        f"the browser window that just opened; the run carries on by itself once it's gone "
        f"(waiting up to {config.CAPTCHA_WAIT_S // 60} minutes).",
        institution=login["institution"], account=login["name"], login_id=login["id"],
        payload={"stop": "captcha_wait"})
    trace.add("captcha_waiting_for_you")
    holder.deadline += config.CAPTCHA_WAIT_S      # waiting for you doesn't count as fetch time
    end = time.monotonic() + config.CAPTCHA_WAIT_S
    try:
        while time.monotonic() < end:
            holder.check()
            page = _live_page(holder)
            page.wait_for_timeout(2000)
            if _classify(page, domains) != "captcha":
                trace.add("captcha_solved_by_you")
                _settle(page)
                return
        raise FetchStopped("captcha", f"The CAPTCHA wasn't solved within "
                           f"{config.CAPTCHA_WAIT_S // 60} minutes. Refresh again when you're "
                           "at the computer.")
    finally:
        store.resolve_inbox_item(ctx.conn, item, "expired")


def _fetch_documents(holder, context, login, steps, domains, trace, ctx=None) -> list[dict]:
    """Open the documents tab, filter or search for the capital account statements, and
    download the latest ones (from the allowed domains only). Returns [{"name", "data"}];
    reading happens later."""
    tab = steps.get("documents_tab") or "Documents"
    wanted = "Capital Account Statements"     # used when the page offers that filter
    page = holder.page
    if tab.lower().startswith("https://"):     # the documents page's own address
        if not host_allowed(tab, domains):
            raise FetchStopped("blocked_click", "The documents page's address isn't on this "
                               "login's own site, so it wasn't opened.")
        _settled_url(page)                      # let sign-in finish its redirects
        trace.add("goto_documents", url=tab)
        page.goto(tab, timeout=30000)
        trace.add("opened", what="the documents page", url=_settled_url(page))
        _settle(page)
        tab = "documents"
    else:
        el = _find_control(page, tab)
        if el is None:
            raise FetchStopped("not_found", f"Couldn't find the {tab!r} tab after signing in. "
                               "Set the documents tab's label on this login (login steps).")
        _click_and_wait(page, el, domains, trace, "the documents tab")
    holder.check()
    page = holder.page
    if _classify(page, domains) == "captcha":
        _wait_for_person(holder, login, ctx, domains)
        page = holder.page
    if not (_apply_filter(page, wanted, domains, trace) or _search(page, wanted, trace)):
        trace.add("documents_unfiltered")     # no filter set and none shown: the whole list
    _settle(page)
    page.wait_for_timeout(1500)
    holder.check()
    if _classify(page, domains) == "captcha":
        _wait_for_person(holder, login, ctx, domains)
        page = holder.page
    found = []
    for _ in range(10):                 # the list can fill in after the page loads
        found = _column_links(page, "Download")
        if found:                       # icons under a "Download" heading, one per row
            trace.add("documents_by_column")
            break
        found = _download_links(page)
        if found:
            break
        page.wait_for_timeout(1000)
        holder.check()
    targets, rows, tried = [], [], set()
    for el in found:          # one entry per visible link (two searches can find the same)
        try:
            if not el.is_visible():
                continue
            key = el.get_attribute("href") or ""
            if not key or key.lower().startswith(("javascript:", "#")):   # same for every row
                key = el.evaluate("e => e.outerHTML")[:300]
        except Exception:
            continue
        if key in tried:
            continue
        tried.add(key)
        targets.append(el)
        rows.append(_row_text(el))
    picked, how = latest_rows(rows, monthly=login.get("cadence") == "monthly")
    trace.add("documents_listed", count=len(targets), picked=len(picked), latest=how)
    docs, seen = [], set()
    for i in picked[:config.MAX_DOCUMENTS]:
        holder.check()
        doc = _download(holder, context, targets[i], page, domains, trace)
        if doc and doc["data"][:64] not in seen:
            seen.add(doc["data"][:64])
            docs.append(doc)
    trace.add("documents_downloaded", count=len(docs))
    if not docs:
        raise FetchStopped("not_found", f"The {tab!r} page had no statement that could be downloaded"
                           f" ({len(targets)} download links found).")
    return docs


# In each body row of a table, the clickable thing in the cell under `heading` (e.g. an
# icon-only "Download" column). Marks them with data-pa-dl so Playwright can find them.
# A page-wide button with the same label (used with checkboxes) is never in a row cell.
COLUMN_LINKS_JS = r"""(heading) => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const want = clean(heading);
  document.querySelectorAll('[data-pa-dl]').forEach(e => e.removeAttribute('data-pa-dl'));
  let n = 0;
  for (const table of document.querySelectorAll('table')) {
    const head = [...table.querySelectorAll('th, thead td')].find(c => clean(c.innerText) === want);
    if (!head) continue;
    const idx = [...head.parentElement.children].indexOf(head);
    for (const row of table.querySelectorAll('tr')) {
      if (row === head.parentElement || row.closest('thead')) continue;
      const cell = row.children[idx];
      if (!cell) continue;
      const el = cell.querySelector('a, button, [role=button], [onclick], input[type=image]')
              || cell.querySelector('i, svg, img, span');
      if (!el || !(el.offsetWidth || el.offsetHeight || el.getClientRects().length)) continue;
      el.setAttribute('data-pa-dl', String(n++));
    }
    if (n) break;
  }
  return n;
}"""


def _column_links(page, heading: str) -> list:
    try:
        n = page.evaluate(COLUMN_LINKS_JS, heading)
    except Exception:
        return []
    return [page.locator(f"[data-pa-dl='{i}']") for i in range(min(n, config.MAX_DOCUMENTS * 4))]


def _download_links(page) -> list:
    found = []
    for role in ("link", "button"):
        loc = page.get_by_role(role, name=DOWNLOAD_NAME)
        found += [loc.nth(i) for i in range(min(loc.count(), config.MAX_DOCUMENTS * 4))]
    loc = page.locator("a[href$='.pdf' i], a[href*='.pdf?' i], a[download]")
    found += [loc.nth(i) for i in range(min(loc.count(), config.MAX_DOCUMENTS * 4))]
    return found


def _path(url: str) -> str:
    return url.split("#")[0].split("?")[0].rstrip("/").lower()


def _settled_url(page, quiet_s: float = 2.0, timeout_s: float = 15.0) -> str:
    """Wait until the page has loaded and its address has stayed the same for a moment
    (a portal can still be redirecting after sign-in). Returns the address."""
    deadline = time.monotonic() + timeout_s
    last, since = page.url, time.monotonic()
    while time.monotonic() < deadline:
        try:
            page.wait_for_load_state("load", timeout=5000)
        except Exception:
            pass
        if page.url != last:
            last, since = page.url, time.monotonic()
        elif time.monotonic() - since >= quiet_s:
            break
        try:
            page.wait_for_timeout(500)
        except Exception:
            pass
    return page.url


def _click_and_wait(page, el, domains, trace, what: str) -> None:
    """Click a tab or menu link and wait for what it opens. A link to another page (an
    older portal that loads a whole new page) is followed until the browser is on that
    page and it has loaded; "network quiet" alone can pass before the new page starts.
    If the portal ends up elsewhere (e.g. sent back to the page before), the link's own
    address (checked like any click, same site only) is opened directly."""
    _settled_url(page)                  # don't click while sign-in is still redirecting
    start = page.url.split("#")[0]

    def link_target() -> str:
        """The link's address, or "" while it has none or points back at this page
        (some portals fill in a menu link's address with a script after loading)."""
        href = ""
        for read in (lambda: el.evaluate("e => (e.closest('a[href]') || e).href || ''",
                                         timeout=5000),
                     lambda: urljoin(page.url, el.get_attribute("href", timeout=5000) or "")):
            try:
                href = read() or ""
            except Exception:
                href = ""
            if href:
                break
        href = href.split("#")[0]
        if not href.lower().startswith(("http://", "https://")) or href == start:
            return ""
        return href

    target = link_target()
    try:
        is_link = bool(el.evaluate("e => !!e.closest('a')", timeout=5000))
    except Exception:
        is_link = False
    deadline = time.monotonic() + 15
    while is_link and not target and time.monotonic() < deadline:   # wait for its address
        page.wait_for_timeout(500)
        target = link_target()
    trace.add("link_target", what=what, url=target or "(none)")
    _safe_click(el, page, domains, trace, what)
    if target:
        try:
            page.wait_for_url(lambda u: u.split("#")[0] != start, timeout=20000)
        except Exception:
            pass
    else:
        try:
            page.wait_for_load_state("load", timeout=20000)
        except Exception:
            pass
    landed = _settled_url(page)
    trace.add("opened", what=what, url=landed)
    if landed.split("#")[0] == start:            # still where it was: the click did nothing
        target = target or link_target()
        if target and host_allowed(target, domains):
            trace.add("followed_link", what=what)
            page.goto(target, timeout=30000)
            trace.add("opened", what=what, url=_settled_url(page))
    elif target and _path(landed) != _path(target) and host_allowed(target, domains):
        trace.add("followed_link", what=what)      # sent somewhere else: open the link
        page.goto(target, timeout=30000)
        trace.add("opened", what=what, url=_settled_url(page))
    _settle(page)


def _download(holder, context, el, page, domains, trace) -> dict | None:
    """Click one document link: a file download, or a PDF opened in a new tab."""
    try:                       # a link to another site, or a risky label: skip, never click
        label = (el.inner_text(timeout=2000) or "")[:80]
        href = el.get_attribute("href") or ""
    except Exception:
        return None
    if check_click(label, urljoin(page.url, href) if href else "", page.url, domains):
        trace.add("download_refused", reason="link outside the allowed domains or not allowed")
        return None
    before, start_url = set(context.pages), page.url
    try:
        with page.expect_download(timeout=20000) as info:
            el.click(timeout=15000)
            trace.add("click", what="a statement")
        d = info.value
        if not host_allowed(d.url, domains):
            trace.add("download_refused", reason="outside the allowed domains")
            return None
        with open(d.path(), "rb") as f:
            data = f.read()
        name = d.suggested_filename or "statement.pdf"
    except FetchStopped:
        raise
    except Exception:
        new = [p for p in context.pages if p not in before]
        if not new and page.url != start_url and host_allowed(page.url, domains):
            try:                   # the PDF opened in the same tab: fetch it, then go back
                r = context.request.get(page.url, timeout=30000)
                data, name = r.body(), page.url.rsplit("/", 1)[-1].split("?")[0]
                page.go_back(timeout=20000)
                _settle(page)
            except Exception:
                return None
            if not data.startswith(b"%PDF-"):
                return None
            trace.add("document_downloaded")
            return {"name": (name or "statement") + ("" if name.lower().endswith(".pdf") else ".pdf"),
                    "data": data}
        if not new:
            return None
        tab = new[-1]
        try:
            tab.wait_for_load_state("load", timeout=20000)
            url = tab.url
            if not host_allowed(url, domains):
                trace.add("download_refused", reason="outside the allowed domains")
                return None
            r = context.request.get(url, timeout=30000)
            data, name = r.body(), url.rsplit("/", 1)[-1].split("?")[0] or "statement.pdf"
        except Exception:
            return None
        finally:
            try:
                tab.close()
            except Exception:
                pass
            holder.page = page
    if not data.startswith(b"%PDF-"):
        trace.add("download_skipped", reason="not a PDF")
        return None
    trace.add("document_downloaded")       # never the file name: it may hold a number
    return {"name": name if name.lower().endswith(".pdf") else name + ".pdf", "data": data}


FUND_PICK_SYSTEM = ("You pick links on an investor portal's dashboard. Reply with JSON only: "
                    '{"ids": [numbers]} listing the links whose text is the name of a fund or '
                    "investment the investor holds. Never settings, documents, profile, help, "
                    "logout, transfer or navigation links.")


# The texts under a column heading (e.g. "Fund Name"): from a table's header if there is
# one, else by position: elements left-aligned under the heading, below it.
COLUMN_JS = r"""(name) => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const want = clean(name).toLowerCase();
  const heads = [...document.querySelectorAll('th, [role=columnheader], div, span, td, p, b, strong')]
    .filter(e => vis(e) && clean(e.innerText).toLowerCase() === want);
  if (!heads.length) return [];
  const head = heads.sort((a, b) => a.querySelectorAll('*').length - b.querySelectorAll('*').length)[0];
  const table = head.closest('table, [role=table], [role=grid]');
  if (table) {
    const row = head.closest('tr, [role=row]');
    const cells = [...row.querySelectorAll('th, td, [role=columnheader], [role=cell], [role=gridcell]')];
    const idx = cells.indexOf(head.closest('th, td, [role=columnheader]'));
    if (idx >= 0) {
      const out = [];
      table.querySelectorAll(table.matches('table') ? 'tr' : '[role=row]').forEach(r => {
        if (r === row) return;
        const cs = r.querySelectorAll('th, td, [role=cell], [role=gridcell], [role=rowheader]');
        if (cs[idx]) { const t = clean(cs[idx].innerText); if (t) out.push(t); }
      });
      if (out.length) return out;
    }
  }
  const h = head.getBoundingClientRect();
  const out = [];
  document.querySelectorAll('a, button, td, [role=cell], [role=gridcell], span, div, p').forEach(e => {
    if (!vis(e) || e.contains(head)) return;
    const r = e.getBoundingClientRect();
    if (r.top < h.bottom - 2 || Math.abs(r.left - h.left) > 12) return;
    if ([...e.children].some(c => clean(c.innerText) === clean(e.innerText))) return;  // inner one
    const t = clean(e.innerText);
    if (t && t.length <= 120 && t.toLowerCase() !== want && !out.includes(t)) out.push(t);
  });
  return out.slice(0, 40);
}"""


def _wait_after_login(holder, steps, trace) -> None:
    """Some portals keep loading their data well after sign-in: wait the login's set
    number of seconds, one second at a time so Cancel and the kill switch still work."""
    try:
        seconds = int(steps.get("post_login_wait") or 0)
    except (TypeError, ValueError):
        seconds = 0
    if seconds <= 0:
        return
    trace.add("wait_after_login", seconds=seconds)
    holder.deadline += seconds          # waiting doesn't count as fetch time
    for _ in range(min(seconds, 180)):
        holder.check()
        holder.page.wait_for_timeout(1000)
    _settle(holder.page)


def _fund_links(page, steps, ctx, trace) -> list[str]:
    """Names of the funds on the dashboard: the cells under the login's fund column (e.g.
    "Fund Name"), else the ones the local model picks from the link names (names only)."""
    column = (steps.get("fund_column") or "").strip()
    if column:
        try:
            names = page.evaluate(COLUMN_JS, column)
        except Exception:
            names = []
        names = [n for n in dict.fromkeys(names)
                 if not re.match(r"^total\b", n, re.I) and not check_click(n, "", page.url, [])]
        trace.add("fund_links_found", count=len(names), how="column")
        if not names:
            raise FetchStopped("not_found", f"Couldn't find a {column!r} column with fund names "
                               "on the dashboard.")
        return names[:20]
    items = page.evaluate(_CLICKABLES_JS)
    labels = [(c["id"], " ".join((c.get("label") or "").split())) for c in items]
    labels = [(i, l) for i, l in labels if 2 < len(l) <= 120]
    from langchain_core.messages import HumanMessage, SystemMessage
    from ..jsonutil import extract_json
    listing = "\n".join(f"[{i}] {l}" for i, l in labels[:150])
    reply = ctx.model_loader().invoke([SystemMessage(FUND_PICK_SYSTEM),
                                       HumanMessage(f"Links:\n{listing}")]).content
    try:
        ids = {int(x) for x in extract_json(reply).get("ids", [])}
    except Exception:
        ids = set()
    names = [l for i, l in labels if i in ids]
    names = [n for n in dict.fromkeys(names) if not check_click(n, "", page.url, [])]
    trace.add("fund_links_found", count=len(names), how="model")
    return names[:20]


PDF_ICON = ("a[href*='.pdf' i], a:has(img[src*='pdf' i]), a:has([class*='pdf' i]), "
            "[class*='pdf' i], img[src*='pdf' i], [title*='pdf' i], [aria-label*='pdf' i], "
            "a:has(img), a:has(i), a:has(svg)")


def _pdf_target(row):
    """In a document's row, the thing to click to get the file: its PDF icon, else the
    row's first link. None if the row has neither (then the matched text is clicked)."""
    for sel in (PDF_ICON, "a"):
        try:
            loc = row.locator(sel)
            for i in range(min(loc.count(), 6)):
                if loc.nth(i).is_visible():
                    return loc.nth(i)
        except Exception:
            continue
    return None


MENU_BUTTON = re.compile(r"^\s*(select|actions?|more|options|⋮|…|\.\.\.)\s*$", re.I)
MENU_CHOICE = re.compile(r"^\s*(download( pdf| file| document)?|view( pdf| document)?|open)\s*$", re.I)


def _expect_file(holder, context, page, domains, trace, click) -> dict | None:
    """Run `click` and catch the file it produces: a download, a PDF in a new tab, or a
    PDF opened in the same tab."""
    before, start_url = set(context.pages), page.url
    data = name = None
    try:
        with page.expect_download(timeout=25000) as info:
            click()
        d = info.value
        if not host_allowed(d.url, domains) and not d.url.startswith(("blob:", "data:")):
            trace.add("download_refused", reason="outside the allowed domains")
            return None
        with open(d.path(), "rb") as f:
            data = f.read()
        name = d.suggested_filename or "statement.pdf"
    except FetchStopped:
        raise
    except Exception:
        page.wait_for_timeout(3000)
        new = [p for p in context.pages if p not in before]
        url = new[-1].url if new else (page.url if page.url != start_url else None)
        if not url or not host_allowed(url, domains):
            return None
        try:
            r = context.request.get(url, timeout=30000)
            data, name = r.body(), url.rsplit("/", 1)[-1].split("?")[0] or "statement"
        except Exception:
            return None
        finally:
            for p in new:
                try:
                    p.close()
                except Exception:
                    pass
            holder.page = page
            if not new:
                try:
                    page.go_back(timeout=20000)
                except Exception:
                    pass
    if not data or not data.startswith(b"%PDF-"):
        trace.add("download_skipped", reason="not a PDF")
        return None
    trace.add("document_downloaded")
    return {"name": name if name.lower().endswith(".pdf") else name + ".pdf", "data": data}


def _download_via_menu(holder, context, row, page, domains, trace) -> dict | None:
    """The row's actions menu ("Select"), then its Download (or View) choice."""
    try:
        btn = row.get_by_role("button", name=MENU_BUTTON)
        if not btn.count():
            btn = row.get_by_text(MENU_BUTTON)
        if not btn.count() or not btn.first.is_visible():
            return None
        btn.first.click(timeout=10000)
        trace.add("click", what="the row's actions menu")
        page.wait_for_timeout(800)
        choice = None
        for role in ("menuitem", "link", "button", "option"):
            loc = page.get_by_role(role, name=MENU_CHOICE)
            for i in range(min(loc.count(), 6)):
                if loc.nth(i).is_visible():
                    choice = loc.nth(i)
                    break
            if choice is not None:
                break
        if choice is None:
            page.keyboard.press("Escape")
            return None
        label = (choice.inner_text(timeout=2000) or "")[:40]
        href = choice.get_attribute("href") or ""
        if check_click(label, urljoin(page.url, href) if href else "", page.url, domains):
            page.keyboard.press("Escape")
            return None
        trace.add("download_via", how="menu")
        return _expect_file(holder, context, page, domains, trace,
                            lambda: choice.click(timeout=10000))
    except FetchStopped:
        raise
    except Exception:
        return None


def _download_via_checkbox(holder, context, row, page, domains, trace) -> dict | None:
    """Tick the row's checkbox and press the page's Download button (a PDF only)."""
    try:
        box = row.locator("input[type=checkbox]").first
        if not box.count() or not box.is_visible():
            return None
        button = _content_control(page, "Download")
        if button is None:
            return None
        box.check(timeout=5000)
        trace.add("download_via", how="checkbox")
        doc = _expect_file(holder, context, page, domains, trace,
                           lambda: button.click(timeout=10000))
        try:
            box.uncheck(timeout=5000)
        except Exception:
            pass
        return doc
    except FetchStopped:
        raise
    except Exception:
        return None


def _fetch_fund_documents(holder, context, login, steps, domains, trace, ctx) -> list[dict]:
    """For each fund on the dashboard: open it, open its documents tab, wait for the list,
    and download the latest file whose name contains the login's document name."""
    tab = steps.get("documents_tab") or "Documents"
    wanted = steps["document_name"]
    home = holder.page.url
    if _classify(holder.page, domains) == "captcha":
        _wait_for_person(holder, login, ctx, domains)
    funds = _fund_links(holder.page, steps, ctx, trace)
    if not funds:
        raise FetchStopped("not_found", "Couldn't tell which links on the dashboard are funds. "
                           "Set the heading of the fund-name column on this login (login steps).")
    docs, seen = [], set()
    for n, fund in enumerate(funds, 1):
        holder.check()
        if n > 1:
            _PageAdapter(holder).goto(home)
        page = holder.page
        el = _find_control(page, fund, ("link", "button", "tab", "menuitem", "row", "cell",
                                        "gridcell"))
        if el is None:                   # a plain text cell: click the text itself
            try:
                loc = page.get_by_text(fund, exact=True)
                el = next((loc.nth(i) for i in range(min(loc.count(), 5))
                           if loc.nth(i).is_visible()), None)
            except Exception:
                el = None
        if el is None:
            trace.add("fund_not_found", n=n)
            continue
        try:                      # a table cell holding the name: click the link in it
            inner = el.locator("a")
            if inner.count() and inner.first.is_visible():
                el = inner.first
        except Exception:
            pass
        _safe_click(el, page, domains, trace, f"fund {n}")
        _settle(page)
        holder.check()
        # the fund page can load slowly: wait for its own Documents tab (not the sidebar's)
        t = _wait_for_content_control(holder, tab, max(60, int(steps.get("post_login_wait") or 0)),
                                      trace)
        page = holder.page
        if t is None:
            trace.add("fund_documents_tab_missing", n=n, url_changed=page.url != home)
            continue
        _safe_click(t, page, domains, trace, "the documents tab")
        _settle(page)
        try:                      # the document list loads after the tab opens
            page.get_by_text(re.compile(re.escape(wanted), re.I)).first.wait_for(timeout=30000)
        except Exception:
            trace.add("fund_documents_none", n=n)
            continue
        if _classify(page, domains) == "captcha":
            _wait_for_person(holder, login, ctx, domains)
            page = holder.page
        found = []
        name_re = re.compile(re.escape(wanted), re.I)
        for role in ("link", "button", "row", "cell"):
            loc = page.get_by_role(role, name=name_re)
            found += [loc.nth(i) for i in range(min(loc.count(), 40))]
        loc = page.get_by_text(name_re)
        found += [loc.nth(i) for i in range(min(loc.count(), 40))]
        targets, rows, row_els, tried = [], [], [], set()
        for el in found:          # one entry per document row
            try:
                if not el.is_visible():
                    continue
                row = el.locator("xpath=ancestor-or-self::*[self::tr or @role='row'][1]")
                row = row.first if row.count() else el
                key = row.evaluate("e => e.outerHTML")[:500]
            except Exception:
                continue
            if key in tried:
                continue
            tried.add(key)
            targets.append(_pdf_target(row) or el)
            row_els.append(row)
            try:
                rows.append(row.inner_text(timeout=2000))
            except Exception:
                rows.append(_row_text(el))
        picked, how = latest_rows(rows)
        trace.add("fund_documents_listed", n=n, count=len(targets), picked=len(picked[:1]),
                  latest=how)
        for i in picked[:1]:      # one capital statement per fund: the latest
            doc = (_download(holder, context, targets[i], page, domains, trace)
                   or _download_via_menu(holder, context, row_els[i], page, domains, trace)
                   or _download_via_checkbox(holder, context, row_els[i], page, domains, trace))
            if doc and doc["data"][:64] not in seen:
                seen.add(doc["data"][:64])
                docs.append(doc)
    trace.add("documents_downloaded", count=len(docs))
    if not docs:
        raise FetchStopped("not_found", f"Opened {len(funds)} fund(s) but found no file named "
                           f"like {wanted!r} that could be downloaded.")
    return docs


PROFILE_IN_USE = re.compile(r"existing browser session|profile is already in use|ProcessSingleton",
                            re.I)


def profile_busy_message(login: dict) -> str:
    """A browser window already has this institution's profile open, usually the window
    from open-browser. Only one window can use a profile at a time."""
    return (f"The browser window for {login['institution']} is still open (for example the "
            "one from open-browser), and only one window can use its saved sign-in at a time. "
            "Finish signing in there, close that window, then Refresh again.")


def _top_level(request) -> bool:
    """Is this navigation for a whole page? A new window's (popup's) first navigation
    has no frame yet: it is a whole page too, so it is checked like one."""
    try:
        return request.frame.parent_frame is None
    except Exception:
        return True


def _frames(page, domains: list[str] | None):
    """The page, then its embedded frames on the login's allowed domains. Some portals
    (Schwab) put the login form in an iframe; a frame from any other site is never used,
    so a password is only ever typed into an allowed site."""
    out = [page]
    if not domains:
        return out
    for f in page.frames:
        try:
            if f != page.main_frame and f.url and host_allowed(f.url, domains):
                out.append(f)
        except Exception:
            continue
    return out


def _login_scope(page, domains: list[str] | None, steps: dict):
    """The page or allowed frame holding the login form (password or username field)."""
    sels = ["input[type=password]", USERNAME_FIELDS]
    for scope in _frames(page, domains):
        if any(_visible(scope.locator(sel)) for sel in sels):
            return scope
    return page


def _find_username(page, steps: dict):
    """The username field, or None: common names first, then the text field in the
    same form as the password. `page` may be a frame."""
    candidates = [page.locator(USERNAME_FIELDS)]
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


def _submit(page, field, steps: dict, scope=None) -> None:
    """Submit the login step: Enter, else the step's sign-in button. scope is the frame
    holding the form, when it's in one."""
    scope = scope or page
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
    buttons = scope.locator(SUBMIT_BUTTONS)
    for i in range(min(buttons.count(), 5)):
        b = buttons.nth(i)
        if b.is_visible():
            b.click(timeout=15000)
            return


def _classify(page, domains: list[str] | None = None) -> str:
    """Also looks inside embedded frames on the allowed domains, where some portals put
    their login form; without that, a login page in a frame looked signed in."""
    scopes = _frames(page, domains)
    has_pw = any(_visible(s.locator("input[type=password]"))
                 or _visible(s.locator(USERNAME_FIELDS)) for s in scopes)   # username-first too
    has_otp = any(_visible(s.locator("input[autocomplete='one-time-code']")) for s in scopes)
    text = _body_text(page)[:5000]
    for f in scopes[1:]:
        try:
            text += "\n" + f.inner_text("body", timeout=5000)[:2000]
        except Exception:
            pass
    frames = [f.url for f in page.frames]
    return classify_page(text, has_pw, has_otp, frames)


def _login(holder: _Holder, login: dict, creds: dict | None, steps: dict,
           ctx: FetchContext) -> None:
    if login.get("sign_in_with") == "google":
        return _google_login(holder, login, creds, ctx)
    page, trace = holder.page, ctx.trace
    domains = registry.domains(login)
    if page.url in ("", "about:blank") or not host_allowed(page.url, domains):
        trace.add("goto_login", url=login["login_url"])
        _PageAdapter(holder).goto(login["login_url"])
        page = holder.page

    counted = False
    for _ in range(5):   # user ID page, password page, then possibly a 2FA page
        state = _classify(page, domains)
        if state == "twofa" and steps.get("otp_button") and any(   # code box beside the password
                _visible(f.locator("input[type=password]")) for f in _frames(page, domains)):
            state = "login"
        trace.add("page_state", state=state, url=page.url)
        if state == "ok":
            return
        if state == "captcha":
            _wait_for_person(holder, login, ctx, domains)
            page = holder.page
            continue
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
        if not host_allowed(page.url, domains):   # never type a password on another site
            raise FetchStopped("login_failed", "The sign-in form isn't on this login's own "
                               "site, so nothing was typed into it.")
        pw_sel = "input[type=password]"
        scope = _login_scope(page, domains, steps)
        if scope is not page:
            trace.add("login_form_in_frame", url=scope.url)
        user = _find_username(scope, steps)
        if user is not None:
            try:
                if not user.input_value():          # leave a remembered user ID alone
                    user.fill(creds["username"])
            except Exception:
                trace.add("username_fill_failed")
        else:
            trace.add("username_field_not_found")
        if not _visible(scope.locator(pw_sel)):
            # Two-step login: submit the user ID, then wait for the password page.
            if user is None:
                raise FetchStopped("login_failed", "Couldn't find the username or password field "
                                   "on the sign-in page.")
            trace.add("submit_username")
            _submit(page, user, steps, scope)
            _settle(page)
            holder.check()
            page = holder.page
            scope = _wait_for_password(page, domains, pw_sel, steps)
            if scope is None:
                continue    # maybe a 2FA, CAPTCHA or other page: classify it next
        pw = scope.locator(pw_sel).first
        pw.fill(creds["password"])
        if steps.get("otp_button"):   # the form sends its code on request: get it first
            _code_on_request(holder, login, scope, steps, ctx)
            page = holder.page
        else:
            trace.add("submit_login")     # values are never traced
            _submit(page, pw, steps, scope)
        passed = _wait_past_login(holder, domains)   # slow portals: wait for the next page
        page = holder.page
        if passed:
            _settle(page)
            holder.check()
            page = holder.page
        else:
            message = _login_message(page, domains)
            trace.add("login_rejected", message=message or "(no message shown)")
            raise FetchStopped("login_failed", "The portal is still showing the login form after "
                               "signing in. Check the saved username and password. Not retrying, "
                               "to avoid a lockout.")
    raise FetchStopped("unexpected", "Login didn't finish after several pages.")


# --- Sign in with Google ------------------------------------------------------------
# The fetcher never types into Google's pages: no password, no code. The owner signs in
# by hand once (open-browser) and the profile keeps the Google session. On each run the
# fetcher only presses the portal's "Sign in with Google" button and picks the account
# in Google's chooser. Anything else Google asks for stops the run with a question.

GOOGLE_DOMAINS = ["accounts.google.com"]
GOOGLE_BUTTON = re.compile(r"\b(sign in|log in|login|continue|sign up)\s+(with|using)\s+google\b", re.I)
GOOGLE_INSECURE = re.compile(r"browser or app may not be secure|couldn.?t sign you in", re.I)
GOOGLE_CONSENT = re.compile(r"wants (additional )?access|wants to access|\ballow\b.*\baccess\b"
                            r"|to continue, google will share", re.I)


def is_google(url: str) -> bool:
    return host_allowed(url, GOOGLE_DOMAINS) and url.startswith("https://")


def google_step(text: str, accounts: list[str], wanted: str | None,
                has_input: bool) -> tuple[str, str]:
    """What to do on a Google page: ("choose", account) to click an account in the
    chooser, ("wait", "") while Google is only redirecting, or ("stop", why). Never
    anything that types."""
    if GOOGLE_INSECURE.search(text):
        return "stop", ("Google refused to sign in from the fetcher's browser (\"This browser "
                        "or app may not be secure\"). Google sign-in can't be used for this "
                        "login; use a username and password if the site offers one, or upload "
                        "statements instead.")
    if GOOGLE_CONSENT.search(text):
        return "stop", ("Google is asking you to grant access to the site. Only you can do "
                        "that: sign in by hand once with open-browser.")
    if accounts:
        want = (wanted or "").strip().lower()
        match = [a for a in accounts if a.lower() == want] if want else []
        if match:
            return "choose", match[0]
        if len(accounts) == 1 and not want:
            return "choose", accounts[0]
        return "stop", ("Google's account chooser shows " + (
            "several accounts" if not want else "no account matching the saved username")
            + ". Save the Google account's email as this login's username, or sign in by "
            "hand with open-browser.")
    if has_input:
        return "stop", ("Google wants your password or a verification step. The fetcher "
                        "never types into Google's pages: sign in by hand once with "
                        "open-browser.")
    return "wait", ""


def _google_scopes(page, domains):
    """The page and its frames on the login's own site or Google's sign-in button frame."""
    out = [page]
    for f in page.frames:
        try:
            if f != page.main_frame and f.url and (host_allowed(f.url, domains) or is_google(f.url)):
                out.append(f)
        except Exception:
            continue
    return out


def _google_button(page, domains):
    """The portal's "Sign in with Google" button, if one is showing."""
    if is_google(page.url):
        return None
    for scope in _google_scopes(page, domains):
        for role in ("button", "link"):
            try:
                loc = scope.get_by_role(role, name=GOOGLE_BUTTON)
                for i in range(min(loc.count(), 3)):
                    if loc.nth(i).is_visible():
                        return loc.nth(i)
            except Exception:
                continue
        try:
            loc = scope.get_by_text(GOOGLE_BUTTON)
            if loc.count() and loc.first.is_visible():
                return loc.first
        except Exception:
            pass
    return None


def _state(page, login: dict, domains) -> str:
    """Page state; for a Google login, a page showing the Google button is a login page."""
    state = _classify(page, domains)
    if state == "ok" and login.get("sign_in_with") == "google" and (
            is_google(page.url) or _google_button(page, domains) is not None):
        return "login"
    return state


def _live_page(holder) -> object:
    """The page to work on: Google's sign-in may open a popup and close it again."""
    try:
        if not holder.page.is_closed():
            return holder.page
        pages = [p for p in holder.page.context.pages if not p.is_closed()]
        if pages:
            holder.page = pages[-1]
    except Exception:
        pass
    return holder.page


def _sign_in_by_hand(login: dict, why: str) -> str:
    return (f"{why} Run `.venv/bin/python -m portfolio_agent.cli open-browser {login['id']}`, "
            "choose Sign in with Google and finish signing in, close the window, then "
            "Refresh again.")


def _google_login(holder: _Holder, login: dict, creds: dict | None, ctx: FetchContext) -> None:
    trace = ctx.trace
    domains = registry.domains(login)
    page = _live_page(holder)
    if page.url in ("", "about:blank") or not host_allowed(page.url, domains):
        trace.add("goto_login", url=login["login_url"])
        _PageAdapter(holder).goto(login["login_url"])
    counted, widened = False, False
    try:
        for _ in range(8):
            holder.check()
            page = _live_page(holder)
            _settle(page)
            if is_google(page.url):
                text = _body_text(page)[:5000]
                tiles = page.locator("[data-identifier]")
                accounts = []
                for i in range(min(tiles.count(), 10)):
                    try:
                        if tiles.nth(i).is_visible():
                            accounts.append(tiles.nth(i).get_attribute("data-identifier") or "")
                    except Exception:
                        continue
                has_input = _visible(page.locator("input:not([type=hidden])"))
                action, arg = google_step(text, [a for a in accounts if a],
                                          (creds or {}).get("username"), has_input)
                trace.add("google_page", step=action)       # never the account's email
                if action == "stop":
                    raise FetchStopped("google_sign_in", _sign_in_by_hand(login, arg))
                if action == "choose":
                    for i in range(min(tiles.count(), 10)):
                        if tiles.nth(i).get_attribute("data-identifier") == arg:
                            tiles.nth(i).click(timeout=15000)
                            break
                    trace.add("google_account_chosen")
                else:
                    page.wait_for_timeout(1500)
                try:
                    page.wait_for_url(lambda u: not is_google(u), timeout=30000)
                except Exception:
                    pass
                continue
            button = _google_button(page, domains)
            if button is None:
                state = _classify(page, domains)
                trace.add("page_state", state=state, url=page.url)
                if state == "ok":
                    return
                if state == "twofa":          # the portal's own code step, after Google
                    _two_factor(holder, login, ctx)
                    continue
                if state == "captcha":
                    _wait_for_person(holder, login, ctx, domains)
                    continue
                if state == "security_question":
                    raise FetchStopped(state, _sign_in_by_hand(
                        login, "The portal asked for a check only you can answer."))
                raise FetchStopped("login_failed", "The sign-in page shows no \"Sign in with "
                                   "Google\" button. If the button is labelled differently, "
                                   "sign in by hand with open-browser and tell me its label.")
            if not counted:
                _check_login_cap(ctx, login)
                store.record_login(ctx.conn, login["id"], ctx.run_kind)
                counted = True
            if not widened:   # Google's sign-in pages, for this step only
                holder.nav_domains += GOOGLE_DOMAINS
                widened = True
            trace.add("google_button_clicked", url=page.url)
            url = page.url
            button.click(timeout=15000)
            try:
                page.wait_for_url(lambda u: u != url, timeout=15000)
            except Exception:
                pass
        raise FetchStopped("google_sign_in", _sign_in_by_hand(
            login, "Sign in with Google didn't finish after several pages."))
    finally:
        if widened:
            holder.nav_domains = [d for d in holder.nav_domains if d not in GOOGLE_DOMAINS]


LOGIN_MESSAGE = ("[role=alert], [aria-live=assertive], [aria-live=polite], .error, .alert,"
                 " [class*='error' i], [id*='error' i], [class*='alert' i]")


def _login_message(page, domains) -> str | None:
    """The message the portal shows next to its login form after a failed sign-in
    (e.g. "incorrect password" vs a generic "unable to process"), for the Debug tab.
    Page text only: typed values aren't part of it."""
    for scope in _frames(page, domains):
        try:
            loc = scope.locator(LOGIN_MESSAGE)
            for i in range(min(loc.count(), 10)):
                el = loc.nth(i)
                if el.is_visible():
                    t = " ".join(el.inner_text(timeout=2000).split())
                    if 3 < len(t) < 400:
                        return t[:300]
        except Exception:
            continue
    return None


LOGIN_ERROR = re.compile(r"invalid|incorrect|wrong|expired|failed|does ?n.?t match|not match"
                         r"|locked|try again|unsuccessful|not recogni[sz]ed", re.I)


def _on_login_form(page, domains, start_url: str | None = None) -> bool:
    """The sign-in form is still showing: a password box, or (while the address hasn't
    changed) an editable username box. Once the portal has moved to another address
    with no password box, it's past the form even if that page has a user-ID field."""
    if any(_visible(f.locator("input[type=password]")) for f in _frames(page, domains)):
        return True
    if start_url and page.url.split("#")[0] != start_url.split("#")[0]:
        return False
    return _classify(page, domains) == "login"


def _wait_past_login(holder: _Holder, domains, timeout_s: float | None = None) -> bool:
    """After pressing sign-in, wait until the login form is gone. Some portals keep the
    form (or a spinner over it) on screen for a while, or reload the login page once,
    before the next page loads. True once past it. False when the form is still there
    at the deadline, or sooner if the portal shows a sign-in error next to it."""
    started = time.monotonic()
    deadline = started + (timeout_s or config.LOGIN_WAIT_S)
    start_url = holder.page.url
    while True:
        holder.check()
        page = holder.page
        try:
            page.wait_for_load_state("load", timeout=5000)
        except Exception:
            pass
        if not _on_login_form(page, domains, start_url):
            return True
        now = time.monotonic()
        if now > deadline:
            return False
        if now - started > 8:            # give a status notice time to turn into an error
            message = _login_message(page, domains)
            if message and LOGIN_ERROR.search(message):
                return False
        try:
            page.wait_for_timeout(1000)
        except Exception:
            pass    # the page navigated away mid-wait; check again


def _wait_for_password(page, domains, pw_sel: str, steps: dict, timeout_s: float = 20):
    """The page or allowed frame where the password field shows up, or None."""
    deadline = time.monotonic() + timeout_s
    while True:
        scope = _login_scope(page, domains, steps)
        if _visible(scope.locator(pw_sel)):
            return scope
        if time.monotonic() > deadline:
            return None
        page.wait_for_timeout(500)


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


LOGIN_BUTTON = re.compile(r"^\s*(log ?in|sign ?in|submit|verify|continue)\s*$", re.I)


def _code_field(scope):
    """The box for the code: a code/OTP field, else the one empty visible text box that
    isn't the username or password."""
    field = scope.locator(CODE_FIELD_VISIBLE).first
    if _visible(field):
        return field
    boxes = scope.locator("input[type=text]:visible, input[type=tel]:visible, "
                          "input[type=number]:visible, input:not([type]):visible")
    empty = []
    for i in range(min(boxes.count(), 10)):
        b = boxes.nth(i)
        try:
            if not b.input_value():
                empty.append(b)
        except Exception:
            continue
    return empty[0] if len(empty) == 1 else None


def _emailed_code(login: dict, steps: dict, since, ctx: FetchContext) -> tuple[str | None, str]:
    """Read an emailed code from Gmail (when set up), from the login's own domains or its
    code sender. Returns (code, why not). The code is never traced."""
    from .. import email_codes
    settings = email_codes.get_settings(ctx.conn)
    if not settings:
        return None, ("Reading codes from Gmail isn't set up (Accounts & logins → Verification "
                      "codes by email). ")
    senders = [x for x in re.split(r"[,\s]+", steps.get("code_sender") or "") if x] \
        or registry.domains(login)
    with email_codes.EMAIL_LOCK:          # one emailed code expected at a time
        try:
            code = email_codes.wait_for_code(settings, since, senders, should_stop=lambda:
                                             config.KILL_FILE.exists() or ctx.should_cancel())
        except email_codes.EmailUnavailable as e:
            return None, str(e) + " "
    if code:
        ctx.trace.add("email_code_read")
        return code, ""
    return None, f"No email with a code arrived within {config.EMAIL_WAIT_S} seconds. "


def _code_on_request(holder: _Holder, login: dict, scope, steps: dict,
                     ctx: FetchContext) -> None:
    """A sign-in form with a "send code" button (e.g. "Generate New OTP"): with the
    username and password filled, press it, get the code (sent by email or text), type
    it into the form's code box, then press the form's sign-in button."""
    page, trace = holder.page, ctx.trace
    label = steps["otp_button"]
    button = None
    for pattern in (_exact(label), re.compile(re.escape(label.strip()), re.I)):
        for role in ("button", "link"):
            loc = scope.get_by_role(role, name=pattern)
            if _visible(loc):
                button = loc.first
                break
        if button is not None:
            break
    if button is None:
        raise FetchStopped("login_failed", f"Couldn't find the {label!r} button on the sign-in "
                           "form. Check its label under Accounts & logins → login steps.")
    from ..timeutil import now as utcnow
    since = utcnow() - timedelta(seconds=15)     # a little slack for the mail server's clock
    button.click(timeout=15000)
    trace.add("code_requested", method=login.get("twofa_method"))
    field = None
    for _ in range(30):                 # the code box can appear after the press
        field = _code_field(scope)
        if field is not None:
            break
        page.wait_for_timeout(1000)
    if field is None:
        raise FetchStopped("twofa_unavailable", f"Pressed {label!r}, but no box for the code "
                           "showed up on the sign-in form.")
    holder.check()
    where = "text message" if login.get("twofa_method") == "sms" else "email"
    code, why = None, ""
    if where == "email":
        code, why = _emailed_code(login, steps, since, ctx)
        holder.check()
    if not code:
        code = ctx.request_code(f"{login['institution']} sent a verification code by {where} for "
                                f"the {login['name']} login. {why}Enter the code you received.")
    holder.check()
    if not code:
        raise FetchStopped("twofa_unavailable", "No verification code was entered in time.")
    field.fill(code.strip())
    trace.add("code_entered")           # the code itself is never traced
    sign_in = scope.get_by_role("button", name=LOGIN_BUTTON)
    if not _visible(sign_in):
        sign_in = scope.locator("input[type=submit]:visible")
    if not _visible(sign_in):
        raise FetchStopped("login_failed", "Typed the code but couldn't find the sign-in "
                           "button on the form.")
    sign_in.first.click(timeout=15000)
    trace.add("submit_login")
    holder.check()                      # _login waits for the next page


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
                                             should_stop=lambda: config.KILL_FILE.exists()
                                             or ctx.should_cancel())
                    if code:
                        trace.add("sms_code_read")          # never the code itself
                    else:
                        why = f"No text with a code arrived within {config.SMS_WAIT_S} seconds. "
                except sms.SmsUnavailable as e:
                    why = str(e) + " "
    elif method == "email":       # the code page is showing: the email may already be in
        code, why = _emailed_code(login, registry.steps_for(ctx.conn, login["institution"]),
                                  utcnow() - timedelta(minutes=3), ctx)
    else:
        why = f"Automatic reading of {method} codes isn't built yet. "
    holder.check()        # cancelled (or kill switch) while waiting for a text
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


# A link to one account's own page: its URL names an account details/activity page,
# or its label ends in a masked account number ("Checking - 1234", "Card ...1234").
_DETAIL_HREF = re.compile(r"detail|activity|account-?summary", re.I)
_MASKED_LABEL = re.compile(r"(?:\.{2,}|…|[xX*]{2,}|-)\s?[A-Z0-9]{4}\s*$")


def _name(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def link_name(label: str) -> str:
    """A link's account name without a masked number after it ("Red ...1234" -> "red")."""
    return _name(_MASKED_LABEL.sub("", label or ""))


# The account number on an account's own page: "Account number: ****1234",
# "Acct # ...1234", "ending in 1234", "Card ending 1234", "Checking - 1234".
_ACCOUNT_NO = [
    re.compile(r"(?:account|acct|card|loan)\s*(?:number\b|no\b\.?|num\b|#)\s*:?\s*[-x*•.…\s]*"
               r"(?:[A-Z0-9]+[-\s]){0,3}([A-Z0-9]{4})\b", re.I),
    re.compile(r"ending\s*(?:in|with)?\s*:?\s*[-x*•.…\s]*([A-Z0-9]{4})\b", re.I),
    re.compile(r"(?:\.{2,}|…|[xX*•]{2,})\s?([A-Z0-9]{4})\b"),
]


def account_number_suffix(text: str) -> str | None:
    """Last 4 of the account number on a details page, read by code (never the model).
    Only the top of the page is read, so a number in the transaction list isn't picked up."""
    top = (text or "")[:3000]
    for pat in _ACCOUNT_NO:
        m = pat.search(top)
        if m and re.search(r"\d", m.group(1)):
            return m.group(1).upper()
    return None


# Shown after clicking a link such as "Account & Routing Numbers": the full number,
# e.g. "Account number 0000 1234 5678". Only the last 4 digits are kept anywhere.
_SHOWN_NO = re.compile(r"(?:account|acct)\s*(?:number|no\b\.?|num\b|#)\s*:?\s*"
                       r"([-x*•.…\s\d]{4,40})", re.I)


def revealed_number_suffix(before: str, after: str) -> str | None:
    """Last 4 digits of the account number that appeared after clicking the number link.
    Text new since the click is searched first (the panel that opened), then the page.
    A routing number is labelled as such and isn't matched."""
    old = set((before or "").splitlines())
    new = "\n".join(l for l in (after or "").splitlines() if l not in old)
    for text in (new, after or ""):
        for m in _SHOWN_NO.finditer(text):
            digits = re.sub(r"\D", "", m.group(1))
            if len(digits) >= 4:
                return digits[-4:]
    return None


def number_link(clickables: list[dict], text: str, base_url: str, domains: list[str]) -> dict | None:
    """The clickable whose label is the configured number link, if it's safe to click."""
    want = _name(text)
    for c in clickables:
        label = (c.get("label") or "").strip()
        if want and want in _name(label) and len(_name(label)) <= len(want) + 20:
            href = (c.get("href") or "").strip()
            url = urljoin(base_url, href) if href and not href.startswith(("javascript:", "#")) else ""
            if url and not host_allowed(url, domains):
                continue
            if check_click(label, url, base_url, domains):
                continue
            return c
    return None


def detail_links(clickables: list[dict], base_url: str, domains: list[str],
                 known_names: list[str] | None = None) -> list[dict]:
    """Links on an overview page that open a single account's page, deduplicated.
    A link counts if its URL names an account page, its label ends in a masked number,
    or its label is the name of an account found on an earlier fetch.
    Only same-site, safe links (never transfer/pay/sign-out and the like)."""
    known = {_name(n) for n in known_names or [] if _name(n)}
    out, seen = [], set()
    for c in clickables:
        href, label = (c.get("href") or "").strip(), (c.get("label") or "").strip()
        if not label or not (_DETAIL_HREF.search(href) or _MASKED_LABEL.search(label)
                             or link_name(label) in known):
            continue
        url = urljoin(base_url, href) if href and not href.startswith(("javascript:", "#")) else ""
        if url and not host_allowed(url, domains):
            continue              # e.g. a Merrill account on a Bank of America page
        if check_click(label, url, base_url, domains):
            continue
        key = url or label
        if key in seen:
            continue
        seen.add(key)
        out.append({"label": label, "url": url})
    return out


def _account_details(holder: _Holder, pages: list[dict], domains: list[str],
                     trace: Trace, known_names: list[str] | None = None,
                     number_link_text: str | None = None) -> list[dict]:
    """Open each account's own page from the overview(s) and capture it.
    With number_link_text, also click that link on each page (e.g. "Account & Routing
    Numbers") and take the last 4 of the account number it shows; the full number is
    never saved, traced or given to the model. A page that fails is skipped; the
    overview values are still read."""
    adapter, found = _PageAdapter(holder), []
    for p in pages:
        adapter.goto(p["url"])
        for link in detail_links(adapter.clickables(), p["url"], domains, known_names):
            found.append({**link, "from": p["url"]})
    found = found[:config.MAX_DETAIL_PAGES]
    trace.add("account_detail_links", count=len(found))
    out, n = [], len(pages)
    for link in found:
        try:
            if link["url"]:
                adapter.goto(link["url"])
            else:                 # script link: click it on its overview page
                adapter.goto(link["from"])
                target = next((c for c in adapter.clickables()
                               if (c.get("label") or "").strip() == link["label"]), None)
                if not target:
                    trace.add("account_detail_missing", label=link["label"][:80])
                    continue
                adapter.click(target["id"])
            if _classify(holder.page, domains) != "ok":
                trace.add("account_detail_stopped", label=link["label"][:80])
                break             # signed out or a prompt appeared: keep what we have
            n += 1
            page = _capture_page(holder.page, n)
            page["detail"] = True
            page["label"] = link["label"]
            if number_link_text:
                page["suffix"] = _reveal_number(adapter, holder, page, number_link_text,
                                                domains, link["label"], trace)
            else:
                page["suffix"] = account_number_suffix(page["text"])
            out.append(page)
            trace.add("account_detail_captured", label=link["label"][:80], url=page["url"],
                      number_found=bool(page["suffix"]))
        except FetchStopped:
            raise
        except Exception as e:
            trace.add("account_detail_failed", label=link["label"][:80], error=type(e).__name__)
    return out


def _reveal_number(adapter, holder: _Holder, page: dict, link_text: str,
                   domains: list[str], label: str, trace: Trace) -> str | None:
    """Click the number link on a captured details page and read the last 4 it shows.
    No fallback to the page's own masked number: for these portals it's a different
    number, so a miss leaves the number unread rather than wrong."""
    target = number_link(adapter.clickables(), link_text, page["url"], domains)
    if not target:
        trace.add("account_number_link_missing", label=label[:80], link=link_text[:80])
        return None
    adapter.click(target["id"])
    try:
        holder.page.wait_for_timeout(1500)       # panels often animate open
    except Exception:
        pass
    suffix = revealed_number_suffix(page["text"], _body_text(holder.page))
    trace.add("account_number_link_clicked", label=label[:80], number_found=bool(suffix))
    return suffix


def _check_login_cap(ctx: FetchContext, login: dict) -> None:
    if ctx.run_kind != "nightly":
        return   # Refresh limits are to be set over time (spec C15)
    since = to_iso(now() - timedelta(hours=12))
    n = store.logins_since(ctx.conn, login["id"], since, "nightly")
    if n >= config.MAX_LOGINS_PER_NIGHT:
        raise FetchStopped("login_cap", f"Already tried {n} logins tonight; the limit is "
                           f"{config.MAX_LOGINS_PER_NIGHT} per portal per night.")


# Tables as rows ("cell | cell"), and values shown in fields and dropdowns (an "As of"
# date picker, say), which a page's visible text leaves out. Never a password, username,
# email address or code field.
STRUCTURE_JS = r"""() => {
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const out = [];
  document.querySelectorAll('table, [role=table], [role=grid], [role=treegrid]').forEach((t, ti) => {
    if (!vis(t) || t.parentElement.closest('table, [role=table], [role=grid]')) return;
    const html = t.matches('table');
    const lines = [];
    t.querySelectorAll(html ? 'tr' : '[role=row]').forEach(r => {
      const cells = r.querySelectorAll(html ? 'th, td'
        : '[role=cell], [role=gridcell], [role=columnheader], [role=rowheader]');
      const vals = [...cells].map(c => {
        const text = clean(c.innerText);
        const tipEl = c.matches('[title]') ? c : c.querySelector('[title]');
        const tip = clean((tipEl && tipEl.getAttribute('title')) || c.getAttribute('aria-label'));
        return tip && tip !== text ? (text ? text + ' (' + tip + ')' : tip) : text;
      });
      if (vals.some(v => v)) lines.push(vals.join(' | '));
    });
    if (lines.length) out.push('=== Table ' + (ti + 1) + ' (one row per line) ===\n' + lines.join('\n'));
  });
  const secret = /pass|user|login|email|e-mail|otp|code|pin|ssn|card|account.?num/i;
  const fields = [];
  document.querySelectorAll('input, select').forEach(e => {
    if (!vis(e)) return;
    const type = (e.type || '').toLowerCase();
    if (['password', 'hidden', 'checkbox', 'radio', 'submit', 'button', 'file', 'email', 'tel']
        .includes(type)) return;
    const lab = e.id && document.querySelector('label[for="' + CSS.escape(e.id) + '"]');
    const label = clean((lab && lab.innerText) || e.getAttribute('aria-label') || e.placeholder || '');
    const hint = [e.name, e.id, e.getAttribute('autocomplete'), label].join(' ');
    if (secret.test(hint)) return;
    const v = clean(e.tagName === 'SELECT' ? (e.options[e.selectedIndex] || {}).text : e.value);
    if (v) fields.push((label ? label + ': ' : '') + v);
  });
  if (fields.length) out.push('=== Values shown in fields and dropdowns ===\n' + fields.join('\n'));
  return out.join('\n\n');
}"""


def _structure(page) -> str:
    try:
        return page.evaluate(STRUCTURE_JS) or ""
    except Exception:
        return ""


def _capture_page(page, n: int) -> dict:
    _settle(page)
    text, extra = _body_text(page), _structure(page)
    return {"n": n, "url": page.url, "text": text + ("\n\n" + extra if extra else ""),
            "png": page.screenshot(full_page=True), "html": page.content()}


def _failure_evidence(page, login: dict, trace: Trace) -> str | None:
    """Screenshot and text of the page a run stopped on. Password fields show as dots."""
    try:
        folder = config.EVIDENCE_DIR / f"login-{login['id']}" / (now_iso().replace(":", "") + "-stopped")
        folder.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(folder / "stopped.png"), full_page=True, timeout=15000)
        (folder / "stopped.txt").write_text(_body_text(page), encoding="utf-8")
        try:                  # the page's structure, to find out why a step failed
            (folder / "stopped.html").write_text(page.content(), encoding="utf-8")
        except Exception:
            pass
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
        if p.get("detail"):   # name, number and balance are at the top; transactions follow
            number = f", account number ending {p['suffix']}" if p.get("suffix") else ""
            texts.append(f"=== Account details page {p['n']} for {p['label']!r}{number}: "
                         f"{p['url']} ===\n" + p["text"][:config.DETAIL_PAGE_CHARS])
        else:
            texts.append(p["text"] if len(pages) == 1
                         else f"=== Page {p['n']}: {p['url']} ===\n{p['text']}")
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
    details = [{"label": p["label"], "suffix": p["suffix"]} for p in pages if p.get("detail")]
    if details:
        (folder / "details.json").write_text(json.dumps(details, indent=2))
    return Evidence(text=text, folder=str(folder), url=pages[0]["url"], captured_at=captured_at,
                    proposal=proposal, trace=trace.steps, details=details)


def browser_profile(login: dict):
    """(profile folder, Playwright channel). A "Sign in with Google" login runs in the
    installed Google Chrome (channel "chrome") with its own profile: Google refuses sign-in
    from the testing build of Chrome, and the two builds can't share a profile."""
    base = config.BROWSER_PROFILES_DIR / _slug(login["institution"])
    if login.get("sign_in_with") == "google":
        if not config.GOOGLE_CHROME_APP.exists():
            raise FetchStopped("config", "Sign in with Google needs Google Chrome installed "
                               f"(looked for {config.GOOGLE_CHROME_APP}).")
        return base.with_name(base.name + "-chrome"), "chrome"
    return base, None


def open_browser_for_manual_login(login: dict) -> None:
    """Open the login's browser profile so the user can sign in by hand once
    (and tick "remember this device"). Nothing is read or saved by the agent."""
    from playwright.sync_api import sync_playwright

    profile, channel = browser_profile(login)
    profile.mkdir(parents=True, exist_ok=True)
    if channel == "chrome":
        # Google's sign-in: an ordinary Chrome window, not one the agent controls. The
        # owner signs in to Google there; runs later reuse that profile's session.
        import subprocess
        print("Opening Google Chrome with this login's own profile (your everyday Chrome is "
              "not touched). Press 'Sign in with Google', sign in, then quit this Chrome "
              "window (Cmd+Q).")
        subprocess.run([str(config.GOOGLE_CHROME_APP), f"--user-data-dir={profile}",
                        "--no-first-run", "--no-default-browser-check", login["login_url"]])
        return
    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(str(profile), headless=False,
                                                       viewport={"width": 1400, "height": 1000})
        page = browser.pages[0] if browser.pages else browser.new_page()
        page.goto(login["login_url"])
        print("Sign in in the browser window, choose 'remember this device' if offered,"
              " then close the window.")
        if login.get("sign_in_with") == "google":
            print("This login signs in with Google: press 'Sign in with Google' and finish"
                  " signing in to Google here. Later runs reuse that Google session.")
        try:
            page.wait_for_event("close", timeout=0)
        except Exception:
            pass
        browser.close()


def _slug(s: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in s.lower()).strip("_") or "portal"


__all__ = ["Evidence", "FetchContext", "FetchStopped", "fetch_portal",
           "open_browser_for_manual_login"]

"""Core contact-page + form discovery engine.

Given a domain, formscout:
  1. Fetches the homepage (https, falling back to http).
  2. Finds the contact page — via common URL paths and via links
     on the homepage whose text/href looks like contact/support.
  3. Fetches each candidate contact page and inspects it for forms,
     mailto: emails, tel: phone numbers, and obstacles
     (password gates, bot challenges, dead pages).

Everything is plain HTTP — no browser needed, so it runs fast over
hundreds of domains. JavaScript-rendered forms may be missed; those
are flagged for a manual/browser pass.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Common contact-page paths, tried in order. Shopify stores live under /pages/*.
CONTACT_PATHS = [
    "/contact",
    "/contact-us",
    "/contactus",
    "/support",
    "/help",
    "/get-in-touch",
    "/pages/contact",
    "/pages/contact-us",
    "/pages/support",
    "/customer-service",
    "/contact.html",
]

# Anchor text / href that suggests a contact page.
CONTACT_HINT = re.compile(r"contact|support|get[ -]?in[ -]?touch|reach[ -]?us|help", re.I)

# Signs the storefront is gated / not a real page.
PASSWORD_GATE_HINT = re.compile(
    r"enter using password|opening soon|store will be launching|password page", re.I
)
BOT_CHALLENGE_HINT = re.compile(
    r"just a moment|checking your browser|attention required|cf-chl|cf_clearance", re.I
)

# Field-name hints used to summarise what a form asks for.
FIELD_HINTS = {
    "name": re.compile(r"name|full.?name|first.?name|last.?name", re.I),
    "email": re.compile(r"e.?mail", re.I),
    "phone": re.compile(r"phone|tel|mobile|contact.?number", re.I),
    "message": re.compile(r"message|comment|inquiry|enquiry|details|question|note", re.I),
    "subject": re.compile(r"subject|topic|reason", re.I),
    "company": re.compile(r"company|business|organisation|organization", re.I),
}


@dataclass
class FormInfo:
    action: str = ""
    method: str = "get"
    fields: list[str] = field(default_factory=list)

    def summary(self) -> str:
        kinds = set()
        for f in self.fields:
            for kind, rx in FIELD_HINTS.items():
                if rx.search(f):
                    kinds.add(kind)
        return ",".join(sorted(kinds)) if kinds else f"{len(self.fields)} fields"


@dataclass
class DiscoveryResult:
    domain: str
    contact_url: str = ""
    http_status: str = ""
    has_form: bool = False
    form_count: int = 0
    form_fields: str = ""
    emails_found: str = ""
    phones_found: str = ""
    notes: str = ""

    def as_row(self) -> dict:
        return {
            "domain": self.domain,
            "contact_url": self.contact_url,
            "http_status": self.http_status,
            "has_form": "yes" if self.has_form else "no",
            "form_count": self.form_count,
            "form_fields": self.form_fields,
            "emails_found": self.emails_found,
            "phones_found": self.phones_found,
            "notes": self.notes,
        }


def normalize_domain(raw: str) -> str:
    """Strip scheme, path, whitespace -> bare domain."""
    raw = raw.strip().lower()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    host = urlparse(raw).netloc or urlparse(raw).path.split("/")[0]
    return host.strip().lstrip(".")


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-GB,en;q=0.9"})
    return s


def _fetch(session: requests.Session, url: str, timeout: float):
    """Return (status_code, text, final_url) or (None, "", "") on failure."""
    try:
        r = session.get(url, timeout=timeout, allow_redirects=True)
        return r.status_code, r.text or "", r.url
    except requests.RequestException:
        return None, "", ""


def _same_domain(url: str, domain: str) -> bool:
    host = urlparse(url).netloc.lower().lstrip(".")
    d = domain.lower().lstrip(".")
    return host == d or host.endswith("." + d)


def _contact_links(soup: BeautifulSoup, base_url: str, domain: str) -> list[str]:
    """Homepage anchors whose text or href hints at a contact page."""
    found: list[str] = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        text = a.get_text(" ", strip=True)
        if CONTACT_HINT.search(text) or CONTACT_HINT.search(href):
            abs_url = urljoin(base_url, href)
            if _same_domain(abs_url, domain) and abs_url not in seen:
                seen.add(abs_url)
                found.append(abs_url)
    return found


def _obstacles(soup: BeautifulSoup, text: str, status: int | None) -> str:
    notes = []
    if status in (403, 429):
        notes.append(f"blocked http {status}")
    title = (soup.title.string if soup.title and soup.title.string else "").strip()
    body = soup.get_text(" ", strip=True)[:2000]
    if PASSWORD_GATE_HINT.search(title + " " + body):
        notes.append("password-gated")
    if BOT_CHALLENGE_HINT.search(title + " " + text[:2000]):
        notes.append("bot-challenge")
    return "; ".join(notes)


def _analyze_forms(soup: BeautifulSoup) -> list[FormInfo]:
    forms = []
    for form in soup.find_all("form"):
        fields = []
        for el in form.find_all(["input", "textarea", "select"]):
            name = el.get("name") or el.get("id") or el.get("placeholder") or ""
            ftype = el.get("type", "text")
            if ftype.lower() in ("hidden", "submit", "button", "checkbox", "radio"):
                continue
            if name:
                fields.append(name.strip()[:40])
        # Deduplicate while preserving order
        seen, uniq = set(), []
        for f in fields:
            if f not in seen:
                seen.add(f)
                uniq.append(f)
        forms.append(
            FormInfo(
                action=form.get("action", "") or "",
                method=(form.get("method", "get") or "get").lower(),
                fields=uniq,
            )
        )
    return forms


def _contact_points(soup: BeautifulSoup) -> tuple[list[str], list[str]]:
    emails, phones = [], []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().startswith("mailto:"):
            addr = href[7:].split("?")[0].strip()
            if addr and addr not in emails:
                emails.append(addr)
        elif href.lower().startswith("tel:"):
            num = href[4:].strip()
            if num and num not in phones:
                phones.append(num)
    return emails[:5], phones[:5]


def discover(domain: str, timeout: float = 15) -> DiscoveryResult:
    """Run full contact discovery for one domain."""
    domain = normalize_domain(domain)
    result = DiscoveryResult(domain=domain)
    if not domain:
        result.notes = "empty domain"
        return result

    session = _session()

    # 1. Homepage (https, fall back to http)
    status, html, final_url = _fetch(session, f"https://{domain}/", timeout)
    if status is None:
        status, html, final_url = _fetch(session, f"http://{domain}/", timeout)
    if status is None:
        result.notes = "unreachable"
        result.http_status = "dns/timeout"
        return result
    if status >= 400:
        result.http_status = str(status)
        result.notes = f"homepage http {status}"
        return result

    soup = BeautifulSoup(html, "html.parser")
    gate_note = _obstacles(soup, html, status)
    if "password-gated" in gate_note:
        result.http_status = str(status)
        result.contact_url = final_url
        result.notes = "password-gated (homepage)"
        return result

    # 2. Candidate contact URLs: homepage links first, then common paths
    candidates = _contact_links(soup, final_url, domain)
    for path in CONTACT_PATHS:
        url = f"https://{domain}{path}"
        if url not in candidates:
            candidates.append(url)

    # 3. Walk candidates until one yields a form (or we run out)
    form_infos: list[FormInfo] = []
    picked_url, picked_status = "", ""
    mails, tels = [], []
    last_note = ""
    for url in candidates[:12]:  # cap: don't hammer the site
        st, page_html, furl = _fetch(session, url, timeout)
        if st is None:
            last_note = "contact page unreachable"
            continue
        if st == 404:
            continue
        if st >= 400:
            last_note = f"contact http {st}"
            continue
        psoup = BeautifulSoup(page_html, "html.parser")
        obs = _obstacles(psoup, page_html, st)
        if "password-gated" in obs or "bot-challenge" in obs:
            picked_url, picked_status = furl, str(st)
            last_note = obs
            break
        forms = _analyze_forms(psoup)
        pmails, ptels = _contact_points(psoup)
        if forms:
            picked_url, picked_status = furl, str(st)
            form_infos = forms
            mails, tels = pmails, ptels
            last_note = ""
            break
        # No form here, but remember the first loadable contact-ish page
        if not picked_url:
            picked_url, picked_status = furl, str(st)
            mails, tels = pmails, ptels
            last_note = "no form found"

    result.contact_url = picked_url
    result.http_status = picked_status or str(status)
    result.has_form = bool(form_infos)
    result.form_count = len(form_infos)
    if form_infos:
        result.form_fields = " | ".join(f.summary() for f in form_infos[:3])
    result.emails_found = "; ".join(mails)
    result.phones_found = "; ".join(tels)
    result.notes = last_note or ("form found" if form_infos else "no contact page found")
    return result

"""Contact-form outreach engine — the submit half of formscout.

Given a list of businesses (name, website, optional postcode) and an
outreach config (your identity + message), this engine:

  1. Opens each site in a real (headless) Chromium via Playwright.
  2. Finds the general "Contact Us" enquiry form.
  3. Fills it with the configured identity and message.
  4. Submits it — at most ONE form per domain — and records the outcome.

Design rules (these mirror a careful human operator, on purpose):

  * CAPTCHAs are NEVER solved. If a reCAPTCHA / hCaptcha / Turnstile
    widget guards the form, the site is flagged ``captcha_review`` for a
    human. A simple arithmetic question printed by the form itself
    ("What is 5 + 6?") is answered — that is the form's own field, not
    a third-party challenge.
  * Newsletter / marketing opt-ins are NEVER ticked. Only mandatory
    privacy-consent boxes are ticked; a mandatory marketing Yes/No is
    answered "No".
  * The phone number is only entered when the field is mandatory.
  * Nothing is ever invented: a mandatory field with no configured
    value (e.g. a postcode missing from the lead row) ends the site
    as ``form_error`` with the reason, rather than a guessed value.
  * Vehicle-specific enquiry forms (registration plate / VIN / stock
    reference and no general message field) are skipped.
  * If a submission is dispatched but no confirmation is visible, the
    outcome is ``uncertain`` and the site is NEVER retried — a second
    submission could duplicate the first.
  * Runs are resumable: point ``--resume`` at a previous results CSV
    and domains with a terminal outcome are skipped.

Requires Playwright + Chromium:
    pip install playwright pyyaml
    python -m playwright install chromium
"""

from __future__ import annotations

import csv
import sys
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Outcomes (kept identical to the labels a human operator would log)
# ---------------------------------------------------------------------------

SENT = "sent"
UNCERTAIN = "uncertain"
CAPTCHA_REVIEW = "captcha_review"
NO_FORM = "no_form"
SITE_DOWN = "site_down"
BLOCKED_WAF = "blocked_waf"
BLOCKED_IP = "blocked_ip"
BLOCKED_BOTWALL = "blocked_botwall"
FORM_ERROR = "form_error"
DRY_RUN = "dry_run"
SKIPPED_DONE = "skipped_done"

TERMINAL = {
    SENT, UNCERTAIN, CAPTCHA_REVIEW, NO_FORM, SITE_DOWN,
    BLOCKED_WAF, BLOCKED_IP, FORM_ERROR,
}

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class OutreachConfig:
    name: str
    email: str
    company: str = ""
    website: str = ""
    phone: str = ""           # used when a phone field is mandatory
    phone_uk: str = ""        # fallback if the form forces a UK number
    subject: str = ""
    message: str = ""         # template; {business_name} is substituted
    delay_seconds: float = 20.0
    timeout_seconds: float = 30.0
    headful: bool = False

    def message_for(self, business_name: str) -> str:
        return self.message.replace("{business_name}", business_name)


def load_config(path: Path) -> OutreachConfig:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover
            raise SystemExit(
                "PyYAML is required for YAML configs: pip install pyyaml "
                "(or use a .json config)"
            ) from exc
        data = yaml.safe_load(text) or {}
    else:
        import json

        data = json.loads(text)
    known = {f.name for f in OutreachConfig.__dataclass_fields__.values()}
    kwargs = {k: v for k, v in data.items() if k in known}
    cfg = OutreachConfig(**kwargs)
    if not cfg.name or not cfg.email or not cfg.message:
        raise SystemExit("config must set at least: name, email, message")
    return cfg


# ---------------------------------------------------------------------------
# Lead rows
# ---------------------------------------------------------------------------


@dataclass
class Lead:
    name: str
    website: str
    postcode: str = ""

    @property
    def domain(self) -> str:
        host = urlparse(self.website if "://" in self.website else f"https://{self.website}").netloc
        return host.lower().removeprefix("www.")


def read_leads(path: Path) -> list[Lead]:
    rows = list(csv.DictReader(path.open(encoding="utf-8", errors="replace")))
    leads: list[Lead] = []
    for r in rows:
        low = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
        name = low.get("name") or low.get("business name") or low.get("business") or ""
        site = low.get("website") or low.get("url") or low.get("domain") or ""
        if not site:
            continue
        leads.append(Lead(name=name or site, website=site, postcode=low.get("postcode", "")))
    return leads


@dataclass
class SubmitResult:
    domain: str
    name: str
    contact_url: str = ""
    outcome: str = ""
    note: str = ""

    def as_row(self) -> dict:
        return {
            "domain": self.domain,
            "name": self.name,
            "contact_url": self.contact_url,
            "outcome": self.outcome,
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# Page-text signals
# ---------------------------------------------------------------------------

SUCCESS_HINT = re.compile(
    r"thank you|thanks for|message (has been )?sent|we('| wi)ll be in touch|"
    r"we have received|received your (message|enquiry|feedback)|"
    r"successfully (submitted|sent)|enquiry (has been )?sent|"
    r"thanks, we received|we'll get back|we will get back",
    re.I,
)
WAF_HINT = re.compile(r"web application firewall|access blocked|blocked by a.*firewall", re.I)
IP_BAN_HINT = re.compile(r"ip (range |address )?banned|your ip.*(blocked|banned)", re.I)
BOTWALL_HINT = re.compile(r"just a moment|checking your browser|verify you are human|attention required", re.I)
PASSWORD_GATE_HINT = re.compile(r"enter using password|opening soon|launching soon|coming soon", re.I)
CAPTCHA_HINT = re.compile(r"recaptcha|hcaptcha|turnstile|g-recaptcha|h-captcha|cf-turnstile", re.I)
URL_REJECT_HINT = re.compile(
    r"(links?|urls?|web addresses?).*(not allowed|are ignored|prohibited|not permitted)|"
    r"may not contain (links|urls)", re.I)
VALIDATION_HINT = re.compile(
    r"field is required|please (enter|fill in|provide)|"
    r"invalid (e-?mail|phone|telephone|number|postcode|zip)|not a valid", re.I)

CONTACT_HINT = re.compile(r"contact|support|get[ -]?in[ -]?touch|reach[ -]?us|enquir|help", re.I)
CONTACT_PATHS = [
    "/contact", "/contact-us", "/contactus", "/get-in-touch", "/enquiry",
    "/enquire", "/pages/contact", "/pages/contact-us", "/support",
]

# Field classification -------------------------------------------------------

FIELD_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("confirm_email", re.compile(r"confirm.*e.?mail|e.?mail.*confirm|repeat.*e.?mail", re.I)),
    ("email", re.compile(r"e.?mail", re.I)),
    ("first_name", re.compile(r"first.?name|forename|given.?name", re.I)),
    ("last_name", re.compile(r"last.?name|surname|family.?name", re.I)),
    ("company", re.compile(r"company|business|organi[sz]ation", re.I)),
    ("full_name", re.compile(r"full.?name|your name|contact.?name|\bname\b", re.I)),
    ("phone", re.compile(r"phone|tel(ephone)?|mobile|contact.?number|cell", re.I)),
    ("website", re.compile(r"website|web ?site|url", re.I)),
    ("subject", re.compile(r"subject|enquiry (type|regarding)|regarding", re.I)),
    ("postcode", re.compile(r"post ?code|zip", re.I)),
    ("message", re.compile(r"message|enquiry|comment|how can we help|details|question", re.I)),
    ("vehicle", re.compile(r"registration|reg ?plate|vin|stock (ref|no|number)|vehicle", re.I)),
]
ARITHMETIC_HINT = re.compile(
    r"(?:what is|calculate|solve)?\s*(\d{1,3})\s*\+\s*(\d{1,3})\s*(?:=|\?)?", re.I
)
MARKETING_HINT = re.compile(r"newsletter|marketing|offers|promotions|mailing list|subscribe", re.I)
CONSENT_HINT = re.compile(r"privacy|consent|gdpr|data protection|terms", re.I)


def classify_field(signals: str, tag: str, input_type: str) -> str:
    """Classify a form field from its name/id/label/placeholder text."""
    # Normalise separators so `reg_plate`, `full-name`, `contactNumber`
    # style attributes read as phrases.
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", signals)
    s = re.sub(r"[_-]+", " ", s).lower()
    if input_type == "email":
        return "confirm_email" if "confirm" in s else "email"
    for kind, pat in FIELD_PATTERNS:
        if pat.search(s):
            # A textarea is the message unless clearly something else.
            if tag == "textarea" and kind not in {"message"}:
                continue
            return kind
    if tag == "textarea":
        return "message"
    return "unknown"


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


def _page_text(page) -> str:
    try:
        return page.inner_text("body", timeout=3000)
    except Exception:
        return ""


def _dismiss_cookies(page) -> None:
    """Dismiss a cookie banner with the least-permissive choice available."""
    for label in ("Reject all", "Reject", "Necessary only", "Essential only",
                  "Decline", "Accept essential"):
        try:
            btn = page.get_by_role("button", name=re.compile(label, re.I))
            if btn.count() and btn.first.is_visible():
                btn.first.click(timeout=1500)
                return
        except Exception:
            continue


def _find_contact_url(page, base_url: str) -> str | None:
    """Return the best contact-page URL for the site, or None."""
    try:
        page.goto(base_url, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        return None
    body = _page_text(page)
    if PASSWORD_GATE_HINT.search(body or ""):
        return None
    # Prefer a contact link on the homepage.
    try:
        links = page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => ({href: e.href, text: (e.innerText || '').trim()}))",
        )
    except Exception:
        links = []
    candidates: list[str] = []
    for lnk in links or []:
        href = lnk.get("href", "")
        if not href or href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        if urlparse(href).netloc.lower().removeprefix("www.") != urlparse(base_url).netloc.lower().removeprefix("www."):
            continue
        if CONTACT_HINT.search(lnk.get("text", "")) or CONTACT_HINT.search(href):
            if href not in candidates:
                candidates.append(href)
    origin = f"{urlparse(base_url).scheme}://{urlparse(base_url).netloc}"
    for pth in CONTACT_PATHS:
        u = origin + pth
        if u not in candidates:
            candidates.append(u)
    candidates.append(base_url)  # some sites put the form on the homepage
    for url in candidates:
        try:
            if page.url != url:
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
            if page.query_selector("form"):
                return url
        except Exception:
            continue
    return None


def _pick_form(page):
    """Pick the best general enquiry form on the page, or None.

    Skips search/newsletter/login forms and vehicle-specific forms.
    """
    forms = page.query_selector_all("form")
    best, best_score = None, 0
    for form in forms:
        try:
            fields = form.query_selector_all("input, textarea, select")
        except Exception:
            continue
        kinds, text_blob = [], form.inner_text().lower() if form else ""
        has_password = False
        for f in fields:
            tag = (f.evaluate("e => e.tagName") or "").lower()
            itype = (f.get_attribute("type") or "text").lower()
            if itype == "password":
                has_password = True
            sig = " ".join(filter(None, [
                f.get_attribute("name") or "", f.get_attribute("id") or "",
                f.get_attribute("placeholder") or "", f.get_attribute("aria-label") or "",
            ]))
            kinds.append(classify_field(sig, tag, itype))
        if has_password:
            continue  # login form
        if "subscribe" in text_blob and "message" not in kinds:
            continue  # newsletter-only
        if "vehicle" in kinds and "message" not in kinds:
            continue  # vehicle-specific enquiry
        score = 0
        if "message" in kinds:
            score += 3
        if "email" in kinds:
            score += 2
        if "full_name" in kinds or "first_name" in kinds:
            score += 1
        if score > best_score:
            best, best_score = form, score
    return best if best_score >= 3 else None


def _field_signals(page, form, el) -> str:
    """All the text that identifies a field: attrs + label + wrapping text."""
    parts = [
        el.get_attribute("name") or "", el.get_attribute("id") or "",
        el.get_attribute("placeholder") or "", el.get_attribute("aria-label") or "",
    ]
    try:
        label = el.evaluate(
            """e => {
                if (e.id) { const l = document.querySelector(`label[for="${e.id}"]`);
                            if (l) return l.innerText; }
                const wrap = e.closest('label');
                if (wrap) return wrap.innerText;
                const p = e.closest('div, p, li, td');
                return p ? (p.innerText || '').slice(0, 120) : '';
            }"""
        )
        if label:
            parts.append(label)
    except Exception:
        pass
    return " ".join(parts)


def _is_required(el) -> bool:
    try:
        return bool(el.evaluate(
            "e => e.required || e.getAttribute('aria-required') === 'true' || "
            "(e.closest('label, div, p') || {}).innerText?.includes('*')"
        ))
    except Exception:
        return el.get_attribute("required") is not None


def _fill_form(page, form, lead: Lead, cfg: OutreachConfig) -> tuple[bool, str]:
    """Fill the form. Returns (ok, note). ok=False means a mandatory
    field could not be filled honestly — do not submit."""
    first, _, last = cfg.name.partition(" ")
    values = {
        "full_name": cfg.name,
        "first_name": first,
        "last_name": last or first,
        "email": cfg.email,
        "confirm_email": cfg.email,
        "company": cfg.company,
        "website": cfg.website,
        "subject": cfg.subject,
        "postcode": lead.postcode,
        "message": cfg.message_for(lead.name),
    }
    fields = form.query_selector_all("input, textarea, select")
    for el in fields:
        try:
            tag = (el.evaluate("e => e.tagName") or "").lower()
            itype = (el.get_attribute("type") or "text").lower()
            if itype in {"hidden", "submit", "button", "file", "image"}:
                continue
            signals = _field_signals(page, form, el)
            required = _is_required(el)

            if itype == "checkbox":
                if MARKETING_HINT.search(signals):
                    continue  # never opt in
                if required and CONSENT_HINT.search(signals):
                    el.check()
                continue
            if itype == "radio":
                # Mandatory marketing Yes/No style choice → answer "No".
                if MARKETING_HINT.search(signals) and re.search(r"\bno\b", signals, re.I):
                    el.check()
                continue

            kind = classify_field(signals, tag, itype)
            # The form's own arithmetic anti-spam question ("What is 5 + 6?").
            m = ARITHMETIC_HINT.search(signals)
            if m and kind in {"unknown", "message"} and tag != "textarea":
                el.fill(str(int(m.group(1)) + int(m.group(2))))
                continue
            if kind in {"unknown", "vehicle"}:
                if required:
                    return False, f"mandatory field with no configured value: {signals[:60]!r}"
                continue
            value = values.get(kind, "")
            if kind == "phone":
                if not required:
                    continue  # phone only when mandatory
                value = cfg.phone
                if not value:
                    return False, "mandatory phone field, no phone in config"
            if not value:
                if required:
                    return False, f"mandatory {kind} field, no value available"
                continue
            if tag == "select":
                # Dropdown (usually enquiry type): prefer a General/Other
                # option, else the first real option. Never leave a
                # mandatory select on a placeholder if a choice exists.
                try:
                    labels = el.eval_on_selector_all(
                        "option", "os => os.map(o => (o.innerText || '').trim())")
                    pick = next((i for i, t in enumerate(labels)
                                 if re.search(r"general|other|enquir", t, re.I)), None)
                    if pick is None:
                        pick = next((i for i, t in enumerate(labels) if t), 0)
                    el.select_option(index=pick)
                except Exception:
                    pass
            else:
                el.fill(value)
        except Exception:
            continue
    return True, ""


def _submit_form(page, form) -> bool:
    for sel in ("button[type=submit]", "input[type=submit]",
                "button:has-text('Send')", "button:has-text('Submit')",
                "button:has-text('Enquire')", "button:has-text('Enquiry')"):
        try:
            btn = form.query_selector(sel)
            if btn and btn.is_visible():
                btn.click(timeout=5000)
                return True
        except Exception:
            continue
    try:
        form.evaluate("f => f.requestSubmit ? f.requestSubmit() : f.submit()")
        return True
    except Exception:
        return False


def _evaluate(page) -> tuple[str, str]:
    """Classify the page state after a submission was dispatched."""
    after = _page_text(page)
    if WAF_HINT.search(after or ""):
        return BLOCKED_WAF, "submission blocked by WAF"
    if IP_BAN_HINT.search(after or ""):
        return BLOCKED_IP, "IP banned from posting"
    if SUCCESS_HINT.search(after or "") or "thank" in page.url.lower():
        return SENT, "confirmation shown"
    if URL_REJECT_HINT.search(after or ""):
        return FORM_ERROR, "message rejected (link/URL filter)"
    if page.query_selector("form") and VALIDATION_HINT.search(after or ""):
        return FORM_ERROR, "validation error shown after submit"
    return UNCERTAIN, "submitted, no confirmation shown; not retried"


def _refill_phone(page, form, number: str) -> bool:
    """Replace the phone field's value (validation-retry path only)."""
    for el in form.query_selector_all("input"):
        try:
            tag = (el.evaluate("e => e.tagName") or "").lower()
            itype = (el.get_attribute("type") or "text").lower()
            signals = _field_signals(page, form, el)
            if classify_field(signals, tag, itype) == "phone":
                el.fill(number)
                return True
        except Exception:
            continue
    return False


def submit_one(page, lead: Lead, cfg: OutreachConfig, dry_run: bool) -> SubmitResult:
    res = SubmitResult(domain=lead.domain, name=lead.name)
    base = lead.website if "://" in lead.website else f"https://{lead.website}"
    contact_url = _find_contact_url(page, base)
    if contact_url is None:
        # Distinguish a dead site from a live site with no form.
        try:
            resp = page.goto(base, wait_until="domcontentloaded", timeout=20000)
            if resp is None or resp.status >= 400:
                res.outcome, res.note = SITE_DOWN, f"homepage status {getattr(resp, 'status', '?')}"
                return res
        except Exception as exc:
            res.outcome, res.note = SITE_DOWN, f"unreachable: {str(exc)[:80]}"
            return res
        res.outcome, res.note = NO_FORM, "no contact form found"
        return res

    res.contact_url = contact_url
    body = _page_text(page)
    if BOTWALL_HINT.search(body or ""):
        res.outcome, res.note = BLOCKED_BOTWALL, "bot challenge wall (Cloudflare etc.)"
        return res
    html = page.content()
    if CAPTCHA_HINT.search(html):
        res.outcome, res.note = CAPTCHA_REVIEW, "CAPTCHA widget guards the form — manual review"
        return res
    form = _pick_form(page)
    if form is None:
        res.outcome, res.note = NO_FORM, "contact page has no usable general form"
        return res

    _dismiss_cookies(page)
    ok, why = _fill_form(page, form, lead, cfg)
    if not ok:
        res.outcome, res.note = FORM_ERROR, why
        return res
    if dry_run:
        res.outcome, res.note = DRY_RUN, "form filled, not submitted (dry run)"
        return res
    if not _submit_form(page, form):
        res.outcome, res.note = FORM_ERROR, "no working submit control"
        return res
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    page.wait_for_timeout(1500)
    outcome, note = _evaluate(page)
    # UK phone fallback: a validation error proves nothing was sent, so
    # ONE retry with the UK-format number is safe (not a duplicate).
    if (outcome == FORM_ERROR and note.startswith("validation")
            and cfg.phone_uk and cfg.phone_uk != cfg.phone
            and page.query_selector("form")):
        form2 = _pick_form(page)
        if form2 and _refill_phone(page, form2, cfg.phone_uk) and _submit_form(page, form2):
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            page.wait_for_timeout(1500)
            outcome, note = _evaluate(page)
            note += " (after UK phone fallback)"
    res.outcome, res.note = outcome, note
    return res


def run_batch(leads: list[Lead], cfg: OutreachConfig, dry_run: bool = False,
              limit: int | None = None, done_domains: set[str] | None = None,
              progress=None) -> list[SubmitResult]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "Playwright is required for submit mode:\n"
            "  pip install playwright pyyaml\n"
            "  python -m playwright install chromium"
        ) from exc
    done_domains = done_domains or set()
    results: list[SubmitResult] = []
    todo = [ld for ld in leads if ld.domain not in done_domains]
    skipped = [ld for ld in leads if ld.domain in done_domains]
    for ld in skipped:
        results.append(SubmitResult(domain=ld.domain, name=ld.name,
                                    outcome=SKIPPED_DONE, note="terminal outcome in resume file"))
    if limit:
        todo = todo[:limit]
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not cfg.headful)
        for i, lead in enumerate(todo, 1):
            page = browser.new_page()
            try:
                r = submit_one(page, lead, cfg, dry_run)
            except Exception as exc:  # one bad site must never kill the run
                r = SubmitResult(domain=lead.domain, name=lead.name,
                                 outcome=FORM_ERROR, note=f"engine error: {str(exc)[:100]}")
            finally:
                page.close()
            results.append(r)
            if progress:
                progress(i, len(todo), r)
            if i < len(todo) and cfg.delay_seconds:
                time.sleep(cfg.delay_seconds)
        browser.close()
    return results


# ---------------------------------------------------------------------------
# CLI:  python -m formscout submit leads.csv --config outreach.yaml
# ---------------------------------------------------------------------------


def _resume_domains(path: str | None) -> set[str]:
    if not path or not Path(path).exists():
        return set()
    done: set[str] = set()
    for row in csv.DictReader(Path(path).open(encoding="utf-8", errors="replace")):
        if (row.get("outcome") or "") in TERMINAL:
            done.add((row.get("domain") or "").strip().lower())
    return done


def run_cli(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="formscout submit",
        description="Submit outreach messages through businesses' own contact forms.",
    )
    ap.add_argument("input", help="Leads CSV (columns: name, website, postcode)")
    ap.add_argument("--config", required=True, help="Outreach config (.yaml/.yml or .json)")
    ap.add_argument("-o", "--output", default="formscout-outreach.csv",
                    help="Results CSV (default: formscout-outreach.csv)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Fill forms but never submit — verify the fill first")
    ap.add_argument("--limit", type=int, default=None, help="Max sites this run")
    ap.add_argument("--resume", default=None,
                    help="Previous results CSV; domains with terminal outcomes are skipped")
    ap.add_argument("--delay", type=float, default=None,
                    help="Seconds between sites (overrides config)")
    ap.add_argument("--headful", action="store_true", help="Show the browser (debugging)")
    args = ap.parse_args(argv)

    in_path = Path(args.input)
    if not in_path.exists():
        print(f"error: input file not found: {in_path}", file=sys.stderr)
        return 2
    cfg = load_config(Path(args.config))
    if args.delay is not None:
        cfg.delay_seconds = args.delay
    if args.headful:
        cfg.headful = True

    leads = read_leads(in_path)
    # One form per domain: dedupe by domain, preserve order.
    seen, unique = set(), []
    for ld in leads:
        if ld.domain and ld.domain not in seen:
            seen.add(ld.domain)
            unique.append(ld)
    if not unique:
        print("error: no leads with a website found", file=sys.stderr)
        return 2

    done = _resume_domains(args.resume)
    mode = "DRY RUN (nothing will be submitted)" if args.dry_run else "LIVE"
    print(f"formscout submit: {len(unique)} unique domains, {mode}, "
          f"{len(done)} already done", file=sys.stderr)

    def progress(i: int, total: int, r: SubmitResult) -> None:
        print(f"  [{i}/{total}] {r.domain}: {r.outcome} — {r.note}",
              file=sys.stderr)

    results = run_batch(unique, cfg, dry_run=args.dry_run, limit=args.limit,
                        done_domains=done, progress=progress)
    out_path = Path(args.output)
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["domain", "name", "contact_url", "outcome", "note"])
        writer.writeheader()
        for r in results:
            writer.writerow(r.as_row())
    sent = sum(1 for r in results if r.outcome == SENT)
    print(f"done: {len(results)} rows, {sent} sent → {out_path}", file=sys.stderr)
    return 0

# formscout

Find contact pages and forms across hundreds of domains — built for cold outreach at scale.

Give it a list of domains. Get back a CSV: the contact page URL, whether it has a form, what the form asks for, any emails/phone numbers on the page, and obstacles (`password-gated`, `unreachable`, `bot-challenge`). The boring discovery half of outreach, automated — so the human half (the actual message) stays human.

## Install

```bash
git clone https://github.com/YOUR-USERNAME/formscout.git
cd formscout
pip install -r requirements.txt
```

Requires Python 3.9+.

## Usage

```bash
# domains.txt — one domain per line
python -m formscout domains.txt -o results.csv

# CSV input works too (uses a 'domain' column, or the first column)
python -m formscout leads.csv -o results.csv --workers 10 --timeout 20
```

Example output (`results.csv`):

| domain | contact_url | has_form | form_fields | emails_found | notes |
|---|---|---|---|---|---|
| example-store.com | https://example-store.com/pages/contact | yes | name,email,message | hello@example-store.com | form found |
| gated-shop.com | https://gated-shop.com/ | no | | | password-gated (homepage) |
| dead-site.co | | no | | | unreachable |

### How it finds contact pages

1. Fetches the homepage (https, falling back to http).
2. Follows homepage links whose text or URL looks like contact/support ("Contact Us", "Support", "Get in Touch"…).
3. Falls back to common paths: `/contact`, `/contact-us`, `/support`, `/pages/contact` (Shopify), and friends.
4. Inspects each candidate for `<form>` tags, `mailto:`/`tel:` links, and obstacles.

### Options

| Flag | Default | What it does |
|---|---|---|
| `-o, --output` | `formscout-results.csv` | Output CSV path |
| `--workers` | `8` | Parallel workers — keep it low, be polite |
| `--timeout` | `15` | Seconds per HTTP request |

## Limitations

- **Plain HTTP only, no browser.** JavaScript-rendered forms may be missed — those rows are still useful (you get the contact URL) but need a manual pass.
- Respects no crawl budget beyond a 12-URL cap per domain; keep `--workers` modest.
- Detection is heuristic. A "no form found" means *no form detected*, not *no form exists*.

## Be a good citizen

This tool fetches public pages the way a visitor would, a handful at a time. Don't point it at someone's site with 200 workers, don't use it to harvest emails for spam, and check a site's `robots.txt`/terms if you're unsure. Outreach is a conversation — this just finds the door.

## License

MIT — see [LICENSE](LICENSE).

## Submit mode — send the outreach too

`formscout submit` drives a real (headless) Chromium via Playwright: it
finds each site's general contact form, fills it with your identity and
message from a config file, and submits it — at most one form per
domain — logging an outcome per site.

```bash
pip install -r requirements-browser.txt
python -m playwright install chromium

cp examples/outreach.example.yaml outreach.yaml   # fill in your details
python -m formscout submit leads.csv --config outreach.yaml --dry-run   # fill only, verify
python -m formscout submit leads.csv --config outreach.yaml -o results.csv
python -m formscout submit leads.csv --config outreach.yaml --resume results.csv  # continue later
```

Leads CSV columns: `name, website, postcode` (postcode optional — used
only when a form requires one; it is never invented).

Outcomes per site: `sent` (confirmation shown), `uncertain` (submitted,
no confirmation — never retried), `captcha_review` (a CAPTCHA guards
the form — flagged for a human), `no_form`, `site_down`, `blocked_waf`,
`blocked_ip`, `blocked_botwall`, `form_error` (with the reason, e.g. a
mandatory field with no honest value).

### House rules, built in

- **CAPTCHAs are never solved.** Sites behind one are flagged for
  manual review. (A form's own arithmetic question — "What is 5 + 6?" —
  is answered; that's a form field, not a third-party challenge.)
- Newsletter/marketing opt-ins are never ticked; only mandatory
  privacy-consent boxes are. A mandatory marketing Yes/No is "No".
- Phone is entered only when the field is mandatory.
- One form per domain, polite delay between sites, resumable runs.
- `--dry-run` fills everything and submits nothing, so you can verify
  accuracy before a live run.

Etiquette: only message businesses through forms they publish for
contact, send something genuinely relevant, and honour opt-outs.

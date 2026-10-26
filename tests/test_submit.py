"""End-to-end tests for the formscout submit engine.

Spins up a local fixture server with six fake business sites and runs
the real Playwright engine against them:

  /simple/    plain form, optional phone, privacy consent -> sent
  /required/  mandatory phone + postcode                    -> sent
  /captcha/   form guarded by a reCAPTCHA widget            -> captcha_review
  /noform/    contact page with phone/mailto only           -> no_form
  /uncertain/ form posts with no confirmation text          -> uncertain
  /vehicle/   vehicle-registration enquiry form only        -> no_form

Run:  python -m pytest tests/ -q      (needs playwright + chromium)
"""

from __future__ import annotations

import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from formscout.submit import (
    CAPTCHA_REVIEW,
    DRY_RUN,
    NO_FORM,
    SENT,
    UNCERTAIN,
    Lead,
    OutreachConfig,
    submit_one,
)

POSTS: dict[str, dict] = {}

PAGES = {
    "/simple/": """
      <html><body><h1>Simple Motors</h1>
      <a href="/simple/contact">Contact Us</a></body></html>""",
    "/simple/contact": """
      <html><body><h1>Contact Simple Motors</h1>
      <form method="post" action="/simple/send">
        <label>Full Name <input name="full_name" required></label>
        <label>Email <input type="email" name="email" required></label>
        <label>Phone <input name="phone"></label>
        <label>Subject <input name="subject"></label>
        <label>Message <textarea name="message" required></textarea></label>
        <label><input type="checkbox" name="privacy" required>
          I agree to the privacy policy *</label>
        <label><input type="checkbox" name="newsletter">
          Sign me up for marketing offers</label>
        <button type="submit">Send</button>
      </form></body></html>""",
    "/simple/thanks": "<html><body><h1>Thank you for your enquiry. "
                      "We'll be in touch soon.</h1></body></html>",
    "/required/": """
      <html><body><a href="/required/contact">Get in Touch</a></body></html>""",
    "/required/contact": """
      <html><body><form method="post" action="/required/send">
        <label>Your Name <input name="name" required></label>
        <label>Email Address <input type="email" name="email" required></label>
        <label>Telephone * <input name="telephone" required></label>
        <label>Postcode * <input name="postcode" required></label>
        <label>Enquiry <textarea name="enquiry" required></textarea></label>
        <button type="submit">Submit</button>
      </form></body></html>""",
    "/required/thanks": "<html><body><p>Thanks for getting in touch.</p></body></html>",
    "/captcha/": """
      <html><body><a href="/captcha/contact">Contact</a></body></html>""",
    "/captcha/contact": """
      <html><body><form method="post" action="/captcha/send">
        <label>Name <input name="name" required></label>
        <label>Email <input type="email" name="email" required></label>
        <label>Message <textarea name="message" required></textarea></label>
        <div class="g-recaptcha" data-sitekey="test"></div>
        <iframe src="https://www.google.com/recaptcha/api2/anchor"></iframe>
        <button type="submit">Send</button>
      </form></body></html>""",
    "/noform/": """
      <html><body><a href="/noform/contact">Contact Us</a></body></html>""",
    "/noform/contact": """
      <html><body><h1>Call us on 01234 567890</h1>
      <a href="mailto:hello@example.com">Send us an email</a></body></html>""",
    "/uncertain/": """
      <html><body><a href="/uncertain/contact">Contact</a></body></html>""",
    "/uncertain/contact": """
      <html><body><form method="post" action="/uncertain/send">
        <label>Name <input name="name" required></label>
        <label>Email <input type="email" name="email" required></label>
        <label>Message <textarea name="message" required></textarea></label>
        <button type="submit">Send Message</button>
      </form></body></html>""",
    "/uncertain/done": "<html><body><p>Reference noted.</p></body></html>",
    "/vehicle/": """
      <html><body><a href="/vehicle/contact">Contact</a></body></html>""",
    "/vehicle/contact": """
      <html><body><form method="post" action="/vehicle/send">
        <label>Vehicle Registration <input name="reg_plate" required></label>
        <label>Name <input name="name" required></label>
        <label>Email <input type="email" name="email" required></label>
        <label>Phone <input name="phone" required></label>
        <button type="submit">Value my car</button>
      </form></body></html>""",
}

REDIRECTS = {
    "/simple/send": "/simple/thanks",
    "/required/send": "/required/thanks",
    "/uncertain/send": "/uncertain/done",
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence
        pass

    def do_GET(self):
        html = PAGES.get(self.path)
        if html is None:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(html.encode())

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode()
        from urllib.parse import parse_qs

        POSTS[self.path] = {k: v[0] for k, v in parse_qs(body).items()}
        target = REDIRECTS.get(self.path, "/")
        self.send_response(303)
        self.send_header("Location", target)
        self.end_headers()


CFG = OutreachConfig(
    name="Zaigham Kazmi",
    email="Support@checkmymot.com",
    company="CheckMyMOT",
    website="https://checkmymot.com",
    phone="+923236721305",
    subject="Free listings offer",
    message="Hi {business_name} team, this is a test outreach message.",
    delay_seconds=0,
)


class SubmitEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        from playwright.sync_api import sync_playwright

        cls._pw = sync_playwright().start()
        cls.browser = cls._pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls._pw.stop()
        cls.server.shutdown()

    def run_one(self, path: str, name: str, postcode: str = "", dry: bool = False):
        page = self.browser.new_page()
        try:
            lead = Lead(name=name, website=f"http://127.0.0.1:{self.port}{path}",
                        postcode=postcode)
            return submit_one(page, lead, CFG, dry)
        finally:
            page.close()

    def test_simple_form_sent_with_exact_values(self):
        r = self.run_one("/simple/", "Simple Motors")
        self.assertEqual(r.outcome, SENT, r.note)
        sent = POSTS["/simple/send"]
        self.assertEqual(sent["full_name"], "Zaigham Kazmi")
        self.assertEqual(sent["email"], "Support@checkmymot.com")
        self.assertEqual(sent["message"],
                         "Hi Simple Motors team, this is a test outreach message.")
        self.assertEqual(sent.get("privacy"), "on")          # consent ticked
        self.assertNotIn("newsletter", sent)                 # marketing NOT ticked
        self.assertNotIn("phone", sent)                      # optional phone left blank

    def test_required_phone_and_postcode_filled(self):
        r = self.run_one("/required/", "Required Cars", postcode="SO23 7RQ")
        self.assertEqual(r.outcome, SENT, r.note)
        sent = POSTS["/required/send"]
        self.assertEqual(sent["telephone"], "+923236721305")
        self.assertEqual(sent["postcode"], "SO23 7RQ")

    def test_captcha_flagged_never_submitted(self):
        r = self.run_one("/captcha/", "Captcha Cars")
        self.assertEqual(r.outcome, CAPTCHA_REVIEW, r.note)
        self.assertNotIn("/captcha/send", POSTS)

    def test_no_form(self):
        r = self.run_one("/noform/", "NoForm Cars")
        self.assertEqual(r.outcome, NO_FORM, r.note)

    def test_uncertain_when_no_confirmation(self):
        r = self.run_one("/uncertain/", "Uncertain Cars")
        self.assertEqual(r.outcome, UNCERTAIN, r.note)
        self.assertIn("/uncertain/send", POSTS)  # it did submit, once

    def test_vehicle_form_skipped(self):
        r = self.run_one("/vehicle/", "Vehicle Cars")
        self.assertEqual(r.outcome, NO_FORM, r.note)
        self.assertNotIn("/vehicle/send", POSTS)

    def test_dry_run_fills_but_does_not_submit(self):
        POSTS.pop("/simple/send", None)
        r = self.run_one("/simple/", "Simple Motors", dry=True)
        self.assertEqual(r.outcome, DRY_RUN, r.note)
        self.assertNotIn("/simple/send", POSTS)


if __name__ == "__main__":
    unittest.main()

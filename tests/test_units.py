"""Browser-free unit tests: field classification, config, leads, resume."""
import unittest
from pathlib import Path

from formscout.submit import (
    Lead, classify_field, load_config, read_leads, _resume_domains,
)

class TestClassify(unittest.TestCase):
    def test_common_fields(self):
        self.assertEqual(classify_field("email address", "input", "text"), "email")
        self.assertEqual(classify_field("your email", "input", "email"), "email")
        self.assertEqual(classify_field("confirm email", "input", "email"), "confirm_email")
        self.assertEqual(classify_field("full name", "input", "text"), "full_name")
        self.assertEqual(classify_field("first name", "input", "text"), "first_name")
        self.assertEqual(classify_field("telephone number", "input", "tel"), "phone")
        self.assertEqual(classify_field("company name", "input", "text"), "company")
        self.assertEqual(classify_field("postcode", "input", "text"), "postcode")
        self.assertEqual(classify_field("subject", "input", "text"), "subject")
        self.assertEqual(classify_field("your message", "textarea", "text"), "message")
        self.assertEqual(classify_field("anything", "textarea", "text"), "message")
        self.assertEqual(classify_field("vehicle registration", "input", "text"), "vehicle")
        self.assertEqual(classify_field("favourite colour", "input", "text"), "unknown")

    def test_domain_normalisation(self):
        self.assertEqual(Lead("X", "https://www.example.co.uk/contact").domain, "example.co.uk")
        self.assertEqual(Lead("X", "example.co.uk").domain, "example.co.uk")

class TestConfigAndLeads(unittest.TestCase):
    def test_example_config_loads(self):
        cfg = load_config(Path("examples/outreach.example.yaml"))
        self.assertTrue(cfg.name and cfg.email and cfg.message)
        self.assertEqual(cfg.message_for("Acme Cars").count("Acme Cars"), 2)

    def test_example_leads_load(self):
        leads = read_leads(Path("examples/leads.example.csv"))
        self.assertEqual(len(leads), 2)
        self.assertEqual(leads[0].postcode, "SO23 7RQ")

class TestResume(unittest.TestCase):
    def test_terminal_outcomes_collected(self):
        import csv, tempfile, os
        fd, path = tempfile.mkstemp(suffix=".csv")
        with os.fdopen(fd, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["domain", "name", "contact_url", "outcome", "note"])
            w.writerow(["a.co.uk", "A", "", "sent", ""])
            w.writerow(["b.co.uk", "B", "", "dry_run", ""])
            w.writerow(["c.co.uk", "C", "", "site_down", ""])
        self.assertEqual(_resume_domains(path), {"a.co.uk", "c.co.uk"})

if __name__ == "__main__":
    unittest.main()

class TestEnvProxy(unittest.TestCase):
    def test_parses_credentialed_proxy(self):
        import os
        from unittest.mock import patch
        from formscout.submit import _env_proxy
        env = {"https_proxy": "http://user:p%40ss@proxy.local:3128",
               "no_proxy": "localhost,127.0.0.1"}
        with patch.dict(os.environ, env, clear=True):
            p = _env_proxy()
        self.assertEqual(p["server"], "http://proxy.local:3128")
        self.assertEqual(p["username"], "user")
        self.assertEqual(p["password"], "p@ss")
        self.assertEqual(p["bypass"], "localhost,127.0.0.1")

    def test_no_proxy_env(self):
        import os
        from unittest.mock import patch
        from formscout.submit import _env_proxy
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(_env_proxy())

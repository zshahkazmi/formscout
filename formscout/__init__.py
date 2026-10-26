"""formscout — find contact pages and forms across hundreds of domains,
and (optionally) submit your outreach through them.

Built for cold outreach at scale: give it a list of domains, get back
the contact page URL, form fields, and any obstacles (password gates,
dead sites) for each one. `formscout submit` goes one step further and
sends your message through each site's own contact form.
"""

__version__ = "0.2.0"
__all__ = ["discover", "submit"]

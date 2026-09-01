---
name: web-launch
description: Launch checklist for public-facing web pages - SEO, conversion, trust, and error-page hygiene. Run in Phase 5 for anything indexable or marketing-facing. N/A for internal-only UI.
---

# Public Web Page Launch Checklist

Report each item PASS / FAIL / N-A (one-line justification per N-A). A FAIL on an indexable page blocks ship.

## SEO & discoverability
- [ ] Unique page title and meta description on every page — written for the searcher, not stuffed.
- [ ] `robots.txt` correct for the environment (staging blocked, production open); sitemap generated.
- [ ] One H1 per page, headings in order; internal links between related pages; breadcrumbs on hierarchies deeper than two levels.
- [ ] Alt text on every meaningful image (empty alt on decorative ones); social share image (OG + Twitter card) set and tested.
- [ ] Local business? Map + directions on the contact page, local schema (JSON-LD), and real reviews — never fabricated ones.

## Conversion & trust
- [ ] Primary CTA above the fold; sticky CTA on mobile.
- [ ] Every form leads to a thank-you page/state: confirms what happened, sets the next step, enables conversion tracking.
- [ ] FAQ section (at least 5 real questions) on landing/product pages; case studies or social proof wherever claims are made.
- [ ] Response-time promise stated at contact points ("we reply within one business day") — and operationally honored.

## Hygiene
- [ ] Custom 404 page: matches the design system, offers search/links home — same for 500.
- [ ] Privacy policy page exists and is linked in the footer; consent handling per `privacy-compliance`.
- [ ] HTTPS enforced; page meets `performance-engineer`'s budget on a mid-range phone over 4G.
- [ ] `craft-editor` has passed over all visible copy.

## Verdict
End with one line: **LAUNCH** or **BLOCKED — <failing items>**.

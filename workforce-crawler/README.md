# RunGrow public business research adapter

Apache-2.0 dependency: Crawlee. This package collects business-level evidence from explicitly allowlisted public websites, respects robots.txt, limits concurrency and records observation time. It does not bypass authentication, paywalls, CAPTCHAs or access controls.

The returned contactPermission field is always false. Publicly displayed phone numbers or email addresses are not consent to receive marketing calls or messages. The outreach policy service must independently verify channel permission, suppression status, applicable DND/UCC rules and provider policy before any contact.

Install with npm install and build with npm run build. Tenant-specific allowlists and crawl quotas must be loaded from the authenticated workforce policy service; never accept arbitrary URLs from an unauthenticated browser.

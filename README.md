# RunGrow website

Vibe coding standard: [docs/vibe-coding-no-nos.md](docs/vibe-coding-no-nos.md). Follow this document before every design or code change.

Static, component-oriented website. The Photo Studio vertical is a hub with two independent product destinations.

## Architecture
- `index.html` and `solutions/*/index.html`: static page composition and page-specific SEO.
- `components/header/header.html`: shared navigation and Photo Studio mega-menu.
- `styles/components/header.css`: shared header styles.
- `styles/components/photo-studio-menu.css`: isolated mega-menu and vertical styles.
- `styles/pages/photo-studio.css`: Photo Studio hub and product page styles.
- `scripts/photo-studio-menu.js`: accessible mega-menu interactions, separate from global navigation.
- `data/photo-studio/*.json`: structured vertical and product metadata.
- `scripts/`: component mounting, theme, navigation and FAQ modules.

## Photo Studio routes
- `/solutions/photo-studio/` — vertical hub.
- `/solutions/photo-studio/growth-os/` — Growth OS.
- `/solutions/photo-studio/billing-software/` — Billing Software planned/early-access page.
- `/solutions/photo-studios/` — permanently redirected to the singular hub route.

## Content integrity
All 10x+ values are synthetic illustration data, not measured performance or customer outcomes. They carry visible demo-data disclosures. WhatsApp AI booking, crew/equipment calendars, proofing galleries, payment gateway collection and shared data synchronization are not promised unless separately implemented and verified. Billing Software is labelled planned/early access; tax and payment behavior requires validation before release.

## Local preview
Serve the repository root with a static HTTP server. Direct `file://` opening is unsupported because shared components are fetched at runtime.

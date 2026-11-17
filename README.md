# RunGrow website

A static, component-oriented website.

## Project structure
- `index.html` and `solutions/*/index.html`: page composition, page-specific content and SEO metadata.
- `components/header` and `components/footer`: shared HTML components maintained once and mounted by the component loader.
- `styles/tokens.css`: theme and design tokens.
- `styles/base.css`: document defaults, reset and accessibility.
- `styles/layout.css`: shared grids, sections, hero, buttons and layout primitives.
- `styles/components/`: isolated header, footer, cards and FAQ styles.
- `styles/pages/`: page-family styles.
- `styles/responsive.css`: breakpoints, reduced-motion and print rules.
- `scripts/`: ES modules; component mounting, theme, navigation and FAQ behaviour are separate modules.
- `assets/`: static assets and web manifest.

## Component workflow
1. Edit reusable markup in its component folder.
2. Edit that component's CSS in `styles/components/`.
3. Keep interaction logic in its own module under `scripts/`.
4. Keep page files focused on composing page-specific sections.

Shared components are fetched from same-origin static files before interactive modules initialize. Run the site through its static host or a local HTTP server; direct `file://` opening is not supported.

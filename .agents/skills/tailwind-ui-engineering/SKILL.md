---
name: tailwind-ui-engineering
description: Build and refactor production UI in RunGrow repositories that already use Tailwind CSS. Detect the repository's installed Tailwind major version first, reuse its existing components and design tokens, and keep UI modules modular.
---

# Tailwind UI Engineering

## Before editing

1. Inspect the target repository's `package.json`, lockfile, PostCSS configuration, Tailwind configuration and existing component conventions.
2. Detect the installed Tailwind major version. Never mix v3 and v4 configuration or directives.
3. Search the repository for an existing component, token, layout or interaction before creating another one.
4. Preserve the repository's framework, router, package manager and build conventions. Do not migrate frameworks as part of a component task.

## Version-specific rules

### Tailwind CSS v4

- Use the repository's installed `tailwindcss` and `@tailwindcss/postcss` versions.
- Keep the existing PostCSS plugin configuration.
- Use `@import "tailwindcss";` in the existing global stylesheet.
- Prefer CSS-first theme tokens and existing CSS variables.
- Do not introduce v3-only `@tailwind base/components/utilities` directives or a legacy config unless the repository explicitly requires compatibility.

### Tailwind CSS v3

- Preserve the existing `tailwind.config.*` content globs and theme extension.
- Preserve the existing PostCSS and Autoprefixer setup.
- Use the v3 layer directives already present in the project.
- Do not add v4-only CSS-first configuration or `@tailwindcss/postcss` unless performing an explicit migration.

## RunGrow UI engineering rules

- Inspect and reuse existing shadcn/ui, Radix, local UI and design-system components before introducing dependencies.
- Keep page composition, reusable components, component-specific styles and business logic in separate files.
- Use semantic tokens for surfaces, borders, text, status and focus states. Avoid hard-coded one-off colors when a token exists.
- Build compact, information-dense enterprise interfaces with clear hierarchy, crisp borders, responsive tables and useful empty/loading/error states.
- Do not use generic AI gradients, neon glows or default purple/indigo SaaS palettes unless explicitly required by the existing brand system.
- Ensure keyboard access, visible focus, accessible labels, semantic HTML, reduced-motion support and sufficient contrast.
- Avoid arbitrary dynamic class construction. Use complete statically discoverable class names or explicit variant maps.
- Do not put secrets, authorization decisions or tenant checks in client-side components.
- Keep forms and mutations connected to real typed API contracts; never display mock values as live metrics.

## Repository-specific boundaries

- `studio-growth-os` currently declares Tailwind CSS v3.4. Preserve its v3 setup unless a separate migration is approved.
- `creativ-i-studios` currently declares Tailwind CSS v4.1 with `@tailwindcss/postcss`. Preserve its v4 setup.
- `Rungrow.in/workforce/` currently uses standalone HTML/CSS/JavaScript. Do not add Tailwind as an unused dependency; keep its existing isolated stylesheet unless the UI is deliberately migrated.
- `studio-x-photography` has its own styling setup. Inspect it before applying Tailwind assumptions.

## Validation

- Run the target repository's existing typecheck, lint, unit tests and production build when available.
- Review responsive layouts at mobile, tablet and desktop widths.
- Confirm no unrelated page, component or stylesheet changes.
- Report unexecuted checks honestly; never claim a build or deployment succeeded without evidence.

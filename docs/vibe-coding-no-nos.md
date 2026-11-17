# Vibe Coding No-Nos

This is the default quality standard for every RunGrow website change made through vibe coding, AI-assisted coding or manual implementation. Fast output is not a reason to ship generic design, fake content, broken interactions or tangled code.

## 1. Writing and typography

- Do not make the website sound like an AI assistant, pitch generator or generic SaaS template.
- Avoid filler such as "revolutionize", "unlock your potential", "seamless experience", "elevate your business", "empower your journey", "game changer", "cutting-edge" and "in today's fast-paced world".
- Avoid repetitive formulas such as "It's not just X. It's Y.", "More than X", "From X to Y" and "Whether you are X or Y" when they add no meaning.
- Do not use em dashes, en dashes, arrow glyphs or decorative Unicode symbols as substitutes for clear writing. Prefer ordinary punctuation and direct wording.
- Do not scatter arrow characters such as ->, → or ↗ through buttons, headings, navigation and cards. Use a clear action label. If an icon is necessary, use a consistent SVG icon and mark decorative icons as hidden from assistive technology.
- Do not use emoji, stars, checkmarks or decorative bullets to make weak copy look designed.
- Do not write every heading in title case. Use natural sentence case.
- Do not use all caps for long content. Reserve uppercase for short labels and metadata.
- Do not repeat the same benefit in the hero, cards, feature list and closing section.
- Do not use vague claims. Name the user, task, feature and outcome precisely.
- Do not invent testimonials, customer logos, awards, certifications, adoption numbers, ROI, speed claims or performance statistics.
- Synthetic sample data must be visibly labelled as illustrative demo data, never customer evidence.
- Do not claim a feature, integration, security control, compliance status or product availability unless it is implemented and verified.
- Do not use placeholder copy, fake contact details or misleading "coming soon" messages.

## 2. Visual design

- Do not ship a generic AI-generated SaaS look: purple-blue gradients, neon glow, floating glass cards, random blobs, excessive blur or decoration without a job.
- Changing the palette does not make a weak layout premium. Improve composition, typography, spacing, imagery, hierarchy and content.
- Do not use CSS shapes as fake substitutes for real product screenshots when presenting an actual software product.
- Do not create fake dashboards with arbitrary numbers and charts that imply capabilities the product does not have.
- Do not use stock photography unrelated to the audience, crop, context or message. Images must tell a specific story and share a coherent art direction.
- Do not mix unrelated photography styles, colour temperatures or image treatments on one page.
- Do not put every section inside a rounded card. Use editorial layouts, open space, grids, tables, bands and image-led sections where appropriate.
- Do not use oversized empty hero sections, excessive padding, giant buttons or huge corner radii without a design reason.
- Do not rely on hover effects to communicate essential information.
- Do not add tilt, parallax, cursor glow, scroll animation or 3D effects just because they are possible. Motion must support hierarchy or feedback.
- Do not animate every element or use distracting continuous loops.
- Do not add gradients, shadows, borders or accent colours without a defined role.
- Do not let decorative visuals compete with the primary message or action.
- Do not make every industry page a duplicated template with only the heading changed.
- Do not use emoji or arbitrary text characters as interface icons. Use a consistent icon system.

## 3. UX and interaction

- Do not create buttons that do nothing, links with empty destinations or controls that only look interactive.
- Do not make every call to action point to the same unrelated destination.
- Do not hide product limitations, pricing conditions, availability or data handling information.
- Do not use hover-only navigation. Menus must work with keyboard, touch and click.
- Do not build inaccessible custom controls when native HTML controls work.
- Do not omit focus states, labels, accessible names, semantic landmarks or useful alternative text.
- Do not communicate state using colour alone.
- Do not use tiny text, low contrast or overly subtle interaction states.
- Do not block the page with unnecessary popups, forced signups or repeated banners.
- Do not show a success message from a fake form that does not submit data. Disclose demos clearly.
- Do not make users guess what happens after a click. Use specific action labels.
- Do not break browser back behaviour, deep links, anchor navigation or direct page loading.
- Do not ignore loading, empty, error, success and disabled states where required.
- Do not let animation or pointer effects interfere with touch, scrolling or reduced-motion preferences.

## 4. Responsive design and accessibility

- Do not design desktop first and treat mobile as a compressed afterthought.
- Do not allow horizontal overflow, clipped headings, overlapping controls or tiny tap targets.
- Do not rely on hover as the only way to reveal content.
- Do not remove focus outlines without a clear replacement.
- Do not ignore prefers-reduced-motion.
- Do not use meaningless alt text. Decorative images should use empty alt text.
- Do not choose heading levels for visual size alone. Maintain logical heading order.
- Do not use tables for layout. Use semantic tables for tabular data.
- Do not assume one viewport, browser or input method represents every user.

## 5. Code and repository

- Do not migrate the static RunGrow site to React, Next.js or another framework without explicit founder approval.
- Do not combine unrelated page sections, components, styles or scripts into monolithic files.
- Do not put all CSS in one file or all JavaScript in one script.
- Do not duplicate shared header, footer, navigation, theme or interaction logic.
- Do not use broad global selectors that unexpectedly change unrelated pages.
- Do not add dependencies for effects that existing browser capabilities can handle cleanly.
- Check maintenance, bundle cost, licence, accessibility and actual need before adding a library.
- Do not leave dead CSS, unused scripts, obsolete assets or duplicate implementations after refactoring.
- Do not use brittle selector hacks, excessive !important declarations or unexplained magic numbers.
- Do not use inline styles for reusable design decisions. Put rules in the correct component or page stylesheet.
- Do not hardcode the same design token across many files.
- Do not use arbitrary timeouts to hide race conditions.
- Do not swallow errors silently. Keep useful diagnostics and provide appropriate user-facing states.
- Never put secrets, API keys or private tokens in client-side files or Git.
- Validate and encode user input at the appropriate boundary.
- Do not claim backend, persistence, authentication, analytics, payment or email functionality when only a front-end mock exists.
- Do not break routes, canonical URLs, redirects, SEO metadata or shared component mounting.
- Do not overwrite files before inspecting the current implementation and preserving unrelated work.
- Do not rename public routes or assets casually. Add redirects when a public route must change.
- Do not introduce a new architecture to make a small visual change.

## 6. Product truth

- Do not merge independent products into one assumed platform or imply automatic synchronization unless implemented.
- Do not blur product concept, planned scope, beta, early access and generally available functionality.
- Do not imply payment tracking is the same as payment collection or a payment gateway.
- Do not claim legal, regulatory or certification compliance without documented evidence and appropriate review.
- Do not use synthetic metrics as customer case studies or verified performance.
- Do not advertise roadmap features as currently available.
- Do not use fake scarcity, countdowns, fabricated discounts or misleading urgency.
- Do not hide commercial conditions, plan limits, renewal terms or important exclusions.

## 7. SEO, performance and production

- Do not duplicate titles, descriptions, canonical URLs or social metadata across unrelated pages.
- Do not add structured data that does not match visible content.
- Do not stuff keywords or create near-identical doorway pages.
- Do not leave broken links, missing pages, redirect loops or incorrect canonical destinations.
- Do not ship oversized images when responsive compressed assets will work.
- Do not lazy-load the primary above-the-fold hero image. Prioritise the actual LCP image and lazy-load below-the-fold media.
- Do not load unnecessary fonts, scripts, trackers or third-party embeds.
- Do not block rendering with avoidable JavaScript.
- Do not ignore console errors, failed requests or missing assets.
- Do not call a deployment visually verified just because its build is ready.
- Do not ship without checking changed routes, mobile layout, keyboard navigation and reduced-motion behaviour.
- A passing syntax check is not proof that the user experience works.

## 8. Required vibe-coding method

1. Inspect the repository, page structure, shared components, styles and scripts before editing.
2. Identify the exact problem and affected routes. Avoid unrelated rewrites.
3. Check whether the requested feature already exists before adding another implementation.
4. Make the smallest coherent change. Keep files modular.
5. Use real product screenshots or approved assets when presenting a product. If unavailable, label concept visuals and never invent product capability.
6. Write specific, human copy. Remove filler, repetitive claims and decorative punctuation.
7. Check links, buttons, forms, navigation, page loading and direct route access.
8. Review desktop, tablet and mobile widths, keyboard focus, touch behaviour and reduced motion.
9. Inspect console errors, missing assets, broken images, overflow and regressions.
10. Verify SEO metadata, canonical URLs and structured data on changed pages.
11. Remove dead code and temporary artifacts introduced by the change.
12. Commit a focused change with a descriptive message. Distinguish deployment readiness from visual verification.

## 9. Final review

Before calling a change complete, ask:

- Does this look intentionally designed or like an interchangeable template?
- Does every visual element support content or a real task?
- Does the writing sound like someone who understands this business?
- Are all claims and product capabilities true today?
- Do all links, buttons and controls work?
- Does the layout work on a phone?
- Is the code modular and consistent with this repository?
- Have changed routes and shared components been checked for regressions?
- Have implemented, illustrative and planned items been distinguished?
- Have I inspected the result or only assumed it works?

Fix real defects before declaring the work complete.

## 10. RunGrow-specific rules

- Preserve the existing static HTML architecture.
- Keep shared header and footer markup in their own component files.
- Keep shared scripts in separate modules and page-specific styles in separate stylesheets.
- Keep each product's scope, identity, roadmap and destination distinct.
- Use the RunGrow brand system consistently.
- Prefer clear action labels over decorative arrow characters.
- Prefer relevant photography and authentic product captures over invented dashboard art.
- Never present illustrative values as measured customer outcomes.
- Keep the website useful, clear, fast, accessible and commercially honest.

This document is the default review standard for future RunGrow vibe-coding work.

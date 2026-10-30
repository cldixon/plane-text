# Design system

- `tokens.css`: every color, font, size and space, as CSS custom properties on `:root`. Dark is the default; `:root[data-theme="light"]` overrides the colors for the light theme, which `src/components/ThemeToggle.astro` sets and remembers. Add a token here before using a new value anywhere.
- `base.css`: element defaults, built only from tokens.
- `index.css`: the entry point, imported once by `src/layouts/BaseLayout.astro`. It also loads the two typefaces, self-hosted from `@fontsource`: B612 Mono (title, labels, code) and Public Sans (reading text).

Astro and Svelte components use tokens through `var(--token-name)` in their scoped `<style>` blocks, and never hard-code values.

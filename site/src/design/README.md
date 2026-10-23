# Design system

- `tokens.css`: every color, font, size and space, as CSS custom properties on `:root`. Add a token here before using a new value anywhere.
- `base.css`: element defaults, built only from tokens.
- `index.css`: the entry point, imported once by `src/layouts/BaseLayout.astro`.

Astro and Svelte components use tokens through `var(--token-name)` in their scoped `<style>` blocks, and never hard-code values.

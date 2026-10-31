# Design system

- `tokens.css`: every color, font, size and space, as CSS custom properties on `:root`. Dark is the default; `:root[data-theme="light"]` overrides the colors for the light theme, which `src/components/ThemeToggle.astro` sets and remembers. Add a token here before using a new value anywhere.
- `base.css`: element defaults, built only from tokens.
- `index.css`: the entry point, imported once by `src/layouts/BaseLayout.astro`. It also loads the typefaces, self-hosted from `@fontsource`: Source Serif 4 for reading, B612 for headings, labels and controls, and B612 Mono for the wordmark, data values and names. The shell uses the system's monospace instead (`--font-family-terminal`), since B612 Mono has no box-drawing characters.

Astro and Svelte components use tokens through `var(--token-name)` in their scoped `<style>` blocks, and never hard-code values.

# Design system

- `tokens.css`: every color, font, size and space, as CSS custom properties on `:root`. Dark is the default; `:root[data-theme="light"]` overrides the colors for the light theme, which `src/components/ThemeToggle.astro` sets and remembers. Add a token here before using a new value anywhere.
- `base.css`: element defaults, built only from tokens.
- `index.css`: the entry point, imported once by `src/layouts/BaseLayout.astro`. It also loads the typefaces, self-hosted from `@fontsource`: Source Serif 4 for reading, B612 for headings, labels and controls, and B612 Mono for the wordmark, data values and names. The shell uses the system's monospace instead (`--font-family-terminal`), since B612 Mono has no box-drawing characters.

Astro and Svelte components use tokens through `var(--token-name)` in their scoped `<style>` blocks, and never hard-code values.

## Fonts

B612 and B612 Mono come from Fontsource. Source Serif 4 does not: Fontsource's build drops its small caps and old-style figures, so `fonts/` holds Adobe's variable WOFF2 release (`WOFF2/VAR/SourceSerif4Variable-{Roman,Italic}.ttf.woff2` in adobe-fonts/source-serif), subset to Latin with every layout feature kept and the weight axis limited to 400–700:

```sh
pyftsubset SourceSerif4Variable-Roman.ttf.woff2 --layout-features='*' --flavor=woff2 \
  --unicodes='U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD' \
  --output-file=source-serif-4-roman.woff2
python -c "from fontTools.ttLib import TTFont; from fontTools.varLib import instancer; \
  f = instancer.instantiateVariableFont(TTFont('source-serif-4-roman.woff2'), {'wght': (400, 700)}); f.flavor = 'woff2'; f.save('source-serif-4-roman.woff2')"
```

The same for the italic. Both need `fonttools` and `brotli`.

The query console (the floating Query button and the SQL shell it opens) takes its look from its own `--console-*` tokens in `tokens.css`, which default to the site's. Restyling the console means changing those, not the components.

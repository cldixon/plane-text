# Deploy

The `cf` project that deploys the built site (`../dist`) as static assets on Cloudflare Workers.

- `cloudflare.config.ts`: the Worker (name, compatibility date, 404 handling, custom domain).
- `wrangler.config.ts`: points the asset build at `../dist`. `cf build` uses Wrangler internally to package static assets; this file is that bundler's setting, not a Wrangler CLI config.

Run from `site/` with `bun run preview` or `bun run deploy`, or here with `bun run build | dev | deploy`.

This lives apart from the Astro project because `cf` 1.0.0-beta.21 detects Astro and expects `@astrojs/cloudflare` to emit Cloudflare's Build Output, which the current adapter does not. Once it does, this can fold back into `site/`.

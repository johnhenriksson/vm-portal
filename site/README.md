# Landing page

The project's one-page website, published to GitHub Pages by
`.github/workflows/pages.yml` on every push to `main`. Plain HTML and
CSS: no build step, no scripts, nothing loaded from other sites.

- `index.html`, `site.css`, `favicon.svg`: the page.
- `img/`: screenshots and `og.png` (the 1200×630 preview shown when the link is shared).
  Not in git: the Pages workflow takes them fresh from each pushed commit.
- `screenshots/`: the tools that make `img/`. They're not published.

## Preview

```sh
python3 -m http.server 8098 --directory site
```

Repository links point at `github.com/OWNER/vm-portal`; the Pages workflow fills
in the real owner and name when it deploys.

## Screenshots

```sh
site/screenshots/run.sh
```

This runs the real portal (`demo_server.py`) with invented data: fictional users,
`192.0.2.0/24` addresses (reserved for documentation), `example.com` names, and
a fake vCenter that exists only in that script; the portal has no demo mode.
Headless Chromium from the Playwright image takes the screenshots, as WebP.
Needs Docker and the project's `.venv`. The Pages workflow runs the same script
before every deploy, so the published page always matches the pushed code; run
it locally only to preview the page with images.

Never take screenshots of a real installation for this folder: they'd publish
your IP addresses, hostnames and usernames.

## First deploy

Repository Settings → Pages → Source: **GitHub Actions**, then push to `main`
(or run the workflow by hand).

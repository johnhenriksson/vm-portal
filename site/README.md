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
`192.0.2.0/24` and `198.51.100.0/24` addresses (reserved for documentation),
`example.com` names, and a fake vCenter and a fake Proxmox VE cluster that exist
only in that script; the portal has no demo mode.
Headless Chromium from the Playwright image takes the screenshots, as WebP.
Needs Docker and the project's `.venv`. The Pages workflow runs the same script
before every deploy, so the published page always matches the pushed code; run
it locally only to preview the page with images.

Never take screenshots of a real installation for this folder: they'd publish
your IP addresses, hostnames and usernames.

## Demo videos

```sh
site/video/run.sh                 # all three, into site/video/out/
site/video/run.sh /tmp/out wide   # one of them
```

Clicks through the same demo portal with headless Chromium (`record.py`):
dashboard, picking the Proxmox VE backend and creating a VM with its job log
streaming, the VM page (power, resize,
autostart), images and backups. Frames are screenshots, so text stays sharp; the
cursor is drawn into the page. `compose.py` adds the captions and title cards and
encodes H.264 MP4 with ffmpeg from Ubuntu's archive (`ffmpeg.Dockerfile`, built
locally, run without network). Each video comes with a JPEG poster (the title card).
The Pages workflow records the wide one on every deploy (into `img/`), for the
page's "See it in action" section; the square and tall ones are only made locally.

| File | Shape | Length | For |
|---|---|---|---|
| `vm-portal-wide.mp4` | 1920×1080, 16:9 | about 55 s | the website, YouTube |
| `vm-portal-square.mp4` | 1080×1080, 1:1 | about 35 s | LinkedIn, X, Mastodon, Instagram feed |
| `vm-portal-tall.mp4` | 1080×1920, 9:16 | about 35 s | Reels, Shorts, TikTok |

The square and tall cuts show the portal's phone layout, zoomed in, so it's
readable on a phone. The end card shows this checkout's GitHub remote. Captions
are in `record.py` (`tour()`). Needs Docker and the project's `.venv`; takes a few
minutes per video. The videos aren't in git.

## First deploy

Repository Settings → Pages → Source: **GitHub Actions**, then push to `main`
(or run the workflow by hand).

"""Record the demo video's frames from a running demo_server.py (see run.sh).

    record.py PORTAL_URL VARIANT OUT_DIR [REPO_URL]

VARIANT is wide (16:9, the full tour for the website), square (1:1) or tall (9:16),
the two short cuts for social media. Writes OUT_DIR/frames/*.jpg, the overlays
(frame, captions, title and end cards) as PNGs, and timeline.json for compose.py.

Frames are screenshots taken one at a time, not a screen recording: text stays sharp,
and the cursor is drawn into the page, so it moves exactly as scripted.
"""
import html
import json
import math
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1].rstrip("/")
VARIANT = sys.argv[2]
OUT = Path(sys.argv[3])
REPO_URL = sys.argv[4] if len(sys.argv) > 4 else ""
FPS = 30
FONT = '"Inter", "Segoe UI", system-ui, -apple-system, Roboto, "Helvetica Neue", Arial, sans-serif'
LOGO = (Path(__file__).resolve().parents[1] / "favicon.svg").read_text()

# canvas (w, h); frame: where the portal sits (x, y, w, h); band: caption area height
LAYOUTS = {
    "wide":   {"canvas": (1920, 1080), "frame": (160, 150, 1600, 900), "view": (1280, 720), "long": True,
               "caption": 46, "sub": 24},
    "square": {"canvas": (1080, 1080), "frame": (40, 190, 1000, 850), "view": (700, 595), "long": False,
               "caption": 50, "sub": 0},
    "tall":   {"canvas": (1080, 1920), "frame": (40, 330, 1000, 1480), "view": (680, 1006), "long": False,
               "caption": 62, "sub": 0},
}
L = LAYOUTS[VARIANT]
VW, VH = L["view"]
SCALE = L["frame"][2] / VW

CURSOR_JS = """
(() => {
  const svg = '<svg xmlns="http://www.w3.org/2000/svg" width="26" height="26" viewBox="0 0 26 26">' +
    '<path d="M3 2 L3 21 L8.2 16.4 L11.6 24 L15 22.5 L11.7 15.2 L18.6 15.2 Z" fill="#111" stroke="#fff" ' +
    'stroke-width="1.6" stroke-linejoin="round"/></svg>';
  const make = () => {
    let c = document.getElementById("__cur");
    if (c) return c;
    const r = document.createElement("div");
    r.id = "__ring";
    Object.assign(r.style, {position: "fixed", left: "0", top: "0", width: "40px", height: "40px",
      borderRadius: "50%", background: "rgba(0,105,255,.22)", border: "2px solid rgba(0,105,255,.75)",
      zIndex: "2147483646", pointerEvents: "none", display: "none"});
    c = document.createElement("div");
    c.id = "__cur";
    c.innerHTML = svg;
    Object.assign(c.style, {position: "fixed", left: "0", top: "0", zIndex: "2147483647",
      pointerEvents: "none", filter: "drop-shadow(0 1px 2px rgba(0,0,0,.35))"});
    document.documentElement.append(r, c);
    return c;
  };
  window.__cur = (x, y, ring) => {
    const c = make(), r = document.getElementById("__ring");
    c.style.transform = `translate(${x - 3}px, ${y - 2}px)`;
    if (ring === null || ring === undefined) { r.style.display = "none"; return; }
    const s = 0.4 + ring * 0.9;
    Object.assign(r.style, {display: "block", opacity: String(1 - ring * 0.8),
      transform: `translate(${x - 20}px, ${y - 20}px) scale(${s})`});
  };
})();
"""


def ease(t: float) -> float:
    return 0.5 - math.cos(math.pi * t) / 2


class Director:
    """Drives the page and keeps the timeline: one entry per frame, with how long it shows."""

    def __init__(self, page):
        self.page = page
        self.frames = OUT / "frames"
        self.frames.mkdir(parents=True, exist_ok=True)
        self.timeline: list[list] = []
        self.t = 0.0
        self.cur = (VW * 0.62, VH * 0.55)
        self.ring = None
        self.captions: list[dict] = []
        self.title_end = 0.0
        self.end_start = 0.0

    # -- frames ------------------------------------------------------------------------
    def frame(self, dur: float = 1 / FPS) -> None:
        self.page.evaluate("([x, y, r]) => window.__cur(x, y, r)", [*self.cur, self.ring])
        name = f"{len(self.timeline):05d}.jpg"
        self.page.screenshot(path=str(self.frames / name), type="jpeg", quality=93, caret="initial",
                             animations="disabled")
        self.timeline.append([name, round(dur, 4)])
        self.t += dur

    def hold(self, secs: float) -> None:
        self.frame(secs)

    # -- moving around -----------------------------------------------------------------
    def _box(self, sel: str):
        loc = self.page.locator(sel).first
        loc.wait_for(state="visible", timeout=10000)
        return loc, loc.bounding_box()

    def _scroll_by(self, dy: float, secs: float) -> None:
        start = self.page.evaluate("window.scrollY")
        n = max(1, int(secs * FPS))
        for i in range(1, n + 1):
            self.page.evaluate("y => window.scrollTo(0, y)", start + dy * ease(i / n))
            self.frame()

    def into_view(self, sel: str, top: float = 0.18, bottom: float = 0.82) -> None:
        _, box = self._box(sel)
        if box["y"] < VH * top - 1 or box["y"] + box["height"] > VH * bottom + 1:
            want = VH * top if box["height"] > VH * (bottom - top) else VH * 0.45 - box["height"] / 2
            room = self.page.evaluate("document.documentElement.scrollHeight - innerHeight - scrollY")
            dy = max(-self.page.evaluate("scrollY"), min(box["y"] - want, room))
            if abs(dy) > 2:
                self._scroll_by(dy, min(1.1, 0.35 + abs(dy) / 1400))

    def scroll_to(self, sel: str, top: float = 0.12) -> None:
        _, box = self._box(sel)
        room = self.page.evaluate("document.documentElement.scrollHeight - innerHeight - scrollY")
        dy = max(-self.page.evaluate("scrollY"), min(box["y"] - VH * top, room))
        if abs(dy) > 2:
            self._scroll_by(dy, min(1.2, 0.4 + abs(dy) / 1400))

    def move(self, sel: str, secs: float | None = None, at=(0.5, 0.5)) -> None:
        # Tab bars scroll sideways on phones: bring the tab into the bar's view first.
        self._box(sel)[0].evaluate("""e => { const bar = e.closest('.tabs');
            if (bar && bar.scrollWidth > bar.clientWidth)
              bar.scrollLeft = e.offsetLeft - bar.offsetLeft - bar.clientWidth / 2 + e.offsetWidth / 2; }""")
        self.into_view(sel)
        _, box = self._box(sel)
        tx, ty = box["x"] + box["width"] * at[0], box["y"] + box["height"] * at[1]
        sx, sy = self.cur
        dist = math.hypot(tx - sx, ty - sy)
        secs = secs if secs is not None else min(0.85, 0.3 + dist / 1800)
        n = max(1, int(secs * FPS))
        for i in range(1, n + 1):
            k = ease(i / n)
            # a slight arc, like a hand moving a mouse
            arc = math.sin(math.pi * i / n) * min(40, dist * 0.08)
            self.cur = (sx + (tx - sx) * k, sy + (ty - sy) * k - arc)
            self.frame()

    def click(self, sel: str, nav: bool = False, at=(0.5, 0.5), settle: float = 0.25) -> None:
        self.move(sel, at=at)
        for i in range(5):                       # the press: a ring grows and fades
            self.ring = i / 5
            self.frame()
        if nav:
            # "load", not "networkidle": a job page keeps its log stream open until the job ends.
            with self.page.expect_navigation(wait_until="load"):
                self.page.mouse.click(*self.cur)
            self.page.wait_for_timeout(150)
        else:
            self.page.mouse.click(*self.cur)
        for i in range(5, 9):
            self.ring = i / 9
            self.frame()
        self.ring = None
        self.hold(settle)

    def choose(self, sel: str, value: str) -> None:
        """Pick an option of a dropdown whose form sends itself (a native dropdown's list
        isn't part of the page, so it can't be filmed): press it, and the page changes."""
        self.move(sel)
        for i in range(5):
            self.ring = i / 5
            self.frame()
        with self.page.expect_navigation(wait_until="load"):
            self.page.select_option(sel, value)
        self.page.wait_for_timeout(150)
        for i in range(5, 9):
            self.ring = i / 9
            self.frame()
        self.ring = None
        self.hold(0.25)

    def type(self, sel: str, text: str) -> None:
        self.click(sel, settle=0.15)
        self.page.keyboard.press("Control+A")
        for ch in text:
            self.page.keyboard.type(ch)
            self.frame(2 / FPS)
        self.hold(0.3)

    def live(self, until: str, max_secs: float = 20.0, step: float = 0.12) -> None:
        """Real time: frames as they come, e.g. a job log streaming in."""
        last = time.monotonic()
        end = last + max_secs
        while time.monotonic() < end:
            done = self.page.locator(until).count() > 0
            now = time.monotonic()
            self.frame(max(1 / FPS, now - last))
            last = now
            if done:
                return
            time.sleep(max(0.0, step - (time.monotonic() - now)))

    # -- titles ------------------------------------------------------------------------
    def caption(self, text: str, sub: str = "") -> None:
        if self.captions and self.captions[-1]["end"] is None:
            self.captions[-1]["end"] = self.t
        self.captions.append({"text": text, "sub": sub if L["sub"] else "", "start": self.t, "end": None})

    def title(self, secs: float) -> None:
        self.hold(secs)
        self.title_end = self.t


PROXMOX_BACKEND = "2"      # demo_server.py adds it after the vCenter


def tour(d: Director) -> None:
    long = L["long"]
    page = d.page
    page.goto(BASE + "/")
    page.wait_for_load_state("networkidle")
    d.title(2.8 if long else 2.2)

    d.caption("All your VMs in one place", "Users see their own VMs; admins see them all")
    d.hold(1.6 if long else 1.2)
    if long:
        d.move("table tbody tr:nth-child(2) td:first-child")
        d.hold(0.7)

    d.caption("Create a VM in a few clicks", "Pick an image, a size and an address")
    d.click("nav a[href='/vms/new']", nav=True)
    d.hold(0.6)
    if long:
        d.caption("vSphere and Proxmox VE, side by side", "Each backend has its own templates and addresses")
        d.hold(0.5)
        d.choose(".backend-picker select", PROXMOX_BACKEND)
        d.hold(1.6)
        d.caption("Pick an image, a size and an address", "The same form on every backend")
    d.click("label.choice:has-text('Debian')", at=(0.3, 0.5))
    d.click("[data-slider=cpus] label.step:has-text('4 vCPU')")
    d.click("[data-slider=memory_mb] label.step:has-text('8GB')")
    if long:
        d.click("label:has(input[name=ip_mode][value=static])")
        d.click("button:has-text('Find free IPs')")
        page.wait_for_selector("#ip-suggestions .chip")
        d.hold(0.5)
        d.click("#ip-suggestions .chip >> nth=1")
    d.type("#name", "app-02")
    if long:
        d.move("label.check-field:has(input[name=autostart])", at=(0.03, 0.2))
        d.hold(1.0)
    d.click("button:has-text('Create VM')", nav=True)

    d.caption("Watch it build, live", "Every OpenTofu run is kept, with its full log")
    d.live("#job-status .badge.succeeded", max_secs=20)
    d.hold(1.2 if long else 0.8)

    d.caption("Power, resize and autostart", "Notes, owners and a browser console too")
    d.click("a:has-text('Go to VM')", nav=True)
    d.hold(1.0 if long else 0.6)
    d.click("details.menu > summary")
    d.hold(1.2 if long else 0.8)
    d.click("details.menu > summary")
    if long:
        d.scroll_to("#resize")
        d.type("#resize input[name=cpus]", "8")
        d.move("#resize label.check-field", at=(0.03, 0.2))
        d.hold(0.8)
    d.move("label.check-field:has(input[name=enabled])", at=(0.03, 0.2))
    d.hold(1.2 if long else 1.0)

    d.caption("Admins build the templates with Packer", "Ubuntu, Debian and Rocky Linux definitions built in")
    page.evaluate("window.scrollTo(0, 0)")
    d.click("nav a[href='/images']", nav=True)
    d.hold(1.4 if long else 1.2)
    if long:
        d.click("nav.tabs a[href='/images?tab=builds']", nav=True)
        d.hold(0.6)
        d.click("a:has-text('View log') >> nth=0", nav=True)
        page.wait_for_function("document.querySelector('#log').textContent.includes('Pruning')")
        d.hold(1.8)

    d.caption("Encrypted backups, restored in the browser", "Full or per-VM; scheduled to a share")
    d.click("nav a[href='/settings']", nav=True)
    d.click("nav.tabs a[href='/settings/backup']", nav=True)
    d.hold(0.8)
    d.move("form.card:has-text('Download a backup') button[type=submit]")
    d.hold(1.6 if long else 1.0)

    d.captions[-1]["end"] = d.t
    d.end_start = d.t
    d.hold(3.6 if long else 3.0)   # the end card fades in over this


# --- overlays, drawn as HTML -------------------------------------------------------------

def overlay_page(browser):
    w, h = L["canvas"]
    return browser.new_page(viewport={"width": w, "height": h}, device_scale_factor=1)


def render(pg, body: str, name: str, transparent: bool = True) -> str:
    w, h = L["canvas"]
    pg.set_content(f"""<!doctype html><html><head><meta charset="utf-8"><style>
      html, body {{ margin: 0; width: {w}px; height: {h}px; background: transparent; font-family: {FONT};
                   -webkit-font-smoothing: antialiased; }}
      .bg {{ position: absolute; inset: 0; background: radial-gradient(120% 90% at 20% 0%, #123a8c 0%, #031b4e 55%, #021338 100%); }}
      .logo svg {{ width: 100%; height: 100%; }}
    </style></head><body>{body}</body></html>""")
    pg.wait_for_timeout(50)
    pg.screenshot(path=str(OUT / name), omit_background=transparent)
    return name


def chrome(pg) -> str:
    """Everything around the portal: background, shadow, rounded corners (the portal shows
    through the hole), and on the tall cut a footer with the name."""
    w, h = L["canvas"]
    x, y, fw, fh = L["frame"]
    r = 16
    footer = ""
    if VARIANT == "tall":
        footer = f"""<div style="position:absolute;left:0;right:0;top:{y + fh + 28}px;display:flex;gap:14px;
          align-items:center;justify-content:center;color:#c9d5f0;font-size:34px;font-weight:600">
          <span class="logo" style="width:40px;height:40px">{LOGO}</span>vm-portal
          <span style="font-weight:400;color:#8ea2cf">· open source</span></div>"""
    return render(pg, f"""
      <svg width="{w}" height="{h}" style="position:absolute;inset:0">
        <defs>
          <radialGradient id="g" cx="20%" cy="0%" r="120%">
            <stop offset="0" stop-color="#123a8c"/><stop offset=".55" stop-color="#031b4e"/>
            <stop offset="1" stop-color="#021338"/></radialGradient>
          <filter id="s" x="-10%" y="-10%" width="120%" height="120%"><feGaussianBlur stdDeviation="18"/></filter>
          <mask id="m"><rect width="{w}" height="{h}" fill="#fff"/>
            <rect x="{x}" y="{y}" width="{fw}" height="{fh}" rx="{r}" fill="#000"/></mask>
        </defs>
        <g mask="url(#m)">
          <rect width="{w}" height="{h}" fill="url(#g)"/>
          <rect x="{x}" y="{y + 14}" width="{fw}" height="{fh}" rx="{r}" fill="#000" opacity=".55" filter="url(#s)"/>
          <rect x="{x - 1}" y="{y - 1}" width="{fw + 2}" height="{fh + 2}" rx="{r + 1}" fill="none"
                stroke="rgba(255,255,255,.18)" stroke-width="2"/>
        </g>
      </svg>{footer}""", "chrome.png")


def caption_png(pg, i: int, text: str, sub: str) -> str:
    w, _ = L["canvas"]
    band = L["frame"][1]
    size, subsize = L["caption"], L["sub"]
    sub_html = (f'<div style="margin-top:10px;font-size:{subsize}px;color:#a9badf;font-weight:400">'
                f'{html.escape(sub)}</div>') if sub else ""
    return render(pg, f"""<div style="position:absolute;left:60px;right:60px;top:0;height:{band}px;display:flex;
        flex-direction:column;justify-content:center;align-items:center;text-align:center;color:#fff;
        font-size:{size}px;font-weight:700;letter-spacing:-.01em;line-height:1.15">
        <div>{html.escape(text)}</div>{sub_html}</div>""", f"caption-{i}.png")


def card(pg, name: str, eyebrow: str, headline: str, lead: str, foot: str) -> str:
    w, h = L["canvas"]
    k = {"wide": 1.0, "square": 0.82, "tall": 0.95}[VARIANT]
    return render(pg, f"""<div class="bg"></div>
      <div style="position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;
                  text-align:center;color:#fff;padding:0 {int(80 * k)}px">
        <div class="logo" style="width:{int(120 * k)}px;height:{int(120 * k)}px;margin-bottom:{int(30 * k)}px">{LOGO}</div>
        <div style="font-size:{int(26 * k)}px;letter-spacing:.14em;text-transform:uppercase;color:#7fa6ff;font-weight:600">{html.escape(eyebrow)}</div>
        <div style="font-size:{int(96 * k)}px;font-weight:800;letter-spacing:-.03em;margin:{int(14 * k)}px 0 {int(10 * k)}px">vm-portal</div>
        <div style="font-size:{int(46 * k)}px;font-weight:600;max-width:{int(1300 * k)}px;line-height:1.2">{html.escape(headline)}</div>
        <div style="font-size:{int(30 * k)}px;color:#a9badf;margin-top:{int(22 * k)}px;max-width:{int(1200 * k)}px;line-height:1.35">{html.escape(lead)}</div>
        <div style="font-size:{int(30 * k)}px;color:#fff;margin-top:{int(44 * k)}px;font-weight:600">{html.escape(foot)}</div>
      </div>""", name, transparent=False)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": VW, "height": VH}, device_scale_factor=SCALE,
                                  color_scheme="light", bypass_csp=True)
        ctx.add_init_script(CURSOR_JS)
        page = ctx.new_page()
        page.goto(BASE + "/login")
        page.fill("input[name=username]", "admin")
        page.fill("input[name=password]", "demo-password-123")
        page.click("button[type=submit], form button")
        page.wait_for_url(BASE + "/")

        d = Director(page)
        tour(d)

        pg = overlay_page(browser)
        caps = [{"png": caption_png(pg, i, c["text"], c["sub"]), "start": round(c["start"], 3),
                 "end": round(c["end"], 3)} for i, c in enumerate(d.captions)]
        title = card(pg, "title.png", "Open source · Self-hosted", "Self-service VMs on vSphere and Proxmox VE",
                     "Users create VMs in a few clicks. Admins build the templates with Packer.", "")
        end = card(pg, "end.png", "Open source · Apache-2.0", "Self-service VMs on vSphere and Proxmox VE",
                   "One container next to your vCenter or Proxmox VE cluster.", REPO_URL)
        meta = {"canvas": L["canvas"], "frame": L["frame"], "fps": FPS, "frames": d.timeline,
                "total": round(d.t, 3), "chrome": chrome(pg), "captions": caps,
                "title": {"png": title, "end": round(d.title_end, 3)},
                "end": {"png": end, "start": round(d.end_start, 3)}}
        (OUT / "timeline.json").write_text(json.dumps(meta, indent=1))
        browser.close()
    print(f"{VARIANT}: {len(d.timeline)} frames, {d.t:.1f} s")


if __name__ == "__main__":
    main()

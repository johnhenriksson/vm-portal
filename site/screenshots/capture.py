"""Take the site's screenshots from a running demo_server.py.

    site/screenshots/run.sh      (starts the demo server and a static server, then this)

    capture.py PORTAL_URL [SITE_URL]

Writes WebP screenshots of the portal to site/img/. Chromium encodes them
(canvas.toDataURL), so no image library is needed. With SITE_URL, also renders the
page's hero as img/og.png (1200x630), the preview image for links shared on social media.
"""
import base64
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:8099"
SITE = sys.argv[2].rstrip("/") if len(sys.argv) > 2 else None
OUT = Path(__file__).resolve().parents[1] / "img"
OUT.mkdir(exist_ok=True)
VIEW = {"width": 1360, "height": 850}


def save_webp(page, png: bytes, name: str, quality: float = 0.9) -> None:
    url = page.evaluate("""async ([b64, q]) => {
        const img = new Image();
        img.src = "data:image/png;base64," + b64;
        await img.decode();
        const c = document.createElement("canvas");
        c.width = img.naturalWidth; c.height = img.naturalHeight;
        c.getContext("2d").drawImage(img, 0, 0);
        return c.toDataURL("image/webp", q);
    }""", [base64.b64encode(png).decode(), quality])
    data = base64.b64decode(url.split(",", 1)[1])
    (OUT / f"{name}.webp").write_bytes(data)
    print(f"{name}.webp  {len(data) // 1024} KB")


def shot(page, path: str, name: str, *, wait: str | None = None, prepare=None, full: bool = False) -> None:
    page.goto(BASE + path)
    page.wait_for_load_state("networkidle")
    if wait:
        page.wait_for_selector(wait)
    if prepare:
        prepare(page)
    page.wait_for_timeout(300)
    save_webp(page, page.screenshot(full_page=full, animations="disabled"), name)


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=VIEW, device_scale_factor=2, color_scheme="light")
        page.goto(BASE + "/login")
        page.fill("input[name=username]", "admin")
        page.fill("input[name=password]", "demo-password-123")
        page.click("button[type=submit], form button")
        page.wait_for_url(BASE + "/")

        shot(page, "/", "dashboard")
        shot(page, "/vms/new", "create-vm")
        shot(page, "/vms/web-01", "vm-detail")
        shot(page, "/images?tab=definitions", "images",
             prepare=lambda pg: pg.evaluate("document.querySelector('details.new-def').open = false"))
        shot(page, "/jobs/8", "build-log", wait="#log:not(:empty)",
             prepare=lambda pg: pg.wait_for_function("document.querySelector('#log').textContent.includes('Pruning')"))

        # A custom definition, so the editor shows Save and Validate.
        page.goto(BASE + "/images?tab=definitions")
        page.select_option("select[name=source]", "ubuntu-26.04")
        page.fill("input[name=name]", "Ubuntu 26.04 + monitoring agent")
        page.fill("input[name=new_id]", "ubuntu-monitoring")
        page.fill("input[name=template_prefix]", "ubuntu-monitoring")
        page.click("text=Create and open editor")
        page.wait_for_url("**/images/defs/ubuntu-monitoring/files**")
        shot(page, "/images/defs/ubuntu-monitoring/files?path=ubuntu.pkr.hcl", "editor", wait=".code-editor.enhanced")

        if SITE:
            # bypass_csp: the page's CSP (rightly) refuses the injected style that crops it.
            og = browser.new_context(viewport={"width": 1200, "height": 630}, device_scale_factor=1,
                                     color_scheme="light", bypass_csp=True).new_page()
            og.goto(SITE + "/")
            og.add_style_tag(content=".top{display:none} .hero{padding:0;height:630px;display:flex;align-items:center}"
                                     " .hero .cta,.hero .fine{display:none} .wrap{max-width:1200px}")
            og.wait_for_timeout(300)
            og.screenshot(path=str(OUT / "og.png"))
            print("og.png", (OUT / "og.png").stat().st_size // 1024, "KB")
        browser.close()


if __name__ == "__main__":
    main()

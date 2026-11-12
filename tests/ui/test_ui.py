"""What a person sees and does in the browser: dropdowns, choice cards, menus, the create
form's script, and feedback after a power action."""
import re

from playwright.sync_api import expect

from app import config

from test_proxmox import TEMPLATE, UPID

from .conftest import PASSWORD, add_vm, box, style

GB = 1024 ** 3


def placement(page, backend):
    """The backend's page, once its dropdowns have come from the (fake) cluster."""
    page.goto(f"/settings/backends/{backend.id}")
    expect(page.locator("#placement .alert.ok")).to_contain_text("Connected to Proxmox VE")
    return page.locator("#placement")


# --- dropdowns ----------------------------------------------------------------

def test_dropdowns_look_like_the_text_fields(page, b):
    area = placement(page, b)
    node, vlan = area.locator("select[name=proxmox_node]"), area.locator("input[name=proxmox_vlan]")
    assert box(node)["height"] == box(vlan)["height"]
    assert style(node, "appearance") == "none"                      # not the browser's own arrow
    assert "svg" in style(node, "background-image")                 # ours
    for prop in ("border-top-color", "border-radius", "font-size", "padding-left"):
        assert style(node, prop) == style(vlan, prop), prop
    # the fields of a row start on one line
    assert box(node)["y"] == box(area.locator("select[name=proxmox_storage]"))["y"]


def test_a_storage_says_how_much_room_it_has(page, b, pve):
    pve.api["GET /nodes/pve1/storage"].append(
        {"storage": "ceph", "type": "rbd", "content": "images", "avail": 900 * GB, "total": 1000 * GB,
         "active": 1, "enabled": 1, "shared": 1})
    area = placement(page, b)
    storage = area.locator("select[name=proxmox_storage]")
    assert storage.locator("option").all_inner_texts() == ["Choose…", "ceph", "local-lvm"]    # names only
    detail = storage.locator("xpath=..").locator("[data-detail-text]")
    expect(detail).to_have_text("200 GB free of 400 GB (lvmthin)")
    storage.select_option("ceph")
    expect(detail).to_have_text("900 GB free of 1,000 GB (rbd, shared between nodes)")


def test_an_empty_dropdown_says_why(page, b, pve):
    """A token without permissions: Proxmox answers with empty lists."""
    from app import db
    db.execute("UPDATE backends SET config = json_remove(config, '$.proxmox_bridge') WHERE id=?", (b.id,))
    config.reset_caches()
    pve.api["GET /nodes/pve1/storage"] = []
    pve.api["GET /nodes/pve1/network?type=any_bridge"] = []
    pve.api["GET /nodes/pve1/network"] = [{"iface": "nic0", "type": "eth"}]
    pve.api["GET /access/permissions"] = {}
    area = placement(page, b)
    expect(area.locator(".alert.warn").first).to_contain_text("This API token has no permissions")
    bridge = area.locator("select[name=proxmox_bridge]")
    expect(bridge).to_be_disabled()
    assert style(bridge, "background-image") == "none"              # nothing to open: no arrow
    expect(bridge.locator("option:checked")).to_have_text("No bridge found")
    expect(bridge.locator("xpath=..").locator(".hint")).to_contain_text("no bridge this API token can see")
    # A saved value that is gone stays visible, and the field says so.
    storage = area.locator("select[name=proxmox_storage]")
    expect(storage.locator("option:checked")).to_contain_text("local-lvm (not found in Proxmox)")
    expect(area.locator("select[name=proxmox_pool]")).to_be_enabled()   # optional: (none) is a choice


# --- choices as cards ---------------------------------------------------------

def test_add_a_backend_by_choosing_its_type(page):
    page.goto("/settings/backends")
    cards = page.locator(".card-choice")
    assert cards.locator(".card-choice-title").all_inner_texts() == ["VMware vSphere", "Proxmox VE"]
    assert box(cards.nth(0))["y"] == box(cards.nth(1))["y"] and box(cards.nth(0))["height"] == box(cards.nth(1))["height"]
    page.fill("input[name=name]", "Lab cluster")
    cards.nth(1).click()                                            # anywhere on the card chooses it
    expect(page.locator("input[name=type][value=proxmox]")).to_be_checked()
    assert style(cards.nth(1), "border-top-color") != style(cards.nth(0), "border-top-color")
    page.click("text=Add and set up")
    page.wait_for_url(re.compile(r"/settings/backends/\d+"))
    expect(page.locator("main")).to_contain_text("Proxmox VE")
    expect(page.locator("input[name=proxmox_token_id]")).to_be_visible()


def test_add_a_user_by_choosing_the_role(page):
    page.goto("/settings/users")
    expect(page.locator("input[name=role][value=user]")).to_be_checked()
    title, body = page.locator(".card-choice-title").first, page.locator(".card-choice-body").first
    assert box(body)["y"] - (box(title)["y"] + box(title)["height"]) < 12       # the two lines sit together
    page.fill("input[name=username]", "kari")
    page.locator(".card-choice", has_text="Admin").click()
    page.fill("input[name=password]", "a-long-password-1")
    page.fill("input[name=confirm]", PASSWORD)                         # adding an admin asks for yours
    page.click("button:has-text('Add user')")
    expect(page.locator(".alert.ok, .flash, [role=status]").first).to_contain_text("Added kari (admin)")
    expect(page.locator("table")).to_contain_text("kari")


# --- menus ----------------------------------------------------------------------

def test_more_menu_closes_on_escape_and_on_a_click_elsewhere(page, b):
    add_vm(b)
    page.goto("/")
    menu = page.locator("details.menu").first
    summary = menu.locator("summary")
    summary.click()
    expect(menu.locator(".menu-list")).to_be_visible()
    page.keyboard.press("Escape")
    expect(menu.locator(".menu-list")).to_be_hidden()
    assert page.evaluate("document.activeElement.tagName") == "SUMMARY"          # focus is back on the button
    summary.click()
    page.locator("h1").click()
    expect(menu.locator(".menu-list")).to_be_hidden()


def test_reboot_says_that_it_is_rebooting(page, b, pve):
    """The power menu's Reboot answers at once with a banner, and the list shows it too."""
    pve.api["POST /nodes/pve1/qemu/101/status/reboot"] = UPID
    add_vm(b)
    page.goto("/vms/web-01")
    page.locator("summary", has_text="Power").click()
    page.click("button:has-text('Reboot')")
    expect(page.locator("[role=status]").first).to_contain_text("Rebooting")
    page.locator("summary", has_text="Power").click()                  # and it can't be asked twice
    expect(page.locator(".menu-note")).to_contain_text("Only a hard power off")
    page.goto("/")
    expect(page.locator("#vm-table")).to_contain_text("Rebooting")


# --- the create form --------------------------------------------------------------

def test_create_form_follows_the_template_and_the_choices(page, cluster, pve):
    pve.api["GET /nodes/pve1/qemu/100/config"]["scsi0"] = "local-lvm:base-100-disk-0,size=50G"
    page.goto("/vms/new")
    picker = page.locator("form.backend-picker select")                # two backends: one is chosen
    picker.select_option(str(cluster.id))                              # choosing is enough: no click on Switch
    page.wait_for_url(re.compile(rf"backend={cluster.id}"))

    version = page.locator("select.compact")
    expect(version).to_be_disabled()                                   # one template: nothing to choose
    expect(version).to_contain_text(TEMPLATE)
    assert box(version)["height"] < box(page.locator("select[name=location]"))["height"]

    assert page.locator("select[name=location] option").all_inner_texts() == ["pve1 (default)", "pve2"]

    # The template's disk is 50 GB: smaller stops are greyed out and the form says why.
    disk = page.locator("[data-slider=disk_gb]")
    expect(page.locator("#disk-min-hint")).to_contain_text("50 GB")
    off = disk.locator(".step.off input").evaluate_all("els => els.map(e => +e.value)")
    assert off and max(off) < 50 and page.locator("#disk_gb").input_value() == "50"
    disk.locator(".step", has=page.locator("input[value='80']")).click()
    assert page.locator("#disk_gb").input_value() == "80"

    page.fill("input[name=name]", "app01")
    summary = page.locator(".summary, aside").last
    expect(page.locator("[data-sum=name]")).to_have_text("app01")
    expect(page.locator("[data-sum=size]")).to_contain_text("80 GB")
    assert summary is not None


def test_pages_fit_a_phone(page, b):
    """No page scrolls sideways at phone width, and the dropdowns stay inside it."""
    add_vm(b)
    page.set_viewport_size({"width": 390, "height": 800})
    problems = []
    for path in ("/", "/vms/new", "/vms/web-01", "/settings/backends", f"/settings/backends/{b.id}",
                 "/settings/users", "/images", "/jobs"):
        page.goto(path)
        page.wait_for_load_state("networkidle")
        wide = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        if wide > 1:
            problems.append(f"{path} is {wide}px wider than the screen")
        for s in page.locator("select:visible").all():
            if box(s)["x"] + box(s)["width"] > 391:
                problems.append(f"a dropdown on {path} runs off the screen")
    assert not problems, problems

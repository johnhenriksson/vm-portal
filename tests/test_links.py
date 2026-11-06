"""Names of VMs, images, templates and job targets link to their pages, when the user may open them."""
from app import db, users
from tests.test_web import client, login

TEMPLATE = "ubuntu-2604-docker-20261105-1200"


def _vm(name, owner_id, created_at=None, status="ready"):
    db.execute("INSERT INTO vms (name,status,template,cpus,memory_mb,disk_gb,ip_mode,created_at,owner_id,image_id)"
               " VALUES (?,?,?,1,1024,10,'dhcp',?,?,'ubuntu-26.04')",
               (name, status, TEMPLATE, created_at or db.now(), owner_id))


def test_admin_sees_links_to_vms_images_and_build_targets():
    with client() as c:
        login(c)
        admin = users.by_name("admin")
        _vm("web01", admin["id"])
        vm_job = db.create_job("vm-create", "web01", admin["id"])
        build_job = db.create_job("packer-build", TEMPLATE, admin["id"])
        jobs_page = c.get("/jobs").text
        assert '<a href="/vms/web01">web01</a>' in jobs_page
        assert f'<a href="/images/defs/ubuntu-26.04">{TEMPLATE}</a>' in jobs_page
        assert 'href="/images/defs/ubuntu-26.04"' in c.get("/vms/web01").text
        assert 'href="/images/defs/ubuntu-26.04"' in c.get("/").text  # VM table template
        assert "Go to VM" in c.get(f"/jobs/{vm_job}").text
        assert "Go to image" in c.get(f"/jobs/{build_job}").text


def test_no_links_to_pages_the_user_cant_open():
    with client() as c:
        users.create("alice", "purple-otter-41", "user")
        alice = users.by_name("alice")
        # A job for an earlier VM of the same name: that VM is gone, so no link.
        old = db.create_job("vm-create", "app01", alice["id"])
        db.execute("UPDATE jobs SET created_at='2000-01-01T00:00:00' WHERE id=?", (old,))
        _vm("app01", alice["id"])
        login(c, "alice", "purple-otter-41")
        new = db.create_job("vm-create", "app01", alice["id"])
        page = c.get("/jobs").text
        assert page.count('href="/vms/app01"') == 1  # the current VM's job only
        assert "Go to VM" in c.get(f"/jobs/{new}").text
        assert "Go to VM" not in c.get(f"/jobs/{old}").text
        # Users can't open image pages, so templates are plain text for them.
        assert "/images/defs/" not in c.get("/vms/app01").text
        assert "/images/defs/" not in c.get("/").text


def test_vm_icons_follow_the_image():
    with client() as c:
        login(c)
        admin = users.by_name("admin")
        db.execute("INSERT INTO vms (name,status,template,cpus,memory_mb,disk_gb,ip_mode,created_at,owner_id,image_id)"
                   " VALUES ('db-01','ready','debian-13-docker-20261101-0300',1,1024,10,'dhcp',?,?,'debian-13')",
                   (db.now(), admin["id"]))
        from app.links import Links
        links = Links(admin)
        assert links.distro("debian-13") == "debian"
        assert links.distro(None, "ubuntu-2604-docker-20261101-0300") == "ubuntu"   # older rows: no image_id
        assert links.distro(None, "something-else") == ""
        assert "#a80030" in c.get("/vms/db-01").text   # Debian's colour, not the Ubuntu logo

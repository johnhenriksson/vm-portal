"""Links from a resource's name to its page, for templates.

One instance per rendered page: the image catalog and VM list are loaded at most once,
however many rows link. A link is only returned when the current user can open it, so
pages never show links that lead to a 403 or 404.
"""
from functools import cached_property

from . import db, images


class Links:
    def __init__(self, user: dict | None):
        self.user = user
        self.admin = bool(user) and user["role"] == "admin"

    @cached_property
    def _catalog(self) -> list:
        return images.catalog()

    @cached_property
    def _vms(self) -> dict[str, dict]:
        return {vm["name"]: vm for vm in db.active_vms()}

    def image(self, image_id: str | None) -> str | None:
        """Image definition page (admins only: the images pages are admin pages)."""
        if not self.admin or not image_id or not any(i.id == image_id for i in self._catalog):
            return None
        return f"/images/defs/{image_id}"

    def template(self, name: str | None, image_id: str | None = None) -> str | None:
        """Templates have no page of their own; link to the image definition they come from."""
        if not self.admin or not name:
            return None
        img = images.for_template(name, self._catalog)
        return self.image(img.id if img else image_id)

    def vm(self, name: str | None, since: str | None = None) -> str | None:
        """VM page, if the VM still exists and is visible to the user.

        since: the time the reference was made (e.g. a job's creation). A VM created later
        under the same name is a different VM, so it isn't linked.
        """
        vm = self._vms.get(name or "")
        if not vm or not self.user or not (self.admin or vm["owner_id"] == self.user["id"]):
            return None
        if since and since < vm["created_at"]:
            return None
        return f"/vms/{vm['name']}"

    def job_target(self, job: dict) -> str | None:
        if job["kind"].startswith("vm-"):
            return self.vm(job["target"], since=job["created_at"])
        if job["kind"] == "packer-build":
            return self.template(job["target"])
        return None

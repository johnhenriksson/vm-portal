"""Settings → Backends (admin): add, edit and remove the hypervisor platforms the portal
creates VMs on. Each backend has its own connection, placement pickers and IP pool."""
import asyncio
import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

from . import backends, feedback
from .auth import require_admin
from .config import BACKEND_SECRETS, POOL_FIELDS, BackendFields
from .platforms import PLATFORMS

log = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(require_admin)])


def render(*args, **kwargs):
    from .main import render as _render
    return _render(*args, **kwargs)


def audit(request: Request, user: dict, action: str, detail: str = "") -> None:
    from .admin import audit as _audit
    _audit(request, user, action, detail)


def _backend_or_404(backend_id: int) -> backends.Backend:
    b = backends.get(backend_id)
    if b is None:
        raise HTTPException(404, "No such backend")
    return b


# --- the fields of one backend --------------------------------------------------------------

def field_views(b: backends.Backend, keys, submitted: dict | None = None) -> list[dict]:
    from .admin import FIELDS
    out = []
    for key in keys:
        meta = {"type": "text", **FIELDS[key], "key": key, "locked": key in b.locked}
        value = getattr(b.cfg, key)
        if key in BACKEND_SECRETS:
            meta["is_set"] = bool(value)
            value = ""  # never send a stored secret back to the browser
        elif meta["type"] == "list":
            value = ", ".join(value)
        if submitted is not None and key not in BACKEND_SECRETS and not meta["locked"]:
            value = (submitted.get(key) == "on") if meta["type"] == "bool" else submitted.get(key, "")
        meta["value"] = value
        out.append(meta)
    return out


def form_changes(b: backends.Backend, form, keys=None) -> dict:
    """The backend's settings in a submitted form (only fields the environment doesn't set)."""
    from .admin import FIELDS
    changes = {}
    for key in keys or b.fields:
        if key in b.locked:
            continue
        kind = FIELDS[key].get("type", "text")
        if kind == "bool":
            changes[key] = form.get(key) == "on"
            continue
        raw = form.get(key)
        if not isinstance(raw, str):
            continue
        if key in BACKEND_SECRETS:
            if raw:
                changes[key] = raw   # empty = keep the stored secret
        else:
            changes[key] = raw.strip()
    return changes


def _credentials(b: backends.Backend, form) -> dict:
    """Connection values from the form, falling back to the stored ones. The stored secret
    only goes to the server it was entered for."""
    pf = b.platform
    changes = form_changes(b, form, pf.CONNECTION)
    server, secret = pf.SERVER_FIELD, pf.SECRET_FIELD
    if changes.get(server, getattr(b.cfg, server)) != getattr(b.cfg, server) and secret not in changes \
            and getattr(b.cfg, secret):
        if secret in b.locked:
            raise ValueError(f"The {secret.upper()} is set in the environment, so the server can't be changed "
                             f"here: set {server.upper()} in the environment as well")
        raise ValueError("Re-enter the " + ("password" if b.type == "vsphere" else "API token secret")
                         + " when you change the server")
    values = {k: changes.get(k, getattr(b.cfg, k)) for k in pf.CONNECTION}
    try:
        checked = BackendFields(**values)
    except ValidationError as e:
        from .admin import _error_text
        raise ValueError("; ".join(_error_text(err) for err in e.errors())) from None
    return {k: getattr(checked, k) for k in pf.CONNECTION}


def _select(b, key: str, value: str, options: list[tuple[str, str]], *, optional: bool = False,
            cascade: bool = False, empty: str = "Choose…", details: dict[str, str] | None = None,
            hint: str = "") -> dict:
    """A placement dropdown. cascade: choosing a value reloads the dropdowns. details: a line
    about each option (free space…), shown under the dropdown for the chosen one. hint: why
    there is nothing to choose."""
    from .admin import FIELDS
    names = [v for v, _ in options]
    missing = bool(value) and value not in names
    if missing:
        options = [(value, f"{value} (not found in {b.platform.API})")] + options
    return {"key": key, "label": FIELDS[key]["label"], "value": value, "options": options,
            "locked": key in b.locked, "missing": missing, "optional": optional,
            "cascade": f"/settings/backends/{b.id}/placement" if cascade else "", "empty": empty,
            "api": b.platform.API, "details": details or {}, "hint": hint}


def _auto(value: str, names: list[str]) -> str:
    return names[0] if not value and len(names) == 1 else value


def vsphere_placement(b, inv: dict, chosen: dict) -> list[dict]:
    dcs = inv["datacenters"]
    dc_val = _auto(chosen["vsphere_datacenter"], [d["name"] for d in dcs])
    dc = next((d for d in dcs if d["name"] == dc_val), None)
    clusters = dc["clusters"] if dc else []
    cl_val = _auto(chosen["vsphere_cluster"], [c["name"] for c in clusters])
    cl = next((c for c in clusters if c["name"] == cl_val), None)
    stores = cl["datastores"] if cl else []
    nets = cl["networks"] if cl else []
    first = "Choose a datacenter first" if not dc else "Choose a cluster first"

    def none(items: list, what: str) -> dict:
        """The placeholder, and the reason when the cluster has nothing to choose."""
        if not cl:
            return {"empty": first}
        if not items:
            return {"empty": f"No {what} found", "hint": f"Cluster {cl['name']} has no {what} this user can see."}
        return {"empty": "Choose…"}
    return [
        _select(b, "vsphere_datacenter", dc_val, [(d["name"], d["name"]) for d in dcs], cascade=True),
        _select(b, "vsphere_cluster", cl_val, [(c["name"], c["name"]) for c in clusters], cascade=True,
                empty="Choose…" if dc else first),
        _select(b, "vsphere_datastore", _auto(chosen["vsphere_datastore"], [d["name"] for d in stores]),
                [(d["name"], d["name"]) for d in stores], **none(stores, "datastore"),
                details={d["name"]: f"{d['free_gb']:,} GB free of {d['capacity_gb']:,} GB ({d['type']})"
                         for d in stores}),
        _select(b, "vsphere_network", _auto(chosen["vsphere_network"], nets), [(n, n) for n in nets],
                **none(nets, "network")),
        _select(b, "vsphere_folder", chosen["vsphere_folder"], [(f, f) for f in (dc["folders"] if dc else [])],
                optional=True, empty="(datacenter root)"),
    ]


def proxmox_placement(b, inv: dict, chosen: dict) -> list[dict]:
    def size(st: dict) -> str:
        shared = ", shared between nodes" if st["shared"] else ""
        return f"{st['free_gb']:,} GB free of {st['capacity_gb']:,} GB ({st['type']}{shared})"

    def none(items: list, what: str) -> dict:
        """The placeholder, and the reason when the node has nothing to choose."""
        if not node:
            return {"empty": "Choose a node first"}
        if not items:
            return {"empty": f"No {what} found",
                    "hint": f"Node {node['name']} has no {what} this API token can see."}
        return {"empty": "Choose…"}

    nodes = [n for n in inv["nodes"] if n["online"]]
    node_val = _auto(chosen["proxmox_node"], [n["name"] for n in nodes])
    node = next((n for n in nodes if n["name"] == node_val), None)
    disks = [st for st in (node["storages"] if node else []) if "images" in st["content"]]
    isos = [st for st in (node["storages"] if node else []) if "iso" in st["content"]]
    bridges = node["bridges"] if node else []

    def text(key: str, hint: str = "") -> dict:
        f = field_views(b, [key])[0]
        value = chosen[key]
        f["value"] = ", ".join(value) if isinstance(value, list) else value
        if hint:
            f["hint"] = f"{f.get('hint', '')}. {hint}"
        return f

    # The other nodes VMs can also go on: those with the same storages and bridge.
    disk_val = _auto(chosen["proxmox_storage"], [d["name"] for d in disks])
    iso_val = _auto(chosen["proxmox_iso_storage"], [d["name"] for d in isos])
    bridge_val = _auto(chosen["proxmox_bridge"], bridges)
    others = [n for n in nodes if n["name"] != node_val]
    fit = [n["name"] for n in others
           if disk_val in [s["name"] for s in n["storages"] if "images" in s["content"]]
           and iso_val in [s["name"] for s in n["storages"] if "iso" in s["content"]]
           and bridge_val in n["bridges"]]
    if not others:
        nodes_hint = "This cluster has no other node online"
    elif node and disk_val and iso_val and bridge_val:
        nodes_hint = ("These have the chosen storages and bridge: " + ", ".join(fit)) if fit \
            else "No other node has the chosen storages and bridge"
    else:
        nodes_hint = ""
    return [
        _select(b, "proxmox_node", node_val, [(n["name"], n["name"]) for n in nodes], cascade=True),
        _select(b, "proxmox_storage", _auto(chosen["proxmox_storage"], [d["name"] for d in disks]),
                [(d["name"], d["name"]) for d in disks], **none(disks, "storage for VM disks"),
                details={d["name"]: size(d) for d in disks}),
        _select(b, "proxmox_iso_storage", _auto(chosen["proxmox_iso_storage"], [d["name"] for d in isos]),
                [(d["name"], d["name"]) for d in isos], **none(isos, "storage for ISO images"),
                details={d["name"]: size(d) for d in isos}),
        _select(b, "proxmox_bridge", _auto(chosen["proxmox_bridge"], bridges), [(x, x) for x in bridges],
                **none(bridges, "bridge")),
        _select(b, "proxmox_pool", chosen["proxmox_pool"], [(p, p) for p in inv["pools"]], optional=True,
                empty="(none)"),
        text("proxmox_vlan"),
        text("proxmox_nodes", nodes_hint),
    ]


def proxmox_access(inv: dict, chosen: dict) -> list[str]:
    """Why the dropdowns are empty, or what the API token can't do yet. Proxmox lists only
    what a token may see, without saying that it left things out."""
    privileges = inv.get("privileges")
    if privileges == []:
        return ["This API token has no permissions in Proxmox, so it sees no storages or networks. A "
                "privilege-separated token gets none of its user's permissions: give the token itself a role "
                "under Datacenter > Permissions > Add > API Token Permission (the README lists the privileges), "
                "then press the button above again."]
    out = []
    missing = inv.get("missing_privileges") or []
    if missing:
        out.append("This API token lacks privileges the portal needs: " + ", ".join(missing) + ". Add them to "
                   "its role under Datacenter > Permissions.")
    node = next((n for n in inv["nodes"] if n["online"] and n["name"] == chosen.get("proxmox_node")), None)
    if node is None:
        return out
    stores = node["storages"]
    if not stores:
        out.append(f"The token sees no storage on node {node['name']}. It needs Datastore.Audit on the storages "
                   "(path /storage).")
    else:
        for content, what in (("images", "VM disks (content Disk image)"), ("iso", "ISO images")):
            if not any(content in st["content"] for st in stores):
                out.append(f"No storage on node {node['name']} takes {what}. Turn that content on for a storage "
                           "under Datacenter > Storage.")
    if not node["bridges"]:
        seen = ", ".join(node.get("interfaces") or [])
        out.append(f"Node {node['name']} has no network bridge the token can see"
                   + (f" (its interfaces: {seen})" if seen else "") + ". VMs attach to a bridge such as vmbr0: "
                   f"create one under {node['name']} > System > Network, or give the token Sys.Audit on the node "
                   "and SDN.Use on the bridge.")
    return out


def proxmox_warnings(inv: dict, chosen: dict) -> list[str]:
    """What the chosen placement means on a cluster with several nodes."""
    nodes = [n for n in inv["nodes"] if n["online"]]
    node = next((n for n in nodes if n["name"] == chosen.get("proxmox_node")), None)
    if node is None or len(inv["nodes"]) < 2:
        return []
    out = []
    by_name = {s["name"]: s for s in node["storages"]}
    iso, disk = by_name.get(chosen.get("proxmox_iso_storage")), by_name.get(chosen.get("proxmox_storage"))
    if iso and not iso["shared"]:
        out.append(f"The ISO storage {iso['name']} is local to each node. Every VM keeps a small cloud-init seed "
                   "ISO attached, so Proxmox won't migrate the portal's VMs to another node. With a shared ISO "
                   "storage it can.")
    if disk and not disk["shared"]:
        out.append(f"The disk storage {disk['name']} is local to each node: a VM for another node than the "
                   "template's is cloned on the template's node and then moved, which takes longer.")
    extra = chosen.get("proxmox_nodes") or []
    extra = [x.strip() for x in extra.split(",") if x.strip()] if isinstance(extra, str) else extra
    known = {n["name"]: n for n in inv["nodes"]}
    for name in extra:
        n = known.get(name)
        if n is None:
            out.append(f"Node {name} isn't in this cluster.")
        elif not n["online"]:
            out.append(f"Node {name} is offline; it isn't offered for new VMs until it's back.")
        else:
            missing = [what for what, ok in (
                (f"storage {chosen.get('proxmox_storage')}", chosen.get("proxmox_storage") in [s["name"] for s in n["storages"]]),
                (f"storage {chosen.get('proxmox_iso_storage')}", chosen.get("proxmox_iso_storage") in [s["name"] for s in n["storages"]]),
                (f"bridge {chosen.get('proxmox_bridge')}", chosen.get("proxmox_bridge") in n["bridges"])) if not ok]
            if missing:
                out.append(f"Node {name} has no {' or '.join(missing)}: VMs can't be created there.")
    return out


# Changing a dropdown resets the ones that depend on it.
DEPENDENTS = {"vsphere_datacenter": ["vsphere_cluster", "vsphere_datastore", "vsphere_network", "vsphere_folder"],
              "vsphere_cluster": ["vsphere_datastore", "vsphere_network"],
              "proxmox_node": ["proxmox_storage", "proxmox_iso_storage", "proxmox_bridge"]}
INVENTORY = {"vsphere": ("inventory", vsphere_placement,
                         lambda c: {"server": c["vsphere_server"], "user": c["vsphere_user"],
                                    "password": c["vsphere_password"], "insecure": c["vsphere_insecure"]},
                         ("vsphere_server", "vsphere_user", "vsphere_password")),
             "proxmox": ("inventory", proxmox_placement,
                         lambda c: {"server": c["proxmox_server"], "token_id": c["proxmox_token_id"],
                                    "token_secret": c["proxmox_token_secret"],
                                    "fingerprint": c["proxmox_fingerprint"]},
                         ("proxmox_server", "proxmox_token_id", "proxmox_token_secret"))}


def backend_context(b: backends.Backend, submitted: dict | None = None) -> dict:
    """What the backend's edit form needs."""
    pf = b.platform
    return {"backend": b, "placement_url": f"/settings/backends/{b.id}/placement",
            "fields": field_views(b, pf.CONNECTION, submitted),
            "pool_fields": field_views(b, POOL_FIELDS, submitted),
            "has_creds": all(getattr(b.cfg, k) for k in INVENTORY[b.type][3]),
            "vm_count": backends.vm_count(b.id)}


def list_context() -> dict:
    rows = []
    for b in backends.all_backends():
        rows.append({"b": b, "vms": backends.vm_count(b.id), "server": getattr(b.cfg, b.platform.SERVER_FIELD)})
    return {"backend_rows": rows, "types": [(p.NAME, p.LABEL, p.SUMMARY) for p in PLATFORMS.values()]}


def render_edit(request: Request, b: backends.Backend, status_code: int = 200, submitted=None, **ctx) -> HTMLResponse:
    from .admin import render_settings
    return render_settings(request, "backends", status_code=status_code,
                           **backend_context(b, submitted), **ctx)


# --- routes -----------------------------------------------------------------------------------

@router.post("/settings/backends")
async def backend_create(request: Request, user: dict = Depends(require_admin), name: str = Form(""),
                         kind: str = Form("", alias="type")):
    from .admin import render_settings
    try:
        if kind not in PLATFORMS:
            raise ValueError("Choose the type of backend")
        bid = backends.create(name, kind, {}, by=user["username"])
    except ValueError as e:
        return render_settings(request, "backends", status_code=400, error=str(e), new_name=name)
    audit(request, user, "backend.created", f"{name.strip()} ({kind})")
    return RedirectResponse(f"/settings/backends/{bid}", status_code=303)


@router.get("/settings/backends/{backend_id}")
async def backend_page(request: Request, backend_id: int):
    return render_edit(request, _backend_or_404(backend_id))


async def save_backend(request: Request, user: dict, b: backends.Backend, form, *, keys=None,
                       general: bool = True) -> list[str]:
    """Store the submitted settings of a backend; raises ValueError for the admin."""
    changes = form_changes(b, form, keys)
    kw = {}
    if general:
        kw = {"name": str(form.get("name", b.name)), "enabled": form.get("enabled") == "on",
              "admin_only": form.get("admin_only") == "on"}
    changed = await asyncio.to_thread(backends.update, b, changes, user["username"], **kw)
    if changed:
        audit(request, user, "backend.changed", f"{b.name}: {', '.join(changed)}")   # names only, never values
    return changed


@router.post("/settings/backends/{backend_id}")
async def backend_save(request: Request, backend_id: int, user: dict = Depends(require_admin)):
    b = _backend_or_404(backend_id)
    form = await request.form()
    try:
        await save_backend(request, user, b, form)
    except ValueError as e:
        submitted = {k: v for k, v in form.items() if isinstance(v, str) and k != "csrf_token"}
        return render_edit(request, b, status_code=400, error=str(e), submitted=submitted)
    return render_edit(request, _backend_or_404(backend_id), notice="Backend saved.")


@router.post("/settings/backends/{backend_id}/delete")
async def backend_delete(request: Request, backend_id: int, user: dict = Depends(require_admin),
                         confirm: str = Form("")):
    b = _backend_or_404(backend_id)
    if confirm != b.name:
        return render_edit(request, b, status_code=400, error="Type the backend's name to confirm")
    try:
        backends.delete(b)
    except ValueError as e:
        return render_edit(request, b, status_code=400, error=str(e))
    audit(request, user, "backend.deleted", f"{b.name} ({b.type})")
    return feedback.redirect(request, "/settings/backends", notice=f"Removed {b.name} from the portal. Nothing "
                             f"was changed in {b.platform.API}.")


@router.post("/settings/backends/{backend_id}/placement")
async def backend_placement(request: Request, backend_id: int, user: dict = Depends(require_admin)):
    """htmx: connect with the form's credentials and render the placement dropdowns."""
    b = _backend_or_404(backend_id)
    pf = b.platform
    form = await request.form()
    chosen = {k: (form.get(k) if k not in b.locked and isinstance(form.get(k), str) else getattr(b.cfg, k))
              for k in pf.PLACEMENT}
    for k in DEPENDENTS.get(request.headers.get("HX-Trigger-Name", ""), []):
        if k not in b.locked:
            chosen[k] = ""
    name, view, args, _ = INVENTORY[b.type]
    inventory = getattr(pf, name)
    ctx = {"chosen": chosen, "locked": b.locked, "api": pf.API, "backend": b}
    try:
        creds = _credentials(b, form)
        if not all(creds[k] for k in INVENTORY[b.type][3]):
            raise ValueError("Enter the connection details first")
        inv = await asyncio.wait_for(asyncio.to_thread(inventory, **args(creds), fresh=form.get("fresh") == "1"),
                                     timeout=45)
    except Exception as e:  # noqa: BLE001 - shown to the admin; falls back to text fields
        if not isinstance(e, ValueError):
            audit(request, user, "backend.connect_failed", f"{b.name}: {str(form.get(pf.SERVER_FIELD, ''))[:100]}")
        text = getattr(e, "msg", None) or str(e) or type(e).__name__
        fields = field_views(b, pf.PLACEMENT)
        for f in fields:
            v = chosen[f["key"]]
            f["value"] = ", ".join(v) if isinstance(v, list) else v
        return render(request, "_placement.html", error_text=text, fields=fields, **ctx)
    selects = view(b, inv, chosen)
    picked = {**chosen, **{f["key"]: f["value"] for f in selects if "options" in f}}   # with the auto-chosen ones
    warnings = proxmox_access(inv, picked) + proxmox_warnings(inv, picked) if b.type == "proxmox" else []
    return render(request, "_placement.html", about=inv["about"], selects=selects, warnings=warnings, **ctx)

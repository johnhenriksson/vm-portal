"""Command line for backups, for disaster recovery and automation.

    python -m app.backup keygen   --out FILE
    python -m app.backup create   --out FILE [--vm NAME ...] [--include-logs]
                                  (--passphrase-file FILE | --recipient AGE1... | --scheduled-keys)
    python -m app.backup verify   FILE (--passphrase-file FILE | --identity-file FILE)
    python -m app.backup restore  FILE (--passphrase-file FILE | --identity-file FILE) [--yes]
    python -m app.backup undo-restore [--yes]

In the container: docker compose run --rm portal python -m app.backup ...
Secrets are read from files ("-" = standard input) or typed at a prompt, never taken as
arguments (those end up in shell history and the process list). Restoring needs the
portal stopped: it swaps the database under the process otherwise.
"""
import argparse
import getpass
import json
import os
import sys
from pathlib import Path

from .. import datalock, db, users
from . import archive, collect, restore


def _read_secret(path: str | None, prompt: str) -> str:
    if path == "-":
        return sys.stdin.readline().rstrip("\n")
    if path:
        p = Path(path)
        if p.stat().st_mode & 0o077:
            print(f"warning: {path} is readable by other users; chmod 600 it", file=sys.stderr)
        return p.read_text().strip()
    if not sys.stdin.isatty():
        raise SystemExit("error: give the secret with --passphrase-file or --identity-file")
    return getpass.getpass(prompt)


def _identity_text(text: str) -> str:
    """An age identity file may hold comments: take the AGE-SECRET-KEY line."""
    for line in text.splitlines():
        if line.strip().startswith("AGE-SECRET-KEY-1"):
            return line.strip()
    return text


def _secret(args) -> str:
    if getattr(args, "identity_file", None):
        return _identity_text(_read_secret(args.identity_file, "Secret key: "))
    return _read_secret(args.passphrase_file, "Passphrase: ")


def _write_new(path: str, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def _stopped(what: str) -> None:
    try:
        datalock.acquire(what)
    except datalock.DataDirInUse as e:
        raise SystemExit(f"error: {e} (docker compose stop portal)") from None


def _max_bytes() -> int:
    from ..config import get_bootstrap
    return get_bootstrap().backup_max_mb * 1024 * 1024


def cmd_keygen(args) -> None:
    import pyrage
    ident = pyrage.x25519.Identity.generate()
    public = str(ident.to_public())
    _write_new(args.out, f"# public key: {public}\n{ident}\n".encode())
    print(public)
    print(f"Secret key written to {args.out} (mode 0600). Keep it offline: it opens every backup "
          "encrypted to the public key above.", file=sys.stderr)


def cmd_create(args) -> None:
    from ..config import get_settings
    if not args.allow_running:
        _stopped("a backup from the command line (or pass --allow-running)")
    if args.passphrase_file:
        passphrase, recipients = _read_secret(args.passphrase_file, ""), None
    elif args.recipient or args.scheduled_keys:
        passphrase = None
        recipients = list(args.recipient or []) + (get_settings().backup_recipients if args.scheduled_keys else [])
        if not recipients:
            raise SystemExit("error: no public keys configured under Settings → Backup")
    else:
        passphrase = getpass.getpass("Passphrase (16+ characters): ") if sys.stdin.isatty() else None
        recipients = None
        if not passphrase or getpass.getpass("Again: ") != passphrase:
            raise SystemExit("error: give --passphrase-file, --recipient or --scheduled-keys")
    if args.vm:
        files, meta = collect.vms(args.vm, by="cli")
        kind = "vms"
    else:
        files, meta = collect.full("cli", include_logs=args.include_logs)
        kind = "full"
    data = archive.write(kind, files, passphrase=passphrase, recipients=recipients, **meta)
    _write_new(args.out, data)
    db.audit("backup.created_cli", f"{kind}, {len(data) // 1024} KB", username="cli")
    print(f"Wrote {args.out} ({len(data) // 1024} KB, {kind})")


def _open(args) -> tuple[archive.Backup, bool]:
    data = Path(args.file).read_bytes()
    return archive.read(data, _secret(args), _max_bytes()), archive.encrypted_to_passphrase(data)


def cmd_verify(args) -> None:
    b, passphrase = _open(args)
    m = b.manifest
    print(f"{m['kind']} backup, format {m['version']}, made {m.get('created_at')} by {m.get('created_by')} "
          f"on installation {str(m.get('installation_id', '?'))[:8]}; {len(b.files)} files, checksums OK")
    if b.kind == "full":
        plan = restore.plan_full_restore(b, public_key_encrypted=not passphrase)
        print(json.dumps(plan.summary, indent=1))
        for w in plan.warnings:
            print(f"  ! {w}")
    else:
        for e in collect.vm_entries(b):
            restore.check_state(e["row"].name, e["state"])
            print(f"  {e['row'].name}: state OK")
    print("The backup can be restored.")


def _confirm(args, question: str) -> None:
    if args.yes:
        return
    if not sys.stdin.isatty():
        raise SystemExit("error: pass --yes to confirm without a terminal")
    if input(f"{question} Type RESTORE to continue: ").strip() != "RESTORE":
        raise SystemExit("Cancelled; nothing changed.")


def cmd_restore(args) -> None:
    _stopped("a restore")
    b, passphrase = _open(args)
    plan = restore.plan_full_restore(b, public_key_encrypted=not passphrase)
    print(json.dumps(plan.summary, indent=1))
    for w in plan.warnings:
        print(f"  ! {w}")
    _confirm(args, "This replaces all of the portal's data.")
    restore.apply_full_restore(plan, by="cli")
    print("Restored. Start the portal and log in with an admin account from the backup. The previous data is in "
          f"{restore.PRE_RESTORE} in the data volume (undo with: python -m app.backup undo-restore).")


def cmd_undo(args) -> None:
    _stopped("undoing a restore")
    _confirm(args, "This swaps the data from before the last restore back in.")
    restore.undo_full_restore("cli")
    print("Done. The data you just replaced is now the one kept for undo.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.backup", description="vm-portal backups")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("keygen", help="make an age key pair for scheduled backups")
    p.add_argument("--out", required=True, help="file for the secret key (created 0600, never overwritten)")
    p.set_defaults(fn=cmd_keygen)

    p = sub.add_parser("create", help="write a backup")
    p.add_argument("--out", required=True)
    p.add_argument("--vm", action="append", help="only this VM's state (repeatable)")
    p.add_argument("--include-logs", action="store_true")
    p.add_argument("--passphrase-file")
    p.add_argument("--recipient", action="append", help="age public key (repeatable)")
    p.add_argument("--scheduled-keys", action="store_true", help="the public keys from Settings → Backup")
    p.add_argument("--allow-running", action="store_true",
                   help="back up while the portal runs (a VM job running at the time may leave its state half-written)")
    p.set_defaults(fn=cmd_create)

    for name, fn, help_ in (("verify", cmd_verify, "decrypt and check a backup without changing anything"),
                            ("restore", cmd_restore, "restore a full backup (portal stopped)")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("file")
        g = p.add_mutually_exclusive_group()
        g.add_argument("--passphrase-file")
        g.add_argument("--identity-file", help="age secret key file")
        if name == "restore":
            p.add_argument("--yes", action="store_true")
        p.set_defaults(fn=fn)

    p = sub.add_parser("undo-restore", help="go back to the data from before the last restore (portal stopped)")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(fn=cmd_undo)

    args = ap.parse_args(argv)
    try:
        args.fn(args)
    except (ValueError, OSError) as e:   # BackupError is a ValueError
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        datalock.release()
        users.setup_token = None
    return 0


if __name__ == "__main__":
    sys.exit(main())

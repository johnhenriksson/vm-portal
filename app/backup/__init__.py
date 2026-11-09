"""Backups: encrypted archives of the portal (full) or of chosen VMs' Terraform state.

    archive.py   the file format: age-encrypted tar.gz with a checksummed manifest
    collect.py   what goes into a backup, read from the live portal
    checks.py    validation of everything that comes out of one
    restore.py   full restore (staged, then swapped in) and VM state import
    schedule.py  scheduled backups to BACKUP_DIR, encrypted to age public keys
    __main__.py  command line: python -m app.backup --help
"""

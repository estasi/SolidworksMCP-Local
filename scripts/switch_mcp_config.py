"""Prepare, apply or restore one MCP block; never start/stop CAD or MCP.

Dry-run is the default. Apply requires a quiescent deployment guard and a fresh
configuration comparison immediately before atomic replacement. The lock only
coordinates this installer: do not save the configuration in another editor
during an apply or restore. Diagnostics never print TOML values or credentials.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import tomllib
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class SwitchError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def digest(data):
    return hashlib.sha256(data).hexdigest()


def masked_path(path):
    value = str(path)
    prefixes = {str(Path.home()), os.environ.get("USERPROFILE", "")}
    for prefix in sorted(filter(None, prefixes), key=len, reverse=True):
        if os.path.normcase(value).startswith(os.path.normcase(prefix)):
            boundary = value[len(prefix):len(prefix) + 1]
            if not boundary or boundary in "\\/":
                return "%USERPROFILE%" + value[len(prefix):]
    return value


def ordinary_path(path):
    value = Path(path).expanduser().absolute()
    for part in [value, *value.parents]:
        if part.is_symlink() or (part.exists() and bool(
                getattr(part.stat(follow_symlinks=False), "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))):
            raise SwitchError("REDIRECTED_PATH", "Redirected deployment paths are not allowed")
    return value.resolve()


def parse(data):
    try:
        return tomllib.loads(data.decode("utf-8-sig"))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise SwitchError("INVALID_TOML", "Configuration is not valid UTF-8 TOML") from exc


def target(parsed):
    value = parsed.get("mcp_servers", {}).get("solidworks")
    if not isinstance(value, dict):
        raise SwitchError("BLOCK_MISSING", "The SolidWorks MCP table is missing")
    return value


def without_target(parsed):
    import copy
    value = copy.deepcopy(parsed)
    value["mcp_servers"].pop("solidworks")
    return value


def block_span(data):
    offset = 3 if data.startswith(b"\xef\xbb\xbf") else 0
    tables = list(re.finditer(
        rb"(?m)^[ \t]*\[([^\r\n]+)\][ \t]*(?:#[^\r\n]*)?\r?$", data[offset:]))
    names = [item.group(1).strip() for item in tables]
    starts = [i for i, name in enumerate(names) if name == b"mcp_servers.solidworks"]
    if len(starts) != 1:
        raise SwitchError("AMBIGUOUS_LAYOUT", "Expected one conventional SolidWorks table")
    first, last = starts[0], starts[0] + 1
    while last < len(tables) and names[last].startswith(b"mcp_servers.solidworks."):
        last += 1
    if any(name.startswith(b"mcp_servers.solidworks.")
           for i, name in enumerate(names) if not first <= i < last):
        raise SwitchError("AMBIGUOUS_LAYOUT", "SolidWorks child tables must be contiguous")
    return (offset + tables[first].start(),
            offset + tables[last].start() if last < len(tables) else len(data))


def path_equal(left, right):
    return isinstance(left, str) and os.path.normcase(os.path.abspath(left)) == os.path.normcase(str(right))


def verify_old(settings, source):
    args = settings.get("args")
    if (not path_equal(settings.get("cwd"), source) or
            not isinstance(settings.get("command"), str) or
            not isinstance(args, list) or len(args) not in (1, 2) or
            (len(args) == 2 and args[0] != "-u") or
            not path_equal(args[-1], source / "solidworks_mcp_server.py")):
        raise SwitchError("UNEXPECTED_SOURCE", "Current MCP source or arguments differ from the expected source")


def trailing_comment(line):
    text, quote, index = line.decode("utf-8").rstrip("\r\n"), None, 0
    while index < len(text):
        if quote:
            if quote.startswith('"') and text[index] == "\\":
                index += 2
                continue
            if text.startswith(quote, index):
                index += len(quote)
                quote = None
                continue
        elif text[index] in "\"'":
            quote = text[index] * (3 if text.startswith(text[index] * 3, index) else 1)
            index += len(quote)
            continue
        elif text[index] == "#":
            start = index
            while start and text[start - 1] in " \t":
                start -= 1
            return text[start:].encode("utf-8")
        index += 1
    return b""


def replace_fields(block, fields):
    lines, seen, in_parent = block.splitlines(keepends=True), set(), True
    for index, line in enumerate(lines):
        if line.lstrip().startswith(b"[") and b"mcp_servers.solidworks." in line:
            in_parent = False
        if not in_parent:
            continue
        match = re.match(rb"([ \t]*)(cwd|command|args)[ \t]*=", line)
        if not match:
            continue
        key = match.group(2).decode("ascii")
        try:
            single = tomllib.loads(line.decode("utf-8"))
        except (UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise SwitchError("UNSUPPORTED_ASSIGNMENT", "Use single-line cwd, command and args assignments") from exc
        if set(single) != {key} or key in seen:
            raise SwitchError("AMBIGUOUS_ASSIGNMENT", "Ambiguous MCP path assignment")
        newline = b"\r\n" if line.endswith(b"\r\n") else b"\n" if line.endswith(b"\n") else b""
        lines[index] = (match.group(1) + match.group(2) + b" = " +
                        json.dumps(fields[key], ensure_ascii=False).encode("utf-8") +
                        trailing_comment(line) + newline)
        seen.add(key)
    if seen != set(fields):
        raise SwitchError("FIELDS_MISSING", "Explicit cwd, command and args assignments are required")
    return b"".join(lines)


def splice(data, block):
    start, end = block_span(data)
    before, after_bytes = parse(data), data[:start] + block + data[end:]
    after = parse(after_bytes)
    if (without_target(before) != without_target(after) or
            target(after) != target(parse(block))):
        raise SwitchError("FOREIGN_SETTINGS_CHANGED", "Replacement would change unrelated configuration")
    return after_bytes


def prepare_switch(data, release_source, python, expected_old_source):
    current = target(parse(data))
    verify_old(current, expected_old_source)
    fields = {"cwd": str(release_source), "command": str(python),
              "args": [*current["args"][:-1], str(release_source / "solidworks_mcp_server.py")]}
    start, end = block_span(data)
    old_block = data[start:end]
    new_block = replace_fields(old_block, fields)
    updated = splice(data, new_block)
    expected = dict(current)
    expected.update(fields)
    if target(parse(updated)) != expected:
        raise SwitchError("TARGET_SETTINGS_CHANGED", "Unexpected change within the MCP table")
    return updated, old_block, new_block


@contextmanager
def config_lock(config):
    lock = config.with_name(config.name + ".queue-switch.lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise SwitchError("CONFIG_LOCKED", "Another installer owns the configuration lock") from exc
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(str(os.getpid()))
        yield
    finally:
        lock.unlink(missing_ok=True)


@contextmanager
def deployment_guard(release_source, restoring=False):
    from solidworks_mcp.coordination.ownership import ProcessLease
    from solidworks_mcp.coordination.platform_guard import legacy_processes, process_inventory
    lease = ProcessLease("solidworks-cad")
    if not lease.acquire(timeout=0):
        raise SwitchError("BROKER_RUNNING", "Drain and stop the shared broker before switching configuration")
    try:
        inventory = process_inventory()
        if legacy_processes(inventory, allowed_source=release_source):
            raise SwitchError("LEGACY_CLIENTS_PRESENT", "Disconnect legacy or unidentifiable MCP processes normally")
        if restoring and any(p["name"].lower() != "sldworks.exe" for p in inventory):
            raise SwitchError("MCP_CLIENTS_PRESENT", "Disconnect candidate MCP clients before restoring legacy access")
        yield
    finally:
        lease.release()


def secure_directory(path):
    from solidworks_mcp.coordination.platform_guard import private_directory
    return private_directory(path)


def write_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def preserve_config_security(config, temporary):
    """Atomic replacement must not widen access to a credential-bearing file."""
    if os.name != "nt":
        os.chmod(temporary, stat.S_IMODE(config.stat().st_mode))
        return
    import ctypes
    from ctypes import wintypes
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    api.GetFileSecurityW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD,
                                    ctypes.c_void_p, wintypes.DWORD,
                                    ctypes.POINTER(wintypes.DWORD))
    api.GetFileSecurityW.restype = wintypes.BOOL
    api.SetFileSecurityW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p)
    api.SetFileSecurityW.restype = wintypes.BOOL
    api.GetSecurityDescriptorControl.argtypes = (ctypes.c_void_p,
                                                ctypes.POINTER(wintypes.WORD),
                                                ctypes.POINTER(wintypes.DWORD))
    api.GetSecurityDescriptorControl.restype = wintypes.BOOL
    size = wintypes.DWORD()
    api.GetFileSecurityW(str(config), 4, None, 0, ctypes.byref(size))
    if not size.value:
        raise SwitchError("CONFIG_ACL_UNAVAILABLE", "Cannot preserve configuration access permissions")
    descriptor = ctypes.create_string_buffer(size.value)
    control, revision = wintypes.WORD(), wintypes.DWORD()
    if (not api.GetFileSecurityW(str(config), 4, descriptor, size.value, ctypes.byref(size)) or
            not api.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision))):
        raise SwitchError("CONFIG_ACL_UNAVAILABLE", "Cannot preserve configuration access permissions")
    flags = 4 | (0x80000000 if control.value & 0x1000 else 0x20000000)
    if not api.SetFileSecurityW(str(temporary), flags, descriptor):
        raise SwitchError("CONFIG_ACL_FAILED", "Cannot preserve configuration access permissions")


def replace_if_unchanged(config, expected, replacement):
    fd, temporary = tempfile.mkstemp(prefix=".queue-switch-", suffix=".tmp", dir=config.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(replacement)
            stream.flush()
            os.fsync(stream.fileno())
        preserve_config_security(config, temporary)
        if config.read_bytes() != expected:
            raise SwitchError("CONFIG_CHANGED", "Configuration changed during preparation; nothing replaced")
        os.replace(temporary, config)
        if config.read_bytes() != replacement:
            raise SwitchError("CONFIG_CHANGED_AFTER_REPLACE", "Configuration changed after replacement; inspect before retrying")
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def switch(config, release_source, python, expected_old_source, backup_dir, *, apply=False):
    config, release_source, python, expected_old_source, backup_dir = map(
        ordinary_path, (config, release_source, python, expected_old_source, backup_dir))
    if not config.is_file() or not python.is_file() or not (release_source / "solidworks_mcp_server.py").is_file():
        raise SwitchError("INPUT_MISSING", "Configuration, candidate entry point or interpreter is missing")
    original = config.read_bytes()
    updated, old_block, new_block = prepare_switch(original, release_source, python, expected_old_source)
    result = {"action": "switch", "dry_run": not apply, "config": masked_path(config),
              "release_source": masked_path(release_source), "python": masked_path(python),
              "unrelated_bytes_preserved": True, "mcp_reconnect_required": True}
    if not apply:
        return result
    with config_lock(config), deployment_guard(release_source):
        if config.read_bytes() != original:
            raise SwitchError("CONFIG_CHANGED", "Configuration changed before applying; nothing replaced")
        # Restrict only the new unique backup, never recursively change ACLs of
        # an arbitrary pre-existing backup parent or unrelated files below it.
        backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup = backup_dir / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex)
        backup.mkdir(mode=0o700)
        secure_directory(backup)
        metadata = {"schema": "solidworks-mcp-config-switch/v1", "config": str(config),
                    "release_source": str(release_source), "expected_old_source": str(expected_old_source),
                    "original_config_sha256": digest(original), "activated_config_sha256": digest(updated),
                    "original_block_sha256": digest(old_block), "activated_block_sha256": digest(new_block)}
        write_private(backup / "original-solidworks-block.toml", old_block)
        write_private(backup / "activated-solidworks-block.toml", new_block)
        write_private(backup / "manifest.json", json.dumps(metadata, indent=2).encode("utf-8"))
        replace_if_unchanged(config, original, updated)
        result.update(backup=masked_path(backup), config_sha256=digest(updated))
    return result


def restore(config, backup, *, expected_current_sha256, apply=False):
    config, backup = ordinary_path(config), ordinary_path(backup)
    try:
        metadata = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
        old_block = (backup / "original-solidworks-block.toml").read_bytes()
        active_block = (backup / "activated-solidworks-block.toml").read_bytes()
    except (OSError, ValueError) as exc:
        raise SwitchError("INVALID_BACKUP", "Complete backup files are required") from exc
    if (metadata.get("schema") != "solidworks-mcp-config-switch/v1" or
            not path_equal(metadata.get("config"), config) or
            metadata.get("original_block_sha256") != digest(old_block) or
            metadata.get("activated_block_sha256") != digest(active_block)):
        raise SwitchError("INVALID_BACKUP", "Backup identity or checksums do not match")
    if (not isinstance(expected_current_sha256, str) or
            expected_current_sha256 != metadata.get("activated_config_sha256")):
        raise SwitchError("EXPECTED_HASH_MISMATCH", "Expected current configuration hash does not match this backup")
    original = config.read_bytes()
    if digest(original) != expected_current_sha256:
        raise SwitchError("CONFIG_CHANGED", "Configuration has changed since activation; restore refused")
    start, end = block_span(original)
    if original[start:end] != active_block:
        raise SwitchError("BLOCK_CHANGED", "Current MCP block differs from the recorded activation")
    updated = splice(original, old_block)
    if digest(updated) != metadata.get("original_config_sha256"):
        raise SwitchError("RESTORE_MISMATCH", "Restore cannot reproduce the original configuration")
    result = {"action": "restore", "dry_run": not apply, "config": masked_path(config),
              "backup": masked_path(backup), "unrelated_bytes_preserved": True,
              "mcp_reconnect_required": True}
    if apply:
        with config_lock(config), deployment_guard(Path(metadata["release_source"]), restoring=True):
            replace_if_unchanged(config, original, updated)
        result["config_sha256"] = digest(updated)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--release-source", type=Path)
    parser.add_argument("--python", type=Path)
    parser.add_argument("--expected-old-source", type=Path)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--restore-backup", type=Path)
    parser.add_argument("--expected-current-sha256")
    parser.add_argument("--apply", action="store_true", help="Explicitly allow the guarded configuration write")
    args = parser.parse_args(argv)
    try:
        if args.restore_backup:
            if any((args.release_source, args.python, args.expected_old_source, args.backup_dir)) or not args.expected_current_sha256:
                raise SwitchError("INVALID_ARGUMENTS", "Restore needs only config, backup and expected current SHA256")
            value = restore(args.config, args.restore_backup,
                            expected_current_sha256=args.expected_current_sha256, apply=args.apply)
        else:
            if not all((args.release_source, args.python, args.expected_old_source, args.backup_dir)) or args.expected_current_sha256:
                raise SwitchError("INVALID_ARGUMENTS", "Switch needs candidate source, Python, expected old source and backup directory")
            value = switch(args.config, args.release_source, args.python,
                           args.expected_old_source, args.backup_dir, apply=args.apply)
        print(json.dumps({"success": True, **value}, ensure_ascii=False))
        return 0
    except Exception as exc:
        # Do not echo exceptions: TOML errors, OS errors and third-party errors
        # may contain configuration source lines, paths or credential material.
        print(json.dumps({"success": False, "code": getattr(exc, "code", type(exc).__name__),
                          "message": "Configuration operation refused; no automatic process action was taken"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

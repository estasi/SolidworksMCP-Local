"""Prepare a new release from committed HEAD; no install, activation or LFS fetch.

The destination must not exist. Only git-archive content is copied. A release
manifest is written last; absence of that manifest means preparation incomplete.
The narrow secret/path checks do not replace a publication review of Git history.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import zipfile

try:
    from .switch_mcp_config import SwitchError, masked_path, ordinary_path, write_private
except ImportError:
    from switch_mcp_config import SwitchError, masked_path, ordinary_path, write_private


FORBIDDEN_DIRS = {".git", ".venv", "venv", ".codex", ".claude", "__pycache__",
                  "logs", "checkpoints", "screenshots", "outputs", "artifacts",
                  "installation-state", "evidence", "runtime-state"}
FORBIDDEN_SUFFIXES = {".sldprt", ".sldasm", ".slddrw", ".step", ".stp", ".x_t", ".x_b",
                      ".iges", ".igs", ".stl", ".3mf", ".dxf", ".dwg", ".sqlite", ".sqlite3",
                      ".db", ".log", ".pid", ".pyc"}
SECRET_PATTERN = re.compile(rb"(?:ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|"
                            rb"sk-proj-[A-Za-z0-9_-]{30,}|AKIA[0-9A-Z]{16}|"
                            rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)")


def git(repo, *args):
    command = ["git", "-C", str(repo)]
    environment = None
    if args and args[0] == "archive":
        # Some Git/LFS installations apply smudge filters to archive output.
        # Export the committed pointer bytes without executing the LFS process,
        # fetching weights, or lazily fetching absent objects from a promisor.
        command += ["-c", "filter.lfs.process=", "-c", "filter.lfs.smudge=",
                    "-c", "filter.lfs.required=false"]
        environment = dict(os.environ, GIT_LFS_SKIP_SMUDGE="1",
                           GIT_TERMINAL_PROMPT="0", GIT_NO_LAZY_FETCH="1")
    run = subprocess.run([*command, *args], capture_output=True, check=False,
                         timeout=60, env=environment)
    if run.returncode:
        raise SwitchError("GIT_FAILED", "Required local Git operation failed")
    return run.stdout


def permitted(name):
    path = PurePosixPath(name)
    lower = path.name.lower()
    return (not path.is_absolute() and ".." not in path.parts and "\\" not in name
            and ":" not in name and not FORBIDDEN_DIRS.intersection(p.lower() for p in path.parts)
            and path.suffix.lower() not in FORBIDDEN_SUFFIXES
            and not lower.endswith(("-wal", "-shm", ".local.json"))
            and lower not in {"endpoint.json", "activation.json", "config.local.json"}
            and not (lower.startswith(".env") and lower != ".env.example"))


def prepare(repo, output):
    repo, output = ordinary_path(repo), ordinary_path(output)
    if output.exists() or not output.parent.is_dir():
        raise SwitchError("OUTPUT_EXISTS_OR_PARENT_MISSING", "Use a new destination with an existing parent")
    if output == repo or repo in output.parents:
        raise SwitchError("OUTPUT_INSIDE_SOURCE", "Release output must be outside the source checkout")
    git_root = Path(git(repo, "rev-parse", "--show-toplevel").decode("utf-8").strip()).resolve()
    if os.path.normcase(str(git_root)) != os.path.normcase(str(repo)):
        raise SwitchError("REPOSITORY_ROOT_REQUIRED", "Select the repository root for release preparation")
    if git(repo, "status", "--porcelain", "--untracked-files=all").strip():
        raise SwitchError("UNCOMMITTED_SOURCE", "Commit all intended changes before preparing a release")
    commit = git(repo, "rev-parse", "--verify", "HEAD").decode("ascii").strip()
    archive = git(repo, "archive", "--format=zip", commit)
    files, models = {}, []
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        for item in zipped.infolist():
            if item.is_dir():
                continue
            if not permitted(item.filename) or stat.S_ISLNK(item.external_attr >> 16):
                raise SwitchError("FORBIDDEN_RELEASE_FILE", "Archive contains runtime, secret, CAD or redirected file material")
            data = zipped.read(item)
            if SECRET_PATTERN.search(data):
                raise SwitchError("SENSITIVE_RELEASE_CONTENT", "Archive contains a selected credential pattern")
            files[item.filename] = data
            if data.startswith(b"version https://git-lfs.github.com/spec/v1"):
                match = re.search(rb"(?m)^size ([0-9]+)\r?$", data)
                models.append({"path": item.filename, "pointer_only": True,
                               "expected_object_bytes": int(match[1]) if match else None})
    required = {"solidworks_mcp_server.py", "solidworks_mcp/__init__.py", "LICENSE"}
    if not required.issubset(files) or "release-manifest.json" in files:
        raise SwitchError("INVALID_SOURCE_TREE", "Required source files are missing or a stale manifest is tracked")
    tree = ast.parse(files["solidworks_mcp/__init__.py"].decode("utf-8-sig"))
    versions = [ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets)]
    if len(versions) != 1 or not isinstance(versions[0], str):
        raise SwitchError("VERSION_MISSING", "A single literal package version is required")
    manifest = {"schema": "solidworks-mcp-release/v1", "version": versions[0], "commit": commit,
                "source": "git archive HEAD", "native_acceptance": "not_performed_by_preparation",
                "lfs_objects_bundled": False, "lfs_pointers": models,
                "publication_review_required": True,
                "files": [{"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                          for name, data in sorted(files.items())]}
    # Validate everything before creating output; never overwrite an existing
    # directory, and never delete an incomplete output after an I/O failure.
    output.mkdir(mode=0o700)
    for name, data in files.items():
        destination = output.joinpath(*PurePosixPath(name).parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        write_private(destination, data)
    write_private(output / "release-manifest.json", json.dumps(manifest, indent=2).encode("utf-8"))
    return {"prepared": True, "output": masked_path(output), "version": versions[0],
            "commit": commit, "file_count": len(files), "lfs_pointer_count": len(models),
            "lfs_objects_bundled": False, "activated": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        print(json.dumps({"success": True, **prepare(args.repo, args.output)}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"success": False, "code": getattr(exc, "code", type(exc).__name__),
                          "message": "Release preparation refused; no installation was changed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

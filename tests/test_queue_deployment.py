"""Synthetic deployment tests. No live config, process signalling or CAD calls."""
from contextlib import contextmanager, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from scripts import switch_mcp_config as switcher
from scripts import prepare_queue_release as release


@contextmanager
def fake_guard(*args, **kwargs):
    yield


def fake_secure(path):
    Path(path).mkdir(parents=True, exist_ok=True)
    return Path(path)


class ConfigSwitchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="cad-switch-synthetic-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.old, self.new = self.root / "old", self.root / "new release"
        self.old.mkdir()
        self.new.mkdir()
        (self.new / "solidworks_mcp_server.py").write_text("# synthetic entry point\n")
        self.python = self.root / "synthetic-python.exe"
        self.python.write_bytes(b"synthetic placeholder, never executed")
        self.config = self.root / "synthetic-config.toml"
        self.backups = self.root / "private-backups"
        self.secret = "synthetic-private-" + "value"
        quoted = lambda value: json.dumps(str(value))
        self.original = (
            '# foreign header\r\ntitle = "preserve"\r\n'
            '[mcp_servers.other]\r\ncommand = "other-command"\r\n'
            '[mcp_servers.other.env]\r\nPRIVATE_VALUE = "' + self.secret + '"\r\n'
            '# preserve before table\r\n[mcp_servers.solidworks]\r\n'
            'command = ' + quoted(self.root / "old-python.exe") + '\r\n'
            'cwd = ' + quoted(self.old) + '\r\n'
            'args = ["-u", ' + quoted(self.old / "solidworks_mcp_server.py") + ']\r\n'
            'startup_timeout_sec = 120 # preserved\r\n'
            '[mcp_servers.solidworks.env]\r\nPRIVATE_VALUE = "' + self.secret + '"\r\n'
            '[features]\r\nkeep = true\r\n# exact final comment\r\n'
        ).encode("utf-8")
        self.config.write_bytes(self.original)
        self.guard = mock.patch.object(switcher, "deployment_guard", side_effect=fake_guard).start()
        self.secure = mock.patch.object(switcher, "secure_directory", side_effect=fake_secure).start()
        self.addCleanup(mock.patch.stopall)

    def switch(self, **kwargs):
        return switcher.switch(self.config, self.new, self.python, self.old, self.backups, **kwargs)

    def backup(self):
        return next(self.backups.iterdir())

    def cli_args(self):
        return ["--config", str(self.config), "--release-source", str(self.new),
                "--python", str(self.python), "--expected-old-source", str(self.old),
                "--backup-dir", str(self.backups)]

    def test_default_dry_run_has_no_mutations_or_process_guard(self):
        before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*"))
        result = self.switch()
        self.assertTrue(result["dry_run"])
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertEqual(before, sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*")))
        self.guard.assert_not_called()
        self.secure.assert_not_called()

    def test_apply_preserves_foreign_bytes_and_target_extra_settings(self):
        result = self.switch(apply=True)
        updated = self.config.read_bytes()
        old_start, old_end = switcher.block_span(self.original)
        new_start, new_end = switcher.block_span(updated)
        self.assertEqual(updated[:new_start], self.original[:old_start])
        self.assertEqual(updated[new_end:], self.original[old_end:])
        old = switcher.target(switcher.parse(self.original))
        new = switcher.target(switcher.parse(updated))
        self.assertEqual(new["env"], old["env"])
        self.assertEqual(new["startup_timeout_sec"], 120)
        self.assertEqual(new["command"], str(self.python))
        self.assertEqual(new["cwd"], str(self.new))
        self.assertEqual(new["args"], ["-u", str(self.new / "solidworks_mcp_server.py")])
        self.assertEqual(result["config_sha256"], hashlib.sha256(updated).hexdigest())
        self.assertEqual((self.backup() / "original-solidworks-block.toml").read_bytes(),
                         self.original[old_start:old_end])
        self.assertNotIn(self.secret, (self.backup() / "manifest.json").read_text())

    def test_backup_restore_exact_equality_and_restore_dry_run(self):
        self.switch(apply=True)
        active = self.config.read_bytes()
        expected = switcher.digest(active)
        switcher.restore(self.config, self.backup(), expected_current_sha256=expected)
        self.assertEqual(active, self.config.read_bytes())
        switcher.restore(self.config, self.backup(), expected_current_sha256=expected, apply=True)
        self.assertEqual(self.original, self.config.read_bytes())
        self.assertTrue(self.guard.call_args.kwargs["restoring"])

    def test_backup_directories_are_unique_after_restore_and_reapply(self):
        self.switch(apply=True)
        first = self.backup()
        switcher.restore(self.config, first, expected_current_sha256=switcher.digest(self.config.read_bytes()), apply=True)
        self.switch(apply=True)
        self.assertEqual(len(list(self.backups.iterdir())), 2)
        self.assertTrue(first.exists())

    def test_changed_foreign_config_refuses_restore(self):
        self.switch(apply=True)
        expected = switcher.digest(self.config.read_bytes())
        changed = self.config.read_bytes().replace(b"keep = true", b"keep = false")
        self.config.write_bytes(changed)
        with self.assertRaisesRegex(switcher.SwitchError, "changed since activation"):
            switcher.restore(self.config, self.backup(), expected_current_sha256=expected, apply=True)
        self.assertEqual(changed, self.config.read_bytes())

    def test_wrong_expected_restore_hash_is_rejected(self):
        self.switch(apply=True)
        with self.assertRaises(switcher.SwitchError):
            switcher.restore(self.config, self.backup(), expected_current_sha256="0" * 64, apply=True)

    def test_backup_corruption_is_rejected(self):
        self.switch(apply=True)
        active = self.config.read_bytes()
        (self.backup() / "original-solidworks-block.toml").write_bytes(b"corrupt")
        with self.assertRaises(switcher.SwitchError):
            switcher.restore(self.config, self.backup(), expected_current_sha256=switcher.digest(active), apply=True)
        self.assertEqual(self.config.read_bytes(), active)

    def test_other_config_cannot_use_backup(self):
        self.switch(apply=True)
        other = self.root / "other-config.toml"
        other.write_bytes(self.config.read_bytes())
        with self.assertRaises(switcher.SwitchError):
            switcher.restore(other, self.backup(), expected_current_sha256=switcher.digest(other.read_bytes()), apply=True)

    def test_cas_during_preparation_refuses_to_overwrite_external_change(self):
        real_write = switcher.write_private
        changed = self.original.replace(b"keep = true", b"keep = false")
        def write_and_edit(path, data):
            real_write(path, data)
            if Path(path).name == "manifest.json":
                self.config.write_bytes(changed)
        with mock.patch.object(switcher, "write_private", side_effect=write_and_edit):
            with self.assertRaisesRegex(switcher.SwitchError, "changed during preparation"):
                self.switch(apply=True)
        self.assertEqual(self.config.read_bytes(), changed)
        self.assertEqual(len(list(self.backups.iterdir())), 1)

    def test_guard_failure_leaves_config_and_backup_untouched(self):
        self.guard.side_effect = switcher.SwitchError("BROKER_RUNNING", "Synthetic occupied lease")
        with self.assertRaises(switcher.SwitchError):
            self.switch(apply=True)
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertFalse(self.backups.exists())
        self.assertFalse(self.config.with_name(self.config.name + ".queue-switch.lock").exists())

    def test_unexpected_old_source_and_multiline_assignment_fail_closed(self):
        with self.assertRaises(switcher.SwitchError):
            switcher.switch(self.config, self.new, self.python, self.root / "wrong", self.backups)
        self.config.write_bytes(self.original.replace(b'args = ["-u", ', b'args = [\r\n"-u", '))
        with self.assertRaises(switcher.SwitchError):
            self.switch()

    def test_noncontiguous_target_env_is_rejected(self):
        changed = self.original.replace(b'[mcp_servers.solidworks.env]', b'[split]\r\nx = 1\r\n[mcp_servers.solidworks.env]')
        self.config.write_bytes(changed)
        with self.assertRaises(switcher.SwitchError):
            self.switch()

    def test_cli_never_echoes_secret_values_on_success_or_failure(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(switcher.main(self.cli_args()), 0)
        self.assertNotIn(self.secret, output.getvalue())
        with mock.patch.object(switcher, "prepare_switch", side_effect=ValueError(self.secret)):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(switcher.main(self.cli_args()), 1)
        self.assertNotIn(self.secret, output.getvalue())

    def test_profile_prefix_is_masked_with_boundary_check(self):
        with mock.patch.dict(os.environ, {"USERPROFILE": str(self.root)}):
            self.assertEqual(switcher.masked_path(self.config), "%USERPROFILE%" + os.sep + self.config.name)
            self.assertEqual(switcher.masked_path(str(self.root) + "-other"), str(self.root) + "-other")

    def test_bom_and_first_target_table_are_preserved(self):
        start, end = switcher.block_span(self.original)
        original = b"\xef\xbb\xbf" + self.original[start:end] + self.original[end:]
        self.config.write_bytes(original)
        self.switch(apply=True)
        self.assertTrue(self.config.read_bytes().startswith(b"\xef\xbb\xbf"))
        switcher.restore(self.config, self.backup(),
                         expected_current_sha256=switcher.digest(self.config.read_bytes()), apply=True)
        self.assertEqual(self.config.read_bytes(), original)

    def test_permissions_failure_leaves_config_unchanged(self):
        with mock.patch.object(switcher, "preserve_config_security", side_effect=
                               switcher.SwitchError("CONFIG_ACL_FAILED", "Synthetic permission failure")):
            with self.assertRaises(switcher.SwitchError):
                self.switch(apply=True)
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_path_assignment_comments_are_preserved(self):
        lines = self.original.splitlines(keepends=True)
        changed = b"".join(line.rstrip(b"\r\n") + b" # retained assignment note\r\n"
                           if line.startswith(b"command = ") or line.startswith(b"cwd = ")
                           else line for line in lines)
        self.config.write_bytes(changed)
        self.switch(apply=True)
        self.assertEqual(self.config.read_bytes().count(b"# retained assignment note"), 3)
        self.assertEqual(switcher.trailing_comment(b'command = "has#inside" # outside\r\n'), b" # outside")


class DeploymentGuardTests(unittest.TestCase):
    def setUp(self):
        from solidworks_mcp.coordination import ownership, platform_guard
        self.owner = mock.patch.object(ownership, "ProcessLease").start()
        self.inventory = mock.patch.object(platform_guard, "process_inventory").start()
        self.addCleanup(mock.patch.stopall)
        self.lease = self.owner.return_value
        self.lease.acquire.return_value = True
        self.release = Path(tempfile.gettempdir()).resolve() / "synthetic-new-release"
        self.inventory.return_value = [{"pid": 100, "name": "SLDWORKS.exe", "script": None}]

    def test_live_broker_refuses_guard_without_scanning_or_releasing_unowned_lease(self):
        self.lease.acquire.return_value = False
        with self.assertRaises(switcher.SwitchError):
            with switcher.deployment_guard(self.release):
                self.fail("Occupied broker guard must not enter")
        self.inventory.assert_not_called()
        self.lease.release.assert_not_called()

    def test_unidentifiable_client_refuses_guard_and_releases_lease(self):
        self.inventory.return_value.append({"pid": 101, "name": "python.exe", "script": None})
        with self.assertRaises(switcher.SwitchError):
            with switcher.deployment_guard(self.release):
                self.fail("Unknown client guard must not enter")
        self.lease.release.assert_called_once()

    def test_restore_requires_candidate_clients_disconnected(self):
        self.inventory.return_value.append({"pid": 101, "name": "python.exe",
                                            "script": str(self.release / "solidworks_mcp_server.py")})
        with self.assertRaises(switcher.SwitchError):
            with switcher.deployment_guard(self.release, restoring=True):
                self.fail("Restore must not retain reconnecting candidate clients")

    def test_clear_guard_uses_stable_resource_and_releases_it(self):
        with switcher.deployment_guard(self.release):
            pass
        self.owner.assert_called_once_with("solidworks-cad")
        self.lease.release.assert_called_once()


class ReleasePreparationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="cad-release-synthetic-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.output = self.root / "release"
        self.git("init", "-q")
        self.git("config", "user.name", "Synthetic Test")
        self.git("config", "user.email", "synthetic@example.invalid")
        (self.repo / "solidworks_mcp").mkdir()
        (self.repo / "solidworks_mcp" / "__init__.py").write_text('__version__ = "0.0.0-test"\n')
        (self.repo / "solidworks_mcp_server.py").write_text("# synthetic fixture\n")
        (self.repo / "LICENSE").write_text("Synthetic test license fixture\n")
        (self.repo / "model.pt").write_bytes(
            b"version https://git-lfs.github.com/spec/v1\noid sha256:" + b"a" * 64 + b"\nsize 141291499\n")
        self.commit()

    def git(self, *args):
        result = subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True)
        self.assertEqual(result.returncode, 0, "Synthetic Git fixture operation failed")
        return result.stdout

    def commit(self):
        self.git("add", "--all")
        self.git("commit", "-q", "-m", "Synthetic fixture")

    def test_prepares_committed_files_manifest_and_lfs_limitation(self):
        result = release.prepare(self.repo, self.output)
        self.assertFalse(result["activated"])
        self.assertFalse(result["lfs_objects_bundled"])
        manifest = json.loads((self.output / "release-manifest.json").read_text())
        self.assertEqual(manifest["version"], "0.0.0-test")
        self.assertEqual(manifest["lfs_pointers"][0]["expected_object_bytes"], 141291499)
        for entry in manifest["files"]:
            data = (self.output / entry["path"]).read_bytes()
            self.assertEqual(entry["sha256"], hashlib.sha256(data).hexdigest())
        self.assertFalse((self.output / ".git").exists())

    def test_refuses_dirty_source_and_existing_output(self):
        (self.repo / "uncommitted.txt").write_text("synthetic change")
        with self.assertRaises(switcher.SwitchError):
            release.prepare(self.repo, self.output)
        self.assertFalse(self.output.exists())
        self.commit()
        self.output.mkdir()
        with self.assertRaises(switcher.SwitchError):
            release.prepare(self.repo, self.output)

    def test_rejects_runtime_and_cad_files_before_creating_output(self):
        for name in ("queue.sqlite3", "customer.SLDPRT", "config.local.json", ".env"):
            with self.subTest(name=name):
                path = self.repo / name
                path.write_text("synthetic rejected fixture")
                self.commit()
                with self.assertRaises(switcher.SwitchError):
                    release.prepare(self.repo, self.output)
                self.assertFalse(self.output.exists())
                path.unlink()
                self.commit()

    def test_selected_secret_pattern_rejected_without_echo(self):
        synthetic_token = "ghp_" + "A" * 36
        (self.repo / "credential.txt").write_text(synthetic_token)
        self.commit()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(release.main(["--repo", str(self.repo), "--output", str(self.output)]), 1)
        self.assertNotIn(synthetic_token, output.getvalue())
        self.assertFalse(self.output.exists())

    def test_archive_path_filter_rejects_escape_and_platform_paths(self):
        for path in ("../outside.py", "/absolute.py", "C:/private.py", "a\\b.py",
                     "logs/job.txt", "queue.sqlite3-wal", "artifacts/result.txt"):
            with self.subTest(path=path):
                self.assertFalse(release.permitted(path))
        self.assertTrue(release.permitted("docs/QUEUE_OPERATIONS.md"))
        self.assertTrue(release.permitted(".env.example"))

    def test_archive_disables_failing_lfs_filter_and_keeps_pointer_bytes(self):
        (self.repo / ".gitattributes").write_text("*.pt filter=lfs diff=lfs merge=lfs -text\n")
        script = self.repo / "synthetic_filter.py"
        script.write_text(
            "from pathlib import Path\n"
            "Path(__file__).with_name('filter-was-run.marker').write_text('called')\n"
            "raise SystemExit(99)\n")
        self.commit()
        command = '"' + Path(sys.executable).as_posix() + '" "' + script.as_posix() + '"'
        self.git("config", "filter.lfs.process", command)
        self.git("config", "filter.lfs.smudge", command)
        self.git("config", "filter.lfs.required", "true")
        release.prepare(self.repo, self.output)
        self.assertFalse((self.repo / "filter-was-run.marker").exists())
        self.assertEqual((self.output / "model.pt").read_bytes(), (self.repo / "model.pt").read_bytes())
        self.assertTrue((self.output / "model.pt").read_bytes().startswith(
            b"version https://git-lfs.github.com/spec/v1"))

    def test_archive_filter_overrides_are_per_command_with_no_lazy_fetch(self):
        completed = subprocess.CompletedProcess([], 0, b"archive", b"")
        with mock.patch.object(release.subprocess, "run", return_value=completed) as runner:
            self.assertEqual(release.git(self.repo, "archive", "--format=zip", "HEAD"), b"archive")
        command = runner.call_args.args[0]
        self.assertIn("filter.lfs.process=", command)
        self.assertIn("filter.lfs.smudge=", command)
        self.assertIn("filter.lfs.required=false", command)
        environment = runner.call_args.kwargs["env"]
        self.assertEqual(environment["GIT_LFS_SKIP_SMUDGE"], "1")
        self.assertEqual(environment["GIT_NO_LAZY_FETCH"], "1")
        self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")


if __name__ == "__main__":
    unittest.main()

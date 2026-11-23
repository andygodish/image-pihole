"""Safety checks that never contact Docker, DNS, or the NAS."""

import argparse
import importlib.util
from contextlib import closing
import json
from pathlib import Path
import subprocess
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("backup", Path(__file__).with_name("backup.py"))
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


SOURCE = "nas.example.net:/pihole-backups"
CONTAINER = "test-pihole"


class BackupTests(unittest.TestCase):
    def test_env_reads_only_backup_fields_without_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / ".env"
            marker = Path(temp) / "must-not-exist"
            path.write_text(
                f"UNRELATED=$(touch {marker})\n"
                "BACKUP_CONTAINER='test-pihole' # selected container\n"
                "BACKUP_VOLUME=test-volume\n"
                "BACKUP_SOURCE=nas.example.net:/backups\n"
                "BACKUP_DESTINATION='/mnt/backup share'\n"
                "export BACKUP_STAGING=/var/lib/pihole-backup\n")
            config = backup.load_env(path)
            self.assertEqual(config.repo, path.parent)
            self.assertEqual(config.destination, Path("/mnt/backup share"))
            self.assertEqual(config.container, "test-pihole")
            self.assertFalse(marker.exists())

    def test_env_rejects_missing_duplicate_and_unknown_settings(self):
        template = Path(__file__).parents[1] / ".env.example"
        text = template.read_text()
        for invalid in ("", text + "\nBACKUP_VOLUME=duplicate\n",
                        text + "\nBACKUP_TYPO=invalid\n",
                        text.replace("BACKUP_STAGING=/var/lib/pihole-backup", "BACKUP_STAGING=")):
            with tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / ".env"
                path.write_text(invalid)
                with self.assertRaises(ValueError):
                    backup.load_env(path)

    def test_installed_snapshot_contains_no_unrelated_env_or_secrets(self):
        with tempfile.TemporaryDirectory() as temp:
            env = Path(temp) / ".env"
            template = Path(__file__).parents[1] / ".env.example"
            env.write_text(template.read_text() + "\nUNRELATED_SECRET=not-for-installed-config\n")
            config = backup.load_env(env)
            installed = Path(temp) / "installed.json"
            backup.write_config(installed, config)
            self.assertEqual(backup.load_config(installed), config)
            self.assertEqual(installed.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("UNRELATED_SECRET", installed.read_text())
            self.assertNotIn("not-for-installed-config", installed.read_text())

    def test_loads_deployment_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "backup.json"
            settings = {"container": CONTAINER, "volume": "test-volume", "source": SOURCE,
                        "repo": "/srv/test", "destination": "/mnt/test", "staging": "/var/lib/test"}
            path.write_text(json.dumps(settings))
            config = backup.load_config(path)
            self.assertEqual(config.container, CONTAINER)
            self.assertEqual(config.source, SOURCE)
            self.assertEqual(config.repo, Path("/srv/test"))

    def test_rejects_incomplete_or_relative_configuration(self):
        settings = {"container": CONTAINER, "volume": "test-volume", "source": SOURCE,
                    "repo": "/srv/test", "destination": "/mnt/test", "staging": "/var/lib/test"}
        for invalid in ({}, dict(settings, repo="relative/path"), dict(settings, source=""),
                        dict(settings, typo="unexpected")):
            with tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "backup.json"
                path.write_text(json.dumps(invalid))
                with self.assertRaises(ValueError):
                    backup.load_config(path)

    def test_missing_config_fails_before_preflight(self):
        with patch.object(backup.sys, "argv", ["backup", "--check"]), \
                patch.object(backup.os, "geteuid", return_value=0), \
                patch.object(backup, "load_config", side_effect=FileNotFoundError), \
                patch.object(backup, "preflight") as preflight:
            with self.assertRaises(FileNotFoundError):
                backup.main()
            preflight.assert_not_called()

    def test_git_metadata_disables_index_refresh(self):
        with patch.object(backup, "run", return_value="") as run:
            backup.git(Path("/checkout"), "status", "--porcelain")
        self.assertEqual(run.call_args.args,
                         ("git", "--no-optional-locks", "-c", "safe.directory=/checkout",
                          "-C", "/checkout", "status", "--porcelain"))

    def test_check_mode_never_captures_backup(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(backup.sys, "argv", ["backup", "--check"]), \
                patch.object(backup.os, "geteuid", return_value=0), \
                patch.object(backup, "load_config", return_value=argparse.Namespace(staging=Path(temp))), \
                patch.object(backup, "preflight", return_value=({}, Path("/volume"))), \
                patch.object(backup, "check_dns"), \
                patch.object(backup, "backup") as capture:
            backup.main()
            capture.assert_not_called()

    def test_archive_restores_database_and_permissions(self):
        real_run = backup.run

        def command(*args, **kwargs):
            if args[0] == "docker":
                return ""
            return real_run(*args, **kwargs)

        with tempfile.TemporaryDirectory() as temp:
            volume = Path(temp) / "volume"
            volume.mkdir()
            db = volume / "gravity.db"
            with closing(sqlite3.connect(db)) as connection, connection:
                connection.execute("CREATE TABLE test (name TEXT)")
                connection.execute("INSERT INTO test VALUES ('backup fixture')")
            db.chmod(0o640)
            archive = Path(temp) / "data.tar"
            with patch.object(backup, "run", side_effect=command), \
                    patch.object(backup, "inspect", return_value={"State": {"Running": False, "ExitCode": 0}}):
                backup.capture_volume(volume, archive, CONTAINER)
            restored = Path(temp) / "restored"
            restored.mkdir()
            real_run("tar", "-xpf", str(archive), "-C", str(restored))
            with closing(sqlite3.connect(restored / "gravity.db")) as connection:
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(connection.execute("SELECT name FROM test").fetchone()[0], "backup fixture")
            self.assertEqual((restored / "gravity.db").stat().st_mode & 0o777, 0o640)

    def test_forced_kill_refuses_archive_and_restarts(self):
        with patch.object(backup, "run", return_value="") as run, \
                patch.object(backup, "inspect", return_value={"State": {"Running": False, "ExitCode": 137}}):
            with self.assertRaises(RuntimeError):
                backup.capture_volume(Path("/volume"), Path("/archive"), CONTAINER)
            self.assertFalse(any(call.args[0] == "tar" for call in run.call_args_list))
            self.assertEqual(run.call_args.args, ("docker", "start", CONTAINER))

    def test_failed_archive_restarts_container(self):
        def command(*args, **kwargs):
            if args[0] == "tar":
                raise subprocess.CalledProcessError(2, args)
            return ""

        with patch.object(backup, "run", side_effect=command) as run, \
                patch.object(backup, "inspect", return_value={"State": {"Running": False}}):
            with self.assertRaises(subprocess.CalledProcessError):
                backup.capture_volume(Path("/volume"), Path("/archive"), CONTAINER)
            self.assertEqual(run.call_args.args, ("docker", "start", CONTAINER))

    def test_timed_out_stop_still_attempts_start(self):
        def command(*args, **kwargs):
            if args[:2] == ("docker", "stop"):
                raise subprocess.TimeoutExpired(args, 60)
            return ""

        with patch.object(backup, "run", side_effect=command) as run:
            with self.assertRaises(subprocess.TimeoutExpired):
                backup.capture_volume(Path("/volume"), Path("/archive"), CONTAINER)
            self.assertEqual(run.call_args.args, ("docker", "start", CONTAINER))

    def test_rejects_local_directory_or_wrong_export(self):
        for source, kind in (("/dev/sda2", "ext4"), ("systemd-1", "autofs"),
                             ("nas.example.net:/wrong", "nfs4")):
            listing = {"filesystems": [{"source": source, "fstype": kind,
                                        "target": "/mnt/pihole-backups"}]}
            with self.subTest(source=source), \
                    patch.object(backup, "run", side_effect=["", json.dumps(listing)]):
                with self.assertRaises(RuntimeError):
                    backup.check_mount(Path("/mnt/pihole-backups"), SOURCE)

    def test_accepts_expected_nfs_export(self):
        listing = {"filesystems": [{"source": SOURCE, "fstype": "nfs4",
                                    "target": "/mnt/pihole-backups"}]}
        with patch.object(backup, "run", side_effect=["", json.dumps(listing)]):
            backup.check_mount(Path("/mnt/pihole-backups"), SOURCE)

    def test_publish_verifies_then_renames(self):
        with tempfile.TemporaryDirectory() as temp:
            local = Path(temp) / "recovery-set"
            local.mkdir()
            (local / "config.tar.gz").write_bytes(b"test fixture")
            destination = Path(temp) / "nas"
            destination.mkdir()
            with patch.object(backup, "check_mount"):
                final = backup.publish(local, destination, SOURCE)
            self.assertEqual(final, destination / local.name)
            self.assertEqual((final / "config.tar.gz").read_bytes(), b"test fixture")
            self.assertFalse((final.parent / ".recovery-set.partial").exists())

    def test_checksum_failure_never_publishes_completed_set(self):
        with tempfile.TemporaryDirectory() as temp:
            local = Path(temp) / "recovery-set"
            local.mkdir()
            (local / "config.tar.gz").write_bytes(b"test fixture")
            destination = Path(temp) / "nas"
            destination.mkdir()
            with patch.object(backup, "check_mount"), \
                    patch.object(backup, "digest", side_effect=["a", "b"]):
                with self.assertRaises(RuntimeError):
                    backup.publish(local, destination, SOURCE)
            self.assertFalse((destination / "recovery-set").exists())
            self.assertTrue(local.exists())


if __name__ == "__main__":
    unittest.main()

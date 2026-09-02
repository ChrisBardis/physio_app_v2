from __future__ import annotations

import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fysio_launcher
import fysio_paths


class PackagingTests(unittest.TestCase):
    def test_packaged_demo_is_copied_once_without_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            resource = root / "resource" / "physio_new.db"
            resource.parent.mkdir()
            resource.write_bytes(b"packaged-demo")
            app_root = root / "Fysio"
            data_dir = app_root / "data"
            default_db = data_dir / "physio_new.db"
            with mock.patch.multiple(
                fysio_paths,
                FROZEN=True,
                APP_ROOT=app_root,
                DATA_DIR=data_dir,
                ARCHIVE_DIR=app_root / "archive",
                LOG_DIR=app_root / "logs",
                DEFAULT_BACKUP_DIR=app_root / "backups",
                DEFAULT_DB=default_db,
                PACKAGED_DEMO_DB=resource,
            ):
                fysio_paths.prepare_writable_layout()
                self.assertEqual(default_db.read_bytes(), b"packaged-demo")
                default_db.write_bytes(b"user-modified")
                fysio_paths.prepare_writable_layout()
                self.assertEqual(default_db.read_bytes(), b"user-modified")

    def test_preferred_port_falls_back_to_an_available_localhost_port(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            occupied.bind((fysio_launcher.HOST, 0))
            preferred = occupied.getsockname()[1]
            selected = fysio_launcher.available_local_port(preferred)
        self.assertNotEqual(selected, preferred)
        self.assertGreater(selected, 0)

    def test_instance_state_round_trip_and_owned_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            state_path = Path(temp) / "instance.json"
            with mock.patch.object(fysio_launcher, "INSTANCE_STATE_PATH", state_path):
                fysio_launcher.write_instance_state(8765)
                state = fysio_launcher.read_instance_state()
                self.assertEqual(state["port"], 8765)
                self.assertGreater(state["pid"], 0)
                fysio_launcher.remove_owned_instance_state()
                self.assertFalse(state_path.exists())

    def test_second_instance_opens_existing_ready_server(self):
        with (
            mock.patch.object(
                fysio_launcher, "read_instance_state",
                return_value={"pid": 123, "port": 8765},
            ),
            mock.patch.object(fysio_launcher, "application_is_ready", return_value=True),
            mock.patch.object(fysio_launcher.webbrowser, "open", return_value=True) as browser_open,
        ):
            self.assertTrue(fysio_launcher.open_existing_instance(attempts=1, interval=0))
        browser_open.assert_called_once_with("http://127.0.0.1:8765/", new=2)

    def test_build_configuration_is_localhost_only_and_preserves_user_data(self):
        root = Path(__file__).parents[1]
        launcher = (root / "fysio_launcher.py").read_text(encoding="utf-8")
        spec = (root / "fysio.spec").read_text(encoding="utf-8")
        installer = (root / "installer" / "fysio.iss").read_text(encoding="utf-8")
        self.assertIn('HOST = "127.0.0.1"', launcher)
        self.assertNotIn('"0.0.0.0"', launcher)
        self.assertIn('console=False', spec)
        self.assertIn('name="Fysio"', spec)
        self.assertIn("DefaultDirName={localappdata}\\Fysio", installer)
        self.assertIn("PrivilegesRequired=lowest", installer)
        self.assertNotIn("uninsdelete", installer.casefold())
        self.assertNotIn("*.db", installer)


if __name__ == "__main__":
    unittest.main()

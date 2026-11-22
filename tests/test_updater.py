import hashlib
import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from src.desktop import updater
from src.web import app as web_app


def _zip_bytes(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    return buf.getvalue()


class AssetSelectionTests(unittest.TestCase):
    def test_platform_tags(self):
        self.assertEqual(updater.platform_tag("win32", "AMD64"), "windows-x64")
        self.assertEqual(updater.platform_tag("darwin", "arm64"), "macos-apple-silicon")
        self.assertEqual(updater.platform_tag("darwin", "x86_64"), "macos-intel")

    def test_linux_is_refused(self):
        with self.assertRaises(updater.UpdateError):
            updater.platform_tag("linux", "x86_64")

    def test_asset_names(self):
        self.assertEqual(
            updater.asset_names("1.9.0", "macos-intel"),
            ("TwitchDropsMiner-1.9.0-macos-intel.zip", "SHA256SUMS-macos.txt"),
        )
        self.assertEqual(updater.asset_names("1.9.0", "windows-x64")[1], "SHA256SUMS-windows.txt")


class ChecksumTests(unittest.TestCase):
    def test_verify_ok_mismatch_and_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.zip"
            path.write_bytes(b"payload")
            digest = hashlib.sha256(b"payload").hexdigest()
            updater.verify_sha256(path, digest.upper())
            with self.assertRaises(updater.UpdateError):
                updater.verify_sha256(path, "0" * 64)
        sums = f"{digest}  a.zip\n{'1' * 64} *b.zip\n"
        self.assertEqual(updater.expected_sha256(sums, "a.zip"), digest)
        self.assertEqual(updater.expected_sha256(sums, "b.zip"), "1" * 64)
        with self.assertRaises(updater.UpdateError):
            updater.expected_sha256(sums, "c.zip")


class ZipSlipTests(unittest.TestCase):
    def test_traversal_and_absolute_names_are_refused(self):
        for name in ("../evil.txt", "a/../../evil.txt", "/abs.txt"):
            with tempfile.TemporaryDirectory() as tmp:
                archive = zipfile.ZipFile(io.BytesIO(_zip_bytes({name: "x"})))
                with self.assertRaises(updater.UpdateError):
                    updater.safe_members(archive, Path(tmp) / "out")

    def test_normal_archive_extracts(self):
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = Path(tmp) / "u.zip"
            zip_path.write_bytes(_zip_bytes({"TwitchDropsMiner/TwitchDropsMiner.exe": "x"}))
            with patch.object(sys, "platform", "win32"):
                updater.extract(zip_path, Path(tmp) / "out")
            self.assertTrue((Path(tmp) / "out/TwitchDropsMiner/TwitchDropsMiner.exe").exists())


class UrlAllowlistTests(unittest.TestCase):
    def test_allowed(self):
        for url in (
            "https://github.com/SimpliAj/twitchdropsminer/releases/download/v1/a.zip",
            "https://objects.githubusercontent.com/x",
            "https://release-assets.githubusercontent.com/x",
        ):
            self.assertEqual(updater.check_url(url), url)

    def test_refused(self):
        for url in (
            "http://github.com/a.zip",
            "https://evil.example/a.zip",
            "https://github.com.evil.example/a.zip",
            "https://githubusercontent.com@evil.example/a.zip",
            "file:///etc/passwd",
        ):
            with self.assertRaises(updater.UpdateError):
                updater.check_url(url)


class WindowsScriptTests(unittest.TestCase):
    def test_script_waits_swaps_and_restores(self):
        install = Path("C:/Apps/Tom's TDM/TwitchDropsMiner")
        with patch.object(sys, "executable", "C:/Apps/TwitchDropsMiner.exe"):
            script = updater.windows_script(
                4242, install, Path("C:/Apps/.stage/new"), Path("C:/Apps/backup"), Path("C:/Apps/.stage"), ["--no-tray"]
            )
        self.assertIn("Wait-Process -Id 4242", script)
        self.assertIn("Move-Item -LiteralPath $install -Destination $backup", script)
        self.assertIn("Move-Item -LiteralPath $new -Destination $install", script)
        self.assertIn("function Restore", script)
        self.assertIn("'--no-tray'", script)
        self.assertIn("Tom''s TDM", script)  # single quote escaped
        self.assertNotIn("Tom's TDM", script)


class InstallTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        updater.status.update(state="idle", error="")

    def test_refuses_when_not_frozen(self):
        with patch.object(updater, "IS_FROZEN", False), self.assertRaises(updater.UpdateError):
            updater.ensure_ready()

    def test_refuses_unwritable_location(self):
        with (
            patch.object(updater, "IS_FROZEN", True),
            patch.object(sys, "platform", "win32"),
            patch.object(updater, "install_paths", return_value=(Path("/x/app"), Path("/x"))),
            patch.object(updater.os, "access", return_value=False),
            self.assertRaisesRegex(updater.UpdateError, "permission"),
        ):
            updater.ensure_ready()

    async def test_checksum_mismatch_never_installs(self):
        with tempfile.TemporaryDirectory() as tmp:
            zip_bytes = _zip_bytes({"TwitchDropsMiner/a.exe": "x"})

            async def fake_sources(staging):
                path = staging / "u.zip"
                path.write_bytes(zip_bytes)
                return path, "0" * 64

            apply = MagicMock()
            with (
                patch.object(updater, "_fetch_sources", fake_sources),
                patch.object(updater, "_apply_windows", apply),
                patch.object(sys, "platform", "win32"),
            ):
                await updater.run_update(Path(tmp) / "TwitchDropsMiner", Path(tmp))
            apply.assert_not_called()
            self.assertEqual(updater.status["state"], "error")
            self.assertIn("Checksum", updater.status["error"])
            self.assertFalse((Path(tmp) / updater.STAGING_NAME).exists())

    async def test_good_download_is_extracted_and_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            zip_bytes = _zip_bytes({"TwitchDropsMiner/a.exe": "x"})

            async def fake_sources(staging):
                path = staging / "u.zip"
                path.write_bytes(zip_bytes)
                return path, hashlib.sha256(zip_bytes).hexdigest()

            apply = MagicMock()
            with (
                patch.object(updater, "_fetch_sources", fake_sources),
                patch.object(updater, "_apply_windows", apply),
                patch.object(sys, "platform", "win32"),
            ):
                await updater.run_update(Path(tmp) / "TwitchDropsMiner", Path(tmp))
            new = apply.call_args.args[1]
            self.assertTrue((new / "a.exe").exists())

    async def test_macos_swap_keeps_backup_and_restarts(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "TwitchDropsMiner.app"
            bundle.mkdir()
            (bundle / "old").write_text("1")
            new = Path(tmp) / "stage" / "TwitchDropsMiner.app"
            new.mkdir(parents=True)
            (new / "new").write_text("2")
            with patch.object(updater.subprocess, "Popen"), patch.object(updater.runtime, "restart_process") as restart:
                updater._apply_macos(bundle, new, Path(tmp) / "stage")
            restart.assert_called_once()
            self.assertTrue((bundle / "new").exists())
            self.assertTrue((Path(tmp) / "TwitchDropsMiner.app.old" / "old").exists())


class EndpointTests(unittest.IsolatedAsyncioTestCase):
    def _request(self, headers=None, host="127.0.0.1"):
        request = MagicMock()
        request.headers = headers or {}
        request.client.host = host
        return request

    async def test_bot_token_is_refused(self):
        with (
            patch.object(web_app, "IS_FROZEN", True),
            patch.object(web_app, "_is_docker", return_value=False),
            patch.object(updater, "ensure_ready") as ready,
            self.assertRaises(HTTPException) as ctx,
        ):
            await web_app.self_update(self._request({"X-Bot-Token": "t"}))
        self.assertEqual(ctx.exception.status_code, 403)
        ready.assert_not_called()

    async def test_remote_client_without_password_is_refused(self):
        with (
            patch.object(web_app, "IS_FROZEN", True),
            patch.object(web_app, "_is_docker", return_value=False),
            patch.object(web_app, "_get_password", return_value=""),
            self.assertRaises(HTTPException) as ctx,
        ):
            await web_app.self_update(self._request(host="192.168.1.5"))
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_start_returns_quickly_and_runs_in_background(self):
        updater.status.update(state="idle", error="")
        ran = MagicMock()

        async def fake_run(target, parent, before_apply=None):
            ran(target, parent)

        with (
            patch.object(web_app, "IS_FROZEN", True),
            patch.object(web_app, "_is_docker", return_value=False),
            patch.object(web_app, "_get_password", return_value=""),
            patch.object(updater, "ensure_ready", return_value=(Path("a"), Path("."))),
            patch.object(updater, "run_update", fake_run),
        ):
            result = await web_app.self_update(self._request())
            await web_app._update_task[-1]
        self.assertEqual(result, {"success": True, "frozen": True, "updating": True})
        ran.assert_called_once()

    async def test_not_ready_becomes_400(self):
        with (
            patch.object(web_app, "IS_FROZEN", True),
            patch.object(web_app, "_is_docker", return_value=False),
            patch.object(web_app, "_get_password", return_value=""),
            patch.object(updater, "ensure_ready", side_effect=updater.UpdateError("no permission")),
            self.assertRaises(HTTPException) as ctx,
        ):
            await web_app.self_update(self._request())
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail, "no permission")


if __name__ == "__main__":
    unittest.main()

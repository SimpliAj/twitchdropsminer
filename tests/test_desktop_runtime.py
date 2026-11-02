import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.config import paths
from src.desktop import runtime
from src.desktop.instance import ERROR_ALREADY_EXISTS, SingleInstanceGuard
from src.desktop.runtime import DesktopOptions


class DesktopOptionsTests(unittest.TestCase):
    def test_defaults(self):
        options = DesktopOptions.parse([])
        self.assertFalse(options.minimized)
        self.assertFalse(options.no_browser)
        self.assertFalse(options.no_tray)

    def test_unknown_existing_flags_are_ignored(self):
        options = DesktopOptions.parse(["-vv", "--dump", "--minimized"])
        self.assertTrue(options.minimized)

    def test_windows_login_launch_stays_in_the_tray(self):
        with patch.object(runtime, "IS_FROZEN", True), patch.object(sys, "platform", "win32"):
            options = DesktopOptions.parse(["--minimized"])
            self.assertFalse(options.open_browser)
            self.assertTrue(options.use_tray)

    def test_manual_launch_opens_the_dashboard(self):
        with patch.object(runtime, "IS_FROZEN", True), patch.object(sys, "platform", "win32"):
            options = DesktopOptions.parse([])
            self.assertTrue(options.open_browser)

    def test_flags_turn_tray_and_browser_off(self):
        with patch.object(runtime, "IS_FROZEN", True), patch.object(sys, "platform", "win32"):
            options = DesktopOptions.parse(["--no-tray", "--no-browser"])
            self.assertFalse(options.use_tray)
            self.assertFalse(options.open_browser)

    def test_source_and_docker_runs_never_open_browser_or_tray(self):
        with patch.object(runtime, "IS_FROZEN", False):
            options = DesktopOptions.parse([])
            self.assertFalse(options.open_browser)
            self.assertFalse(options.use_tray)


class HostTests(unittest.TestCase):
    def test_desktop_build_is_local_only_by_default(self):
        with patch.object(runtime, "IS_FROZEN", True), patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TDM_HOST", None)
            self.assertEqual(runtime.default_host(), "127.0.0.1")

    def test_docker_and_source_keep_listening_on_all_interfaces(self):
        with patch.object(runtime, "IS_FROZEN", False), patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TDM_HOST", None)
            self.assertEqual(runtime.default_host(), "0.0.0.0")

    def test_env_override_wins(self):
        with patch.object(runtime, "IS_FROZEN", True), patch.dict(os.environ, {"TDM_HOST": "0.0.0.0"}):
            self.assertEqual(runtime.default_host(), "0.0.0.0")


class StdStreamTests(unittest.TestCase):
    def test_missing_streams_are_replaced(self):
        with patch.object(sys, "stdout", None), patch.object(sys, "stderr", None):
            runtime.ensure_std_streams()
            self.assertTrue(hasattr(sys.stdout, "isatty"))  # what uvicorn's formatter calls
            sys.stdout.write("x")
            sys.stderr.write("x")

    def test_existing_streams_are_kept(self):
        before = (sys.stdout, sys.stderr)
        runtime.ensure_std_streams()
        self.assertEqual((sys.stdout, sys.stderr), before)


class DataDirTests(unittest.TestCase):
    def test_source_run_keeps_project_data_dir(self):
        with patch.object(paths, "IS_FROZEN", False):
            self.assertEqual(paths._default_data_dir(), paths.PROJECT_ROOT / "data")

    def test_frozen_build_stores_data_in_appdata(self):
        with tempfile.TemporaryDirectory() as appdata:
            with patch.object(paths, "IS_FROZEN", True), patch.dict(os.environ, {"APPDATA": appdata}):
                self.assertEqual(paths._default_data_dir(), Path(appdata) / "TwitchDropsMiner")


class FakeKernel32:
    def __init__(self, already_exists: bool):
        self.already_exists = already_exists
        self.closed: list[object] = []
        self.created: list[str] = []

    def CreateMutexW(self, _attrs, _owner, name):  # noqa: N802
        self.created.append(name)
        return 1234

    def CloseHandle(self, handle):  # noqa: N802
        self.closed.append(handle)


class SingleInstanceTests(unittest.TestCase):
    def _guard(self, already_exists: bool):
        kernel = FakeKernel32(already_exists)
        code = ERROR_ALREADY_EXISTS if already_exists else 0
        return SingleInstanceGuard(8080, kernel32=kernel, last_error=lambda: code), kernel

    def test_first_instance_acquires_and_keeps_the_mutex(self):
        guard, kernel = self._guard(already_exists=False)
        self.assertTrue(guard.acquire())
        self.assertEqual(kernel.closed, [])
        guard.release()
        self.assertEqual(kernel.closed, [1234])

    def test_second_instance_is_refused_and_its_handle_closed(self):
        guard, kernel = self._guard(already_exists=True)
        self.assertFalse(guard.acquire())
        self.assertEqual(kernel.closed, [1234])

    def test_mutex_is_per_port_and_per_user_session(self):
        guard, kernel = self._guard(already_exists=False)
        guard.acquire()
        self.assertEqual(kernel.created, ["Local\\TwitchDropsMiner-8080"])

    def test_noop_off_windows(self):
        with patch.object(sys, "platform", "linux"):
            self.assertTrue(SingleInstanceGuard(8080).acquire())


class RestartTests(unittest.TestCase):
    def tearDown(self):
        runtime._restart_started.clear()
        runtime._restart_hooks.clear()

    def test_windows_restart_spawns_detached_child_that_waits_for_us(self):
        with (
            patch.object(runtime, "IS_FROZEN", True),
            patch.object(sys, "platform", "win32"),
            patch.object(sys, "executable", "C:\\TDM\\TwitchDropsMiner.exe"),
            patch.object(sys, "argv", ["TwitchDropsMiner.exe", "--minimized"]),
            patch.object(runtime.subprocess, "Popen") as popen,
            patch.object(runtime.os, "_exit") as exit_,
            patch.object(runtime.time, "sleep"),
        ):
            runtime.restart_process()
        args = popen.call_args.args[0]
        self.assertEqual(args, ["C:\\TDM\\TwitchDropsMiner.exe", "--minimized"])
        self.assertEqual(popen.call_args.kwargs["env"][runtime.RESTART_PARENT_ENV], str(os.getpid()))
        self.assertEqual(popen.call_args.kwargs["env"][runtime.RESTARTED_ENV], "1")
        self.assertEqual(popen.call_args.kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"], "1")
        flags = popen.call_args.kwargs["creationflags"]
        self.assertTrue(flags & runtime._CREATE_NEW_PROCESS_GROUP)
        self.assertFalse(flags & runtime._DETACHED_PROCESS)  # frozen exe: nothing to detach from
        exit_.assert_called_once_with(0)

    def test_posix_restart_still_uses_execv(self):
        with (
            patch.object(sys, "platform", "linux"),
            patch.object(runtime.os, "execv") as execv,
        ):
            runtime.restart_process()
        execv.assert_called_once()

    def test_wait_for_parent_exit_is_a_noop_without_the_env_var(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(runtime.RESTART_PARENT_ENV, None)
            runtime.wait_for_parent_exit()  # must not raise or block


class TrayTests(unittest.TestCase):
    def test_tray_failure_never_breaks_startup(self):
        from src.desktop.tray import TrayIcon

        tray = TrayIcon(MagicMock(), MagicMock(), MagicMock())
        with patch.dict(sys.modules, {"pystray": None}):  # import raises ImportError
            self.assertFalse(tray.start())
        tray.stop()  # safe even though nothing started


class LoginGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_unarmed_gate_does_not_wait(self):
        from src.desktop.login_gate import LoginGate

        await asyncio.wait_for(LoginGate().wait(), 1)

    async def test_armed_gate_waits_until_released_and_only_once(self):
        from src.desktop.login_gate import LoginGate

        gate = LoginGate()
        gate.arm()
        waiter = asyncio.create_task(gate.wait())
        await asyncio.sleep(0.05)
        self.assertFalse(waiter.done())
        self.assertTrue(gate.waiting)
        gate.release()
        await asyncio.wait_for(waiter, 1)
        await asyncio.wait_for(gate.wait(), 1)  # later attempts are not deferred again

    async def test_release_before_the_prompt_cancels_the_deferral(self):
        from src.desktop.login_gate import LoginGate

        gate = LoginGate()
        gate.arm()
        gate.release()
        await asyncio.wait_for(gate.wait(), 1)


class LoginGatePersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_rearm_does_nothing_without_a_tray(self):
        from src.desktop.login_gate import LoginGate

        gate = LoginGate()
        self.assertFalse(gate.rearm())
        await asyncio.wait_for(gate.wait(), 1)  # source/Docker runs keep retrying as before

    async def test_closed_login_window_waits_for_the_user_again(self):
        from src.desktop.login_gate import LoginGate

        gate = LoginGate()
        gate.enabled = True
        self.assertTrue(gate.rearm())
        waiter = asyncio.create_task(gate.wait())
        await asyncio.sleep(0.05)
        self.assertFalse(waiter.done())  # does not reopen by itself
        gate.release()  # user opened the dashboard from the tray
        await asyncio.wait_for(waiter, 1)


class LoginPendingTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_only_while_a_tray_holds_the_login_back(self):
        from src.desktop.login_gate import LoginGate

        gate = LoginGate()
        gate.arm()
        self.assertFalse(gate.pending)  # no tray: source/Docker never defer
        gate.enabled = True
        self.assertTrue(gate.pending)
        waiter = asyncio.create_task(gate.wait())
        await asyncio.sleep(0.05)
        self.assertTrue(gate.pending)
        gate.release()
        await asyncio.wait_for(waiter, 1)
        self.assertFalse(gate.pending)


class BrowserEnvTests(unittest.TestCase):
    def test_source_runs_keep_the_environment(self):
        from src.auth.plain_chrome import clean_browser_env

        with patch.object(sys, "frozen", False, create=True):
            self.assertEqual(clean_browser_env({"A": "1", "_PYI_X": "2"}), {"A": "1", "_PYI_X": "2"})

    def test_frozen_app_hides_its_bootloader_state_from_browsers(self):
        from src.auth.plain_chrome import clean_browser_env

        bundle = os.path.abspath(os.path.join(tempfile.gettempdir(), "tdm_bundle"))
        env = {
            "_PYI_ARCHIVE_FILE": "x",
            "_MEIPASS2": "y",
            "PYINSTALLER_RESET_ENVIRONMENT": "1",
            "KEEP": "z",
            "PATH": os.pathsep.join([os.path.join(bundle, "lib"), "C:\\Windows\\system32"]),
        }
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "_MEIPASS", bundle, create=True):
            cleaned = clean_browser_env(env)
        self.assertEqual(cleaned["KEEP"], "z")
        self.assertNotIn("_PYI_ARCHIVE_FILE", cleaned)
        self.assertNotIn("_MEIPASS2", cleaned)
        self.assertNotIn("PYINSTALLER_RESET_ENVIRONMENT", cleaned)
        self.assertEqual(cleaned["PATH"], "C:\\Windows\\system32")


class OriginalBehaviourOutsideTheWindowsAppTests(unittest.TestCase):
    """Source and Docker runs must behave exactly as before."""

    def test_windows_source_run_still_restarts_with_execv(self):
        with (
            patch.object(runtime, "IS_FROZEN", False),
            patch.object(sys, "platform", "win32"),
            patch.object(runtime.subprocess, "Popen") as popen,
            patch.object(runtime.os, "execv") as execv,
        ):
            runtime.restart_process()
        execv.assert_called_once()
        popen.assert_not_called()

    def test_source_run_binds_all_interfaces_like_before(self):
        with patch.object(runtime, "IS_FROZEN", False), patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TDM_HOST", None)
            self.assertEqual(runtime.default_host(), "0.0.0.0")

    def test_configured_port_matches_the_original_expression(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TDM_PORT", None)
            os.environ.pop("PORT", None)
            self.assertEqual(runtime.configured_port(), 8080)
        with patch.dict(os.environ, {"PORT": "9000"}):
            os.environ.pop("TDM_PORT", None)
            self.assertEqual(runtime.configured_port(), 9000)
        with patch.dict(os.environ, {"TDM_PORT": "9100", "PORT": "9000"}):
            self.assertEqual(runtime.configured_port(), 9100)


class LauncherTests(unittest.TestCase):
    def _prepare(self, acquired):
        from src.desktop import launcher

        guard = MagicMock()
        guard.acquire.return_value = acquired
        patches = (
            patch.object(launcher, "ensure_std_streams"),
            patch.object(launcher, "wait_for_parent_exit"),
            patch.object(launcher, "SingleInstanceGuard", return_value=guard),
            patch.object(launcher.DesktopOptions, "parse", return_value=DesktopOptions()),
            patch.object(launcher, "port_in_use", return_value=False),
        )
        return launcher, patches

    def test_second_instance_shows_the_running_dashboard_and_exits(self):
        launcher, patches = self._prepare(acquired=False)
        with (
            patches[0], patches[1], patches[2], patches[3],
            patch.object(runtime, "IS_FROZEN", True),
            patch.object(launcher, "open_dashboard") as opener,
            patch.object(launcher.sys, "exit", side_effect=SystemExit) as exit_,
            patch.dict(os.environ, {"TDM_PORT": "8123"}),
        ):
            with self.assertRaises(SystemExit):
                launcher.DesktopLauncher.prepare()
        opener.assert_called_once_with(8123, True)
        exit_.assert_called_once_with(0)

    def test_busy_port_is_reported_instead_of_a_silent_exit(self):
        launcher, patches = self._prepare(acquired=True)
        with (
            patches[0], patches[1], patches[2], patches[3],
            patch.object(launcher, "port_in_use", return_value=True),
            patch.object(launcher, "show_error") as show,
            patch.object(launcher, "note_startup"),
            patch.object(launcher.sys, "exit", side_effect=SystemExit) as exit_,
            patch.dict(os.environ, {"TDM_PORT": "8123"}),
        ):
            with self.assertRaises(SystemExit):
                launcher.DesktopLauncher.prepare()
        self.assertIn("8123", show.call_args.args[0])
        self.assertIn("TDM_PORT", show.call_args.args[0])
        exit_.assert_called_once_with(1)

    def test_first_instance_returns_a_launcher(self):
        launcher, patches = self._prepare(acquired=True)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            result = launcher.DesktopLauncher.prepare()
        self.assertIsInstance(result, launcher.DesktopLauncher)
        result.stop()  # nothing started yet: must be safe


class AppWindowTests(unittest.TestCase):
    def test_app_window_uses_edge_in_app_mode(self):
        with (
            patch.object(runtime, "find_app_browser", return_value="C:\\Edge\\msedge.exe"),
            patch.object(runtime.subprocess, "Popen") as popen,
            patch.object(runtime.webbrowser, "open") as web_open,
        ):
            runtime.open_dashboard(8080, app_window=True)
        call = popen.call_args
        args = call.kwargs.get("args") or call.args[0]
        self.assertEqual(args[:2], ["C:\\Edge\\msedge.exe", "--app=http://127.0.0.1:8080/"])
        self.assertTrue(any(a.startswith("--user-data-dir=") for a in args))  # own profile
        web_open.assert_not_called()

    def test_falls_back_to_default_browser(self):
        with (
            patch.object(runtime, "find_app_browser", return_value=None),
            patch.object(runtime.webbrowser, "open") as web_open,
        ):
            runtime.open_dashboard(8080, app_window=True)
        web_open.assert_called_once_with("http://127.0.0.1:8080/")

    def test_browser_tab_flag(self):
        self.assertTrue(DesktopOptions.parse(["--browser-tab"]).browser_tab)


class DashboardWindowTests(unittest.TestCase):
    def test_second_open_does_not_stack_a_window_until_the_first_is_closed(self):
        proc = MagicMock()
        proc.poll.return_value = None
        window = runtime.DashboardWindow(8080)
        with patch.object(runtime, "open_dashboard", return_value=proc) as opener:
            window.open()
            window.open()  # still open: must not stack a second window
            self.assertEqual(opener.call_count, 1)
            self.assertTrue(window.is_open)
            proc.poll.return_value = 0  # user closed the window
            self.assertFalse(window.is_open)
            window.open()
            self.assertEqual(opener.call_count, 2)

    def test_default_browser_fallback_is_not_tracked(self):
        window = runtime.DashboardWindow(8080)
        with patch.object(runtime, "open_dashboard", return_value=None):
            window.open()
        self.assertFalse(window.is_open)


class QuitAndRestartTests(unittest.TestCase):
    def test_quit_closes_cancels_login_and_forces_exit_if_still_alive(self):
        import threading

        calls = []
        loop = MagicMock()
        loop.call_soon_threadsafe.side_effect = lambda fn: fn()
        exited = threading.Event()
        timer = runtime.request_quit(
            close=lambda: calls.append("close"),
            cancel_login=lambda: calls.append("cancel_login"),
            loop=loop,
            force_after=0.05,
            exit_fn=lambda code: (calls.append(("exit", code)), exited.set()),
        )
        self.assertEqual(calls[:2], ["close", "cancel_login"])
        self.assertTrue(exited.wait(2))
        self.assertEqual(calls[-1], ("exit", 0))
        timer.cancel()

    def test_restart_removes_tray_first_and_only_spawns_once(self):
        runtime._restart_hooks.clear()
        runtime._restart_started.clear()
        order = []
        runtime.register_restart_hook(lambda: order.append("tray stopped"))
        try:
            with (
                patch.object(sys, "platform", "win32"),
                patch.object(runtime, "IS_FROZEN", True),
                patch.object(sys, "executable", "C:\\TDM\\TwitchDropsMiner.exe"),
                patch.object(sys, "argv", ["TwitchDropsMiner.exe"]),
                patch.object(runtime.subprocess, "Popen", side_effect=lambda *a, **k: order.append("spawned")) as popen,
                patch.object(runtime.os, "_exit") as exit_,
                patch.object(runtime.time, "sleep"),
            ):
                runtime.restart_process()
                runtime.restart_process()  # a second request must not start a second copy
            self.assertEqual(order, ["tray stopped", "spawned"])
            self.assertEqual(popen.call_count, 1)
            exit_.assert_called_once_with(0)
        finally:
            runtime._restart_hooks.clear()
            runtime._restart_started.clear()

    def test_failed_spawn_keeps_the_app_running_and_allows_a_retry(self):
        runtime._restart_hooks.clear()
        runtime._restart_started.clear()
        with (
            patch.object(sys, "platform", "win32"),
            patch.object(runtime, "IS_FROZEN", True),
            patch.object(runtime.subprocess, "Popen", side_effect=OSError("boom")),
            patch.object(runtime.os, "_exit") as exit_,
        ):
            with self.assertRaises(OSError):
                runtime.restart_process()
        exit_.assert_not_called()
        self.assertFalse(runtime._restart_started.is_set())


class LoginLoopExitTests(unittest.TestCase):
    def _auth(self, state):
        from src.auth.auth_state import _AuthState

        auth = _AuthState.__new__(_AuthState)
        auth._twitch = MagicMock(_state=state)
        return auth

    def test_quit_state_stops_the_login_retry_loop_in_the_tray_app(self):
        from src.config import State
        from src.desktop.login_gate import login_gate
        from src.exceptions import ExitRequest

        with patch.object(login_gate, "enabled", True):
            with self.assertRaises(ExitRequest):
                self._auth(State.EXIT)._raise_if_exiting()
            self._auth(State.INVENTORY_FETCH)._raise_if_exiting()  # not quitting: keeps going

    def test_source_and_docker_runs_are_not_affected(self):
        from src.config import State
        from src.desktop.login_gate import login_gate

        self.assertFalse(login_gate.enabled)
        self._auth(State.EXIT)._raise_if_exiting()  # no new behaviour outside the tray app


class HiddenIntegrityWindowTests(unittest.TestCase):
    def test_hidden_args_park_the_window_offscreen_and_unthrottled(self):
        from src.auth.plain_chrome import PlainChrome

        args = PlainChrome._HIDDEN_ARGS
        self.assertIn("--window-position=-32000,-32000", args)
        self.assertIn("--start-minimized", args)
        self.assertNotIn("--headless", " ".join(args))  # Twitch flags headless browsers


if __name__ == "__main__":
    unittest.main()

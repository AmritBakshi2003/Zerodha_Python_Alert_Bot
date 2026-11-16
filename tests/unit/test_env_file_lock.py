"""Shared lock around read-modify-write of a ``.env`` file (F26)."""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

from dotenv import dotenv_values

from kite_alerts.cli.login_cmd import write_access_token
from kite_alerts.config.env_file import env_write_lock
from kite_alerts.ui.settings_store import SettingsStore


class TestEnvFileLock:
    def test_concurrent_save_and_token_write_loses_nothing(self, tmp_path: Path) -> None:
        env_path = tmp_path / ".env"
        env_path.write_text("WARMUP_BARS=40\nKITE_ACCESS_TOKEN=\n", encoding="utf-8")

        store = SettingsStore(env_path, min_interval=0.0)
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def saver() -> None:
            try:
                barrier.wait()
                for i in range(50):
                    store.save({"WARMUP_BARS": 40 + i})
            except BaseException as exc:
                errors.append(exc)

        def token_writer() -> None:
            try:
                barrier.wait()
                for i in range(50):
                    write_access_token(env_path, f"tok{i}")
            except BaseException as exc:
                errors.append(exc)

        t1 = threading.Thread(target=saver)
        t2 = threading.Thread(target=token_writer)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert errors == []
        values = dotenv_values(env_path)
        assert "KITE_ACCESS_TOKEN" in values
        assert values["KITE_ACCESS_TOKEN"] == "tok49"
        assert "WARMUP_BARS" in values
        # The file must still parse as a clean env file (no interleaved lines).
        assert all("=" in line or line.strip() == "" for line in env_path.read_text().splitlines())

    def test_cross_process_lock_is_released_on_crash(self, tmp_path: Path) -> None:
        env_path = tmp_path / ".env"
        env_path.write_text("X=1\n", encoding="utf-8")

        script = tmp_path / "hold_and_die.py"
        # Use a platform-agnostic holder that releases on death.
        script_text = (
            "import os, sys, time\n"
            "sys.path.insert(0, {!r})\n"
            "from kite_alerts.config.env_file import env_write_lock\n"
            "from pathlib import Path\n"
            "p = Path({!r})\n"
            "with env_write_lock(p):\n"
            "    print('locked', flush=True)\n"
            "    import os as _os\n"
            "    _os._exit(3)\n"
        ).format(str(Path(__file__).resolve().parents[2] / "src"), str(env_path))
        script.write_text(script_text, encoding="utf-8")

        proc = subprocess.Popen(
            [sys.executable, str(script)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert proc.stdout is not None
        line = proc.stdout.readline().strip()
        assert line == "locked"
        proc.wait(timeout=10)
        assert proc.returncode == 3

        # The OS must have released the advisory lock on process death.
        acquired = {"ok": False}

        def worker() -> None:
            try:
                with env_write_lock(env_path, timeout=5.0):
                    acquired["ok"] = True
            except Exception as exc:
                acquired["exc"] = exc

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=10)
        assert not t.is_alive()
        assert acquired.get("ok") is True, acquired

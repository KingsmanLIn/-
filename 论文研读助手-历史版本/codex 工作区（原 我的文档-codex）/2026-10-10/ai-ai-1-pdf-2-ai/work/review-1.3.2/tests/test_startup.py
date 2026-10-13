"""Phase 11 checks: start-up must never dead-end (review of the "软件无法打开" report).

Reproduces the reported failure and verifies every start-up path:
1. a second server on the same library reports "already open" instead of crashing;
2. the launcher starts the service and exits 0 when nothing is running;
3. the launcher reuses this folder's own service (idempotent double click);
4. with another copy holding the port, the launcher says so and exits 0;
5. a broken data folder produces a readable dialog instead of a traceback dump.

    python -m unittest discover -s tests -p "test_startup.py"
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from baseline_helpers import PROJECT_ROOT, FIXTURE_LIBRARY_V1, free_port

SHIPPED = ["pdf_worker.py", "server.py", "launch.py", "schema.py", "storage.py", "pdftext.py", "textclean.py",
           "chunker.py", "extraction.py", "providers.py", "analysis.py", "reading_card.py", "credentials.py", "secret_guard.py", "endpoint_policy.py", "secure_transport.py", "session.py", "launcher_pipe.py", "job_policy.py", "backup_guard.py", "extract_subprocess.py", "backup_guard.py", "extract_subprocess.py", "job_policy.py", "backup_guard.py", "extract_subprocess.py", "backup_guard.py", "extract_subprocess.py", "session.py", "launcher_pipe.py", "job_policy.py", "backup_guard.py", "extract_subprocess.py", "backup_guard.py", "extract_subprocess.py", "job_policy.py", "backup_guard.py", "extract_subprocess.py", "backup_guard.py", "extract_subprocess.py",
           "quotes.py", "credentials.py", "secret_guard.py", "endpoint_policy.py", "secure_transport.py", "session.py", "launcher_pipe.py", "job_policy.py", "backup_guard.py", "extract_subprocess.py", "backup_guard.py", "extract_subprocess.py", "job_policy.py", "backup_guard.py", "extract_subprocess.py", "backup_guard.py", "extract_subprocess.py", "session.py", "launcher_pipe.py", "job_policy.py", "backup_guard.py", "extract_subprocess.py", "backup_guard.py", "extract_subprocess.py", "job_policy.py", "backup_guard.py", "extract_subprocess.py", "backup_guard.py", "extract_subprocess.py"]
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def port_open(port):
    with __import__("socket").socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def health(port, timeout=2):
    from urllib.request import urlopen
    try:
        with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return None


class StartupPaths(unittest.TestCase):
    def setUp(self):
        # Every test gets its own folder: the instance lock and the recorded port are
        # per-folder state, and sharing them across tests made earlier runs interfere.
        self.root = Path(tempfile.mkdtemp(prefix="startup-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        # a private port range: other tests' sandboxes must be invisible to this launcher
        self.port = free_port()
        self.env = dict(os.environ, PAPER_READER_NO_BROWSER="1", PYTHONIOENCODING="utf-8",
                        PAPER_READER_NO_DIALOG="1", PAPER_READER_PORT=str(self.port))
        self.copy = self.make_app("app")
        self.processes = []
        self.addCleanup(self.stop_all)

    def make_app(self, name):
        folder = self.root / name
        (folder / "data" / "papers").mkdir(parents=True)
        (folder / "run").mkdir(parents=True)
        for item in SHIPPED:
            shutil.copy2(PROJECT_ROOT / item, folder / item)
        for sub in ("static", "vendor"):
            shutil.copytree(PROJECT_ROOT / sub, folder / sub)
        shutil.copy2(FIXTURE_LIBRARY_V1, folder / "data" / "library.json")
        for pdf in (PROJECT_ROOT / "data" / "papers").glob("*.pdf"):
            shutil.copy2(pdf, folder / "data" / "papers" / pdf.name)
        return folder

    def stop_all(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
        # the service is detached from the launcher, so stop it by its recorded pid
        for folder in self.root.iterdir() if self.root.exists() else []:
            try:
                pid = json.loads((folder / "run" / "server.json").read_text())["pid"]
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=15)
            except Exception:
                pass

    def port_of(self, folder, timeout=90):
        """The port the folder's service settled on, once it answers."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                port = json.loads((folder / "run" / "server.json").read_text())["port"]
            except Exception:
                time.sleep(0.3)
                continue
            if health(port):
                return port
            time.sleep(0.3)
        return None

    def spawn(self, args, cwd=None):
        # the child must not inherit a pipe we forget to close: use a file and read it back
        log = self.root / f"spawn-{len(self.processes)}.log"
        handle = log.open("wb")
        self.addCleanup(handle.close)
        process = subprocess.Popen([sys.executable] + args, cwd=str(cwd or self.copy),
                                   env=self.env, stdout=handle, stderr=subprocess.STDOUT,
                                   creationflags=NO_WINDOW)
        process.log_path = log
        self.processes.append(process)
        return process

    def output(self, process):
        process.wait(timeout=90)
        try:
            return process.log_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""

    def wait_for_exit(self, process, timeout=90):
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
            self.fail("process did not exit in time")

    def wait_health(self, port, timeout=90):
        deadline = time.time() + timeout
        while time.time() < deadline:
            payload = health(port)
            if payload:
                return payload
            time.sleep(0.3)
        return None

    # -- 1. two servers, one library ---------------------------------------
    def test_second_server_reports_already_open_without_crashing(self):
        """The reported crash: the second start raised PermissionError on instance.lock."""
        first = self.spawn(["server.py", "--port", str(self.port)])
        port = self.port_of(self.copy)
        self.assertIsNotNone(port, "the first instance must start")
        second = self.spawn(["server.py", "--port", str(self.port)])
        out = self.output(second)
        self.assertNotIn("Traceback", out, "no traceback may reach the user")
        self.assertTrue("already running" in out or "已经有一个" in out, out)
        self.assertIn(f"127.0.0.1:{port}", out, "it must point at the running instance")
        self.assertIn(second.returncode, (0, 3),
                      f"a clean exit, not a crash: {second.returncode}")
        self.assertIsNotNone(health(port), "the first instance keeps working")
        first.terminate()

    # -- 2. launcher starts the service ------------------------------------
    def test_launcher_starts_the_service(self):
        process = self.spawn(["launch.py"])
        port = self.port_of(self.copy, timeout=90)
        self.assertIsNotNone(port, "launch.py must start a service and record its port")
        payload = health(port)
        self.assertEqual("paper-reader", payload["app"])
        self.wait_for_exit(process)
        self.assertEqual(0, process.returncode, "a successful launch exits 0")

    # -- 3. double click again ---------------------------------------------
    def test_launcher_reuses_its_own_service(self):
        first = self.spawn(["launch.py"])
        port = self.port_of(self.copy, timeout=90)
        self.assertIsNotNone(port)
        before = json.loads((self.copy / "run" / "server.json").read_text())
        second = self.spawn(["launch.py"])
        self.wait_for_exit(second)
        self.assertEqual(0, second.returncode, "reuse is not an error")
        after = json.loads((self.copy / "run" / "server.json").read_text())
        self.assertEqual(before["pid"], after["pid"],
                         "a second click must reuse the running service, not start another")
        self.assertEqual(before["port"], after["port"])
        self.assertIsNotNone(health(port))
        first.terminate()

    # -- 4. another copy holds the port ------------------------------------
    def test_launcher_survives_a_taken_port(self):
        other = self.make_app("other")
        port = free_port()
        holder = self.spawn(["server.py", "--port", str(port)], cwd=other)
        self.assertIsNotNone(self.wait_health(port), "the other copy must start")
        # make this folder believe the port is its own; the fingerprints will not match
        (self.copy / "run" / "server.json").write_text(
            json.dumps({"port": port, "pid": 0}), encoding="utf-8")
        launcher = self.spawn(["launch.py"])
        out = self.output(launcher)
        self.assertEqual(0, launcher.returncode, f"it must not dead-end: {out}")
        self.assertTrue("已经有一个" in out or "占用" in out, out)
        self.assertIn(f"127.0.0.1:{port}", out)
        self.assertFalse((self.copy / "run" / "startup.log").exists(),
                         "it must not spawn a second service when the port is held")
        self.assertIsNotNone(health(port), "the running instance is untouched")
        holder.terminate()

    # -- 5. a broken library is reported, not dumped ------------------------
    def test_broken_library_reports_a_sentence(self):
        (self.copy / "data" / "library.json").write_text("{ this is not json", encoding="utf-8")
        process = self.spawn(["server.py", "--port", str(free_port())])
        out = self.output(process)
        self.assertEqual(1, process.returncode, out)
        self.assertIn("启动失败", out, "the user must get a sentence, not just a traceback")
        self.assertIn("startup.log", out)

    # -- the old crash must be gone ----------------------------------------
    def test_no_permission_error_path_remains(self):
        server = (PROJECT_ROOT / "server.py").read_text(encoding="utf-8")
        self.assertIn("def acquire_instance_lock", server)
        self.assertNotIn('.open("a+b")', server,
                         "the exclusive open() is what raised PermissionError on Windows")
        self.assertIn("if instance_lock is None:", server)


if __name__ == "__main__":
    unittest.main(verbosity=2)

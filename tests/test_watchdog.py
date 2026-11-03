import os
import signal
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request


def find_spare_port():
    for p in range(3910, 3990):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise RuntimeError("No free port in 39xx range")


class WatchdogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FACTORY_HOME"] = self.tmp.name
        self.addCleanup(os.environ.pop, "FACTORY_HOME", None)
        self.port = find_spare_port()
        self.repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.addCleanup(self.kill_serve)

    def kill_serve(self):
        subprocess.run(
            ["pkill", "-9", "-f", f"factory serve --port {self.port}$"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def tearDown(self):
        self.kill_serve()
        self.tmp.cleanup()

    def test_watchdog_refuses_when_port_unset_in_dev_checkout(self):
        cmd = f"FACTORY_REPO={self.repo} bash deploy/watchdog.sh"
        env = {k: v for k, v in os.environ.items() if k != "FACTORY_PORT"}
        res = subprocess.run(
            ["bash", "-c", cmd],
            cwd=self.repo,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(res.returncode, 1)
        self.assertIn("refusing to run", res.stderr)
        self.assertEqual(len(res.stderr.strip().splitlines()), 1)

    def test_watchdog_pipe_returns_immediately_and_serve_runs(self):
        # deploy/watchdog.sh | cat must return immediately (< 3s)
        cmd = f"FACTORY_PORT={self.port} FACTORY_REPO={self.repo} FACTORY_HOME={self.tmp.name} bash deploy/watchdog.sh | cat"
        start = time.time()
        res = subprocess.run(
            ["bash", "-c", cmd],
            cwd=self.repo,
            timeout=5,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        elapsed = time.time() - start
        self.assertEqual(res.returncode, 0)
        self.assertLess(elapsed, 3.0)

        # Serve keeps running and answers 401 on the spare port
        url = f"http://127.0.0.1:{self.port}/"
        answered = False
        for _ in range(50):
            try:
                with urllib.request.urlopen(url, timeout=1) as resp:
                    code = resp.status
            except urllib.error.HTTPError as e:
                code = e.code
            except (urllib.error.URLError, ConnectionResetError, OSError):
                time.sleep(0.1)
                continue
            if code == 401:
                answered = True
                break
            time.sleep(0.1)
        self.assertTrue(answered, f"Server on port {self.port} did not answer 401")

        # Started inside the user manager (an agent session), the server must get its own scope
        if f"/user@{os.getuid()}.service/" in open("/proc/self/cgroup").read():
            out = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True).stdout
            pid = next(l.split("pid=")[1].split(",")[0] for l in out.splitlines() if f"127.0.0.1:{self.port} " in l)
            self.assertIn("/factory-serve-", open(f"/proc/{pid}/cgroup").read())


if __name__ == "__main__":
    unittest.main()

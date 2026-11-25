"""Characterize 7za download handling for rusEFI issue #10714.

Run with: python3 firmware/bin/test_prepare_windows_7za.py
The current-bad-behavior assertion records that a transient HTTP 503 aborts
preparation instead of retrying; keep the same server stimulus when fixing it.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest


SCRIPT = Path(__file__).with_name("prepare_windows_7za.sh")
PINNED_SHA256 = "396c180f12d2247c24b0b1fcd482c6fc2a2700f61211ffede1061dd971f5e104"


class TransientServer(ThreadingHTTPServer):
    def __init__(self, address, archive, persistent_failure=False, transient_failure=True):
        super().__init__(address, Handler)
        self.archive = archive
        self.persistent_failure = persistent_failure
        self.transient_failure = transient_failure
        self.requests = 0


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.requests += 1
        if self.server.persistent_failure or (
            self.server.transient_failure and self.server.requests == 1
        ):
            self.send_response(503)
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(self.server.archive)

    def log_message(self, *_args):
        pass


class PrepareWindows7zaTest(unittest.TestCase):
    def make_download_fixture(
        self, root, archive_data, persistent_failure=False, expected_data=None,
        transient_failure=True,
    ):
        if expected_data is None:
            expected_data = archive_data
        tools = root / "tools"
        tools.mkdir()
        server = TransientServer(
            ("127.0.0.1", 0), archive_data,
            persistent_failure=persistent_failure, transient_failure=transient_failure,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)

        real_curl = shutil.which("curl")
        self.assertIsNotNone(real_curl, "curl is required by the 7za preparer")
        curl = tools / "curl"
        curl.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "args = [a for a in sys.argv[1:] if a not in "
            "('--proto', '=https', '--tlsv1.2')]\n"
            f"args[args.index('{SCRIPT_URL_MARKER}')] = "
            f"'http://127.0.0.1:{server.server_port}/archive'\n"
            "args += ['--connect-timeout', '1', '--max-time', '2', "
            "'--retry-delay', '0', '--retry-max-time', '3']\n"
            f"os.execv({real_curl!r}, [{real_curl!r}, *args])\n"
        )
        curl.chmod(0o755)
        checksum = tools / "sha256sum"
        checksum.write_text(
            "#!/usr/bin/env python3\n"
            "import hashlib, sys\n"
            "from pathlib import Path\n"
            "data = Path(sys.argv[1]).read_bytes()\n"
            f"expected = {expected_data!r}\n"
            f"digest = '{PINNED_SHA256}' if data == expected else hashlib.sha256(data).hexdigest()\n"
            "print(f'{digest}  {sys.argv[1]}')\n"
        )
        checksum.chmod(0o755)
        seven_zip = tools / "7z"
        seven_zip.write_text(
            "#!/bin/sh\n"
            "case \"$*\" in\n"
            "  *x64/7za.exe*) printf 'exe fixture\\n' ;;\n"
            "  *License.txt*) printf 'license fixture\\n' ;;\n"
            "  *readme.txt*) printf 'readme fixture\\n' ;;\n"
            "  *) exit 99 ;;\n"
            "esac\n"
        )
        seven_zip.chmod(0o755)
        env = dict(os.environ, PATH=str(tools) + os.pathsep + os.environ["PATH"])
        return server, env

    def test_corrupt_automatic_cache_fails_without_removing_entry(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive_data = b"deterministic archive fixture"
            server, env = self.make_download_fixture(root, archive_data)
            output = root / "console"
            cache = root / "cache"
            output.mkdir()
            cache.mkdir()
            cached = cache / "7z2606-extra.7z"
            cached.write_bytes(b"corrupt cache entry")
            result = subprocess.run(
                ["bash", str(SCRIPT), str(output), "--cache-dir", str(cache)],
                env=env, capture_output=True, text=True, timeout=10,
            )

            self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("SHA-256 mismatch", result.stderr)
            self.assertEqual(0, server.requests, "baseline rejects the cache without redownloading")
            self.assertEqual(b"corrupt cache entry", cached.read_bytes())
            self.assertEqual([cached], list(cache.iterdir()))
            self.assertEqual([], list(output.iterdir()))

    def test_invalid_explicit_archive_fails_without_modifying_it(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "console"
            archive = root / "offline.7z"
            output.mkdir()
            archive.write_bytes(b"invalid offline archive")
            result = subprocess.run(
                ["bash", str(SCRIPT), str(output), "--archive", str(archive)],
                capture_output=True, text=True, timeout=20,
            )

            self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("SHA-256 mismatch", result.stderr)
            self.assertEqual(b"invalid offline archive", archive.read_bytes())
            self.assertEqual([], list(output.iterdir()))

    def test_valid_cache_is_used_without_network(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive_data = b"deterministic archive fixture"
            server, env = self.make_download_fixture(root, archive_data)
            output = root / "console"
            cache = root / "cache"
            output.mkdir()
            cache.mkdir()
            (cache / "7z2606-extra.7z").write_bytes(archive_data)

            result = subprocess.run(
                ["bash", str(SCRIPT), str(output), "--cache-dir", str(cache)],
                env=env, capture_output=True, text=True, timeout=10,
            )

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual(0, server.requests)
            self.assertEqual(b"exe fixture\n", (output / "7za.exe").read_bytes())

    def test_bad_download_checksum_is_not_cached_or_staged(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            valid_data = b"deterministic archive fixture"
            server, env = self.make_download_fixture(
                root, b"corrupt download", expected_data=valid_data,
                transient_failure=False,
            )
            output = root / "console"
            cache = root / "cache"
            output.mkdir()

            result = subprocess.run(
                ["bash", str(SCRIPT), str(output), "--cache-dir", str(cache)],
                env=env, capture_output=True, text=True, timeout=10,
            )

            self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("SHA-256 mismatch", result.stderr)
            self.assertEqual(1, server.requests)
            self.assertEqual([], list(cache.iterdir()), "bad archive must not enter cache")
            self.assertEqual([], list(output.iterdir()), "bad archive must not be extracted")

    def test_persistent_503_currently_stops_without_retries(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive_data = b"deterministic archive fixture"
            server, env = self.make_download_fixture(
                root, archive_data, persistent_failure=True,
            )
            output = root / "console"
            cache = root / "cache"
            output.mkdir()

            result = subprocess.run(
                ["bash", str(SCRIPT), str(output), "--cache-dir", str(cache)],
                env=env, capture_output=True, text=True, timeout=10,
            )

            self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual(1, server.requests, "baseline stops after first 503")
            self.assertEqual([], list(cache.iterdir()))
            self.assertEqual([], list(output.iterdir()))

    def test_transient_503_currently_aborts_download(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive_data = b"deterministic archive fixture"
            server, env = self.make_download_fixture(root, archive_data)
            output = root / "console"
            cache = root / "cache"
            output.mkdir()
            result = subprocess.run(
                ["bash", str(SCRIPT), str(output), "--cache-dir", str(cache)],
                env=env, capture_output=True, text=True, timeout=10,
            )

            self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual(1, server.requests, "baseline makes no retry after HTTP 503")
            self.assertEqual([], list(output.iterdir()), "no files should be staged")
            self.assertEqual([], list(cache.iterdir()), "failed download must not enter cache")


# Replaced by the helper's pinned release URL by the PATH-local curl shim.
SCRIPT_URL_MARKER = (
    "https://github.com/ip7z/7zip/releases/download/26.06/7z2606-extra.7z"
)


if __name__ == "__main__":
    unittest.main()

"""Test JRE download retry and cache handling for rusEFI issue #10714.

Run with: python3 firmware/bin/test_prepare_windows_jre.py
"""

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import urllib.error
import zipfile


SCRIPT = Path(__file__).with_name("prepare_windows_jre.py")
SPEC = importlib.util.spec_from_file_location("prepare_windows_jre", SCRIPT)
JRE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(JRE)


def valid_archive():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in {
            "runtime/bin/java.exe": b"java",
            "runtime/bin/javaw.exe": b"javaw",
            "runtime/bin/server/jvm.dll": b"jvm",
            "runtime/release": (
                'JAVA_VERSION="17.0.1"\nOS_ARCH="x86_64"\nOS_NAME="Windows"\n'
            ),
            "runtime/legal/license.txt": b"license",
        }.items():
            archive.writestr(name, content)
    return output.getvalue()


class PrepareWindowsJreTest(unittest.TestCase):
    def test_retry_classification(self):
        for error, expected in (
            (urllib.error.HTTPError("url", 429, "rate limited", {}, None), True),
            (urllib.error.HTTPError("url", 403, "forbidden", {}, None), False),
            (urllib.error.URLError(TimeoutError("timeout")), True),
            (urllib.error.URLError(PermissionError("not a network failure")), False),
        ):
            with self.subTest(error=error):
                try:
                    self.assertEqual(expected, JRE.retryable_download_error(error))
                finally:
                    if isinstance(error, urllib.error.HTTPError):
                        error.close()

    def test_retry_delay_uses_bounded_backoff_and_retry_after(self):
        for attempt, retry_after, expected in (
            (0, None, 1), (1, None, 2), (2, None, 4),
            (0, "10", 10), (0, "600", 30), (1, "-1", 2),
            (2, "invalid", 4),
        ):
            with self.subTest(attempt=attempt, retry_after=retry_after):
                headers = {} if retry_after is None else {"Retry-After": retry_after}
                error = urllib.error.HTTPError("url", 503, "unavailable", headers, None)
                try:
                    self.assertEqual(expected, JRE.download_retry_delay(error, attempt))
                finally:
                    error.close()

    def make_manifest(self, root, data):
        manifest = root / "manifest.json"
        manifest.write_text(json.dumps({
            "version": "17",
            "url": "https://example.invalid/runtime.zip",
            "sha256": hashlib.sha256(data).hexdigest(),
            "archive": "runtime.zip",
            "runtime_dir": "runtime",
        }))
        return manifest

    def test_valid_cache_is_used_without_network(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = valid_archive()
            manifest = self.make_manifest(root, data)
            cache = root / "cache"
            cache.mkdir()
            (cache / "runtime.zip").write_bytes(data)
            output = root / "runtime-out"

            with mock.patch.object(JRE.urllib.request, "urlopen",
                                   side_effect=AssertionError("cache hit made a request")):
                JRE.prepare(output, cache, None, manifest)

            self.assertTrue((output / "bin/java.exe").is_file())
            self.assertTrue((output / "bin/javaw.exe").is_file())
            self.assertEqual(data, (cache / "runtime.zip").read_bytes())

    def test_bad_download_checksum_is_not_cached_or_staged(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = valid_archive()
            manifest = self.make_manifest(root, data)
            cache = root / "cache"
            output = root / "runtime-out"

            with mock.patch.object(JRE.urllib.request, "urlopen",
                                   return_value=io.BytesIO(b"corrupt archive")):
                with self.assertRaisesRegex(ValueError, "downloaded archive SHA-256 mismatch"):
                    JRE.prepare(output, cache, None, manifest)

            self.assertEqual([], list(cache.iterdir()), "bad archive must not enter cache")
            self.assertFalse(output.exists(), "bad archive must not stage output")

    def test_corrupt_automatic_cache_is_replaced(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = valid_archive()
            manifest = self.make_manifest(root, data)
            cache = root / "cache"
            cache.mkdir()
            cached = cache / "runtime.zip"
            cached.write_bytes(b"corrupt cache")
            output = root / "runtime-out"

            with mock.patch.object(JRE.urllib.request, "urlopen",
                                   return_value=io.BytesIO(data)) as urlopen:
                JRE.prepare(output, cache, None, manifest)

            urlopen.assert_called_once()
            self.assertEqual(data, cached.read_bytes())
            self.assertTrue((output / "bin/java.exe").is_file())

    def test_invalid_explicit_archive_fails_without_modifying_it(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = valid_archive()
            manifest = self.make_manifest(root, data)
            cache = root / "cache"
            archive = root / "offline.zip"
            archive.write_bytes(b"invalid explicit archive")
            output = root / "runtime-out"

            with mock.patch.object(JRE.urllib.request, "urlopen",
                                   side_effect=AssertionError("offline path used network")):
                with self.assertRaisesRegex(ValueError, "archive SHA-256 mismatch"):
                    JRE.prepare(output, cache, archive, manifest)

            self.assertEqual(b"invalid explicit archive", archive.read_bytes())
            self.assertFalse(output.exists())

    def test_http_404_is_not_retried(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = valid_archive()
            manifest = self.make_manifest(root, data)
            cache = root / "cache"
            output = root / "runtime-out"
            calls = []

            def not_found(_request, timeout):
                calls.append(timeout)
                error = urllib.error.HTTPError(
                    "https://example.invalid/runtime.zip", 404, "Not Found", {}, None,
                )
                error.close()
                raise error

            with mock.patch.object(JRE.urllib.request, "urlopen", side_effect=not_found):
                with mock.patch("time.sleep"):
                    with self.assertRaises(urllib.error.HTTPError):
                        JRE.prepare(output, cache, None, manifest)

            self.assertEqual([30], calls)
            self.assertEqual([], list(cache.iterdir()))
            self.assertFalse(output.exists())

    def test_disk_write_failure_is_not_retried(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = valid_archive()
            manifest = self.make_manifest(root, data)
            cache = root / "cache"
            output = root / "runtime-out"
            calls = []
            original_open = Path.open

            def fail_partial_write(path, mode="r", *args, **kwargs):
                if path.parent == cache and mode == "wb":
                    raise PermissionError("fixture disk write failure")
                return original_open(path, mode, *args, **kwargs)

            def response(_request, timeout):
                calls.append(timeout)
                return io.BytesIO(data)

            with mock.patch.object(JRE.urllib.request, "urlopen", side_effect=response):
                with mock.patch.object(Path, "open", fail_partial_write):
                    with mock.patch("time.sleep"):
                        with self.assertRaisesRegex(PermissionError, "fixture disk write"):
                            JRE.prepare(output, cache, None, manifest)

            self.assertEqual([30], calls)
            self.assertEqual([], list(cache.iterdir()), "failed partial must be removed")
            self.assertFalse(output.exists())

    def test_transient_503_is_retried_and_archive_staged(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = valid_archive()
            manifest = self.make_manifest(root, data)
            output = root / "runtime-out"
            cache = root / "cache"
            calls = []
            success = io.BytesIO(data)

            def transient_then_success(_request, timeout):
                calls.append(timeout)
                if len(calls) == 1:
                    error = urllib.error.HTTPError(
                        "https://example.invalid/runtime.zip", 503,
                        "Service Unavailable", {}, None,
                    )
                    error.close()
                    raise error
                return success

            with mock.patch.object(JRE.urllib.request, "urlopen",
                                   side_effect=transient_then_success):
                with mock.patch("time.sleep"):
                    JRE.prepare(output, cache, None, manifest)

            self.assertEqual([30, 30], calls)
            self.assertEqual(data, (cache / "runtime.zip").read_bytes())
            self.assertTrue((output / "bin/java.exe").is_file())

    def test_interrupted_read_is_retried_and_partial_replaced(self):
        class InterruptedResponse(io.BytesIO):
            def __init__(self, data):
                super().__init__(data)
                self.interrupted = False

            def read(self, size=-1):
                if not self.interrupted:
                    self.interrupted = True
                    return super().read(min(size, 128))
                raise ConnectionResetError("connection interrupted during read")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = valid_archive()
            manifest = self.make_manifest(root, data)
            cache = root / "cache"
            output = root / "runtime-out"
            calls = []

            def interrupted_then_valid(_request, timeout):
                calls.append(timeout)
                if len(calls) == 1:
                    return InterruptedResponse(data)
                return io.BytesIO(data)

            with mock.patch.object(JRE.urllib.request, "urlopen",
                                   side_effect=interrupted_then_valid):
                with mock.patch("time.sleep"):
                    JRE.prepare(output, cache, None, manifest)

            self.assertEqual([30, 30], calls)
            self.assertEqual(data, (cache / "runtime.zip").read_bytes())
            self.assertTrue((output / "bin/java.exe").is_file())

    def test_repeated_503s_fail_after_bounded_retries(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = valid_archive()
            manifest = self.make_manifest(root, data)
            cache = root / "cache"
            output = root / "runtime-out"
            calls = []

            def unavailable(_request, timeout):
                calls.append(timeout)
                error = urllib.error.HTTPError(
                    "https://example.invalid/runtime.zip", 503,
                    "Service Unavailable", {}, None,
                )
                error.close()
                raise error

            with mock.patch.object(JRE.urllib.request, "urlopen", side_effect=unavailable):
                with mock.patch("time.sleep"):
                    with self.assertRaises(urllib.error.HTTPError):
                        JRE.prepare(output, cache, None, manifest)

            self.assertEqual([30] * 4, calls)
            self.assertEqual([], list(cache.iterdir()))
            self.assertFalse(output.exists())

    def test_premature_eof_is_retried(self):
        class PrematureEOF(io.BytesIO):
            def __init__(self, data):
                super().__init__(data[:-1])
                self.headers = {"Content-Length": str(len(data))}

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = valid_archive()
            manifest = self.make_manifest(root, data)
            cache = root / "cache"
            output = root / "runtime-out"
            calls = []

            def truncated_then_valid(_request, timeout):
                calls.append(timeout)
                return PrematureEOF(data) if len(calls) == 1 else io.BytesIO(data)

            with mock.patch.object(JRE.urllib.request, "urlopen",
                                   side_effect=truncated_then_valid):
                with mock.patch("time.sleep"):
                    JRE.prepare(output, cache, None, manifest)

            self.assertEqual([30, 30], calls)
            self.assertEqual(data, (cache / "runtime.zip").read_bytes())
            self.assertTrue((output / "bin/java.exe").is_file())


if __name__ == "__main__":
    unittest.main()

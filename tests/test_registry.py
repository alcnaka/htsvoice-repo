import hashlib
import io
import json
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import registry


FAKE_HTS = b"""[GLOBAL]\nHTS_VOICE_VERSION:1.0\nSAMPLING_FREQUENCY:48000\nFRAME_PERIOD:240\nNUM_STATES:5\nNUM_STREAMS:2\nSTREAM_TYPE:MCP,LF0\n[DATA]\n""" + b"binary"


class RegistryTests(unittest.TestCase):
    def test_inspect_htsvoice(self):
        m = registry.inspect_htsvoice(FAKE_HTS)
        self.assertEqual(m["sampling_frequency"], 48000)
        self.assertEqual(m["frame_period"], 240)
        self.assertEqual(m["num_states"], 5)

    def test_zip_member(self):
        b = io.BytesIO()
        with zipfile.ZipFile(b, "w") as z:
            z.writestr("root/voice.htsvoice", FAKE_HTS)
        out = registry.archive_member(b.getvalue(), "zip", "voice.htsvoice", 1)
        self.assertEqual(out, FAKE_HTS)

    def test_targz_member(self):
        b = io.BytesIO()
        with tarfile.open(fileobj=b, mode="w:gz") as t:
            info = tarfile.TarInfo("root/voice.htsvoice")
            info.size = len(FAKE_HTS)
            t.addfile(info, io.BytesIO(FAKE_HTS))
        out = registry.archive_member(b.getvalue(), "tar.gz", "voice.htsvoice", 1)
        self.assertEqual(out, FAKE_HTS)

    def test_content_addressed_key(self):
        h = hashlib.sha256(b"x").hexdigest()
        self.assertEqual(registry.blob_key(h, "htsvoice"), f"v1/blobs/sha256/{h[:2]}/{h}.htsvoice")

    def test_schema_files_are_valid_json(self):
        for name in ("voice.schema.json", "registry.schema.json"):
            data = json.loads((ROOT / "schema" / name).read_text(encoding="utf-8"))
            self.assertEqual(data["$schema"], "https://json-schema.org/draft/2020-12/schema")

    def test_expected_sha256_is_validated(self):
        voice = {
            "id": "test",
            "releases": [],
        }
        rel = {
            "id": "1+r1",
            "source": {
                "url": "https://example.invalid/source.zip",
                "expected_sha256": "0" * 64,
                "kind": "zip",
            },
            "variants": [],
        }
        old_download = registry.download
        try:
            registry.download = lambda url, use_cache=True: b"not-the-expected-content"
            with self.assertRaises(registry.RegistryError):
                registry.fetch_release(voice, rel)
        finally:
            registry.download = old_download


if __name__ == "__main__":
    unittest.main()

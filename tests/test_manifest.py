import unittest

from xlit.common.manifest import Manifest, ManifestError, validate_relpath

SHA = "a" * 64


def _manifest_dict(**over):
    d = {
        "schema": 1,
        "lang": "hi",
        "version": "hi-1.0.0",
        "created_at": "2026-11-02T00:00:00Z",
        "engine": {"beam_width": 4},
        "packages": {"base": {"files": [{"path": "model/transformer/indicxlit.pt", "size": 10, "sha256": SHA}]}},
    }
    d.update(over)
    return d


class ManifestTests(unittest.TestCase):
    def test_roundtrip(self):
        m = Manifest.from_dict(_manifest_dict())
        self.assertEqual(Manifest.from_dict(m.to_dict()), m)
        self.assertFalse(m.has_rescore)
        self.assertIsNotNone(m.entry("model/transformer/indicxlit.pt"))

    def test_rejects_unsafe_paths(self):
        for bad in ["../etc/passwd", "/abs", "a/../b", "a\\b", "", "./x"]:
            with self.subTest(bad=bad), self.assertRaises(ManifestError):
                validate_relpath(bad)

    def test_rejects_bad_version_lang_schema_and_missing_base(self):
        with self.assertRaises(ManifestError):
            Manifest.from_dict(_manifest_dict(version="../x"))
        with self.assertRaises(ManifestError):
            Manifest.from_dict(_manifest_dict(lang="HINDI"))
        with self.assertRaises(ManifestError):
            Manifest.from_dict(_manifest_dict(schema=99))
        with self.assertRaises(ManifestError):
            Manifest.from_dict(_manifest_dict(packages={"rescore": {"files": [{"path": "x", "size": 1, "sha256": SHA}]}}))

    def test_rejects_bad_sha(self):
        d = _manifest_dict()
        d["packages"]["base"]["files"][0]["sha256"] = "nothex"
        with self.assertRaises(ManifestError):
            Manifest.from_dict(d)


if __name__ == "__main__":
    unittest.main()

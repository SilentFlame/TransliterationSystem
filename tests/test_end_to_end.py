"""Central registry <-> local service over real HTTP, with a fake engine."""

import os
import tempfile
import unittest
from pathlib import Path

from tests.helpers import ServerThread, fake_loader, make_fake_source
from xlit.central.publish import collect_packages
from xlit.central.registry import ModelRegistry, RegistryError
from xlit.central.server import create_app as create_central
from xlit.local.central_client import CentralClient, CentralUnavailable, IntegrityError
from xlit.local.config import LocalConfig
from xlit.local.engine_manager import EngineManager
from xlit.local.package_store import PackageStore
from xlit.local.server import create_app as create_local
from xlit.local.sync_agent import SyncAgent


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.src = make_fake_source(self.root / "lib")
        self.registry = ModelRegistry(self.root / "registry")
        self.manifest = self.registry.publish("hi", "hi-1.0.0", collect_packages(self.src, "hi", True))

    def tearDown(self):
        self.tmp.cleanup()

    def _agent(self, url, store, manager, rescore="on"):
        cfg = LocalConfig(central_url=url, store_dir=store.root, rescore_mode=rescore)
        return SyncAgent(cfg, store, CentralClient(url, store.client_id(), connect_timeout_s=0.5), manager, loader=fake_loader)

    def test_central_api_contract(self):
        c = create_central(self.registry).test_client()
        r = c.get("/v1/models/hi/latest")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json["version"], "hi-1.0.0")
        self.assertEqual(c.get("/v1/models/hi/latest", headers={"If-None-Match": r.headers["ETag"]}).status_code, 304)

        f = c.get("/v1/models/hi/hi-1.0.0/files/model/transformer/indicxlit.pt", headers={"Range": "bytes=10-19"})
        self.assertEqual(f.status_code, 206)
        self.assertEqual(f.data, b"W" * 10)

        self.assertEqual(c.get("/v1/models/hi/hi-1.0.0/files/manifest.json").status_code, 404)
        self.assertIn(c.get("/v1/models/hi/hi-1.0.0/files/../LATEST").status_code, (400, 404))
        self.assertEqual(c.get("/v1/models/xx/latest").status_code, 404)

    def test_relative_registry_path_serves_files(self):
        cwd = os.getcwd()
        os.chdir(self.root)
        try:
            c = create_central(ModelRegistry(Path("registry"))).test_client()
            self.assertEqual(c.get("/v1/models/hi/hi-1.0.0/files/lang_list.txt").status_code, 200)
        finally:
            os.chdir(cwd)

    def test_versions_are_immutable(self):
        with self.assertRaises(RegistryError):
            self.registry.publish("hi", "hi-1.0.0", collect_packages(self.src, "hi", False))

    def test_bootstrap_rescore_upgrade_and_new_version_reuse(self):
        store = PackageStore(self.root / "local")
        manager = EngineManager(idle_promote_s=60)
        with ServerThread(create_central(self.registry)) as central:
            agent = self._agent(central.url, store, manager)
            agent.reconcile_once()

            # Base served immediately; rescore loaded and staged for the next word boundary.
            self.assertEqual(manager.active.key, ("hi-1.0.0", "base"))
            self.assertEqual(manager.staged.key, ("hi-1.0.0", "rescore"))
            self.assertTrue(store.is_ready(self.manifest, "rescore"))
            manager.word_boundary()
            self.assertEqual(manager.active.key, ("hi-1.0.0", "rescore"))

            # Same bytes republished as a new version: files are hard-linked locally, not re-downloaded.
            self.registry.publish("hi", "hi-1.0.1", collect_packages(self.src, "hi", True))
            agent.reconcile_once()
            self.assertEqual(manager.staged.key, ("hi-1.0.1", "rescore"))
            manager.word_boundary()
            agent.reconcile_once()  # prunes versions that are no longer active/staged/target
            self.assertEqual([m.version for m in store.installed_manifests("hi")], ["hi-1.0.1"])

    def test_offline_boot_serves_from_local_cache(self):
        store = PackageStore(self.root / "local")
        with ServerThread(create_central(self.registry)) as central:
            self._agent(central.url, store, EngineManager()).reconcile_once()

        manager = EngineManager()
        agent = self._agent("http://127.0.0.1:9", store, manager, rescore="off")
        agent._boot_from_local()
        self.assertEqual(manager.active.key, ("hi-1.0.0", "base"))
        with self.assertRaises(CentralUnavailable):
            agent.reconcile_once()
        self.assertEqual(agent.central_state, "offline")
        self.assertEqual(manager.suggest("mera", 2).version, "hi-1.0.0")

    def test_resume_and_integrity(self):
        entry = self.manifest.packages["base"].files[-1]
        self.assertEqual(entry.path, "model/transformer/indicxlit.pt")
        with ServerThread(create_central(self.registry)) as central:
            client = CentralClient(central.url, "test")
            dest = self.root / "dl" / "indicxlit.pt"
            dest.parent.mkdir()

            # Simulate an interrupted download: half the bytes already in .part.
            (dest.parent / "indicxlit.pt.part").write_bytes(b"W" * (entry.size // 2))
            client.download_file("hi", "hi-1.0.0", entry, dest)
            self.assertEqual(dest.read_bytes(), b"W" * entry.size)

            # Corrupted partial -> rejected and cleaned up.
            dest.unlink()
            (dest.parent / "indicxlit.pt.part").write_bytes(b"X" * 100)
            with self.assertRaises(IntegrityError):
                client.download_file("hi", "hi-1.0.0", entry, dest)
            self.assertFalse((dest.parent / "indicxlit.pt.part").exists())

    def test_local_api(self):
        manager = EngineManager()
        store = PackageStore(self.root / "local")
        with ServerThread(create_central(self.registry)) as central:
            self._agent(central.url, store, manager, rescore="off").reconcile_once()
        app = create_local(LocalConfig(), manager, None).test_client()

        r = app.get("/v1/suggest?q=Mera&k=3&word_start=1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json["candidates"], ["hi-1.0.0/base:mera:0", "hi-1.0.0/base:mera:1", "hi-1.0.0/base:mera:2"])
        for bad in ["", "mera1", "x" * 100, "मेरा", "a b"]:
            self.assertEqual(app.get("/v1/suggest", query_string={"q": bad}).status_code, 400, bad)
        self.assertEqual(app.post("/v1/events", json={"type": "commit", "roman": "mera", "chosen": "मेरा", "rank": 0}).status_code, 200)
        self.assertEqual(app.post("/v1/events", json={"type": "nope"}).status_code, 400)
        self.assertEqual(app.get("/").status_code, 200)

    def test_local_api_not_ready(self):
        app = create_local(LocalConfig(), EngineManager(), None).test_client()
        self.assertEqual(app.get("/v1/suggest?q=mera").status_code, 503)
        self.assertEqual(app.get("/healthz").status_code, 503)


if __name__ == "__main__":
    unittest.main()

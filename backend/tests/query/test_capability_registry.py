import shutil
import tempfile
import unittest
from pathlib import Path

from app.query.capability_registry import CapabilityConfigError, CapabilityRegistry


CAPABILITY_DIR = Path(__file__).resolve().parents[3] / "resources" / "capabilities"


class CapabilityRegistryTests(unittest.TestCase):
    def test_registry_loads_all_capability_files(self):
        registry = CapabilityRegistry.load(config_dir=CAPABILITY_DIR)

        self.assertIn("disease", registry.snapshot.subjects)
        self.assertIn("count", registry.snapshot.metrics["disease"])
        self.assertIn("crack", registry.snapshot.entity_families)
        self.assertTrue(registry.snapshot.version)
        self.assertTrue(registry.snapshot.config_hash)

    def test_registry_rejects_unknown_config_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "capabilities"
            shutil.copytree(CAPABILITY_DIR, config_dir)
            subjects = config_dir / "subjects.yaml"
            subjects.write_text(
                subjects.read_text(encoding="utf-8")
                + "\nunexpected_key: true\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(CapabilityConfigError, "unknown_key"):
                CapabilityRegistry.load(config_dir=config_dir)


if __name__ == "__main__":
    unittest.main()

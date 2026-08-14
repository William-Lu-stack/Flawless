import io
import tempfile
import textwrap
import unittest
import zipfile

from backend.app.services.harness_packages import HarnessPackageManager
from backend.app.services.harness_plugins import HarnessPluginRuntime
from backend.app.services.harness_scaffold import build_plugin_project


class HarnessScaffoldTests(unittest.TestCase):
    def test_scaffold_exports_a_complete_team_plugin_project(self):
        source = textwrap.dedent("""
            apiVersion: cisre.io/v1alpha1
            kind: HarnessPlugin
            metadata:
              name: team.database-observer
              version: 1.2.0
              description: Read-only database evidence provider
            spec:
              category: domain
              domains: [database]
              agents: [database]
              provides: [inventory.database, evidence.database, verification.database]
              requires: [session.events]
              events: [inventory/discovered]
              permissions: [service:provide, inventory:read, evidence:read, ops:propose]
              runtime: {type: declarative}
        """).strip()
        with tempfile.TemporaryDirectory() as directory:
            manager = HarnessPackageManager(HarnessPluginRuntime(runtime_id="scaffold-test"), directory)
            package = manager.validate_source(source)
            filename, payload = build_plugin_project(source, package)

        self.assertEqual(filename, "team.database-observer-1.2.0.zip")
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = set(archive.namelist())
            root = "team.database-observer/"
            for required in (
                "manifest.yaml",
                "README.md",
                "provider/app.py",
                "provider/requirements.txt",
                "provider/Dockerfile",
                "deploy/deployment.yaml",
                "skills/database-operations/SKILL.md",
                "tests/test_provider_contract.py",
                "docs/SECURITY_BOUNDARY.md",
            ):
                self.assertIn(root + required, names)
            provider = archive.read(root + "provider/app.py").decode("utf-8")
            self.assertIn("read_only", provider)
            self.assertNotIn("subprocess", provider)
            security = archive.read(root + "docs/SECURITY_BOUNDARY.md").decode("utf-8")
            self.assertIn("人工审批", security)


if __name__ == "__main__":
    unittest.main()

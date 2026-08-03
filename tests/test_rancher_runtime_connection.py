import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from backend.app import main as server
from backend.app.schemas.operations import RancherConnectionUpsertRequest
from backend.app.services.cluster_registry import ClusterRegistry


class RancherRuntimeConnectionTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _credential(label: str) -> str:
        return "-".join(("fixture", label, "credential"))

    async def test_environment_connection_remains_default_without_runtime_override(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ClusterRegistry(Path(directory) / "clusters.db")
            environment_url = "https://" + "rancher.environment.example"
            environment_token = self._credential("environment")
            with patch.object(server, "CLUSTER_REGISTRY", registry), patch.dict(
                os.environ,
                {
                    "RANCHER_URL": environment_url,
                    "RANCHER_TOKEN": environment_token,
                    "RANCHER_VERIFY_SSL": "false",
                },
                clear=False,
            ):
                self.assertEqual(server._rancher_base(), environment_url)
                self.assertEqual(server._rancher_token(), environment_token)
                self.assertFalse(server._rancher_verify_ssl())
                metadata = await server.rancher_connection_status()
            self.assertEqual(metadata["source"], "environment")
            self.assertTrue(metadata["configured"])
            self.assertFalse(metadata["editable"])
            self.assertNotIn("bearer_token", metadata)

    async def test_verified_runtime_connection_overrides_and_delete_restores_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ClusterRegistry(Path(directory) / "clusters.db")
            environment_url = "https://" + "rancher.environment.example"
            runtime_url = "https://" + "rancher.runtime.example"
            environment_token = self._credential("environment")
            runtime_token = self._credential("runtime")
            request = RancherConnectionUpsertRequest(
                rancher_url=runtime_url,
                bearer_token=runtime_token,
                verify_ssl=True,
            )
            with patch.object(server, "CLUSTER_REGISTRY", registry), patch.dict(
                os.environ,
                {"RANCHER_URL": environment_url, "RANCHER_TOKEN": environment_token},
                clear=False,
            ), patch.object(
                server,
                "_probe_rancher_connection",
                AsyncMock(return_value={
                    "status": "connected",
                    "cluster_count": 4,
                    "last_error": "",
                    "last_checked_at": "now",
                }),
            ):
                saved = await server.upsert_rancher_connection(request)
                self.assertEqual(saved["source"], "runtime_encrypted")
                self.assertEqual(server._rancher_base(), runtime_url)
                self.assertEqual(server._rancher_token(), runtime_token)
                self.assertNotIn("bearer_token", saved)
                deleted = await server.delete_rancher_connection_override()
                self.assertEqual(server._rancher_base(), environment_url)
                self.assertEqual(server._rancher_token(), environment_token)
            self.assertEqual(deleted["active_connection"]["source"], "environment")

    async def test_failed_runtime_validation_preserves_current_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ClusterRegistry(Path(directory) / "clusters.db")
            existing_url = "https://" + "rancher.stable.example"
            existing_token = self._credential("stable")
            registry.save_rancher_connection(
                base_url=existing_url,
                bearer_token=existing_token,
                verify_ssl=True,
                cluster_count=2,
            )
            request = RancherConnectionUpsertRequest(
                rancher_url="https://" + "rancher.invalid.example",
                bearer_token=self._credential("invalid"),
                verify_ssl=True,
            )
            with patch.object(server, "CLUSTER_REGISTRY", registry), patch.object(
                server,
                "_probe_rancher_connection",
                AsyncMock(side_effect=RuntimeError("connection refused")),
            ):
                with self.assertRaises(HTTPException) as raised:
                    await server.upsert_rancher_connection(request)
                active = registry.rancher_connection()
            self.assertEqual(raised.exception.status_code, 422)
            self.assertEqual(active["base_url"], existing_url)
            self.assertEqual(active["bearer_token"], existing_token)


if __name__ == "__main__":
    unittest.main()

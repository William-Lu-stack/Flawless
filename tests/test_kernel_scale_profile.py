import os
import unittest
from unittest.mock import patch

from backend.app.kernel import KERNEL_CONTRACT_VERSION, scalability_profile
from backend.app.api.features.harness import build_router


class KernelScaleProfileTests(unittest.TestCase):
    def test_scale_readiness_is_exposed_as_a_stable_api(self):
        paths = {route.path for route in build_router().routes}
        self.assertIn("/api/harness/scalability", paths)

    def test_single_replica_local_profile_is_safe_but_not_distributed_ready(self):
        with patch.dict(os.environ, {
            "CISRE_API_REPLICAS": "1",
            "CISRE_EVENT_BACKEND": "file",
            "CISRE_LEASE_BACKEND": "local",
            "CISRE_JOB_QUEUE_BACKEND": "inline",
            "CISRE_SNAPSHOT_BACKEND": "file",
        }, clear=False):
            profile = scalability_profile()
        self.assertEqual(profile["contract_version"], KERNEL_CONTRACT_VERSION)
        self.assertTrue(profile["safe_for_configured_replicas"])
        self.assertFalse(profile["distributed_ready"])
        self.assertEqual(profile["mode"], "single-replica")

    def test_multi_replica_local_profile_fails_closed(self):
        with patch.dict(os.environ, {
            "CISRE_API_REPLICAS": "3",
            "CISRE_EVENT_BACKEND": "file",
            "CISRE_LEASE_BACKEND": "local",
            "CISRE_JOB_QUEUE_BACKEND": "inline",
            "CISRE_SNAPSHOT_BACKEND": "file",
        }, clear=False):
            profile = scalability_profile()
        self.assertFalse(profile["safe_for_configured_replicas"])
        self.assertEqual(len(profile["violations"]), 4)

    def test_distributed_backends_are_scale_ready_without_changing_contract(self):
        with patch.dict(os.environ, {
            "CISRE_API_REPLICAS": "8",
            "CISRE_EVENT_BACKEND": "postgres",
            "CISRE_LEASE_BACKEND": "redis",
            "CISRE_JOB_QUEUE_BACKEND": "kafka",
            "CISRE_SNAPSHOT_BACKEND": "postgres",
        }, clear=False):
            profile = scalability_profile()
        self.assertTrue(profile["safe_for_configured_replicas"])
        self.assertTrue(profile["distributed_ready"])
        self.assertEqual(profile["replicas"], 8)
        self.assertEqual(profile["contract_version"], KERNEL_CONTRACT_VERSION)


if __name__ == "__main__":
    unittest.main()

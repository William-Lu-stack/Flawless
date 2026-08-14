import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timezone

from backend.app import application as server
from backend.app.adapters.contracts import (
    ADAPTER_CONTRACT_VERSION,
    AdapterDescriptor,
    EvidenceBundle,
    InfrastructureResource,
    VerificationResult,
    adapter_contract_payload,
)
from backend.app.adapters.domains import operations_domain_catalog
from backend.app.adapters.registry import InfrastructureAdapterRegistry
from backend.app.adapters import ADAPTER_REGISTRY
from backend.app.api.features.operations import build_router as build_operations_router
from backend.app.services.infrastructure_providers import discover_adapter_resources


class _DatabaseAdapter:
    descriptor = AdapterDescriptor(
        id="test-database",
        domain="database",
        display_name="Test Database Adapter",
        version="1.0.0",
        provider="test",
        supported_products=("testdb",),
    )

    async def discover(self, request):
        return [InfrastructureResource(
            id="db-01",
            domain="database",
            name="db-01",
            provider="test",
            facts={"role": "primary", "region": request.get("region", "test")},
        )]

    async def collect_evidence(self, resource, request):
        return EvidenceBundle(
            resource_id=resource.id,
            domain=resource.domain,
            observed_at=datetime.now(timezone.utc).isoformat(),
            health="degraded",
            signals=[{"type": "replication_lag", "value": 90}],
        )

    async def verify(self, resource, receipt, criteria):
        return VerificationResult(
            resource_id=resource.id,
            recovered=True,
            status="completed",
            checked_at=datetime.now(timezone.utc).isoformat(),
            checks=[{"criterion": item, "passed": True} for item in criteria],
        )


class InfrastructureAdapterContractTests(unittest.TestCase):
    def test_read_only_adapter_lifecycle_and_typed_results(self):
        registry = InfrastructureAdapterRegistry()
        registry.register(_DatabaseAdapter())
        self.assertEqual(registry.list()[0]["contract_version"], ADAPTER_CONTRACT_VERSION)

        resources = asyncio.run(registry.discover("test-database", {"region": "test-1"}))
        self.assertEqual(resources[0]["facts"]["role"], "primary")
        resource = InfrastructureResource(
            id="db-01", domain="database", name="db-01", provider="test",
        )
        evidence = asyncio.run(registry.collect_evidence("test-database", resource, {}))
        self.assertEqual(evidence["health"], "degraded")
        verified = asyncio.run(registry.verify(
            "test-database", resource, {"receipt_id": "change-1"}, ["replication_healthy"],
        ))
        self.assertTrue(verified["recovered"])
        self.assertTrue(registry.unregister("test-database"))

    def test_adapter_contract_rejects_mutation_capability_and_secret_facts(self):
        with self.assertRaisesRegex(ValueError, "unsupported adapter capabilities"):
            AdapterDescriptor(
                id="unsafe", domain="database", display_name="unsafe", version="1",
                capabilities=("discover", "execute"),
            ).validate()
        with self.assertRaisesRegex(ValueError, "secret field"):
            InfrastructureResource(
                id="db-01", domain="database", name="db", provider="test",
                facts={"pass" + "word": "must-" + "not-enter-contract"},
            ).validate()

    def test_domain_catalog_starts_with_kubernetes_and_exposes_team_directories(self):
        payload = operations_domain_catalog({"database": 2, "storage": 1})
        ids = [item["id"] for item in payload["domains"]]
        self.assertEqual(ids[0], "kubernetes")
        self.assertEqual(
            set(ids),
            {"kubernetes", "database", "virtual_machine", "storage", "middleware", "cloud_service"},
        )
        database = next(item for item in payload["domains"] if item["id"] == "database")
        self.assertEqual(database["status"], "configured")
        self.assertEqual(database["adapter_directory"], "backend/app/adapters/database")

    def test_public_contract_and_api_are_machine_readable(self):
        contract = adapter_contract_payload()
        self.assertEqual(contract["contract_version"], ADAPTER_CONTRACT_VERSION)
        self.assertIn("collect_evidence", contract["interfaces"])
        route_paths = {route.path for route in build_operations_router(vars(server)).routes}
        self.assertIn("/api/operations/domains", route_paths)
        self.assertIn("/api/infrastructure/contracts", route_paths)
        domains = asyncio.run(server.operations_domains())
        self.assertEqual(domains["contract_version"], "cisre.operations.domain-catalog/v1")
        contracts = asyncio.run(server.infrastructure_adapter_contracts())
        self.assertEqual(contracts["contract_version"], ADAPTER_CONTRACT_VERSION)

    def test_registered_adapter_is_reachable_through_discovery_service(self):
        adapter = _DatabaseAdapter()
        ADAPTER_REGISTRY.register(adapter, replace=True)
        previous = os.environ.get("INFRASTRUCTURE_INVENTORY_STORE_PATH")
        try:
            with tempfile.TemporaryDirectory() as directory:
                os.environ["INFRASTRUCTURE_INVENTORY_STORE_PATH"] = f"{directory}/inventory.json"
                payload = asyncio.run(discover_adapter_resources(
                    "test-database",
                    resource_types=["database"],
                    regions=["test-1"],
                    account_ref="test-account",
                    persist=True,
                ))
            self.assertEqual(payload["adapter_mode"], "in_process_read_only")
            self.assertEqual(payload["resource_count"], 1)
            self.assertEqual(payload["resources"][0]["type"], "database")
            self.assertEqual(payload["persisted"]["accepted"], 1)
        finally:
            ADAPTER_REGISTRY.unregister("test-database")
            if previous is None:
                os.environ.pop("INFRASTRUCTURE_INVENTORY_STORE_PATH", None)
            else:
                os.environ["INFRASTRUCTURE_INVENTORY_STORE_PATH"] = previous


if __name__ == "__main__":
    unittest.main()

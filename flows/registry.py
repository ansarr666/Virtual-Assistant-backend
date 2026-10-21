"""
Central flow registry: maps tenant IDs to client configurations and discovers client flows.
Ensures strict tenant isolation.
"""
import json
import logging
from pathlib import Path
import threading
from typing import Any

from app.config import PROJECT_ROOT
from flows.loader import CLIENTS_DIR, load_client_flow, FlowValidationError

logger = logging.getLogger(__name__)

TENANT_CONFIG_DIR = PROJECT_ROOT / "config" / "tenants"


class UnknownTenantError(KeyError):
    """Raised when an unrecognized tenant ID is requested."""
    pass


class FlowRegistry:
    """Thread-safe flow registry."""
    def __init__(self):
        self._lock = threading.RLock()
        self._tenant_to_client: dict[str, str] = {}
        self._custom_client_dirs: dict[str, Path] = {}
        self._discovered = False

    def clear(self) -> None:
        with self._lock:
            self._tenant_to_client.clear()
            self._custom_client_dirs.clear()
            self._discovered = False

    def discover_clients(self) -> dict[str, str]:
        """
        Discovers all client flows in flows/clients/ by reading config.json in each directory,
        and cross-references with config/tenants/*.json.
        """
        with self._lock:
            if self._discovered:
                return dict(self._tenant_to_client)

            mapping: dict[str, str] = {}

            # 1. Scan flows/clients/*/config.json
            if CLIENTS_DIR.exists():
                for client_dir in sorted(CLIENTS_DIR.iterdir()):
                    if client_dir.is_dir():
                        cfg_file = client_dir / "config.json"
                        if cfg_file.exists():
                            try:
                                with open(cfg_file, "r", encoding="utf-8") as f:
                                    data = json.load(f)
                                tenant_id = data.get("tenant_id")
                                if tenant_id:
                                    mapping[tenant_id] = client_dir.name
                            except Exception as e:
                                logger.warning("Could not inspect client config in %s: %s", client_dir.name, e)

            # 2. Cross-reference with config/tenants/*.json
            if TENANT_CONFIG_DIR.exists():
                for tenant_file in sorted(TENANT_CONFIG_DIR.glob("*.json")):
                    try:
                        with open(tenant_file, "r", encoding="utf-8") as f:
                            t_data = json.load(f)
                        tenant_id = t_data.get("tenant_id") or tenant_file.stem
                        client_target = t_data.get("client")
                        if not client_target and "flow" in t_data:
                            # e.g. "orionsoft_flow.json" -> matches client "orionsoft" if exists
                            flow_stem = t_data["flow"].replace("_flow.json", "").replace(".json", "")
                            if (CLIENTS_DIR / flow_stem).exists():
                                client_target = flow_stem
                        if tenant_id and client_target and tenant_id not in mapping:
                            mapping[tenant_id] = client_target
                    except Exception as e:
                        logger.warning("Could not inspect tenant config in %s: %s", tenant_file.name, e)

            self._tenant_to_client.update(mapping)
            self._discovered = True
            return dict(self._tenant_to_client)

    def register_client(self, tenant_id: str, client_name: str, client_dir: Path | None = None) -> None:
        """Explicitly registers a tenant to a client flow folder (useful for tests or plugins)."""
        with self._lock:
            self._tenant_to_client[tenant_id] = client_name
            if client_dir:
                self._custom_client_dirs[client_name] = Path(client_dir)

    def get_client_for_tenant(self, tenant_id: str) -> str | None:
        """Returns the client folder name registered for this tenant, or None if not found."""
        if not tenant_id or not isinstance(tenant_id, str):
            return None
        clean_id = tenant_id.strip()
        with self._lock:
            self.discover_clients()
            return self._tenant_to_client.get(clean_id)

    def load_flow_for_tenant(self, tenant_id: str) -> dict[str, Any]:
        """
        Loads the modular flow for the given tenant ID.
        Raises UnknownTenantError if the tenant is not registered or known.
        Ensures strict tenant isolation: never falls back to another tenant's flow.
        """
        client_name = self.get_client_for_tenant(tenant_id)
        if not client_name:
            raise UnknownTenantError(f"No client flow registered for tenant '{tenant_id}'.")

        with self._lock:
            client_dir = self._custom_client_dirs.get(client_name) or (CLIENTS_DIR / client_name)

        if not client_dir.exists():
            raise FileNotFoundError(f"Client flow directory not found for tenant '{tenant_id}': {client_dir}")

        flow = load_client_flow(client_dir)
        # Strict validation: verify loaded flow belongs to this tenant
        loaded_tenant = flow.get("tenant_id")
        if loaded_tenant and loaded_tenant != tenant_id:
            raise FlowValidationError(
                [f"Tenant isolation error: flow for '{loaded_tenant}' cannot be served to '{tenant_id}'."],
                client_name,
            )

        return flow

    def list_registered_tenants(self) -> list[str]:
        """Returns all registered tenant IDs."""
        with self._lock:
            self.discover_clients()
            return sorted(self._tenant_to_client.keys())


# Global singleton instance
registry = FlowRegistry()


def get_client_for_tenant(tenant_id: str) -> str | None:
    return registry.get_client_for_tenant(tenant_id)


def load_flow_for_tenant(tenant_id: str) -> dict[str, Any]:
    return registry.load_flow_for_tenant(tenant_id)


def register_client(tenant_id: str, client_name: str, client_dir: Path | None = None) -> None:
    registry.register_client(tenant_id, client_name, client_dir)


def list_registered_tenants() -> list[str]:
    return registry.list_registered_tenants()


def clear_registry_cache() -> None:
    registry.clear()

import json
import logging

from app.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

TENANT_CONFIG_PATH = PROJECT_ROOT / "config" / "tenants"


def get_tenant(tenant_id: str):
    # Tenant ids map to file names, so reject anything that could escape the directory.
    if not tenant_id or not tenant_id.replace("_", "").replace("-", "").isalnum():
        return None

    file_path = TENANT_CONFIG_PATH / f"{tenant_id}.json"

    if not file_path.exists():
        return None

    with open(file_path, "r", encoding="utf-8") as file:
        return json.load(file)


def list_tenants() -> list[dict]:
    tenants = []
    for path in sorted(TENANT_CONFIG_PATH.glob("*.json")):
        try:
            with open(path, "r", encoding="utf-8") as file:
                tenants.append(json.load(file))
        except (OSError, json.JSONDecodeError) as e:
            logger.error("Could not read tenant config %s: %s", path.name, e)
    return tenants

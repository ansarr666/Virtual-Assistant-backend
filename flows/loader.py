"""
Flow loader: loads, validates, and assembles modular conversation flow components.
"""
import json
import logging
from pathlib import Path
import threading
from typing import Any

from app.config import PROJECT_ROOT
from flows.schemas import FlowPackage

logger = logging.getLogger(__name__)

CLIENTS_DIR = PROJECT_ROOT / "flows" / "clients"
TERMINAL_STEP = "completed"

SUPPORTED_INPUT_TYPES = {"text", "rag"}
SUPPORTED_ACTIONS = {"BOOK_CALL", "HUMAN_HANDOFF"}
SUPPORTED_COLLECTS = {"requirement", "timeline", "booking_slot", "contact_info"}
SUPPORTED_ROUTES = {
    "MAIN_MENU", "SERVICES", "PRICING", "BOOK_CALL", "HUMAN_AGENT",
    "CONTACT_ME", "RAG", "GENERIC_REQUIREMENT", "PORTFOLIO",
}
SERVICE_STEP_FIELDS = ("menu_step", "requirement_step", "pricing_step")
MAX_SHORT_LABEL = 20


class FlowValidationError(ValueError):
    """Raised when flow configuration fails structural or relational validation."""
    def __init__(self, errors: list[str], client_or_flow_id: str = ""):
        self.errors = errors
        self.client_or_flow_id = client_or_flow_id
        msg = f"Flow configuration validation failed for '{client_or_flow_id}':\n" + "\n".join(
            f"  - {e}" for e in errors
        )
        super().__init__(msg)


_MODULAR_CACHE: dict[str, dict[str, Any]] = {}
_MODULAR_LOCK = threading.Lock()


def clear_loader_cache() -> None:
    """Clears the modular flow cache."""
    with _MODULAR_LOCK:
        _MODULAR_CACHE.clear()


def _read_json_file(file_path: Path, context_name: str) -> dict[str, Any]:
    if not file_path.exists():
        raise FileNotFoundError(f"Required configuration file not found for {context_name}: {file_path.name}")
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise FlowValidationError(
                [f"File '{file_path.name}' must contain a valid JSON object, got {type(data).__name__}."],
                context_name,
            )
        return data
    except json.JSONDecodeError as e:
        raise FlowValidationError([f"Invalid JSON in '{file_path.name}': {e}"], context_name) from e


def validate_modular_flow(client_dir: Path | str) -> list[str]:
    """
    Validates all component files in a client directory:
    - flow.json
    - config.json
    - destinations.json
    - templates.json
    - steps.json
    Returns a list of error strings; empty list indicates full validity.
    """
    path = Path(client_dir)
    client_name = path.name
    errors: list[str] = []

    if not path.exists() or not path.is_dir():
        return [f"Client directory does not exist: {path}"]

    # Required files
    flow_file = path / "flow.json"
    config_file = path / "config.json"
    dest_file = path / "destinations.json"
    tmpl_file = path / "templates.json"
    steps_file = path / "steps.json"

    for req in (flow_file, config_file, dest_file, tmpl_file, steps_file):
        if not req.exists():
            errors.append(f"Missing required component file: {req.name}")
        else:
            try:
                _read_json_file(req, client_name)
            except FlowValidationError as e:
                errors.extend(e.errors)

    if errors:
        return errors

    try:
        flow_data = _read_json_file(flow_file, client_name)
    except FlowValidationError as e:
        errors.extend(e.errors)
        flow_data = {}

    try:
        config_data = _read_json_file(config_file, client_name)
    except FlowValidationError as e:
        errors.extend(e.errors)
        config_data = {}

    try:
        dest_data = _read_json_file(dest_file, client_name)
    except FlowValidationError as e:
        errors.extend(e.errors)
        dest_data = {}

    try:
        tmpl_data = _read_json_file(tmpl_file, client_name)
    except FlowValidationError as e:
        errors.extend(e.errors)
        tmpl_data = {}

    try:
        steps_data = _read_json_file(steps_file, client_name)
    except FlowValidationError as e:
        errors.extend(e.errors)
        steps_data = {}

    if errors:
        return errors

    # Tenant and Flow ID validation
    flow_id = flow_data.get("flow_id")
    flow_tenant = flow_data.get("tenant_id")
    cfg_tenant = config_data.get("tenant_id")

    if not flow_id:
        errors.append("flow.json is missing 'flow_id'.")
    if not flow_tenant:
        errors.append("flow.json is missing 'tenant_id'.")
    if not cfg_tenant:
        errors.append("config.json is missing 'tenant_id'.")
    elif flow_tenant and cfg_tenant != flow_tenant:
        errors.append(f"Tenant ID mismatch: flow.json specifies '{flow_tenant}' but config.json specifies '{cfg_tenant}'.")

    # Destinations validation
    destinations = dest_data.get("destinations")
    if not isinstance(destinations, dict) or not destinations:
        errors.append("destinations.json must have a non-empty 'destinations' dictionary.")

    # Steps validation
    steps = steps_data.get("steps")
    if not isinstance(steps, dict) or not steps:
        errors.append("steps.json must have a non-empty 'steps' dictionary.")
        return errors

    start_step = flow_data.get("start_step")
    if not start_step or start_step not in steps:
        errors.append(f"start_step '{start_step}' does not exist in 'steps'.")

    # Routes validation
    routes = flow_data.get("routes", {})
    if not isinstance(routes, dict):
        errors.append("flow.json 'routes' must be an object.")
    else:
        for r_name, r_target in routes.items():
            if r_name not in SUPPORTED_ROUTES:
                errors.append(f"Route '{r_name}' is not supported. Supported: {sorted(SUPPORTED_ROUTES)}.")
            if r_target not in steps:
                errors.append(f"Route '{r_name}' points to non-existent step '{r_target}'.")

    # Global options validation
    global_options = flow_data.get("global_options", {})
    if not isinstance(global_options, dict):
        errors.append("flow.json 'global_options' must be an object.")
    else:
        for opt_label, opt_target in global_options.items():
            if opt_target not in steps:
                errors.append(f"Global option '{opt_label}' points to non-existent step '{opt_target}'.")

    # Short labels validation
    short_labels = flow_data.get("short_labels", {})
    if not isinstance(short_labels, dict):
        errors.append("flow.json 'short_labels' must be an object.")
    else:
        for opt_label, short in short_labels.items():
            if not isinstance(short, str) or not short.strip() or len(short) > MAX_SHORT_LABEL:
                errors.append(f"Short label for '{opt_label}' must be non-empty and at most {MAX_SHORT_LABEL} chars.")

    # Services validation
    services = flow_data.get("services", {})
    if not isinstance(services, dict):
        errors.append("flow.json 'services' must be an object.")
    else:
        for svc_key, svc in services.items():
            if not isinstance(svc, dict) or not svc.get("service"):
                errors.append(f"Service '{svc_key}' must be an object with a 'service' name.")
                continue
            for field in SERVICE_STEP_FIELDS:
                tgt = svc.get(field)
                if tgt is not None and tgt not in steps:
                    errors.append(f"Service '{svc_key}' {field} points to non-existent step '{tgt}'.")

    # Individual step definitions and template references
    for step_id, step in steps.items():
        if not isinstance(step, dict):
            errors.append(f"Step '{step_id}' must be a dictionary.")
            continue

        template_id = step.get("template_id")
        has_reply = isinstance(step.get("reply"), str) and bool(step.get("reply").strip())

        if template_id is not None:
            if template_id not in tmpl_data:
                errors.append(f"Step '{step_id}' references unknown template_id '{template_id}'.")
        elif not has_reply:
            errors.append(f"Step '{step_id}' must define either 'template_id' or 'reply'.")

        next_step = step.get("next_step")
        if next_step is not None and next_step != TERMINAL_STEP and next_step not in steps:
            errors.append(f"Step '{step_id}' next_step points to non-existent step '{next_step}'.")

        input_type = step.get("input_type")
        if input_type is not None and input_type not in SUPPORTED_INPUT_TYPES:
            errors.append(f"Step '{step_id}' has unsupported input_type '{input_type}'.")
        if input_type == "text" and "next_step" not in step:
            errors.append(f"Text step '{step_id}' must define 'next_step'.")

        action = step.get("action")
        if action is not None and action not in SUPPORTED_ACTIONS:
            errors.append(f"Step '{step_id}' has unsupported action '{action}'.")

        collects = step.get("collects")
        if collects is not None and collects not in SUPPORTED_COLLECTS:
            errors.append(f"Step '{step_id}' collects unsupported field '{collects}'.")

        options = step.get("options")
        if options is not None:
            if not isinstance(options, dict) or not options:
                errors.append(f"Step '{step_id}' options must be a non-empty dictionary.")
            else:
                for opt_label, opt_val in options.items():
                    if not opt_label.strip():
                        errors.append(f"Step '{step_id}' option has an empty label.")
                    if not isinstance(opt_val, dict):
                        errors.append(f"Step '{step_id}' option '{opt_label}' must be a dictionary.")
                        continue
                    opt_next = opt_val.get("next_step")
                    if opt_next is not None and opt_next != TERMINAL_STEP and opt_next not in steps:
                        errors.append(f"Step '{step_id}' option '{opt_label}' next_step points to non-existent step '{opt_next}'.")
                    opt_svc_key = opt_val.get("service_key")
                    if opt_svc_key is not None and opt_svc_key not in services:
                        errors.append(f"Step '{step_id}' option '{opt_label}' service_key '{opt_svc_key}' is not in 'services'.")

    return errors


def load_client_flow(client_name_or_path: str | Path) -> dict[str, Any]:
    """
    Loads, validates, and assembles the modular components of a client flow into
    an assembled flow dictionary. Caches valid flows thread-safely.
    """
    if isinstance(client_name_or_path, Path):
        client_dir = client_name_or_path
        cache_key = client_dir.name
    else:
        path_candidate = Path(client_name_or_path)
        if path_candidate.is_dir():
            client_dir = path_candidate
            cache_key = client_dir.name
        else:
            client_dir = CLIENTS_DIR / client_name_or_path
            cache_key = str(client_name_or_path)

    cached = _MODULAR_CACHE.get(cache_key)
    if cached is not None:
        return cached

    with _MODULAR_LOCK:
        if cache_key in _MODULAR_CACHE:
            return _MODULAR_CACHE[cache_key]

        if not client_dir.exists():
            raise FileNotFoundError(f"Client flow directory not found: {client_dir}")

        errors = validate_modular_flow(client_dir)
        if errors:
            logger.error("Modular flow validation failed for '%s': %s", client_dir.name, "; ".join(errors))
            raise FlowValidationError(errors, client_dir.name)

        flow_data = _read_json_file(client_dir / "flow.json", client_dir.name)
        config_data = _read_json_file(client_dir / "config.json", client_dir.name)
        dest_data = _read_json_file(client_dir / "destinations.json", client_dir.name)
        tmpl_data = _read_json_file(client_dir / "templates.json", client_dir.name)
        steps_data = _read_json_file(client_dir / "steps.json", client_dir.name)

        resolved_steps: dict[str, Any] = {}
        for step_id, step in steps_data["steps"].items():
            s_copy = dict(step)
            # Resolve reply text from template if reply is missing or template_id is specified
            template_id = s_copy.get("template_id")
            if template_id and template_id in tmpl_data:
                tmpl_entry = tmpl_data[template_id]
                if isinstance(tmpl_entry, dict):
                    s_copy["reply"] = tmpl_entry.get("text", "")
                elif isinstance(tmpl_entry, str):
                    s_copy["reply"] = tmpl_entry
            elif "reply" not in s_copy and step_id in tmpl_data:
                tmpl_entry = tmpl_data[step_id]
                if isinstance(tmpl_entry, dict):
                    s_copy["reply"] = tmpl_entry.get("text", "")
                elif isinstance(tmpl_entry, str):
                    s_copy["reply"] = tmpl_entry

            resolved_steps[step_id] = s_copy

        package = FlowPackage(
            flow_id=flow_data["flow_id"],
            tenant_id=flow_data["tenant_id"],
            version=flow_data.get("version", "1.0.0"),
            start_step=flow_data["start_step"],
            routes=flow_data.get("routes", {}),
            global_options=flow_data.get("global_options", {}),
            fallback_options=flow_data.get("fallback_options", []),
            short_labels=flow_data.get("short_labels", {}),
            services=flow_data.get("services", {}),
            steps=resolved_steps,
            templates=tmpl_data,
            destinations=dest_data.get("destinations", {}),
            config=config_data,
        )

        flow_dict = package.to_dict()
        _MODULAR_CACHE[cache_key] = flow_dict
        # Also cache by tenant_id
        if package.tenant_id:
            _MODULAR_CACHE[package.tenant_id] = flow_dict

        return flow_dict

import json
import logging
import threading
from typing import Any

from app.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

FLOW_PATH = PROJECT_ROOT / "flows"

# Terminal pseudo-step: a transition to it ends the flow.
TERMINAL_STEP = "completed"

SUPPORTED_INPUT_TYPES = {"text", "rag"}
SUPPORTED_ACTIONS = {"BOOK_CALL", "HUMAN_HANDOFF"}
SUPPORTED_COLLECTS = {"requirement", "timeline", "booking_slot", "contact_info"}
SUPPORTED_ROUTES = {
    "MAIN_MENU", "SERVICES", "PRICING", "BOOK_CALL", "HUMAN_AGENT",
    "CONTACT_ME", "RAG", "GENERIC_REQUIREMENT", "PORTFOLIO",
}
SUPPORTED_SERVICE_CONTEXT = {"menu_step", "requirement_step", "pricing_step"}
SERVICE_STEP_FIELDS = ("menu_step", "requirement_step", "pricing_step")
# Messenger / Instagram quick-reply titles and WhatsApp button titles are limited to 20 chars.
MAX_SHORT_LABEL = 20
STEP_KEYS = {
    "reply", "options", "input_type", "next_step", "action", "collects", "service",
    "template_id", "type", "description",
}
OPTION_KEYS = {
    "next_step", "reply", "intent", "service", "sub_service", "service_key",
    "timeline", "platform", "use_service_context", "aliases", "template_id",
}


class FlowValidationError(ValueError):
    """Raised when conversation flow JSON fails validation."""
    def __init__(self, errors: list[str], flow_id: str = ""):
        self.errors = errors
        self.flow_id = flow_id
        msg = f"Flow validation failed for '{flow_id}':\n" + "\n".join(f"  - {e}" for e in errors)
        super().__init__(msg)


def _step_exists(target: Any, steps: dict[str, Any]) -> bool:
    return isinstance(target, str) and (target == TERMINAL_STEP or target in steps)


def validate_flow(flow: dict[str, Any], filename: str = "") -> list[str]:
    """
    Validates the structure and every step reference of a conversation flow.
    Returns a list of error strings; an empty list means the flow is valid.
    """
    errors: list[str] = []

    if not isinstance(flow, dict):
        return ["Flow definition must be a JSON object."]

    flow_id = flow.get("flow_id", filename)
    start_step = flow.get("start_step")
    steps = flow.get("steps")

    if not flow.get("flow_id"):
        errors.append("Flow is missing 'flow_id'.")

    if not isinstance(steps, dict) or not steps:
        errors.append(f"Flow '{flow_id}' has no 'steps' dictionary defined.")
        return errors

    if not start_step or start_step not in steps:
        errors.append(f"start_step '{start_step}' does not exist in 'steps'.")

    # Flow-level routing tables
    routes = flow.get("routes", {})
    if not isinstance(routes, dict):
        errors.append("'routes' must be an object.")
    else:
        for name, target in routes.items():
            if name not in SUPPORTED_ROUTES:
                errors.append(f"Route '{name}' is not supported. Supported: {sorted(SUPPORTED_ROUTES)}.")
            if not isinstance(target, str) or target not in steps:
                errors.append(f"Route '{name}' points to non-existent step '{target}'.")

    global_options = flow.get("global_options", {})
    if not isinstance(global_options, dict):
        errors.append("'global_options' must be an object.")
    else:
        for label, target in global_options.items():
            if not isinstance(target, str) or target not in steps:
                errors.append(f"Global option '{label}' points to non-existent step '{target}'.")

    fallback_options = flow.get("fallback_options", [])
    start_options = steps.get(start_step, {}).get("options", {}) if isinstance(steps.get(start_step), dict) else {}
    if not isinstance(fallback_options, list):
        errors.append("'fallback_options' must be a list.")
    else:
        for label in fallback_options:
            resolvable = isinstance(global_options, dict) and label in global_options
            if not isinstance(label, str) or not (resolvable or label in start_options):
                errors.append(
                    f"Fallback option '{label}' must be a global option or a start-step option, "
                    "otherwise clicking it cannot be routed."
                )

    short_labels = flow.get("short_labels", {})
    if not isinstance(short_labels, dict):
        errors.append("'short_labels' must be an object of {option label: short display label}.")
    else:
        for label, short in short_labels.items():
            if not isinstance(short, str) or not short.strip() or len(short) > MAX_SHORT_LABEL:
                errors.append(
                    f"Short label for '{label}' must be a non-empty string of at most {MAX_SHORT_LABEL} characters."
                )

    services = flow.get("services", {})
    if not isinstance(services, dict):
        errors.append("'services' must be an object.")
    else:
        for key, svc in services.items():
            if not isinstance(svc, dict) or not svc.get("service"):
                errors.append(f"Service '{key}' must be an object with a 'service' name.")
                continue
            for field in SERVICE_STEP_FIELDS:
                target = svc.get(field)
                if target is not None and target not in steps:
                    errors.append(f"Service '{key}' {field} points to non-existent step '{target}'.")

    for step_name, step_config in steps.items():
        if not isinstance(step_config, dict):
            errors.append(f"Step '{step_name}' configuration must be a dictionary.")
            continue

        unknown = set(step_config) - STEP_KEYS
        if unknown:
            errors.append(f"Step '{step_name}' has unknown keys {sorted(unknown)}.")

        has_reply = isinstance(step_config.get("reply"), str) and bool(step_config.get("reply").strip())
        has_template = isinstance(step_config.get("template_id"), str) and bool(step_config.get("template_id").strip())
        if not has_reply and not has_template:
            errors.append(f"Step '{step_name}' is missing required 'reply' message.")

        if "next_step" in step_config and not _step_exists(step_config["next_step"], steps):
            errors.append(
                f"Step '{step_name}' next_step points to non-existent step '{step_config['next_step']}'."
            )

        input_type = step_config.get("input_type")
        if input_type is not None and input_type not in SUPPORTED_INPUT_TYPES:
            errors.append(
                f"Step '{step_name}' has unsupported input_type '{input_type}'. "
                f"Supported: {sorted(SUPPORTED_INPUT_TYPES)}."
            )
        if input_type == "text" and "next_step" not in step_config:
            errors.append(f"Text step '{step_name}' must define 'next_step' for the captured answer.")

        action = step_config.get("action")
        if action is not None and action not in SUPPORTED_ACTIONS:
            errors.append(
                f"Step '{step_name}' has unrecognized action '{action}'. "
                f"Supported: {sorted(SUPPORTED_ACTIONS)}."
            )

        collects = step_config.get("collects")
        if collects is not None:
            if collects not in SUPPORTED_COLLECTS:
                errors.append(
                    f"Step '{step_name}' collects unsupported field '{collects}'. "
                    f"Supported: {sorted(SUPPORTED_COLLECTS)}."
                )
            if "next_step" not in step_config:
                errors.append(f"Step '{step_name}' collects '{collects}' but has no 'next_step' to continue to.")

        if "options" in step_config:
            options = step_config["options"]
            if not isinstance(options, dict) or not options:
                errors.append(f"Step '{step_name}' 'options' must be a non-empty dictionary.")
                continue
            for opt_name, opt_config in options.items():
                if not opt_name.strip():
                    errors.append(f"Step '{step_name}' has an option with an empty label.")
                if not isinstance(opt_config, dict):
                    errors.append(f"Step '{step_name}' option '{opt_name}' must be a dictionary.")
                    continue

                unknown = set(opt_config) - OPTION_KEYS
                if unknown:
                    errors.append(f"Step '{step_name}' option '{opt_name}' has unknown keys {sorted(unknown)}.")

                if "next_step" not in opt_config and "reply" not in opt_config:
                    errors.append(
                        f"Step '{step_name}' option '{opt_name}' must define 'next_step' or 'reply'."
                    )

                if "next_step" in opt_config and not _step_exists(opt_config["next_step"], steps):
                    errors.append(
                        f"Step '{step_name}' option '{opt_name}' next_step points to "
                        f"non-existent step '{opt_config['next_step']}'."
                    )

                aliases = opt_config.get("aliases")
                if aliases is not None and (
                    not isinstance(aliases, list) or not all(isinstance(x, str) and x.strip() for x in aliases)
                ):
                    errors.append(f"Step '{step_name}' option '{opt_name}' aliases must be a list of non-empty strings.")

                context = opt_config.get("use_service_context")
                if context is not None and context not in SUPPORTED_SERVICE_CONTEXT:
                    errors.append(
                        f"Step '{step_name}' option '{opt_name}' use_service_context '{context}' is not "
                        f"supported. Supported: {sorted(SUPPORTED_SERVICE_CONTEXT)}."
                    )

                service_key = opt_config.get("service_key")
                if service_key is not None and service_key not in services:
                    errors.append(
                        f"Step '{step_name}' option '{opt_name}' service_key '{service_key}' is not in 'services'."
                    )

    return errors


def find_unreachable_steps(flow: dict[str, Any]) -> list[str]:
    """Steps that no option, next_step, route or service entry can reach (warnings only)."""
    steps = flow.get("steps", {})
    reachable = {flow.get("start_step")}
    reachable.update(flow.get("routes", {}).values())
    reachable.update(flow.get("global_options", {}).values())
    for svc in flow.get("services", {}).values():
        reachable.update(svc.get(f) for f in SERVICE_STEP_FIELDS)
    for step in steps.values():
        reachable.add(step.get("next_step"))
        for opt in step.get("options", {}).values():
            reachable.add(opt.get("next_step"))
    return sorted(name for name in steps if name not in reachable and name != TERMINAL_STEP)


_FLOW_CACHE: dict[str, dict[str, Any]] = {}
_FLOW_LOCK = threading.Lock()


def _resolve_client_name(identifier: str) -> str | None:
    """Helper to detect if identifier points to a modular client."""
    clean = identifier.replace("_flow.json", "").replace(".json", "")
    client_dir = FLOW_PATH / "clients" / clean
    if client_dir.is_dir():
        return clean
    if (FLOW_PATH / "clients" / identifier).is_dir():
        return identifier
    return None


def get_flow_for_tenant(tenant_id: str) -> dict[str, Any]:
    """Resolves and loads flow for a specific tenant ID using the centralized registry."""
    from flows.registry import load_flow_for_tenant
    return load_flow_for_tenant(tenant_id)


def load_flow(flow_filename: str) -> dict[str, Any]:
    """
    Loads and validates a flow from the modular client directory or legacy JSON flow file.
    Only valid flows are cached; an invalid flow raises FlowValidationError on every load.
    """
    cached = _FLOW_CACHE.get(flow_filename)
    if cached is not None:
        return cached

    with _FLOW_LOCK:
        if flow_filename in _FLOW_CACHE:
            return _FLOW_CACHE[flow_filename]

        client_name = _resolve_client_name(flow_filename)
        if client_name:
            from flows.loader import load_client_flow
            flow = load_client_flow(FLOW_PATH / "clients" / client_name)
            _FLOW_CACHE[flow_filename] = flow
            return flow

        file_path = FLOW_PATH / flow_filename
        if not file_path.exists():
            raise FileNotFoundError(f"Flow file not found: {file_path}")

        with open(file_path, "r", encoding="utf-8") as file:
            flow = json.load(file)

        errors = validate_flow(flow, flow_filename)
        if errors:
            logger.error("Flow validation errors in '%s': %s", flow_filename, "; ".join(errors))
            raise FlowValidationError(errors, flow.get("flow_id", flow_filename))

        _FLOW_CACHE[flow_filename] = flow
        return flow


def clear_flow_cache() -> None:
    with _FLOW_LOCK:
        _FLOW_CACHE.clear()
    try:
        from flows.loader import clear_loader_cache
        from flows.registry import clear_registry_cache
        clear_loader_cache()
        clear_registry_cache()
    except ImportError:
        pass


def validate_all_flows() -> dict[str, list[str]]:
    """
    Scans and validates all JSON flow files in FLOW_PATH as well as modular client flows.
    Returns mapping of filename -> errors list.
    """
    results: dict[str, list[str]] = {}
    if not FLOW_PATH.exists():
        return results

    for flow_file in sorted(FLOW_PATH.glob("*.json")):
        try:
            with open(flow_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            results[flow_file.name] = validate_flow(data, flow_file.name)
        except Exception as e:
            results[flow_file.name] = [f"JSON load error: {e}"]

    clients_dir = FLOW_PATH / "clients"
    if clients_dir.exists():
        from flows.loader import validate_modular_flow
        for cdir in sorted(clients_dir.iterdir()):
            if cdir.is_dir():
                results[f"clients/{cdir.name}"] = validate_modular_flow(cdir)

    return results


if __name__ == "__main__":
    # Usage: python -m app.services.flow_service
    import sys

    failed = False
    for name, errs in validate_all_flows().items():
        if errs:
            failed = True
            print(f"[FAIL] {name}")
            for err in errs:
                print(f"    - {err}")
        else:
            with open(FLOW_PATH / name, encoding="utf-8") as fh:
                unreachable = find_unreachable_steps(json.load(fh))
            note = f" (unreachable steps: {', '.join(unreachable)})" if unreachable else ""
            print(f"[OK]   {name}{note}")
    sys.exit(1 if failed else 0)

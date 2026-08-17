"""Enforce declared service-to-service communication via OpenAPI contracts."""

from __future__ import annotations

import re
import ast
import sys
import json
from typing import cast
from pathlib import Path
from dataclasses import dataclass

from shared.validation.contract_validator import run_contract_validation

COMMUNICATION_MAP_RELATIVE_PATH = Path("contracts/service_communication_map.json")
_CONTRACTS_DIRECTORY = Path("contracts/openapi")
_URL_PATTERN = re.compile(r"https?://([A-Za-z0-9_.:-]+)")
_LOCAL_HOST_NAMES = ("localhost", "127.0.0.1")


class ServiceContractGuardError(Exception):
    """Represent a base service contract guard violation.

    :param file_path: Path associated with the violation.
    :param message: Human-readable violation message.
    """

    def __init__(self, file_path: Path, message: str) -> None:
        super().__init__(message)
        self.file_path = file_path
        self.message = message


class MissingCommunicationMapError(ServiceContractGuardError):
    """Represent a missing service communication map violation."""


class InvalidCommunicationMapJsonError(ServiceContractGuardError):
    """Represent an invalid service communication map JSON violation."""


class UnknownServiceInMapError(ServiceContractGuardError):
    """Represent an unknown service reference in communication map."""


class MissingContractReferenceError(ServiceContractGuardError):
    """Represent a missing or invalid contract reference violation."""


class DuplicateServiceCallDeclarationError(ServiceContractGuardError):
    """Represent a duplicate caller-to-target declaration violation."""


class UndeclaredServiceCallError(ServiceContractGuardError):
    """Represent an undeclared detected service-to-service call violation."""


class PhantomServiceCallDeclarationError(ServiceContractGuardError):
    """Represent a declared call that is not detected in code violation."""


class ServiceContractGuardFailure(Exception):
    """Represent aggregate service contract guard violations.

    :param errors: Collected guard violations.
    """

    def __init__(self, errors: tuple[ServiceContractGuardError, ...]) -> None:
        super().__init__("Service contract guard violations detected.")
        self.errors = errors


@dataclass(frozen=True)
class GuardIssue:
    """Describe one service contract guard issue.

    :param file_path: Path associated with the issue.
    :param error_type: Violation error type name.
    :param message: Human-readable violation message.
    """

    file_path: Path
    error_type: str
    message: str


@dataclass(frozen=True)
class GuardResult:
    """Represent aggregate service contract guard results.

    :param success: Whether guard checks passed.
    :param issues: Collection of detected guard issues.
    """

    success: bool
    issues: tuple[GuardIssue, ...]


@dataclass(frozen=True)
class OutboundCallDeclaration:
    """Represent one declared outbound service call.

    :param caller_service: Caller service name.
    :param target_service: Target service name.
    :param contract_path: Declared contract path.
    :param call_sites: Human-auditable call site strings.
    :param planned: Whether declaration is planned-only.
    :param reason: Optional planned declaration reason.
    """

    caller_service: str
    target_service: str
    contract_path: Path
    call_sites: tuple[str, ...]
    planned: bool
    reason: str | None


def run_service_contract_guard(repo_root: Path | None = None) -> GuardResult:
    """Run service contract communication checks.

    :param repo_root: Repository root path. Uses current working directory when omitted.
    :return: Aggregate service contract guard result.
    """

    target_root = repo_root if repo_root is not None else Path.cwd()
    errors = collect_service_contract_errors(target_root)
    issues = tuple(_issue_from_error(error) for error in errors)
    return GuardResult(success=len(issues) == 0, issues=issues)


def enforce_service_contract_guard(repo_root: Path | None = None) -> None:
    """Run service contract checks and raise on violations.

    :param repo_root: Repository root path. Uses current working directory when omitted.
    :return: None.
    :raises ServiceContractGuardFailure: If one or more violations are detected.
    """

    target_root = repo_root if repo_root is not None else Path.cwd()
    errors = collect_service_contract_errors(target_root)
    if errors:
        raise ServiceContractGuardFailure(tuple(errors))


def collect_service_contract_errors(repo_root: Path) -> tuple[ServiceContractGuardError, ...]:
    """Collect all service contract guard violations.

    :param repo_root: Repository root path.
    :return: Tuple of service contract guard violations.
    """

    service_names = discover_service_names(repo_root)
    map_path = repo_root / COMMUNICATION_MAP_RELATIVE_PATH

    errors: list[ServiceContractGuardError] = []
    declarations = _load_declarations(map_path, repo_root, service_names, errors)

    detected_calls = detect_outbound_service_calls(repo_root, service_names)
    for caller_service, target_service in sorted(
        detected_calls,
        key=lambda item: (item[0], item[1]),
    ):
        if (caller_service, target_service) in declarations:
            continue
        source_files = detected_calls[(caller_service, target_service)]
        source_file = min(source_files, key=lambda item: item.as_posix())
        errors.append(
            UndeclaredServiceCallError(
                file_path=source_file,
                message=(
                    f"Detected call from '{caller_service}' to '{target_service}' must be "
                    "declared in contracts/service_communication_map.json."
                ),
            )
        )

    for declaration in declarations.values():
        pair = (declaration.caller_service, declaration.target_service)
        if pair in detected_calls:
            continue
        if declaration.planned:
            continue
        errors.append(
            PhantomServiceCallDeclarationError(
                file_path=map_path,
                message=(
                    f"Declared call from '{declaration.caller_service}' to "
                    f"'{declaration.target_service}' was not detected in service code. "
                    "Set planned=true with reason for planned-only declarations."
                ),
            )
        )

    return tuple(errors)


def discover_service_names(repo_root: Path) -> tuple[str, ...]:
    """Discover services under the repository services directory.

    :param repo_root: Repository root path.
    :return: Sorted tuple of service names.
    """

    services_root = repo_root / "services"
    if not services_root.exists():
        return ()

    discovered = [
        service_path.name for service_path in services_root.iterdir() if service_path.is_dir()
    ]
    return tuple(sorted(discovered))


def detect_outbound_service_calls(
    repo_root: Path,
    service_names: tuple[str, ...],
) -> dict[tuple[str, str], set[Path]]:
    """Detect likely inter-service calls under services code.

    :param repo_root: Repository root path.
    :param service_names: Known service names.
    :return: Mapping of caller-target pairs to source file paths.
    """

    detected: dict[tuple[str, str], set[Path]] = {}
    services_root = repo_root / "services"

    for caller_service in service_names:
        service_root = services_root / caller_service
        for file_path in sorted(service_root.rglob("*.py"), key=lambda item: item.as_posix()):
            literals = _extract_string_literals(file_path)
            for literal in literals:
                for target_service in _match_target_services(literal, service_names):
                    if target_service == caller_service:
                        continue
                    pair = (caller_service, target_service)
                    if pair not in detected:
                        detected[pair] = set()
                    detected[pair].add(file_path)

            httpx_targets = _detect_httpx_base_url_targets(file_path, service_names)
            for target_service in httpx_targets:
                if target_service == caller_service:
                    continue
                pair = (caller_service, target_service)
                if pair not in detected:
                    detected[pair] = set()
                detected[pair].add(file_path)

    return detected


def main() -> int:
    """Execute service contract guard checks as a CLI command.

    :return: Process exit code.
    """

    repo_root = Path(__file__).resolve().parents[2]
    try:
        enforce_service_contract_guard(repo_root)
    except ServiceContractGuardFailure as error:
        print(
            f"Service contract guard failed with {len(error.errors)} violation(s):",
            file=sys.stderr,
        )
        for issue in error.errors:
            display_path = _format_path(repo_root, issue.file_path)
            print(
                f"- {issue.__class__.__name__}: {display_path} - {issue.message}",
                file=sys.stderr,
            )
        return 1
    except Exception as error:  # pragma: no cover
        print(f"Unexpected service contract guard failure: {error}", file=sys.stderr)
        return 2

    print("Service contract guard passed.")
    return 0


def _load_declarations(
    map_path: Path,
    repo_root: Path,
    service_names: tuple[str, ...],
    errors: list[ServiceContractGuardError],
) -> dict[tuple[str, str], OutboundCallDeclaration]:
    if not map_path.exists():
        errors.append(
            MissingCommunicationMapError(
                file_path=map_path,
                message="Missing contracts/service_communication_map.json.",
            )
        )
        return {}

    document = _load_json_document(map_path, errors)
    if document is None:
        return {}

    services_section = _extract_services_section(document, map_path, errors)
    if services_section is None:
        return {}

    service_name_set = set(service_names)
    map_services = set(services_section.keys())

    missing_services = sorted(service_name_set - map_services)
    if missing_services:
        errors.append(
            InvalidCommunicationMapJsonError(
                file_path=map_path,
                message=(
                    "Communication map must include every service key; missing: "
                    + ", ".join(missing_services)
                    + "."
                ),
            )
        )

    unknown_services = sorted(map_services - service_name_set)
    for service_name in unknown_services:
        errors.append(
            UnknownServiceInMapError(
                file_path=map_path,
                message=f"Unknown caller service '{service_name}' in communication map.",
            )
        )

    declarations: dict[tuple[str, str], OutboundCallDeclaration] = {}
    referenced_contracts: set[Path] = set()

    for caller_service in service_names:
        caller_entry = services_section.get(caller_service)
        if caller_entry is None:
            continue
        outbound_calls = _extract_outbound_calls(map_path, caller_service, caller_entry, errors)
        for raw_call in outbound_calls:
            declaration = _parse_call_declaration(
                map_path=map_path,
                repo_root=repo_root,
                caller_service=caller_service,
                raw_call=raw_call,
                service_names=service_name_set,
                errors=errors,
            )
            if declaration is None:
                continue
            pair = (declaration.caller_service, declaration.target_service)
            if pair in declarations:
                errors.append(
                    DuplicateServiceCallDeclarationError(
                        file_path=map_path,
                        message=(
                            f"Duplicate declaration for caller '{declaration.caller_service}' and "
                            f"target '{declaration.target_service}'."
                        ),
                    )
                )
                continue

            declarations[pair] = declaration
            referenced_contracts.add(declaration.contract_path)

    if referenced_contracts:
        contract_result = run_contract_validation(repo_root)
        for issue in contract_result.issues:
            if issue.file_path not in referenced_contracts:
                continue
            errors.append(
                MissingContractReferenceError(
                    file_path=issue.file_path,
                    message=(
                        "Referenced contract failed validation: "
                        f"{issue.error_type} - {issue.message}"
                    ),
                )
            )

    return declarations


def _load_json_document(
    map_path: Path,
    errors: list[ServiceContractGuardError],
) -> dict[str, object] | None:
    try:
        contents = map_path.read_text(encoding="utf-8")
    except OSError as error:
        errors.append(
            InvalidCommunicationMapJsonError(
                file_path=map_path,
                message=f"Unable to read communication map: {error}.",
            )
        )
        return None

    try:
        document = json.loads(contents)
    except json.JSONDecodeError as error:
        errors.append(
            InvalidCommunicationMapJsonError(
                file_path=map_path,
                message=(
                    f"Invalid communication map JSON: {error.msg} at "
                    f"line {error.lineno} column {error.colno}."
                ),
            )
        )
        return None

    if not isinstance(document, dict):
        errors.append(
            InvalidCommunicationMapJsonError(
                file_path=map_path,
                message="Communication map root must be an object.",
            )
        )
        return None

    document_dict = cast(dict[object, object], document)
    result: dict[str, object] = {}
    for key, value in document_dict.items():
        if not isinstance(key, str):
            errors.append(
                InvalidCommunicationMapJsonError(
                    file_path=map_path,
                    message="Communication map root keys must be strings.",
                )
            )
            return None
        result[key] = value
    return result


def _extract_services_section(
    document: dict[str, object],
    map_path: Path,
    errors: list[ServiceContractGuardError],
) -> dict[str, object] | None:
    services_value = document.get("services")
    if not isinstance(services_value, dict):
        errors.append(
            InvalidCommunicationMapJsonError(
                file_path=map_path,
                message="Communication map must define a 'services' object.",
            )
        )
        return None

    services_dict = cast(dict[object, object], services_value)
    services_section: dict[str, object] = {}
    for key, value in services_dict.items():
        if not isinstance(key, str):
            errors.append(
                InvalidCommunicationMapJsonError(
                    file_path=map_path,
                    message="Communication map service keys must be strings.",
                )
            )
            continue
        services_section[key] = value
    return services_section


def _extract_outbound_calls(
    map_path: Path,
    caller_service: str,
    caller_entry: object,
    errors: list[ServiceContractGuardError],
) -> tuple[dict[str, object], ...]:
    if not isinstance(caller_entry, dict):
        errors.append(
            InvalidCommunicationMapJsonError(
                file_path=map_path,
                message=f"Service '{caller_service}' entry must be an object.",
            )
        )
        return ()

    caller_entry_dict = cast(dict[object, object], caller_entry)
    outbound_calls_value = caller_entry_dict.get("outbound_calls")
    if not isinstance(outbound_calls_value, list):
        errors.append(
            InvalidCommunicationMapJsonError(
                file_path=map_path,
                message=f"Service '{caller_service}' must define an 'outbound_calls' array.",
            )
        )
        return ()

    outbound_calls_list = cast(list[object], outbound_calls_value)
    parsed_calls: list[dict[str, object]] = []
    for index, call_entry in enumerate(outbound_calls_list):
        if not isinstance(call_entry, dict):
            errors.append(
                InvalidCommunicationMapJsonError(
                    file_path=map_path,
                    message=(
                        f"Service '{caller_service}' outbound_calls[{index}] must be an object."
                    ),
                )
            )
            continue

        call_entry_dict = cast(dict[object, object], call_entry)
        parsed_call: dict[str, object] = {}
        for key, value in call_entry_dict.items():
            if not isinstance(key, str):
                errors.append(
                    InvalidCommunicationMapJsonError(
                        file_path=map_path,
                        message=(
                            f"Service '{caller_service}' outbound_calls[{index}] keys must be "
                            "strings."
                        ),
                    )
                )
                parsed_call = {}
                break
            parsed_call[key] = value
        if parsed_call:
            parsed_calls.append(parsed_call)

    return tuple(parsed_calls)


def _parse_call_declaration(
    map_path: Path,
    repo_root: Path,
    caller_service: str,
    raw_call: dict[str, object],
    service_names: set[str],
    errors: list[ServiceContractGuardError],
) -> OutboundCallDeclaration | None:
    target_service = raw_call.get("target_service")
    contract_value = raw_call.get("contract")
    call_sites_value = raw_call.get("call_sites")
    planned_value = raw_call.get("planned", False)
    reason_value = raw_call.get("reason")

    if not isinstance(target_service, str) or not target_service.strip():
        errors.append(
            InvalidCommunicationMapJsonError(
                file_path=map_path,
                message=(
                    f"Service '{caller_service}' outbound call must include non-empty "
                    "'target_service'."
                ),
            )
        )
        return None
    target_service = target_service.strip()

    if target_service not in service_names:
        errors.append(
            UnknownServiceInMapError(
                file_path=map_path,
                message=(
                    f"Service '{caller_service}' references unknown target service "
                    f"'{target_service}'."
                ),
            )
        )
        return None

    if not isinstance(contract_value, str) or not contract_value.strip():
        errors.append(
            MissingContractReferenceError(
                file_path=map_path,
                message=(
                    f"Service '{caller_service}' to '{target_service}' must include a "
                    "non-empty 'contract' path."
                ),
            )
        )
        return None
    contract_string = contract_value.strip()
    expected_contract = (_CONTRACTS_DIRECTORY / f"{target_service}.yaml").as_posix()
    if contract_string != expected_contract:
        errors.append(
            MissingContractReferenceError(
                file_path=map_path,
                message=(
                    f"Service '{caller_service}' to '{target_service}' must reference "
                    f"'{expected_contract}', found '{contract_string}'."
                ),
            )
        )
        return None

    contract_path = repo_root / Path(contract_string)
    if not contract_path.exists():
        errors.append(
            MissingContractReferenceError(
                file_path=contract_path,
                message=(
                    f"Referenced contract for '{caller_service}' to '{target_service}' "
                    "does not exist."
                ),
            )
        )
        return None

    call_sites = _extract_call_sites(
        map_path=map_path,
        caller_service=caller_service,
        target_service=target_service,
        call_sites_value=call_sites_value,
        errors=errors,
    )
    if call_sites is None:
        return None

    if not isinstance(planned_value, bool):
        errors.append(
            InvalidCommunicationMapJsonError(
                file_path=map_path,
                message=(
                    f"Service '{caller_service}' to '{target_service}' has non-boolean "
                    "'planned' value."
                ),
            )
        )
        return None

    planned = planned_value
    reason: str | None = None
    if planned:
        if not isinstance(reason_value, str) or not reason_value.strip():
            errors.append(
                InvalidCommunicationMapJsonError(
                    file_path=map_path,
                    message=(
                        f"Planned declaration for '{caller_service}' to '{target_service}' "
                        "must include a non-empty 'reason'."
                    ),
                )
            )
            return None
        reason = reason_value.strip()

    return OutboundCallDeclaration(
        caller_service=caller_service,
        target_service=target_service,
        contract_path=contract_path,
        call_sites=call_sites,
        planned=planned,
        reason=reason,
    )


def _extract_call_sites(
    map_path: Path,
    caller_service: str,
    target_service: str,
    call_sites_value: object,
    errors: list[ServiceContractGuardError],
) -> tuple[str, ...] | None:
    if not isinstance(call_sites_value, list):
        errors.append(
            InvalidCommunicationMapJsonError(
                file_path=map_path,
                message=(
                    f"Service '{caller_service}' to '{target_service}' must include "
                    "'call_sites' array."
                ),
            )
        )
        return None

    call_sites_list = cast(list[object], call_sites_value)
    call_sites: list[str] = []
    for index, call_site_value in enumerate(call_sites_list):
        if not isinstance(call_site_value, str) or not call_site_value.strip():
            errors.append(
                InvalidCommunicationMapJsonError(
                    file_path=map_path,
                    message=(
                        f"Service '{caller_service}' to '{target_service}' has invalid "
                        f"call_sites[{index}] value."
                    ),
                )
            )
            return None
        call_sites.append(call_site_value.strip())

    if not call_sites:
        errors.append(
            InvalidCommunicationMapJsonError(
                file_path=map_path,
                message=(
                    f"Service '{caller_service}' to '{target_service}' must declare at least "
                    "one call site."
                ),
            )
        )
        return None

    return tuple(call_sites)


def _extract_string_literals(file_path: Path) -> tuple[str, ...]:
    try:
        contents = file_path.read_text(encoding="utf-8")
    except OSError:
        return ()

    try:
        tree = ast.parse(contents, filename=str(file_path))
    except SyntaxError:
        return ()

    literals: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            literals.append(node.value)
        if isinstance(node, ast.JoinedStr):
            joined_parts = [
                part.value
                for part in node.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            ]
            if joined_parts:
                literals.append("".join(joined_parts))
    return tuple(literals)


def _detect_httpx_base_url_targets(
    file_path: Path,
    service_names: tuple[str, ...],
) -> tuple[str, ...]:
    try:
        contents = file_path.read_text(encoding="utf-8")
    except OSError:
        return ()

    try:
        tree = ast.parse(contents, filename=str(file_path))
    except SyntaxError:
        return ()

    module_string_values = _collect_module_string_assignments(tree)
    detector = _HttpxBaseUrlTargetDetector(service_names, module_string_values)
    detector.visit(tree)
    return tuple(sorted(detector.detected_targets))


def _collect_module_string_assignments(tree: ast.Module) -> dict[str, ast.expr]:
    assignments: dict[str, ast.expr] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                assignments[target.id] = node.value
    return assignments


class _HttpxBaseUrlTargetDetector(ast.NodeVisitor):
    """Detect internal target services from httpx base_url constructor calls."""

    def __init__(
        self,
        service_names: tuple[str, ...],
        module_string_values: dict[str, ast.expr],
    ) -> None:
        self._service_names = service_names
        self._module_string_values = module_string_values
        self._class_stack: list[ast.ClassDef] = []
        self.detected_targets: set[str] = set()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._class_stack.append(node)
        self.generic_visit(node)
        self._class_stack.pop()

    def visit_Call(self, node: ast.Call) -> None:
        if not _is_httpx_client_constructor(node):
            self.generic_visit(node)
            return

        base_url_value = _find_keyword_value(node, "base_url")
        if base_url_value is None:
            self.generic_visit(node)
            return

        class_context = self._class_stack[-1] if self._class_stack else None
        urls = _resolve_base_url_candidates(
            expression=base_url_value,
            module_string_values=self._module_string_values,
            class_context=class_context,
        )
        for url in urls:
            for target in _match_target_services(url, self._service_names):
                self.detected_targets.add(target)

        self.generic_visit(node)


def _is_httpx_client_constructor(node: ast.Call) -> bool:
    func = node.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr not in {"Client", "AsyncClient"}:
        return False
    if not isinstance(func.value, ast.Name):
        return False
    return func.value.id == "httpx"


def _find_keyword_value(node: ast.Call, keyword_name: str) -> ast.expr | None:
    for keyword in node.keywords:
        if keyword.arg != keyword_name:
            continue
        return keyword.value
    return None


def _resolve_base_url_candidates(
    expression: ast.expr,
    module_string_values: dict[str, ast.expr],
    class_context: ast.ClassDef | None,
) -> tuple[str, ...]:
    class_attribute_sources: dict[str, ast.expr] = {}
    init_parameter_defaults: dict[str, ast.expr] = {}
    if class_context is not None:
        class_attribute_sources, init_parameter_defaults = _extract_class_init_sources(
            class_context,
        )

    urls = _resolve_expression_candidates(
        expression=expression,
        module_string_values=module_string_values,
        class_attribute_sources=class_attribute_sources,
        init_parameter_defaults=init_parameter_defaults,
        visited=set(),
    )
    return tuple(urls)


def _extract_class_init_sources(
    class_node: ast.ClassDef,
) -> tuple[dict[str, ast.expr], dict[str, ast.expr]]:
    for child in class_node.body:
        if isinstance(child, ast.FunctionDef) and child.name == "__init__":
            return _extract_init_assignments(child), _extract_parameter_defaults(child)
    return {}, {}


def _extract_init_assignments(function_node: ast.FunctionDef) -> dict[str, ast.expr]:
    assignments: dict[str, ast.expr] = {}
    for node in ast.walk(function_node):
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Attribute):
            continue
        if not isinstance(target.value, ast.Name):
            continue
        if target.value.id != "self":
            continue
        assignments[target.attr] = node.value
    return assignments


def _extract_parameter_defaults(function_node: ast.FunctionDef) -> dict[str, ast.expr]:
    defaults: dict[str, ast.expr] = {}
    positional_parameters = function_node.args.args
    positional_defaults = function_node.args.defaults
    if positional_defaults:
        offset = len(positional_parameters) - len(positional_defaults)
        for index, default in enumerate(positional_defaults):
            parameter_name = positional_parameters[offset + index].arg
            defaults[parameter_name] = default
    for parameter, default in zip(
        function_node.args.kwonlyargs,
        function_node.args.kw_defaults,
        strict=False,
    ):
        if default is None:
            continue
        defaults[parameter.arg] = default
    return defaults


def _resolve_expression_candidates(
    expression: ast.expr,
    module_string_values: dict[str, ast.expr],
    class_attribute_sources: dict[str, ast.expr],
    init_parameter_defaults: dict[str, ast.expr],
    visited: set[str],
) -> list[str]:
    if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
        return [expression.value]

    if isinstance(expression, ast.JoinedStr):
        joined_parts = [
            part.value
            for part in expression.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        ]
        if joined_parts and len(joined_parts) == len(expression.values):
            return ["".join(joined_parts)]
        return []

    if isinstance(expression, ast.Name):
        symbol_key = f"name:{expression.id}"
        if symbol_key in visited:
            return []
        visited.add(symbol_key)

        target_expression = module_string_values.get(expression.id)
        if target_expression is None:
            target_expression = init_parameter_defaults.get(expression.id)
        if target_expression is None:
            return []

        return _resolve_expression_candidates(
            expression=target_expression,
            module_string_values=module_string_values,
            class_attribute_sources=class_attribute_sources,
            init_parameter_defaults=init_parameter_defaults,
            visited=visited,
        )

    if isinstance(expression, ast.Attribute):
        if isinstance(expression.value, ast.Name) and expression.value.id == "self":
            symbol_key = f"self:{expression.attr}"
            if symbol_key in visited:
                return []
            visited.add(symbol_key)

            attribute_expression = class_attribute_sources.get(expression.attr)
            if attribute_expression is None:
                return []

            return _resolve_expression_candidates(
                expression=attribute_expression,
                module_string_values=module_string_values,
                class_attribute_sources=class_attribute_sources,
                init_parameter_defaults=init_parameter_defaults,
                visited=visited,
            )
        return []

    return []


def _match_target_services(
    raw_literal: str,
    service_names: tuple[str, ...],
) -> tuple[str, ...]:
    matched_services: set[str] = set()
    for match in _URL_PATTERN.finditer(raw_literal):
        host = match.group(1).lower()
        host_without_port = host.split(":", maxsplit=1)[0]
        if host_without_port in _LOCAL_HOST_NAMES:
            continue
        for service_name in service_names:
            if _host_matches_service(host_without_port, service_name):
                matched_services.add(service_name)

    return tuple(sorted(matched_services))


def _host_matches_service(host: str, service_name: str) -> bool:
    candidates = (service_name.lower(), service_name.lower().replace("_", "-"))
    for candidate in candidates:
        if host == candidate:
            return True
        if host.startswith(f"{candidate}."):
            return True
    return False


def _issue_from_error(error: ServiceContractGuardError) -> GuardIssue:
    return GuardIssue(
        file_path=error.file_path,
        error_type=error.__class__.__name__,
        message=error.message,
    )


def _format_path(repo_root: Path, file_path: Path) -> str:
    try:
        return str(file_path.relative_to(repo_root))
    except ValueError:
        return str(file_path)


if __name__ == "__main__":
    raise SystemExit(main())

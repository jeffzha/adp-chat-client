#!/usr/bin/env python3
"""Audit the ADP workbench fork patch and enforce component boundaries."""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


DEFAULT_BASELINE = "186084bfddc42cc369c722cced95842dd83c305f"
BASELINE = os.environ.get("CLAW_ADP_BASELINE", DEFAULT_BASELINE)
ALLOWED_PREFIXES = ("server/", "client/", ".github/")
ALLOWED_ROOT_PATHS = {"README.md"}
SOURCE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".vue"}

NEW_API_CORE_PATTERNS = (
    re.compile(
        r"github\.com/QuantumNous/new-api/"
        r"(?:model|relay|setting|middleware|constant|dto|types|service|controller|router)(?:\b|/)"
    ),
    re.compile(r"\b(?:from|import)\s+new_api(?:\.(?:model|relay|billing|quota|token))?\b"),
    re.compile(
        r"\bNEW_API_(?:DATABASE(?:_URL)?|DB_DSN|API_KEY|QUOTA|BILLING|RELAY)(?:\b|_)"
    ),
    re.compile(r"/(?:api/(?:token|channel)|v1/(?:chat|responses|messages))(?:/|\b)"),
)

SENSITIVE_NAME_RE = re.compile(
    r"(?:^|_)(?:app_?key|secret_?(?:id|key)|provider_?workspace_?locator|"
    r"workspace_?locator|locator_?(?:ciphertext|plaintext))(?:$|_)",
    re.IGNORECASE,
)
SENSITIVE_FRONTEND_RE = re.compile(
    r"\b(?:app_?key|appKey|secret_?(?:id|key)|secret(?:Id|Key)|"
    r"providerWorkspaceLocator|workspaceLocator|locatorCiphertext|locatorPlaintext)\b"
)
HARDCODED_SECRET_RES = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bAKID[A-Za-z0-9]{12,}\b"),
    re.compile(
        r"(?i)\b(?:APP_?KEY|SECRET_?(?:ID|KEY)|ACCESS_?TOKEN)\s*[:=]\s*"
        r"[\"'](?!\$\{|<|example|test|dummy|replace|change|your|xxx)[^\"']{12,}[\"']"
    ),
)

# ADP is the execution/data plane.  Customer/App/plan/payment truth belongs to
# claw-control and must not silently grow a second persistence model in this
# fork.  A plan snapshot is the sole exception: offline execution may retain an
# immutable, version-bound projection, but it must declare that fact explicitly
# and must not expose update/delete lifecycle columns.
CONTROL_PLANE_ENTITY_WORDS = frozenset(
    {
        "customer",
        "membership",
        "customerapp",
        "appconfig",
        "plan",
        "payment",
        "invoice",
        "subscription",
        "billing",
        "wallet",
        "balance",
        "charge",
        "settlement",
        "refund",
        "ledger",
    }
)
IMMUTABLE_PLAN_PROJECTION_MARKER = "__workbench_immutable_projection__"
IMMUTABLE_PROJECTION_MUTATION_FIELDS = frozenset(
    {
        "deletedat",
        "paymentstatus",
        "rowversion",
        "status",
        "updatedat",
    }
)
CONTROL_PLANE_STATE_FIELD_RES = (
    re.compile(r"^customer(?:code|displayname|status)$", re.IGNORECASE),
    re.compile(r"^membership(?:id|role|slot|status)$", re.IGNORECASE),
    re.compile(
        r"^plan(?:id|versionid|periodid|status|amount|price|currency)$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:payment|invoice|subscription|billing|wallet|balance|charge|"
        r"settlement|refund|ledger)",
        re.IGNORECASE,
    ),
)
APP_CONTEXT_SECRET_FIELD_RE = re.compile(
    r"(?:appkey|secretid|secretkey)",
    re.IGNORECASE,
)
RUNTIME_APP_CONTEXT_NAMES = frozenset({"app_context", "workbench_app_context"})

ADR_OVERRIDE_ENV = "CLAW_CONSTRAINT_ADR_OVERRIDE"
ADR_PATH_ENV = "CLAW_CONSTRAINT_ADR"
ADR_PATH_RE = re.compile(r"^server/WORKBENCH_ADR_[A-Z0-9][A-Z0-9_-]*\.md$")
ADR_FIELDS = (
    "Status",
    "Approved-By",
    "Approval-Date",
    "Constraint-IDs",
    "Impact",
    "Rollback",
)


@dataclass(frozen=True, order=True)
class Finding:
    code: str
    message: str
    path: str = ""

    def render(self) -> str:
        location = f" [{self.path}]" if self.path else ""
        return f"{self.code}{location}: {self.message}"


@dataclass(frozen=True)
class PatchEntry:
    path: str
    additions: int
    deletions: int


@dataclass
class Report:
    baseline: str
    existing_patches: list[PatchEntry]
    added_paths: list[str]
    findings: list[Finding]
    override_accepted: bool = False
    override_adr: str = ""

    @property
    def passed(self) -> bool:
        return not self.findings or self.override_accepted


def git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or "git command failed")
    return result.stdout


def has_commit(repo: Path, revision: str) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(repo), "cat-file", "-e", f"{revision}^{{commit}}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )


def normalize(path: str) -> str:
    return path.replace("\\", "/").removeprefix("./")


def collect_changes(
    repo: Path, baseline: str
) -> tuple[set[str], set[str], dict[str, tuple[int, int]]]:
    baseline_paths = {
        normalize(path)
        for path in git(repo, "ls-tree", "-r", "--name-only", baseline).splitlines()
        if path
    }
    changed_paths: set[str] = set()
    for raw in git(
        repo, "diff", "--no-renames", "--name-status", baseline, "--", "."
    ).splitlines():
        fields = raw.split("\t")
        if len(fields) >= 2:
            changed_paths.update(normalize(path) for path in fields[1:] if path)
    changed_paths.update(
        normalize(path)
        for path in git(repo, "ls-files", "--others", "--exclude-standard").splitlines()
        if path
    )

    numstat: dict[str, tuple[int, int]] = {}
    for raw in git(
        repo, "diff", "--no-renames", "--numstat", baseline, "--", "."
    ).splitlines():
        fields = raw.split("\t")
        if len(fields) == 3 and fields[0] != "-" and fields[1] != "-":
            numstat[normalize(fields[2])] = (int(fields[0]), int(fields[1]))
    return baseline_paths, changed_paths, numstat


def added_line_map(repo: Path, baseline: str, path: str, is_new: bool) -> dict[int, str]:
    target = repo / path
    if not target.is_file():
        return {}
    if is_new:
        return {
            number: line
            for number, line in enumerate(
                target.read_text(encoding="utf-8", errors="replace").splitlines(), 1
            )
        }

    diff = git(
        repo,
        "diff",
        "--no-renames",
        "--unified=0",
        baseline,
        "--",
        path,
    )
    result: dict[int, str] = {}
    new_line = 0
    for line in diff.splitlines():
        hunk = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if hunk:
            new_line = int(hunk.group(1))
            continue
        if not new_line or line.startswith("---") or line.startswith("+++"):
            continue
        if line.startswith("+"):
            result[new_line] = line[1:]
            new_line += 1
        elif line.startswith("-"):
            continue
        else:
            new_line += 1
    return result


def node_intersects_added(node: ast.AST, added_lines: set[int]) -> bool:
    start = getattr(node, "lineno", 0)
    end = getattr(node, "end_lineno", start)
    return bool(start and any(start <= line <= end for line in added_lines))


def node_has_sensitive_value(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and SENSITIVE_NAME_RE.search(child.id):
            return True
        if isinstance(child, ast.Attribute) and SENSITIVE_NAME_RE.search(child.attr):
            return True
        if isinstance(child, ast.Dict):
            for key in child.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    if SENSITIVE_NAME_RE.search(key.value.replace("-", "_")):
                        return True
    return False


def assignment_target_names(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, (ast.Tuple, ast.List)):
        return set().union(*(assignment_target_names(item) for item in node.elts))
    current = node
    while isinstance(current, (ast.Attribute, ast.Subscript)):
        current = current.value
    return {current.id} if isinstance(current, ast.Name) else set()


def expression_is_tainted(node: ast.AST, tainted: set[str]) -> bool:
    if isinstance(node, ast.Call):
        name = call_name(node)
        if name in {
            "workbenchactionpolicy.project_response",
            "workbenchcatalogpolicy.project_response",
            "workbenchsecurefilepipeline.redact_private_urls",
            "project_workbench_exception",
        }:
            return False
        if (
            name.endswith(".get")
            and len(node.args) == 1
            and not node.keywords
            and isinstance(node.func, ast.Attribute)
            and not node_has_sensitive_value(node.func.value)
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and not SENSITIVE_NAME_RE.search(node.args[0].value.replace("-", "_"))
        ):
            return False
    if (
        isinstance(node, ast.Subscript)
        and not node_has_sensitive_value(node.value)
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, str)
        and not SENSITIVE_NAME_RE.search(node.slice.value.replace("-", "_"))
    ):
        return False
    if isinstance(node, ast.Attribute):
        if SENSITIVE_NAME_RE.search(node.attr):
            return True
        if (
            isinstance(node.value, ast.Name)
            and node.value.id.casefold() in RUNTIME_APP_CONTEXT_NAMES
        ):
            # Individual non-secret scope/policy fields may be projected by a
            # dedicated endpoint.  Serializing the complete dataclass through
            # __dict__/vars/asdict is forbidden because it contains AppKey and
            # provider credentials.
            return node.attr == "__dict__"
    if isinstance(node, ast.Name):
        return SENSITIVE_NAME_RE.search(node.id) is not None or (
            isinstance(node.ctx, ast.Load) and node.id in tainted
        )
    if isinstance(node, ast.Dict):
        for key in node.keys:
            if (
                isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and SENSITIVE_NAME_RE.search(key.value.replace("-", "_"))
            ):
                return True
    return any(expression_is_tainted(child, tainted) for child in ast.iter_child_nodes(node))


def expression_contains_full_app_context(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id.casefold() in RUNTIME_APP_CONTEXT_NAMES
    if isinstance(node, ast.Attribute):
        if (
            isinstance(node.value, ast.Name)
            and node.value.id.casefold() in RUNTIME_APP_CONTEXT_NAMES
        ):
            return node.attr == "__dict__"
    return any(
        expression_contains_full_app_context(child)
        for child in ast.iter_child_nodes(node)
    )


def scope_nodes(tree: ast.AST) -> dict[ast.AST, list[ast.AST]]:
    """Group nodes by their nearest function so generic names do not cross-taint."""

    grouped: dict[ast.AST, list[ast.AST]] = {tree: []}

    def visit(node: ast.AST, scope: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            scope = node
            grouped.setdefault(scope, [])
        grouped[scope].append(node)
        for child in ast.iter_child_nodes(node):
            visit(child, scope)

    visit(tree, tree)
    return grouped


def tainted_names_for_scope(nodes: list[ast.AST]) -> set[str]:
    tainted = {
        node.arg
        for node in nodes
        if isinstance(node, ast.arg) and SENSITIVE_NAME_RE.search(node.arg)
    }
    changed = True
    while changed:
        changed = False
        for node in nodes:
            targets: set[str] = set()
            assignment_targets: set[str] = set()
            sensitive_target = False
            value: ast.AST | None = None
            if isinstance(node, ast.Assign):
                assignment_targets = set().union(
                    *(assignment_target_names(target) for target in node.targets)
                )
                value = node.value
                sensitive_target = any(
                    node_has_sensitive_value(target) for target in node.targets
                )
            elif isinstance(node, ast.AnnAssign):
                assignment_targets = assignment_target_names(node.target)
                sensitive_target = node_has_sensitive_value(node.target)
                value = node.value
            elif isinstance(node, ast.NamedExpr):
                assignment_targets = assignment_target_names(node.target)
                sensitive_target = node_has_sensitive_value(node.target)
                value = node.value
            elif isinstance(node, ast.AugAssign):
                assignment_targets = assignment_target_names(node.target)
                sensitive_target = node_has_sensitive_value(node.target)
                value = node.value
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"update", "setdefault", "append", "extend"}
                and isinstance(node.func.value, ast.Name)
                and expression_is_tainted(node, tainted)
            ):
                targets = {node.func.value.id}
            if value is not None and (
                sensitive_target or expression_is_tainted(value, tainted)
            ):
                targets.update(assignment_targets)
            additions = targets - tainted
            if additions:
                tainted.update(additions)
                changed = True
    return tainted


def call_name(call: ast.Call) -> str:
    current: ast.AST = call.func
    parts: list[str] = []
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts)).casefold()


def identifier_words(value: str) -> set[str]:
    words: list[str] = []
    for part in re.split(r"[^A-Za-z0-9]+", value):
        if not part:
            continue
        words.extend(
            match.group(0).casefold()
            for match in re.finditer(
                r"[A-Z]+(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+|\d+",
                part,
            )
        )
    result = set(words)
    if "customer" in result and "app" in result:
        result.add("customerapp")
    if "app" in result and "config" in result:
        result.add("appconfig")
    return result


def assigned_name(node: ast.AST) -> str | None:
    target: ast.AST | None = None
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target = node.targets[0]
    elif isinstance(node, ast.AnnAssign):
        target = node.target
    if isinstance(target, ast.Name):
        return target.id
    return None


def assignment_value(node: ast.AST) -> ast.AST | None:
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        return node.value
    return None


def persistent_class(node: ast.ClassDef) -> bool:
    for base in node.bases:
        if isinstance(base, ast.Name) and base.id in {"Base", "DeclarativeBase"}:
            return True
        if isinstance(base, ast.Attribute) and base.attr in {"Base", "DeclarativeBase"}:
            return True
    return any(assigned_name(item) == "__tablename__" for item in node.body)


def table_name(node: ast.ClassDef) -> str:
    for item in node.body:
        if assigned_name(item) != "__tablename__":
            continue
        value = assignment_value(item)
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
    return ""


def immutable_projection_marker(node: ast.ClassDef) -> bool:
    for item in node.body:
        if assigned_name(item) != IMMUTABLE_PLAN_PROJECTION_MARKER:
            continue
        value = assignment_value(item)
        return isinstance(value, ast.Constant) and value.value is True
    return False


def python_persistence_boundary_findings(
    repo: Path,
    path: str,
    added_lines: dict[int, str],
) -> list[Finding]:
    """Reject ADP-owned persistence that duplicates claw-control truth."""

    try:
        tree = ast.parse((repo / path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return []
    added = set(added_lines)
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or not persistent_class(node):
            continue
        changed_members = [
            item for item in node.body if node_intersects_added(item, added)
        ]
        if not changed_members:
            continue

        words = identifier_words(node.name) | identifier_words(table_name(node))
        control_plane_entity = bool(words & CONTROL_PLANE_ENTITY_WORDS)
        plan_projection = "plan" in words and bool(words & {"snapshot", "projection"})
        marker = immutable_projection_marker(node)
        field_names = {
            name.casefold()
            for item in node.body
            if (name := assigned_name(item)) is not None
        }
        has_onupdate = any(
            isinstance(child, ast.keyword) and child.arg == "onupdate"
            for child in ast.walk(node)
        )

        if control_plane_entity:
            projection_is_immutable = (
                plan_projection
                and marker
                and not (field_names & IMMUTABLE_PROJECTION_MUTATION_FIELDS)
                and not has_onupdate
            )
            if not projection_is_immutable:
                findings.append(
                    Finding(
                        "CONTROL_PLANE_PERSISTENCE",
                        (
                            f"persistent class {node.name!r} duplicates claw-control "
                            "Customer/App/plan/payment ownership; only an explicitly "
                            "marked immutable PlanSnapshot/PlanProjection is allowed"
                        ),
                        path,
                    )
                )

        if plan_projection and marker and (
            field_names & IMMUTABLE_PROJECTION_MUTATION_FIELDS or has_onupdate
        ):
            findings.append(
                Finding(
                    "MUTABLE_PLAN_PROJECTION",
                    f"plan projection {node.name!r} declares mutable lifecycle state",
                    path,
                )
            )

        for item in changed_members:
            name = assigned_name(item)
            if not name or name.startswith("__"):
                continue
            normalized = re.sub(r"[^a-z0-9]", "", name.casefold())
            if APP_CONTEXT_SECRET_FIELD_RE.search(normalized):
                findings.append(
                    Finding(
                        "APP_CONTEXT_SECRET_PERSISTENCE",
                        (
                            f"runtime App context secret field {name!r} is persisted "
                            f"by {node.name!r}"
                        ),
                        path,
                    )
                )
            if normalized.startswith("plansnapshot") or normalized.startswith("planprojection"):
                continue
            if any(pattern.search(normalized) for pattern in CONTROL_PLANE_STATE_FIELD_RES):
                findings.append(
                    Finding(
                        "CONTROL_PLANE_PERSISTENCE",
                        (
                            f"control-plane state field {name!r} is persisted by "
                            f"ADP class {node.name!r}"
                        ),
                        path,
                    )
                )
    return findings


def python_exposure_findings(
    repo: Path, path: str, added_lines: dict[int, str]
) -> list[Finding]:
    try:
        tree = ast.parse((repo / path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return []
    added = set(added_lines)
    findings: list[Finding] = []
    browser_boundary = path.startswith("server/router/") or path.startswith(
        "server/middleware/"
    )
    for _scope, nodes in scope_nodes(tree).items():
        tainted = tainted_names_for_scope(nodes)
        for node in nodes:
            if not node_intersects_added(node, added):
                continue
            if browser_boundary and isinstance(node, ast.Return) and node.value:
                if expression_is_tainted(
                    node.value, tainted
                ) or expression_contains_full_app_context(node.value):
                    findings.append(
                        Finding(
                            "SECRET_OR_LOCATOR_EXPOSURE",
                            f"browser-facing return contains a secret/locator at line {node.lineno}",
                            path,
                        )
                    )
            if (
                isinstance(node, ast.Raise)
                and node.exc
                and (
                    expression_is_tainted(node.exc, tainted)
                    or expression_contains_full_app_context(node.exc)
                )
            ):
                findings.append(
                    Finding(
                        "SECRET_OR_LOCATOR_EXPOSURE",
                        f"exception includes a secret/locator value at line {node.lineno}",
                        path,
                    )
                )
            if not isinstance(node, ast.Call):
                continue
            name = call_name(node)
            is_log_sink = name == "print" or name.startswith(("logging.", "logger."))
            terminal = name.rsplit(".", 1)[-1]
            is_response_sink = browser_boundary and terminal in {
                "json",
                "jsonresponse",
                "jsonable_encoder",
                "model_dump",
                "raw",
                "text",
            }
            if (is_log_sink or is_response_sink) and (
                expression_is_tainted(node, tainted)
                or expression_contains_full_app_context(node)
            ):
                findings.append(
                    Finding(
                        "SECRET_OR_LOCATOR_EXPOSURE",
                        f"secret/locator reaches {name or 'a response sink'} at line {node.lineno}",
                        path,
                    )
                )
    return findings


def parse_adr_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for name in ADR_FIELDS:
        match = re.search(rf"(?mi)^\s*{re.escape(name)}\s*:\s*(.+?)\s*$", text)
        if match:
            fields[name] = match.group(1).strip()
    return fields


def validate_adr(repo: Path, path_value: str, findings: list[Finding]) -> list[str]:
    path_value = normalize(path_value.strip())
    if not ADR_PATH_RE.fullmatch(path_value):
        return ["ADR path must match server/WORKBENCH_ADR_<SLUG>.md"]
    path = (repo / path_value).resolve()
    try:
        path.relative_to(repo.resolve())
    except ValueError:
        return ["ADR path escapes the repository"]
    if not path.is_file():
        return ["ADR file does not exist"]
    fields = parse_adr_fields(path.read_text(encoding="utf-8", errors="replace"))
    missing = [name for name in ADR_FIELDS if name not in fields]
    if missing:
        return ["missing required fields: " + ", ".join(missing)]
    errors: list[str] = []
    if fields["Status"].casefold() != "approved":
        errors.append("Status must be approved")
    if len(fields["Approved-By"]) < 3 or fields["Approved-By"].casefold() in {
        "tbd",
        "todo",
        "unknown",
    }:
        errors.append("Approved-By must identify the approving project owner")
    try:
        approval_date = dt.date.fromisoformat(fields["Approval-Date"])
        if approval_date > dt.date.today():
            errors.append("Approval-Date cannot be in the future")
    except ValueError:
        errors.append("Approval-Date must use YYYY-MM-DD")
    if len(fields["Impact"]) < 20:
        errors.append("Impact must contain a substantive impact description")
    if len(fields["Rollback"]) < 20:
        errors.append("Rollback must contain a substantive rollback procedure")
    approved = {
        value.strip()
        for value in re.split(r"[,\s]+", fields["Constraint-IDs"])
        if value.strip()
    }
    missing_codes = sorted({finding.code for finding in findings} - approved)
    if missing_codes:
        errors.append("Constraint-IDs does not cover: " + ", ".join(missing_codes))
    return errors


def evaluate(
    repo: Path,
    baseline: str = BASELINE,
    environ: dict[str, str] | None = None,
) -> Report:
    repo = repo.resolve()
    if not has_commit(repo, baseline):
        return Report(
            baseline,
            [],
            [],
            [Finding("BASELINE_UNAVAILABLE", f"baseline commit is unavailable: {baseline}")],
        )
    baseline_paths, changed_paths, numstat = collect_changes(repo, baseline)
    existing_patches = [
        PatchEntry(path, *numstat.get(path, (0, 0)))
        for path in sorted(changed_paths & baseline_paths)
    ]
    added_paths = sorted(changed_paths - baseline_paths)
    findings: list[Finding] = []

    for path in sorted(changed_paths):
        if path not in ALLOWED_ROOT_PATHS and not path.startswith(ALLOWED_PREFIXES):
            findings.append(
                Finding(
                    "ADP_PATH_BOUNDARY",
                    "workbench patch is outside server/client/CI ownership",
                    path,
                )
            )
        target = repo / path
        if not target.is_file() or Path(path).suffix.lower() not in SOURCE_SUFFIXES:
            continue
        added_lines = added_line_map(repo, baseline, path, path not in baseline_paths)
        added_text = "\n".join(added_lines.values())
        is_test = (
            "/test/" in path
            or "/tests/" in path
            or path.startswith("server/test/")
            or path.endswith((".test.ts", ".test.tsx", ".test.js", ".test.jsx"))
        )
        if not is_test:
            for pattern in NEW_API_CORE_PATTERNS:
                if pattern.search(added_text):
                    findings.append(
                        Finding(
                            "NEW_API_CORE_DEPENDENCY",
                            "ADP fork depends on a new-api DB/API-key/quota/relay surface",
                            path,
                        )
                    )
                    break
            for line_number, line in added_lines.items():
                if any(pattern.search(line) for pattern in HARDCODED_SECRET_RES):
                    findings.append(
                        Finding(
                            "HARDCODED_SECRET",
                            f"added credential-like literal at line {line_number}",
                            path,
                        )
                    )
            if path.startswith("client/"):
                for line_number, line in added_lines.items():
                    if SENSITIVE_FRONTEND_RE.search(line):
                        findings.append(
                            Finding(
                                "SECRET_OR_LOCATOR_EXPOSURE",
                                f"frontend code references a server-only secret/locator at line {line_number}",
                                path,
                            )
                        )
        if path.endswith(".py") and not is_test:
            findings.extend(python_exposure_findings(repo, path, added_lines))
            findings.extend(
                python_persistence_boundary_findings(repo, path, added_lines)
            )

    diff_check = subprocess.run(
        ["git", "-C", str(repo), "diff", "--check", baseline, "--", "."],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if diff_check.returncode:
        findings.append(Finding("DIFF_CHECK", "git diff --check reported whitespace errors"))

    report = Report(
        baseline,
        existing_patches,
        added_paths,
        sorted(set(findings)),
    )
    env = os.environ if environ is None else environ
    if not findings or env.get(ADR_OVERRIDE_ENV, "").strip().casefold() != "true":
        return report
    adr_path = env.get(ADR_PATH_ENV, "").strip()
    adr_errors = validate_adr(repo, adr_path, report.findings)
    if adr_errors:
        report.findings.extend(
            Finding("ADR_INVALID", message, adr_path) for message in adr_errors
        )
        report.findings = sorted(set(report.findings))
        return report
    report.override_accepted = True
    report.override_adr = normalize(adr_path)
    return report


def print_report(report: Report) -> None:
    print(f"ADP workbench baseline: {report.baseline}")
    print(f"Patched baseline-existing files: {len(report.existing_patches)}")
    for entry in report.existing_patches:
        print(f"  {entry.path}: +{entry.additions}/-{entry.deletions}")
    print(f"Added files: {len(report.added_paths)}")
    for path in report.added_paths:
        print(f"  {path}")
    if report.override_accepted:
        print(
            f"WARNING: approved ADR override accepted: {report.override_adr}",
            file=sys.stderr,
        )
    elif report.findings:
        for finding in report.findings:
            print(f"ERROR: {finding.render()}", file=sys.stderr)


def write_json_report(path: Path, report: Report) -> None:
    payload = {
        "baseline": report.baseline,
        "existing_patches": [asdict(entry) for entry in report.existing_patches],
        "added_paths": report.added_paths,
        "findings": [asdict(finding) for finding in report.findings],
        "override_accepted": report.override_accepted,
        "override_adr": report.override_adr,
        "passed": report.passed,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--baseline", default=BASELINE)
    parser.add_argument("--report-json", type=Path)
    args = parser.parse_args(argv)
    report = evaluate(args.repo, args.baseline)
    print_report(report)
    if args.report_json:
        write_json_report(args.report_json, report)
    return 0 if report.passed else (2 if any(f.code == "BASELINE_UNAVAILABLE" for f in report.findings) else 1)


if __name__ == "__main__":
    raise SystemExit(main())

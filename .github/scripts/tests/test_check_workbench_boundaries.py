from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "check_workbench_boundaries.py"
SPEC = importlib.util.spec_from_file_location("check_workbench_boundaries", SCRIPT)
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gate
SPEC.loader.exec_module(gate)


class RepositoryFixture:
    def __init__(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Boundary Test")
        self.git("config", "user.email", "boundary@example.invalid")

    def close(self) -> None:
        self._temporary.cleanup()

    def git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.root), *args],
            check=True,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return result.stdout.strip()

    def write(self, path: str, content: str) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def baseline(self, files: dict[str, str] | None = None) -> str:
        for path, content in (files or {"README.md": "baseline\n"}).items():
            self.write(path, content)
        self.git("add", ".")
        self.git("commit", "-q", "-m", "baseline")
        return self.git("rev-parse", "HEAD")


class ADPBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = RepositoryFixture()

    def tearDown(self) -> None:
        self.repo.close()

    @staticmethod
    def codes(report: object) -> set[str]:
        return {finding.code for finding in report.findings}

    def test_server_owned_addition_is_allowed(self) -> None:
        baseline = self.repo.baseline()
        self.repo.write("server/core/workbench_runtime.py", "ENABLED = False\n")

        report = gate.evaluate(self.repo.root, baseline, {})

        self.assertTrue(report.passed)

    def test_root_readme_is_the_only_allowed_root_documentation_patch(self) -> None:
        baseline = self.repo.baseline()
        self.repo.write("README.md", "baseline\n\nSee server/WORKBENCH_BOUNDARY_GATE.md.\n")

        report = gate.evaluate(self.repo.root, baseline, {})

        self.assertTrue(report.passed)

    def test_patch_report_records_baseline_existing_file_line_counts(self) -> None:
        baseline = self.repo.baseline({"server/core/chat.py": "value = 1\n"})
        self.repo.write("server/core/chat.py", "value = 2\nextra = True\n")

        report = gate.evaluate(self.repo.root, baseline, {})

        self.assertEqual(1, len(report.existing_patches))
        patch = report.existing_patches[0]
        self.assertEqual("server/core/chat.py", patch.path)
        self.assertEqual((2, 1), (patch.additions, patch.deletions))

    def test_new_api_api_key_or_core_dependency_is_denied(self) -> None:
        baseline = self.repo.baseline()
        self.repo.write(
            "server/core/workbench_runtime.py",
            'NEW_API_API_KEY = "read-from-another-service"\n',
        )

        report = gate.evaluate(self.repo.root, baseline, {})

        self.assertIn("NEW_API_CORE_DEPENDENCY", self.codes(report))

    def test_frontend_cannot_receive_server_secret_or_locator(self) -> None:
        baseline = self.repo.baseline()
        self.repo.write(
            "client/src/workbench.ts",
            "export const secretKey = payload.secretKey\n",
        )

        report = gate.evaluate(self.repo.root, baseline, {})

        self.assertIn("SECRET_OR_LOCATOR_EXPOSURE", self.codes(report))

    def test_browser_router_cannot_return_plaintext_secret(self) -> None:
        baseline = self.repo.baseline()
        self.repo.write(
            "server/router/workbench.py",
            "def response(app_key):\n    return {'app_key': app_key}\n",
        )

        report = gate.evaluate(self.repo.root, baseline, {})

        self.assertIn("SECRET_OR_LOCATOR_EXPOSURE", self.codes(report))

    def test_browser_router_cannot_return_indirect_sensitive_payload(self) -> None:
        baseline = self.repo.baseline()
        self.repo.write(
            "server/router/workbench.py",
            "from sanic import json\n"
            "def response(app_key):\n"
            "    payload = {'app_key': app_key}\n"
            "    return json(payload)\n",
        )

        report = gate.evaluate(self.repo.root, baseline, {})

        self.assertIn("SECRET_OR_LOCATOR_EXPOSURE", self.codes(report))

    def test_browser_router_cannot_serialize_complete_app_context(self) -> None:
        baseline = self.repo.baseline()
        self.repo.write(
            "server/router/workbench.py",
            "from sanic import json\n"
            "def response(app_context):\n"
            "    return json(app_context.__dict__)\n",
        )

        report = gate.evaluate(self.repo.root, baseline, {})

        self.assertIn("SECRET_OR_LOCATOR_EXPOSURE", self.codes(report))

    def test_router_may_project_a_non_secret_app_context_field(self) -> None:
        baseline = self.repo.baseline()
        self.repo.write(
            "server/router/workbench.py",
            "from sanic import json\n"
            "def response(app_context):\n"
            "    return json({'application_id': app_context.application_id})\n",
        )

        report = gate.evaluate(self.repo.root, baseline, {})

        self.assertTrue(report.passed)

    def test_generic_payload_name_is_not_tainted_across_functions(self) -> None:
        baseline = self.repo.baseline()
        self.repo.write(
            "server/router/workbench.py",
            "from sanic import json\n"
            "def internal(app_key):\n"
            "    payload = {'app_key': app_key}\n"
            "    return None\n"
            "def public():\n"
            "    payload = {'status': 'ok'}\n"
            "    return json(payload)\n",
        )

        report = gate.evaluate(self.repo.root, baseline, {})

        self.assertTrue(report.passed)

    def test_backend_cannot_log_locator_value(self) -> None:
        baseline = self.repo.baseline()
        self.repo.write(
            "server/core/workbench_runtime.py",
            "def run(logger, workspace_locator):\n"
            "    logger.info('workspace resolved', workspace_locator)\n",
        )

        report = gate.evaluate(self.repo.root, baseline, {})

        self.assertIn("SECRET_OR_LOCATOR_EXPOSURE", self.codes(report))

    def test_credential_like_literal_is_denied(self) -> None:
        baseline = self.repo.baseline()
        self.repo.write(
            "server/core/workbench_runtime.py",
            "SECRET_KEY = 'live-credential-value-123456789'\n",
        )

        report = gate.evaluate(self.repo.root, baseline, {})

        self.assertIn("HARDCODED_SECRET", self.codes(report))

    def test_adp_cannot_add_control_plane_persistence_entities(self) -> None:
        baseline = self.repo.baseline()
        forbidden = {
            "customer": (
                "WorkbenchCustomerMembership",
                "workbench_customer_membership",
            ),
            "app": ("WorkbenchCustomerApp", "workbench_customer_app"),
            "plan": ("WorkbenchPlan", "workbench_plan"),
            "payment": ("WorkbenchPayment", "workbench_payment"),
            "invoice": ("WorkbenchInvoice", "workbench_invoice"),
            "balance": ("WorkbenchBalanceLedger", "workbench_balance_ledger"),
        }
        for name, (class_name, table) in forbidden.items():
            self.repo.write(
                f"server/model/workbench_{name}.py",
                "from model.base import Base\n"
                f"class {class_name}(Base):\n"
                f"    __tablename__ = {table!r}\n"
                "    Id = object()\n",
            )

        report = gate.evaluate(self.repo.root, baseline, {})

        self.assertIn("CONTROL_PLANE_PERSISTENCE", self.codes(report))
        finding_paths = {
            finding.path
            for finding in report.findings
            if finding.code == "CONTROL_PLANE_PERSISTENCE"
        }
        self.assertEqual(
            {f"server/model/workbench_{name}.py" for name in forbidden},
            finding_paths,
        )

    def test_control_plane_table_name_is_denied_even_with_generic_class_name(self) -> None:
        baseline = self.repo.baseline()
        self.repo.write(
            "server/model/workbench_record.py",
            "from model.base import Base\n"
            "class Record(Base):\n"
            "    __tablename__ = 'workbench_customer_apps'\n"
            "    Id = object()\n",
        )

        report = gate.evaluate(self.repo.root, baseline, {})

        self.assertIn("CONTROL_PLANE_PERSISTENCE", self.codes(report))

    def test_embedded_plan_snapshot_fields_are_allowed(self) -> None:
        baseline = self.repo.baseline()
        self.repo.write(
            "server/model/workbench_scheduled.py",
            "from model.base import Base\n"
            "class WorkbenchScheduledTask(Base):\n"
            "    __tablename__ = 'workbench_scheduled_task'\n"
            "    Id = object()\n"
            "    PlanSnapshotJson = object()\n"
            "    PlanSnapshotHash = object()\n",
        )

        report = gate.evaluate(self.repo.root, baseline, {})

        self.assertTrue(report.passed)

    def test_explicit_immutable_plan_projection_is_allowed(self) -> None:
        baseline = self.repo.baseline()
        self.repo.write(
            "server/model/workbench_plan_projection.py",
            "from model.base import Base\n"
            "class WorkbenchPlanProjection(Base):\n"
            "    __tablename__ = 'workbench_plan_projection'\n"
            "    __workbench_immutable_projection__ = True\n"
            "    Id = object()\n"
            "    SourceVersion = object()\n"
            "    SnapshotJson = object()\n"
            "    SnapshotHash = object()\n"
            "    CreatedAt = object()\n",
        )

        report = gate.evaluate(self.repo.root, baseline, {})

        self.assertTrue(report.passed)

    def test_plan_projection_requires_marker_and_rejects_update_lifecycle(self) -> None:
        baseline = self.repo.baseline()
        self.repo.write(
            "server/model/workbench_plan_snapshot.py",
            "from model.base import Base\n"
            "class WorkbenchPlanSnapshot(Base):\n"
            "    __tablename__ = 'workbench_plan_snapshot'\n"
            "    Id = object()\n"
            "    SnapshotJson = object()\n",
        )
        self.repo.write(
            "server/model/workbench_mutable_plan_projection.py",
            "from model.base import Base\n"
            "class WorkbenchPlanProjection(Base):\n"
            "    __tablename__ = 'workbench_plan_projection'\n"
            "    __workbench_immutable_projection__ = True\n"
            "    Id = object()\n"
            "    UpdatedAt = object()\n",
        )

        report = gate.evaluate(self.repo.root, baseline, {})

        self.assertIn("CONTROL_PLANE_PERSISTENCE", self.codes(report))
        self.assertIn("MUTABLE_PLAN_PROJECTION", self.codes(report))

    def test_immutable_plan_projection_cannot_become_a_payment_or_balance_ledger(self) -> None:
        baseline = self.repo.baseline()
        self.repo.write(
            "server/model/workbench_plan_projection.py",
            "from model.base import Base\n"
            "class WorkbenchPlanProjection(Base):\n"
            "    __tablename__ = 'workbench_plan_projection'\n"
            "    __workbench_immutable_projection__ = True\n"
            "    Id = object()\n"
            "    SnapshotJson = object()\n"
            "    WalletBalance = object()\n",
        )

        report = gate.evaluate(self.repo.root, baseline, {})

        self.assertIn("CONTROL_PLANE_PERSISTENCE", self.codes(report))

    def test_runtime_app_context_secrets_cannot_be_persisted(self) -> None:
        baseline = self.repo.baseline()
        self.repo.write(
            "server/model/workbench_vendor_config.py",
            "from model.base import Base\n"
            "class WorkbenchVendorConfig(Base):\n"
            "    __tablename__ = 'workbench_vendor_config'\n"
            "    Id = object()\n"
            "    AppKeyCiphertext = object()\n"
            "    SecretIdRef = object()\n"
            "    SecretKeyRef = object()\n",
        )

        report = gate.evaluate(self.repo.root, baseline, {})

        self.assertIn("APP_CONTEXT_SECRET_PERSISTENCE", self.codes(report))

    def test_incomplete_adr_cannot_override_path_boundary(self) -> None:
        baseline = self.repo.baseline()
        self.repo.write("workbench.py", "enabled = True\n")
        adr = "server/WORKBENCH_ADR_BOUNDARY.md"
        self.repo.write(adr, "Approved: yes\n")

        report = gate.evaluate(
            self.repo.root,
            baseline,
            {gate.ADR_OVERRIDE_ENV: "true", gate.ADR_PATH_ENV: adr},
        )

        self.assertFalse(report.passed)
        self.assertIn("ADR_INVALID", self.codes(report))

    def test_explicit_structured_adr_can_override_named_finding(self) -> None:
        baseline = self.repo.baseline()
        self.repo.write("workbench.py", "enabled = True\n")
        adr = "server/WORKBENCH_ADR_BOUNDARY.md"
        self.repo.write(adr, self.valid_adr("ADP_PATH_BOUNDARY"))

        default_report = gate.evaluate(self.repo.root, baseline, {})
        self.assertFalse(default_report.passed)

        override_report = gate.evaluate(
            self.repo.root,
            baseline,
            {gate.ADR_OVERRIDE_ENV: "true", gate.ADR_PATH_ENV: adr},
        )
        self.assertTrue(override_report.passed)
        self.assertTrue(override_report.override_accepted)

    @staticmethod
    def valid_adr(constraint_ids: str) -> str:
        return f"""# Approved ADP fork boundary exception

Status: approved
Approved-By: Project Owner
Approval-Date: 2026-08-09
Constraint-IDs: {constraint_ids}
Impact: This exception expands the audited ADP fork ownership and merge surface.
Rollback: Disable workbench mode and revert every file listed by this decision.
"""


if __name__ == "__main__":
    unittest.main()

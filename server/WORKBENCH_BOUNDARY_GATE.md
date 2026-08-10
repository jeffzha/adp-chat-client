# Workbench fork boundary gate

The fork is mechanically audited against upstream revision
`186084bfddc42cc369c722cced95842dd83c305f` by
`.github/scripts/check_workbench_boundaries.py`.

Run before review:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 .github/scripts/check_workbench_boundaries.py \
  --repo . --report-json /tmp/adp-workbench-boundary.json
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover \
  -s .github/scripts/tests -p 'test_*.py' -v
```

The report records the complete baseline-existing patch list and line counts.
The gate rejects implementation work outside `server`, `client` and fork CI
ownership (the root README is the sole documentation exception); new-api
database/API-key/quota/relay dependencies; credential-like literals; frontend
Secret/provider-locator fields; and browser response, exception or log sinks
that contain Secret/provider-locator values. The Python AST check follows
same-function assignments and container mutation into indirect payloads, so
`payload = {"app_key": ...}; return sanic.json(payload)` is rejected as well
as a direct return. Generic names are scoped per function to avoid unrelated
false positives. Only the reviewed workbench projection/redaction functions
are modeled as sanitizer boundaries; adding another sanitizer requires a gate
change and regression fixture.

Normal CI has no ADR override environment. A protected manual exception requires
both `CLAW_CONSTRAINT_ADR_OVERRIDE=true` and an explicitly named
`CLAW_CONSTRAINT_ADR=server/WORKBENCH_ADR_<SLUG>.md`. The ADR must be approved,
dated, name every finding code, and contain substantive impact and rollback
fields. A one-line waiver is invalid.

Internal ADP-to-control APIs are POST-with-JSON contracts only. Their HMAC
canonical path is an exact, normalized `/api/internal/workbench/**` path;
queries, fragments, backslashes, empty segments and traversal segments are
rejected before network I/O. Add new filter inputs to the signed JSON body,
not to a query string, so independent ADP and claw-control releases cannot
silently disagree about the signature canonicalization.

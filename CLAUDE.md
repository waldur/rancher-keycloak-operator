# CLAUDE.md

## Project overview

`rancher-keycloak-operator` is a Python (kopf) Kubernetes operator that reconciles `ManagedRancherProject` CRDs against Rancher and Keycloak APIs. It manages the lifecycle of Rancher projects, Keycloak OIDC groups, role bindings, and user memberships across **multiple downstream clusters** managed by a single Rancher management server.

One operator instance connects to a **Rancher management server** (not a specific cluster). Each CR specifies its target cluster via `spec.clusterId`, enabling multi-cluster management from a single operator deployment.

This operator is designed to work with the Waldur site agent, which writes CRs instead of calling Rancher/Keycloak APIs directly.

## Repository structure

```
rancher-keycloak-operator/
├── pyproject.toml                          # Package config (kopf, httpx, kubernetes)
├── rancher_keycloak_operator/
│   ├── operator.py                         # kopf handlers (create/update/delete/timer/startup)
│   ├── reconciler.py                       # Core reconciliation logic (9 steps)
│   ├── rancher_client.py                   # Async Rancher v3 API client (httpx)
│   ├── keycloak_client.py                  # Async Keycloak admin API client (httpx)
│   └── discovery.py                        # Unmanaged project scanner + RancherProjectInventory
├── helm/rancher-keycloak-operator/
│   └── templates/crds/
│       ├── managedrancherproject.yaml      # ManagedRancherProject CRD
│       └── rancherprojectinventory.yaml    # RancherProjectInventory CRD
├── examples/
│   └── test-project.yaml                   # Example CR
└── tests/
    └── test_integration.py                 # 22 integration tests against real Rancher + Keycloak
```

## Development commands

```bash
# Install
uv sync --extra dev
uv pip install -e .

# Run operator locally
RANCHER_URL=... RANCHER_BEARER_TOKEN=... RANCHER_CLUSTER_ID=... \
KEYCLOAK_URL=... KEYCLOAK_REALM=... KEYCLOAK_USERNAME=... KEYCLOAK_PASSWORD=... \
  uv run kopf run --module rancher_keycloak_operator.operator --namespace=waldur-system --verbose

# Run integration tests (requires real Rancher + Keycloak access)
RANCHER_URL=... RANCHER_BEARER_TOKEN=... RANCHER_CLUSTER_ID=... \
KEYCLOAK_URL=... KEYCLOAK_REALM=... KEYCLOAK_USERNAME=... KEYCLOAK_PASSWORD=... \
  uv run pytest tests/test_integration.py -v -s

# Deploy CRDs to a cluster
kubectl apply -f helm/rancher-keycloak-operator/templates/crds/

# Lint
uv run ruff check .
```

## Key conventions

- **Async throughout**: all Rancher/Keycloak clients use `httpx.AsyncClient`. kopf handlers are `async def`.
- **Idempotent reconciliation**: every step is "ensure exists" not "create". Re-running reconcile produces the same result.
- **Non-fatal namespace errors**: if the Rancher cluster is unavailable for namespace operations (e.g., `updating` state), the condition is set to `False` but reconciliation continues with Keycloak steps.
- **Duplicate groupName merging**: when the same `groupName` appears in multiple `roleBindings`, members are merged into a union before syncing to prevent accidental removal.
- **Status conditions**: 5 conditions track each subsystem independently: `RancherProjectReady`, `NamespaceReady`, `KeycloakGroupsReady`, `RancherBindingsReady`, `MembershipSynced`.
- **Phase state machine**: `Pending → Creating → Ready ⇄ Updating → Error / Deleting`.
- **Cleanup via kopf delete handler**: cascading cleanup removes bindings → users from groups → groups → project. Resilient to already-deleted resources.

## Architecture notes

### Reconciliation sequence (reconciler.py)

1. Carry forward existing status (for incremental patches)
2. Ensure Rancher project (create or adopt by name)
3. Ensure namespace (non-fatal)
4. If keycloak enabled:
   a. Ensure parent group
   b. For each roleBinding: ensure child group → ensure Rancher PRTB → sync members
5. Set phase from conditions

### Client design

- `RancherClient`: wraps Rancher v3 REST API. Connects to the **management server** (not a specific cluster). `cluster_id` is passed per-method from `spec.clusterId`, enabling multi-cluster support. Methods: `create_project(cluster_id, ...)`, `find_project_by_name(cluster_id, ...)`, `list_all_projects(cluster_id)`, etc. Cluster-agnostic methods (e.g., `get_project(project_id)`) extract the cluster from the project ID.
- `KeycloakClient`: wraps Keycloak admin REST API directly (no `python-keycloak` dependency). Auto-refreshes access token on 401. Methods: `create_group`, `get_group_by_name`, `add_user_to_group`, `find_user`, etc.

### Discovery (discovery.py)

- `ProjectDiscovery.scan(cluster_id)`: lists all Rancher projects for a given cluster, compares against `ManagedRancherProject` CRs, returns diff
- System projects (`Default`, `System`) are excluded
- Results written to `RancherProjectInventory` CR status via `update_inventory_status()`
- Triggered by kopf timer every 5 minutes per `RancherProjectInventory` CR (each CR scans one cluster)

## Integration test categories

Tests run against real Rancher and Keycloak (not mocked):

| Category | Tests | What they validate |
|----------|-------|--------------------|
| Connectivity | 5 | API access to Rancher, Keycloak, user lookup |
| Full lifecycle | 1 | Create → add user → remove user → delete → verify cleanup |
| Idempotency | 1 | Reconcile twice = same IDs |
| Multi-user | 1 | Add 2 users, remove 1, verify other stays |
| Multi-role | 1 | Two roleBindings with different members |
| User multiple roles | 2 | Same user in two roles; remove from one, keep other |
| Duplicate handling | 2 | Duplicate user dedup; shared groupName member merging |
| Adopt existing | 1 | Pre-create project in Rancher, reconcile adopts it |
| Keycloak disabled | 1 | No KC operations when `keycloak.enabled=false` |
| Drift recovery | 1 | Externally delete KC group → reconcile recreates |
| Nonexistent user | 1 | Bad user ID doesn't crash; valid users still synced |
| Cleanup resilience | 1 | Cleanup when resources already deleted externally |
| Edge cases | 2 | Missing namespace spec; empty roleBindings |
| Discovery | 2 | Excludes system projects; detects manually created |

## Relationship to Waldur

This operator is part of the Waldur ecosystem but is not Waldur-specific:

- **CRD API group**: `waldur.io` — identifies the Waldur ecosystem
- **Rancher annotations**: `waldur/managed`, `waldur/organization` — for Waldur tracking
- **Site agent**: Waldur's `rancher-user-management` site agent plugin writes CRs instead of calling APIs directly
- **Multi-cluster**: one operator per Rancher management server; one Waldur offering = one Rancher server; each Waldur resource = one downstream cluster; each ResourceProject = one project within a cluster
- **Standalone**: can be used without Waldur by creating CRs manually or via any other tool

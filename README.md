# rancher-keycloak-operator

A Kubernetes operator that manages Rancher projects and Keycloak OIDC group bindings via Custom Resource Definitions.

## What it does

When you create a `ManagedRancherProject` CR, the operator:

1. Creates a Rancher project (or adopts an existing one) in the cluster specified by `spec.clusterId`
2. Creates a namespace in the project
3. Creates Keycloak groups (parent + child per role)
4. Binds Keycloak groups to the Rancher project via `ProjectRoleTemplateBinding`
5. Syncs user membership in Keycloak groups

When you update the CR (add/remove users, change quotas), the operator reconciles the diff. When you delete the CR, the operator cleans up all created resources.

## Multi-cluster architecture

One operator instance connects to a **Rancher management server** and can manage projects across **multiple downstream clusters**. Each CR specifies its target cluster via `spec.clusterId`.

```
Site Agent / kubectl                     Operator (kopf)
       │                                      │
       │  create/patch/delete                  │  watches
       ▼                                       ▼
┌─────────────────────┐              ┌──────────────────┐
│ ManagedRancherProject│─────────────│  Reconciler      │
│   spec.clusterId ────┼─────────┐  │                  │
│ (CRD on K8s)        │         │  │  ┌─────────────┐ │
└─────────────────────┘         └──┼──│RancherClient│──── Rancher mgmt server
                                   │  └─────────────┘ │   (serves N clusters)
                                   │  ┌──────────────┐│
                                   │  │KeycloakClient│── Keycloak Admin API
                                   │  └──────────────┘│
                                   └──────────────────┘
```

The operator also periodically scans for **unmanaged Rancher projects** per cluster via the `RancherProjectInventory` CRD.

## CRDs

### ManagedRancherProject (`waldur.io/v1alpha1`)

Declares the desired state of a Rancher project with Keycloak OIDC access.

```yaml
apiVersion: waldur.io/v1alpha1
kind: ManagedRancherProject
metadata:
  name: my-project
  namespace: waldur-system
spec:
  projectName: "waldur-my-project"
  clusterId: "c-m-abc123"
  description: "My project"
  organization: "my-org"
  projectSlug: "my-project"

  namespace:
    name: "waldur-my-project"
    labels:
      gpu-pool: "h100-2x"

  resourceQuotas:
    limits.cpu: "4000m"
    limits.memory: "8192Mi"

  keycloak:
    enabled: true
    parentGroupName: "c_abc123"
    roleBindings:
      - groupName: "project_my-project_workloads-manage"
        rancherRole: "workloads-manage"
        members:
          - userIdentifier: "keycloak-user-uuid"
            lookupByID: true
```

**Status** (set by operator):

```yaml
status:
  phase: Ready              # Pending | Creating | Ready | Updating | Error | Deleting
  rancherProjectId: "c-m-abc123:p-xyz789"
  namespaceName: "waldur-my-project"
  keycloakParentGroupId: "kc-group-uuid"
  keycloakRoleBindings:
    - groupName: "project_my-project_workloads-manage"
      keycloakGroupId: "kc-child-uuid"
      rancherBindingId: "prtb-id"
      memberCount: 1
      syncedMembers: ["keycloak-user-uuid"]
  conditions:
    - type: RancherProjectReady   # Project exists in Rancher
    - type: NamespaceReady        # Namespace exists
    - type: KeycloakGroupsReady   # All KC groups created
    - type: RancherBindingsReady  # All PRTBs created
    - type: MembershipSynced      # KC group members match spec
```

### RancherProjectInventory (`waldur.io/v1alpha1`)

Read-only singleton per cluster. Updated by the operator's discovery scan.

```yaml
apiVersion: waldur.io/v1alpha1
kind: RancherProjectInventory
metadata:
  name: cluster-c-m-abc123
  namespace: waldur-system
spec:
  clusterId: "c-m-abc123"
status:
  lastScanTime: "2026-04-22T10:00:00Z"
  managedProjects: 5
  unmanagedProjectCount: 2
  unmanagedProjects:
    - projectId: "c-m-abc123:p-unknown1"
      name: "manually-created"
      namespaces: ["ns1"]
```

## Quick start

### Prerequisites

- Python 3.10+
- Access to a Kubernetes cluster (for CRDs)
- Rancher API access (bearer token)
- Keycloak admin access

### Install

```bash
uv sync --extra dev
uv pip install -e .
```

### Deploy CRDs

```bash
kubectl apply -f helm/rancher-keycloak-operator/templates/crds/managedrancherproject.yaml
kubectl apply -f helm/rancher-keycloak-operator/templates/crds/rancherprojectinventory.yaml
kubectl create namespace waldur-system
```

### Run locally

```bash
export RANCHER_URL=https://rancher.example.com    # Management server URL
export RANCHER_BEARER_TOKEN=token-xxxxx:yyyyyy     # Server-level token (not cluster-specific)
export KEYCLOAK_URL=https://keycloak.example.com
export KEYCLOAK_REALM=myrealm
export KEYCLOAK_USERNAME=admin
export KEYCLOAK_PASSWORD=secret
export RANCHER_VERIFY_SSL=false
export KEYCLOAK_VERIFY_SSL=false

uv run kopf run --module rancher_keycloak_operator.operator --namespace=waldur-system --verbose
```

### Run tests

```bash
# Set env vars as above, plus RANCHER_CLUSTER_ID for test target cluster:
export RANCHER_CLUSTER_ID=c-m-abc123
uv run pytest tests/test_integration.py -v -s
```

## Reconciliation sequence

Each reconciliation is idempotent ("ensure exists", not "create"):

1. **Rancher Project** — create or adopt existing by name
2. **Namespace** — create in project (non-fatal if cluster unavailable)
3. **Resource Quotas** — apply to namespace
4. **Keycloak Parent Group** — ensure exists
5. **Keycloak Child Groups** — one per roleBinding
6. **Rancher Bindings** — `ProjectRoleTemplateBinding` per group
7. **Membership Sync** — diff desired vs actual, add/remove users
8. **Periodic resync** — every 5 minutes, re-reconcile for drift detection
9. **Deletion** — cascading cleanup via kopf's delete handler

## Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `RANCHER_URL` | Yes | — | Rancher management server URL |
| `RANCHER_BEARER_TOKEN` | Yes | — | Server-level API bearer token |
| `RANCHER_VERIFY_SSL` | No | `true` | Verify Rancher TLS |
| `KEYCLOAK_URL` | No | — | Keycloak base URL (omit to disable) |
| `KEYCLOAK_REALM` | If KC | — | Keycloak realm for managed groups |
| `KEYCLOAK_USERNAME` | If KC | — | Keycloak admin username |
| `KEYCLOAK_PASSWORD` | If KC | — | Keycloak admin password |
| `KEYCLOAK_USER_REALM` | No | `master` | Realm for admin authentication |
| `KEYCLOAK_VERIFY_SSL` | No | `true` | Verify Keycloak TLS |
| `CR_NAMESPACE` | No | `waldur-system` | Namespace for CRs |

**Note:** There is no `RANCHER_CLUSTER_ID` env var. Each CR specifies its target cluster via `spec.clusterId`. One operator instance serves all clusters managed by the Rancher server.

## Duplicate handling

- **Same user twice in one roleBinding**: automatically deduplicated (set-based)
- **Same groupName across roleBindings**: member lists are merged into a union before syncing, so no roleBinding accidentally removes another's members. A warning is logged.

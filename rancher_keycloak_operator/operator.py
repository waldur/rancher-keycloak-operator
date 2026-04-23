"""Kopf handlers for ManagedRancherProject CRD.

The operator connects to a Rancher management server (not a specific cluster).
Each CR specifies its target cluster via spec.clusterId.
"""

import logging
import os

import kopf

from rancher_keycloak_operator.discovery import ProjectDiscovery
from rancher_keycloak_operator.keycloak_client import KeycloakClient
from rancher_keycloak_operator.rancher_client import RancherClient
from rancher_keycloak_operator.reconciler import Reconciler

logger = logging.getLogger(__name__)

# Clients initialized on startup
_reconciler: Reconciler | None = None
_discovery: ProjectDiscovery | None = None


def _get_reconciler() -> Reconciler:
    global _reconciler
    if _reconciler is None:
        # Rancher management server config (no cluster_id — per-CR)
        rancher = RancherClient(
            api_url=os.environ["RANCHER_URL"],
            bearer_token=os.environ["RANCHER_BEARER_TOKEN"],
            verify_ssl=os.environ.get("RANCHER_VERIFY_SSL", "true").lower() == "true",
        )

        # Keycloak config (optional)
        keycloak = None
        if os.environ.get("KEYCLOAK_URL"):
            keycloak = KeycloakClient(
                server_url=os.environ["KEYCLOAK_URL"],
                realm=os.environ["KEYCLOAK_REALM"],
                username=os.environ["KEYCLOAK_USERNAME"],
                password=os.environ["KEYCLOAK_PASSWORD"],
                user_realm=os.environ.get("KEYCLOAK_USER_REALM", "master"),
                verify_ssl=os.environ.get("KEYCLOAK_VERIFY_SSL", "true").lower() == "true",
            )

        _reconciler = Reconciler(rancher=rancher, keycloak=keycloak)
    return _reconciler


@kopf.on.create("waldur.io", "v1alpha1", "managedrancherprojects")
async def on_create(spec, status, patch, **_kwargs):
    """Full reconciliation on CR creation."""
    logger.info("Creating ManagedRancherProject: %s", spec.get("projectName"))
    reconciler = _get_reconciler()
    await reconciler.reconcile(spec, status, patch)


@kopf.on.update("waldur.io", "v1alpha1", "managedrancherprojects")
async def on_update(spec, status, patch, diff, **_kwargs):
    """Incremental reconciliation on CR update."""
    logger.info("Updating ManagedRancherProject: %s (diff: %s)", spec.get("projectName"), diff)
    reconciler = _get_reconciler()
    await reconciler.reconcile(spec, status, patch)


@kopf.on.delete("waldur.io", "v1alpha1", "managedrancherprojects")
async def on_delete(spec, status, **_kwargs):
    """Cascading cleanup on CR deletion."""
    logger.info("Deleting ManagedRancherProject: %s", spec.get("projectName"))
    reconciler = _get_reconciler()
    await reconciler.cleanup(spec, status)


@kopf.timer("waldur.io", "v1alpha1", "managedrancherprojects", interval=300)
async def periodic_resync(spec, status, patch, **_kwargs):
    """Periodic drift detection — re-reconcile every 5 minutes."""
    logger.debug("Periodic resync for: %s", spec.get("projectName"))
    reconciler = _get_reconciler()
    await reconciler.reconcile(spec, status, patch)


# --- Discovery: detect unmanaged Rancher projects ---


def _get_discovery() -> ProjectDiscovery:
    global _discovery
    if _discovery is None:
        reconciler = _get_reconciler()
        namespace = os.environ.get("CR_NAMESPACE", "waldur-system")
        _discovery = ProjectDiscovery(
            rancher=reconciler.rancher,
            namespace=namespace,
        )
    return _discovery


@kopf.timer("waldur.io", "v1alpha1", "rancherprojectinventories", interval=300)
async def discovery_scan(spec, patch, **_kwargs):
    """Periodic scan for unmanaged Rancher projects.

    Each RancherProjectInventory CR specifies which cluster to scan
    via spec.clusterId.
    """
    cluster_id = spec.get("clusterId", "")
    if not cluster_id:
        return

    logger.info("Running discovery scan for cluster %s", cluster_id)
    discovery = _get_discovery()
    status = await discovery.scan(cluster_id)
    discovery.update_inventory_status(cluster_id, status)

    unmanaged_count = status.get("unmanagedProjectCount", 0)
    if unmanaged_count > 0:
        names = [p["name"] for p in status.get("unmanagedProjects", [])]
        logger.info("Found %d unmanaged projects: %s", unmanaged_count, names)

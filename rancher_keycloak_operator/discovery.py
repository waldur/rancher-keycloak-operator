"""Discover unmanaged Rancher projects and report via RancherProjectInventory CR."""

import logging
from datetime import datetime, timezone

from kubernetes import client as k8s_client
from kubernetes import config as k8s_config
from kubernetes.client.rest import ApiException

from rancher_keycloak_operator.rancher_client import RancherClient

logger = logging.getLogger(__name__)

RPI_API_GROUP = "waldur.io"
RPI_API_VERSION = "v1alpha1"
RPI_PLURAL = "rancherprojectinventories"

# System projects that should never appear as "unmanaged"
SYSTEM_PROJECT_NAMES = {"Default", "System"}


class ProjectDiscovery:
    """Discovers unmanaged Rancher projects by comparing Rancher state against CRs."""

    def __init__(self, rancher: RancherClient, namespace: str = "waldur-system"):
        self.rancher = rancher
        self.namespace = namespace

        try:
            k8s_config.load_incluster_config()
        except k8s_config.ConfigException:
            k8s_config.load_kube_config()

        self.custom_api = k8s_client.CustomObjectsApi()

    def _get_managed_project_ids(self) -> set[str]:
        """Get all Rancher project IDs that are managed by ManagedRancherProject CRs."""
        try:
            result = self.custom_api.list_namespaced_custom_object(
                group=RPI_API_GROUP,
                version=RPI_API_VERSION,
                namespace=self.namespace,
                plural="managedrancherprojects",
            )
            managed_ids = set()
            for item in result.get("items", []):
                project_id = item.get("status", {}).get("rancherProjectId", "")
                if project_id:
                    managed_ids.add(project_id)
            return managed_ids
        except ApiException as e:
            logger.warning("Failed to list ManagedRancherProject CRs: %s", e)
            return set()

    async def scan(self, cluster_id: str) -> dict:
        """Scan a Rancher cluster for all projects and identify unmanaged ones.

        Returns a status dict suitable for patching a RancherProjectInventory CR.
        """
        # Get all projects from the specified cluster
        all_projects = await self.rancher.list_all_projects(cluster_id)

        # Get managed project IDs from CRs
        managed_ids = self._get_managed_project_ids()

        unmanaged = []
        for project in all_projects:
            name = project.get("name", "")
            project_id = project.get("id", "")

            # Skip system projects
            if name in SYSTEM_PROJECT_NAMES:
                continue

            # Skip managed projects
            if project_id in managed_ids:
                continue

            # Get namespaces for this project
            namespaces = []
            try:
                namespaces = await self.rancher.get_project_namespaces(project_id)
            except Exception:
                pass  # Namespace listing may fail for some projects

            unmanaged.append({
                "projectId": project_id,
                "name": name,
                "description": project.get("description", ""),
                "namespaces": namespaces,
                "createdAt": project.get("created", ""),
            })

        return {
            "lastScanTime": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "managedProjects": len(managed_ids),
            "unmanagedProjectCount": len(unmanaged),
            "unmanagedProjects": unmanaged,
        }

    def ensure_inventory_cr(self, cluster_id: str) -> None:
        """Create the RancherProjectInventory CR if it doesn't exist."""
        name = f"cluster-{cluster_id.replace(':', '-')}"
        try:
            self.custom_api.get_namespaced_custom_object(
                group=RPI_API_GROUP,
                version=RPI_API_VERSION,
                namespace=self.namespace,
                plural=RPI_PLURAL,
                name=name,
            )
        except ApiException as e:
            if e.status == 404:
                body = {
                    "apiVersion": f"{RPI_API_GROUP}/{RPI_API_VERSION}",
                    "kind": "RancherProjectInventory",
                    "metadata": {"name": name, "namespace": self.namespace},
                    "spec": {"clusterId": cluster_id},
                }
                self.custom_api.create_namespaced_custom_object(
                    group=RPI_API_GROUP,
                    version=RPI_API_VERSION,
                    namespace=self.namespace,
                    plural=RPI_PLURAL,
                    body=body,
                )
                logger.info("Created RancherProjectInventory CR: %s", name)

    def update_inventory_status(self, cluster_id: str, status: dict) -> None:
        """Patch the RancherProjectInventory CR status."""
        name = f"cluster-{cluster_id.replace(':', '-')}"
        try:
            self.custom_api.patch_namespaced_custom_object_status(
                group=RPI_API_GROUP,
                version=RPI_API_VERSION,
                namespace=self.namespace,
                plural=RPI_PLURAL,
                name=name,
                body={"status": status},
            )
            logger.info(
                "Updated inventory: %d managed, %d unmanaged",
                status.get("managedProjects", 0),
                status.get("unmanagedProjectCount", 0),
            )
        except ApiException as e:
            logger.error("Failed to update inventory status: %s", e)

"""Async Rancher v3 API client for managing projects and bindings.

This client connects to a Rancher management server (not a specific cluster).
Cluster IDs are passed per-method, allowing one client to manage multiple clusters.
"""

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class RancherClient:
    """Async Rancher API client using httpx."""

    def __init__(
        self,
        api_url: str,
        bearer_token: str,
        verify_ssl: bool = True,
    ):
        self.api_url = api_url.rstrip("/")
        self.v3_url = f"{self.api_url}/v3"
        self._client = httpx.AsyncClient(
            verify=verify_ssl,
            timeout=60.0,
            headers={
                "Authorization": f"Bearer {bearer_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

    async def ping(self) -> bool:
        """Check Rancher management server connectivity."""
        try:
            resp = await self._client.get(self.v3_url)
            return resp.status_code == 200
        except Exception as e:
            logger.error("Rancher ping failed: %s", e)
            return False

    # --- Project operations ---

    async def list_all_projects(self, cluster_id: str) -> list[dict]:
        """List ALL projects in a cluster (for discovery)."""
        resp = await self._client.get(
            f"{self.v3_url}/projects", params={"clusterId": cluster_id}
        )
        resp.raise_for_status()
        return resp.json().get("data", [])

    async def find_project_by_name(self, cluster_id: str, name: str) -> Optional[dict]:
        """Find a project by name in a cluster."""
        projects = await self.list_all_projects(cluster_id)
        for p in projects:
            if p.get("name") == name:
                return p
        return None

    async def get_project(self, project_id: str) -> Optional[dict]:
        resp = await self._client.get(f"{self.v3_url}/projects/{project_id}")
        if resp.status_code == 200:
            return resp.json()
        return None

    async def create_project(
        self,
        cluster_id: str,
        name: str,
        description: str = "",
        organization: str = "",
        project_slug: str = "",
    ) -> str:
        """Create a Rancher project in a cluster. Returns the project ID."""
        body = {
            "type": "project",
            "clusterId": cluster_id,
            "name": name,
            "description": description,
            "annotations": {
                "waldur/organization": organization,
                "waldur/managed": "true",
                "waldur/project_slug": project_slug or "unknown",
            },
        }
        resp = await self._client.post(f"{self.v3_url}/projects", json=body)
        resp.raise_for_status()
        project_id = resp.json().get("id", "")
        logger.info("Created Rancher project: %s (ID: %s)", name, project_id)
        return project_id

    async def delete_project(self, project_id: str) -> None:
        resp = await self._client.delete(f"{self.v3_url}/projects/{project_id}")
        if resp.status_code == 404:
            logger.warning("Project %s not found, skipping deletion", project_id)
            return
        resp.raise_for_status()
        logger.info("Deleted Rancher project: %s", project_id)

    # --- Namespace operations ---

    async def create_namespace(
        self,
        project_id: str,
        namespace: str,
        extra_labels: Optional[dict[str, str]] = None,
    ) -> str:
        """Create a namespace in a project. Cluster ID is extracted from project_id."""
        cluster_id = project_id.split(":")[0]
        labels = {
            "pod-security.kubernetes.io/enforce": "restricted",
            "pod-security.kubernetes.io/enforce-version": "latest",
        }
        if extra_labels:
            labels.update(extra_labels)

        body = {
            "type": "namespace",
            "name": namespace,
            "projectId": project_id,
            "labels": labels,
            "annotations": {"waldur/managed": "true"},
        }
        resp = await self._client.post(
            f"{self.v3_url}/clusters/{cluster_id}/namespaces", json=body
        )
        resp.raise_for_status()
        ns_id = resp.json().get("id", "")
        logger.info("Created namespace %s in project %s", namespace, project_id)
        return ns_id

    async def get_project_namespaces(self, project_id: str) -> list[str]:
        """List namespaces in a project. Cluster ID is extracted from project_id."""
        cluster_id = project_id.split(":")[0]
        resp = await self._client.get(
            f"{self.v3_url}/clusters/{cluster_id}/namespaces",
            params={"projectId": project_id},
        )
        resp.raise_for_status()
        return [item.get("name", "") for item in resp.json().get("data", [])]

    # --- ProjectRoleTemplateBinding operations ---

    async def get_project_group_bindings(
        self, project_id: str, group_principal_id: str = "", role: str = ""
    ) -> list[dict]:
        params: dict[str, str] = {"projectId": project_id}
        if group_principal_id:
            params["groupPrincipalId"] = group_principal_id
        if role:
            params["roleTemplateId"] = role
        resp = await self._client.get(
            f"{self.v3_url}/projectroletemplatebindings", params=params
        )
        resp.raise_for_status()
        return resp.json().get("data", [])

    async def create_project_group_binding(
        self, project_id: str, group_principal_id: str, role: str
    ) -> str:
        body = {
            "type": "projectRoleTemplateBinding",
            "roleTemplateId": role,
            "projectId": project_id,
            "groupPrincipalId": group_principal_id,
        }
        resp = await self._client.post(
            f"{self.v3_url}/projectroletemplatebindings", json=body
        )
        resp.raise_for_status()
        binding_id = resp.json().get("id", "")
        logger.info(
            "Created binding: %s → %s (role: %s, id: %s)",
            group_principal_id, project_id, role, binding_id,
        )
        return binding_id

    async def delete_project_group_binding(self, binding_id: str) -> None:
        resp = await self._client.delete(
            f"{self.v3_url}/projectroletemplatebindings/{binding_id}"
        )
        if resp.status_code == 404:
            logger.warning("Binding %s not found, skipping", binding_id)
            return
        resp.raise_for_status()
        logger.info("Deleted binding: %s", binding_id)

    async def close(self):
        await self._client.aclose()

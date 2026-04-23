"""Async Keycloak client for managing groups and user memberships."""

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class KeycloakClient:
    """Async Keycloak admin client using httpx."""

    def __init__(
        self,
        server_url: str,
        realm: str,
        username: str,
        password: str,
        user_realm: str = "master",
        client_id: str = "admin-cli",
        verify_ssl: bool = True,
    ):
        self.server_url = server_url.rstrip("/")
        self.realm = realm
        self.username = username
        self.password = password
        self.user_realm = user_realm
        self.client_id = client_id
        self._token: Optional[str] = None
        self._client = httpx.AsyncClient(verify=verify_ssl, timeout=60.0)

    @property
    def _admin_url(self) -> str:
        return f"{self.server_url}/admin/realms/{self.realm}"

    async def _get_token(self) -> str:
        """Get or refresh admin access token."""
        url = f"{self.server_url}/realms/{self.user_realm}/protocol/openid-connect/token"
        resp = await self._client.post(
            url,
            data={
                "grant_type": "password",
                "client_id": self.client_id,
                "username": self.username,
                "password": self.password,
            },
        )
        resp.raise_for_status()
        self._token = resp.json()["access_token"]
        return self._token

    async def _headers(self) -> dict:
        if not self._token:
            await self._get_token()
        return {"Authorization": f"Bearer {self._token}"}

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Make an authenticated request, refreshing token on 401."""
        url = f"{self._admin_url}{path}"
        headers = await self._headers()
        resp = await self._client.request(method, url, headers=headers, **kwargs)
        if resp.status_code == 401:
            await self._get_token()
            headers = await self._headers()
            resp = await self._client.request(method, url, headers=headers, **kwargs)
        return resp

    async def ping(self) -> bool:
        try:
            resp = await self._request("GET", "")
            return resp.status_code == 200
        except Exception as e:
            logger.error("Keycloak ping failed: %s", e)
            return False

    # --- User operations ---

    async def find_user_by_id(self, user_id: str) -> Optional[dict]:
        resp = await self._request("GET", f"/users/{user_id}")
        if resp.status_code == 200:
            return resp.json()
        return None

    async def find_user_by_username(self, username: str) -> Optional[dict]:
        resp = await self._request("GET", "/users", params={"username": username, "exact": "true"})
        if resp.status_code == 200:
            users = resp.json()
            return users[0] if users else None
        return None

    async def find_user(self, identifier: str, by_id: bool = True) -> Optional[dict]:
        if by_id:
            return await self.find_user_by_id(identifier)
        return await self.find_user_by_username(identifier)

    # --- Group operations ---

    async def get_groups(self, search: str = "") -> list[dict]:
        params = {"max": "100"}
        if search:
            params["search"] = search
        resp = await self._request("GET", "/groups", params=params)
        resp.raise_for_status()
        return resp.json()

    async def get_group_by_name(self, name: str) -> Optional[dict]:
        """Find a group by exact name (searches top-level and one level of subgroups)."""
        groups = await self.get_groups(search=name)
        for group in groups:
            if group.get("name") == name:
                return group
            for sub in group.get("subGroups", []):
                if sub.get("name") == name:
                    return sub
        return None

    async def get_group_by_id(self, group_id: str) -> Optional[dict]:
        resp = await self._request("GET", f"/groups/{group_id}")
        if resp.status_code == 200:
            return resp.json()
        return None

    async def create_group(
        self, name: str, description: str = "", parent_id: Optional[str] = None
    ) -> str:
        """Create a group. Returns the group ID."""
        body = {
            "name": name,
            "attributes": {
                "description": [description] if description else [],
                "managed_by": ["rancher-keycloak-operator"],
            },
        }
        if parent_id:
            resp = await self._request("POST", f"/groups/{parent_id}/children", json=body)
        else:
            resp = await self._request("POST", "/groups", json=body)
        resp.raise_for_status()

        # Keycloak returns the group ID in the Location header
        location = resp.headers.get("Location", "")
        group_id = location.rsplit("/", 1)[-1] if location else ""

        if not group_id:
            # Fallback: look up the group we just created
            created = await self.get_group_by_name(name)
            group_id = created["id"] if created else ""

        logger.info("Created Keycloak group: %s (ID: %s)", name, group_id)
        return group_id

    async def delete_group(self, group_id: str) -> None:
        resp = await self._request("DELETE", f"/groups/{group_id}")
        if resp.status_code == 404:
            logger.warning("Group %s not found, skipping deletion", group_id)
            return
        resp.raise_for_status()
        logger.info("Deleted Keycloak group: %s", group_id)

    # --- Membership operations ---

    async def get_group_members(self, group_id: str) -> list[dict]:
        resp = await self._request("GET", f"/groups/{group_id}/members")
        if resp.status_code == 200:
            return resp.json()
        return []

    async def add_user_to_group(self, user_id: str, group_id: str) -> None:
        resp = await self._request("PUT", f"/users/{user_id}/groups/{group_id}")
        resp.raise_for_status()
        logger.info("Added user %s to group %s", user_id, group_id)

    async def remove_user_from_group(self, user_id: str, group_id: str) -> None:
        resp = await self._request("DELETE", f"/users/{user_id}/groups/{group_id}")
        resp.raise_for_status()
        logger.info("Removed user %s from group %s", user_id, group_id)

    async def close(self):
        await self._client.aclose()

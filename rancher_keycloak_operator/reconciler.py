"""Core reconciliation logic for ManagedRancherProject CRs."""

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from rancher_keycloak_operator.keycloak_client import KeycloakClient
from rancher_keycloak_operator.rancher_client import RancherClient

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _condition(
    ctype: str, status: str, reason: str, message: str = ""
) -> dict[str, str]:
    return {
        "type": ctype,
        "status": status,
        "lastTransitionTime": _now_iso(),
        "reason": reason,
        "message": message,
    }


def _set_condition(conditions: list[dict], new: dict) -> list[dict]:
    """Upsert a condition by type."""
    result = [c for c in conditions if c.get("type") != new["type"]]
    result.append(new)
    return result


class Reconciler:
    """Reconciles ManagedRancherProject CRs against Rancher and Keycloak."""

    def __init__(self, rancher: RancherClient, keycloak: Optional[KeycloakClient]):
        self.rancher = rancher
        self.keycloak = keycloak

    async def reconcile(self, spec: dict, status: dict, patch: Any) -> None:
        """Full reconciliation: project → namespace → groups → bindings → members."""
        # Carry forward existing status values so patches are incremental
        for key, value in status.items():
            if key not in patch.status:
                patch.status[key] = value

        conditions = list(status.get("conditions", []))
        phase = "Creating" if not status.get("rancherProjectId") else "Updating"
        patch.status["phase"] = phase

        try:
            # Step 1: Rancher Project
            project_id = await self._ensure_rancher_project(spec, status, patch, conditions)

            # Step 2: Namespace
            await self._ensure_namespace(spec, project_id, patch, conditions)

            # Step 3: Keycloak groups + bindings + members
            kc_spec = spec.get("keycloak", {})
            if kc_spec.get("enabled", True) and self.keycloak:
                await self._ensure_keycloak(kc_spec, project_id, patch, conditions)

            # Determine final phase from conditions
            all_true = all(
                c.get("status") == "True"
                for c in conditions
                if c.get("type") != "Reconciled"
            )
            patch.status["phase"] = "Ready" if all_true else "Updating"
            patch.status["conditions"] = conditions
            patch.status["lastReconcileTime"] = _now_iso()

        except Exception as e:
            logger.exception("Reconciliation failed: %s", e)
            conditions = _set_condition(
                conditions,
                _condition("Reconciled", "False", "Error", str(e)),
            )
            patch.status["phase"] = "Error"
            patch.status["conditions"] = conditions
            raise

    async def cleanup(self, spec: dict, status: dict) -> None:
        """Cascading cleanup on CR deletion."""
        logger.info("Cleaning up resources for project %s", spec.get("projectName"))

        # Remove Rancher bindings
        for rb in status.get("keycloakRoleBindings", []):
            binding_id = rb.get("rancherBindingId")
            if binding_id:
                try:
                    await self.rancher.delete_project_group_binding(binding_id)
                except Exception as e:
                    logger.warning("Failed to delete binding %s: %s", binding_id, e)

        # Remove users from Keycloak groups and delete groups
        if self.keycloak:
            for rb in status.get("keycloakRoleBindings", []):
                group_id = rb.get("keycloakGroupId")
                if not group_id:
                    continue
                # Remove all members first
                for user_id in rb.get("syncedMembers", []):
                    try:
                        await self.keycloak.remove_user_from_group(user_id, group_id)
                    except Exception as e:
                        logger.warning("Failed to remove user %s: %s", user_id, e)
                # Delete the group
                try:
                    await self.keycloak.delete_group(group_id)
                except Exception as e:
                    logger.warning("Failed to delete group %s: %s", group_id, e)

            # Delete parent group if empty
            parent_id = status.get("keycloakParentGroupId")
            if parent_id:
                try:
                    parent = await self.keycloak.get_group_by_id(parent_id)
                    if parent and len(parent.get("subGroups", [])) == 0:
                        await self.keycloak.delete_group(parent_id)
                except Exception as e:
                    logger.warning("Failed to delete parent group: %s", e)

        # Delete Rancher project
        project_id = status.get("rancherProjectId")
        if project_id:
            try:
                await self.rancher.delete_project(project_id)
            except Exception as e:
                logger.warning("Failed to delete Rancher project %s: %s", project_id, e)

        logger.info("Cleanup complete for %s", spec.get("projectName"))

    # --- Internal reconciliation steps ---

    async def _ensure_rancher_project(
        self, spec: dict, status: dict, patch: Any, conditions: list[dict]
    ) -> str:
        """Step 1: Ensure Rancher project exists."""
        project_id = status.get("rancherProjectId", "")

        if project_id:
            # Verify it still exists
            project = await self.rancher.get_project(project_id)
            if project:
                conditions[:] = _set_condition(
                    conditions,
                    _condition("RancherProjectReady", "True", "ProjectExists"),
                )
                patch.status["conditions"] = conditions
                return project_id
            else:
                logger.warning("Project %s disappeared, recreating", project_id)
                project_id = ""

        # Try to adopt existing project by name
        cluster_id = spec["clusterId"]
        project_name = spec["projectName"]
        existing = await self.rancher.find_project_by_name(cluster_id, project_name)
        if existing:
            project_id = existing["id"]
            logger.info("Adopted existing project: %s (ID: %s)", project_name, project_id)
        else:
            # Create new
            project_id = await self.rancher.create_project(
                cluster_id=cluster_id,
                name=project_name,
                description=spec.get("description", ""),
                organization=spec.get("organization", ""),
                project_slug=spec.get("projectSlug", ""),
            )

        patch.status["rancherProjectId"] = project_id
        conditions[:] = _set_condition(
            conditions,
            _condition("RancherProjectReady", "True", "ProjectCreated"),
        )
        patch.status["conditions"] = conditions
        return project_id

    async def _ensure_namespace(
        self, spec: dict, project_id: str, patch: Any, conditions: list[dict]
    ) -> None:
        """Step 2: Ensure namespace exists in the Rancher project.

        Non-fatal: if the cluster is unavailable (e.g., updating state),
        the namespace step is retried on the next reconciliation.
        """
        ns_spec = spec.get("namespace", {})
        ns_name = ns_spec.get("name", spec["projectName"])

        try:
            # Check if namespace already exists
            existing_ns = await self.rancher.get_project_namespaces(project_id)
            if ns_name in existing_ns:
                patch.status["namespaceName"] = ns_name
                conditions[:] = _set_condition(
                    conditions,
                    _condition("NamespaceReady", "True", "NamespaceExists"),
                )
                patch.status["conditions"] = conditions
                return

            # Create namespace
            await self.rancher.create_namespace(
                project_id, ns_name, extra_labels=ns_spec.get("labels")
            )
            patch.status["namespaceName"] = ns_name
            conditions[:] = _set_condition(
                conditions,
                _condition("NamespaceReady", "True", "NamespaceCreated"),
            )
        except Exception as e:
            logger.warning("Namespace operation failed (will retry): %s", e)
            conditions[:] = _set_condition(
                conditions,
                _condition("NamespaceReady", "False", "NamespaceError", str(e)),
            )
        patch.status["conditions"] = conditions

    async def _ensure_keycloak(
        self, kc_spec: dict, project_id: str, patch: Any, conditions: list[dict]
    ) -> None:
        """Steps 3-5: Keycloak groups, Rancher bindings, membership sync."""
        assert self.keycloak is not None

        # Step 3: Parent group
        parent_name = kc_spec.get("parentGroupName", "")
        parent_id = ""
        if parent_name:
            parent = await self.keycloak.get_group_by_name(parent_name)
            if parent:
                parent_id = parent["id"]
            else:
                parent_id = await self.keycloak.create_group(
                    parent_name, "Cluster access group"
                )
            patch.status["keycloakParentGroupId"] = parent_id

        # Step 4-5: Role bindings (child groups + Rancher bindings + members)
        #
        # When the same groupName appears in multiple roleBindings, merge their
        # members before syncing — otherwise the second binding's sync would
        # remove members that belong to the first binding.
        role_bindings = kc_spec.get("roleBindings", [])
        merged = self._merge_role_bindings(role_bindings)

        role_bindings_status = []
        all_groups_ok = True
        all_bindings_ok = True
        all_members_ok = True

        for rb_spec in merged:
            rb_status = await self._ensure_role_binding(
                rb_spec, parent_id, project_id
            )
            role_bindings_status.append(rb_status)
            if not rb_status.get("keycloakGroupId"):
                all_groups_ok = False
            if not rb_status.get("rancherBindingId"):
                all_bindings_ok = False
            if rb_status.get("_members_error"):
                all_members_ok = False

        patch.status["keycloakRoleBindings"] = [
            {k: v for k, v in rb.items() if not k.startswith("_")}
            for rb in role_bindings_status
        ]

        conditions[:] = _set_condition(
            conditions,
            _condition(
                "KeycloakGroupsReady",
                "True" if all_groups_ok else "False",
                "GroupsReady" if all_groups_ok else "GroupError",
            ),
        )
        conditions[:] = _set_condition(
            conditions,
            _condition(
                "RancherBindingsReady",
                "True" if all_bindings_ok else "False",
                "BindingsReady" if all_bindings_ok else "BindingError",
            ),
        )
        conditions[:] = _set_condition(
            conditions,
            _condition(
                "MembershipSynced",
                "True" if all_members_ok else "False",
                "MembersInSync" if all_members_ok else "MemberSyncError",
            ),
        )
        patch.status["conditions"] = conditions

    @staticmethod
    def _merge_role_bindings(role_bindings: list[dict]) -> list[dict]:
        """Merge roleBindings that share the same groupName.

        When the same Keycloak group is referenced by multiple roleBindings
        (with different Rancher roles), their member lists must be merged so
        that the membership sync sees the full desired set.  Each unique
        (groupName, rancherRole) pair produces one output entry.
        """
        from collections import OrderedDict

        # Key: (groupName, rancherRole) → merged spec
        seen: OrderedDict[tuple[str, str], dict] = OrderedDict()
        # Track all members for a given groupName (across roles)
        group_members: dict[str, dict[str, dict]] = {}  # groupName → {uid: member}

        for rb in role_bindings:
            gn = rb["groupName"]
            role = rb["rancherRole"]
            key = (gn, role)

            if key not in seen:
                import copy
                seen[key] = copy.deepcopy(rb)
            else:
                # Merge members into existing entry
                existing_uids = {
                    m["userIdentifier"] for m in seen[key].get("members", [])
                }
                for m in rb.get("members", []):
                    if m["userIdentifier"] not in existing_uids:
                        seen[key].setdefault("members", []).append(m)

            # Also accumulate all members for the groupName (cross-role)
            if gn not in group_members:
                group_members[gn] = {}
            for m in rb.get("members", []):
                group_members[gn][m["userIdentifier"]] = m

        # For groups referenced by multiple roles, each role's entry needs
        # the FULL member set (union across all roles) for correct sync.
        result = list(seen.values())
        group_names_with_multiple_roles = set()
        role_count: dict[str, int] = {}
        for gn, role in seen:
            role_count[gn] = role_count.get(gn, 0) + 1
        for gn, count in role_count.items():
            if count > 1:
                group_names_with_multiple_roles.add(gn)
                logger.warning(
                    "GroupName '%s' is used by %d roleBindings — members are merged",
                    gn, count,
                )

        for entry in result:
            gn = entry["groupName"]
            if gn in group_names_with_multiple_roles:
                # Replace members with the union across all roles for this group
                entry["members"] = list(group_members[gn].values())

        return result

    async def _ensure_role_binding(
        self, rb_spec: dict, parent_id: str, project_id: str
    ) -> dict:
        """Ensure a single role binding: Keycloak group + Rancher binding + members."""
        assert self.keycloak is not None
        group_name = rb_spec["groupName"]
        rancher_role = rb_spec["rancherRole"]
        result: dict[str, Any] = {
            "groupName": group_name,
            "keycloakGroupId": "",
            "rancherBindingId": "",
            "memberCount": 0,
            "syncedMembers": [],
        }

        # Ensure Keycloak child group
        try:
            group = await self.keycloak.get_group_by_name(group_name)
            if group:
                group_id = group["id"]
            else:
                group_id = await self.keycloak.create_group(
                    group_name,
                    rb_spec.get("description", ""),
                    parent_id=parent_id or None,
                )
            result["keycloakGroupId"] = group_id
        except Exception as e:
            logger.error("Failed to ensure group %s: %s", group_name, e)
            return result

        # Ensure Rancher ProjectRoleTemplateBinding
        group_principal_id = f"keycloakoidc_group://{group_name}"
        try:
            existing = await self.rancher.get_project_group_bindings(
                project_id, group_principal_id, rancher_role
            )
            if existing:
                result["rancherBindingId"] = existing[0]["id"]
            else:
                binding_id = await self.rancher.create_project_group_binding(
                    project_id, group_principal_id, rancher_role
                )
                result["rancherBindingId"] = binding_id
        except Exception as e:
            logger.error("Failed to ensure binding for %s: %s", group_name, e)

        # Sync members
        desired_members = {
            m["userIdentifier"] for m in rb_spec.get("members", [])
        }
        lookup_by_id = {
            m["userIdentifier"]: m.get("lookupByID", True)
            for m in rb_spec.get("members", [])
        }

        try:
            current_kc_members = await self.keycloak.get_group_members(group_id)
            current_ids = {m["id"] for m in current_kc_members}

            # Add missing members
            to_add = desired_members - current_ids
            for uid in to_add:
                by_id = lookup_by_id.get(uid, True)
                user = await self.keycloak.find_user(uid, by_id=by_id)
                if user:
                    await self.keycloak.add_user_to_group(user["id"], group_id)
                else:
                    logger.warning("User %s not found in Keycloak", uid)

            # Remove extra members
            to_remove = current_ids - desired_members
            for uid in to_remove:
                await self.keycloak.remove_user_from_group(uid, group_id)

            # Re-read actual state
            actual_members = await self.keycloak.get_group_members(group_id)
            result["syncedMembers"] = [m["id"] for m in actual_members]
            result["memberCount"] = len(actual_members)

        except Exception as e:
            logger.error("Failed to sync members for %s: %s", group_name, e)
            result["_members_error"] = str(e)

        return result

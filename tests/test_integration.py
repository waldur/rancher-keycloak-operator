"""Comprehensive integration tests against real Rancher and Keycloak.

Test plan covers:
  - Connectivity validation
  - Happy path: full CRUD lifecycle
  - Idempotency: reconcile twice = same result
  - Multi-user: add/remove multiple users independently
  - Multi-role: multiple roleBindings in single CR
  - Adopt existing: reconcile against pre-existing Rancher project
  - Keycloak disabled: project without Keycloak groups
  - Drift recovery: external deletion of resources
  - Nonexistent user: user ID not found in Keycloak
  - Cleanup resilience: cleanup when resources already gone
  - Discovery: unmanaged project detection

Run with:
    RANCHER_URL=https://rancher-aio.cloud.ut.ee \
    RANCHER_BEARER_TOKEN=token-l6n2v:... \
    RANCHER_CLUSTER_ID=c-m-jqzh4hd7 \
    KEYCLOAK_URL=https://auth.k8s.riigipilv.hpc.ut.ee \
    KEYCLOAK_REALM=riigipilv \
    KEYCLOAK_USERNAME=admin \
    KEYCLOAK_PASSWORD=cb6u98YVku \
    uv run pytest tests/test_integration.py -v -s
"""

import asyncio
import os

import pytest
import pytest_asyncio

from rancher_keycloak_operator.discovery import ProjectDiscovery
from rancher_keycloak_operator.keycloak_client import KeycloakClient
from rancher_keycloak_operator.rancher_client import RancherClient
from rancher_keycloak_operator.reconciler import Reconciler

# Test constants
TEST_USER_00 = "7952b22d-d52c-42bc-82cc-66c11c915fd7"  # test-rancher-00
TEST_USER_01 = "c951730f-0ea0-402e-95a4-30a2e776188b"  # test-rancher-01
NONEXISTENT_USER = "00000000-0000-0000-0000-000000000000"
TEST_RANCHER_ROLE = "workloads-manage"


def _skip_if_no_env():
    if not os.environ.get("RANCHER_URL"):
        pytest.skip("RANCHER_URL not set — skipping integration tests")


def _cluster_id() -> str:
    return os.environ["RANCHER_CLUSTER_ID"]


class FakePatch:
    """Mimics kopf's patch object for testing."""

    def __init__(self):
        self.status = {}


# --- Fixtures ---


@pytest_asyncio.fixture
async def rancher():
    _skip_if_no_env()
    client = RancherClient(
        api_url=os.environ["RANCHER_URL"],
        bearer_token=os.environ["RANCHER_BEARER_TOKEN"],
        verify_ssl=False,
    )
    yield client
    await client.close()


@pytest_asyncio.fixture
async def keycloak():
    _skip_if_no_env()
    client = KeycloakClient(
        server_url=os.environ["KEYCLOAK_URL"],
        realm=os.environ["KEYCLOAK_REALM"],
        username=os.environ["KEYCLOAK_USERNAME"],
        password=os.environ["KEYCLOAK_PASSWORD"],
        verify_ssl=False,
    )
    yield client
    await client.close()


@pytest_asyncio.fixture
async def reconciler(rancher, keycloak):
    return Reconciler(rancher=rancher, keycloak=keycloak)


async def _wait_project_gone(rancher, project_id, timeout=15):
    """Wait for Rancher project to be fully removed or in removing state."""
    for _ in range(timeout):
        project = await rancher.get_project(project_id)
        if project is None:
            return None
        if project.get("state") == "removing":
            await asyncio.sleep(1)
            continue
        return project
    return project


# ============================================================
# 1. CONNECTIVITY
# ============================================================


class TestConnectivity:
    """Validate access to all external systems."""

    @pytest.mark.asyncio
    async def test_rancher_ping(self, rancher):
        assert await rancher.ping()

    @pytest.mark.asyncio
    async def test_keycloak_ping(self, keycloak):
        assert await keycloak.ping()

    @pytest.mark.asyncio
    async def test_keycloak_find_user_by_id(self, keycloak):
        user = await keycloak.find_user_by_id(TEST_USER_00)
        assert user is not None
        assert user["username"] == "test-rancher-00"

    @pytest.mark.asyncio
    async def test_keycloak_find_user_by_username(self, keycloak):
        user = await keycloak.find_user_by_username("test-rancher-01")
        assert user is not None
        assert user["id"] == TEST_USER_01

    @pytest.mark.asyncio
    async def test_rancher_list_projects(self, rancher):
        projects = await rancher.list_all_projects(_cluster_id())
        assert len(projects) > 0, "Cluster should have at least Default + System"


# ============================================================
# 2. HAPPY PATH: FULL LIFECYCLE
# ============================================================


class TestFullLifecycle:
    """Create → verify → add user → remove user → delete → verify cleanup."""

    @pytest.mark.asyncio
    async def test_create_verify_modify_delete(self, reconciler, rancher, keycloak):
        spec = {
            "projectName": "rko-lifecycle-test",
            "clusterId": _cluster_id(),
            "description": "Lifecycle test — safe to delete",
            "organization": "test-org",
            "projectSlug": "lifecycle",
            "namespace": {"name": "rko-lifecycle-test"},
            "keycloak": {
                "enabled": True,
                "parentGroupName": "c_lifecycletest",
                "roleBindings": [
                    {
                        "groupName": "project_lifecycle_workloads-manage",
                        "rancherRole": TEST_RANCHER_ROLE,
                        "members": [],
                    }
                ],
            },
        }

        # CREATE
        patch = FakePatch()
        await reconciler.reconcile(spec, {}, patch)
        assert patch.status["phase"] == "Ready"
        project_id = patch.status["rancherProjectId"]
        assert project_id

        # Verify in Rancher
        project = await rancher.get_project(project_id)
        assert project is not None
        assert project["name"] == "rko-lifecycle-test"

        # Verify Keycloak parent + child groups
        parent_id = patch.status["keycloakParentGroupId"]
        assert parent_id
        rb = patch.status["keycloakRoleBindings"][0]
        assert rb["keycloakGroupId"]
        assert rb["rancherBindingId"]
        assert rb["memberCount"] == 0

        # ADD USER
        spec_with_user = _with_members(spec, [TEST_USER_00])
        patch2 = FakePatch()
        await reconciler.reconcile(spec_with_user, patch.status, patch2)
        assert patch2.status["phase"] == "Ready"
        rb2 = patch2.status["keycloakRoleBindings"][0]
        assert TEST_USER_00 in rb2["syncedMembers"]
        assert rb2["memberCount"] == 1

        # Verify user in Keycloak group
        members = await keycloak.get_group_members(rb2["keycloakGroupId"])
        assert any(m["id"] == TEST_USER_00 for m in members)

        # REMOVE USER
        patch3 = FakePatch()
        await reconciler.reconcile(spec, patch2.status, patch3)
        assert patch3.status["phase"] == "Ready"
        rb3 = patch3.status["keycloakRoleBindings"][0]
        assert rb3["memberCount"] == 0
        assert TEST_USER_00 not in rb3.get("syncedMembers", [])

        # Verify user gone from Keycloak
        members = await keycloak.get_group_members(rb3["keycloakGroupId"])
        assert len(members) == 0

        # DELETE
        await reconciler.cleanup(spec, patch3.status)
        result = await _wait_project_gone(rancher, project_id)
        assert result is None or result.get("state") == "removing"

        # Verify Keycloak groups gone
        assert await keycloak.get_group_by_name("project_lifecycle_workloads-manage") is None
        print("\n  ✓ Full lifecycle test passed!")


# ============================================================
# 3. IDEMPOTENCY
# ============================================================


class TestIdempotency:
    """Reconciling the same spec twice must produce identical results."""

    @pytest.mark.asyncio
    async def test_reconcile_twice_same_ids(self, reconciler):
        spec = {
            "projectName": "rko-idempotent-test",
            "clusterId": _cluster_id(),
            "description": "Idempotency test",
            "organization": "test-org",
            "projectSlug": "idempotent",
            "namespace": {"name": "rko-idempotent-test"},
            "keycloak": {
                "enabled": True,
                "parentGroupName": "c_idempotenttest2",
                "roleBindings": [
                    {
                        "groupName": "project_idempotent2_workloads-manage",
                        "rancherRole": TEST_RANCHER_ROLE,
                        "members": [{"userIdentifier": TEST_USER_00, "lookupByID": True}],
                    }
                ],
            },
        }

        try:
            patch1 = FakePatch()
            await reconciler.reconcile(spec, {}, patch1)
            assert patch1.status["phase"] == "Ready"

            patch2 = FakePatch()
            await reconciler.reconcile(spec, patch1.status, patch2)
            assert patch2.status["phase"] == "Ready"

            # All IDs must be stable
            assert patch1.status["rancherProjectId"] == patch2.status["rancherProjectId"]
            assert (
                patch1.status["keycloakParentGroupId"]
                == patch2.status["keycloakParentGroupId"]
            )
            assert (
                patch1.status["keycloakRoleBindings"][0]["keycloakGroupId"]
                == patch2.status["keycloakRoleBindings"][0]["keycloakGroupId"]
            )
            assert (
                patch1.status["keycloakRoleBindings"][0]["rancherBindingId"]
                == patch2.status["keycloakRoleBindings"][0]["rancherBindingId"]
            )
            # Members must be identical
            assert (
                patch1.status["keycloakRoleBindings"][0]["syncedMembers"]
                == patch2.status["keycloakRoleBindings"][0]["syncedMembers"]
            )
            print("\n  ✓ Idempotency test passed!")
        finally:
            await reconciler.cleanup(spec, patch1.status)


# ============================================================
# 4. MULTI-USER MANAGEMENT
# ============================================================


class TestMultiUser:
    """Add two users, remove one, verify the other stays."""

    @pytest.mark.asyncio
    async def test_add_two_remove_one(self, reconciler, keycloak):
        spec = _make_spec(
            "rko-multiuser-test",
            parent_group="c_multiusertest",
            child_group="project_multiuser_workloads-manage",
        )

        try:
            # Create with zero members
            patch1 = FakePatch()
            await reconciler.reconcile(spec, {}, patch1)
            assert patch1.status["phase"] == "Ready"

            # Add both users
            spec2 = _with_members(spec, [TEST_USER_00, TEST_USER_01])
            patch2 = FakePatch()
            await reconciler.reconcile(spec2, patch1.status, patch2)
            rb = patch2.status["keycloakRoleBindings"][0]
            assert rb["memberCount"] == 2
            assert set(rb["syncedMembers"]) == {TEST_USER_00, TEST_USER_01}

            # Remove user_00, keep user_01
            spec3 = _with_members(spec, [TEST_USER_01])
            patch3 = FakePatch()
            await reconciler.reconcile(spec3, patch2.status, patch3)
            rb3 = patch3.status["keycloakRoleBindings"][0]
            assert rb3["memberCount"] == 1
            assert TEST_USER_01 in rb3["syncedMembers"]
            assert TEST_USER_00 not in rb3["syncedMembers"]

            # Verify in Keycloak
            members = await keycloak.get_group_members(rb3["keycloakGroupId"])
            member_ids = {m["id"] for m in members}
            assert TEST_USER_01 in member_ids
            assert TEST_USER_00 not in member_ids

            print("\n  ✓ Multi-user test passed!")
        finally:
            await reconciler.cleanup(spec, patch1.status)


# ============================================================
# 5. MULTI-ROLE BINDINGS
# ============================================================


class TestMultiRole:
    """Single CR with two roleBindings, each with different members."""

    @pytest.mark.asyncio
    async def test_two_roles_independent_members(self, reconciler, keycloak):
        spec = {
            "projectName": "rko-multirole-test",
            "clusterId": _cluster_id(),
            "description": "Multi-role test",
            "organization": "test-org",
            "projectSlug": "multirole",
            "namespace": {"name": "rko-multirole-test"},
            "keycloak": {
                "enabled": True,
                "parentGroupName": "c_multiroletest",
                "roleBindings": [
                    {
                        "groupName": "project_multirole_workloads-manage",
                        "rancherRole": "workloads-manage",
                        "members": [
                            {"userIdentifier": TEST_USER_00, "lookupByID": True},
                        ],
                    },
                    {
                        "groupName": "project_multirole_read-only",
                        "rancherRole": "read-only",
                        "members": [
                            {"userIdentifier": TEST_USER_01, "lookupByID": True},
                        ],
                    },
                ],
            },
        }

        try:
            patch = FakePatch()
            await reconciler.reconcile(spec, {}, patch)
            assert patch.status["phase"] == "Ready"

            rbs = patch.status["keycloakRoleBindings"]
            assert len(rbs) == 2

            # First role: user_00 as workloads-manage
            rb_manage = next(
                r for r in rbs if r["groupName"] == "project_multirole_workloads-manage"
            )
            assert rb_manage["memberCount"] == 1
            assert TEST_USER_00 in rb_manage["syncedMembers"]

            # Second role: user_01 as read-only
            rb_readonly = next(r for r in rbs if r["groupName"] == "project_multirole_read-only")
            assert rb_readonly["memberCount"] == 1
            assert TEST_USER_01 in rb_readonly["syncedMembers"]

            # Both should have separate Keycloak groups
            assert rb_manage["keycloakGroupId"] != rb_readonly["keycloakGroupId"]

            # Both should have separate Rancher bindings
            assert rb_manage["rancherBindingId"] != rb_readonly["rancherBindingId"]

            # Verify in Keycloak: user_00 NOT in read-only group
            ro_members = await keycloak.get_group_members(rb_readonly["keycloakGroupId"])
            assert not any(m["id"] == TEST_USER_00 for m in ro_members)

            print("\n  ✓ Multi-role test passed!")
        finally:
            await reconciler.cleanup(spec, patch.status)


# ============================================================
# 5b. SAME USER WITH MULTIPLE ROLES
# ============================================================


class TestUserMultipleRoles:
    """Same user in two roleBindings (e.g., workloads-manage + read-only)."""

    @pytest.mark.asyncio
    async def test_same_user_two_roles(self, reconciler, keycloak):
        """User appears in both roleBindings — should be in both KC groups."""
        spec = {
            "projectName": "rko-userroles-test",
            "clusterId": _cluster_id(),
            "description": "Same user, multiple roles",
            "organization": "test-org",
            "projectSlug": "userroles",
            "namespace": {"name": "rko-userroles-test"},
            "keycloak": {
                "enabled": True,
                "parentGroupName": "c_userrolestest",
                "roleBindings": [
                    {
                        "groupName": "project_userroles_workloads-manage",
                        "rancherRole": "workloads-manage",
                        "members": [
                            {"userIdentifier": TEST_USER_00, "lookupByID": True},
                        ],
                    },
                    {
                        "groupName": "project_userroles_read-only",
                        "rancherRole": "read-only",
                        "members": [
                            {"userIdentifier": TEST_USER_00, "lookupByID": True},
                        ],
                    },
                ],
            },
        }

        try:
            patch = FakePatch()
            await reconciler.reconcile(spec, {}, patch)
            assert patch.status["phase"] == "Ready"

            rbs = patch.status["keycloakRoleBindings"]
            assert len(rbs) == 2

            rb_manage = next(r for r in rbs if "workloads-manage" in r["groupName"])
            rb_readonly = next(r for r in rbs if "read-only" in r["groupName"])

            # User should be in BOTH groups
            assert TEST_USER_00 in rb_manage["syncedMembers"]
            assert TEST_USER_00 in rb_readonly["syncedMembers"]
            assert rb_manage["memberCount"] == 1
            assert rb_readonly["memberCount"] == 1

            # Verify in Keycloak: user is actually in both groups
            manage_members = await keycloak.get_group_members(rb_manage["keycloakGroupId"])
            readonly_members = await keycloak.get_group_members(rb_readonly["keycloakGroupId"])
            assert any(m["id"] == TEST_USER_00 for m in manage_members)
            assert any(m["id"] == TEST_USER_00 for m in readonly_members)

            # Each group has its own Rancher binding
            assert rb_manage["rancherBindingId"] != rb_readonly["rancherBindingId"]

            print("\n  ✓ Same user, multiple roles test passed!")
        finally:
            await reconciler.cleanup(spec, patch.status)

    @pytest.mark.asyncio
    async def test_remove_user_from_one_role_keeps_other(self, reconciler, keycloak):
        """Remove user from one roleBinding, verify they stay in the other."""
        spec = {
            "projectName": "rko-rolechange-test",
            "clusterId": _cluster_id(),
            "description": "Role change test",
            "organization": "test-org",
            "projectSlug": "rolechange",
            "namespace": {"name": "rko-rolechange-test"},
            "keycloak": {
                "enabled": True,
                "parentGroupName": "c_rolechangetest",
                "roleBindings": [
                    {
                        "groupName": "project_rolechange_workloads-manage",
                        "rancherRole": "workloads-manage",
                        "members": [
                            {"userIdentifier": TEST_USER_00, "lookupByID": True},
                        ],
                    },
                    {
                        "groupName": "project_rolechange_read-only",
                        "rancherRole": "read-only",
                        "members": [
                            {"userIdentifier": TEST_USER_00, "lookupByID": True},
                        ],
                    },
                ],
            },
        }

        try:
            # Create with user in both roles
            patch1 = FakePatch()
            await reconciler.reconcile(spec, {}, patch1)
            assert patch1.status["phase"] == "Ready"

            # Remove user from workloads-manage, keep in read-only
            import copy
            spec2 = copy.deepcopy(spec)
            spec2["keycloak"]["roleBindings"][0]["members"] = []  # empty manage
            # roleBindings[1] still has the user

            patch2 = FakePatch()
            await reconciler.reconcile(spec2, patch1.status, patch2)
            assert patch2.status["phase"] == "Ready"

            rbs2 = patch2.status["keycloakRoleBindings"]
            rb_manage = next(r for r in rbs2 if "workloads-manage" in r["groupName"])
            rb_readonly = next(r for r in rbs2 if "read-only" in r["groupName"])

            # User removed from manage
            assert TEST_USER_00 not in rb_manage.get("syncedMembers", [])
            assert rb_manage["memberCount"] == 0

            # User still in read-only
            assert TEST_USER_00 in rb_readonly["syncedMembers"]
            assert rb_readonly["memberCount"] == 1

            # Verify in Keycloak
            manage_members = await keycloak.get_group_members(rb_manage["keycloakGroupId"])
            readonly_members = await keycloak.get_group_members(rb_readonly["keycloakGroupId"])
            assert not any(m["id"] == TEST_USER_00 for m in manage_members)
            assert any(m["id"] == TEST_USER_00 for m in readonly_members)

            print("\n  ✓ Remove from one role, keep other test passed!")
        finally:
            await reconciler.cleanup(spec, patch1.status)


# ============================================================
# 5c. DUPLICATE HANDLING
# ============================================================


class TestDuplicateHandling:
    """Verify correct behavior with duplicate entries in spec."""

    @pytest.mark.asyncio
    async def test_duplicate_user_in_same_role(self, reconciler, keycloak):
        """Same userIdentifier listed twice in one roleBinding — should deduplicate."""
        spec = {
            "projectName": "rko-dupuser-test",
            "clusterId": _cluster_id(),
            "description": "Duplicate user test",
            "organization": "test-org",
            "projectSlug": "dupuser",
            "namespace": {"name": "rko-dupuser-test"},
            "keycloak": {
                "enabled": True,
                "parentGroupName": "c_dupusertest",
                "roleBindings": [
                    {
                        "groupName": "project_dupuser_workloads-manage",
                        "rancherRole": TEST_RANCHER_ROLE,
                        "members": [
                            {"userIdentifier": TEST_USER_00, "lookupByID": True},
                            {"userIdentifier": TEST_USER_00, "lookupByID": True},  # duplicate!
                        ],
                    }
                ],
            },
        }

        try:
            patch = FakePatch()
            await reconciler.reconcile(spec, {}, patch)
            assert patch.status["phase"] == "Ready"

            rb = patch.status["keycloakRoleBindings"][0]
            # User should appear exactly once
            assert rb["memberCount"] == 1
            assert rb["syncedMembers"].count(TEST_USER_00) == 1

            # Verify in Keycloak: exactly one member
            members = await keycloak.get_group_members(rb["keycloakGroupId"])
            assert len(members) == 1

            print("\n  ✓ Duplicate user deduplication test passed!")
        finally:
            await reconciler.cleanup(spec, patch.status)

    @pytest.mark.asyncio
    async def test_duplicate_group_name_across_role_bindings(self, reconciler, keycloak):
        """Same groupName in two roleBindings with different roles.

        This is a misconfiguration — two different Rancher roles pointing to the same
        KC group. The reconciler should handle it without crashing: both bindings
        share the same KC group, but get separate Rancher PRTBs.
        """
        spec = {
            "projectName": "rko-dupgroup-test",
            "clusterId": _cluster_id(),
            "description": "Duplicate group name test",
            "organization": "test-org",
            "projectSlug": "dupgroup",
            "namespace": {"name": "rko-dupgroup-test"},
            "keycloak": {
                "enabled": True,
                "parentGroupName": "c_dupgrouptest",
                "roleBindings": [
                    {
                        "groupName": "project_dupgroup_shared",  # same name
                        "rancherRole": "workloads-manage",
                        "members": [
                            {"userIdentifier": TEST_USER_00, "lookupByID": True},
                        ],
                    },
                    {
                        "groupName": "project_dupgroup_shared",  # same name!
                        "rancherRole": "read-only",
                        "members": [
                            {"userIdentifier": TEST_USER_01, "lookupByID": True},
                        ],
                    },
                ],
            },
        }

        try:
            patch = FakePatch()
            await reconciler.reconcile(spec, {}, patch)
            assert patch.status["phase"] == "Ready"

            rbs = patch.status["keycloakRoleBindings"]
            assert len(rbs) == 2

            # Both roleBindings share the same KC group
            assert rbs[0]["keycloakGroupId"] == rbs[1]["keycloakGroupId"]

            # But they should have different Rancher bindings (different roles)
            assert rbs[0]["rancherBindingId"] != rbs[1]["rancherBindingId"]

            # Both users should be in the shared group (second reconcile adds user_01,
            # sees user_00 already there from first)
            group_id = rbs[0]["keycloakGroupId"]
            members = await keycloak.get_group_members(group_id)
            member_ids = {m["id"] for m in members}
            assert TEST_USER_00 in member_ids
            assert TEST_USER_01 in member_ids

            print("\n  ✓ Duplicate groupName handling test passed!")
        finally:
            await reconciler.cleanup(spec, patch.status)


# ============================================================
# 6. ADOPT EXISTING PROJECT
# ============================================================


class TestAdoptExisting:
    """Reconcile against a project that already exists in Rancher."""

    @pytest.mark.asyncio
    async def test_adopt_preexisting_project(self, reconciler, rancher):
        # Pre-create a project directly in Rancher
        pre_id = await rancher.create_project(
            cluster_id=_cluster_id(),
            name="rko-adopt-test",
            description="Pre-existing project",
            organization="test-org",
        )
        assert pre_id

        spec = _make_spec(
            "rko-adopt-test",
            parent_group="c_adopttest",
            child_group="project_adopt_workloads-manage",
        )

        try:
            patch = FakePatch()
            await reconciler.reconcile(spec, {}, patch)
            assert patch.status["phase"] == "Ready"

            # Should have adopted the existing project, not created a new one
            assert patch.status["rancherProjectId"] == pre_id, \
                f"Should adopt existing project {pre_id}, got {patch.status['rancherProjectId']}"

            print(f"\n  Adopted existing project: {pre_id}")
            print("  ✓ Adopt existing test passed!")
        finally:
            await reconciler.cleanup(spec, patch.status)


# ============================================================
# 7. KEYCLOAK DISABLED
# ============================================================


class TestKeycloakDisabled:
    """Project with keycloak.enabled=false should skip all Keycloak operations."""

    @pytest.mark.asyncio
    async def test_no_keycloak_groups_created(self, reconciler, rancher, keycloak):
        spec = {
            "projectName": "rko-nokeycloak-test",
            "clusterId": _cluster_id(),
            "description": "No-Keycloak test",
            "organization": "test-org",
            "projectSlug": "nokeycloak",
            "namespace": {"name": "rko-nokeycloak-test"},
            "keycloak": {"enabled": False},
        }

        try:
            patch = FakePatch()
            await reconciler.reconcile(spec, {}, patch)

            assert patch.status["phase"] == "Ready"
            assert patch.status["rancherProjectId"]

            # No Keycloak state should be set
            assert not patch.status.get("keycloakParentGroupId")
            assert not patch.status.get("keycloakRoleBindings")

            # Verify project exists in Rancher
            project = await rancher.get_project(patch.status["rancherProjectId"])
            assert project is not None

            print("\n  ✓ Keycloak disabled test passed!")
        finally:
            await reconciler.cleanup(spec, patch.status)


# ============================================================
# 8. DRIFT RECOVERY
# ============================================================


class TestDriftRecovery:
    """Externally delete a Keycloak group, then reconcile should recreate it."""

    @pytest.mark.asyncio
    async def test_recreate_deleted_keycloak_group(self, reconciler, keycloak):
        spec = _make_spec(
            "rko-drift-test",
            parent_group="c_drifttest",
            child_group="project_drift_workloads-manage",
            members=[TEST_USER_00],
        )

        try:
            # Initial reconcile
            patch1 = FakePatch()
            await reconciler.reconcile(spec, {}, patch1)
            assert patch1.status["phase"] == "Ready"
            original_group_id = patch1.status["keycloakRoleBindings"][0]["keycloakGroupId"]

            # Externally delete the child group (simulating drift)
            await keycloak.delete_group(original_group_id)
            deleted = await keycloak.get_group_by_id(original_group_id)
            assert deleted is None, "Group should be gone after external delete"

            # Reconcile again — should recreate the group
            patch2 = FakePatch()
            await reconciler.reconcile(spec, patch1.status, patch2)
            assert patch2.status["phase"] == "Ready"

            new_group_id = patch2.status["keycloakRoleBindings"][0]["keycloakGroupId"]
            assert new_group_id, "Group should be recreated"
            assert new_group_id != original_group_id, "Should be a new group ID"

            # User should be re-added
            members = await keycloak.get_group_members(new_group_id)
            assert any(m["id"] == TEST_USER_00 for m in members), "User should be re-added"

            print(f"\n  Original group: {original_group_id}")
            print(f"  Recreated group: {new_group_id}")
            print("  ✓ Drift recovery test passed!")
        finally:
            await reconciler.cleanup(spec, patch2.status)


# ============================================================
# 9. NONEXISTENT USER
# ============================================================


class TestNonexistentUser:
    """Adding a user ID that doesn't exist in Keycloak should not crash."""

    @pytest.mark.asyncio
    async def test_missing_user_graceful(self, reconciler, keycloak):
        spec = _make_spec(
            "rko-baduser-test",
            parent_group="c_badusertest",
            child_group="project_baduser_workloads-manage",
            members=[NONEXISTENT_USER, TEST_USER_00],  # one bad, one good
        )

        try:
            patch = FakePatch()
            await reconciler.reconcile(spec, {}, patch)

            # Should still reach Ready (nonexistent user is logged, not fatal)
            assert patch.status["phase"] == "Ready"
            rb = patch.status["keycloakRoleBindings"][0]

            # The valid user should be synced
            assert TEST_USER_00 in rb["syncedMembers"]
            # The nonexistent user should NOT appear in synced (it was never added)
            assert NONEXISTENT_USER not in rb["syncedMembers"]

            # memberCount reflects actual Keycloak state
            members = await keycloak.get_group_members(rb["keycloakGroupId"])
            assert len(members) == 1
            assert members[0]["id"] == TEST_USER_00

            print("\n  ✓ Nonexistent user test passed!")
        finally:
            await reconciler.cleanup(spec, patch.status)


# ============================================================
# 10. CLEANUP RESILIENCE
# ============================================================


class TestCleanupResilience:
    """Cleanup should not crash when external resources are already gone."""

    @pytest.mark.asyncio
    async def test_cleanup_already_deleted(self, reconciler, rancher, keycloak):
        spec = _make_spec(
            "rko-cleanup-test",
            parent_group="c_cleanuptest",
            child_group="project_cleanup_workloads-manage",
        )

        # Create resources
        patch = FakePatch()
        await reconciler.reconcile(spec, {}, patch)
        assert patch.status["phase"] == "Ready"
        project_id = patch.status["rancherProjectId"]
        group_id = patch.status["keycloakRoleBindings"][0]["keycloakGroupId"]

        # Externally delete everything before cleanup runs
        await rancher.delete_project(project_id)
        await keycloak.delete_group(group_id)
        parent_id = patch.status["keycloakParentGroupId"]
        if parent_id:
            await keycloak.delete_group(parent_id)

        # Cleanup should not raise
        await reconciler.cleanup(spec, patch.status)
        print("\n  ✓ Cleanup resilience test passed!")


# ============================================================
# 11. EMPTY SPEC EDGE CASES
# ============================================================


class TestEdgeCases:
    """Edge cases in spec construction."""

    @pytest.mark.asyncio
    async def test_no_namespace_spec_uses_project_name(self, reconciler, rancher):
        """When namespace is not specified, projectName should be used."""
        spec = {
            "projectName": "rko-nonamespace-test",
            "clusterId": _cluster_id(),
            "keycloak": {"enabled": False},
        }

        try:
            patch = FakePatch()
            await reconciler.reconcile(spec, {}, patch)
            assert patch.status["phase"] == "Ready"
            assert patch.status["namespaceName"] == "rko-nonamespace-test"
            print("\n  ✓ No-namespace edge case passed!")
        finally:
            await reconciler.cleanup(spec, patch.status)

    @pytest.mark.asyncio
    async def test_empty_role_bindings(self, reconciler):
        """keycloak.enabled=true but roleBindings is empty."""
        spec = {
            "projectName": "rko-emptyrb-test",
            "clusterId": _cluster_id(),
            "namespace": {"name": "rko-emptyrb-test"},
            "keycloak": {
                "enabled": True,
                "parentGroupName": "c_emptyrb",
                "roleBindings": [],
            },
        }

        try:
            patch = FakePatch()
            await reconciler.reconcile(spec, {}, patch)
            assert patch.status["phase"] == "Ready"
            assert patch.status.get("keycloakParentGroupId")
            assert patch.status.get("keycloakRoleBindings") == []
            print("\n  ✓ Empty roleBindings edge case passed!")
        finally:
            await reconciler.cleanup(spec, patch.status)


# ============================================================
# 12. DISCOVERY
# ============================================================


class TestDiscovery:
    """Detect unmanaged Rancher projects."""

    @pytest.mark.asyncio
    async def test_excludes_system_projects(self, rancher):
        discovery = ProjectDiscovery(rancher=rancher, namespace="waldur-system")
        status = await discovery.scan(_cluster_id())

        unmanaged_names = [p["name"] for p in status["unmanagedProjects"]]
        assert "Default" not in unmanaged_names
        assert "System" not in unmanaged_names

    @pytest.mark.asyncio
    async def test_detects_manually_created_project(self, rancher):
        """Create a project via Rancher API, verify discovery finds it."""
        discovery = ProjectDiscovery(rancher=rancher, namespace="waldur-system")

        # Pre-scan
        before = await discovery.scan(_cluster_id())

        # Create a project directly
        project_id = await rancher.create_project(
            cluster_id=_cluster_id(),
            name="rko-discovery-manual",
            description="Manually created for discovery test",
        )

        try:
            # Post-scan
            after = await discovery.scan(_cluster_id())
            after_names = {p["name"] for p in after["unmanagedProjects"]}

            assert "rko-discovery-manual" in after_names, \
                "Manually created project should appear as unmanaged"
            assert after["unmanagedProjectCount"] > before["unmanagedProjectCount"]

            print(f"\n  Before: {before['unmanagedProjectCount']} unmanaged")
            print(f"  After:  {after['unmanagedProjectCount']} unmanaged")
            print("  ✓ Discovery detects manual project!")
        finally:
            await rancher.delete_project(project_id)


# ============================================================
# HELPERS
# ============================================================


def _make_spec(
    project_name: str,
    parent_group: str,
    child_group: str,
    members: list[str] | None = None,
) -> dict:
    """Build a standard test spec."""
    member_list = [
        {"userIdentifier": uid, "lookupByID": True}
        for uid in (members or [])
    ]
    return {
        "projectName": project_name,
        "clusterId": _cluster_id(),
        "description": f"Test: {project_name}",
        "organization": "test-org",
        "projectSlug": project_name.replace("rko-", ""),
        "namespace": {"name": project_name},
        "keycloak": {
            "enabled": True,
            "parentGroupName": parent_group,
            "roleBindings": [
                {
                    "groupName": child_group,
                    "rancherRole": TEST_RANCHER_ROLE,
                    "members": member_list,
                }
            ],
        },
    }


def _with_members(spec: dict, user_ids: list[str]) -> dict:
    """Return a copy of spec with the given members in the first roleBinding."""
    import copy
    new = copy.deepcopy(spec)
    new["keycloak"]["roleBindings"][0]["members"] = [
        {"userIdentifier": uid, "lookupByID": True}
        for uid in user_ids
    ]
    return new

import unittest

from fcx_control.roles import MANAGED_CONTROL_ROLES, assignable_control_roles


class ControlRolePolicyTests(unittest.TestCase):
    def test_developer_can_assign_every_managed_role(self):
        self.assertEqual(assignable_control_roles(["developer"]), list(MANAGED_CONTROL_ROLES))

    def test_super_admin_can_assign_every_managed_role(self):
        self.assertEqual(assignable_control_roles(["super_admin"]), list(MANAGED_CONTROL_ROLES))

    def test_fec_admin_can_only_create_investigators(self):
        self.assertEqual(assignable_control_roles(["fec_admin"]), ["fec_investigator"])

    def test_commissioner_cannot_escalate_roles(self):
        self.assertEqual(assignable_control_roles(["commissioner"]), [])

    def test_fcx_admin_and_investigator_cannot_create_control_accounts(self):
        self.assertEqual(assignable_control_roles(["fcx_admin"]), [])
        self.assertEqual(assignable_control_roles(["fec_investigator"]), [])


if __name__ == "__main__":
    unittest.main()

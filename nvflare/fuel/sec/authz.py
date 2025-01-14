# Copyright (c) 2021-2022, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import time

_KEY_PERMISSIONS = "permissions"
_KEY_FORMAT_VERSION = "format_version"
_TARGET_SITE = "site"
_TARGET_SUBMITTER = "submitter"
_ANY_RIGHT = "*"


class Person(object):
    def __init__(self, name: str, org: str, role):
        self.name = _normalize_str(name)
        self.org = _normalize_str(org)
        self.roles = []
        if isinstance(role, str):
            self.roles.append(_normalize_str(role))
        elif isinstance(role, list):
            if len(role) <= 0:
                raise TypeError("roles not specified - it must be a list of strings")

            for r in role:
                if not isinstance(r, str):
                    raise TypeError(f"role value must be a str but got {type(r)}")
                self.roles.append(_normalize_str(r))
        else:
            raise TypeError(f"role must be a str or list of str but got {type(role)}")

    def __str__(self):
        return f"{self.name}:{self.org}:{self.roles[0]}"


class AuthzContext(object):
    def __init__(self, right: str, user: Person, submitter: Person = None):
        """Base class to contain context data for authorization."""
        self.right = right
        self.user = user
        self.submitter = submitter
        self.attrs = {}
        if not submitter:
            self.submitter = Person("", "", "")

    def set_attr(self, key: str, value):
        self.attrs[key] = value

    def get_attr(self, key: str, default=None):
        return self.attrs.get(key, default)


class ConditionEvaluator(object):
    def evaluate(self, site_org: str, ctx: AuthzContext) -> bool:
        pass


class UserOrgEvaluator(ConditionEvaluator):
    def __init__(self, target):
        self.target = target

    def evaluate(self, site_org: str, ctx: AuthzContext):
        if self.target == _TARGET_SITE:
            return ctx.user.org == site_org
        elif self.target == _TARGET_SUBMITTER:
            return ctx.user.org == ctx.submitter.org
        else:
            return ctx.user.org == self.target


class UserNameEvaluator(ConditionEvaluator):
    def __init__(self, target: str):
        self.target = target

    def evaluate(self, site_org: str, ctx: AuthzContext):
        if self.target == _TARGET_SUBMITTER:
            return ctx.user.name == ctx.submitter.name
        else:
            return ctx.user.name == self.target


class TrueEvaluator(ConditionEvaluator):
    def evaluate(self, site_org: str, ctx: AuthzContext) -> bool:
        return True


class FalseEvaluator(ConditionEvaluator):
    def evaluate(self, site_org: str, ctx: AuthzContext) -> bool:
        return False


class _RoleRightConditions(object):
    def __init__(self):
        self.allowed_conditions = []
        self.blocked_conditions = []
        self.exp = None

    def _any_condition_matched(self, conds: [ConditionEvaluator], site_org: str, ctx: AuthzContext):
        # if any condition is met, return True
        # only when all conditions fail to match, return False
        for e in conds:
            matched = e.evaluate(site_org, ctx)
            if matched:
                return True
        return False

    def evaluate(self, site_org: str, ctx: AuthzContext):
        # first evaluate blocked list
        if self.blocked_conditions:
            if self._any_condition_matched(self.blocked_conditions, site_org, ctx):
                # if any block condition is met, return False
                # print("blocked")
                return False

        # evaluate allowed list
        if self.allowed_conditions:
            if self._any_condition_matched(self.allowed_conditions, site_org, ctx):
                # print("allowed")
                return True
            else:
                # all allowed conditions failed
                # print("not allowed")
                return False

        # no allowed list specified - only blocked list specified
        # we got here since no blocked condition matched
        return True

    def _parse_one_expression(self, exp) -> str:
        v = _normalize_str(exp)
        blocked = False
        parts = v.split()
        if len(parts) == 2 and parts[0] == "not":
            blocked = True
            v = parts[1]

        if v in ["all", "any"]:
            ev = TrueEvaluator()
        elif v in ["none", "no"]:
            ev = FalseEvaluator()
        else:
            parts = v.split(":")
            if len(parts) == 2:
                target_type = _normalize_str(parts[0])
                target_value = _normalize_str(parts[1])

                if target_type in ["o", "org"]:
                    ev = UserOrgEvaluator(target_value)
                elif target_type in ["n", "name"]:
                    ev = UserNameEvaluator(target_value)
                else:
                    return f'bad condition expression "{exp}": invalid type "{target_type}"'
            else:
                return f'bad condition expression "{exp}"'

        if blocked:
            self.blocked_conditions.append(ev)
        else:
            self.allowed_conditions.append(ev)
        return ""

    def parse_expression(self, exp):
        """
        Parse the value expression into a list of condition(s)


        Args:
            exp: expression to be parsed

        Returns:
            A list of conditions if exp is valid. Each condition is expressed as str to be evaluated.
            Return an error str if value is invalid

        """
        self.exp = exp
        if isinstance(exp, str):
            return self._parse_one_expression(exp)

        if isinstance(exp, list):
            # we expect the list contains str only
            if not exp:
                # empty list
                return "bad condition expression - no conditions specified"

            for ex in exp:
                err = self._parse_one_expression(ex)
                if err:
                    # this is an error
                    return err
        else:
            return f"bad condition expression type - expect str or list but got {type(exp)}"


class Policy(object):
    def __init__(self, config: dict, role_right_map: dict, roles: list, rights: list, role_rights: dict):
        self.config = config
        self.role_right_map = role_right_map
        self.roles = roles
        self.rights = rights
        self.roles.sort()
        self.rights.sort()
        self.role_rights = role_rights

    def get_rights(self):
        return self.rights

    def get_roles(self):
        return self.roles

    def _eval_for_role(self, role: str, site_org: str, ctx: AuthzContext):
        conds = self.role_right_map.get(_role_right_key(role, _ANY_RIGHT))
        if not conds:
            conds = self.role_right_map.get(_role_right_key(role, ctx.right))

        if not conds:
            return False

        assert isinstance(conds, _RoleRightConditions)
        return conds.evaluate(site_org, ctx)

    def evaluate(self, site_org: str, ctx: AuthzContext):
        """

        Args:
            site_org:
            right: right to be evaluated
            ctx:

        Returns: a tuple of (result, error)

        """
        site_org = _normalize_str(site_org)
        for role in ctx.user.roles:
            permitted = self._eval_for_role(role=role, site_org=site_org, ctx=ctx)
            if permitted:
                # permitted if any role is okay
                return True, ""
        return False, ""


def _normalize_str(s: str) -> str:
    return " ".join(s.lower().split())


def _role_right_key(role_name: str, right_name: str):
    return role_name + ":" + right_name


def _add_role_right_conds(role, right, conds, rr_map: dict, rights, right_conds):
    right_conds[right] = conds.exp
    rr_map[_role_right_key(role, right)] = conds
    if right not in rights:
        rights.append(right)


def parse_policy_config(config: dict, right_categories: dict):
    """Validates that an authorization policy configuration has the right syntax.

    Args:
        config: configuration dictionary to validate
        right_categories: a dict of right => category mapping

    Returns: a Policy object if no error, a string describing the error encountered

    """
    if not isinstance(config, dict):
        return None, f"policy definition must be a dict but got {type(config)}"

    if not config:
        # empty policy
        return None, "policy definition is empty"

    role_right_map = {}
    role_rights = {}
    roles = []
    rights = []

    # Compute category => right list
    cat_to_rights = {}
    if right_categories:
        for r, c in right_categories.items():
            right_list = cat_to_rights.get(c)
            if not right_list:
                right_list = []
            right_list.append(r)
            cat_to_rights[c] = right_list

    # check version
    format_version = config.get(_KEY_FORMAT_VERSION)
    if not format_version or format_version != "1.0":
        return None, "missing or invalid policy format_version: must be 1.0"

    permissions = config.get(_KEY_PERMISSIONS)
    if not permissions:
        return None, "missing permissions"

    if not isinstance(permissions, dict):
        return None, f"invalid permissions: expect a dict but got {type(permissions)}"

    # permissions is a dict of role => rights;
    for role_name, right_conf in permissions.items():
        if not isinstance(role_name, str):
            return None, f"bad role name: expect a str but got {type(role_name)}"

        role_name = _normalize_str(role_name)
        roles.append(role_name)
        right_conds = {}  # rights of this role
        role_rights[role_name] = right_conds

        if isinstance(right_conf, str) or isinstance(right_conf, list):
            conds = _RoleRightConditions()
            err = conds.parse_expression(right_conf)
            if err:
                return None, err
            _add_role_right_conds(role_name, _ANY_RIGHT, conds, role_right_map, rights, right_conds)
            continue

        if not isinstance(right_conf, dict):
            return None, f"bad right config: expect a dict but got {type(right_conf)}"

        # process right categories
        for right, exp in right_conf.items():
            if not isinstance(right, str):
                return None, f"bad right name: expect a str but got {type(right)}"

            right = _normalize_str(right)

            # see whether this is a right category
            right_list = cat_to_rights.get(right)
            if not right_list:
                # this is a regular right - skip it
                continue

            conds = _RoleRightConditions()
            err = conds.parse_expression(exp)
            if err:
                return None, err

            # all rights in the category share the same conditions
            _add_role_right_conds(role_name, right, conds, role_right_map, rights, right_conds)
            for r in right_list:
                _add_role_right_conds(role_name, r, conds, role_right_map, rights, right_conds)

        # process regular rights, which may override the rights from categories
        for right, exp in right_conf.items():
            right = _normalize_str(right)

            # see whether this is a right category
            right_list = cat_to_rights.get(right)
            if right_list:
                # this is category - already processed
                continue

            conds = _RoleRightConditions()
            err = conds.parse_expression(exp)
            if err:
                return None, err

            # this may cause the same right to be overwritten in the map
            _add_role_right_conds(role_name, right, conds, role_right_map, rights, right_conds)

    return Policy(config=config, role_right_map=role_right_map, role_rights=role_rights, roles=roles, rights=rights), ""


class Authorizer(object):
    def __init__(self, site_org: str, right_categories: dict = None):
        """Base class containing the authorization policy."""
        self.site_org = _normalize_str(site_org)
        self.right_categories = right_categories
        self.policy = None
        self.last_load_time = None

    def get_policy(self) -> Policy:
        return self.policy

    def authorize(self, ctx: AuthzContext) -> (bool, str):
        if not ctx:
            return True, ""

        assert isinstance(ctx, AuthzContext), f"ctx must be AuthzContext but got {type(ctx)}"
        assert isinstance(ctx.user, Person), "program error: no user in ctx!"
        if "super" in ctx.user.roles:
            # use this for testing purpose
            return True, ""

        authorized, err = self.evaluate(ctx)
        if not authorized:
            if err:
                return False, err
            else:
                return False, f"user '{ctx.user.name}' is not authorized for '{ctx.right}'"

        return True, ""

    def evaluate(self, ctx: AuthzContext):
        if not self.policy:
            return None, "policy not defined"

        return self.policy.evaluate(ctx=ctx, site_org=self.site_org)

    def load_policy(self, policy_config: dict) -> str:
        policy, err = parse_policy_config(policy_config, self.right_categories)
        if err:
            # this is an error
            return err

        self.policy = policy
        self.last_load_time = time.time()
        return ""


class AuthorizationService(object):

    the_authorizer = None

    @staticmethod
    def initialize(authorizer: Authorizer) -> (Authorizer, str):
        assert isinstance(authorizer, Authorizer), "authorizer must be Authorizer but got {}".format(type(authorizer))

        if not AuthorizationService.the_authorizer:
            # authorizer is not loaded
            AuthorizationService.the_authorizer = authorizer

        return AuthorizationService.the_authorizer, ""

    @staticmethod
    def get_authorizer():
        return AuthorizationService.the_authorizer

    @staticmethod
    def authorize(ctx: AuthzContext):
        if not AuthorizationService.the_authorizer:
            # no authorizer - assume that authorization is not required
            return True, ""
        return AuthorizationService.the_authorizer.authorize(ctx)

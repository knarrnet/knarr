import pytest
import time
import asyncio
import hashlib
import os
from knarr.core.models import GroupPolicy, SkillPolicy, Policy, Task, SkillSheet
from knarr.core.messages import TaskRequest, sign_message
from knarr.dht.node import DHTNode
from nacl.signing import SigningKey

# Realistic identity pairs: public_key (64-char hex) and derived node_id
_PK1 = "aa" * 32
_NID1 = hashlib.sha256(bytes.fromhex(_PK1)).hexdigest()
_PK2 = "bb" * 32
_NID2 = hashlib.sha256(bytes.fromhex(_PK2)).hexdigest()
_PK_UNKNOWN = "cc" * 32

def test_group_policy_creation():
    members = {"key1", "key2"}
    policy = GroupPolicy(
        name="team",
        members=members,
        members_file="members.txt",
        initial_credit=100.0,
        min_balance=-50.0
    )
    assert policy.name == "team"
    assert policy.members == members
    assert policy.members_file == "members.txt"
    assert policy.initial_credit == 100.0
    assert policy.min_balance == -50.0

def test_skill_policy_creation():
    policy = SkillPolicy(
        skill_name="gpu-inference",
        initial_credit=None,
        min_balance=0.0
    )
    assert policy.skill_name == "gpu-inference"
    assert policy.initial_credit is None
    assert policy.min_balance == 0.0

def test_group_policy_members_file_optional():
    policy = GroupPolicy(
        name="public",
        members=set(),
        members_file=None,
        initial_credit=3.0,
        min_balance=-10.0
    )
    assert policy.members_file is None

@pytest.mark.asyncio
async def test_resolve_policy_default():
    node = DHTNode("127.0.0.1", 0)
    await node.start()
    try:
        ic, mb = node._resolve_policy(_PK_UNKNOWN, "echo")
        assert ic == node.policy.initial_credit
        assert mb == node.policy.min_balance
    finally:
        await node.stop()

@pytest.mark.asyncio
async def test_resolve_policy_group_match():
    node = DHTNode("127.0.0.1", 0)
    await node.start()
    try:
        node._group_policies = [
            GroupPolicy("team", {_NID1}, None, 100.0, -100.0)
        ]
        ic, mb = node._resolve_policy(_PK1, "echo")
        assert ic == 100.0
        assert mb == -100.0
    finally:
        await node.stop()

@pytest.mark.asyncio
async def test_resolve_policy_group_no_match():
    node = DHTNode("127.0.0.1", 0)
    await node.start()
    try:
        node._group_policies = [
            GroupPolicy("team", {_NID1}, None, 100.0, -100.0)
        ]
        ic, mb = node._resolve_policy(_PK2, "echo")
        assert ic == node.policy.initial_credit
        assert mb == node.policy.min_balance
    finally:
        await node.stop()

@pytest.mark.asyncio
async def test_resolve_policy_skill_override():
    node = DHTNode("127.0.0.1", 0)
    await node.start()
    try:
        node._skill_policies = {
            "gpu-inference": SkillPolicy("gpu-inference", 0.0, 0.0)
        }
        ic, mb = node._resolve_policy(_PK_UNKNOWN, "gpu-inference")
        assert ic == 0.0
        assert mb == 0.0
    finally:
        await node.stop()

@pytest.mark.asyncio
async def test_resolve_policy_skill_then_group():
    # H-01: group membership can override skill restrictions
    node = DHTNode("127.0.0.1", 0)
    await node.start()
    try:
        node._skill_policies = {
            "gpu-inference": SkillPolicy("gpu-inference", 0.0, 0.0)
        }
        node._group_policies = [
            GroupPolicy("team", {_NID1}, None, 1000.0, -1000.0)
        ]
        ic, mb = node._resolve_policy(_PK1, "gpu-inference")
        assert ic == 1000.0
        assert mb == -1000.0
    finally:
        await node.stop()

@pytest.mark.asyncio
async def test_resolve_policy_first_group_wins():
    node = DHTNode("127.0.0.1", 0)
    await node.start()
    try:
        node._group_policies = [
            GroupPolicy("alpha", {_NID1}, None, 100.0, -100.0),
            GroupPolicy("beta", {_NID1}, None, 200.0, -200.0)
        ]
        ic, mb = node._resolve_policy(_PK1, "echo")
        assert ic == 100.0
        assert mb == -100.0
    finally:
        await node.stop()

@pytest.mark.asyncio
async def test_handle_task_request_uses_resolve_policy():
    node = DHTNode("127.0.0.1", 0)
    await node.start()
    try:
        sk = SigningKey.generate()
        pk = sk.verify_key.encode().hex()
        nid = hashlib.sha256(bytes.fromhex(pk)).hexdigest()

        node._group_policies = [
            GroupPolicy("team", {nid}, None, 1000.0, -1000.0)
        ]

        async def mock_handler(d): return d
        node.register_handler("test", mock_handler)
        # Manually add skill sheet since register_handler doesn't do it
        # Set price to 0.0 so balance stays at 1000.0
        node._own_skills["test"] = SkillSheet("test", "1.0.0", "d", [], {}, {}, price=0.0)

        req = sign_message(TaskRequest(
            task_id="t1",
            requester_node_id="r1",
            requester_host="127.0.0.1",
            requester_port=9999,
            skill_name="test",
            input_data={}
        ), sk)

        await node._handle_task_request(req)
        balance = node.storage.get_ledger_balance(pk)
        assert balance == 1000.0
    finally:
        await node.stop()

@pytest.mark.asyncio
async def test_members_file_loading(tmp_path):
    members_file = tmp_path / "members.txt"
    members_file.write_text("key1\n# comment\n\nkey2\n  key3  \n")

    group_cfg = {
        "members": ["key_initial"],
        "members_file": str(members_file)
    }

    members = set(group_cfg["members"])
    if os.path.exists(members_file):
        with open(members_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    members.add(line)

    assert members == {"key_initial", "key1", "key2", "key3"}

@pytest.mark.asyncio
async def test_group_policy_backward_compat():
    node = DHTNode("127.0.0.1", 0)
    await node.start()
    try:
        node._group_policies = []
        node._skill_policies = {}
        ic, mb = node._resolve_policy(_PK_UNKNOWN, "any")
        assert ic == node.policy.initial_credit
        assert mb == node.policy.min_balance
    finally:
        await node.stop()

@pytest.mark.asyncio
async def test_config_with_groups_and_skills(tmp_path):
    config_toml = """
[policy]
initial_credit = 5.0

[policy.group.team]
members = ["key1"]
initial_credit = 100.0
min_balance = -100.0

[policy.skill.expensive]
initial_credit = 0.0
min_balance = 0.0
"""
    config_file = tmp_path / "knarr.toml"
    config_file.write_text(config_toml)

    from knarr.cli.config import load_config
    config = load_config(config_file, explicit=True)

    policy_cfg = config.get("policy", {})
    policy = Policy(initial_credit=float(policy_cfg.get("initial_credit", 3.0)))

    group_policies = []
    for group_name, group_cfg in policy_cfg.get("group", {}).items():
        group_policies.append(GroupPolicy(
            name=group_name,
            members=set(group_cfg.get("members", [])),
            members_file=group_cfg.get("members_file"),
            initial_credit=float(group_cfg.get("initial_credit", policy.initial_credit)),
            min_balance=float(group_cfg.get("min_balance", -10.0)),
        ))

    skill_policies = {}
    for skill_name, skill_cfg in policy_cfg.get("skill", {}).items():
        skill_policies[skill_name.lower()] = SkillPolicy(
            skill_name=skill_name.lower(),
            initial_credit=float(skill_cfg["initial_credit"]) if "initial_credit" in skill_cfg else None,
            min_balance=float(skill_cfg["min_balance"]) if "min_balance" in skill_cfg else None,
        )

    assert len(group_policies) == 1
    assert group_policies[0].name == "team"
    assert group_policies[0].initial_credit == 100.0

    assert "expensive" in skill_policies
    assert skill_policies["expensive"].initial_credit == 0.0

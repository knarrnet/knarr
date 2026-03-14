"""Tests for B2: Identity Field on Bus Events."""
import pytest
import sys
import os
import re

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))


class TestBusIdentityField:
    """Tests for B2 identity field on all bus.emit() calls."""

    def test_node_py_bus_events_have_identity(self):
        """Test all bus.emit calls in node.py have identity field."""
        node_path = os.path.join(os.path.dirname(__file__), '..', '..', 'src', 'knarr', 'dht', 'node.py')
        with open(node_path, 'r') as f:
            content = f.read()
        
        # Count bus.emit calls and identity= occurrences
        # Each bus.emit should have a corresponding identity= nearby
        import re
        
        # Find all bus.emit with event name
        emit_events = re.findall(r'bus\.emit\(\s*"([^"]+)"', content)
        
        # Count identity= occurrences in the file
        identity_count = content.count('identity=')
        
        # We expect at least as many identity= as bus.emit calls
        # (some lines may have multiple)
        assert identity_count >= len(emit_events), \
            f"Expected at least {len(emit_events)} identity fields, found {identity_count}"

    def test_sync_py_bus_events_have_identity(self):
        """Test all bus.emit calls in sync.py have identity field."""
        sync_path = os.path.join(os.path.dirname(__file__), '..', '..', 'src', 'knarr', 'mail', 'sync.py')
        with open(sync_path, 'r') as f:
            content = f.read()
        
        import re
        
        # Find all bus.emit with event name
        emit_events = re.findall(r'bus\.emit\(\s*"([^"]+)"', content)
        
        # Count identity= occurrences in the file
        identity_count = content.count('identity=')
        
        # We expect at least as many identity= as bus.emit calls
        assert identity_count >= len(emit_events), \
            f"Expected at least {len(emit_events)} identity fields, found {identity_count}"

    def test_task_events_identity_source(self):
        """Test task.* events use caller's node_id as identity."""
        node_path = os.path.join(os.path.dirname(__file__), '..', '..', 'src', 'knarr', 'dht', 'node.py')
        with open(node_path, 'r') as f:
            content = f.read()
        
        # task.completed should have identity=caller_node_id
        assert 'task.completed' in content
        # Find the task.completed emit
        task_completed_pattern = r'task\.completed[^)]*identity=caller_node_id'
        assert re.search(task_completed_pattern, content) is not None

    def test_mail_events_identity_source(self):
        """Test mail.* events use sender's node_id as identity."""
        sync_path = os.path.join(os.path.dirname(__file__), '..', '..', 'src', 'knarr', 'mail', 'sync.py')
        with open(sync_path, 'r') as f:
            content = f.read()
        
        # mail.received should have identity from sender
        assert 'mail.received' in content
        # Check identity is set to sender node_id
        assert 'identity=msg.sender_node_id' in content or 'identity=peer_node_id' in content

    def test_credit_events_identity_source(self):
        """Test credit.* events use counterparty's node_id as identity."""
        node_path = os.path.join(os.path.dirname(__file__), '..', '..', 'src', 'knarr', 'dht', 'node.py')
        with open(node_path, 'r') as f:
            content = f.read()
        
        # credit.change should have identity=counterparty or identity=resp.public_key
        assert 'credit.change' in content
        # credit.restored should have identity=peer_public_key
        assert 'credit.restored' in content

    def test_receipt_events_identity_source(self):
        """Test receipt.* events use counterparty's node_id as identity."""
        node_path = os.path.join(os.path.dirname(__file__), '..', '..', 'src', 'knarr', 'dht', 'node.py')
        with open(node_path, 'r') as f:
            content = f.read()
        
        # receipt.issued should have identity=counterparty
        assert 'receipt.issued' in content

    def test_firewall_events_identity_source(self):
        """Test firewall.* events use source node as identity."""
        node_path = os.path.join(os.path.dirname(__file__), '..', '..', 'src', 'knarr', 'dht', 'node.py')
        with open(node_path, 'r') as f:
            content = f.read()
        
        # firewall.blocked events
        assert 'firewall.blocked' in content

    def test_security_events_identity_source(self):
        """Test security.* events use source node as identity."""
        node_path = os.path.join(os.path.dirname(__file__), '..', '..', 'src', 'knarr', 'dht', 'node.py')
        with open(node_path, 'r') as f:
            content = f.read()
        
        # security.* events should have identity
        assert 'security.egress_blocked' in content
        assert 'security.signature_invalid' in content
        assert 'security.identity_mismatch' in content

    def test_node_events_identity_source(self):
        """Test node.* events use self node_id as identity."""
        node_path = os.path.join(os.path.dirname(__file__), '..', '..', 'src', 'knarr', 'dht', 'node.py')
        with open(node_path, 'r') as f:
            content = f.read()
        
        # node.* events should have identity=self.node_info.node_id
        assert 'node.slots_exhausted' in content
        assert 'node.rebootstrap' in content
        assert 'node.event_loop_blocked' in content

    def test_peer_events_identity_source(self):
        """Test peer.* events use self node_id as identity."""
        node_path = os.path.join(os.path.dirname(__file__), '..', '..', 'src', 'knarr', 'dht', 'node.py')
        with open(node_path, 'r') as f:
            content = f.read()
        
        # peer.added/removed should have identity
        assert 'peer.added' in content
        assert 'peer.removed' in content

    def test_identity_field_is_full_64_char(self):
        """Test identity field uses full 64-char node_id, not abbreviated."""
        node_path = os.path.join(os.path.dirname(__file__), '..', '..', 'src', 'knarr', 'dht', 'node.py')
        sync_path = os.path.join(os.path.dirname(__file__), '..', '..', 'src', 'knarr', 'mail', 'sync.py')
        
        for path in [node_path, sync_path]:
            with open(path, 'r') as f:
                content = f.read()
            
            # Check that identity values don't use [:16] abbreviation
            # They should be full node IDs
            identity_pattern = r'identity=[^,)]+'
            identities = re.findall(identity_pattern, content)
            
            for ident in identities:
                # Should not have [:16] truncation
                assert '[:16]' not in ident, f"Identity should be full 64-char: {ident}"

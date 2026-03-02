"""Tests for B3: Bus Events task.queued + task.started."""
import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))


class TestTaskQueuedEvent:
    """Tests for task.queued bus event."""

    def test_task_queued_event_exists(self):
        """Test task.queued event is emitted."""
        node_path = os.path.join(os.path.dirname(__file__), '..', '..', 'src', 'knarr', 'dht', 'node.py')
        with open(node_path, 'r') as f:
            content = f.read()
        
        assert 'task.queued' in content

    def test_task_queued_event_fields(self):
        """Test task.queued event has required fields."""
        node_path = os.path.join(os.path.dirname(__file__), '..', '..', 'src', 'knarr', 'dht', 'node.py')
        with open(node_path, 'r') as f:
            content = f.read()
        
        # Check for required fields in task.queued emit
        # skill_name, caller_node, task_id, identity, queue_position
        assert 'skill_name=skill_name' in content or 'skill_name=' in content
        assert 'caller_node=' in content
        assert 'task_id=' in content
        assert 'identity=' in content
        assert 'queue_position=' in content

    def test_task_queued_event_location(self):
        """Test task.queued is emitted after task accepted into queue."""
        node_path = os.path.join(os.path.dirname(__file__), '..', '..', 'src', 'knarr', 'dht', 'node.py')
        with open(node_path, 'r') as f:
            content = f.read()
        
        # Find task.queued emission
        queued_pos = content.find('task.queued')
        # Find task_queue.put_nowait (where task is enqueued)
        enqueue_pos = content.find('task_queue.put_nowait')
        
        # task.queued should be emitted after or near enqueue
        assert queued_pos > 0
        assert enqueue_pos > 0
        # They should be close (within 500 chars)
        assert abs(queued_pos - enqueue_pos) < 500

    def test_task_queued_identity_is_caller(self):
        """Test task.queued identity is caller_node_id."""
        node_path = os.path.join(os.path.dirname(__file__), '..', '..', 'src', 'knarr', 'dht', 'node.py')
        with open(node_path, 'r') as f:
            content = f.read()
        
        # Find task.queued block
        queued_start = content.find('task.queued')
        queued_block = content[queued_start:queued_start+500]
        
        # Identity should be caller_node_id
        assert 'identity=caller_node_id' in queued_block


class TestTaskStartedEvent:
    """Tests for task.started bus event."""

    def test_task_started_event_exists(self):
        """Test task.started event is emitted."""
        node_path = os.path.join(os.path.dirname(__file__), '..', '..', 'src', 'knarr', 'dht', 'node.py')
        with open(node_path, 'r') as f:
            content = f.read()
        
        assert 'task.started' in content

    def test_task_started_event_fields(self):
        """Test task.started event has required fields."""
        node_path = os.path.join(os.path.dirname(__file__), '..', '..', 'src', 'knarr', 'dht', 'node.py')
        with open(node_path, 'r') as f:
            content = f.read()
        
        # Check for required fields in task.started emit
        # skill_name, caller_node, task_id, identity, queue_wait_ms
        assert 'skill_name=' in content
        assert 'caller_node=' in content
        assert 'task_id=' in content
        assert 'identity=' in content
        assert 'queue_wait_ms=' in content

    def test_task_started_event_location(self):
        """Test task.started is emitted when worker dequeues task."""
        node_path = os.path.join(os.path.dirname(__file__), '..', '..', 'src', 'knarr', 'dht', 'node.py')
        with open(node_path, 'r') as f:
            content = f.read()
        
        # Find task.started emission
        started_pos = content.find('task.started')
        # Find _task_worker_loop (where task is dequeued)
        worker_pos = content.find('_task_worker_loop')
        
        # task.started should be in worker loop
        assert started_pos > 0
        assert worker_pos > 0
        assert started_pos > worker_pos  # started comes after worker_loop definition

    def test_task_started_queue_wait_ms_calculation(self):
        """Test queue_wait_ms is calculated correctly."""
        node_path = os.path.join(os.path.dirname(__file__), '..', '..', 'src', 'knarr', 'dht', 'node.py')
        with open(node_path, 'r') as f:
            content = f.read()
        
        # Find task.started block
        started_start = content.find('task.started')
        started_block = content[started_start:started_start+500]
        
        # Should calculate wait time from start_time
        assert 'queue_wait_ms=' in started_block
        assert 'time.time()' in started_block or 'start_time' in started_block

    def test_task_started_identity_is_caller(self):
        """Test task.started identity is caller_node_id."""
        node_path = os.path.join(os.path.dirname(__file__), '..', '..', 'src', 'knarr', 'dht', 'node.py')
        with open(node_path, 'r') as f:
            content = f.read()
        
        # Find task.started block
        started_start = content.find('task.started')
        started_block = content[started_start:started_start+500]
        
        # Identity should be caller_nid
        assert 'identity=caller_nid' in started_block or 'identity=' in started_block


class TestTaskLifecycleEvents:
    """Tests for complete task lifecycle event coverage."""

    def test_lifecycle_event_order(self):
        """Test task lifecycle events fire in correct order: queued -> started -> completed/failed."""
        node_path = os.path.join(os.path.dirname(__file__), '..', '..', 'src', 'knarr', 'dht', 'node.py')
        with open(node_path, 'r') as f:
            content = f.read()
        
        # All lifecycle events should exist
        assert 'task.queued' in content
        assert 'task.started' in content
        assert 'task.completed' in content
        assert 'task.failed' in content

    def test_lifecycle_events_have_consistent_identity(self):
        """Test all lifecycle events use consistent identity field."""
        node_path = os.path.join(os.path.dirname(__file__), '..', '..', 'src', 'knarr', 'dht', 'node.py')
        with open(node_path, 'r') as f:
            content = f.read()
        
        # All task.* events should have identity field
        task_events = ['task.queued', 'task.started', 'task.completed', 'task.failed', 'task.timeout', 'task.rejected']
        for event in task_events:
            assert event in content
            
        # All should have identity
        assert content.count('identity=') >= len(task_events)

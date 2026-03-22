import hashlib
import time
from typing import List, Dict, Any, Optional

class ProviderCache:
    """In-memory cache for skill providers."""
    
    def __init__(self, max_records: int = 50000):
        self.max_records = max_records
        # sha256(skill_key) -> {node_id -> {host, port, sidecar_port, stored_at, ttl, skill_key}}
        self.cache: Dict[str, Dict[str, Dict]] = {}
        self._total_records = 0

    def _get_key(self, skill_key: str) -> str:
        return hashlib.sha256(skill_key.encode()).hexdigest()

    def store(self, skill_key: str, node_id: str, host: str, port: int, sidecar_port: int, ttl: int = 1800):
        """Store or update a provider record."""
        key = self._get_key(skill_key)
        now = time.monotonic()
        
        if key not in self.cache:
            self.cache[key] = {}
            
        if node_id not in self.cache[key]:
            # Enforce max records
            if self._total_records >= self.max_records:
                self._evict_oldest()
            self._total_records += 1
            
        self.cache[key][node_id] = {
            "node_id": node_id,
            "host": host,
            "port": port,
            "sidecar_port": sidecar_port,
            "stored_at": now,
            "ttl": ttl,
            "skill_key": skill_key  # Store original key for searching
        }

    def _evict_oldest(self):
        """Evict the oldest record in the cache."""
        oldest_at = float('inf')
        oldest_key = None
        oldest_node = None
        
        for key, providers in self.cache.items():
            for node_id, record in providers.items():
                if record["stored_at"] < oldest_at:
                    oldest_at = record["stored_at"]
                    oldest_key = key
                    oldest_node = node_id
                    
        if oldest_key and oldest_node:
            del self.cache[oldest_key][oldest_node]
            if not self.cache[oldest_key]:
                del self.cache[oldest_key]
            self._total_records -= 1

    def get_providers(self, skill_key: str) -> List[Dict]:
        """Return non-expired provider records for this skill key."""
        key = self._get_key(skill_key)
        providers = self.cache.get(key, {})
        now = time.monotonic()
        
        results = []
        for node_id, record in providers.items():
            if now - record["stored_at"] <= record["ttl"]:
                results.append(record)
        return results

    def search(self, query_value: str, query_type: str) -> List[Dict]:
        """Search cached providers by name or all."""
        now = time.monotonic()
        results = []
        
        if query_type == "all":
            for providers in self.cache.values():
                for record in providers.values():
                    if now - record["stored_at"] <= record["ttl"]:
                        results.append(record)
        elif query_type == "name":
            query_val_lower = query_value.lower()
            for providers in self.cache.values():
                for record in providers.values():
                    if record["skill_key"].lower() == query_val_lower:
                        if now - record["stored_at"] <= record["ttl"]:
                            results.append(record)
                            
        return results

    def remove(self, skill_key: str, node_id: str):
        """Remove a specific provider record."""
        key = self._get_key(skill_key)
        if key in self.cache and node_id in self.cache[key]:
            del self.cache[key][node_id]
            self._total_records -= 1
            if not self.cache[key]:
                del self.cache[key]
            return True
        return False

    def evict_expired(self):
        """Remove all expired records."""
        now = time.monotonic()
        to_delete_keys = []
        
        for key, providers in self.cache.items():
            to_delete_nodes = [
                node_id for node_id, record in providers.items()
                if now - record["stored_at"] > record["ttl"]
            ]
            for node_id in to_delete_nodes:
                del providers[node_id]
                self._total_records -= 1
            if not providers:
                to_delete_keys.append(key)
                
        for key in to_delete_keys:
            del self.cache[key]

    def stats(self) -> Dict[str, int]:
        """Return cache statistics."""
        return {
            "total_records": self._total_records,
            "unique_skills": len(self.cache),
            "unique_providers": len(set(
                node_id for providers in self.cache.values() for node_id in providers
            ))
        }

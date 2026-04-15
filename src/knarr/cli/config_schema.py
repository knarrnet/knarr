from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class SectionSchema:
    name: str
    known_keys: frozenset[str] = field(default_factory=frozenset)
    allow_namespaced: bool = False


NODE_CONFIG_SCHEMA = [
    SectionSchema(
        "node",
        frozenset({
            "port", "host", "storage", "advertise_host", "sidecar_port", "max_asset_size",
            "max_task_timeout", "task_slots", "min_protocol_version",
            "auto_upgrade", "backup_retention_days", "wallet", "jurisdiction",
            "event_bus_size", "event_bus_debug", "max_queue_depth",
            "log_retention", "log_retention_hours", "housekeeping_retention_days",
            "startup_jitter", "sweep_interval",
            "implicit_hb_types", "pools", "tls", "tls_required", "tls_pin_certs",
            "identity_bus_size",
        }),
    ),
    SectionSchema(
        "network",
        frozenset({
            "bootstrap", "upnp", "tls_cert", "tls_key", "max_connections",
            "connection_idle_timeout", "gossip_fanout", "heartbeat_silence_threshold",
            "peer_dead_timeout", "min_peers",
        }),
    ),
    SectionSchema(
        "skills",
        frozenset({"minimum_price", "default_timeout"}),
        allow_namespaced=True,
    ),
    SectionSchema("bridges", allow_namespaced=True),
    SectionSchema(
        "policy",
        frozenset({"initial_credit", "min_balance", "tit_for_tat", "group", "skill"}),
    ),
    SectionSchema(
        "mail",
        frozenset({
            "accept_from", "default_ttl_hours", "max_messages", "whitelist", "price",
            "debug", "stale_inbox_hours", "max_inbox", "pull_interval", "max_pull_batch",
            "accept_groups",
        }),
    ),
    SectionSchema(
        "cockpit",
        frozenset({"port", "bind", "auth_token", "tls", "tls_cert", "tls_key", "allowed_ips"}),
    ),
    SectionSchema(
        "economy",
        frozenset({"default_soft_limit", "default_hard_limit", "settlement_min_interval_seconds"}),
    ),
    SectionSchema(
        "settlement",
        frozenset({"tab_reminder_auto_netting", "tab_reminder_threshold", "netting_interval", "consumer_interval"}),
    ),
    SectionSchema("pricing"),
    SectionSchema("netting"),
    SectionSchema("prepaid"),
    SectionSchema("sidecar", frozenset({"asset_dir"})),
    SectionSchema("warehouse_manager", frozenset({"enabled", "debug", "rules"})),
    SectionSchema("token", frozenset({"mint", "rpc_url"})),
    SectionSchema("static", frozenset({"enabled", "max_deployments", "max_extracted_size"})),
    SectionSchema("peer_overrides", allow_namespaced=True),
    SectionSchema("identities", allow_namespaced=True),
    SectionSchema("tor", allow_namespaced=True),
]

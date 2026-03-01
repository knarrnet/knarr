"""TLS certificate generation for Knarr nodes.

Two cert types:
- Ed25519: protocol/sidecar (identity-derived, not browser-compatible)
- ECDSA P-256: cockpit (browser-compatible)
"""
import datetime
import ipaddress
import logging
import os
import ssl

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey, SECP256R1, generate_private_key, ECDSA
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.x509.oid import NameOID

logger = logging.getLogger(__name__)


def generate_tls_cert(signing_key_bytes: bytes, node_id: str, config_dir: str,
                      days: int = 365, force: bool = False):
    """Generate self-signed TLS cert from node's Ed25519 identity key.

    Args:
        signing_key_bytes: Raw 32-byte Ed25519 seed (same as node identity).
        node_id: Hex node_id (SHA-256 of public key). CN uses first 16 chars.
        config_dir: Directory for cert.pem and key.pem.
        days: Certificate validity period.
        force: Overwrite existing files.

    Returns:
        (cert_path, key_path) tuple.
    """
    cert_path = os.path.join(config_dir, "cert.pem")
    key_path = os.path.join(config_dir, "key.pem")

    if os.path.exists(cert_path) and os.path.exists(key_path) and not force:
        return cert_path, key_path

    private_key = Ed25519PrivateKey.from_private_bytes(signing_key_bytes)

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, node_id[:16]),
    ])

    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=days))
        .sign(private_key, algorithm=None)
    )

    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))

    with open(key_path, "wb") as f:
        f.write(private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
    os.chmod(key_path, 0o600)

    logger.info(f"Generated TLS certificate (valid {days} days)")
    return cert_path, key_path


def generate_cockpit_cert(node_id: str, config_dir: str,
                          days: int = 365, force: bool = False):
    """Generate ECDSA P-256 self-signed cert for the cockpit (browser-compatible).

    Args:
        node_id: Hex node_id for CN.
        config_dir: Directory for cockpit-cert.pem and cockpit-key.pem.
        days: Certificate validity period.
        force: Overwrite existing files.

    Returns:
        (cert_path, key_path) tuple.
    """
    cert_path = os.path.join(config_dir, "cockpit-cert.pem")
    key_path = os.path.join(config_dir, "cockpit-key.pem")

    if os.path.exists(cert_path) and os.path.exists(key_path) and not force:
        return cert_path, key_path

    private_key = generate_private_key(SECP256R1())

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, node_id[:16]),
    ])

    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=days))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
            ]),
            critical=False,
        )
        .sign(private_key, SHA256())
    )

    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))

    with open(key_path, "wb") as f:
        f.write(private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
    os.chmod(key_path, 0o600)

    logger.info(f"Generated cockpit ECDSA certificate (valid {days} days)")
    return cert_path, key_path


def resolve_cert_paths(config: dict, config_dir: str):
    """Resolve cert and key paths from config, with defaults.

    Returns:
        (cert_path, key_path) tuple.
    """
    network = config.get("network", {})
    cert_rel = network.get("tls_cert", "cert.pem")
    key_rel = network.get("tls_key", "key.pem")

    cert_path = cert_rel if os.path.isabs(cert_rel) else os.path.join(config_dir, cert_rel)
    key_path = key_rel if os.path.isabs(key_rel) else os.path.join(config_dir, key_rel)

    return cert_path, key_path


def resolve_cockpit_cert_paths(config: dict, config_dir: str):
    """Resolve cockpit cert paths. Falls back to ECDSA cockpit-cert.pem.

    Config: [cockpit] tls_cert / tls_key override the defaults.
    """
    cockpit = config.get("cockpit", {})
    cert_rel = cockpit.get("tls_cert", "cockpit-cert.pem")
    key_rel = cockpit.get("tls_key", "cockpit-key.pem")

    cert_path = cert_rel if os.path.isabs(cert_rel) else os.path.join(config_dir, cert_rel)
    key_path = key_rel if os.path.isabs(key_rel) else os.path.join(config_dir, key_rel)

    return cert_path, key_path


def create_server_ssl_context(cert_path: str, key_path: str) -> ssl.SSLContext:
    """Create TLS server context for the sidecar."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert_path, key_path)
    return ctx


def create_client_ssl_context() -> ssl.SSLContext:
    """Create TLS client context for outbound sidecar connections.

    Phase 1: CERT_NONE — wire is encrypted, identity verified by
    Ed25519 message signatures (already implemented).
    """
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx

"""
Hubble TLS certificate auto-discovery.

Fetches Hubble relay client certs from Kubernetes secrets and writes
them to a temp directory for use with gRPC channel credentials.

Secret discovery order:
  1. Secret named by HUBBLE_TLS_SECRET env var (explicit override)
  2. hubble-relay-client-certs  in CILIUM_NAMESPACE (default: kube-system)
  3. hubble-server-certs        in CILIUM_NAMESPACE
  4. cilium-ca                  in CILIUM_NAMESPACE  (CA-only, no mTLS)

If none found and HUBBLE_TLS=true: raises RuntimeError.
If HUBBLE_TLS is unset/false: returns None (plaintext connection).
"""

import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Secret key names used by Cilium
_CA_KEYS = ("ca.crt", "tls.ca")
_CERT_KEYS = ("tls.crt",)
_KEY_KEYS = ("tls.key",)

# Candidate secret names in discovery order
_SECRET_CANDIDATES = [
    "hubble-relay-client-certs",
    "hubble-server-certs",
    "cilium-ca",
]


@dataclass
class TLSConfig:
    """Paths to TLS cert files for gRPC credentials."""

    ca_cert: str
    client_cert: Optional[str] = None  # None = server-side TLS only
    client_key: Optional[str] = None

    @property
    def has_mtls(self) -> bool:
        return self.client_cert is not None and self.client_key is not None


def _load_k8s_client():
    """Load Kubernetes client (in-cluster preferred, kubeconfig fallback)."""
    try:
        from kubernetes import client, config
        try:
            config.load_incluster_config()
            logger.debug("Loaded in-cluster kubeconfig")
        except config.ConfigException:
            config.load_kube_config()
            logger.debug("Loaded kubeconfig from file")
        return client.CoreV1Api()
    except ImportError:
        raise RuntimeError(
            "kubernetes package is required for TLS auto-discovery. "
            "Install with: pip install kubernetes"
        )


def _extract_data(secret_data: dict, keys: tuple) -> Optional[bytes]:
    """Extract and decode the first matching key from secret data."""
    import base64
    for key in keys:
        if key in secret_data:
            raw = secret_data[key]
            if isinstance(raw, str):
                return base64.b64decode(raw)
            return raw
    return None


def _fetch_secret(v1, namespace: str, name: str) -> Optional[dict]:
    """Fetch a secret by name, returning None if not found."""
    try:
        secret = v1.read_namespaced_secret(name=name, namespace=namespace)
        return secret.data or {}
    except Exception as exc:
        # 404 is expected during discovery; log other errors
        msg = str(exc)
        if "404" not in msg and "Not Found" not in msg:
            logger.debug("Could not read secret %s/%s: %s", namespace, name, exc)
        return None


def gather_tls_certs(tmpdir: Optional[str] = None) -> Optional[TLSConfig]:
    """
    Auto-discover and write Hubble TLS certs to tmpdir.

    Returns TLSConfig with paths to written files, or None if TLS is
    disabled (HUBBLE_TLS not set / set to false).

    Raises RuntimeError if HUBBLE_TLS=true but no certs found.
    """
    use_tls = os.environ.get("HUBBLE_TLS", "").lower() in ("1", "true", "yes")
    if not use_tls:
        logger.info("HUBBLE_TLS not set — connecting to Hubble without TLS")
        return None

    cilium_ns = os.environ.get("CILIUM_NAMESPACE", "kube-system")
    explicit_secret = os.environ.get("HUBBLE_TLS_SECRET", "")

    candidates = (
        [(explicit_secret, cilium_ns)] if explicit_secret
        else [(name, cilium_ns) for name in _SECRET_CANDIDATES]
    )

    logger.info("Auto-discovering Hubble TLS certs (namespace=%s)...", cilium_ns)

    v1 = _load_k8s_client()
    secret_data: Optional[dict] = None
    found_secret = ""

    for name, ns in candidates:
        data = _fetch_secret(v1, ns, name)
        if data:
            secret_data = data
            found_secret = f"{ns}/{name}"
            logger.info("Found Hubble TLS secret: %s", found_secret)
            break

    if not secret_data:
        tried = ", ".join(f"{ns}/{n}" for n, ns in candidates)
        raise RuntimeError(
            f"HUBBLE_TLS=true but no TLS secret found. Tried: {tried}. "
            "Set HUBBLE_TLS_SECRET or CILIUM_NAMESPACE to override."
        )

    ca_bytes = _extract_data(secret_data, _CA_KEYS)
    cert_bytes = _extract_data(secret_data, _CERT_KEYS)
    key_bytes = _extract_data(secret_data, _KEY_KEYS)

    if not ca_bytes:
        raise RuntimeError(
            f"Secret {found_secret} has no CA cert (tried keys: {_CA_KEYS})"
        )

    # Write certs to temp directory
    out_dir = Path(tmpdir) if tmpdir else Path(tempfile.mkdtemp(prefix="hubble-tls-"))
    out_dir.mkdir(parents=True, exist_ok=True)

    ca_path = out_dir / "ca.crt"
    ca_path.write_bytes(ca_bytes)
    logger.info("Wrote CA cert to %s", ca_path)

    client_cert_path: Optional[str] = None
    client_key_path: Optional[str] = None

    if cert_bytes and key_bytes:
        client_cert_path = str(out_dir / "tls.crt")
        client_key_path = str(out_dir / "tls.key")
        Path(client_cert_path).write_bytes(cert_bytes)
        Path(client_key_path).write_bytes(key_bytes)
        logger.info("Wrote client cert/key to %s", out_dir)
    else:
        logger.info(
            "No client cert/key in secret %s — using server-side TLS only",
            found_secret,
        )

    return TLSConfig(
        ca_cert=str(ca_path),
        client_cert=client_cert_path,
        client_key=client_key_path,
    )


def build_grpc_credentials(tls: TLSConfig):
    """Build gRPC SSL channel credentials from a TLSConfig."""
    import grpc

    ca_cert = Path(tls.ca_cert).read_bytes()
    cert_chain = Path(tls.client_cert).read_bytes() if tls.client_cert else None
    private_key = Path(tls.client_key).read_bytes() if tls.client_key else None

    return grpc.ssl_channel_credentials(
        root_certificates=ca_cert,
        certificate_chain=cert_chain,
        private_key=private_key,
    )

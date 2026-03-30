"""E-02: knarr URI parser for plugin/resource addressing. v0.54.0

URI format: knarr://[identity_authority]/[plugin_selector]/[resource_path]

Selectors:
  s = skill          (service URIs)
  p = provider       (provider/catalog URIs)
  c = commerce       (commerce/document URIs)
  m = mail           (mail URIs)
  k = kad            (DHT/kademlia URIs)
  g = group          (group URIs)
  o = object/asset   (object store URIs)

Authority:
  empty = anonymous (any provider), e.g. knarr:///s/llm/chat@1.0
  hex   = specific identity node_id, e.g. knarr://deadbeef.../p/catalog
"""
import re
from typing import Tuple

# Valid plugin selectors for routing URIs
PLUGIN_SELECTORS = frozenset({"s", "p", "c", "m", "k", "g", "o"})

# knarr://[authority]/[single-char selector]/[resource_path]
# Authority may be empty (three-slash) or a hex node_id
_URI_RE = re.compile(
    r'^knarr://([^/]*)/([a-z])/(.*)$'
)


def parse_knarr_uri(uri: object) -> Tuple[str, str, str]:
    """Parse knarr URI -> (identity_authority, plugin_selector, resource_path).
    Returns ("", "", "") for empty/invalid URI.

    Examples:
        parse_knarr_uri("knarr:///s/llm/chat@1.0")
            -> ("", "s", "llm/chat@1.0")
        parse_knarr_uri("knarr://deadbeef.../p/catalog")
            -> ("deadbeef...", "p", "catalog")
        parse_knarr_uri("") -> ("", "", "")
        parse_knarr_uri(None) -> ("", "", "")
    """
    if not uri or not isinstance(uri, str):
        return ("", "", "")
    m = _URI_RE.match(uri)
    if not m:
        return ("", "", "")
    authority = m.group(1)
    selector = m.group(2)
    resource = m.group(3)
    if selector not in PLUGIN_SELECTORS:
        return ("", "", "")
    # TP-6: Validate authority is empty or a valid hex node_id fragment
    if authority and not re.match(r'^[a-f0-9]{1,64}$', authority):
        return ("", "", "")
    # TP-6: Reject path traversal and enforce length cap
    if ".." in resource or len(resource) > 1024:
        return ("", "", "")
    return (authority, selector, resource)

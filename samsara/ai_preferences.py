"""Pure AI preference transactions and consent contracts (onboarding package A).

No model calls, probes, persistence, cancellation or Qt. Package B must consult
effective_state at every route and invalidate in-flight work when applying Off.
Readiness and DNS verification are caller-supplied observations, never consent.
"""
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import ipaddress
from urllib.parse import urlsplit

from samsara.config_defaults import cfg_get

CONSENT_VERSION = 2
FEATURES = frozenset({"ava", "editing"})
POLICIES = frozenset({"off", "local", "cloud"})


def _section(config, key):
    value = config.get(key, {})
    return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class Provider:
    identity: str
    origin: str
    location: str  # loopback / remote / unverified

    def record(self) -> dict:
        return {"identity": self.identity, "origin": self.origin}


def identify_provider(identity: str, endpoint: str, *, resolved_addresses=()) -> Provider:
    """Normalize an origin without IO. Only numeric/resolved loopback is local.

    An unresolved hostname, including localhost, is not verification. The runtime
    must supply current DNS results and prevent redirects/rebinding when sending.
    Reject userinfo, query/fragment and malformed ports; never put secrets in records.
    """
    if not isinstance(identity, str) or not identity or not identity.replace("_", "").isalnum():
        raise ValueError("invalid provider identity")
    if not isinstance(endpoint, str) or any(ch.isspace() for ch in endpoint):
        raise ValueError("invalid endpoint")
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise ValueError("endpoint must be an HTTP(S) URL without credentials/query/fragment")
    host = parsed.hostname.lower()
    if "%" in host or "\\" in host:
        raise ValueError("unsupported endpoint host")
    port = parsed.port
    if port == 0:
        raise ValueError("invalid endpoint port")
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            addresses = [ipaddress.ip_address(item) for item in resolved_addresses]
        except ValueError as exc:
            raise ValueError("invalid resolved address") from exc
    location = "loopback" if addresses and all(ip.is_loopback for ip in addresses) else (
        "remote" if addresses else "unverified")
    authority = f"[{host}]" if ":" in host else host
    return Provider(identity.lower(), f"{parsed.scheme}://{authority}:{port}", location)


def provider_for(config: dict, policy: str, *, resolved_addresses=()) -> Provider:
    if policy == "local":
        return identify_provider("ollama", cfg_get(config, "ollama.host"), resolved_addresses=resolved_addresses)
    if policy == "cloud":
        # Reuse the runtime's closed provider list; ignore legacy redirect maps.
        from samsara.cloud_llm import BUILTIN_PROVIDERS
        identity = cfg_get(config, "cloud_llm.provider")
        if identity not in BUILTIN_PROVIDERS:
            raise ValueError("unsupported cloud provider")
        endpoint = BUILTIN_PROVIDERS[identity]["base_url"]
        value = identify_provider(identity, endpoint)
        return Provider(value.identity, value.origin, "remote")
    raise ValueError("Off has no provider")


def _features(features) -> tuple[str, ...]:
    if isinstance(features, str):
        raise ValueError("features must be a collection")
    selected = tuple(sorted(set(features)))
    if not selected or not set(selected) <= FEATURES:
        raise ValueError("select Ava and/or editing")
    return selected


def consent_covers(record: object, *, features, provider: Provider, cloud: bool,
                   version: int = CONSENT_VERSION) -> bool:
    """Check grants and exact destination. v1 may cover only migrated Ava scope."""
    selected = _features(features)
    if not isinstance(record, dict) or not isinstance(record.get("accepted_at"), str) or not record["accepted_at"].strip():
        return False
    if record.get("provider") != provider.record():
        return False
    if type(record.get("version")) is not int:
        return False
    grants = record.get("features")
    if not isinstance(grants, list) or not all(isinstance(v, str) for v in grants):
        return False
    if record.get("version") == 1 and version == CONSENT_VERSION:
        return set(selected) <= {"ava"} and "ava" in grants and (not cloud or record.get("cloud_version") == 1)
    return (record.get("version") == version and set(selected) <= set(grants)
            and (not cloud or record.get("cloud_version") == version))


def accept_consent(config: dict, *, features=("ava",), provider: Provider,
                   cloud: bool = False, accepted_at: str | None = None,
                   version: int = CONSENT_VERSION) -> dict:
    """EXPLICIT ACCEPT ONLY. The sole helper allowed to generate a timestamp.

    This does not enable features. A caller must present the disclosure and
    receive explicit acceptance before calling, then apply_preferences atomically.
    """
    selected = _features(features)
    verified = identify_provider(provider.identity, provider.origin)
    old = _section(config, "ava").get("consent", {})
    old = old if isinstance(old, dict) else {}
    same = old.get("provider") == verified.record()
    previously_accepted = (same and type(old.get("version")) is int
                          and old.get("version") == version
                          and isinstance(old.get("accepted_at"), str)
                          and bool(old["accepted_at"].strip()))
    previous = old.get("features", []) if previously_accepted else []
    if not isinstance(previous, list):
        previous = []
    grants = sorted(set(selected) | {f for f in previous if isinstance(f, str) and f in FEATURES})
    if accepted_at is not None and (not isinstance(accepted_at, str) or not accepted_at.strip()):
        raise ValueError("acceptance needs a nonempty timestamp")
    return {"version": version, "accepted_at": accepted_at if accepted_at is not None else datetime.now(timezone.utc).isoformat(),
            "cloud_version": version if cloud else (old.get("cloud_version") if previously_accepted else None),
            "features": grants, "provider": verified.record()}


def apply_preferences(config: dict, *, policy: str, ava: bool = False,
                      editing: bool = False, consent: dict | None = None,
                      local_model: str | None = None, resolved_addresses=(),
                      requested_ai: str | None = None) -> dict:
    """One validated, nonmutating full-config transaction implementing §4.

    Config already contains provider credentials/model selection from the existing
    credential/settings path. Off preserves credentials, models and prior grants.
    A pending card choice calls Off with requested_ai; it never accepts consent.
    """
    if policy not in POLICIES or type(ava) is not bool or type(editing) is not bool:
        raise ValueError("invalid AI selection")
    if requested_ai is not None and requested_ai not in POLICIES:
        raise ValueError("invalid requested AI route")
    if policy != "off" and requested_ai is not None and requested_ai != policy:
        raise ValueError("enabled route must match the requested route")
    if policy == "off" and (ava or editing or consent is not None):
        raise ValueError("Off cannot enable features or accept consent")
    active = policy != "off"
    selected = [name for name, enabled in (("ava", ava), ("editing", editing)) if enabled]
    if active:
        _features(selected)
        provider = provider_for(config, policy, resolved_addresses=resolved_addresses)
        if policy == "local" and provider.location != "loopback":
            raise ValueError("local setup needs verified loopback; remote Ollama needs separate consent")
        record = consent if consent is not None else _section(config, "ava").get("consent")
        if not consent_covers(record, features=selected, provider=provider, cloud=policy == "cloud"):
            raise ValueError("explicit feature/provider consent required")
        if policy == "cloud" and not _section(config, "cloud_llm").get("api_key"):
            raise ValueError("cloud setup needs a credential from the existing settings path")
        if policy == "local" and (not isinstance(local_model, str) or not local_model.strip()):
            raise ValueError("local setup needs an explicitly selected model")
    result = deepcopy(config)
    for section in ("ava", "ava_command_session", "ava_edit", "command_packs", "ollama",
                    "cloud_llm", "smart_corrections", "smart_actions"):
        result[section] = deepcopy(_section(config, section))
    result["ava"].update(provider_policy=policy, warm_on_boot=False)
    result["ava_command_session"].update(enabled=ava, backend="cloud" if policy == "cloud" else "ollama",
                                          keep_warm=ava and policy == "local")
    result["ava_edit"]["enabled"] = editing
    result["command_packs"]["ai"] = ava
    result["ollama"]["enabled"] = active and policy == "local"
    result["cloud_llm"].update(enabled=active and policy == "cloud", web_search=False)
    result["smart_corrections"].update(enabled=False, allow_cloud_fallback=False)
    result["smart_actions"]["enabled"] = False
    if active:
        result["ava"]["consent"] = deepcopy(record)
    if policy == "local":
        result["ollama"]["model"] = local_model.strip()
        for feature, section in ((ava, "ava_command_session"), (editing, "ava_edit")):
            if feature:
                result[section]["model"] = local_model.strip()
    from samsara.onboarding.state import migrate_onboarding, validate_state
    result = migrate_onboarding(result)
    result["onboarding"]["requested_ai"] = requested_ai if requested_ai is not None else policy
    result["onboarding"] = validate_state(result["onboarding"])
    return result


def apply_accepted_ava(config: dict, *, policy: str, consent: dict) -> dict:
    """Build the full persisted config transaction after explicit Ava consent.

    Enables only the Ava command-session feature. Existing editing preference
    data is preserved, but its own provider-scoped consent gate remains in
    force if this acceptance did not grant editing.
    """
    session = _section(config, "ava_command_session")
    local_model = session.get("model") or _section(config, "ollama").get("model")
    resolved_addresses = ()
    if policy == "local":
        try:
            host = urlsplit(cfg_get(config, "ollama.host")).hostname
        except (TypeError, ValueError):
            host = None
        if host and host.lower() == "localhost":
            # Same explicit loopback treatment as runtime_state; no DNS/network
            # lookup is performed by this preference transaction.
            resolved_addresses = ("127.0.0.1", "::1")

    result = apply_preferences(
        config, policy=policy, ava=True, editing=False, consent=consent,
        local_model=local_model, resolved_addresses=resolved_addresses,
    )
    result["ava_edit"] = deepcopy(_section(config, "ava_edit"))
    return result


@dataclass(frozen=True)
class EffectiveState:
    status: str  # allowed / off-by-choice / setup-pending / unavailable
    reason: str
    provider: Provider | None = None

    @property
    def allowed(self) -> bool:
        return self.status == "allowed"


def effective_state(config: dict, feature: str, *, provider_route: str | None = None,
                    ready: bool | None = None, resolved_addresses=()) -> EffectiveState:
    """Fail closed; caller passes readiness for THIS provider, never probes here."""
    if feature not in FEATURES:
        raise ValueError("unknown AI feature")
    policy = _section(config, "ava").get("provider_policy", "off")
    pending = _section(config, "onboarding").get("requested_ai", "off") in {"local", "cloud"}
    if policy == "off":
        reason = (
            "Ava setup is pending; enable Ava in Settings > Modes to finish setup."
            if pending else
            "Ava is disabled in Settings > Modes > Ava Command Session."
        )
        return EffectiveState("setup-pending" if pending else "off-by-choice", reason)
    if policy not in POLICIES:
        return EffectiveState("setup-pending", "Ava's provider selection is invalid in Settings > Modes.")
    if provider_route is not None and provider_route != policy:
        return EffectiveState("off-by-choice", "This Ava route is not enabled for the selected provider in Settings > Modes.")
    section = "ava_command_session" if feature == "ava" else "ava_edit"
    if _section(config, section).get("enabled") is not True:
        reason = (
            "Ava command session is disabled in Settings > Modes > Ava Command Session."
            if feature == "ava" else
            "Ava editing is disabled in Settings > Ava / Cloud."
        )
        return EffectiveState("off-by-choice", reason)
    try:
        provider = provider_for(config, policy, resolved_addresses=resolved_addresses)
    except (ValueError, TypeError):
        location = "Settings > Ava / Cloud" if policy == "cloud" else "Settings > Modes > Ava Command Session"
        name = "Cloud AI" if policy == "cloud" else "Ollama"
        return EffectiveState("setup-pending", f"{name} configuration is invalid in {location}.")
    if policy == "local" and provider.location != "loopback":
        return EffectiveState("setup-pending", "Ollama must be running on this computer; check Settings > Modes > Ava Command Session.", provider)
    record = _section(config, "ava").get("consent")
    if not consent_covers(record, features=(feature,), provider=provider, cloud=policy == "cloud"):
        return EffectiveState("setup-pending", "Ava needs consent for this provider; enable Ava in Settings > Modes to review it.", provider)
    provider_config = _section(config, "ollama" if policy == "local" else "cloud_llm")
    if provider_config.get("enabled") is not True:
        name = "Ollama" if policy == "local" else "Cloud AI"
        location = "Settings > Modes > Ava Command Session" if policy == "local" else "Settings > Ava / Cloud"
        return EffectiveState("setup-pending", f"{name} is disabled in {location}.", provider)
    if policy == "cloud" and not provider_config.get("api_key"):
        return EffectiveState("setup-pending", "Cloud AI needs its own API key in Settings > Ava / Cloud.", provider)
    if ready is not True:
        if policy == "local":
            reason = "Ollama is not running." if ready is False else "Ollama has not been checked yet."
        else:
            reason = "The configured cloud provider is unavailable." if ready is False else "The cloud provider has not been checked yet."
        return EffectiveState("unavailable" if ready is False else "setup-pending", reason, provider)
    return EffectiveState("allowed", "Ready", provider)


def runtime_state(config: dict, feature: str, *, provider_route: str | None = None) -> EffectiveState:
    """Authorize a runtime route without probing or sending anything.

    ``effective_state`` deliberately requires the caller to provide DNS and
    readiness evidence.  Runtime inference routes must make their policy
    decision *before* they can perform either operation, so the conventional
    local ``localhost`` endpoint is supplied as its fixed loopback addresses
    here.  A custom hostname remains unverified and therefore fails closed.
    Provider-route callers use backend names (``ollama``/``cloud``); normalize
    those to the persisted policy vocabulary.
    """
    route = {"ollama": "local", "cloud": "cloud"}.get(provider_route, provider_route)
    resolved_addresses = ()
    if _section(config, "ava").get("provider_policy", "off") == "local":
        try:
            host = urlsplit(cfg_get(config, "ollama.host")).hostname
        except (TypeError, ValueError):
            host = None
        if host and host.lower() == "localhost":
            resolved_addresses = ("127.0.0.1", "::1")
    return effective_state(config, feature, provider_route=route, ready=True,
                           resolved_addresses=resolved_addresses)


def migrate_ai_preferences(persisted: dict) -> dict:
    """Call BEFORE defaults merge. Pure/idempotent; package G owns boot wiring.

    Snapshot old fallback booleans for an existing AI profile, never manufacture
    consent. v1 Ava scope is bound to its persisted destination only at migration;
    editing and destination changes must get a fresh explicit acceptance.
    """
    result = deepcopy(persisted)
    result["ava"] = deepcopy(_section(persisted, "ava"))
    if "provider_policy" in result["ava"]:
        if result["ava"]["provider_policy"] not in POLICIES:
            raise ValueError("invalid persisted provider policy")
        return result
    # A partial legacy profile also inherited True runtime fallbacks. Empty
    # input and already-v2 setup are fresh/off; key values must precede merge.
    evidence = bool(persisted) and _section(persisted, "onboarding").get("version") != 2
    local = _section(persisted, "ollama")
    cloud = _section(persisted, "cloud_llm")
    session = (_section(persisted, "ava_command_session") if "ava_command_session" in persisted
               else _section(persisted, "ai_command_mode"))
    editing = _section(persisted, "ava_edit")
    a = session.get("enabled", True) is True if evidence else False
    e = editing.get("enabled", True) is True if evidence else False
    cloud_on = cloud.get("enabled") is True and bool(cloud.get("api_key"))
    local_on = local.get("enabled", True) is True if evidence else False
    policy = ("cloud" if cloud_on else "local") if (a or e) and (cloud_on or local_on) else "off"
    result["ava"]["provider_policy"] = policy
    if evidence:
        result["ava_command_session"] = {**deepcopy(session), "enabled": a,
            "keep_warm": session.get("keep_warm", True)}
        result["ava_edit"] = {**deepcopy(editing), "enabled": e}
    record = result["ava"].get("consent")
    if (policy != "off" and isinstance(record, dict) and record.get("version") == 1
            and isinstance(record.get("accepted_at"), str) and record["accepted_at"].strip()
            and "provider" not in record):
        try:
            bound = provider_for(persisted, policy)
        except (ValueError, TypeError):
            pass
        else:
            record.update(features=["ava"], provider=bound.record())
    return result

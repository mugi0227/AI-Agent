"""Read-only Bedrock model discovery. Listing never invokes a model or buys access.

Availability is AWS's account/model signal, not a simulation of effective IAM/SCP
or cross-region authorization. A synthetic inference test is still required.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import time
from urllib.parse import quote, urlencode

from .bedrock import BedrockClient
from .common import AssistantError
from .security import model_api


@dataclass(frozen=True)
class ModelChoice:
    model: str
    name: str
    api: str
    kind: str
    regions: tuple[str, ...]
    availability: str  # available, unknown; known unavailable entries are excluded.
    streaming: bool = True

    @property
    def title(self):
        state = "利用条件確認済み" if self.availability == "available" else "利用条件未確認"
        return f"{self.name} / {self.kind} / {state} / {self.model}"


@dataclass
class CatalogResult:
    choices: list[ModelChoice]
    warnings: list[str]
    excluded: int = 0


def _supported(model):
    try: return model_api(model)
    except AssistantError: return None


def list_models(cfg, *, client=None, clock=time.monotonic):
    """Use the form's auth/region without saving it or requiring a model ID.

    Both API-key and refreshable profile authentication use the same fixed AWS
    control endpoint. The client exposes no arbitrary URL to the desktop form.
    """
    if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d", cfg.region): raise AssistantError("BEDROCK_REGION_INVALID")
    if cfg.auth not in ("sigv4", "api_key", "access_keys"): raise AssistantError("CONFIG_AUTH")
    client = client or BedrockClient(cfg)
    base = f"https://bedrock.{cfg.region}.amazonaws.com"
    deadline = clock() + 60
    warnings, foundation, profiles = [], [], []

    def get(path):
        remaining = deadline - clock()
        if remaining <= 0: raise AssistantError("MODEL_CATALOG_TIMEOUT")
        return client._request(base + path, service="bedrock", method="GET", timeout=min(10, remaining))

    # Either permission may be absent while the other is granted. Keep useful
    # partial results, explicitly labelled in the UI, instead of claiming none.
    failures = []
    try:
        data = get("/foundation-models?byOutputModality=TEXT")
        foundation = data["modelSummaries"]
        if not isinstance(foundation, list) or len(foundation) > 5000: raise ValueError()
    except (AssistantError, KeyError, ValueError, TypeError) as error:
        failures.append(error); warnings.append("基本モデル一覧を取得できませんでした。ListFoundationModelsの権限を確認してください。")
    try:
        token, seen = None, set()
        for _ in range(20):
            query = {"maxResults": 100, "type": "SYSTEM_DEFINED"}
            if token: query["nextToken"] = token
            data = get("/inference-profiles?" + urlencode(query))
            page = data["inferenceProfileSummaries"]
            if not isinstance(page, list) or len(page) > 1000: raise ValueError()
            profiles.extend(page)
            token = data.get("nextToken")
            if not token: break
            if not isinstance(token, str) or len(token) > 2048 or token in seen: raise ValueError()
            seen.add(token)
        else:
            warnings.append("推論プロファイルが多いため一覧を一部省略しました。必要なIDは手入力できます。")
    except (AssistantError, KeyError, ValueError, TypeError) as error:
        failures.append(error); warnings.append("推論プロファイル一覧を最後まで取得できませんでした。ListInferenceProfilesの権限を確認してください。")
    if len(failures) == 2 and not foundation and not profiles:
        code = next((e.code for e in failures if isinstance(e, AssistantError)), "MODEL_CATALOG_INVALID")
        raise AssistantError(code, "モデル一覧を取得できませんでした")
    candidates, excluded = {}, 0
    names, streaming = {}, {}
    try:
        for model in foundation:
            mid = model["modelId"]; api = _supported(mid)
            if not api or model.get("modelLifecycle", {}).get("status") != "ACTIVE" or "TEXT" not in model.get("outputModalities", []): continue
            names[mid] = str(model.get("modelName") or mid)[:200]
            streaming[mid] = bool(model.get("responseStreamingSupported", False))
            # OpenAI Runtime uses profiles; Claude supports both regional models
            # and profiles. Profiles-only entries are not presented as base IDs.
            if api == "converse" and "ON_DEMAND" in model.get("inferenceTypesSupported", []):
                candidates[mid] = (names[mid], api, "リージョン内", (cfg.region,), mid)
        for profile in profiles:
            mid = profile["inferenceProfileId"]; api = _supported(mid)
            if not api or profile.get("status") != "ACTIVE": continue
            model_arns = [x["modelArn"] for x in profile["models"]]
            if not model_arns or any(not isinstance(a, str) or not re.fullmatch(r"arn:aws:bedrock:[a-z0-9-]+::foundation-model/[a-z0-9.:-]+", a) for a in model_arns): continue
            bases = {a.split("foundation-model/", 1)[1] for a in model_arns}
            if len(bases) != 1 or _supported(next(iter(bases))) != api: continue
            source = next(iter(bases))
            regions = tuple(sorted({a.split(":")[3] for a in model_arns}))
            candidates[mid] = (str(profile.get("inferenceProfileName") or names.get(source) or mid)[:200], api, "推論プロファイル", regions, source)
    except (KeyError, TypeError, AttributeError):
        raise AssistantError("MODEL_CATALOG_INVALID") from None
    states, unknown = {}, False
    for source in sorted({v[4] for v in candidates.values()}):
        # Optional availability permission is often withheld by corporate IAM.
        # Stop probing after its first failure; retain unknown choices honestly.
        if unknown or len(states) >= 100:
            states[source] = "unknown"; continue
        try:
            data = get("/foundation-model-availability/" + quote(source, safe=""))
            fields = (data.get("authorizationStatus"), data.get("entitlementAvailability"),
                      data.get("regionAvailability"), data.get("agreementAvailability", {}).get("status"))
            denied = any(value in ("NOT_AUTHORIZED", "NOT_AVAILABLE", "PENDING", "ERROR") for value in (fields[0], fields[1], fields[3]))
            # A base model can be unavailable in the source region while its
            # cross-region profile is usable. Never apply that regional absence
            # to the profile's destination regions.
            profile_state = "blocked" if denied else "available" if fields == ("AUTHORIZED", "AVAILABLE", "AVAILABLE", "AVAILABLE") else "unknown"
            base_state = "blocked" if fields[2] == "NOT_AVAILABLE" else profile_state
            states[source] = (base_state, profile_state)
        except (AssistantError, AttributeError, TypeError):
            unknown = True; states[source] = "unknown"
    if any(value == "unknown" or isinstance(value, tuple) and "unknown" in value for value in states.values()):
        warnings.append("一部の利用条件は未確認です。GetFoundationModelAvailabilityの権限や取得状況を確認し、選択後に接続テストを行ってください。")
    choices = []
    for mid, (name, api, kind, regions, source) in candidates.items():
        value = states[source]
        state = value[0 if kind == "リージョン内" else 1] if isinstance(value, tuple) else value
        if state == "blocked": excluded += 1; continue
        choices.append(ModelChoice(mid, name, api, kind, regions, state, streaming.get(source, True)))
    choices.sort(key=lambda c: (c.availability != "available", c.api, c.name, c.model))
    return CatalogResult(choices, warnings, excluded)

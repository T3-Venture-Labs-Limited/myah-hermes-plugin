# ── Myah: provider catalog overrides (not present upstream) ─────────────────
"""MYAH_OVERRIDES — Myah-specific catalog augmentation.

Single-source-of-truth for V1-visible providers and Myah-specific hints
(validation URLs, custom-provider config for synthetic entries like plain
OpenAI, v1 gating). Adding a provider to the Myah picker: add one entry here.
Removing: delete the entry.

All 23 upstream Hermes providers are exposed to the Myah picker. Each
entry below marks v1_visible=True. Providers whose auth_type is
oauth_external or external_process render a "Coming soon" tile in the
ProviderPicker — the catalog entry exists but no UI flow is wired yet.
"""

MYAH_OVERRIDES: dict = {
    # -----------------------------------------------------------------------
    # Tier-1 API-key providers (validated)
    # -----------------------------------------------------------------------

    # OpenRouter: NOT in PROVIDER_REGISTRY upstream (treated as the
    # default fallback inside resolve_provider at auth.py:892). We
    # declare the full shape here.
    "openrouter": {
        "display_name": "OpenRouter",
        "description": "200+ models via one key",
        "auth_type": "api_key",
        "env_var": "OPENROUTER_API_KEY",
        "validation": {"url": "https://openrouter.ai/api/v1/auth/key",
                       "method": "GET", "auth": "bearer"},
        "inference_base_url": "https://openrouter.ai/api/v1",
        "default_model": "openai/gpt-4o-mini",
        "v1_visible": True,
        "write_type": "env_var",
    },

    # OpenAI API: the bare "openai" slug aliases to "openrouter" in
    # providers.ALIASES:170-172, so we CANNOT use provider="openai" in
    # config.yaml — that would route through OpenRouter using an OpenAI
    # API key (rejected). Instead we write a providers: block and set
    # model.provider to "custom:openai-direct".
    "openai": {
        "display_name": "OpenAI API",
        "description": "Use your OpenAI developer API key (sk-...)",
        "auth_type": "api_key",
        "env_var": "OPENAI_API_KEY",
        "validation": {"url": "https://api.openai.com/v1/models",
                       "method": "GET", "auth": "bearer"},
        "inference_base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
        "v1_visible": True,
        "write_type": "custom_provider",
        "custom_provider": {
            "slug": "openai-direct",
            "base_url": "https://api.openai.com/v1",
            "api_mode": "codex_responses",
            "model_provider_value": "custom:openai-direct",
        },
    },

    # openai-codex upstream auth_type is "oauth_external" because the
    # PROVIDER_REGISTRY entry reflects CLI behavior; the Myah picker routes
    # it through the device-code UI (DeviceCode.svelte), so we override.
    "openai-codex": {"default_model": "gpt-5.4", "v1_visible": True, "write_type": "oauth_codex",
                     "auth_type": "oauth_device_code"},
    "anthropic":    {"default_model": "claude-sonnet-4.6", "v1_visible": True, "write_type": "env_var"},
    "gemini":       {"default_model": "gemini-2.5-flash",  "v1_visible": True, "write_type": "env_var"},
    "xai":          {"default_model": "grok-4",            "v1_visible": True, "write_type": "env_var"},
    "deepseek":     {"default_model": "deepseek-chat",     "v1_visible": True, "write_type": "env_var"},

    # -----------------------------------------------------------------------
    # Tier-2 API-key providers (validated key, inference unverified in Myah)
    # -----------------------------------------------------------------------

    # Z.AI / GLM coding plan
    "zai": {
        "default_model": "glm-5.1",
        "v1_visible": True,
        "write_type": "env_var",
        "validation": {"url": "https://api.z.ai/api/paas/v4/models",
                       "method": "GET", "auth": "bearer"},
    },

    # Kimi coding plan (Moonshot global)
    "kimi-coding": {
        "default_model": "kimi-for-coding",
        "v1_visible": True,
        "write_type": "env_var",
        "validation": {"url": "https://api.moonshot.ai/v1/models",
                       "method": "GET", "auth": "bearer"},
    },

    # Kimi coding plan (Moonshot China)
    "kimi-coding-cn": {
        "default_model": "kimi-k2.5",
        "v1_visible": True,
        "write_type": "env_var",
        "validation": {"url": "https://api.moonshot.cn/v1/models",
                       "method": "GET", "auth": "bearer"},
    },

    # MiniMax (global)
    "minimax": {
        "default_model": "MiniMax-M2.7",
        "v1_visible": True,
        "write_type": "env_var",
        # MiniMax uses Anthropic-compatible endpoint; validation URL would
        # need a POST — leave unset so we accept optimistically and let the
        # first inference call surface the real error.
    },

    # MiniMax (China)
    "minimax-cn": {
        "default_model": "MiniMax-M2.7",
        "v1_visible": True,
        "write_type": "env_var",
    },

    # Alibaba Cloud DashScope (Qwen family)
    "alibaba": {
        "default_model": "qwen3.5-plus",
        "v1_visible": True,
        "write_type": "env_var",
        "validation": {"url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/models",
                       "method": "GET", "auth": "bearer"},
    },

    # Arcee AI
    "arcee": {
        "default_model": "trinity-large-thinking",
        "v1_visible": True,
        "write_type": "env_var",
        "validation": {"url": "https://api.arcee.ai/api/v1/models",
                       "method": "GET", "auth": "bearer"},
    },

    # Hugging Face router
    "huggingface": {
        "default_model": "Qwen/Qwen3.5-397B-A17B",
        "v1_visible": True,
        "write_type": "env_var",
        "validation": {"url": "https://router.huggingface.co/v1/models",
                       "method": "GET", "auth": "bearer"},
    },

    # Xiaomi MiMo
    "xiaomi": {
        "default_model": "mimo-v2-pro",
        "v1_visible": True,
        "write_type": "env_var",
        "validation": {"url": "https://api.xiaomimimo.com/v1/models",
                       "method": "GET", "auth": "bearer"},
    },

    # Vercel AI Gateway
    "ai-gateway": {
        "default_model": "anthropic/claude-opus-4.6",
        "v1_visible": True,
        "write_type": "env_var",
        "validation": {"url": "https://ai-gateway.vercel.sh/v1/models",
                       "method": "GET", "auth": "bearer"},
    },

    # OpenCode Zen
    "opencode-zen": {
        "default_model": "gpt-5.4-pro",
        "v1_visible": True,
        "write_type": "env_var",
        "validation": {"url": "https://opencode.ai/zen/v1/models",
                       "method": "GET", "auth": "bearer"},
    },

    # OpenCode Go
    "opencode-go": {
        "default_model": "glm-5",
        "v1_visible": True,
        "write_type": "env_var",
        "validation": {"url": "https://opencode.ai/zen/go/v1/models",
                       "method": "GET", "auth": "bearer"},
    },

    # Kilo Code
    "kilocode": {
        "default_model": "anthropic/claude-opus-4.6",
        "v1_visible": True,
        "write_type": "env_var",
        # Kilo Code's gateway doesn't expose a /models endpoint by default;
        # optimistic accept, inference will surface real errors.
    },

    # GitHub Copilot — uses a GitHub PAT with a special header dance.
    # Listed here because it's catalog-visible; ApiKey flow will paste
    # the GH token, but inference may require additional setup (Copilot
    # subscription, device-auth upgrade). Leave validation unset.
    "copilot": {
        "default_model": "gpt-5.4",
        "v1_visible": True,
        "write_type": "env_var",
    },

    # -----------------------------------------------------------------------
    # OAuth device-code (same flow as openai-codex)
    # -----------------------------------------------------------------------

    # Nous Portal (subscription) — oauth_device_code
    "nous": {
        "default_model": "Hermes-4-405B",
        "v1_visible": True,
        "write_type": "oauth_device_code",
    },

    # -----------------------------------------------------------------------
    # Coming-soon tiles (render "This provider requires a flow not yet
    # supported in the UI. Coming soon." in ProviderPicker).
    # -----------------------------------------------------------------------

    # Qwen OAuth portal — oauth_external, requires manual CLI login today
    "qwen-oauth": {
        "default_model": "qwen-max",
        "v1_visible": True,
        "write_type": "oauth_external",
    },

    # GitHub Copilot ACP — external_process (spawns copilot agent subprocess)
    "copilot-acp": {
        "default_model": "gpt-5.4",
        "v1_visible": True,
        "write_type": "external_process",
    },
}
# ─────────────────────────────────────────────────────────────────────────────

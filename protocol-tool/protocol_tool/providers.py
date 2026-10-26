"""The AI models the protocol tool can design with, and their defaults."""

from __future__ import annotations

from dataclasses import dataclass, replace

from .config import AssistantSettings


@dataclass(frozen=True)
class Provider:
    name: str  # claude | gemini | ollama | openai
    label: str  # shown on the page
    model: str
    url: str  # OpenAI-compatible base URL (unused for Claude)
    key_env: str  # environment/.env variable for the key; "" when no key is needed
    key_help: str  # where to get a key or the software
    free: bool
    # Tried in order when the model is overloaded, out of quota or retired (OpenAI-compatible only).
    fallback_models: tuple[str, ...] = ()


PRESETS = {
    "claude": Provider(
        "claude", "Claude", "claude-opus-5-5", "", "ANTHROPIC_API_KEY",
        "Get a key at console.anthropic.com (paid)", free=False,
    ),
    "gemini": Provider(
        # An alias Google keeps pointed at its current Flash model, so retirements don't break us.
        "gemini", "Gemini", "gemini-flash-latest", "https://generativelanguage.googleapis.com/v1beta/openai",
        "GEMINI_API_KEY", "Get a free key at aistudio.google.com/apikey", free=True,
        # Free-tier capacity differs per model; older and Lite models are often less busy.
        fallback_models=("gemini-3.5-flash", "gemini-flash-lite-latest"),
    ),
    "ollama": Provider(
        "ollama", "Ollama", "qwen3:30b", "http://localhost:11434/v1", "",
        "Install Ollama from ollama.com", free=True,
    ),
    "openai": Provider(
        "openai", "AI model", "", "", "OPENAI_API_KEY",
        "Set the key for your OpenAI-compatible server", free=False,
    ),
}


def resolve(settings: AssistantSettings) -> Provider:
    preset = PRESETS.get(settings.provider)
    if preset is None:
        raise ValueError(f"[assistant] provider must be one of {', '.join(PRESETS)}, not {settings.provider!r}")
    provider = replace(
        preset,
        model=settings.model or preset.model,
        url=(settings.url or preset.url).rstrip("/"),
        key_env=settings.api_key_env or preset.key_env,
        fallback_models=tuple(settings.fallback_models) if settings.fallback_models is not None else preset.fallback_models,
    )
    if provider.name == "openai" and not (provider.url and provider.model):
        raise ValueError('[assistant] provider "openai" needs url and model')
    return provider

from app.providers.openai_compatible import OpenAICompatibleProvider


class GroqProvider(OpenAICompatibleProvider):
    """Groq, through its OpenAI-compatible API."""

    name = "groq"
    base_url = "https://api.groq.com/openai/v1"

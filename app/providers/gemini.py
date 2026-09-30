from app.providers.openai_compatible import OpenAICompatibleProvider


class GeminiProvider(OpenAICompatibleProvider):
    """Google Gemini, through its OpenAI-compatible API."""

    name = "gemini"
    base_url = "https://generativelanguage.googleapis.com/v1beta/openai"

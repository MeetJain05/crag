from langchain_core.runnables import RunnableLambda
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_groq import ChatGroq
from langchain_huggingface import ChatHuggingFace, HuggingFaceEndpoint

from app.config import settings

primary_llm = ChatGoogleGenerativeAI(
    model=settings.chat_model,
    temperature=0,
    google_api_key=settings.gemini_api_key,
    # Gemini's free tier is 5 requests/minute; without capping retries, the client's
    # own exponential backoff burns tens of seconds per call on a 429 before
    # `.with_fallbacks()` below ever gets a chance to hand off to the next model.
    max_retries=1,
)
fallback_llm = ChatGroq(model=settings.fallback_chat_model, temperature=0, groq_api_key=settings.groq_api_key)


def _build_hf_chat(repo_id: str) -> ChatHuggingFace:
    endpoint = HuggingFaceEndpoint(
        repo_id=repo_id,
        provider="auto",
        huggingfacehub_api_token=settings.hf_token,
        max_new_tokens=1024,
    )
    return ChatHuggingFace(llm=endpoint)


# Groq is primary because HuggingFace Inference Providers can stop accepting
# requests when an account's prepaid credits are exhausted. Gemini and
# HuggingFace remain available as fallback tiers.
hf_chats = [_build_hf_chat(repo_id) for repo_id in settings.hf_chat_models]

llm = fallback_llm.with_fallbacks([primary_llm, *hf_chats])


def _hf_structured(chat: ChatHuggingFace, schema):
    """HuggingFace's `with_structured_output` only supports a dict/JSON-schema for
    tool-calling - a Pydantic class raises `NotImplementedError` there - so the
    schema is converted to a dict up front and the result coerced back into the
    Pydantic `schema` afterward, to match what Gemini/Groq's chains return.

    Open-weight models occasionally mis-type a tool-call argument (e.g. emitting
    the string "false" instead of the boolean `false`), which the provider rejects
    server-side as a 400 before any response reaches this chain - retrying once
    resolves it most of the time without needing a different model.
    """
    schema_dict = schema.model_json_schema()
    schema_dict["title"] = schema.__name__
    raw_chain = chat.with_structured_output(schema_dict, method="function_calling")
    return (raw_chain | RunnableLambda(lambda d: schema(**d))).with_retry(stop_after_attempt=2)


def structured(schema):
    """Build a Runnable that returns `schema`, trying Groq first, then Gemini,
    then HuggingFace.

    Groq's configured model is more reliable with JSON mode than tool calling,
    so the schema is enforced from the JSON response without registering a tool.
    """
    fallback_structured = fallback_llm.with_structured_output(schema, method="json_mode").with_retry(
        stop_after_attempt=3,
        wait_exponential_jitter=True,
    )
    gemini_structured = primary_llm.with_structured_output(schema)
    hf_structured_chats = [_hf_structured(chat, schema) for chat in hf_chats]
    return fallback_structured.with_fallbacks([gemini_structured, *hf_structured_chats])

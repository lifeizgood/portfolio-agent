"""Loads a local Hugging Face model as a LangChain chat model.

Everything else in the repo only calls `model.invoke(messages).content`, so tests
can swap in a fake model and you can swap in a different backend later.
"""

from functools import lru_cache

from . import config


def pick_device() -> str:
    import torch

    if torch.backends.mps.is_available():  # Apple Silicon GPU
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


@lru_cache(maxsize=2)
def load_chat_model(model_id: str | None = None, max_new_tokens: int | None = None):
    """Download (first run only) and load the model. Cached per process."""
    from langchain_huggingface import ChatHuggingFace, HuggingFacePipeline
    from transformers import AutoTokenizer, pipeline

    model_id = model_id or config.MODEL_ID
    tokenizer = AutoTokenizer.from_pretrained(model_id)
    pipe = pipeline(
        "text-generation",
        model=model_id,
        tokenizer=tokenizer,
        device=pick_device(),
        dtype="auto",              # bf16 on MPS for Qwen models
        max_new_tokens=max_new_tokens or config.MAX_NEW_TOKENS,
        do_sample=False,           # deterministic output: easier to test and debug
        return_full_text=False,    # only the new text, not the prompt
    )
    llm = HuggingFacePipeline(pipeline=pipe, model_id=model_id)
    return ChatHuggingFace(llm=llm, tokenizer=tokenizer, model_id=model_id)

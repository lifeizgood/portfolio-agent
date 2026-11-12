"""Loads a local Hugging Face model as a LangChain chat model.

Everything else in the repo only calls `model.invoke(messages).content`, so tests
can swap in a fake model and you can swap in a different backend later.
"""

import threading
from functools import lru_cache

from . import config


def pick_device() -> str:
    import torch

    if torch.backends.mps.is_available():  # Apple Silicon GPU
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


_LOAD_LOCK = threading.Lock()   # parallel refreshes must not load the model twice
_GEN_LOCK = threading.Lock()    # one generation at a time on the GPU


class _OneAtATime:
    """The model, shared by parallel refreshes: calls take turns (browsing still
    runs in parallel; only reading pages with the model waits)."""

    def __init__(self, model):
        self._model = model

    def invoke(self, *args, **kwargs):
        with _GEN_LOCK:
            return self._model.invoke(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._model, name)


def load_chat_model(model_id: str | None = None, max_new_tokens: int | None = None):
    """Download (first run only) and load the model, once per process."""
    with _LOAD_LOCK:
        return _load(model_id, max_new_tokens)


@lru_cache(maxsize=2)
def _load(model_id: str | None, max_new_tokens: int | None):
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
    return _OneAtATime(ChatHuggingFace(llm=llm, tokenizer=tokenizer, model_id=model_id))

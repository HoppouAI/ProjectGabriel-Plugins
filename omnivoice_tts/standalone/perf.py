"""Opt-in perf tweaks for the OmniVoice provider.

Two knobs, both off by default because each has a real tradeoff:

- ``apply_flash_attention_2``: swaps the LLMs attention impl to flash-attn 2.
  Faster on long sequences but historically crashes on some varlen paths in
  the omnivoice generate() flow, so its kept behind a flag.

- ``install_cuda_graph_cache``: wraps ``model.llm.forward`` with a shape-keyed
  CUDA Graph cache. Skips per-kernel launch overhead in the diffusion loop.
  Measured ~1.6x on the isolated LLM but only a few percent end-to-end for
  +~2GB VRAM, so its opt-in.

Both helpers are safe to call: they no-op on cpu, no-op if already installed,
and fall back to the original forward on any error.

(Mirrors the perf module from the standalone OmniVoice server, slightly
trimmed for in-process use.)
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def apply_flash_attention_2(model) -> bool:
    """Try to switch the inner LLM to flash_attention_2. Returns True on success."""
    try:
        import torch
        if not torch.cuda.is_available():
            return False
        import flash_attn  # noqa: F401
    except Exception:
        logger.info("omnivoice_tts: flash-attn not available, skipping FA2 swap.")
        return False

    llm = getattr(model, "llm", None)
    if llm is None or not hasattr(llm, "config"):
        return False
    try:
        llm.config._attn_implementation = "flash_attention_2"
        for layer in getattr(llm, "layers", []):
            sa = getattr(layer, "self_attn", None)
            if sa is not None and hasattr(sa, "config"):
                sa.config._attn_implementation = "flash_attention_2"
        logger.info("omnivoice_tts: FA2 enabled on LLM.")
        return True
    except Exception as e:
        logger.warning("omnivoice_tts: FA2 swap failed: %s", e)
        return False


def install_cuda_graph_cache(model, max_entries: int = 8) -> bool:
    """Wrap model.llm.forward with a shape-keyed CUDA Graph cache.

    The diffusion decoding loop calls llm.forward many times per sentence at
    the exact same shapes, so capturing one graph per (bs, seq_len, dtype)
    and replaying it skips the per-kernel launch overhead.
    """
    import torch

    if not torch.cuda.is_available():
        return False

    llm = getattr(model, "llm", None)
    if llm is None or getattr(llm, "_ov_graph_installed", False):
        return False

    orig_forward = llm.forward
    cache: dict = {}
    # share one mem pool across captures so static buffers dont duplicate per shape
    shared_pool = torch.cuda.graph_pool_handle()

    def forward(*args, **kwargs):
        emb = kwargs.get("inputs_embeds")
        pid = kwargs.get("position_ids")
        amask = kwargs.get("attention_mask")
        # only graph the simple inputs_embeds path; bail to original for anything weird
        if (emb is None or not isinstance(emb, torch.Tensor)
                or kwargs.get("use_cache", False)
                or kwargs.get("past_key_values") is not None
                or kwargs.get("input_ids") is not None):
            return orig_forward(*args, **kwargs)
        bs, sl, _hd = emb.shape
        amask_shape = tuple(amask.shape) if isinstance(amask, torch.Tensor) else None
        pid_shape = tuple(pid.shape) if isinstance(pid, torch.Tensor) else None
        key = (bs, sl, str(emb.dtype), amask_shape, pid_shape)

        entry = cache.get(key)
        if entry is None:
            if len(cache) >= max_entries:
                # cache full, just run eagerly so we dont OOM
                return orig_forward(*args, **kwargs)
            try:
                static_emb = torch.empty_like(emb)
                static_pid = torch.empty_like(pid) if isinstance(pid, torch.Tensor) else None
                static_amask = torch.empty_like(amask) if isinstance(amask, torch.Tensor) else None
                static_emb.copy_(emb)
                if static_pid is not None:
                    static_pid.copy_(pid)
                if static_amask is not None:
                    static_amask.copy_(amask)
                # warmup on a side stream before capture (recommended in pytorch docs)
                s = torch.cuda.Stream()
                s.wait_stream(torch.cuda.current_stream())
                with torch.cuda.stream(s):
                    for _ in range(2):
                        _ = orig_forward(
                            inputs_embeds=static_emb,
                            position_ids=static_pid,
                            attention_mask=static_amask,
                            use_cache=False,
                        )
                torch.cuda.current_stream().wait_stream(s)
                torch.cuda.synchronize()
                g = torch.cuda.CUDAGraph()
                with torch.cuda.graph(g, pool=shared_pool):
                    static_out = orig_forward(
                        inputs_embeds=static_emb,
                        position_ids=static_pid,
                        attention_mask=static_amask,
                        use_cache=False,
                    )
                entry = {
                    "graph": g, "emb": static_emb, "pid": static_pid,
                    "amask": static_amask, "out": static_out,
                }
                cache[key] = entry
                return static_out
            except Exception as e:
                logger.debug("omnivoice_tts: graph capture failed for key=%s: %s", key, e)
                return orig_forward(*args, **kwargs)

        # replay path
        entry["emb"].copy_(emb)
        if entry["pid"] is not None and isinstance(pid, torch.Tensor):
            entry["pid"].copy_(pid)
        if entry["amask"] is not None and isinstance(amask, torch.Tensor):
            entry["amask"].copy_(amask)
        entry["graph"].replay()
        return entry["out"]

    llm.forward = forward  # type: ignore[assignment]
    llm._ov_graph_installed = True
    llm._ov_graph_cache = cache
    logger.info("omnivoice_tts: CUDA graph cache installed on LLM (max %d shapes).", max_entries)
    return True


def apply_perf_tweaks(
    model,
    use_flash_attn: bool = False,
    use_cuda_graphs: bool = False,
    max_graph_cache: int = 8,
) -> dict:
    """Apply selected perf tweaks. Returns a dict of what was actually enabled."""
    import torch
    # gradients are never needed in inference, this is essentially free
    try:
        torch.set_grad_enabled(False)
    except Exception:
        pass
    enabled = {"fa2": False, "cuda_graphs": False}
    if use_flash_attn:
        enabled["fa2"] = apply_flash_attention_2(model)
    if use_cuda_graphs:
        enabled["cuda_graphs"] = install_cuda_graph_cache(model, max_entries=max_graph_cache)
    return enabled

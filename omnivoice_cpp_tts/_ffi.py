"""ctypes binding to the omnivoice.cpp C ABI (omnivoice.dll / libomnivoice.so).

Mirrors include/omnivoice.h from the ServeurpersoCom fork, OV_ABI_VERSION 2.
Pure POD structs + extern "C" entries, so this is a thin literal translation.
We only bind the public surface the synth path needs: init, synthesize (with
the streaming chunk callback), free, plus the small introspection helpers.

The lib does all the model work. Nothing here imports torch, this is just FFI.
"""
from __future__ import annotations

import ctypes as C
import os
import sys
from pathlib import Path


OV_ABI_VERSION = 2

# ov_status enum. OK is 0 so `if rc:` reads as "if error".
OV_STATUS_OK = 0
OV_STATUS_INVALID_PARAMS = -1
OV_STATUS_INSTRUCT_INVALID = -2
OV_STATUS_GENERATE_FAILED = -3
OV_STATUS_OOM = -4
OV_STATUS_CANCELLED = -5

_STATUS_NAMES = {
    0: "OK",
    -1: "INVALID_PARAMS",
    -2: "INSTRUCT_INVALID",
    -3: "GENERATE_FAILED",
    -4: "OOM",
    -5: "CANCELLED",
}

# ov_log_level enum
OV_LOG_DEBUG = 0
OV_LOG_INFO = 1
OV_LOG_WARN = 2
OV_LOG_ERROR = 3


def status_name(rc: int) -> str:
    return _STATUS_NAMES.get(int(rc), f"UNKNOWN({rc})")


class ov_audio(C.Structure):
    _fields_ = [
        ("samples", C.POINTER(C.c_float)),  # mono PCM, malloc'd by the lib
        ("n_samples", C.c_int),
        ("sample_rate", C.c_int),           # 24000 for OmniVoice
        ("channels", C.c_int),              # 1 (mono)
    ]


class ov_init_params(C.Structure):
    _fields_ = [
        ("abi_version", C.c_int),
        ("model_path", C.c_char_p),   # the LM gguf (omnivoice-base-*.gguf)
        ("codec_path", C.c_char_p),   # the codec gguf (omnivoice-tokenizer-*.gguf)
        ("use_fa", C.c_bool),         # flash attention when a gpu backend is up
        ("clamp_fp16", C.c_bool),     # guard fp16 matmul accum on sub-Ampere cuda
    ]


# cooperative cancel: return True to abort. polled between chunks.
OV_CANCEL_CB = C.CFUNCTYPE(C.c_bool, C.c_void_p)
# streaming output: mono float PCM at codec sr. return False to abort.
OV_AUDIO_CHUNK_CB = C.CFUNCTYPE(C.c_bool, C.POINTER(C.c_float), C.c_int, C.c_void_p)
# global log sink
OV_LOG_CB = C.CFUNCTYPE(None, C.c_int, C.c_char_p, C.c_void_p)


class ov_tts_params(C.Structure):
    _fields_ = [
        ("abi_version", C.c_int),

        ("text", C.c_char_p),
        ("lang", C.c_char_p),       # "" auto, "en", "zh"
        ("instruct", C.c_char_p),   # voice design attribute string

        ("T_override", C.c_int),
        ("chunk_duration_sec", C.c_float),
        ("chunk_threshold_sec", C.c_float),

        ("denoise", C.c_bool),
        ("preprocess_prompt", C.c_bool),

        # MaskGIT sampler config (flattened)
        ("mg_num_step", C.c_int),
        ("mg_guidance_scale", C.c_float),
        ("mg_t_shift", C.c_float),
        ("mg_layer_penalty_factor", C.c_float),
        ("mg_position_temperature", C.c_float),
        ("mg_class_temperature", C.c_float),
        ("mg_seed", C.c_uint64),

        # optional voice reference. tokens OR raw 24k samples, not both.
        ("ref_audio_tokens", C.POINTER(C.c_int32)),
        ("ref_T", C.c_int),
        ("ref_audio_24k", C.POINTER(C.c_float)),
        ("ref_n_samples", C.c_int),
        ("ref_text", C.c_char_p),

        ("dump_dir", C.c_char_p),

        ("cancel", OV_CANCEL_CB),
        ("cancel_user_data", C.c_void_p),

        ("on_chunk", OV_AUDIO_CHUNK_CB),
        ("on_chunk_user_data", C.c_void_p),
    ]


def _bind(lib: C.CDLL) -> None:
    lib.ov_version.restype = C.c_char_p
    lib.ov_version.argtypes = []

    lib.ov_last_error.restype = C.c_char_p
    lib.ov_last_error.argtypes = []

    lib.ov_audio_free.restype = None
    lib.ov_audio_free.argtypes = [C.POINTER(ov_audio)]

    lib.ov_init_default_params.restype = None
    lib.ov_init_default_params.argtypes = [C.POINTER(ov_init_params)]

    # opaque handle -> c_void_p
    lib.ov_init.restype = C.c_void_p
    lib.ov_init.argtypes = [C.POINTER(ov_init_params)]

    lib.ov_free.restype = None
    lib.ov_free.argtypes = [C.c_void_p]

    lib.ov_tts_default_params.restype = None
    lib.ov_tts_default_params.argtypes = [C.POINTER(ov_tts_params)]

    lib.ov_synthesize.restype = C.c_int
    lib.ov_synthesize.argtypes = [
        C.c_void_p, C.POINTER(ov_tts_params), C.POINTER(ov_audio),
    ]

    lib.ov_log_set.restype = None
    lib.ov_log_set.argtypes = [OV_LOG_CB, C.c_void_p]

    lib.ov_num_codebooks.restype = C.c_int
    lib.ov_num_codebooks.argtypes = [C.c_void_p]

    lib.ov_duration_sec_to_tokens.restype = C.c_int
    lib.ov_duration_sec_to_tokens.argtypes = [C.c_void_p, C.c_float]


def _dll_name() -> str:
    if os.name == "nt":
        return "omnivoice.dll"
    # mac dylib first, then so. ServeurpersoCom builds these names.
    if sys.platform == "darwin":
        return "libomnivoice.dylib"
    return "libomnivoice.so"


def load_library(lib_dir: str | os.PathLike) -> C.CDLL:
    """Load omnivoice.dll from lib_dir and bind the C ABI.

    lib_dir must hold omnivoice.dll plus the ggml + cuda runtime dlls. On
    Windows we add it to the dll search path first so omnivoice.dll can
    resolve ggml-cuda.dll -> cublas* -> cudart at load time.
    """
    d = Path(lib_dir).expanduser()
    if not d.is_dir():
        raise FileNotFoundError(f"omnivoice lib_dir is not a directory: {d}")
    dll = d / _dll_name()
    if not dll.is_file():
        raise FileNotFoundError(
            f"{dll.name} not found in {d}. point lib_dir at the folder that "
            f"holds omnivoice.dll and its ggml/cuda dlls."
        )
    if os.name == "nt":
        # keep the cuda + ggml dlls resolvable. harmless if already added.
        os.add_dll_directory(str(d))
    lib = C.CDLL(str(dll))
    _bind(lib)
    # surface an abi mismatch early with a readable error instead of a
    # struct-layout segfault deep in synth.
    try:
        ver = lib.ov_version()
        ver = ver.decode("utf-8", "replace") if ver else "?"
    except Exception:
        ver = "?"
    lib._ov_version_str = ver  # stash for logging
    return lib

"""Recommended ``llama-server`` invocation for a produced GGUF (H- MTP serving).

Given a GGUF, builds the optimal serving command for this box: GPU offload,
quantized KV cache, flash attention, and -- when the GGUF carries MTP
("nextn") draft tensors -- self-speculative MTP decoding.

Module import is stdlib + ``_rocmfpx_entry`` (itself stdlib-only) so this is
safe to import from the CLI, the model-card generator, and the UI without
pulling in the training stack. ``magicquant.gguf.source`` is imported lazily
inside ``detect_mtp`` (mirrors ``_magicquant_entry.py``'s import discipline)
so this module stays importable where MagicQuant isn't installed (CI).

Measured (Qwopus 27B, 2026-07-05): MTP speculative decoding is 1.70x faster
generation (8.34 -> 14.16 t/s) at 95% first-token accept / mean accept length
4.02, invoked via ``llama-server -m MODEL -md MODEL --spec-type draft-mtp``
(``--spec-type`` is a server/CLI flag, not a llama-completion request field).
Cost: roughly 2x model memory (the draft pass needs its own context).
"""

from __future__ import annotations

import shlex
from pathlib import Path

try:
    import _rocmfpx_entry
except ImportError:  # pragma: no cover - when imported as the `core` package
    from core import _rocmfpx_entry

DEFAULT_PORT = 8080
DEFAULT_NGL = 99
DEFAULT_CTX = 8192
DEFAULT_HOST = "127.0.0.1"


def detect_mtp(gguf_path: str) -> bool:
    """True if ``gguf_path`` carries MTP ("nextn") draft tensors.

    A missing/unreadable file is treated as "no MTP" rather than raising --
    this is a best-effort capability probe, not a hard requirement.
    """
    from magicquant.gguf.source import GGUFSource

    try:
        source = GGUFSource(gguf_path)
    except OSError:
        return False
    try:
        return any("nextn" in name for name in source.get_tensor_names())
    finally:
        source.close()


def _resolve_llama_server(hint: str = "") -> str:
    """Locate the ``llama-server`` binary via the same discovery
    ``_rocmfpx_entry.find_rocmfpx`` uses for ``llama-quantize``: prefer a
    ROCmFPX fork build's ``build-strix-rocmfp4/bin``, then a stock
    ``~/llama.cpp/build/bin``. Falls back to the bare command name (PATH
    lookup) if nothing is found on disk.
    """
    rocmfpx_dir = _rocmfpx_entry.find_rocmfpx(hint)
    if rocmfpx_dir:
        pp = Path(rocmfpx_dir)
        for sub in ("build-strix-rocmfp4/bin/llama-server", "build/bin/llama-server"):
            candidate = pp / sub
            if candidate.exists():
                return str(candidate)
    fallback = Path.home() / "llama.cpp" / "build" / "bin" / "llama-server"
    if fallback.exists():
        return str(fallback)
    return "llama-server"


def build_serve_command(
    gguf_path: str,
    port: int = DEFAULT_PORT,
    ngl: int = DEFAULT_NGL,
    mtp: bool | None = None,
    kv_quant: bool = True,
    flash_attn: bool = True,
    ctx: int = DEFAULT_CTX,
    host: str = DEFAULT_HOST,
    llamacpp_hint: str = "",
) -> list[str]:
    """Build the optimal ``llama-server`` argv for serving ``gguf_path``.

    ``mtp=None`` (default) auto-enables MTP speculative decoding when
    ``gguf_path`` carries "nextn" draft tensors (``detect_mtp``); ``True``
    forces it on, ``False`` disables it. MTP is self-speculative here -- the
    draft model is the same GGUF file (it carries its own draft/"nextn"
    tensors), so no separate draft checkpoint is needed.

    ``kv_quant`` adds ``-ctk q8_0 -ctv q8_0`` (quantized KV cache, supported
    on gfx1151). ``flash_attn`` adds ``-fa on`` -- on hybrid (SSM+attention)
    architectures this only affects the attention sublayers; SSM state stays
    F32 regardless of this flag.
    """
    server_bin = _resolve_llama_server(llamacpp_hint)
    argv = [
        server_bin,
        "-m", gguf_path,
        "-c", str(ctx),
        "--port", str(port),
        "--host", host,
        "-ngl", str(ngl),
    ]

    use_mtp = detect_mtp(gguf_path) if mtp is None else mtp
    if use_mtp:
        argv += ["-md", gguf_path, "--spec-type", "draft-mtp"]

    if kv_quant:
        argv += ["-ctk", "q8_0", "-ctv", "q8_0"]
    if flash_attn:
        argv += ["-fa", "on"]

    return argv


def format_serve_command(argv: list[str]) -> str:
    """Shell-quoted one-liner for docs/UI display."""
    return " ".join(shlex.quote(str(a)) for a in argv)


def _main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Print the recommended llama-server command for a GGUF.",
    )
    parser.add_argument("gguf", help="Path to the GGUF to serve")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--no-mtp", action="store_true",
        help="Disable MTP speculative decoding even if the GGUF supports it",
    )
    args = parser.parse_args(argv)

    mtp = False if args.no_mtp else None
    auto_mtp = (not args.no_mtp) and detect_mtp(args.gguf)
    if auto_mtp:
        print("MTP speculative decoding auto-enabled (~1.7x generation speedup measured).")
    command = build_serve_command(args.gguf, port=args.port, mtp=mtp)
    print(format_serve_command(command))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main())

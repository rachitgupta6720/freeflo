"""Registry of downloadable Turbo-mode LLM models. The ONE place model facts live."""

MODELS = {
    'lite': {
        'label': 'Lite — Llama 3.2 1B',
        'filename': 'lite.gguf',
        'url': 'https://huggingface.co/bartowski/Llama-3.2-1B-Instruct-GGUF/resolve/main/Llama-3.2-1B-Instruct-Q4_K_M.gguf',
        'size_bytes': 807_694_336,     # approx; used for the disk-space check + progress %
        'sha256': '6f85a640a97cf2bf5b8e764087b1e83da0fdb51d7c9fab7d0fece9385611df83',
        'ram_note': '~1.5 GB RAM',
        'ctx': 4096,
        'blurb': 'Fastest, lowest memory. Good for quick cleanup on 8 GB Macs.',
    },
}

DEFAULT_TIER = 'lite'


def get(tier):
    return MODELS.get(tier) or MODELS[DEFAULT_TIER]


def is_installed(tier):
    """A model counts as installed only if the file exists and is the full size."""
    import os
    import config
    m = MODELS.get(tier)
    if not m:
        return False
    path = config.get_turbo_model_path(tier)
    try:
        # allow a little slack; sizes above are approximate
        return os.path.exists(path) and os.path.getsize(path) > m['size_bytes'] * 0.98
    except OSError:
        return False

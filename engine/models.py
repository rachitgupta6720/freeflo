"""Registry of downloadable Turbo-mode LLM models. The ONE place model facts live."""

MODELS = {
    'lite': {
        'label': 'Lite — Llama 3.2 1B',
        'filename': 'lite.gguf',
        'url': 'https://huggingface.co/bartowski/Llama-3.2-1B-Instruct-GGUF/resolve/main/Llama-3.2-1B-Instruct-Q4_K_M.gguf',
        'size_bytes': 807_694_336,     # approx; used for the disk-space check + progress %
        'sha256': None,                # TODO: pin before release (see PRD §4)
        'ram_note': '~1.5 GB RAM',
        'ctx': 4096,
        'blurb': 'Fastest, lowest memory. Good for quick cleanup on 8 GB Macs.',
    },
    'balanced': {
        'label': 'Balanced — Llama 3.2 3B',
        'filename': 'balanced.gguf',
        'url': 'https://huggingface.co/bartowski/Llama-3.2-3B-Instruct-GGUF/resolve/main/Llama-3.2-3B-Instruct-Q4_K_M.gguf',
        'size_bytes': 2_019_377_696,
        'sha256': None,                # TODO: pin before release
        'ram_note': '~3 GB RAM',
        'ctx': 4096,
        'blurb': 'Recommended. Great structure and cleanup for everyday use.',
    },
    'max': {
        'label': 'Max — Qwen 2.5 7B',
        'filename': 'max.gguf',
        'url': 'https://huggingface.co/bartowski/Qwen2.5-7B-Instruct-GGUF/resolve/main/Qwen2.5-7B-Instruct-Q4_K_M.gguf',
        'size_bytes': 4_683_073_344,
        'sha256': None,                # TODO: pin before release
        'ram_note': '~6 GB RAM',
        'ctx': 8192,
        'blurb': 'Best quality summaries and formatting. Needs 16 GB+ RAM.',
    },
}

DEFAULT_TIER = 'balanced'


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

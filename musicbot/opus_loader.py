from nextcord import opus


def load_opus_lib() -> None:
    if opus.is_loaded():
        return

    try:
        opus._load_default()
        return
    except OSError:
        pass

    raise RuntimeError('Could not load an opus lib.')

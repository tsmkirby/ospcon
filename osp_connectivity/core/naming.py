"""IDF-scoped storage keys and local labels for panels and switches."""


def local_asset_id(identifier, idf_id):
    text = str(identifier or '').strip()
    prefix = str(idf_id or '').strip() + '-'
    if idf_id and text.casefold().startswith(prefix.casefold()):
        return text[len(prefix):]
    return text


def scoped_asset_id(identifier, idf_id):
    text = local_asset_id(identifier, idf_id)
    return f'{idf_id}-{text}' if idf_id and text else text

import re
import unicodedata

def normalize(key):
    """Canonical specimen number used for blocking and matching.

    NFKC folding + trim + casefold means that re-registering the same paper
    sample with different width, padding or letter case still hits the
    tombstone.  The original spelling is never needed to purge a value.
    """
    return unicodedata.normalize('NFKC',str(key)).strip().casefold()

def keys_in_snapshot(snapshot):
    return {normalize(row.get('source_key')) for row in (snapshot or [])
            if isinstance(row,dict) and row.get('source_key') is not None}

def contains(text,key):
    """True when the normalized number occurs as a standalone token.

    Boundaries keep ``p-1`` from matching ``p-10`` so redaction of audit notes
    does not destroy records about a different paper sample.
    """
    needle=normalize(key)
    if not needle: return False
    hay=unicodedata.normalize('NFKC',str(text)).casefold()
    pattern=r'(?<![0-9a-z])'+re.escape(needle)+r'(?![0-9a-z])'
    return re.search(pattern,hay) is not None

"""High level processing blocks.

This subpackage gathers higher level blocks, or "lobes" for HuggingFace Transformers.
"""

# Transformers is required for this package.
try:
    import transformers  # noqa
except ImportError:
    MSG = "Please install transformers from HuggingFace.\n"
    MSG += "E.G. run: pip install transformers \n"
    MSG += "For more information, visit: https://huggingface.co/docs/transformers/installation"
    raise ImportError(MSG)

from . import *  # noqa

r"""
Tools for working with ARPA format N-gram models

Expects the ARPA format to have:
- a \data\ header
- counts of ngrams in the order that they are later listed
- line breaks between \data\ and \n-grams: sections
- \end\
E.G.
    ```
    \data\
    ngram 1=2
    ngram 2=1

    \1-grams:
    -1.0000 Hello -0.23
    -0.6990 world -0.2553

    \2-grams:
    -0.2553 Hello world

    \end\
    ```
"""
import collections
import logging

logger = logging.getLogger(__name__)

ArpaProb = collections.namedtuple("ArpaProb", ["prob", "backoff"])
"""
An ARPA LM probability, with prob and backoff
"""


def read_arpa(fin):
    find_data_section(fin)
    num_ngrams = read_num_ngrams(fin)
    ngrams_by_order = {}
    backoffs_by_order = {}
    for order in num_ngrams:
        logger.debug(f"Reading {order}-grams")
        probs, backoffs = read_ngrams_section(fin, order)
        ngrams_by_order[order] = probs
        backoffs_by_order[order] = backoffs
        # num_grams = sum(len(probs[context]) for context in probs)
        # assert num_grams == num_ngrams[order]
    read_end(fin)
    return num_ngrams, ngrams_by_order, backoffs_by_order


def find_data_section(fstream):
    for line in fstream:
        if line[:6] == "\\data\\":
            break
    else:  # For-else is obscure but fits here perfectly
        raise ValueError("Not a properly formatted ARPA file")


def read_num_ngrams(fstream):
    num_ngrams = {}
    for line in fstream:
        if line[:5] == "ngram":
            lhs, rhs = line.strip().split("=")
            order = int(lhs.split()[1])
            num_grams = int(rhs)
            num_ngrams[order] = num_grams
        else:
            break
    if not num_ngrams:
        raise ValueError("Empty ARPA file")
    return num_ngrams


def read_ngrams_section(fstream, order):
    section_header = fstream.readline()
    if not section_header.startswith(f"\\{order}-grams:"):
        raise ValueError("Not a properly formatted ARPA file")
    probs = {}
    backoffs = {}
    for line in fstream:
        if not line.strip():
            break
        parts = line.strip().split()
        prob = float(parts[0])
        if len(parts[1:]) == order + 1:
            context = tuple(parts[1:-2])
            token = parts[-2]
            backoff = float(parts[-1])
        elif len(parts[1:]) == order:
            context = tuple(parts[1:-1])
            token = parts[-1]
            backoff = 0.0
        else:
            raise ValueError("Not a properly formatted ARPA file")
        backoff_context = context + (token,)
        probs.setdefault(context, {})[token] = prob
        backoffs[backoff_context] = backoff
    return probs, backoffs


def read_end(fstream):
    for line in fstream:
        if line[:5] == "\\end\\":
            break
    else:
        raise ValueError("Not a properly formatted ARPA file")

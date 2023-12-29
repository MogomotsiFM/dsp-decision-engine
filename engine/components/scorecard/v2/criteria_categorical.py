import typing
from functools import partial
from itertools import chain
import re as builtin_re

import pandas as pd
import numpy as np
from pydantic import Field
from .common import DiscreteScoreCriteriaBuilderMixin

try:
    # Try use re2 where possible as it provides a speedup
    import re2 as re
except ImportError:
    re = builtin_re


def regex_match(patt: str)->str:
    return patt

def regex_starts_with(patt: str):
    if not patt.startswith("^"):
        patt = "^"+patt
    return patt

def regex_ends_with(patt: str):
    if not patt.endswith("$") or patt.endswith("\\$"):
        patt = patt + "$"
    return patt

def regex_full_match(patt: str):
    return regex_ends_with(regex_starts_with(patt))

# On testing it appears doing the plaintext versions of these (patt == val or val.startswith(patt))
# Is sometimes only marginally faster and becomes a lot slower when mix and matching with other methods
# treating them all as regex patterns but escaping plaintext makes it run a lot faster
def plaintext_match(patt: str)->str:
    return builtin_re.escape(patt)

def plaintext_starts_with(patt: str):
    return regex_starts_with(plaintext_match(patt))

def plaintext_ends_with(patt: str):
    return regex_ends_with(plaintext_match(patt))

def plaintext_full_match(patt: str):
    return regex_full_match(plaintext_match(patt))

pattern_constructors = {
    "regex": regex_full_match,
    "regex_end": regex_ends_with,
    "regex_start": regex_starts_with,
    "regex_partial": regex_match,
    "matches": plaintext_full_match,
    "matches_end": plaintext_ends_with,
    "matches_start": plaintext_starts_with,
    "matches_partial": plaintext_match,
}
PatternConstructorKey = typing.Literal[
    "regex",
    "regex_end",
    "regex_start",
    "regex_partial",
    "matches",
    "matches_end",
    "matches_start",
    "matches_partial"
]


def match_fn(equals_array: typing.List[builtin_re.Pattern], default_value, x):
    return next(chain(
        (i for i,v in enumerate(equals_array) if v.search(x) is not None),
        (default_value,)
    ))

class ScoreCriteriaCategorical(DiscreteScoreCriteriaBuilderMixin[typing.Optional[str]]):
    type: typing.Literal["categorical"] = Field(default="categorical")
    default_behavior: PatternConstructorKey = Field(default="matches")

    def _get_check_array(self, bin_prefix: str, score_prefix: str, desc_prefix: str):

        other_record = {
            f"{bin_prefix}{self.variable}": -1,
            f"{score_prefix}{self.variable}": -1,
            f"{desc_prefix}{self.variable}": None,
        }
        if self.other_score is not None:
            other_record = {
                f"{bin_prefix}{self.variable}": self.other_score.group_id,
                f"{score_prefix}{self.variable}": self.other_score.score,
                f"{desc_prefix}{self.variable}": self.other_score.description,
            }
        
        result_array = [other_record]
        default_idx = 0
        nan_idx = 0
        idx_lookup = []

        equals_array = []
        # Put it in reverse so that last items have more priority over earlier items
        for ds in reversed(self.discrete_scores):
            res_idx = len(result_array)
            result_array.append({
                f"{bin_prefix}{self.variable}": ds.group_id,
                f"{score_prefix}{self.variable}": ds.score,
                f"{desc_prefix}{self.variable}": ds.description,
            })
            line_patterns = []
            for v in ds.values:
                if v is None:
                    # Keep first res containing nan
                    if nan_idx == 0:
                        nan_idx = res_idx
                    continue
                split_v = v.split(":")
                patt_creator = pattern_constructors[self.default_behavior]
                patt = v
                if len(split_v) > 1:
                    patt_creator_temp = pattern_constructors.get(split_v[0])
                    if patt_creator_temp is not None:
                        patt = v[len(split_v[0])+1:]
                        patt_creator = patt_creator_temp
                # Wrap all patterns in non capturing groups
                line_patterns.append(f"(?:{patt_creator(patt)})")
            if len(line_patterns) == 0: continue
            patt = "|".join(line_patterns)
            if len(patt) > 5000:
                # RE2 not as good with very long patterns
                equals_array.append(builtin_re.compile(patt))
            else:
                equals_array.append(re.compile(patt))
            idx_lookup.append(res_idx)
        
        # # Always keep nan result as second last in array
        # if nan_idx != None:
        #     result_array.append(other_record)
        # else:
        #     result_array.append(result_array[nan_idx])
        # # Always keep default as last in array
        # result_array.append(other_record)
        idx_lookup.extend([nan_idx, default_idx])

        res_df = pd.DataFrame(result_array)

        return (
            res_df,
            partial(match_fn, equals_array, len(idx_lookup)-1),
            np.array(idx_lookup)
        )
            

    def _execute(self, 
        value: pd.Series, 
        res_df: pd.DataFrame,
        get_match_idx: typing.Callable[[str,],int],
        idx_lookup: np.ndarray
    ):
        # Not a fan of accessing private variables 
        # but this is the fastest way to get things done
        res_idx = value.str._data.array._str_map(
            get_match_idx, 
            na_value=len(idx_lookup)-2, 
            dtype=np.uint32
        ).astype(np.uint32)
        return res_df.iloc[idx_lookup[res_idx]]
        
"""Causal feature computation for XAU/USD research (docs/06).

Every function here is causal: output[t] depends only on input[0..t]. Warm-up values are NaN.
Nothing here produces a trading signal; features become signals only through a pre-registered hypothesis.
"""

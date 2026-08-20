"""
ledger/what_if/__init__.py — What-If Counterfactual Projection Module
"""
from ledger.what_if.projector import run_what_if, WhatIfResult

__all__ = ["run_what_if", "WhatIfResult"]

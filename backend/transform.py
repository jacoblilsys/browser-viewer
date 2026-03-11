"""
ADC → physical unit transformations.
Mirrors the logic in data_viewer_v2/data_viewer/viewer2/transform_functions.py.
"""

import numpy as np


def adc_to_volt(data: np.ndarray, adc_v_per_step: float) -> np.ndarray:
    return data * adc_v_per_step


def volt_to_force(
    data: np.ndarray,
    zero_mv_v: float,
    supply_v: float,
    rated_mv_v: float,
    full_scale_n: float,
) -> np.ndarray:
    """
    Wheatstone bridge load-cell formula.
    zero_mv_v  — zero-signal output in mV/V
    rated_mv_v — rated-load output in mV/V
    """
    num = (data * 1000.0) - (zero_mv_v * supply_v)
    denom = rated_mv_v * supply_v
    if np.isclose(denom, 0.0):
        return np.full_like(data, np.nan)
    return num * full_scale_n / denom

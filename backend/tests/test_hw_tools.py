"""Tests for the bench-tool maths — no hardware needed.

The hardware tools decide PASS/FAIL from these functions, so a silently broken
estimator would turn the whole suite into a rubber stamp. Each test feeds a
signal whose answer is known analytically and checks the estimator recovers it,
including the cases that are easy to get wrong: a tone sitting between bins
(scalloping), and a spectrum that must NOT be flagged.

Run:  python backend/tests/test_hw_tools.py
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'protobuf'))

import numpy as np

from tools.hw_common import ENBW, firmware_spectrum, tone_amplitude, welch

_passed = 0
_failed = 0


def check(name, cond, detail=''):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f'  ok  {name}')
    else:
        _failed += 1
        print(f'  FAIL {name}' + (f' — {detail}' if detail else ''))


def approx(a, b, tol):
    return abs(a - b) <= tol * abs(b)


# ── the firmware's own contract ─────────────────────────────────────────────

def test_firmware_spectrum_contract():
    """0x1033: a tone of amplitude A reads A; a DC offset A reads A in bin 0."""
    for n in (256, 512, 1024, 2048):
        fs, A = 26667.0, 1.75
        k = 12                                    # exactly on a bin centre
        t = np.arange(n) / fs
        x = A * np.sin(2 * np.pi * (k * fs / n) * t)
        spec = firmware_spectrum(x, n)
        check(f'tone of amplitude A reads A at N={n}',
              approx(spec[k], A, 0.02), f'{spec[k]:.4f} vs {A}')

        off = 3.25
        x = np.full(n, off)
        spec = firmware_spectrum(x, n)
        check(f'DC offset A reads A in bin 0 at N={n}',
              approx(spec[0], off, 0.02), f'{spec[0]:.4f} vs {off}')


# ── the tone estimator ──────────────────────────────────────────────────────

def test_tone_amplitude_on_centre():
    n, fs, A = 1024, 26667.0, 0.5
    k = 40
    t = np.arange(n) / fs
    spec = firmware_spectrum(A * np.sin(2 * np.pi * (k * fs / n) * t), n)
    est = tone_amplitude(spec, int(np.argmax(spec[1:])) + 1)
    check('tone estimator, tone on a bin centre', approx(est, A, 0.05),
          f'{est:.4f} vs {A}')


def test_tone_amplitude_between_bins():
    """The case that matters: half a bin off, where the peak bin alone reads
    low by up to 15 % and would look like a scaling error."""
    n, fs, A = 1024, 26667.0, 0.5
    f_tone = (40 + 0.5) * fs / n
    t = np.arange(n) / fs
    spec = firmware_spectrum(A * np.sin(2 * np.pi * f_tone * t), n)
    kp = int(np.argmax(spec[1:])) + 1
    peak_only = spec[kp]
    est = tone_amplitude(spec, kp)
    check('tone estimator survives worst-case scalloping',
          approx(est, A, 0.06), f'{est:.4f} vs {A}')
    check('peak bin alone would have been wrong (proving the estimator earns '
          'its keep)', peak_only < 0.9 * A, f'peak bin {peak_only:.4f} vs {A}')


def test_tone_amplitude_is_not_a_rubber_stamp():
    """A spectrum with the wrong scale must NOT pass as correct."""
    n, fs, A = 1024, 26667.0, 0.5
    k = 40
    t = np.arange(n) / fs
    spec = firmware_spectrum(A * np.sin(2 * np.pi * (k * fs / n) * t), n)
    bad = spec * 8.0                              # the pre-0x1033 Q15 error
    est = tone_amplitude(bad, int(np.argmax(bad[1:])) + 1)
    check('mis-scaled spectrum is detected', not approx(est, A, 0.2),
          f'{est:.4f} vs {A} — should differ')


# ── Welch ───────────────────────────────────────────────────────────────────

def test_welch_white_noise_density():
    """White noise of known density must come back at that density."""
    rng = np.random.default_rng(1234)
    fs, n = 4096.0, 1 << 17
    density = 3e-3                                 # units/sqrt(Hz)
    sigma = density * math.sqrt(fs / 2)            # total RMS over the band
    x = rng.normal(0.0, sigma, n)
    f, P, nseg = welch(x, fs, nperseg=2048)
    band = (f > 0.1 * fs / 2) & (f < 0.4 * fs / 2)
    got = math.sqrt(float(np.median(P[band])))
    check('welch recovers a white-noise density', approx(got, density, 0.10),
          f'{got:.5f} vs {density}')
    check('welch reports its segment count', nseg > 50, f'nseg={nseg}')


def test_welch_parseval_on_a_tone():
    """Integrating the PSD over a tone gives its mean square, A^2/2."""
    fs, n, A = 4096.0, 1 << 15, 2.0
    t = np.arange(n) / fs
    x = A * np.sin(2 * np.pi * 200.0 * t)
    f, P, _ = welch(x, fs, nperseg=4096)
    power = float(np.sum(P) * (f[1] - f[0]))
    check('welch integrates a tone to A^2/2', approx(power, A * A / 2, 0.05),
          f'{power:.4f} vs {A*A/2}')


def test_welch_ignores_dc():
    """Gravity is a huge DC term; it must not leak into the noise estimate."""
    rng = np.random.default_rng(7)
    fs, n = 4096.0, 1 << 16
    x = rng.normal(0.0, 1e-3, n) + 9.81            # noise sitting on gravity
    f, P, _ = welch(x, fs, nperseg=2048)
    band = (f > 0.1 * fs / 2) & (f < 0.4 * fs / 2)
    got = math.sqrt(float(np.median(P[band])))
    expect = 1e-3 / math.sqrt(fs / 2)
    check('welch is immune to a large DC offset', approx(got, expect, 0.15),
          f'{got:.6f} vs {expect:.6f}')


# ── the decimation prediction the noise tool asserts ────────────────────────

def test_boxcar_decimation_preserves_density():
    """The firmware's boxcar: averaging R samples divides variance by R and
    bandwidth by R, so the DENSITY is unchanged while total RMS falls as
    sqrt(R). This is the claim noise_floor_check.py tests on hardware."""
    rng = np.random.default_rng(99)
    fs, n, R = 8192.0, 1 << 18, 8
    x = rng.normal(0.0, 1.0, n)
    f0, P0, _ = welch(x, fs, nperseg=2048)
    d0 = math.sqrt(float(np.median(P0[(f0 > 0.1 * fs / 2) & (f0 < 0.4 * fs / 2)])))

    y = x[:(n // R) * R].reshape(-1, R).mean(axis=1)     # boxcar + decimate
    fsd = fs / R
    f1, P1, _ = welch(y, fsd, nperseg=2048)
    d1 = math.sqrt(float(np.median(P1[(f1 > 0.1 * fsd / 2) & (f1 < 0.4 * fsd / 2)])))

    check('boxcar decimation leaves the noise density unchanged',
          approx(d1, d0, 0.10), f'{d1:.5f} vs {d0:.5f}')
    check('boxcar decimation cuts total RMS by sqrt(R)',
          approx(float(np.std(y)), float(np.std(x)) / math.sqrt(R), 0.05),
          f'{np.std(y):.5f} vs {np.std(x)/math.sqrt(R):.5f}')


if __name__ == '__main__':
    for fn in [test_firmware_spectrum_contract, test_tone_amplitude_on_centre,
               test_tone_amplitude_between_bins,
               test_tone_amplitude_is_not_a_rubber_stamp,
               test_welch_white_noise_density, test_welch_parseval_on_a_tone,
               test_welch_ignores_dc, test_boxcar_decimation_preserves_density]:
        fn()
    print()
    if _failed:
        print(f'{_failed} of {_passed + _failed} tests FAILED')
        sys.exit(1)
    print(f'All {_passed} tests passed.')

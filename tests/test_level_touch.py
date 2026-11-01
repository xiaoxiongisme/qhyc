# -*- coding: utf-8 -*-
"""level_touch 引擎纯数学单测（不依赖数据库）。"""
import math

import numpy as np
import pytest

from app.backtest.level_touch.engine import (
    add_box, add_range, auto_bin_width, fit_drift_vol, gain_for_threshold,
    make_grid, recurrence_no_return, singleton_levels, support_zones,
    topology_summary, z_score)


def test_make_grid():
    edges, centers, e0 = make_grid(0.0, 10.0, 1.0)
    assert centers[0] == pytest.approx(0.5)
    assert len(centers) == 11
    assert e0 == 0.0


def test_make_grid_max_bins():
    edges, centers, e0 = make_grid(0.0, 1000.0, 1.0, max_bins=100)
    assert len(centers) <= 100


def test_add_range():
    edges, centers, e0 = make_grid(0.0, 10.0, 1.0)
    touches = np.zeros(len(centers))
    add_range(touches, e0, 1.0, 2.3, 4.7)
    # bins covering [2.3,4.7]: centers 2.5,3.5,4.5 -> indices 2,3,4
    assert touches[2] == 1 and touches[3] == 1 and touches[4] == 1
    assert touches[1] == 0 and touches[5] == 0


def test_add_box():
    edges, centers, e0 = make_grid(0.0, 10.0, 1.0)
    exp = np.zeros(len(centers))
    add_box(exp, e0, 1.0, 5.0, 1.5)  # [3.5,6.5] -> centers 3.5,4.5,5.5,6.5
    assert exp[3] == 1 and exp[4] == 1 and exp[5] == 1 and exp[6] == 1


def test_z_score():
    touches = np.array([0, 10, 0])
    exp = np.array([1.0, 10.0, 1.0])
    z = z_score(touches, exp)
    assert z[1] == pytest.approx(0.0, abs=1e-9)
    assert z[0] == pytest.approx(-1.0, abs=1e-9)


def test_fit_drift_vol():
    rng = np.random.default_rng(0)
    r = rng.normal(0.001, 0.02, 5000)
    mu, sigma = fit_drift_vol(r)
    assert mu == pytest.approx(0.001, abs=0.002)
    assert sigma == pytest.approx(0.02, abs=0.002)


def test_recurrence_no_return_monotonic_and_zero_drift():
    # 无漂移 => 必然回归 => 概率 0
    assert recurrence_no_return(0.0, 0.02, 0.2) == 0.0
    assert recurrence_no_return(-0.001, 0.02, 0.2) == 0.0
    # 正漂移 => 概率随涨幅单调递增且 <1
    p1 = recurrence_no_return(0.0005, 0.02, 0.1)
    p2 = recurrence_no_return(0.0005, 0.02, 0.5)
    assert 0 < p1 < p2 < 1


def test_recurrence_inverse():
    mu, sigma, gain = 0.0008, 0.018, 0.3
    p = recurrence_no_return(mu, sigma, gain)
    g2 = gain_for_threshold(mu, sigma, p)
    assert g2 == pytest.approx(gain, rel=1e-6)


def test_singleton_levels():
    touches = np.array([1, 0, 3, 1, 2])
    centers = np.array([10.0, 20.0, 30.0, 40.0, 50.0])
    assert singleton_levels(centers, touches, 1) == [10.0, 40.0]


def test_support_zones():
    centers = np.array([1.0, 2.0, 3.0, 4.0])
    touches = np.array([5, 5, 50, 5])
    exp = np.array([5.0, 5.0, 5.0, 5.0])
    z = z_score(touches, exp)
    zones = support_zones(centers, z, touches, z_thr=2.0, min_touches=8)
    assert zones and zones[0][0] == 3.0 and zones[0][1] > 2.0


def test_topology_summary_flat():
    x = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    topo = topology_summary(x)
    assert topo["n_local_max"] == 0
    assert topo["n_monotonic_runs"] == 1


def test_topology_summary_peaks():
    x = np.array([1.0, 3.0, 2.0, 4.0, 1.0])
    topo = topology_summary(x)
    assert topo["n_local_max"] == 2
    assert topo["n_local_min"] == 1

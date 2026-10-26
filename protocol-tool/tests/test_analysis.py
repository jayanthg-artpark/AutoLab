"""Result analysis: curve fits and interpolation against known answers."""

import math

import numpy as np
import pytest

from protocol_tool.analysis import analyse_run, analysis_csv, find_measurement, fit_curve, run_analysis
from protocol_tool.models import Analysis


def reading(label, values):
    return {"label": label, "unit": "OD", "values": values}


def bradford():
    """Standards 0-2000 ug/mL in duplicate (A/B), linear OD = 0.05 + 0.0005*conc; samples in rows C-D."""
    conc = [0, 125, 250, 500, 750, 1000, 1500, 2000]
    values = {}
    for i, c in enumerate(conc, start=1):
        values[f"A{i}"] = 0.05 + 0.0005 * c + 0.002
        values[f"B{i}"] = 0.05 + 0.0005 * c - 0.002
    for i, c in enumerate([300, 900, 2600], start=1):  # the last is above the curve
        values[f"C{i}"] = values[f"D{i}"] = 0.05 + 0.0005 * c
    spec = Analysis(
        title="Bradford", kind="standard_curve", measurement="Absorbance 595 nm", fit="linear", x_unit="ug/mL",
        blank_wells=["A1", "B1"],
        standards=[{"name": f"S{i}", "wells": [f"A{i}", f"B{i}"], "x": c} for i, c in enumerate(conc, start=1) if c],
        samples=[{"name": f"U{i}", "wells": [f"C{i}", f"D{i}"], "dilution": 2 if i == 2 else 1} for i in (1, 2, 3)],
    )
    return spec, [reading("Absorbance 595 nm", values)]


def test_bradford_linear_standard_curve():
    spec, measurements = bradford()
    out = run_analysis(spec, measurements)
    fit = out["series"][0]["fit"]
    assert fit["model"] == "linear" and fit["r2"] > 0.999
    assert fit["params"]["b"] == pytest.approx(0.0005, rel=1e-3)
    assert fit["params"]["a"] == pytest.approx(0.0, abs=1e-3)  # blank subtracted
    u1, u2, u3 = out["samples"]
    assert u1["value"] == pytest.approx(300, rel=0.01) and u1["flag"] == ""
    assert u2["value"] == pytest.approx(1800, rel=0.01)  # 900 x dilution 2
    assert u3["flag"] == "above the highest standard"
    assert any("Blank" in w for w in out["warnings"]) and any("Outside the standard range" in w for w in out["warnings"])
    assert len(out["series"][0]["curve"]) > 50 and out["x_label"] == "Concentration (ug/mL)"


def test_4pl_elisa_with_reference_wavelength():
    a, b, c, d = 0.05, 1.3, 120.0, 2.4
    conc = [15.6, 31.25, 62.5, 125, 250, 500, 1000]
    f = lambda x: d + (a - d) / (1 + (x / c) ** b)  # noqa: E731
    v450, v570 = {}, {}
    for i, x in enumerate(conc, start=1):
        v450[f"A{i}"] = f(x) + 0.04
        v570[f"A{i}"] = 0.04
    v450["B1"], v570["B1"] = f(200) + 0.04, 0.04
    spec = Analysis(title="IL-6", kind="standard_curve", measurement="Absorbance 450 nm", reference_measurement="Absorbance 570 nm",
                    fit="4pl", standards=[{"name": f"S{i}", "wells": [f"A{i}"], "x": x} for i, x in enumerate(conc, start=1)],
                    samples=[{"name": "Patient 1", "wells": ["B1"]}])
    measurements = [reading("Step 4 Read plate: Absorbance 450 nm", v450), reading("Step 4 Read plate: Absorbance 570 nm", v570)]
    out = run_analysis(spec, measurements)
    fit = out["series"][0]["fit"]
    assert fit["r2"] > 0.9999 and fit["ec50"] == pytest.approx(120, rel=0.01)
    assert out["samples"][0]["value"] == pytest.approx(200, rel=0.01)
    assert out["x_log"] is True and out["measurement"] == "Step 4 Read plate: Absorbance 450 nm - Absorbance 570 nm"


def test_qpcr_efficiency_from_a_log_linear_standard_curve():
    quantities = [1e2, 1e3, 1e4, 1e5, 1e6]
    ct = {f"A{i}": 38 - 3.32 * math.log10(q) for i, q in enumerate(quantities, start=1)}
    spec = Analysis(title="qPCR", kind="standard_curve", measurement="Ct", fit="log_linear",
                    standards=[{"name": f"Std {q:g}", "wells": [f"A{i}"], "x": q} for i, q in enumerate(quantities, start=1)],
                    samples=[{"name": "S", "wells": ["A1"]}])
    fit = run_analysis(spec, [{"label": "Ct", "values": ct}])["series"][0]["fit"]
    assert fit["params"]["b"] == pytest.approx(-3.32, rel=1e-3)
    assert fit["efficiency_percent"] == pytest.approx(100.1, abs=0.5)


def test_dose_response_per_series_and_group_comparison():
    doses = [0.01, 0.1, 1, 10, 100, 1000]
    values = {}
    for i, x in enumerate(doses, start=1):
        values[f"A{i}"] = 1.0 / (1 + (x / 5) ** 1)
        values[f"B{i}"] = 1.0 / (1 + (x / 50) ** 1)
    spec = Analysis(title="IC50", kind="dose_response", measurement="Luminescence", fit="4pl",
                    standards=[{"name": f"{s} {x}", "wells": [f"{row}{i}"], "x": x, "series": s}
                               for s, row in (("Drug A", "A"), ("Drug B", "B")) for i, x in enumerate(doses, start=1)])
    out = run_analysis(spec, [{"label": "Luminescence", "values": values}])
    assert [s["name"] for s in out["series"]] == ["Drug A", "Drug B"]
    assert out["series"][0]["fit"]["ec50"] == pytest.approx(5, rel=0.02)
    assert out["series"][1]["fit"]["ec50"] == pytest.approx(50, rel=0.02)

    compare = Analysis(title="Treatment", kind="group_comparison", measurement="Luminescence", control="Vehicle",
                       samples=[{"name": "Vehicle", "wells": ["C1", "C2", "C3"]}, {"name": "Drug", "wells": ["D1", "D2", "D3"]}])
    groups = run_analysis(compare, [{"label": "Luminescence", "values": {"C1": 100, "C2": 110, "C3": 90, "D1": 200, "D2": 210, "D3": 190}}])["groups"]
    assert groups[1]["fold_change"] == pytest.approx(2.0) and groups[1]["p_value"] < 0.01


def test_plans_are_checked():
    with pytest.raises(ValueError, match="4pl fit needs at least 4 standards"):
        Analysis(title="x", kind="standard_curve", measurement="Absorbance 450 nm", fit="4pl",
                 standards=[{"name": "S1", "wells": ["A1"], "x": 1}], samples=[{"name": "U", "wells": ["B1"]}])
    with pytest.raises(ValueError, match="x must be above 0"):
        Analysis(title="x", kind="dose_response", measurement="Ct", fit="4pl",
                 standards=[{"name": f"S{i}", "wells": [f"A{i}"], "x": i - 1} for i in range(1, 6)])
    with pytest.raises(ValueError, match="control 'Nope'"):
        Analysis(title="x", kind="group_comparison", measurement="Ct", control="Nope",
                 samples=[{"name": "A", "wells": ["A1"]}, {"name": "B", "wells": ["B1"]}])


def test_missing_readings_are_reported_not_raised():
    spec, measurements = bradford()
    out = analyse_run([spec.model_dump()], {"measurements": [reading("Absorbance 600 nm", {"A1": 1.0})]})
    assert out[0]["error"].startswith("No 'Absorbance 595 nm' reading in this run")
    assert analyse_run([spec.model_dump()], None)[0]["error"] == "No readings yet"
    assert "row_type" in analysis_csv("run-1", analyse_run([spec.model_dump()], {"measurements": measurements})).splitlines()[0]


def test_find_measurement_by_step():
    ms = [reading("Step 2 Read plate: Absorbance 450 nm", {}), reading("Step 5 Read plate: Absorbance 450 nm", {"A1": 1})]
    assert find_measurement(ms, "Absorbance 450 nm", 5) is ms[1]
    assert find_measurement(ms, "absorbance 450 nm", None) is ms[0]


def test_quadratic_inverse_picks_the_root_inside_the_range():
    x = np.array([0.0, 250, 500, 1000, 1500, 2000])
    y = 0.02 + 0.0009 * x - 0.0002e-3 * x ** 2
    fit = fit_curve("quadratic", x, y)
    assert fit.inverse(0.02 + 0.0009 * 800 - 0.0002e-3 * 800 ** 2) == pytest.approx(800, rel=1e-3)

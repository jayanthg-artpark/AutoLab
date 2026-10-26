"""Turns a run's readings into results: fitted curves, interpolated samples, comparisons.

Deterministic (no AI). The analysis plan (models.Analysis) comes with the run: which
wells are standards at which concentration, which are blanks and samples, and the
curve to fit. Everything here is the standard plate-assay arithmetic biologists do
by hand:

  standard_curve    protein assays (Bradford, BCA: linear or quadratic), ELISA
                    (4PL/5PL), qPCR (Ct against log10 quantity); unknowns are read
                    off the curve and multiplied back by their dilution
  dose_response     4PL per series (compound): EC50/IC50, Hill slope, top, bottom
  dilution_series   signal against dilution or concentration, optional fit
  group_comparison  mean, SD, CV and fold change against a control, Welch's t-test
"""

from __future__ import annotations

import math
import warnings
from typing import Any

import numpy as np
from scipy import optimize, stats

from .models import Analysis, WellGroup

CURVE_POINTS = 120


class AnalysisError(ValueError):
    """The readings can't be analysed as planned (missing measurement, no usable wells)."""


# --- Picking the readings -------------------------------------------------------------------


def find_measurement(measurements: list[dict[str, Any]], label: str, step: int | None) -> dict[str, Any]:
    """The measurement labelled `label` ("Absorbance 595 nm"). With several reads the master
    node labels them "Step 3 Read plate: Absorbance 595 nm"; `step` picks the right one."""
    wanted = label.strip().lower()

    def base(m: dict[str, Any]) -> str:
        return m["label"].split(": ", 1)[-1].strip().lower() if m["label"].startswith("Step ") else m["label"].strip().lower()

    matches = [m for m in measurements if base(m) == wanted or m["label"].strip().lower() == wanted]
    if step is not None:
        on_step = [m for m in matches if m["label"].startswith(f"Step {step} ")]
        matches = on_step or [m for m in matches if not m["label"].startswith("Step ")]
    if not matches:
        have = ", ".join(m["label"] for m in measurements) or "none"
        raise AnalysisError(f"No '{label}' reading in this run (readings: {have})")
    return matches[0]


def _signals(analysis: Analysis, measurements: list[dict[str, Any]]) -> tuple[dict[str, float], str, list[str]]:
    """Well -> corrected signal (reference and blank subtracted), the label used, warnings."""
    notes: list[str] = []
    main = find_measurement(measurements, analysis.measurement, analysis.step)
    values = {w: v for w, v in main["values"].items() if v is not None and math.isfinite(v)}
    missing = sorted(w for w in analysis.wells() if w not in values)
    if missing:
        notes.append(f"No reading for {', '.join(missing[:12])}{' ...' if len(missing) > 12 else ''}; left out")
    if analysis.reference_measurement:
        ref = find_measurement(measurements, analysis.reference_measurement, analysis.step)["values"]
        values = {w: v - ref[w] for w, v in values.items() if ref.get(w) is not None}
    blanks = [values[w] for w in analysis.blank_wells if w in values]
    if analysis.blank_wells:
        if blanks:
            blank = float(np.mean(blanks))
            values = {w: v - blank for w, v in values.items()}
            notes.append(f"Blank {blank:.4g} (mean of {len(blanks)} well{'s' * (len(blanks) != 1)}) subtracted")
        else:
            notes.append("No blank wells were read; nothing subtracted")
    label = main["label"] + (f" - {analysis.reference_measurement}" if analysis.reference_measurement else "")
    return values, label, notes


def _group_values(group: WellGroup, values: dict[str, float]) -> list[float]:
    return [values[w] for w in group.wells if w in values]


def _summary(vals: list[float]) -> dict[str, Any]:
    n = len(vals)
    mean = float(np.mean(vals)) if n else None
    sd = float(np.std(vals, ddof=1)) if n > 1 else None
    cv = (abs(sd / mean) * 100 if sd is not None and mean else None)
    return {"mean": mean, "sd": sd, "cv": cv, "n": n}


# --- Models ----------------------------------------------------------------------------------


def _4pl(x, a, b, c, d):
    """a: response at zero, d: response at infinity, c: inflection (EC50), b: Hill slope."""
    return d + (a - d) / (1.0 + (x / c) ** b)


def _5pl(x, a, b, c, d, g):
    return d + (a - d) / (1.0 + (x / c) ** b) ** g


class Fit:
    def __init__(self, model: str, params: dict[str, float], predict, inverse, equation: str):
        self.model, self.params, self.predict, self.inverse, self.equation = model, params, predict, inverse, equation
        self.r2: float | None = None

    def view(self) -> dict[str, Any]:
        out = {"model": self.model, "params": {k: _round(v) for k, v in self.params.items()},
               "equation": self.equation, "r2": _round(self.r2)}
        if self.model in ("4pl", "5pl"):
            out["ec50"] = _round(self.params["c"])
        if self.model == "log_linear":
            # qPCR: Ct = a + b*log10(quantity); a perfect doubling each cycle gives slope -3.32.
            out["efficiency_percent"] = _round((10 ** (-1 / self.params["b"]) - 1) * 100) if self.params["b"] else None
        return out


def fit_curve(model: str, x: np.ndarray, y: np.ndarray) -> Fit:
    if model == "linear":
        b, a = np.polyfit(x, y, 1)
        fit = Fit(model, {"a": a, "b": b}, lambda v: a + b * v, lambda s: (s - a) / b if b else math.nan,
                  f"y = {_fmt(b)}·x {_signed(a)}".strip())
    elif model == "linear_zero":
        b = float(np.dot(x, y) / np.dot(x, x))
        fit = Fit(model, {"b": b}, lambda v: b * v, lambda s: s / b if b else math.nan, f"y = {_fmt(b)}·x")
    elif model == "quadratic":
        c2, b, a = np.polyfit(x, y, 2)
        lo, hi = float(min(x)), float(max(x))

        def inverse(s: float) -> float:
            if abs(c2) < 1e-15:
                return (s - a) / b if b else math.nan
            roots = np.roots([c2, b, a - s])
            real = sorted(r.real for r in roots if abs(r.imag) < 1e-9)
            inside = [r for r in real if lo - (hi - lo) <= r <= hi + (hi - lo)]
            return min(inside, key=lambda r: abs(r - (lo + hi) / 2)) if inside else math.nan

        fit = Fit(model, {"a": a, "b": b, "c": c2}, lambda v: a + b * v + c2 * v ** 2, inverse,
                  " ".join(f"y = {_fmt(c2)}·x² {_signed(b)}·x {_signed(a)}".split()).replace(" ·x", ""))
    elif model == "log_linear":
        b, a = np.polyfit(np.log10(x), y, 1)
        fit = Fit(model, {"a": a, "b": b}, lambda v: a + b * np.log10(v),
                  lambda s: 10 ** ((s - a) / b) if b else math.nan, f"y = {_fmt(b)}·log10(x) {_signed(a)}".strip())
    elif model in ("4pl", "5pl"):
        order = np.argsort(x)
        lo_y, hi_y = float(y[order[0]]), float(y[order[-1]])
        c0 = float(np.exp(np.mean(np.log(x))))
        p0 = [lo_y, 1.0, c0, hi_y] + ([1.0] if model == "5pl" else [])
        lower = [-np.inf, 0.05, float(min(x)) / 1000, -np.inf] + ([0.05] if model == "5pl" else [])
        upper = [np.inf, 20.0, float(max(x)) * 1000, np.inf] + ([20.0] if model == "5pl" else [])
        func = _4pl if model == "4pl" else _5pl
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                popt, _ = optimize.curve_fit(func, x, y, p0=p0, bounds=(lower, upper), maxfev=20000)
            except (RuntimeError, ValueError) as exc:
                raise AnalysisError(f"The {model.upper()} fit did not converge: {exc}") from exc
        names = ["a", "b", "c", "d"] + (["g"] if model == "5pl" else [])
        params = dict(zip(names, map(float, popt)))
        a, b, c, d = params["a"], params["b"], params["c"], params["d"]
        g = params.get("g", 1.0)

        def inverse(s: float) -> float:
            with np.errstate(all="ignore"):
                inner = ((a - d) / (s - d)) ** (1 / g) - 1
                return float(c * inner ** (1 / b)) if inner > 0 else math.nan

        equation = (f"y = {_fmt(d)} + ({_fmt(a)} - {_fmt(d)}) / (1 + (x/{_fmt(c)})^{_fmt(b)})"
                    + (f"^{_fmt(g)}" if model == "5pl" else ""))
        fit = Fit(model, params, lambda v: func(v, *popt), inverse, equation)
    else:
        raise AnalysisError(f"Unknown fit '{model}'")
    predicted = np.array([fit.predict(v) for v in x], dtype=float)
    ss_res = float(np.sum((y - predicted) ** 2))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    fit.r2 = 1 - ss_res / ss_tot if ss_tot > 0 else None
    return fit


# --- Analyses --------------------------------------------------------------------------------


def run_analysis(analysis: Analysis, measurements: list[dict[str, Any]]) -> dict[str, Any]:
    values, label, notes = _signals(analysis, measurements)
    out: dict[str, Any] = {
        "id": analysis.id, "title": analysis.title, "kind": analysis.kind, "measurement": label,
        "x_label": analysis.x_label + (f" ({analysis.x_unit})" if analysis.x_unit else ""),
        "y_label": analysis.y_label or label, "x_log": analysis.x_log or analysis.fit in ("log_linear", "4pl", "5pl"),
        "series": [], "samples": [], "groups": [], "warnings": notes,
    }
    if analysis.kind == "group_comparison":
        out["groups"] = _compare(analysis, values, notes)
        return out

    by_series: dict[str, list[WellGroup]] = {}
    for g in analysis.standards:
        by_series.setdefault(g.series, []).append(g)
    for name, groups in by_series.items():
        out["series"].append(_series(name or ("Standards" if analysis.kind == "standard_curve" else analysis.title),
                                     groups, values, analysis, notes))
    if analysis.kind == "standard_curve" and out["series"]:
        out["samples"] = _interpolate(analysis, values, out["series"][0], notes)
    for series in out["series"]:
        series.pop("_fit", None)  # the fit object itself stays server-side
    return out


def _series(name: str, groups: list[WellGroup], values: dict[str, float], analysis: Analysis, notes: list[str]) -> dict[str, Any]:
    points, xs, ys = [], [], []
    for g in sorted(groups, key=lambda g: g.x):
        vals = _group_values(g, values)
        if not vals:
            continue
        s = _summary(vals)
        points.append({"name": g.name, "x": g.x, "y": s["mean"], "sd": s["sd"], "cv": _round(s["cv"]), "n": s["n"],
                       "wells": g.wells, "values": vals})
        xs += [g.x] * len(vals)
        ys += vals
    series: dict[str, Any] = {"name": name, "points": points, "fit": None, "curve": []}
    if analysis.fit == "none" or len(points) < 2:
        return series
    try:
        fit = fit_curve(analysis.fit, np.array(xs, dtype=float), np.array(ys, dtype=float))
    except AnalysisError as exc:
        notes.append(f"{name}: {exc}")
        return series
    series["_fit"] = fit
    series["fit"] = fit.view()
    lo, hi = min(p["x"] for p in points), max(p["x"] for p in points)
    if analysis.x_log or analysis.fit in ("log_linear", "4pl", "5pl"):
        grid = np.logspace(math.log10(lo), math.log10(hi), CURVE_POINTS)
    else:
        grid = np.linspace(lo, hi, CURVE_POINTS)
    series["curve"] = [{"x": _round(float(x)), "y": _round(float(fit.predict(x)))} for x in grid]
    if fit.r2 is not None and fit.r2 < 0.98 and analysis.kind == "standard_curve":
        notes.append(f"{name}: R² {fit.r2:.3f} is below 0.98; check the standards before trusting the results")
    return series


def _interpolate(analysis: Analysis, values: dict[str, float], series: dict[str, Any], notes: list[str]) -> list[dict[str, Any]]:
    fit: Fit | None = series.pop("_fit", None)
    ys = [p["y"] for p in series["points"]]
    lo_y, hi_y = (min(ys), max(ys)) if ys else (None, None)
    rows = []
    for g in analysis.samples:
        vals = _group_values(g, values)
        row: dict[str, Any] = {"name": g.name, "wells": g.wells, "dilution": g.dilution, "signal": None, "signal_sd": None,
                               "value": None, "value_sd": None, "cv": None, "n": len(vals), "flag": "", "values": vals}
        if not vals:
            row["flag"] = "not read"
            rows.append(row)
            continue
        s = _summary(vals)
        row.update(signal=_round(s["mean"]), signal_sd=_round(s["sd"]))
        if fit is None:
            row["flag"] = "no curve"
        else:
            conc = [fit.inverse(v) * g.dilution for v in vals]
            usable = [c for c in conc if c is not None and math.isfinite(c)]
            if usable:
                cs = _summary(usable)
                row.update(value=_round(cs["mean"]), value_sd=_round(cs["sd"]), cv=_round(cs["cv"]))
            if not usable:
                row["flag"] = "off the curve"
            elif s["mean"] > hi_y:
                row["flag"] = "above the highest standard"
            elif s["mean"] < lo_y:
                row["flag"] = "below the lowest standard"
        rows.append(row)
    flagged = [r["name"] for r in rows if r["flag"] in ("above the highest standard", "below the lowest standard")]
    if flagged:
        notes.append(f"Outside the standard range (extrapolated; dilute and re-run): {', '.join(flagged[:10])}")
    return rows


def _compare(analysis: Analysis, values: dict[str, float], notes: list[str]) -> list[dict[str, Any]]:
    data = {g.name: _group_values(g, values) for g in analysis.samples}
    control = analysis.control or ""
    control_vals = data.get(control, [])
    control_mean = float(np.mean(control_vals)) if control_vals else None
    rows = []
    for g in analysis.samples:
        vals = data[g.name]
        s = _summary(vals)
        row = {"name": g.name, "wells": g.wells, "mean": _round(s["mean"]), "sd": _round(s["sd"]), "cv": _round(s["cv"]),
               "n": s["n"], "values": vals, "fold_change": None, "p_value": None}
        if control_mean and g.name != control and vals:
            row["fold_change"] = _round(s["mean"] / control_mean)
            if len(vals) > 1 and len(control_vals) > 1:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    p = stats.ttest_ind(vals, control_vals, equal_var=False).pvalue
                row["p_value"] = _round(float(p)) if math.isfinite(p) else None
        rows.append(row)
    if control and not control_vals:
        notes.append(f"The control group '{control}' has no readings; no fold changes")
    return rows


def analyse_run(specs: list[dict[str, Any]], results: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Every analysis planned for a run, against its readings. Each failure is reported, not raised."""
    measurements = (results or {}).get("measurements", [])
    out = []
    for spec in specs:
        try:
            analysis = Analysis.model_validate(spec)
        except ValueError as exc:
            out.append({"title": spec.get("title", "Analysis"), "error": f"The analysis plan is invalid: {exc}"})
            continue
        if not measurements:
            out.append({"id": analysis.id, "title": analysis.title, "error": "No readings yet"})
            continue
        try:
            out.append(run_analysis(analysis, measurements))
        except AnalysisError as exc:
            out.append({"id": analysis.id, "title": analysis.title, "error": str(exc)})
    return out


def analysis_csv(run_id: str, analyses: list[dict[str, Any]]) -> str:
    """The results as rows a spreadsheet can take."""
    import csv
    import io

    buffer = io.StringIO()
    w = csv.writer(buffer)
    w.writerow(["run_id", "analysis", "kind", "row_type", "name", "series", "x", "signal_mean", "signal_sd", "n",
                "result", "result_sd", "cv_percent", "fold_change", "p_value", "flag", "wells", "fit", "r2", "ec50", "efficiency_percent"])
    for a in analyses:
        if a.get("error"):
            w.writerow([run_id, a["title"], "", "error", a["error"]])
            continue
        for s in a["series"]:
            fit = s.get("fit") or {}
            w.writerow([run_id, a["title"], a["kind"], "fit", s["name"], s["name"], "", "", "", "", "", "", "", "", "", "", "",
                        fit.get("equation", ""), fit.get("r2", ""), fit.get("ec50", ""), fit.get("efficiency_percent", "")])
            for p in s["points"]:
                w.writerow([run_id, a["title"], a["kind"], "point", p["name"], s["name"], p["x"], _round(p["y"]), _round(p["sd"]),
                            p["n"], "", "", p["cv"], "", "", "", " ".join(p["wells"])])
        for r in a["samples"]:
            w.writerow([run_id, a["title"], a["kind"], "sample", r["name"], "", "", r["signal"], r["signal_sd"], r["n"],
                        r["value"], r["value_sd"], r["cv"], "", "", r["flag"], " ".join(r["wells"])])
        for g in a["groups"]:
            w.writerow([run_id, a["title"], a["kind"], "group", g["name"], "", "", g["mean"], g["sd"], g["n"], "", "",
                        g["cv"], g["fold_change"], g["p_value"], "", " ".join(g["wells"])])
    return buffer.getvalue()


def _round(v: float | None, digits: int = 5) -> float | None:
    if v is None or not math.isfinite(v):
        return None
    return float(f"{v:.{digits}g}")


def _fmt(v: float) -> str:
    return "0" if abs(v) < 1e-12 else f"{v:.4g}"


def _signed(v: float) -> str:
    """A trailing term of an equation; empty when it is zero (to rounding)."""
    if abs(v) < 1e-12:
        return ""
    return f"- {_fmt(abs(v))}" if v < 0 else f"+ {_fmt(v)}"

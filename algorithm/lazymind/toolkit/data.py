"""Bounded statistical operations and chart specs. No generated programs are executed."""

import io
import math
import statistics
import threading
from typing import Literal

from .contracts import ToolError, file_result, public_tool


_PLOT_LOCK = threading.Lock()


def finite_values(values):
    if (not 1 <= len(values) <= 100000
            or any(type(value) not in (int, float) or not math.isfinite(value) for value in values)):
        raise ToolError('INVALID_ARGUMENT', 'Use 1..100000 finite numeric values; handle missing data explicitly.')
    return values


@public_tool
def data_statistics(values: list[float], operation: Literal['describe', 'ttest_ind', 'pearsonr'] = 'describe',
                    other: list[float] | None = None) -> dict:
    """Compute descriptive statistics, Welch's two-sample t-test or Pearson correlation on supplied data.

    Args:
        values: Numeric observations, with missing data explicitly removed by the caller.
        operation: Explicit method; no automatic method selection or interpretation.
        other: Required second sample for ttest_ind/pearsonr.
    """
    finite_values(values)
    if operation == 'describe':
        return {'count': len(values), 'mean': statistics.mean(values), 'median': statistics.median(values),
                'min': min(values), 'max': max(values),
                'sample_stddev': statistics.stdev(values) if len(values) > 1 else None}
    from scipy import stats
    finite_values(other or [])
    if len(values) < 2 or len(other) < 2:
        raise ToolError('INVALID_ARGUMENT', 'Both samples need at least two observations.')
    if operation == 'pearsonr' and len(values) != len(other):
        raise ToolError('INVALID_ARGUMENT', 'Pearson correlation requires paired samples of equal length.')
    result = (stats.pearsonr(values, other) if operation == 'pearsonr'
              else stats.ttest_ind(values, other, equal_var=False))
    if not math.isfinite(float(result.statistic)) or not math.isfinite(float(result.pvalue)):
        raise ToolError('UNDEFINED_STATISTIC',
                        'Statistic is undefined for these samples; inspect variance and sample size.')
    return {'method': 'pearsonr' if operation == 'pearsonr' else 'welch_ttest',
            'statistic': float(result.statistic), 'pvalue': float(result.pvalue),
            'n': [len(values), len(other)]}


@public_tool
def chart_render(x: list[str | float], y: list[float], kind: Literal['bar', 'line', 'scatter'] = 'bar',
                 title: str = '', x_label: str = '', y_label: str = '', format: Literal['png', 'svg'] = 'png') -> dict:
    """Render an explicit chart specification as a file. No generated code, external assets or implicit statistics.

    Args:
        x: Category labels or numeric coordinates, at most 1000.
        y: Finite values with the same length as x.
        kind: bar, line or scatter.
        title: Figure title.
        x_label: X-axis label.
        y_label: Y-axis label.
        format: png or svg.
    """
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    if len(x) != len(y) or len(x) > 1000:
        raise ToolError('INVALID_ARGUMENT', 'x and y must have the same length, at most 1000.')
    finite_values(y)
    if any(isinstance(value, (int, float)) and not math.isfinite(value) for value in x):
        raise ToolError('INVALID_ARGUMENT', 'x coordinates must be finite.')
    if any(len(value) > 1000 for value in (title, x_label, y_label)):
        raise ToolError('INVALID_ARGUMENT', 'Chart labels must not exceed 1000 characters.')
    # Matplotlib font caches and rc state are process-global; serialize drawing only.
    with _PLOT_LOCK:
        import matplotlib
        with matplotlib.rc_context({'text.usetex': False, 'svg.fonttype': 'none'}):
            figure = Figure(figsize=(8, 5), dpi=120)
            FigureCanvasAgg(figure)
            axis = figure.subplots()
            getattr(axis, {'bar': 'bar', 'line': 'plot', 'scatter': 'scatter'}[kind])(x, y)
            axis.set(title=title, xlabel=x_label, ylabel=y_label)
            figure.tight_layout()
            output = io.BytesIO()
            figure.savefig(output, format=format)
            figure.clear()
    return {'file': file_result(output.getvalue(), f'chart.{format}'), 'kind': kind, 'point_count': len(y)}

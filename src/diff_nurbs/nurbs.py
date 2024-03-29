from typing import List, Optional, Tuple, Type, TypeVar, Union

import matplotlib as mpl
import matplotlib.pyplot as plt
from packaging import version
import torch
import torch as th

C = TypeVar('C', bound='NURBSCurve')
S = TypeVar('S', bound='NURBSSurface')


class NoConvergenceError(RuntimeError):
    """An error indicating that NURBS point inversion failed to fit
    towards a satisfying result.
    """
    pass


_TORCH_VER = version.parse(th.__version__)
# `th.linalg.lu_{factor,solve}` was introduced in PyTorch 1.13.
if _TORCH_VER.major == 1 and _TORCH_VER.minor <= 12:
    def lu_factor(A: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        return th.lu(A)

    def lu_solve(
            b: torch.Tensor,
            LU_data: torch.Tensor,
            LU_pivots: torch.Tensor,
    ) -> torch.Tensor:
        return th.lu_solve(b, LU_data, LU_pivots)
else:
    def lu_factor(A: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        return th.linalg.lu_factor(A)

    def lu_solve(
            b: torch.Tensor,
            LU_data: torch.Tensor,
            LU_pivots: torch.Tensor,
    ) -> torch.Tensor:
        return th.linalg.lu_solve(LU_data, LU_pivots, b)


def setup_nurbs(
        degree: int,
        num_control_points: int,
        device: torch.device,
) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """Return uninitialized parameters for a NURBS curve with the
    desired properties on the desired device.
    """
    assert num_control_points > degree, \
        f'need at least {degree + 1} control points'
    control_points = th.empty((num_control_points, 2), device=device)
    # to get b-splines, set these weights to all ones
    control_point_weights = th.empty((num_control_points, 1), device=device)
    control_point_weights.clamp_(1e-8, th.finfo().max)
    next_degree = degree + 1
    knots = th.empty(num_control_points + next_degree, device=device)
    knots[:next_degree] = 0
    knots[next_degree:-next_degree] = 0.5
    knots[-next_degree:] = 1
    return control_points, control_point_weights, knots


def find_span(
        evaluation_points: torch.Tensor,
        degree: int,
        num_control_points: int,
        knots: torch.Tensor,
) -> torch.Tensor:
    """For each evaluation point, return the span in which it lies."""
    result = th.empty(
        len(evaluation_points),
        dtype=th.int64,
        device=knots.device,
    )
    not_upper_span_indices = \
        evaluation_points != knots[num_control_points]
    result[~not_upper_span_indices] = num_control_points - 1
    spans = th.searchsorted(
        knots,
        evaluation_points[not_upper_span_indices],
        right=True,
    ) - 1
    result[not_upper_span_indices] = spans
    return result


def get_basis(
        evaluation_points: torch.Tensor,
        span: torch.Tensor,
        degree: int,
        knots: torch.Tensor,
) -> torch.Tensor:
    """Return the basis functions applied to the evaluation points."""
    device = knots.device
    num_evaluation_points = len(evaluation_points)
    next_degree = degree + 1
    next_span = span + 1
    basis_values = th.empty(
        (num_evaluation_points, next_degree),
        device=device,
    )
    basis_values[:, 0] = 1
    left = th.empty((num_evaluation_points, next_degree), device=device)
    right = th.empty((num_evaluation_points, next_degree), device=device)
    for j in range(1, next_degree):
        left[:, j] = evaluation_points - knots[next_span - j]
        right[:, j] = knots[span + j] - evaluation_points
        saved = th.zeros(num_evaluation_points, device=device)
        for r in range(j):
            tmp = basis_values[:, r] / (right[:, r + 1] + left[:, j - r])
            basis_values[:, r] = saved + right[:, r + 1] * tmp
            saved = left[:, j - r] * tmp
        basis_values[:, j] = saved
    return basis_values


def get_single_basis(
        evaluation_points: torch.Tensor,
        span: torch.Tensor,
        degree: int,
        knots: torch.Tensor,
) -> torch.Tensor:
    device = knots.device
    last_knot_index = len(knots) - 1
    next_degree = degree + 1

    basis = th.empty((len(evaluation_points), next_degree), device=device)
    result = th.empty((len(evaluation_points),), device=device)
    not_done_indices = th.ones_like(result, dtype=th.bool, device=device)

    edge_indices = (
        (
            (span == 0)
            & (evaluation_points == knots[0])
        ) | (
            (span == last_knot_index - degree - 1)
            & (evaluation_points == knots[-1])
        )
    )
    result[edge_indices] = 1
    not_done_indices = not_done_indices & ~edge_indices

    local_indices = (
        not_done_indices
        & (
            (evaluation_points < knots[span])
            | (evaluation_points >= knots[span + next_degree])
        )
    )
    result[local_indices] = 0
    not_done_indices = not_done_indices & ~local_indices

    curr_eval_points = evaluation_points[not_done_indices]
    if len(span) > 1:
        curr_span = span[not_done_indices]
    else:
        curr_span = span
    next_curr_span = curr_span + 1
    for j in range(next_degree):
        basis[not_done_indices, j] = th.where(
            (
                (curr_eval_points >= knots[curr_span + j])
                & (curr_eval_points < knots[next_curr_span + j])
            ),
            th.tensor(1.0, device=device),
            th.tensor(0.0, device=device),
        )

    for k in range(1, next_degree):
        curr_basis = basis[not_done_indices, 0]
        saved = th.where(
            curr_basis == 0,
            th.tensor(0.0, device=device),
            (
                ((curr_eval_points - knots[curr_span]) * curr_basis)
                / (knots[curr_span + k] - knots[curr_span])
            ),
        )

        for j in range(next_degree - k):
            left = knots[next_curr_span + j]
            right = knots[next_curr_span + j + k]

            zero_basis_indices = not_done_indices & (basis[:, j + 1] == 0)
            dense_zero_basis_indices = zero_basis_indices[not_done_indices]

            basis[zero_basis_indices, j] = saved[dense_zero_basis_indices]

            not_zero_basis_indices = not_done_indices & ~zero_basis_indices
            not_dense_zero_basis_indices = \
                not_zero_basis_indices[not_done_indices]

            if len(span) > 1:
                left = left[not_dense_zero_basis_indices]
                right = right[not_dense_zero_basis_indices]

            tmp = (
                basis[not_zero_basis_indices, j + 1]
                / (right - left)
            )
            basis[not_zero_basis_indices, j] = (
                saved[not_dense_zero_basis_indices]
                + (
                    (right - curr_eval_points[not_dense_zero_basis_indices])
                    * tmp
                )
            )

            nonzeros = (
                curr_eval_points[not_dense_zero_basis_indices] - left
            ) * tmp
            saved[dense_zero_basis_indices] = 0
            saved[not_dense_zero_basis_indices] = nonzeros

    result[not_done_indices] = basis[not_done_indices, 0]
    return result


def calc_basis_derivs(
        evaluation_points: torch.Tensor,
        span: torch.Tensor,
        degree: int,
        knots: torch.Tensor,
        nth_deriv: int = 1,
) -> torch.Tensor:
    """Return the first `nth_deriv` derivatives for the basis functions
    applied to the given evaluation points. The k-th derivative is at
    index k, 0 <= k <= `nth_deriv`.
    """
    device = knots.device
    num_evaluation_points = len(evaluation_points)
    next_span = span + 1
    next_degree = degree + 1
    next_nth_deriv = nth_deriv + 1
    ndu = th.empty(
        (num_evaluation_points, next_degree, next_degree),
        device=device,
    )
    ndu[:, 0, 0] = 1
    left = th.empty((num_evaluation_points, next_degree), device=device)
    right = th.empty((num_evaluation_points, next_degree), device=device)
    for j in range(1, next_degree):
        left[:, j] = evaluation_points - knots[next_span - j]
        right[:, j] = knots[span + j] - evaluation_points
        saved = th.zeros(num_evaluation_points, device=device)
        for r in range(j):
            ndu[:, j, r] = right[:, r + 1] + left[:, j - r]
            tmp = ndu[:, r, j - 1] / ndu[:, j, r]
            ndu[:, r, j] = saved + right[:, r + 1] * tmp
            saved = left[:, j - r] * tmp
        ndu[:, j, j] = saved

    ders = th.empty(
        (num_evaluation_points, next_nth_deriv, next_degree),
        device=device,
    )
    for j in range(next_degree):
        ders[:, 0, j] = ndu[:, j, degree]
    a = th.empty((num_evaluation_points, 2, next_degree), device=device)
    for r in range(next_degree):
        s1 = 0
        s2 = 1
        a[:, 0, 0] = 1
        for k in range(1, next_nth_deriv):
            d = th.zeros(num_evaluation_points, device=device)
            rk = r - k
            pk = degree - k
            if r >= k:
                a[:, s2, 0] = a[:, s1, 0] / ndu[:, pk + 1, rk]
                d = a[:, s2, 0] * ndu[:, rk, pk]
            if rk >= -1:
                j1 = 1
            else:
                j1 = -rk
            if r - 1 <= pk:
                j2 = k - 1
            else:
                j2 = degree - r
            for j in range(j1, j2 + 1):
                a[:, s2, j] = (
                    (a[:, s1, j] - a[:, s1, j - 1])
                    / ndu[:, pk + 1, rk + j]
                )
                d += a[:, s2, j] * ndu[:, rk + j, pk]
            if r <= pk:
                a[:, s2, k] = -a[:, s1, k - 1] / ndu[:, pk + 1, r]
                d += a[:, s2, k] * ndu[:, r, pk]
            ders[:, k, r] = d
            j = s1
            s1 = s2
            s2 = j

    r = degree
    for k in range(1, next_nth_deriv):
        for j in range(next_degree):
            ders[:, k, j] *= r
        r *= degree - k
    return ders


def calc_basis_derivs_slow(
        evaluation_points: torch.Tensor,
        span: torch.Tensor,
        degree: int,
        knots: torch.Tensor,
        nth_deriv: int = 1,
) -> List[List[torch.Tensor]]:
    """Return the first `nth_deriv` derivatives for the basis functions
    applied to the given evaluation points. The k-th derivative is at
    index k, 0 <= k <= `nth_deriv`.

    This function is slightly slower than `calc_basis_derivs` but is
    fully differentiable.
    """
    device = knots.device
    num_evaluation_points = len(evaluation_points)
    next_span = span + 1
    next_degree = degree + 1
    next_nth_deriv = nth_deriv + 1
    ndu = [
        [
            th.empty(
                (num_evaluation_points,),
                device=device,
            )
            for _ in range(next_degree)
        ]
        for _ in range(next_degree)
    ]
    ndu[0][0] = th.ones_like(ndu[0][0])
    left = [
        th.empty((num_evaluation_points,), device=device)
        for _ in range(next_degree)
    ]
    right = [
        th.empty((num_evaluation_points,), device=device)
        for _ in range(next_degree)
    ]
    for j in range(1, next_degree):
        left[j] = evaluation_points - knots[next_span - j]
        right[j] = knots[span + j] - evaluation_points
        saved = th.zeros(num_evaluation_points, device=device)
        for r in range(j):
            ndu[j][r] = right[r + 1] + left[j - r]
            tmp = ndu[r][j - 1] / ndu[j][r]
            ndu[r][j] = saved + right[r + 1] * tmp
            saved = left[j - r] * tmp
        ndu[j][j] = saved

    ders = [
        [
            th.empty((num_evaluation_points,), device=device)
            for _ in range(next_degree)
        ]
        for _ in range(next_nth_deriv)
    ]
    for j in range(next_degree):
        ders[0][j] = ndu[j][degree]
    a = [
        [
            th.empty((num_evaluation_points,), device=device)
            for _ in range(next_degree)
        ]
        for _ in range(2)
    ]
    for r in range(next_degree):
        s1 = 0
        s2 = 1
        a[0][0] = th.ones_like(a[0][0])
        for k in range(1, next_nth_deriv):
            d = th.zeros(num_evaluation_points, device=device)
            rk = r - k
            pk = degree - k
            if r >= k:
                a[s2][0] = a[s1][0] / ndu[pk + 1][rk]
                d = a[s2][0] * ndu[rk][pk]
            if rk >= -1:
                j1 = 1
            else:
                j1 = -rk
            if r - 1 <= pk:
                j2 = k - 1
            else:
                j2 = degree - r
            for j in range(j1, j2 + 1):
                a[s2][j] = (
                    (a[s1][j] - a[s1][j - 1])
                    / ndu[pk + 1][rk + j]
                )
                d += a[s2][j] * ndu[rk + j][pk]
            if r <= pk:
                a[s2][k] = -a[s1][k - 1] / ndu[pk + 1][r]
                d += a[s2][k] * ndu[r][pk]
            ders[k][r] = d
            j = s1
            s1 = s2
            s2 = j

    r = degree
    for k in range(1, next_nth_deriv):
        for j in range(next_degree):
            ders[k][j] *= r
        r *= degree - k
    return ders


def project_control_points(
        control_points: torch.Tensor,
        control_point_weights: torch.Tensor,
) -> torch.Tensor:
    """Project the given n-D control points with their weights into (n +
    1)-D space.
    """
    projected = control_point_weights * control_points
    projected = th.cat([projected, control_point_weights], dim=-1)
    return projected


@th.no_grad()
def check_nurbs_constraints(
        evaluation_points: torch.Tensor,
        degree: int,
        control_points: torch.Tensor,
        control_point_weights: torch.Tensor,
        knots: torch.Tensor,
) -> None:
    """Assert that NURBS constraints are fulfilled for evaluating the
    given curve.
    """
    next_degree = degree + 1
    assert control_points.shape[-1] == 2, \
        "please use another evaluation function for this NURBS' dimensionality"
    assert control_points.ndim == 2, \
        "please use another evaluation function for this NURBS' dimensionality"
    assert (control_point_weights > 0).all(), \
        'control point weights must be greater than zero'
    assert (knots[:next_degree] == 0).all(), \
        f'first {next_degree} knots must be zero'
    assert (knots[len(control_points):] == 1).all(), \
        f'last {next_degree} knots must be one'
    assert (knots.sort().values == knots).all(), \
        'knots must be ordered monotonically increasing in value'


def evaluate_nurbs(
        evaluation_points: torch.Tensor,
        degree: int,
        control_points: torch.Tensor,
        control_point_weights: torch.Tensor,
        knots: torch.Tensor,
) -> torch.Tensor:
    """Return the result for evaluating a NURBS curve with the given
    parameters on the given evaluation points.
    """
    check_nurbs_constraints(
        evaluation_points,
        degree,
        control_points,
        control_point_weights,
        knots,
    )

    projected = project_control_points(control_points, control_point_weights)
    spans = find_span(evaluation_points, degree, len(control_points), knots)
    spansmdeg = spans - degree
    basis_values = get_basis(evaluation_points, spans, degree, knots)
    Cw = th.zeros(
        (len(evaluation_points), projected.shape[-1]),
        device=control_points.device,
    )
    for j in range(degree + 1):
        Cw += basis_values[:, j] * projected[spansmdeg + j]
    return Cw[:, :-1] / Cw[:, -1]


def calc_bspline_derivs(
        evaluation_point: torch.Tensor,
        degree: int,
        control_points: torch.Tensor,
        knots: torch.Tensor,
        nth_deriv: int = 1,
) -> torch.Tensor:
    """Return the first `nth_deriv` derivatives for the given B-spline
    curve at the given evaluation points. The k-th derivative is at
    index k, 0 <= k <= `nth_deriv`.
    """
    device = control_points.device
    next_degree = degree + 1
    next_nth_deriv = nth_deriv + 1
    num_control_points = len(control_points) - 1
    du = min(nth_deriv, degree)
    result = th.empty(
        (next_nth_deriv, control_points.shape[-1]), device=device)
    for k in range(next_degree, next_nth_deriv):
        result[k] = 0
    span = find_span(evaluation_point, degree, num_control_points, knots)
    basis_derivs = calc_basis_derivs(evaluation_point, span, degree, knots, du)
    spanmdeg = span - degree
    for k in range(du + 1):
        result[k] = 0
        for j in range(next_degree):
            result[k] += basis_derivs[k, j] * control_points[spanmdeg + j]
    return result


def calc_derivs(
        evaluation_point: torch.Tensor,
        degree: int,
        control_points: torch.Tensor,
        control_point_weights: torch.Tensor,
        knots: torch.Tensor,
        nth_deriv: int = 1,
) -> torch.Tensor:
    """Return the first `nth_deriv` derivatives for the given NURBS
    curve at the given evaluation points. The k-th derivative is at
    index k, 0 <= k <= `nth_deriv`.
    """
    dtype = control_points.dtype
    device = control_points.device
    next_nth_deriv = nth_deriv + 1

    projected = project_control_points(control_points, control_point_weights)
    Cwders = calc_bspline_derivs(
        evaluation_point, degree, projected, knots, nth_deriv)
    Aders = Cwders[:, :-1]
    wders = Cwders[:, -1]
    result = th.empty_like(Aders)
    for k in th.arange(next_nth_deriv, device=device):
        v = Aders[k]
        for i in th.arange(1, k + 1, device=device):
            v -= (
                th.binomial(k.to(dtype), i.to(dtype))
                * wders[i]
                * result[k - i]
            )
        result[k] = v / wders[0]
    return result


class NURBSCurve:
    def __init__(
            self,
            degree: int,
            control_points: torch.Tensor,
            control_point_weights: torch.Tensor,
            knots: torch.Tensor,
    ) -> None:
        self.degree = degree
        self.control_points = control_points
        self.control_point_weights = control_point_weights
        self.knots = knots

    @classmethod
    def create_empty(
            cls: Type[C],
            degree: int,
            num_control_points: int,
            device: th.device,
    ) -> C:
        control_points, control_point_weights, knots = setup_nurbs(
            degree, num_control_points, device)
        return cls(degree, control_points, control_point_weights, knots)

    def find_span(
            self,
            evaluation_point: torch.Tensor,
            num_control_points: Optional[int] = None,
    ) -> torch.Tensor:
        if num_control_points is None:
            num_control_points = len(self.control_points)
        return find_span(
            evaluation_point,
            self.degree,
            num_control_points,
            self.knots,
        )

    def get_basis(
            self,
            evaluation_point: torch.Tensor,
            span: Optional[torch.Tensor] = None,
    ) -> torch.Tensor:
        if span is None:
            span = self.find_span(evaluation_point)
        return get_basis(evaluation_point, span, self.degree, self.knots)

    def calc_basis_derivs(
            self,
            evaluation_point: torch.Tensor,
            span: Optional[torch.Tensor] = None,
            nth_deriv: int = 1,
    ) -> torch.Tensor:
        if span is None:
            span = self.find_span(evaluation_point)
        return calc_basis_derivs(
            evaluation_point, span, self.degree, self.knots, nth_deriv)

    def project_control_points(self) -> torch.Tensor:
        return project_control_points(
            self.control_points, self.control_point_weights)

    def evaluate(self, evaluation_point: torch.Tensor) -> torch.Tensor:
        return evaluate_nurbs(
            evaluation_point,
            self.degree,
            self.control_points,
            self.control_point_weights,
            self.knots,
        )

    def calc_bspline_derivs(
            self,
            evaluation_point: torch.Tensor,
            nth_deriv: int = 1,
    ) -> torch.Tensor:
        return calc_bspline_derivs(
            evaluation_point,
            self.degree,
            self.control_points,
            self.knots,
            nth_deriv,
        )

    def calc_derivs(
            self,
            evaluation_point: torch.Tensor,
            nth_deriv: int = 1,
    ) -> torch.Tensor:
        return calc_derivs(
            evaluation_point,
            self.degree,
            self.control_points,
            self.control_point_weights,
            self.knots,
            nth_deriv,
        )


def setup_nurbs_surface(
        degree_x: int,
        degree_y: int,
        num_control_points_x: int,
        num_control_points_y: int,
        device: torch.device,
) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
    """Return uninitialized parameters for a NURBS surface with the
    desired properties on the given device.
    """
    next_degree_x = degree_x + 1
    next_degree_y = degree_y + 1
    assert num_control_points_x > degree_x, \
        f'need at least {next_degree_x} control points in x direction'
    assert num_control_points_y > degree_y, \
        f'need at least {next_degree_y} control points in y direction'

    control_points_shape = (num_control_points_x, num_control_points_y)
    control_points = th.empty(
        control_points_shape + (3,),
        device=device,
    )
    # to get b-splines, set these weights to all ones
    control_point_weights = th.empty(
        control_points_shape + (1,),
        device=device,
    )
    control_point_weights.clamp_(1e-8, th.finfo().max)

    knots_x = th.zeros(num_control_points_x + next_degree_x, device=device)
    # knots_x[:next_degree_x] = 0
    knots_x[next_degree_x:-next_degree_x] = 0.5
    knots_x[-next_degree_x:] = 1

    knots_y = th.zeros(num_control_points_y + next_degree_y, device=device)
    # knots_y[:next_degree_y] = 0
    knots_y[next_degree_y:-next_degree_y] = 0.5
    knots_y[-next_degree_y:] = 1
    return control_points, control_point_weights, knots_x, knots_y


@th.no_grad()
def check_nurbs_surface_constraints(
        evaluation_points_x: torch.Tensor,
        evaluation_points_y: torch.Tensor,
        degree_x: int,
        degree_y: int,
        control_points: torch.Tensor,
        control_point_weights: torch.Tensor,
        knots_x: torch.Tensor,
        knots_y: torch.Tensor,
) -> None:
    """Assert that NURBS constraints are fulfilled for evaluating the
    given surface.
    """
    next_degree_x = degree_x + 1
    next_degree_y = degree_y + 1
    assert control_points.shape[-1] == 3, \
        "please use another evaluation function for this NURBS' dimensionality"
    assert control_points.ndim == 3, \
        "please use another evaluation function for this NURBS' dimensionality"
    assert (control_point_weights > 0).all(), \
        'control point weights must be greater than zero'
    assert (knots_x[:next_degree_x] == 0).all(), \
        f'first {next_degree_x} knots must be zero'
    assert (knots_x[control_points.shape[0]:] == 1).all(), \
        f'last {next_degree_x} knots must be one'
    assert (knots_x.sort().values == knots_x).all(), \
        'knots must be ordered monotonically increasing in value'
    assert (knots_y[:next_degree_y] == 0).all(), \
        f'first {next_degree_y} knots must be zero'
    assert (knots_y[control_points.shape[1]:] == 1).all(), \
        f'last {next_degree_y} knots must be one'
    assert (knots_y.sort().values == knots_y).all(), \
        'knots must be ordered monotonically increasing in value'
    assert evaluation_points_x.shape == evaluation_points_y.shape, \
        "evaluation point shapes don't match"


def evaluate_nurbs_surface_at_spans(
        num_evaluation_points: int,
        spans_x: torch.Tensor,
        spans_y: torch.Tensor,
        basis_values_x: torch.Tensor,
        basis_values_y: torch.Tensor,
        degree_x: int,
        degree_y: int,
        control_points: torch.Tensor,
        control_point_weights: torch.Tensor,
) -> torch.Tensor:
    """Return evaluations of the given NURBS surface at the given spans
    with the corresponding basis values.
    """
    device = control_points.device
    projected = project_control_points(control_points, control_point_weights)
    tmp = th.empty(
        (num_evaluation_points, degree_y + 1, projected.shape[-1]),
        device=device,
    )
    for j in range(degree_y + 1):
        tmp[:, j] = 0
        for k in range(degree_x + 1):
            tmp[:, j] += (
                basis_values_x[:, k].unsqueeze(-1)
                * projected[spans_x - degree_x + k, spans_y - degree_y + j]
            )
    Sw = th.zeros((num_evaluation_points, projected.shape[-1]), device=device)
    for j in range(degree_y + 1):
        Sw += basis_values_y[:, j].unsqueeze(-1) * tmp[:, j]
    return Sw[:, :-1] / Sw[:, -1].unsqueeze(-1)


def evaluate_nurbs_surface_flex(
        evaluation_points_x: torch.Tensor,
        evaluation_points_y: torch.Tensor,
        degree_x: int,
        degree_y: int,
        control_points: torch.Tensor,
        control_point_weights: torch.Tensor,
        knots_x: torch.Tensor,
        knots_y: torch.Tensor,
) -> torch.Tensor:
    """Return evaluations of the given NURBS surface at the given
    evaluation points in x- and y-direction.
    """
    check_nurbs_surface_constraints(
        evaluation_points_x,
        evaluation_points_y,
        degree_x,
        degree_y,
        control_points,
        control_point_weights,
        knots_x,
        knots_y,
    )

    num_evaluation_points = len(evaluation_points_x)
    num_control_points_x = control_points.shape[0]
    spans_x = find_span(
        evaluation_points_x, degree_x, num_control_points_x, knots_x)
    basis_values_x = get_basis(evaluation_points_x, spans_x, degree_x, knots_x)

    num_control_points_y = control_points.shape[1]
    spans_y = find_span(
        evaluation_points_y, degree_y, num_control_points_y, knots_y)
    basis_values_y = get_basis(evaluation_points_y, spans_y, degree_y, knots_y)

    return evaluate_nurbs_surface_at_spans(
        num_evaluation_points,
        spans_x,
        spans_y,
        basis_values_x,
        basis_values_y,
        degree_x,
        degree_y,
        control_points,
        control_point_weights,
    )


def calc_bspline_derivs_surface(
        evaluation_points_x: torch.Tensor,
        evaluation_points_y: torch.Tensor,
        degree_x: int,
        degree_y: int,
        control_points: torch.Tensor,
        knots_x: torch.Tensor,
        knots_y: torch.Tensor,
        nth_deriv: int = 1,
) -> torch.Tensor:
    """Return partial derivatives up to `nth_deriv` at the given
    evaluation points for the given B-spline surface.

    The resulting 4-D tensor `derivs` contains at `derivs[:, k, l]` the
    derivatives with respect to `evaluation_points_x` `k` times and
    `evaluation_points_y` `l` times.
    """
    device = control_points.device
    num_evaluation_points = len(evaluation_points_x)
    next_nth_deriv = nth_deriv + 1
    next_degree_x = degree_x + 1
    next_degree_y = degree_y + 1
    result = th.empty(
        (
            num_evaluation_points,
            next_nth_deriv,
            next_nth_deriv,
            control_points.shape[-1],
        ),
        device=device,
    )
    du = min(nth_deriv, degree_x)
    for k in range(next_degree_x, next_nth_deriv):
        for j in range(next_nth_deriv - k):
            result[:, k, j] = 0
    dv = min(nth_deriv, degree_y)
    for j in range(next_degree_y, next_nth_deriv):
        for k in range(next_nth_deriv - j):
            result[:, k, j] = 0

    num_control_points_x = control_points.shape[0]
    spans_x = find_span(
        evaluation_points_x, degree_x, num_control_points_x, knots_x)
    basis_derivs_x = calc_basis_derivs(
        evaluation_points_x, spans_x, degree_x, knots_x, du)

    num_control_points_y = control_points.shape[1]
    spans_y = find_span(
        evaluation_points_y, degree_y, num_control_points_y, knots_y)
    basis_derivs_y = calc_basis_derivs(
        evaluation_points_y, spans_y, degree_y, knots_y, dv)

    tmp = th.empty(
        (num_evaluation_points, next_degree_y, control_points.shape[-1]),
        device=device,
    )
    spanmdegree_x = spans_x - degree_x
    spanmdegree_y = spans_y - degree_y
    for k in range(du + 1):
        for s in range(next_degree_y):
            tmp[:, s] = 0
            for r in range(next_degree_x):
                tmp[:, s] += (
                    basis_derivs_x[:, k, r].unsqueeze(-1)
                    * control_points[spanmdegree_x + r, spanmdegree_y + s]
                )
        dd = min(nth_deriv - k, dv)
        for j in range(dd + 1):
            result[:, k, j] = 0
            for s in range(next_degree_y):
                result[:, k, j] += \
                    basis_derivs_y[:, j, s].unsqueeze(-1) * tmp[:, s]
    return result


def calc_bspline_derivs_surface_slow(
        evaluation_points_x: torch.Tensor,
        evaluation_points_y: torch.Tensor,
        degree_x: int,
        degree_y: int,
        control_points: torch.Tensor,
        knots_x: torch.Tensor,
        knots_y: torch.Tensor,
        nth_deriv: int = 1,
) -> torch.Tensor:
    """Return partial derivatives up to `nth_deriv` at the given
    evaluation points for the given B-spline surface.

    The resulting 4-D tensor `derivs` contains at `derivs[:, k, l]` the
    derivatives with respect to `evaluation_points_x` `k` times and
    `evaluation_points_y` `l` times.

    This function is slightly slower than `calc_bspline_derivs_surface`
    but is fully differentiable.
    """
    device = control_points.device
    num_evaluation_points = len(evaluation_points_x)
    next_nth_deriv = nth_deriv + 1
    next_degree_x = degree_x + 1
    next_degree_y = degree_y + 1
    result = th.empty(
        (
            num_evaluation_points,
            next_nth_deriv,
            next_nth_deriv,
            control_points.shape[-1],
        ),
        device=device,
    )
    du = min(nth_deriv, degree_x)
    for k in range(next_degree_x, next_nth_deriv):
        for j in range(next_nth_deriv - k):
            result[:, k, j] = 0
    dv = min(nth_deriv, degree_y)
    for j in range(next_degree_y, next_nth_deriv):
        for k in range(next_nth_deriv - j):
            result[:, k, j] = 0

    num_control_points_x = control_points.shape[0]
    spans_x = find_span(
        evaluation_points_x, degree_x, num_control_points_x, knots_x)
    basis_derivs_x = calc_basis_derivs_slow(
        evaluation_points_x, spans_x, degree_x, knots_x, du)

    num_control_points_y = control_points.shape[1]
    spans_y = find_span(
        evaluation_points_y, degree_y, num_control_points_y, knots_y)
    basis_derivs_y = calc_basis_derivs_slow(
        evaluation_points_y, spans_y, degree_y, knots_y, dv)

    tmp = [
        th.empty(
            (num_evaluation_points, control_points.shape[-1]),
            device=device,
        )
        for _ in range(next_degree_y)
    ]
    spanmdegree_x = spans_x - degree_x
    spanmdegree_y = spans_y - degree_y
    for k in range(du + 1):
        for s in range(next_degree_y):
            tmp[s] = th.zeros_like(tmp[s])
            for r in range(next_degree_x):
                tmp[s] += (
                    basis_derivs_x[k][r].unsqueeze(-1)
                    * control_points[spanmdegree_x + r, spanmdegree_y + s]
                )
        dd = min(nth_deriv - k, dv)
        for j in range(dd + 1):
            result[:, k, j] = 0
            for s in range(next_degree_y):
                result[:, k, j] += \
                    basis_derivs_y[j][s].unsqueeze(-1) * tmp[s]
    return result


def calc_derivs_surface(
        evaluation_points_x: torch.Tensor,
        evaluation_points_y: torch.Tensor,
        degree_x: int,
        degree_y: int,
        control_points: torch.Tensor,
        control_point_weights: torch.Tensor,
        knots_x: torch.Tensor,
        knots_y: torch.Tensor,
        nth_deriv: int = 1,
) -> torch.Tensor:
    """Return partial derivatives up to `nth_deriv` at the given
    evaluation points for the given NURBS surface.

    The resulting 4-D tensor `derivs` contains at `derivs[:, k, l]` the
    derivatives with respect to `evaluation_points_x` `k` times and
    `evaluation_points_y` `l` times.
    """
    check_nurbs_surface_constraints(
        evaluation_points_x,
        evaluation_points_y,
        degree_x,
        degree_y,
        control_points,
        control_point_weights,
        knots_x,
        knots_y,
    )

    dtype = control_points.dtype
    device = control_points.device
    next_nth_deriv = nth_deriv + 1

    projected = project_control_points(control_points, control_point_weights)
    Swders = calc_bspline_derivs_surface(
        evaluation_points_x,
        evaluation_points_y,
        degree_x,
        degree_y,
        projected,
        knots_x,
        knots_y,
        nth_deriv,
    )
    Aders = Swders[:, :, :, :-1]
    wders = Swders[:, :, :, -1]
    result = th.empty_like(Aders)
    for k in th.arange(next_nth_deriv, device=device):
        for m in th.arange(next_nth_deriv - k, device=device):
            vs = Aders[:, k, m]
            for j in th.arange(1, m + 1, device=device):
                vs -= (
                    th.binomial(m.to(dtype), j.to(dtype))
                    * wders[:, 0, j].unsqueeze(-1)
                    * result[:, k, m - j]
                )
            for i in th.arange(1, k + 1, device=device):
                vs -= (
                    th.binomial(k.to(dtype), i.to(dtype))
                    * wders[:, i, 0].unsqueeze(-1)
                    * result[:, k - i, m]
                )
                vs2 = th.zeros_like(vs)
                for j in th.arange(1, m + 1, device=device):
                    vs2 += (
                        th.binomial(m.to(dtype), j.to(dtype))
                        * wders[:, i, j].unsqueeze(-1)
                        * result[:, k - i, m - j]
                    )
                vs -= th.binomial(k.to(dtype), i.to(dtype)) * vs2
            result[:, k, m] = vs / wders[:, 0, 0].unsqueeze(-1)
    return result


def calc_derivs_surface_slow(
        evaluation_points_x: torch.Tensor,
        evaluation_points_y: torch.Tensor,
        degree_x: int,
        degree_y: int,
        control_points: torch.Tensor,
        control_point_weights: torch.Tensor,
        knots_x: torch.Tensor,
        knots_y: torch.Tensor,
        nth_deriv: int = 1,
) -> List[List[torch.Tensor]]:
    """Return partial derivatives up to `nth_deriv` at the given
    evaluation points for the given NURBS surface.

    The resulting 4-D tensor `derivs` contains at `derivs[:, k, l]` the
    derivatives with respect to `evaluation_points_x` `k` times and
    `evaluation_points_y` `l` times.

    This function is slightly slower than `calc_derivs_surface` but is
    fully differentiable.
    """
    check_nurbs_surface_constraints(
        evaluation_points_x,
        evaluation_points_y,
        degree_x,
        degree_y,
        control_points,
        control_point_weights,
        knots_x,
        knots_y,
    )

    dtype = control_points.dtype
    device = control_points.device
    next_nth_deriv = nth_deriv + 1

    projected = project_control_points(control_points, control_point_weights)
    Swders = calc_bspline_derivs_surface_slow(
        evaluation_points_x,
        evaluation_points_y,
        degree_x,
        degree_y,
        projected,
        knots_x,
        knots_y,
        nth_deriv,
    )
    Aders = Swders[:, :, :, :-1]
    wders = Swders[:, :, :, -1]
    result = [
        [
            th.empty(Aders.shape[0], Aders.shape[-1], device=device)
            for _ in range(Aders.shape[1])
        ]
        for _ in range(Aders.shape[2])
    ]
    for k in th.arange(next_nth_deriv, device=device):
        for m in th.arange(next_nth_deriv - k, device=device):
            vs = Aders[:, k, m]
            for j in th.arange(1, m + 1, device=device):
                vs = vs - (
                    th.binomial(m.to(dtype), j.to(dtype))
                    * wders[:, 0, j].unsqueeze(-1)
                    * result[k][m - j]
                )
            for i in th.arange(1, k + 1, device=device):
                vs = vs - (
                    th.binomial(k.to(dtype), i.to(dtype))
                    * wders[:, i, 0].unsqueeze(-1)
                    * result[k - i][m]
                )
                vs2 = th.zeros_like(vs)
                for j in th.arange(1, m + 1, device=device):
                    vs2 += (
                        th.binomial(m.to(dtype), j.to(dtype))
                        * wders[:, i, j].unsqueeze(-1)
                        * result[k - i][m - j]
                    )
                vs = vs - th.binomial(k.to(dtype), i.to(dtype)) * vs2
            result[k][m] = vs / wders[:, 0, 0].unsqueeze(-1)
    return result


def calc_normals_surface(
        evaluation_points_x: torch.Tensor,
        evaluation_points_y: torch.Tensor,
        degree_x: int,
        degree_y: int,
        control_points: torch.Tensor,
        control_point_weights: torch.Tensor,
        knots_x: torch.Tensor,
        knots_y: torch.Tensor,
) -> torch.Tensor:
    """Return the normals of the given NURBS surface at the given
    evaluation points.
    """
    derivs = calc_derivs_surface(
        evaluation_points_x,
        evaluation_points_y,
        degree_x,
        degree_y,
        control_points,
        control_point_weights,
        knots_x,
        knots_y,
        nth_deriv=1,
    )
    cross_prod = th.cross(derivs[:, 1, 0], derivs[:, 0, 1])
    return cross_prod / th.linalg.norm(cross_prod, dim=1).unsqueeze(-1)


def calc_normals_surface_slow(
        evaluation_points_x: torch.Tensor,
        evaluation_points_y: torch.Tensor,
        degree_x: int,
        degree_y: int,
        control_points: torch.Tensor,
        control_point_weights: torch.Tensor,
        knots_x: torch.Tensor,
        knots_y: torch.Tensor,
) -> torch.Tensor:
    """Return the normals of the given NURBS surface at the given
    evaluation points.

    This function is slightly slower than `calc_normals_surface` but is
    fully differentiable.
    """
    derivs = calc_derivs_surface_slow(
        evaluation_points_x,
        evaluation_points_y,
        degree_x,
        degree_y,
        control_points,
        control_point_weights,
        knots_x,
        knots_y,
        nth_deriv=1,
    )
    cross_prod = th.cross(derivs[1][0], derivs[0][1])
    return cross_prod / th.linalg.norm(cross_prod, dim=1).unsqueeze(-1)


def calc_normals_and_surface_slow(
        evaluation_points_x: torch.Tensor,
        evaluation_points_y: torch.Tensor,
        degree_x: int,
        degree_y: int,
        control_points: torch.Tensor,
        control_point_weights: torch.Tensor,
        knots_x: torch.Tensor,
        knots_y: torch.Tensor,
) -> Tuple[torch.Tensor, torch.Tensor]:
    """Return both the evaluation and normals of the given NURBS surface
    at the given evaluation points.
    """
    derivs = calc_derivs_surface_slow(
        evaluation_points_x,
        evaluation_points_y,
        degree_x,
        degree_y,
        control_points,
        control_point_weights,
        knots_x,
        knots_y,
        nth_deriv=1,
    )
    cross_prod = th.cross(derivs[1][0], derivs[0][1])
    return (
        derivs[0][0],
        cross_prod / th.linalg.norm(cross_prod, dim=1).unsqueeze(-1),
    )


def plot_surface(
        degree_x: int,
        degree_y: int,
        control_points: torch.Tensor,
        control_point_weights: torch.Tensor,
        knots_x: torch.Tensor,
        knots_y: torch.Tensor,
        step_granularity_x: float = 0.02,
        step_granularity_y: float = 0.02,
        show_plot: bool = True,
) -> Tuple[mpl.figure.Figure, mpl.axes.Axes]:
    device = control_points.device
    xs = th.arange(0, 1, step_granularity_x, device=device)
    ys = th.arange(0, 1, step_granularity_y, device=device)
    xs = th.hstack([xs, th.tensor(1, device=device)])
    ys = th.hstack([ys, th.tensor(1, device=device)])

    eval_points = th.cartesian_prod(xs, ys)
    res = evaluate_nurbs_surface_flex(
        eval_points[:, 0],
        eval_points[:, 1],
        degree_x,
        degree_y,
        control_points,
        control_point_weights,
        knots_x,
        knots_y,
    )
    res = res.reshape((len(xs), len(ys)) + res.shape[1:])

    fig, ax = plt.subplots(subplot_kw={'projection': '3d'})
    ax.scatter(
        control_points[:, :, 0].detach().cpu().numpy(),
        control_points[:, :, 1].detach().cpu().numpy(),
        control_points[:, :, 2].detach().cpu().numpy(),
        color='black',
        alpha=0.3,
        label='control_points',
    )
    ax.plot_wireframe(
        control_points[:, :, 0].detach().cpu().numpy(),
        control_points[:, :, 1].detach().cpu().numpy(),
        control_points[:, :, 2].detach().cpu().numpy(),
        color='black',
        alpha=0.3,
    )
    ax.plot_surface(
        res[:, :, 0].detach().cpu().numpy(),
        res[:, :, 1].detach().cpu().numpy(),
        res[:, :, 2].detach().cpu().numpy(),
        cmap='plasma',
        alpha=0.8,
    )
    if show_plot:
        plt.show()
    return fig, ax


def plot_surface_derivs(
        degree_x: int,
        degree_y: int,
        control_points: torch.Tensor,
        control_point_weights: torch.Tensor,
        knots_x: torch.Tensor,
        knots_y: torch.Tensor,
        step_granularity_x: float = 0.02,
        step_granularity_y: float = 0.02,
        nth_deriv: int = 1,
        show_plot: bool = True,
        plot_normals: Optional[bool] = None,
) -> Tuple[mpl.figure.Figure, mpl.axes.Axes]:
    if plot_normals is None:
        plot_normals = nth_deriv == 1
    device = control_points.device
    xs = th.arange(0, 1, step_granularity_x, device=device)
    ys = th.arange(0, 1, step_granularity_y, device=device)
    xs = th.hstack([xs, th.tensor(1, device=device)])
    ys = th.hstack([ys, th.tensor(1, device=device)])

    eval_points = th.cartesian_prod(xs, ys)
    res = calc_derivs_surface(
        eval_points[:, 0],
        eval_points[:, 1],
        degree_x,
        degree_y,
        control_points,
        control_point_weights,
        knots_x,
        knots_y,
        nth_deriv,
    )
    res = res.reshape((len(xs), len(ys)) + res.shape[1:])
    if plot_normals:
        normals = calc_normals_surface(
            eval_points[:, 0],
            eval_points[:, 1],
            degree_x,
            degree_y,
            control_points,
            control_point_weights,
            knots_x,
            knots_y,
        )
        normals = normals.reshape((len(xs), len(ys)) + normals.shape[1:])

    fig, ax = plt.subplots(subplot_kw={'projection': '3d'})
    ax.scatter(
        control_points[:, :, 0].detach().cpu().numpy(),
        control_points[:, :, 1].detach().cpu().numpy(),
        control_points[:, :, 2].detach().cpu().numpy(),
        color='black',
        alpha=0.1,
        label='control_points',
    )
    ax.plot_wireframe(
        control_points[:, :, 0].detach().cpu().numpy(),
        control_points[:, :, 1].detach().cpu().numpy(),
        control_points[:, :, 2].detach().cpu().numpy(),
        color='black',
        alpha=0.1,
    )
    ax.plot_surface(
        res[:, :, 0, 0, 0].detach().cpu().numpy(),
        res[:, :, 0, 0, 1].detach().cpu().numpy(),
        res[:, :, 0, 0, 2].detach().cpu().numpy(),
        cmap='plasma',
        alpha=0.3,
    )
    ax.quiver(
        res[:, :, 0, 0, 0].detach().cpu().numpy(),
        res[:, :, 0, 0, 1].detach().cpu().numpy(),
        res[:, :, 0, 0, 2].detach().cpu().numpy(),
        res[:, :, 1, 0, 0].detach().cpu().numpy(),
        res[:, :, 1, 0, 1].detach().cpu().numpy(),
        res[:, :, 1, 0, 2].detach().cpu().numpy(),
        length=0.05,
        alpha=0.8,
        label='dS/dx',
    )
    ax.quiver(
        res[:, :, 0, 0, 0].detach().cpu().numpy(),
        res[:, :, 0, 0, 1].detach().cpu().numpy(),
        res[:, :, 0, 0, 2].detach().cpu().numpy(),
        res[:, :, 0, 1, 0].detach().cpu().numpy(),
        res[:, :, 0, 1, 1].detach().cpu().numpy(),
        res[:, :, 0, 1, 2].detach().cpu().numpy(),
        length=0.05,
        color='red',
        alpha=0.8,
        label='dS/dy',
    )
    if plot_normals:
        ax.quiver(
            res[:, :, 0, 0, 0].detach().cpu().numpy(),
            res[:, :, 0, 0, 1].detach().cpu().numpy(),
            res[:, :, 0, 0, 2].detach().cpu().numpy(),
            normals[:, :, 0].detach().cpu().numpy(),
            normals[:, :, 1].detach().cpu().numpy(),
            normals[:, :, 2].detach().cpu().numpy(),
            length=0.05,
            color='green',
            alpha=0.8,
            label='normals',
        )
    ax.legend()
    if show_plot:
        plt.show()
    return fig, ax


def plot_surface_derivs_slow(
        degree_x: int,
        degree_y: int,
        control_points: torch.Tensor,
        control_point_weights: torch.Tensor,
        knots_x: torch.Tensor,
        knots_y: torch.Tensor,
        step_granularity_x: float = 0.02,
        step_granularity_y: float = 0.02,
        nth_deriv: int = 1,
        show_plot: bool = True,
        plot_normals: Optional[bool] = None,
) -> Tuple[mpl.figure.Figure, mpl.axes.Axes]:
    if plot_normals is None:
        plot_normals = nth_deriv == 1
    device = control_points.device
    xs = th.arange(0, 1, step_granularity_x, device=device)
    ys = th.arange(0, 1, step_granularity_y, device=device)
    xs = th.hstack([xs, th.tensor(1, device=device)])
    ys = th.hstack([ys, th.tensor(1, device=device)])

    eval_points = th.cartesian_prod(xs, ys)
    res = calc_derivs_surface_slow(
        eval_points[:, 0],
        eval_points[:, 1],
        degree_x,
        degree_y,
        control_points,
        control_point_weights,
        knots_x,
        knots_y,
        nth_deriv,
    )
    if plot_normals:
        normals = calc_normals_surface_slow(
            eval_points[:, 0],
            eval_points[:, 1],
            degree_x,
            degree_y,
            control_points,
            control_point_weights,
            knots_x,
            knots_y,
        )
        normals = normals.reshape((len(xs), len(ys)) + normals.shape[1:])

    fig, ax = plt.subplots(subplot_kw={'projection': '3d'})
    ax.scatter(
        control_points[:, :, 0].detach().cpu().numpy(),
        control_points[:, :, 1].detach().cpu().numpy(),
        control_points[:, :, 2].detach().cpu().numpy(),
        color='black',
        alpha=0.1,
        label='control_points',
    )
    ax.plot_wireframe(
        control_points[:, :, 0].detach().cpu().numpy(),
        control_points[:, :, 1].detach().cpu().numpy(),
        control_points[:, :, 2].detach().cpu().numpy(),
        color='black',
        alpha=0.1,
    )
    ax.plot_surface(
        res[0][0][:, 0].detach().cpu().numpy(),
        res[0][0][:, 1].detach().cpu().numpy(),
        res[0][0][:, 2].detach().cpu().numpy(),
        cmap='plasma',
        alpha=0.3,
    )
    ax.quiver(
        res[0][0][:, 0].detach().cpu().numpy(),
        res[0][0][:, 1].detach().cpu().numpy(),
        res[0][0][:, 2].detach().cpu().numpy(),
        res[1][0][:, 0].detach().cpu().numpy(),
        res[1][0][:, 1].detach().cpu().numpy(),
        res[1][0][:, 2].detach().cpu().numpy(),
        length=0.05,
        alpha=0.8,
        label='dS/dx',
    )
    ax.quiver(
        res[0][0][:, 0].detach().cpu().numpy(),
        res[0][0][:, 1].detach().cpu().numpy(),
        res[0][0][:, 2].detach().cpu().numpy(),
        res[0][1][:, 0].detach().cpu().numpy(),
        res[0][1][:, 1].detach().cpu().numpy(),
        res[0][1][:, 2].detach().cpu().numpy(),
        length=0.05,
        color='red',
        alpha=0.8,
        label='dS/dy',
    )
    if plot_normals:
        ax.quiver(
            res[0][0][:, 0].detach().cpu().numpy(),
            res[0][0][:, 1].detach().cpu().numpy(),
            res[0][0][:, 2].detach().cpu().numpy(),
            normals[:, :, 0].detach().cpu().numpy(),
            normals[:, :, 1].detach().cpu().numpy(),
            normals[:, :, 2].detach().cpu().numpy(),
            length=0.05,
            color='green',
            alpha=0.8,
            label='normals',
        )
    ax.legend()
    if show_plot:
        plt.show()
    return fig, ax


def plot_surface_normals(
        degree_x: int,
        degree_y: int,
        control_points: torch.Tensor,
        control_point_weights: torch.Tensor,
        knots_x: torch.Tensor,
        knots_y: torch.Tensor,
        step_granularity_x: float = 0.02,
        step_granularity_y: float = 0.02,
        show_plot: bool = True,
) -> Tuple[mpl.figure.Figure, mpl.axes.Axes]:
    device = control_points.device
    xs = th.arange(0, 1, step_granularity_x, device=device)
    ys = th.arange(0, 1, step_granularity_y, device=device)
    xs = th.hstack([xs, th.tensor(1, device=device)])
    ys = th.hstack([ys, th.tensor(1, device=device)])

    eval_points = th.cartesian_prod(xs, ys)
    res = evaluate_nurbs_surface_flex(
        eval_points[:, 0],
        eval_points[:, 1],
        degree_x,
        degree_y,
        control_points,
        control_point_weights,
        knots_x,
        knots_y,
    )
    res = res.reshape((len(xs), len(ys)) + res.shape[1:])
    normals = calc_normals_surface(
        eval_points[:, 0],
        eval_points[:, 1],
        degree_x,
        degree_y,
        control_points,
        control_point_weights,
        knots_x,
        knots_y,
    )
    normals = normals.reshape((len(xs), len(ys)) + normals.shape[1:])

    fig, ax = plt.subplots(subplot_kw={'projection': '3d'})
    ax.scatter(
        control_points[:, :, 0].detach().cpu().numpy(),
        control_points[:, :, 1].detach().cpu().numpy(),
        control_points[:, :, 2].detach().cpu().numpy(),
        color='black',
        alpha=0.1,
        label='control_points',
    )
    ax.plot_wireframe(
        control_points[:, :, 0].detach().cpu().numpy(),
        control_points[:, :, 1].detach().cpu().numpy(),
        control_points[:, :, 2].detach().cpu().numpy(),
        color='black',
        alpha=0.1,
    )
    ax.plot_surface(
        res[:, :, 0].detach().cpu().numpy(),
        res[:, :, 1].detach().cpu().numpy(),
        res[:, :, 2].detach().cpu().numpy(),
        cmap='plasma',
        alpha=0.3,
    )
    ax.quiver(
        res[:, :, 0].detach().cpu().numpy(),
        res[:, :, 1].detach().cpu().numpy(),
        res[:, :, 2].detach().cpu().numpy(),
        normals[:, :, 0].detach().cpu().numpy(),
        normals[:, :, 1].detach().cpu().numpy(),
        normals[:, :, 2].detach().cpu().numpy(),
        length=0.05,
        color='green',
        alpha=0.8,
        label='normals',
    )
    ax.legend()
    if show_plot:
        plt.show()
    return fig, ax


def plot_surface_normals_slow(
        degree_x: int,
        degree_y: int,
        control_points: torch.Tensor,
        control_point_weights: torch.Tensor,
        knots_x: torch.Tensor,
        knots_y: torch.Tensor,
        step_granularity_x: float = 0.02,
        step_granularity_y: float = 0.02,
        show_plot: bool = True,
) -> Tuple[mpl.figure.Figure, mpl.axes.Axes]:
    device = control_points.device
    xs = th.arange(0, 1, step_granularity_x, device=device)
    ys = th.arange(0, 1, step_granularity_y, device=device)
    xs = th.hstack([xs, th.tensor(1, device=device)])
    ys = th.hstack([ys, th.tensor(1, device=device)])

    eval_points = th.cartesian_prod(xs, ys)
    res, normals = calc_normals_and_surface_slow(
        eval_points[:, 0],
        eval_points[:, 1],
        degree_x,
        degree_y,
        control_points,
        control_point_weights,
        knots_x,
        knots_y,
    )
    res = res.reshape((len(xs), len(ys)) + res.shape[1:])
    normals = normals.reshape((len(xs), len(ys)) + normals.shape[1:])

    fig, ax = plt.subplots(subplot_kw={'projection': '3d'})
    ax.scatter(
        control_points[:, :, 0].detach().cpu().numpy(),
        control_points[:, :, 1].detach().cpu().numpy(),
        control_points[:, :, 2].detach().cpu().numpy(),
        color='black',
        alpha=0.1,
        label='control_points',
    )
    ax.plot_wireframe(
        control_points[:, :, 0].detach().cpu().numpy(),
        control_points[:, :, 1].detach().cpu().numpy(),
        control_points[:, :, 2].detach().cpu().numpy(),
        color='black',
        alpha=0.1,
    )
    ax.plot_surface(
        res[:, :, 0].detach().cpu().numpy(),
        res[:, :, 1].detach().cpu().numpy(),
        res[:, :, 2].detach().cpu().numpy(),
        cmap='plasma',
        alpha=0.3,
    )
    ax.quiver(
        res[:, :, 0].detach().cpu().numpy(),
        res[:, :, 1].detach().cpu().numpy(),
        res[:, :, 2].detach().cpu().numpy(),
        normals[:, :, 0].detach().cpu().numpy(),
        normals[:, :, 1].detach().cpu().numpy(),
        normals[:, :, 2].detach().cpu().numpy(),
        length=0.05,
        color='green',
        alpha=0.8,
        label='normals',
    )
    ax.legend()
    if show_plot:
        plt.show()
    return fig, ax


def get_inversion_start_values(
        world_points: torch.Tensor,
        degree_x: int,
        degree_y: int,
        control_points: torch.Tensor,
        control_point_weights: torch.Tensor,
        knots_x: torch.Tensor,
        knots_y: torch.Tensor,
        num_samples: int,
        norm_p: int = 2,
) -> Tuple[torch.Tensor, torch.Tensor]:
    """Return values in `world_points` and their distance; the values
    chosen minimize the distance to the given NURBS surface.

    The values are used as start values for Newton iterations for point
    inversion.
    """
    device = control_points.device

    start_spans_x = th.arange(degree_x, len(knots_x) - degree_x, device=device)
    start_spans_y = th.arange(degree_y, len(knots_y) - degree_y, device=device)

    evaluation_points_x = th.hstack([
        th.linspace(
            knots_x[span_x],  # type: ignore[arg-type]
            knots_x[span_x + 1],  # type: ignore[arg-type]
            num_samples,
            device=device,
        )[:-1]
        for span_x in start_spans_x[:-1]
    ] + [knots_x[start_spans_x[-1]]])
    evaluation_points_y = th.hstack([
        th.linspace(
            knots_y[span_y],  # type: ignore[arg-type]
            knots_y[span_y + 1],  # type: ignore[arg-type]
            num_samples,
            device=device,
        )[:-1]
        for span_y in start_spans_y[:-1]
    ] + [knots_y[start_spans_y[-1]]])
    evaluation_points = th.cartesian_prod(
        evaluation_points_x, evaluation_points_y)
    del start_spans_x
    del start_spans_y
    del evaluation_points_x
    del evaluation_points_y

    surface_points = evaluate_nurbs_surface_flex(
        evaluation_points[:, 0],
        evaluation_points[:, 1],
        degree_x,
        degree_y,
        control_points,
        control_point_weights,
        knots_x,
        knots_y,
    )

    distances = th.linalg.norm(
        surface_points.unsqueeze(0) - world_points.unsqueeze(1),
        ord=norm_p,
        dim=-1,
    )
    min_distances, argmin_distances = distances.min(1)
    return evaluation_points[argmin_distances], min_distances


def batch_dot(x: torch.Tensor, y: torch.Tensor) -> torch.Tensor:
    """Return a dot product over the batch dimensions of tensors `x` and
    `y`.
    """
    return (x * y).sum(-1).unsqueeze(-1)


def invert_points(
        world_points: torch.Tensor,
        degree_x: int,
        degree_y: int,
        control_points: torch.Tensor,
        control_point_weights: torch.Tensor,
        knots_x: torch.Tensor,
        knots_y: torch.Tensor,
        num_samples: int = 8,
        norm_p: int = 2,
        max_iters: int = 100,
        distance_tolerance: float = 1e-5,
        cosine_tolerance: float = 1e-7,
) -> Tuple[torch.Tensor, torch.Tensor]:
    """Return evaluation points and their evaluated distances to
    `world_points` for the given NURBS surface. The returned evaluation
    points are calculated so that `world_points` are fitted to the
    desired error tolerances.
    """
    argmin_distances, min_distances = get_inversion_start_values(
        world_points,
        degree_x,
        degree_y,
        control_points,
        control_point_weights,
        knots_x,
        knots_y,
        num_samples,
        norm_p=norm_p,
    )

    # TODO We should handle differing x and y limits here.
    point_min = 0
    point_max = 1

    derivs = calc_derivs_surface(
        argmin_distances[:, 0],
        argmin_distances[:, 1],
        degree_x,
        degree_y,
        control_points,
        control_point_weights,
        knots_x,
        knots_y,
        nth_deriv=2,
    )

    surface_points = derivs[:, 0, 0]

    point_difference = surface_points - world_points
    no_change_indices = th.zeros_like(min_distances, dtype=th.bool)

    for i in range(max_iters):
        Su = derivs[:, 1, 0]
        Sv = derivs[:, 0, 1]

        points_coincide_indices = min_distances <= distance_tolerance
        no_change_indices = no_change_indices | points_coincide_indices

        zero_cosine_indices = (
            (
                th.linalg.norm(
                    batch_dot(Su, point_difference),
                    ord=norm_p,
                    dim=-1,
                )
                / (th.linalg.norm(Su, ord=norm_p, dim=-1) * min_distances)
            ) <= cosine_tolerance
        ) & (
            (
                th.linalg.norm(
                    batch_dot(Sv, point_difference),
                    ord=norm_p,
                    dim=-1,
                )
                / (th.linalg.norm(Sv, ord=norm_p, dim=-1) * min_distances)
            ) <= cosine_tolerance
        )
        no_change_indices = no_change_indices | zero_cosine_indices

        if points_coincide_indices.all() and zero_cosine_indices.all():
            break

        both_dir_dot = (
            batch_dot(Su, Sv)
            + batch_dot(point_difference, derivs[:, 1, 1])
        )

        J = th.stack([
            th.hstack([
                (
                    th.linalg.norm(Su, ord=norm_p, dim=-1).pow(2).unsqueeze(-1)
                    + batch_dot(point_difference, derivs[:, 2, 0])
                ),
                both_dir_dot,
            ]),
            th.hstack([
                both_dir_dot,
                (
                    th.linalg.norm(Sv, ord=norm_p, dim=-1).pow(2).unsqueeze(-1)
                    + batch_dot(point_difference, derivs[:, 0, 2])
                ),
            ]),
        ], dim=1)
        kappa = -th.hstack([
            batch_dot(point_difference, Su),
            batch_dot(point_difference, Sv),
        ])

        delta = th.linalg.solve(J, kappa)

        prev_argmin_distances = argmin_distances.clone()
        change_indices = ~no_change_indices
        argmin_distances[change_indices] = (
            delta[change_indices]
            + prev_argmin_distances[change_indices]
        )

        argmin_distances[change_indices] = \
            argmin_distances[change_indices].clamp(point_min, point_max)

        # TODO We always assume non-closed surfaces.
        # argmin_distance_x = argmin_distance[:, 0]
        # argmin_distance_y = argmin_distance[:, 1]

        # argmin_distance_x = argmin_distance_x.clamp(point_min, point_max)
        # argmin_distance_y = argmin_distance_y.clamp(point_min, point_max)

        # argmin_distance = th.stack([
        #     argmin_distance_x,
        #     argmin_distance_y,
        # ], dim=-1)

        derivs = calc_derivs_surface(
            argmin_distances[:, 0],
            argmin_distances[:, 1],
            degree_x,
            degree_y,
            control_points,
            control_point_weights,
            knots_x,
            knots_y,
            nth_deriv=2,
        )

        surface_points = derivs[:, 0, 0]

        point_difference = surface_points - world_points
        min_distances = th.linalg.norm(
            point_difference,
            ord=norm_p,
            dim=-1,
        )

        insignificant_change_indices = th.linalg.norm(
            (
                (
                    argmin_distances[:, 0]
                    - prev_argmin_distances[:, 0]
                ).unsqueeze(-1) * Su
                + (
                    argmin_distances[:, 1]
                    - prev_argmin_distances[:, 1]
                ).unsqueeze(-1) * Sv
            ),
            ord=norm_p,
            dim=-1,
        ) <= distance_tolerance
        no_change_indices = no_change_indices | insignificant_change_indices
        if no_change_indices.all():
            break
    else:
        # We recalculate here in order to handle `max_iters == 0`.
        change_indices = ~no_change_indices
        num_unconverged = change_indices.count_nonzero()
        raise NoConvergenceError(
            f'convergence failed for {num_unconverged} points; '
            f'try to increase `num_samples`, `max_iters`, '
            f'`distance_tolerance`, or `cosine_tolerance`'
        )
    return argmin_distances, min_distances


def invert_points_slow(
        world_points: torch.Tensor,
        degree_x: int,
        degree_y: int,
        control_points: torch.Tensor,
        control_point_weights: torch.Tensor,
        knots_x: torch.Tensor,
        knots_y: torch.Tensor,
        num_samples: int = 8,
        norm_p: int = 2,
        max_iters: int = 100,
        distance_tolerance: float = 1e-5,
        cosine_tolerance: float = 1e-7,
) -> Tuple[torch.Tensor, torch.Tensor]:
    """Return evaluation points and their evaluated distances to
    `world_points` for the given NURBS surface. The returned evaluation
    points are calculated so that `world_points` are fitted to the
    desired error tolerances.

    This function is slightly slower than `invert_points` but fully
    differentiable.
    """
    argmin_distances, min_distances = get_inversion_start_values(
        world_points,
        degree_x,
        degree_y,
        control_points,
        control_point_weights,
        knots_x,
        knots_y,
        num_samples,
        norm_p=norm_p,
    )

    # TODO We should handle differing x and y limits here.
    point_min = 0
    point_max = 1

    derivs = calc_derivs_surface_slow(
        argmin_distances[:, 0],
        argmin_distances[:, 1],
        degree_x,
        degree_y,
        control_points,
        control_point_weights,
        knots_x,
        knots_y,
        nth_deriv=2,
    )

    surface_points = derivs[0][0]

    point_difference = surface_points - world_points
    no_change_indices = th.zeros_like(min_distances, dtype=th.bool)

    for i in range(max_iters):
        Su = derivs[1][0]
        Sv = derivs[0][1]

        points_coincide_indices = min_distances <= distance_tolerance
        no_change_indices = no_change_indices | points_coincide_indices

        zero_cosine_indices = (
            (
                th.linalg.norm(
                    batch_dot(Su, point_difference),
                    ord=norm_p,
                    dim=-1,
                )
                / (th.linalg.norm(Su, ord=norm_p, dim=-1) * min_distances)
            ) <= cosine_tolerance
        ) & (
            (
                th.linalg.norm(
                    batch_dot(Sv, point_difference),
                    ord=norm_p,
                    dim=-1,
                )
                / (th.linalg.norm(Sv, ord=norm_p, dim=-1) * min_distances)
            ) <= cosine_tolerance
        )
        no_change_indices = no_change_indices | zero_cosine_indices

        if points_coincide_indices.all() and zero_cosine_indices.all():
            break

        both_dir_dot = (
            batch_dot(Su, Sv)
            + batch_dot(point_difference, derivs[1][1])
        )

        J = th.stack([
            th.hstack([
                (
                    th.linalg.norm(Su, ord=norm_p, dim=-1).pow(2).unsqueeze(-1)
                    + batch_dot(point_difference, derivs[2][0])
                ),
                both_dir_dot,
            ]),
            th.hstack([
                both_dir_dot,
                (
                    th.linalg.norm(Sv, ord=norm_p, dim=-1).pow(2).unsqueeze(-1)
                    + batch_dot(point_difference, derivs[0][2])
                ),
            ]),
        ], dim=1)
        kappa = -th.hstack([
            batch_dot(point_difference, Su),
            batch_dot(point_difference, Sv),
        ])

        delta = th.linalg.solve(J, kappa)

        prev_argmin_distances = argmin_distances.clone()
        change_indices = ~no_change_indices
        argmin_distances[change_indices] = (
            delta[change_indices]
            + prev_argmin_distances[change_indices]
        )

        argmin_distances[change_indices] = \
            argmin_distances[change_indices].clamp(point_min, point_max)

        # TODO We always assume non-closed surfaces.
        # argmin_distance_x = argmin_distance[:, 0]
        # argmin_distance_y = argmin_distance[:, 1]

        # argmin_distance_x = argmin_distance_x.clamp(point_min, point_max)
        # argmin_distance_y = argmin_distance_y.clamp(point_min, point_max)

        # argmin_distance = th.stack([
        #     argmin_distance_x,
        #     argmin_distance_y,
        # ], dim=-1)

        derivs = calc_derivs_surface_slow(
            argmin_distances[:, 0],
            argmin_distances[:, 1],
            degree_x,
            degree_y,
            control_points,
            control_point_weights,
            knots_x,
            knots_y,
            nth_deriv=2,
        )

        surface_points = derivs[0][0]

        point_difference = surface_points - world_points
        min_distances = th.linalg.norm(
            point_difference,
            ord=norm_p,
            dim=-1,
        )

        insignificant_change_indices = th.linalg.norm(
            (
                (
                    argmin_distances[:, 0]
                    - prev_argmin_distances[:, 0]
                ).unsqueeze(-1) * Su
                + (
                    argmin_distances[:, 1]
                    - prev_argmin_distances[:, 1]
                ).unsqueeze(-1) * Sv
            ),
            ord=norm_p,
            dim=-1,
        ) <= distance_tolerance
        no_change_indices = no_change_indices | insignificant_change_indices
        if no_change_indices.all():
            break
    else:
        # We recalculate here in order to handle `max_iters == 0`.
        change_indices = ~no_change_indices
        num_unconverged = change_indices.count_nonzero()
        raise NoConvergenceError(
            f'convergence failed for {num_unconverged} points; '
            f'try to increase `num_samples`, `max_iters`, '
            f'`distance_tolerance`, or `cosine_tolerance`'
        )
    return argmin_distances, min_distances


def get_mesh_params_(
        world_points: torch.Tensor,
        num_points: int,
        num_other_points: int,
        in_row_dir: bool,
) -> torch.Tensor:
    dtype = world_points.dtype
    device = world_points.device

    if in_row_dir:
        if world_points.ndim == 3:
            def wp(row: int, col: int) -> torch.Tensor:
                return world_points[row, col]
        else:
            def wp(row: int, col: int) -> torch.Tensor:
                return world_points[row * (num_other_points + 1) + col]
    else:
        if world_points.ndim == 3:
            def wp(row: int, col: int) -> torch.Tensor:
                return world_points[col, row]
        else:
            def wp(row: int, col: int) -> torch.Tensor:
                return world_points[row + col * (num_points + 1)]

    num_nondegenerate = num_other_points + 1
    params = th.zeros(num_points + 1, dtype=dtype, device=device)
    params[-1] = 1
    cds = th.empty(
        num_points + 1,
        dtype=dtype,
        device=device,
    )

    for m in range(num_other_points + 1):
        total = th.tensor(0, dtype=dtype, device=device)
        for k in range(1, num_points + 1):
            # chordal distances
            cds[k - 1] = th.linalg.norm(wp(k, m) - wp(k - 1, m))
            total += cds[k - 1]
        if total == 0:
            num_nondegenerate -= 1
        else:
            d = th.tensor(0, dtype=dtype, device=device)
            for k in range(1, num_points):
                d += cds[k - 1]
                params[k] += d / total

    if num_nondegenerate == 0:
        raise ValueError()
    params[1:num_points] /= num_nondegenerate
    return params


def get_mesh_params(
        world_points: torch.Tensor,
        num_points_x: int,
        num_points_y: int,
) -> Tuple[torch.Tensor, torch.Tensor]:
    assert len(world_points) == (num_points_x + 1) * (num_points_y + 1)
    params_x = get_mesh_params_(world_points, num_points_x, num_points_y, True)
    params_y = get_mesh_params_(
        world_points, num_points_y, num_points_x, False)
    return params_x, params_y


def place_knots(
        params: torch.Tensor,
        num_control_points: int,
        degree: int,
) -> torch.Tensor:
    # m = num_points
    # n = num_control_points
    num_points = len(params) - 1

    device = params.device
    knots = th.empty((num_control_points + degree + 2,), device=device)
    knots[:degree + 1] = 0
    knots[-degree - 1:] = 1

    d = (num_points + 1) / (num_control_points - degree + 1)
    js = th.arange(1, num_control_points - degree + 1, device=device)
    jds = js * d
    ks = jds.long()
    alphas = jds - ks
    knots[degree + js] = (1 - alphas) * params[ks - 1] + alphas * params[ks]

    return knots


def calc_basis_mat(
        params: torch.Tensor,
        num_control_points: int,
        degree: int,
        knots: torch.Tensor,
) -> torch.Tensor:
    # m = num_points
    # n = num_control_points
    num_points = len(params) - 1

    device = params.device
    selected_params = params[1:num_points]
    N = th.stack([
        get_single_basis(
            selected_params,
            th.full((len(selected_params),), i, device=device),
            degree,
            knots,
        )  # TODO .to_sparse()
        for i in range(1, num_control_points)
    ], -1)

    assert N.shape == (num_points - 1, num_control_points - 1), f'{N.shape}'
    return N


def calc_basisTbasis(N: torch.Tensor) -> torch.Tensor:
    return th.matmul(N.T, N)


def calc_R(
        world_points: torch.Tensor,
        col: int,
        params: torch.Tensor,
        num_other_points: int,
        N: torch.Tensor,
        degree: int,
        num_control_points: int,
        knots: torch.Tensor,
        in_row_dir: bool,
) -> torch.Tensor:
    num_points = len(params) - 1

    device = world_points.device

    if in_row_dir:
        if world_points.ndim == 3:
            def wp(row: Union[torch.Tensor, int]) -> torch.Tensor:
                return world_points[row, col]
        else:
            def wp(row: Union[torch.Tensor, int]) -> torch.Tensor:
                return world_points[row * (num_other_points + 1) + col]
    else:
        if world_points.ndim == 3:
            def wp(row: Union[torch.Tensor, int]) -> torch.Tensor:
                return world_points[col, row]
        else:
            def wp(row: Union[torch.Tensor, int]) -> torch.Tensor:
                return world_points[row + col * (num_points + 1)]

    selected_params = params[1:num_points]
    Rk = (
        wp(th.arange(1, num_points))
        - (
            get_single_basis(
                selected_params,
                th.tensor([0], device=device),
                degree,
                knots,
            ).unsqueeze(-1)
            * wp(0).unsqueeze(0)
        )
        - (
            get_single_basis(
                selected_params,
                th.tensor([num_control_points], device=device),
                degree,
                knots,
            ).unsqueeze(-1)
            * wp(num_points).unsqueeze(0)
        )
    )
    R = th.matmul(N.T, Rk)
    return R


def approximate_surface(
        world_points: torch.Tensor,
        num_points_x: int,
        num_points_y: int,
        degree_x: int,
        degree_y: int,
        num_control_points_x: int,
        num_control_points_y: int,
        knots_x: Optional[torch.Tensor] = None,
        knots_y: Optional[torch.Tensor] = None,
) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    # TODO Allow other direction first.
    # r = num_points_x
    # s = num_points_y
    # Q = world points
    # p = degree_x
    # q = degree_y
    # n = num_control_points_x
    # m = num_control_points_y
    assert len(world_points) == num_points_x * num_points_y

    # Algorithms assumes for example `num_points + 1` points, so
    # subtract one.
    num_points_x -= 1
    num_points_y -= 1
    num_control_points_x -= 1
    num_control_points_y -= 1

    device = world_points.device

    params_x = get_mesh_params_(world_points, num_points_x, num_points_y, True)
    if knots_x is None:
        knots_x = place_knots(params_x, num_control_points_x, degree_x)

    params_y = get_mesh_params_(
        world_points, num_points_y, num_points_x, False)
    if knots_y is None:
        knots_y = place_knots(params_y, num_control_points_y, degree_y)

    Nu = calc_basis_mat(params_x, num_control_points_x, degree_x, knots_x)
    assert Nu.shape == (num_points_x - 1, num_control_points_x - 1)
    NTNu = calc_basisTbasis(Nu)
    assert NTNu.shape == (num_control_points_x - 1, num_control_points_x - 1)
    NTNu_LU, NTNu_pivots = lu_factor(NTNu)

    tmp = th.empty(
        (num_control_points_x + 1, num_points_y + 1, world_points.shape[-1]),
        device=device,
    )
    for j in range(num_points_y + 1):
        tmp[0, j] = world_points[0 * (num_points_y + 1) + j]
        tmp[num_control_points_x, j] = \
            world_points[num_points_x * (num_points_y + 1) + j]
        Ru = calc_R(
            world_points,
            j,
            params_x,
            num_points_y,
            Nu,
            degree_x,
            num_control_points_x,
            knots_x,
            True,
        )
        assert Ru.shape == (num_control_points_x - 1, 3), f'{Ru.shape}'
        tmp[1:num_control_points_x, j] = lu_solve(Ru, NTNu_LU, NTNu_pivots)

    Nv = calc_basis_mat(params_y, num_control_points_y, degree_y, knots_y)
    assert Nv.shape == (num_points_y - 1, num_control_points_y - 1)
    NTNv = calc_basisTbasis(Nv)
    assert NTNv.shape == (num_control_points_y - 1, num_control_points_y - 1)
    NTNv_LU, NTNv_pivots = lu_factor(NTNv)

    P = th.empty(
        (
            num_control_points_x + 1,
            num_control_points_y + 1,
            world_points.shape[-1],
        ),
        device=device,
    )
    for i in range(num_control_points_x + 1):
        P[i, 0] = tmp[i, 0]
        P[i, num_control_points_y] = tmp[i, num_points_y]
        Rv = calc_R(
            tmp,
            i,
            params_y,
            num_points_x,
            Nv,
            degree_y,
            num_control_points_y,
            knots_y,
            False,
        )
        assert Rv.shape == (num_control_points_y - 1, 3)
        P[i, 1:num_control_points_y] = lu_solve(Rv, NTNv_LU, NTNv_pivots)
    return P, knots_x, knots_y


class NURBSSurface:
    def __init__(
            self,
            degree_x: int,
            degree_y: int,
            control_points: torch.Tensor,
            control_point_weights: torch.Tensor,
            knots_x: torch.Tensor,
            knots_y: torch.Tensor,
    ) -> None:
        self.degree_x = degree_x
        self.degree_y = degree_y
        self.control_points = control_points
        self.control_point_weights = control_point_weights
        self.knots_x = knots_x
        self.knots_y = knots_y

    @classmethod
    def create_empty(
            cls: Type[S],
            degree_x: int,
            degree_y: int,
            num_control_points_x: int,
            num_control_points_y: int,
            device: th.device,
    ) -> S:
        (
            control_points,
            control_point_weights,
            knots_x,
            knots_y,
        ) = setup_nurbs_surface(
            degree_x,
            degree_y,
            num_control_points_x,
            num_control_points_y,
            device,
        )
        return cls(
            degree_x,
            degree_y,
            control_points,
            control_point_weights,
            knots_x,
            knots_y,
        )

    @classmethod
    def create_example(
            cls: Type[S],
            device: th.device = th.device('cpu'),
    ) -> S:
        degree = 3
        num_ctrl = 6
        surf = cls.create_empty(degree, degree, num_ctrl, num_ctrl, device)

        y_inds, x_inds = th.meshgrid(
            th.linspace(0, 1, num_ctrl),
            th.linspace(0, 1, num_ctrl),
            indexing='ij',
        )

        surf.control_points[:, :, 0] = y_inds
        surf.control_points[:, :, 1] = x_inds

        first_circle_height = 1/3
        surf.control_points[0, :, 2] = first_circle_height
        surf.control_points[:, 0, 2] = first_circle_height
        surf.control_points[-1, :, 2] = first_circle_height
        surf.control_points[:, -1, 2] = first_circle_height

        second_circle_height = 0
        surf.control_points[1, 1:-1, 2] = second_circle_height
        surf.control_points[1:-1, 1, 2] = second_circle_height
        surf.control_points[-2, 1:-1, 2] = second_circle_height
        surf.control_points[1:-1, -2, 2] = second_circle_height

        third_circle_height = 1
        surf.control_points[2, 2:-2, 2] = third_circle_height
        surf.control_points[2:-2, 2, 2] = third_circle_height
        surf.control_points[-3, 2:-2, 2] = third_circle_height
        surf.control_points[2:-2, -3, 2] = third_circle_height

        surf.control_point_weights[:] = 1

        surf.knots_x[degree:-degree] = th.linspace(
            0, 1, len(surf.knots_x[degree:-degree]))
        surf.knots_y[degree:-degree] = th.linspace(
            0, 1, len(surf.knots_y[degree:-degree]))
        # surf.knots_x[:] = 1
        # surf.knots_y[:] = 1
        # surf.knots_x[degree + 1] = 0
        # surf.knots_x[-degree - 2] = 1
        # surf.knots_y[degree + 1] = 0
        # surf.knots_y[-degree - 2] = 1
        return surf

    def evaluate(
            self,
            evaluation_point_x: torch.Tensor,
            evaluation_point_y: torch.Tensor,
    ) -> torch.Tensor:
        return evaluate_nurbs_surface_flex(
            evaluation_point_x,
            evaluation_point_y,
            self.degree_x,
            self.degree_y,
            self.control_points,
            self.control_point_weights,
            self.knots_x,
            self.knots_y,
        )

    def calc_bspline_derivs(
            self,
            evaluation_point_x: torch.Tensor,
            evaluation_point_y: torch.Tensor,
            nth_deriv: int = 1,
    ) -> torch.Tensor:
        return calc_bspline_derivs_surface(
                evaluation_point_x,
                evaluation_point_y,
                self.degree_x,
                self.degree_y,
                self.control_points,
                self.knots_x,
                self.knots_y,
                nth_deriv,
        )

    def calc_derivs(
            self,
            evaluation_point_x: torch.Tensor,
            evaluation_point_y: torch.Tensor,
            nth_deriv: int = 1,
    ) -> torch.Tensor:
        return calc_derivs_surface(
            evaluation_point_x,
            evaluation_point_y,
            self.degree_x,
            self.degree_y,
            self.control_points,
            self.control_point_weights,
            self.knots_x,
            self.knots_y,
            nth_deriv,
        )

    def plot(
            self,
            step_granularity_x: float = 0.02,
            step_granularity_y: float = 0.02,
            show_plot: bool = True,
    ) -> Tuple[mpl.figure.Figure, mpl.axes.Axes]:
        return plot_surface(
            self.degree_x,
            self.degree_y,
            self.control_points,
            self.control_point_weights,
            self.knots_x,
            self.knots_y,
            step_granularity_x,
            step_granularity_y,
            show_plot,
        )

    def plot_derivs(
            self,
            step_granularity_x: float = 0.1,
            step_granularity_y: float = 0.1,
            nth_deriv: int = 1,
            show_plot: bool = True,
            plot_normals: Optional[bool] = None,
    ) -> Tuple[mpl.figure.Figure, mpl.axes.Axes]:
        if plot_normals is None:
            plot_normals = nth_deriv == 1
        return plot_surface_derivs(
            self.degree_x,
            self.degree_y,
            self.control_points,
            self.control_point_weights,
            self.knots_x,
            self.knots_y,
            step_granularity_x,
            step_granularity_y,
            nth_deriv,
            show_plot,
            plot_normals,
        )

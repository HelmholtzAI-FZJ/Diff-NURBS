# Diff-NURBS

An automatically differentiable pure-Python NURBS implementation in
PyTorch.

## Usage

When you use the `setup_nurbs*` functions, the returned NURBS tensors
will not be initialized in any sensible default way; this is up to the
user. However, even the "uninitialized" values will not violate any
mathematical conditions. We provide here a high-level usage
description and then suggestions for initialization. Place the
`control_points` according to your use case, for example uniformly in
an N-dimensional grid.

### NURBS Surface

#### Object-oriented API

Here, we create a nice-to-look-at example surface and plot its shape
as well as its derivatives and normals:

```python
from diff_nurbs import NURBSSurface

nurbs_surface = NURBSSurface.create_example()
nurbs_surface.plot_derivs()
```

You should see something resembling a mountain surrounded by a valley.

#### Functional API

```python
import diff_nurbs

# Parameters
degree_x = 3
degree_y = 3
rows = 8
cols = 8

# Create relevant tensors
(
    control_points,
    control_point_weights,
    knots_x,
    knots_y,
) = diff_nurbs.setup_nurbs_surface(degree_x, degree_y, rows, cols)

# Initialize above tensors depending on use case.
# (Not shown here.)

# Evaluate on a uniform 10×10 grid.
eval_points_rows = th.linspace(0, 1, 10)
eval_points_columns = th.linspace(0, 1, 10)
eval_points = th.cartesian_prod(eval_points_rows, eval_points_cols)
eval_points_x = eval_points[:, 0]
eval_points_y = eval_points[:, 1]

surface_points, normals = diff_nurbs.calc_normals_and_surface_slow(
    eval_points_x,
    eval_points_y,
    degree_x,
    degree_y,
    control_points,
    control_point_weights,
    knots_x,
    knots_y,
)
```

#### Initialization suggestions

```python
# Place knots uniformly.
def place_uniformly(knots: torch.Tensor, spline_degree: int) -> None:
    num_knot_vals = len(knots[spline_degree:-spline_degree])
    knot_vals = th.linspace(0, 1, num_knot_vals)
    knots[:spline_degree] = 0
    knots[spline_degree:-spline_degree] = knot_vals
    knots[-spline_degree:] = 1


place_uniformly(knots_x, degree_x)
place_uniformly(knots_y, degree_y)
```

```python
# To create a B-spline surface instead.
control_point_weights[:] = 1
```

## Relation to NURBS-Diff

While we were aware of [NURBS-Diff](https://arxiv.org/abs/2104.14547)
before implementing this, [its
code](https://github.com/idealab-isu/NURBSDiff) was only released – or
we only found it – after we had finished this implementation. Our
advantage is the pure-Python implementation, but we are slower than
NURBS-Diff even when using the PyTorch 2 compiler. Our recommendation
would be to use the NURBS-Diff library for speed if you are not
limited by (possibly) fewer derivative options due to its manual
backward implementation.

## References

Les Piegl and Wayne Tiller. _The NURBS Book, Second Edition_.
Monographs in Visual Communication, Springer, 1997.
ISBN 978-3-540-61545-3.

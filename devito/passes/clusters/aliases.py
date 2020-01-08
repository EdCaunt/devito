from collections import OrderedDict, namedtuple

from cached_property import cached_property
from sympy import Indexed
import numpy as np

from devito.finite_differences import Eq
from devito.ir import (ROUNDABLE, DataSpace, IterationInstance, IterationSpace,
                       Interval, IntervalGroup, LabeledVector, Stencil,
                       detect_accesses, build_intervals)
from devito.logger import perf_adv
from devito.passes.clusters.utils import dse_pass, make_is_time_invariant
from devito.symbolics import estimate_cost, retrieve_indexed
from devito.types import Array, IncrDimension

__all__ = ['cire']


MIN_COST_ALIAS = 10
"""
Minimum operation count of an aliasing expression to be lifted into
a vector temporary.
"""

MIN_COST_ALIAS_INV = 50
"""
Minimum operation count of a time-invariant aliasing expression to be
lifted into a vector temporary. Time-invariant aliases are lifted outside
of the time-marching loop, thus they will require vector temporaries as big
as the entire grid.
"""


@dse_pass
def cire(cluster, template, platform):
    """
    Cross-iteration redundancies elimination.

    Examples
    --------
    1) expensive t-invariant sub-expression

    t0 = (cos(a[x,y,z])*sin(b[x,y,z]))*c[t,x,y,z]

    becomes

    t1[x,y,z] = cos(a[x,y,z])*sin(b[x,y,z])
    t0 = t1[x,y,z]*c[t,x,y,z]

    2) Redundant sub-expressions

    t0 = 2.0*a[x,y,z]*b[x,y,z]
    t1 = 3.0*a[x,y,z+1]*b[x,y,z+1]

    becomes

    t2[x,y,z] = a[x,y,z]*b[x,y,z]
    t0 = 2.0*t2[x,y,z]
    t1 = 3.0*t2[x,y,z+1]
    """
    exprs = cluster.exprs

    # Collect all aliasing expressions
    aliases = collect(exprs)

    # Heuristically determine the best (trade-off flops/memory) aliasing expressions
    candidates, processed = extract(exprs, aliases)

    # Create Aliases from aliasing expressions and assign them to Clusters
    clusters, subs = process(cluster, candidates, processed, aliases, template, platform)

    # Rebuild `cluster` so as to use the newly created tensor temporaries
    processed = [e.xreplace(subs) for e in processed]
    ispace = cluster.ispace.augment(aliases.index_mapper)
    rebuilt = cluster.rebuild(exprs=processed, ispace=ispace)

    return clusters + [rebuilt]


def collect(exprs):
    """
    Determine groups of aliasing expressions.

    An expression A aliases an expression B if both A and B perform the same
    arithmetic operations over the same input operands, with the possibility for
    Indexeds to access locations at a fixed constant offset in each Dimension.

    For example, consider the following expressions:

        * a[i+1] + b[i+1]
        * a[i+1] + b[j+1]
        * a[i] + c[i]
        * a[i+2] - b[i+2]
        * a[i+2] + b[i]
        * a[i-1] + b[i-1]

    The following alias to `a[i] + b[i]`:

        * a[i+1] + b[i+1] : same operands and operations, distance along i: 1
        * a[i-1] + b[i-1] : same operands and operations, distance along i: -1

    Whereas the following do not:

        * a[i+1] + b[j+1] : because at least one index differs
        * a[i] + c[i] : because at least one of the operands differs
        * a[i+2] - b[i+2] : because at least one operation differs
        * a[i+2] + b[i] : because the distances along ``i`` differ (+2 and +0)
    """
    # Determine the potential aliases
    candidates = []
    for expr in exprs:
        candidate = analyze(expr)
        if candidate is not None:
            candidates.append(candidate)

    # Group together the aliasing expressions (ultimately build an Alias for each
    # group of aliasing expressions)
    aliases = Aliases()
    unseen = list(candidates)
    while unseen:
        c = unseen.pop(0)

        # Find aliasing expressions
        group = [c]
        for i in list(unseen):
            if compare_ops(c.expr, i.expr) and is_translated(c, i):
                group.append(i)
                unseen.remove(i)

        # Try creating a basis spanning the aliasing expressions' iteration vectors
        try:
            COM, distances = calculate_COM(group)
        except ValueError:
            # Ignore these aliasing expressions and move on
            continue

        # Create an alias expression centering `c`'s Indexeds at the COM
        subs = {i: i.function[[x + v.fromlabel(x, 0) for x in b]]
                for i, b, v in zip(c.indexeds, c.bases, COM)}
        alias = c.expr.xreplace(subs)
        aliased = [i.expr for i in group]

        aliases.add(alias, aliased, distances)

    # Heuristically attempt to relax the Aliases offsets to maximize the
    # likelyhood of loop fusion
    groups = OrderedDict()
    for i in aliases.values():
        groups.setdefault(i.dimensions, []).append(i)
    for group in groups.values():
        ideal_anti_stencil = Stencil.union(*[i.anti_stencil for i in group])
        for i in group:
            if i.anti_stencil.subtract(ideal_anti_stencil).empty:
                aliases[i.alias] = i.relax(ideal_anti_stencil)

    return aliases


def extract(exprs, aliases):
    """
    Extract the candidate aliases.
    """
    #TODO: turn is_time_invariant into is_invariant
    is_time_invariant = make_is_time_invariant(exprs)
    time_invariants = {e.rhs: is_time_invariant(e) for e in exprs}

    processed = []
    candidates = OrderedDict()
    for e in exprs:
        # Cost check (to keep the memory footprint under control)
        naliases = len(aliases.get(e.rhs))
        cost = estimate_cost(e, True)*naliases
        test0 = lambda: cost >= MIN_COST_ALIAS and naliases > 1
        test1 = lambda: cost >= MIN_COST_ALIAS_INV and time_invariants[e.rhs]
        if test0() or test1():
            candidates[e.rhs] = e.lhs
        else:
            processed.append(e)

    return candidates, processed


def process(cluster, candidates, processed, aliases, template, platform):
    """
    Create Clusters from aliasing expressions.
    """
    clusters = []
    subs = {}
    for origin, alias in aliases.items():
        if all(i not in candidates for i in alias.aliased):
            continue

        # Create a temporary to store `alias`
        array = Array(name=template(), dimensions=alias.writeto.dimensions,
                      halo=[(abs(i.lower), abs(i.upper)) for i in alias.writeto],
                      dtype=cluster.dtype, scope='stack' if alias.fits_stack else 'heap')

        # The expression computing `alias`
        expression = Eq(array[aliases.index(origin)], origin.xreplace(subs))

        # Create the substitution rules so that we can use the newly created
        # temporary in place of the aliasing expressions
        for aliased, distance in alias.with_distance:
            assert all(i.dim in distance.labels for i in alias.writeto)
            offsets = [-i.lower + distance[i.dim] for i in alias.writeto]
            indices = [i + o for i, o in zip(aliases.index(origin), offsets)]
            if aliased in candidates:
                # It would *not* be in `candidates` if part of a composite alias
                subs[candidates[aliased]] = array[indices]
            subs[aliased] = array[indices]

        # Construct the `alias` IterationSpace
        ispace = cluster.ispace.add(alias.writeto).augment(aliases.index_mapper)

        # Optimize the `alias` IterationSpace: if possible, the innermost
        # IterationInterval is rounded up to a multiple of the vector length
        try:
            it = ispace.itintervals[-1]
            if ROUNDABLE in cluster.properties[it.dim]:
                vl = platform.simd_items_per_reg(cluster.dtype)
                ispace = ispace.add(Interval(it.dim, 0, it.interval.size % vl))
        except (TypeError, KeyError):
            pass

        # Construct the `alias` DataSpace
        accesses = detect_accesses(expression)
        parts = {k: IntervalGroup(build_intervals(v)).add(ispace.intervals)
                 for k, v in accesses.items() if k}
        dspace = DataSpace(cluster.dspace.intervals, parts)

        # Finally create the new Cluster hosting `alias`
        clusters.append(cluster.rebuild(exprs=expression, ispace=ispace, dspace=dspace))

    return clusters, subs


# Helpers

Candidate = namedtuple('Candidate', 'expr indexeds bases offsets')


def analyze(expr):
    """
    Determine whether ``expr`` is a potential Alias and collect relevant metadata.

    A necessary condition is that all Indexeds in ``expr`` are affine in the
    access Dimensions so that the access offsets (or "strides") can be derived.
    For example, given the following Indexeds: ::

        A[i, j, k], B[i, j+2, k+3], C[i-1, j+4]

    All of the access functions are affine in ``i, j, k``, and the offsets are: ::

        (0, 0, 0), (0, 2, 3), (-1, 4)
    """
    # No way if writing to a tensor or an increment
    if expr.lhs.is_Indexed or expr.is_Increment:
        return

    indexeds = retrieve_indexed(expr.rhs)
    if not indexeds:
        return

    bases = []
    offsets = []
    for i in indexeds:
        ii = IterationInstance(i)

        # There must not be irregular accesses, otherwise we won't be able to
        # calculate the offsets
        if ii.is_irregular:
            return

        # Since `ii` is regular (and therefore affine), it is guaranteed that `ai`
        # below won't be None
        base = []
        offset = []
        for e, ai in zip(ii, ii.aindices):
            if e.is_Number:
                base.append(e)
            else:
                base.append(ai)
                offset.append((ai, e - ai))
        bases.append(tuple(base))
        offsets.append(LabeledVector(offset))

    return Candidate(expr.rhs, indexeds, bases, offsets)


def compare_ops(e1, e2):
    """
    Return True if the two expressions ``e1`` and ``e2`` perform the same arithmetic
    operations over the same input operands, False otherwise.
    """
    if type(e1) == type(e2) and len(e1.args) == len(e2.args):
        if e1.is_Atom:
            return True if e1 == e2 else False
        elif isinstance(e1, Indexed) and isinstance(e2, Indexed):
            return True if e1.base == e2.base else False
        else:
            for a1, a2 in zip(e1.args, e2.args):
                if not compare_ops(a1, a2):
                    return False
            return True
    else:
        return False


def is_translated(c1, c2):
    """
    Given two potential aliases ``c1`` and ``c2``, return True if ``c1``
    is translated w.r.t. ``c2``, False otherwise.

    For example: ::

        c1 = A[i,j] + A[i,j+1]
        c2 = A[i+1,j] + A[i+1,j+1]

    ``c1``'s Toffsets are ``{i: [0, 0], j: [0, 1]}``, while ``c2``'s Toffsets are
    ``{i: [1, 1], j: [0, 1]}``. Then, ``c2`` is translated w.r.t. ``c1`` by
    ``(1, 0)``, and True is returned.
    """
    assert len(c1.offsets) == len(c2.offsets)

    # Transpose `offsets` so that
    # offsets = [{x: 2, y: 0}, {x: 1, y: 3}] => {x: [2, 1], y: [0, 3]}
    Toffsets1 = LabeledVector.transpose(*c1.offsets)
    Toffsets2 = LabeledVector.transpose(*c2.offsets)

    return all(len(set(i - j)) == 1 for (_, i), (_, j) in zip(Toffsets1, Toffsets2))


def calculate_COM(group):
    """
    Determine a centre of mass (COM) for a group of definitely aliasing expressions,
    which is a set of bases spanning all iteration vectors.

    Return the COM as well as the vector distance of each aliasing expression from
    the COM.
    """
    # Find the COM
    COM = []
    for ofs in zip(*[i.offsets for i in group]):
        Tofs = LabeledVector.transpose(*ofs)
        entries = []
        for k, v in Tofs:
            try:
                entries.append((k, int(np.mean(v, dtype=int))))
            except TypeError:
                # At least an element in `v` has symbolic components. Even though
                # `analyze` guarantees that no accesses can be irregular, a symbol
                # might still be present as long as it's constant (i.e., known to
                # be never written to). For example: `A[t, x_m + 2, y, z]`
                # At this point, the only chance we have is that the symbolic entry
                # is identical across all elements in `v`
                if len(set(v)) == 1:
                    entries.append((k, v[0]))
                else:
                    raise ValueError
        COM.append(LabeledVector(entries))

    # Calculate the distance from the COM
    distances = []
    for i in group:
        assert len(COM) == len(i.offsets)
        distance = [o.distance(c) for o, c in zip(i.offsets, COM)]
        distance = [(l, set(i)) for l, i in LabeledVector.transpose(*distance)]
        # The distance of each Indexed from the COM must be uniform across all Indexeds
        if any(len(i) != 1 for l, i in distance):
            raise ValueError
        distances.append(LabeledVector([(l, i.pop()) for l, i in distance]))

    return COM, distances


class Aliases(OrderedDict):

    def __init__(self):
        super(Aliases, self).__init__()

        self.index_mapper = {}

    def add(self, alias, aliased, distances):
        self[alias] = Alias(alias, aliased, distances)

        # Update the index_mapper
        for d in self[alias].writeto.dimensions:
            if d in self.index_mapper:
                continue
            if d.is_Incr:
                # IncrDimensions, if present, must be substituted such that we
                # stay in bounds when indexing into the alias
                self.index_mapper[d] = IncrDimension(d, 0, d.symbolic_size - 1, 1,
                                                     "%ss" % d.name)

    def get(self, key):
        ret = super(Aliases, self).get(key)
        if ret is not None:
            return ret.aliased
        for v in self.values():
            if key in v.aliased:
                return v.aliased
        return []

    def index(self, key):
        if key not in self:
            raise KeyError
        return [self.index_mapper.get(d, d) for d in self[key].writeto.dimensions]


class Alias(object):

    """
    Map an expression (the so called "alias") to a set of aliasing expressions.
    For each aliasing expression, the distance from the Alias along each Dimension
    is tracked.
    """

    def __init__(self, alias, aliased=None, distances=None, ghost_offsets=None):
        self.alias = alias
        self.aliased = aliased or []
        self.distances = distances or []
        self.ghost_offsets = ghost_offsets or Stencil()

        assert len(self.aliased) == len(self.distances)

        # Transposed distances
        self.Tdistances = LabeledVector.transpose(*distances)

    @cached_property
    def dimensions(self):
        return tuple(i for i, _ in self.Tdistances)

    @cached_property
    def anti_stencil(self):
        ret = Stencil()
        for k, v in self.Tdistances:
            ret[k].update(set(v))
        for k, v in self.ghost_offsets.items():
            ret[k].update(v)
        return ret

    @cached_property
    def with_distance(self):
        """
        Return a tuple associating each aliased expression with its distance
        from ``self.alias``.
        """
        return tuple(zip(self.aliased, self.distances))

    @cached_property
    def writeto(self):
        """
        The written data region, as an IntervalGroup.
        """
        intervals = [Interval(d, *v) for d, v in self._relaxed_diameter.items()]
        intervals = IntervalGroup(intervals)

        # Optimization: only retain those Interval along which the redundancies
        # have been captured
        dep_inducing = [i for i in intervals if any(i.offsets)]
        try:
            if dep_inducing:
                index = intervals.index(dep_inducing[0])
                intervals = IntervalGroup(intervals[index:])
        except IndexError:
            perf_adv("Couldn't optimize some of the detected redundancies")

        return intervals

    @property
    def fits_stack(self):
        #TODO: improve me
        return len([i for i in self.writeto if not i.dim.is_Incr])

    def add(self, aliased, distance):
        aliased = self.aliased + [aliased]
        distances = self.distances + [distance]
        return Alias(self.alias, aliased, distances, self.ghost_offsets)

    def relax(self, stencil):
        ghost_offsets = stencil.add(self.ghost_offsets)
        return Alias(self.alias, self.aliased, self.distances, ghost_offsets)

    @property
    def _diameter(self):
        """
        The min/max distance along each Dimension for this Alias.
        """
        return OrderedDict((k, (min(v), max(v))) for k, v in self.Tdistances)

    @property
    def _relaxed_diameter(self):
        """
        Return a map telling the min/max offsets in each Dimension for this Alias.
        The extremes are potentially larger than those provided by ``self.diameter``,
        as here we're also taking into account the ghost offsets.
        """
        return OrderedDict((k, (min(v), max(v))) for k, v in self.anti_stencil.items())

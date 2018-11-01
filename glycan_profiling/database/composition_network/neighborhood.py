from collections import defaultdict, OrderedDict
import numbers as abc_numbers

try:
    basestring
except NameError:
    basestring = (str, bytes)

from glypy.structure.glycan_composition import FrozenMonosaccharideResidue

from ..glycan_composition_filter import GlycanCompositionFilter
from ... import symbolic_expression

from .space import CompositionSpace, n_glycan_distance
from .rule import CompositionExpressionRule, CompositionRangeRule, CompositionRuleClassifier


class NeighborhoodCollection(object):
    def __init__(self, neighborhoods=None):
        if neighborhoods is None:
            neighborhoods = OrderedDict()
        self.neighborhoods = OrderedDict()
        if isinstance(neighborhoods, (dict)):
            self.neighborhoods = OrderedDict(neighborhoods)
        else:
            for item in neighborhoods:
                self.add(item)

    def add(self, classifier):
        self.neighborhoods[classifier.name] = classifier

    def remove(self, key):
        return self.neighborhoods.pop(key)

    def update(self, iterable):
        for case in iterable:
            self.add(case)

    def clear(self):
        self.neighborhoods.clear()

    def copy(self):
        return self.__class__(self.neighborhoods)

    clone = copy

    def __iter__(self):
        return iter(self.neighborhoods.values())

    def __repr__(self):
        return "NeighborhoodCollection(%s)" % (', '.join(self.neighborhoods.keys()))

    def __eq__(self, other):
        return list(self) == list(other)

    def __ne__(self, other):
        return not (self == other)

    def get_neighborhood(self, key):
        return self.neighborhoods[key]

    def __getitem__(self, key):
        try:
            return self.get_neighborhood(key)
        except KeyError:
            if isinstance(key, abc_numbers.Number):
                return self.neighborhoods.values()[key]
            else:
                raise

    def __len__(self):
        return len(self.neighborhoods)


def make_n_glycan_neighborhoods():
    """Create broad N-glycan neighborhoods

    Returns
    -------
    NeighborhoodCollection
    """
    neighborhoods = NeighborhoodCollection()

    _neuraminic = "(%s)" % ' + '.join(map(str, (
        FrozenMonosaccharideResidue.from_iupac_lite("NeuAc"),
        FrozenMonosaccharideResidue.from_iupac_lite("NeuGc")
    )))
    _hexose = "(%s)" % ' + '.join(
        map(str, map(FrozenMonosaccharideResidue.from_iupac_lite, ['Hex', ])))
    _hexnac = "(%s)" % ' + '.join(
        map(str, map(FrozenMonosaccharideResidue.from_iupac_lite, ['HexNAc', ])))

    high_mannose = CompositionRangeRule(
        _hexose, 3, 12) & CompositionRangeRule(
        _hexnac, 2, 2) & CompositionRangeRule(
        _neuraminic, 0, 0)
    high_mannose.name = "high-mannose"
    neighborhoods.add(high_mannose)

    base_hexnac = 3
    base_neuac = 2
    for i, spec in enumerate(['hybrid', 'bi', 'tri', 'tetra', 'penta', "hexa", "hepta"]):
        if i == 0:
            rule = CompositionRangeRule(
                _hexnac, base_hexnac - 1, base_hexnac + 1
            ) & CompositionRangeRule(
                _neuraminic, 0, base_neuac) & CompositionRangeRule(
                _hexose, base_hexnac + i - 1,
                base_hexnac + i + 3)
            rule.name = spec
            neighborhoods.add(rule)
        else:
            sialo = CompositionRangeRule(
                _hexnac, base_hexnac + i - 1, base_hexnac + i + 1
            ) & CompositionRangeRule(
                _neuraminic, 1, base_neuac + i
            ) & CompositionRangeRule(
                _hexose, base_hexnac + i - 1,
                base_hexnac + i + 2)

            sialo.name = "%s-antennary" % spec
            asialo = CompositionRangeRule(
                _hexnac, base_hexnac + i - 1, base_hexnac + i + 1
            ) & CompositionRangeRule(
                _neuraminic, 0, 1 if i < 2 else 0
            ) & CompositionRangeRule(
                _hexose, base_hexnac + i - 1,
                base_hexnac + i + 2)

            asialo.name = "asialo-%s-antennary" % spec
            neighborhoods.add(sialo)
            neighborhoods.add(asialo)
    return neighborhoods


def make_adjacency_neighborhoods(network):
    space = CompositionSpace([node.composition for node in network])

    rules = []
    for node in network:
        terms = []
        for monosaccharide in space.monosaccharides:
            terms.append(("abs({} - {})".format(
                monosaccharide, node.composition[monosaccharide])))
        expr = '(%s) < 2' % (' + '.join(terms),)
        expr_rule = CompositionExpressionRule(expr)
        rule = CompositionRuleClassifier(str(node.composition), [expr_rule])
        rules.append(rule)
    return rules


_n_glycan_neighborhoods = make_n_glycan_neighborhoods()


class NeighborhoodWalker(object):

    def __init__(self, network, neighborhoods=None, assign=True):
        if neighborhoods is None:
            neighborhoods = NeighborhoodCollection(_n_glycan_neighborhoods)
        self.network = network
        self.neighborhood_assignments = defaultdict(set)
        self.neighborhoods = neighborhoods
        self.filter_space = GlycanCompositionFilter(
            [self.normalize_composition(node.composition) for node in self.network])

        self.symbols = symbolic_expression.SymbolSpace(self.filter_space.monosaccharides)

        self.neighborhood_maps = defaultdict(list)

        if assign:
            self.assign()

    def normalize_composition(self, composition):
        return self.network.normalize_composition(composition)

    def _pack_maps(self):
        key_neighborhood_assignments = defaultdict(set)
        key_neighborhood_maps = defaultdict(list)

        for key, value in self.neighborhood_assignments.items():
            key_neighborhood_assignments[key.glycan_composition] = value
        for key, value in self.neighborhood_maps.items():
            key_neighborhood_maps[key.glycan_composition] = value
        return key_neighborhood_assignments, key_neighborhood_maps

    def _unpack_maps(self, packed_maps):
        (key_neighborhood_assignments, key_neighborhood_maps) = packed_maps

        for key, value in key_neighborhood_assignments.items():
            self.neighborhood_assignments[self.network[key.glycan_composition]] = value

        for key, value in key_neighborhood_maps.items():
            self.neighborhood_maps[self.network[key.glycan_composition]] = value

    def __getstate__(self):
        return self._pack_maps()

    def __setstate__(self, state):
        self._unpack_maps(state)

    def __reduce__(self):
        return self.__class__, (self.network, self.neighborhoods, False)

    def neighborhood_names(self):
        return [n.name for n in self.neighborhoods]

    def __getitem__(self, key):
        return self.neighborhood_assignments[key]

    def query_neighborhood(self, neighborhood):
        query = None
        filters = []
        for rule in neighborhood.rules:
            if not self.symbols.partially_defined(rule.symbols):
                continue

            filters.append(rule)
            try:
                low = rule.low
                high = rule.high
            except AttributeError:
                continue
            if low is None:
                low = 0
            if high is None:
                # No glycan will have more than 100 of a single residue
                # in practice.
                high = 100
            name = rule.symbols[0]
            if query is None:
                query = self.filter_space.query(name, low, high)
            else:
                query.add(name, low, high)
        if filters:
            query = filter(lambda x: all([f(x) for f in filters]), query)
        else:
            query = query.all()
        return query

    def assign(self):
        for neighborhood in self.neighborhoods:
            query = self.query_neighborhood(neighborhood)
            if query is None:
                raise ValueError("Query cannot be None! %r" % neighborhood)
            for composition in query:
                composition = self.normalize_composition(composition)
                if neighborhood(composition):
                    self.neighborhood_assignments[
                        self.network[composition]].add(neighborhood.name)
        for node in self.network:
            for neighborhood in self[node]:
                self.neighborhood_maps[neighborhood].append(node)

    def compute_belongingness(self, node, neighborhood, distance_fn=n_glycan_distance):
        count = 0
        total_weight = 0
        for member in self.neighborhood_maps[neighborhood]:
            distance, weight = distance_fn(node.glycan_composition, member.glycan_composition)
            if distance == 0:
                weight = 1.0
            total_weight += weight
            count += 1
        if count == 0:
            return 0
        return total_weight / count
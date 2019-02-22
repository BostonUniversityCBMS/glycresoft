from collections import namedtuple
from functools import partial

import numpy as np

from ms_deisotope.peak_dependency_network.intervals import SpanningMixin

from glycopeptidepy.structure.composition import Composition
from glycopeptidepy.structure.sequence import PeptideSequence
from glycopeptidepy.structure.parser import sequence_tokenizer
from glycopeptidepy.algorithm import reverse_preserve_sequon
from glycopeptidepy.structure.glycan import HashableGlycanComposition
from glycopeptidepy.structure.fragmentation_strategy import StubGlycopeptideStrategy
from glypy.structure.glycan_composition import FrozenMonosaccharideResidue


from .lru import LRUCache


class GlycanCompositionCache(dict):
    pass


class CachingGlycanCompositionParser(object):
    def __init__(self, cache_size=4000):
        self.cache = GlycanCompositionCache()
        self.cache_size = cache_size
        self.lru = LRUCache()

    def _check_cache_valid(self):
        lru = self.lru
        while len(self.cache) > self.cache_size:
            key = lru.get_least_recently_used()
            lru.remove_node(key)
            value = self.cache.pop(key)
            try:
                value.clear_caches()
            except AttributeError:
                pass

    def _make_new_value(self, struct):
        value = HashableGlycanComposition.parse(struct.composition)
        value.id = struct.id
        return value

    def _populate_cache(self, struct, key):
        self._check_cache_valid()
        value = self._make_new_value(struct)
        self.cache[key] = value
        self.lru.add_node(key)
        return value

    def _extract_key(self, struct):
        return struct.composition

    def parse(self, struct):
        struct_key = self._extract_key(struct)
        try:
            seq = self.cache[struct_key]
            self.lru.hit_node(struct_key)
            return seq
        except KeyError:
            return self._populate_cache(struct, struct_key)

    def __call__(self, value):
        return self.parse(value)


class TextHashableGlycanCompositionParser(object):
    def __init__(self, size=int(2**16)):
        # self.cache = LRUMapping(size)
        self.cache = {}
        self.size = size

    def _parse(self, text):
        return HashableGlycanComposition.parse(text)

    def parse(self, text):
        try:
            return self.cache[text].clone()
        except KeyError:
            inst = self._parse(text)
            self.cache[text] = inst
            if len(self.cache) > self.size and self.size != -1:
                self.cache.popitem()
            return inst.clone()

    def __call__(self, text):
        return self.parse(text)


_glycan_parser = TextHashableGlycanCompositionParser()

hashable_glycan_glycopeptide_parser = partial(
    sequence_tokenizer, glycan_parser_function=_glycan_parser)


class GlycanFragmentCache(object):
    def __init__(self):
        self.cache = dict()

    def get_oxonium_ions(self, glycopeptide):
        try:
            return self.cache[glycopeptide.glycan]
        except KeyError:
            oxonium_ions = list(glycopeptide._glycan_fragments())
            self.cache[glycopeptide.glycan] = oxonium_ions
            return oxonium_ions

    def __call__(self, glycopeptide):
        return self.get_oxonium_ions(glycopeptide)


oxonium_ion_cache = GlycanFragmentCache()


class PeptideProteinRelation(SpanningMixin):
    __slots__ = ["protein_id", "hypothesis_id"]

    def __init__(self, start_position, end_position, protein_id, hypothesis_id):
        self.start = start_position
        self.end = end_position
        self.protein_id = protein_id
        self.hypothesis_id = hypothesis_id

    @property
    def start_position(self):
        return int(self.start)

    @start_position.setter
    def start_position(self, value):
        self.start = value

    @property
    def end_position(self):
        return int(self.end)

    @end_position.setter
    def end_position(self, value):
        self.end = value

    def __repr__(self):
        return "PeptideProteinRelation(%d, %d, %d, %d)" % (self.start, self.end, self.protein_id, self.hypothesis_id)

    def __iter__(self):
        yield self.start_position
        yield self.end_position
        yield self.protein_id
        yield self.hypothesis_id

    def __reduce__(self):
        return self.__class__, tuple(self)

    def __eq__(self, other):
        return (self.start_position == other.start_position and
                self.end_position == other.end_position and
                self.protein_id == other.protein_id and
                self.hypothesis_id == other.hypothesis_id)

    def __ne__(self, other):
        return not (self == other)

    def __hash__(self):
        return hash((self.start_position, self.end_position))


class FragmentCachingGlycopeptide(PeptideSequence):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault('parser_function', hashable_glycan_glycopeptide_parser)
        super(FragmentCachingGlycopeptide, self).__init__(*args, **kwargs)
        self.fragment_caches = {}
        self.protein_relation = None
        self.id = None

    def __reduce__(self):
        return self.__class__, (str(self), ), self.__getstate__()

    def __getstate__(self):
        state = {}
        state['protein_relation'] = self.protein_relation
        state['id'] = self.id
        return state

    def __setstate__(self, state):
        self.protein_relation = state['protein_relation']
        self.id = state['id']

    def __eq__(self, other):
        try:
            return (self.protein_relation == other.protein_relation) and (
                super(FragmentCachingGlycopeptide, self).__eq__(other))
        except AttributeError:
            return super(FragmentCachingGlycopeptide, self).__eq__(other)

    def get_fragments(self, *args, **kwargs):
        key = ("get_fragments", args, frozenset(kwargs.items()))
        try:
            return self.fragment_caches[key]
        except KeyError:
            result = list(super(FragmentCachingGlycopeptide, self).get_fragments(*args, **kwargs))
            self.fragment_caches[key] = result
            return result

    def stub_fragments(self, *args, **kwargs):
        kwargs.setdefault("strategy", CachingStubGlycopeptideStrategy)
        key = ('stub_fragments', args, frozenset(kwargs.items()))
        try:
            return self.fragment_caches[key]
        except KeyError:
            result = list(super(FragmentCachingGlycopeptide, self).stub_fragments(*args, **kwargs))
            self.fragment_caches[key] = result
            return result

    def _glycan_fragments(self):
        return list(super(FragmentCachingGlycopeptide, self).glycan_fragments(oxonium=True))

    def glycan_fragments(self, *args, **kwargs):
        return oxonium_ion_cache(self)

    def clear_caches(self):
        self.fragment_caches.clear()

    def clone(self, *args, **kwargs):
        share_cache = kwargs.pop("share_cache", True)
        new = super(FragmentCachingGlycopeptide, self).clone(*args, **kwargs)
        new.id = self.id
        new.protein_relation = self.protein_relation
        # Intentionally share caches with offspring
        if share_cache:
            new.fragment_caches = self.fragment_caches
        return new

    def __repr__(self):
        return str(self)


KeyTuple = namedtuple("KeyTuple", ['id', 'sequence'])


class GlycopeptideCache(object):
    def __init__(self):
        self.sequence_map = dict()
        self.key_map = dict()

    def __getitem__(self, key):
        try:
            result = self.key_map[key]
            return result
        except KeyError:
            value = self.sequence_map[key.sequence]
            value = value.clone()
            self.key_map[key] = value
            return value

    def __setitem__(self, key, value):
        self.key_map[key] = value
        self.sequence_map[key.sequence] = value

    def __len__(self):
        return len(self.key_map)

    def pop(self, key):
        self.key_map.pop(key)
        self.sequence_map.pop(key.sequence, None)


class CachingGlycopeptideParser(object):
    def __init__(self, cache_size=4000):
        self.cache = GlycopeptideCache()
        self.cache_size = cache_size
        self.lru = LRUCache()
        self.churn = 0

    def _check_cache_valid(self):
        lru = self.lru
        while len(self.cache) > self.cache_size:
            self.churn += 1
            key = lru.get_least_recently_used()
            lru.remove_node(key)
            value = self.cache.pop(key)
            try:
                value.clear_caches()
            except AttributeError:
                pass

    def _make_new_value(self, struct):
        value = FragmentCachingGlycopeptide(struct.glycopeptide_sequence)
        value.id = struct.id
        value.protein_relation = PeptideProteinRelation(
            struct.start_position, struct.end_position,
            struct.protein_id, struct.hypothesis_id)
        return value

    def _populate_cache(self, struct, key):
        self._check_cache_valid()
        value = self._make_new_value(struct)
        self.cache[key] = value
        self.lru.add_node(key)
        return value

    def _extract_key(self, struct):
        return KeyTuple(struct.id, struct.glycopeptide_sequence)

    def parse(self, struct):
        struct_key = self._extract_key(struct)
        try:
            seq = self.cache[struct_key]
            self.lru.hit_node(struct_key)
            return seq
        except KeyError:
            return self._populate_cache(struct, struct_key)

    def __call__(self, value):
        return self.parse(value)


class CachingPeptideParser(CachingGlycopeptideParser):
    def _make_new_value(self, struct):
        value = FragmentCachingGlycopeptide(struct.modified_peptide_sequence)
        value.id = struct.id
        value.protein_relation = PeptideProteinRelation(
            struct.start_position, struct.end_position,
            struct.protein_id, struct.hypothesis_id)
        return value

    def _extract_key(self, struct):
        return KeyTuple(struct.id, struct.modified_peptide_sequence)


class DecoyFragmentCachingGlycopeptide(FragmentCachingGlycopeptide):

    def stub_fragments(self, *args, **kwargs):
        key = ('stub_fragments', args, frozenset(kwargs.items()))
        try:
            return self.fragment_caches[key]
        except KeyError:
            result = list(super(FragmentCachingGlycopeptide, self).stub_fragments(*args, **kwargs))
            random_state = np.random.RandomState(
                int(round(self.glycan_composition.mass())))
            random_low = kwargs.get('random_low', 1.0)
            random_high = kwargs.get("random_high", 30.0)
            for frag in result:
                if frag.glycosylation_size > 1:
                    delta = random_state.uniform(random_low, random_high)
                    frag.mass += delta
            self.fragment_caches[key] = result
            return result

    @classmethod
    def from_target(cls, target):
        inst = cls()
        if target._glycosylation_manager.aggregate is not None:
            glycan = target._glycosylation_manager.aggregate.clone()
            glycan.composition_offset = Composition("H2O")
        else:
            glycan = None
        inst._init_from_components(
            target.sequence, glycan,
            target.n_term.modification,
            target.c_term.modification)
        inst.id = target.id + (-1, )
        inst.protein_relation = target.protein_relation
        # Intentionally share caches with offspring
        inst.fragment_caches = {k: v for k, v in target.fragment_caches.items() if 'stub_fragments' not in k}
        return inst


class CachingStubGlycopeptideStrategy(StubGlycopeptideStrategy):
    _cache = dict()

    def n_glycan_composition_fragments(self, glycan, core_count=1, iteration_count=0):
        try:
            value = self._cache[glycan, core_count, iteration_count]
            return value
        except KeyError:
            value = super(CachingStubGlycopeptideStrategy, self).n_glycan_composition_fragments(
                glycan, core_count, iteration_count)
            self._cache[glycan, core_count, iteration_count] = value
            return value


class DecoyMakingCachingGlycopeptideParser(CachingGlycopeptideParser):

    def _make_new_value(self, struct):
        value = DecoyFragmentCachingGlycopeptide(str(reverse_preserve_sequon(struct.glycopeptide_sequence)))
        value.id = struct.id
        value.protein_relation = PeptideProteinRelation(
            struct.start_position, struct.end_position,
            struct.protein_id, struct.hypothesis_id)
        return value


class DecoyMonosaccharideResidue(FrozenMonosaccharideResidue):
    _delta = 3.0
    __cache = {}

    @classmethod
    def get_cache(self):
        return self.__cache

    def mass(self, *args, **kwargs):
        return super(DecoyMonosaccharideResidue, self).mass(*args, **kwargs) + self._delta


class DecoyShiftingStubGlycopeptideStrategy(StubGlycopeptideStrategy):
    def __init__(self, peptide, extended=True):
        super(DecoyShiftingStubGlycopeptideStrategy, self).__init__(peptide, extended=extended)

    def _prepare_monosaccharide(self, name):
        return DecoyMonosaccharideResidue.from_iupac_lite(name)


class GlycopeptideDatabaseRecord(object):
    __slots__ = [
        "id", "calculated_mass",
        "glycopeptide_sequence",
        "protein_id",
        "start_position",
        "end_position",
        "peptide_mass",
        "hypothesis_id"
    ]

    def __init__(self, id, calculated_mass, glycopeptide_sequence, protein_id,
                 start_position, end_position, peptide_mass, hypothesis_id):
        self.id = id
        self.calculated_mass = calculated_mass
        self.glycopeptide_sequence = glycopeptide_sequence
        self.protein_id = protein_id
        self.start_position = start_position
        self.end_position = end_position
        self.peptide_mass = peptide_mass
        self.hypothesis_id = hypothesis_id

    def __reduce__(self):
        return self.__class__, (self.id, self.calculated_mass, self.glycopeptide_sequence, self.protein_id,
                                self.start_position, self.end_position, self.peptide_mass, self.hypothesis_id)

    def __repr__(self):
        template = (
            "{self.__class__.__name__}(id={self.id}, calculated_mass={self.calculated_mass}, "
            "glycopeptide_sequence={self.glycopeptide_sequence}, protein_id={self.protein_id}, "
            "start_position={self.start_position}, end_position={self.end_position}, "
            "peptide_mass={self.peptide_mass}, hypothesis_id={self.hypothesis_id}, ")
        return template.format(self=self)


class PeptideDatabaseRecord(object):
    __slots__ = ['id', "calculated_mass", "modified_peptide_sequence", "protein_id", "start_position", "end_position",
                 "hypothesis_id", "n_glycosylation_sites", "o_glycosylation_sites", "gagylation_sites"]

    def __init__(self, id, calculated_mass, modified_peptide_sequence, protein_id, start_position, end_position,
                 hypothesis_id, n_glycosylation_sites, o_glycosylation_sites, gagylation_sites):
        self.id = id
        self.calculated_mass = calculated_mass
        self.modified_peptide_sequence = modified_peptide_sequence
        self.protein_id = protein_id
        self.start_position = start_position
        self.end_position = end_position
        self.hypothesis_id = hypothesis_id
        self.n_glycosylation_sites = list(n_glycosylation_sites)
        self.o_glycosylation_sites = list(o_glycosylation_sites)
        self.gagylation_sites = list(gagylation_sites)

    def convert(self):
        peptide = FragmentCachingGlycopeptide(self.modified_peptide_sequence)
        peptide.id = self.id
        rel = PeptideProteinRelation(self.start_position, self.end_position, self.protein_id, self.hypothesis_id)
        peptide.protein_relation = rel
        return peptide

    def __hash__(self):
        return hash(self.modified_peptide_sequence)

    def __eq__(self, other):
        if other is None:
            return False
        if self.id != other.id:
            return False
        if self.protein_id != other.protein_id:
            return False
        if abs(self.calculated_mass - other.calculated_mass) > 1e-3:
            return False
        if self.start_position != other.start_position:
            return False
        if self.end_position != other.end_position:
            return False
        if self.hypothesis_id != other.hypothesis_id:
            return False
        if self.n_glycosylation_sites != other.n_glycosylation_sites:
            return False
        if self.o_glycosylation_sites != other.o_glycosylation_sites:
            return False
        if self.gagylation_sites != other.gagylation_sites:
            return False
        return True

    def __ne__(self, other):
        return not (self == other)

    def has_glycosylation_sites(self):
        return (len(self.n_glycosylation_sites) + len(self.o_glycosylation_sites) + len(self.gagylation_sites)) > 0

    def __repr__(self):
        fields = ', '.join(["%s=%r" % (n, getattr(self, n)) for n in self.__slots__])
        return "{self.__class__.__name__}({fields})".format(self=self, fields=fields)

    @classmethod
    def from_record(cls, record):
        return cls(**record)

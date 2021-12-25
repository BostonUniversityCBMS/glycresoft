cimport cython

from libc.stdlib cimport malloc, free

from cpython cimport (
    PyTuple_GetItem, PyTuple_GET_ITEM, PyTuple_GET_SIZE,
    PyInt_AsLong, PyFloat_AsDouble,
    PyObject, PyDict_GetItem, PyDict_SetItem,
    PyList_GET_SIZE, PyList_GET_ITEM)

from glypy._c.structure.glycan_composition cimport _CompositionBase
from ms_deisotope._c.peak_set cimport DeconvolutedPeak, DeconvolutedPeakSet
from glycopeptidepy._c.structure.fragment cimport SimpleFragment

from collections import defaultdict

from glycopeptidepy import PeptideSequence
from glypy.structure.glycan_composition import FrozenMonosaccharideResidue
from glycopeptidepy.structure.glycan import GlycanCompositionProxy as _GlycanCompositionProxy

from_iupac_lite = FrozenMonosaccharideResidue.from_iupac_lite

cdef object GlycanCompositionProxy = _GlycanCompositionProxy

cdef class SignatureSpecification(object):
    def __init__(self, components, masses):
        self.components = tuple(from_iupac_lite(k) for k in components)
        self.masses = tuple(masses)
        self._hash = hash(self.components)
        self._is_compound = len(self.masses) > 1
        self.n_masses = PyTuple_GET_SIZE(self.masses)
        self._init_mass_array()

    cdef void _init_mass_array(self):
        self._masses = <double*>malloc(sizeof(double) * self.n_masses)
        for i in range(self.n_masses):
            obj = self.masses[i]
            self._masses[i] = PyFloat_AsDouble(obj)

    def __dealloc__(self):
        if self._masses != NULL:
            free(self._masses)
            self._masses = NULL

    def __getitem__(self, i):
        return <object>PyTuple_GetItem(self.components, i)

    def __iter__(self):
        return iter(self.components)

    def __hash__(self):
        return self._hash

    def __eq__(self, other):
        if other is None:
            return False
        if isinstance(other, SignatureSpecification):
            return self.components == (<SignatureSpecification>other).components
        else:
            return self.components == other.components

    def __ne__(self, other):
        return not (self == other)

    def __repr__(self):
        return "{self.__class__.__name__}({self.components}, {self.masses})".format(self=self)

    cpdef bint is_expected(self, glycan_composition):
        cdef:
            size_t i, n
            bint is_expected
            long count
            _CompositionBase composition

        if isinstance(glycan_composition, GlycanCompositionProxy):
            glycan_composition = glycan_composition.obj
            while isinstance(glycan_composition, GlycanCompositionProxy):
                glycan_composition = glycan_composition.obj
            composition = <_CompositionBase?>glycan_composition
        elif isinstance(glycan_composition, _CompositionBase):
            composition = <_CompositionBase?>glycan_composition
        else:
            raise TypeError("Requires a _CompositionBase or GlycanCompositionProxy")

        n = PyTuple_GET_SIZE(self.components)
        for i in range(n):
            component = <object>PyTuple_GET_ITEM(self.components, i)
            tmp = composition._getitem_fast(component)
            count = PyInt_AsLong(tmp)
            if count == 0:
                return False
        return True

    cpdef int count_of(self, glycan_composition):
        cdef:
            size_t i, n
            bint is_expected
            long count, limit
            _CompositionBase composition

        if isinstance(glycan_composition, GlycanCompositionProxy):
            glycan_composition = glycan_composition.obj
            while isinstance(glycan_composition, GlycanCompositionProxy):
                glycan_composition = glycan_composition.obj
            composition = <_CompositionBase?>glycan_composition
        elif isinstance(glycan_composition, _CompositionBase):
            composition = <_CompositionBase?>glycan_composition
        else:
            raise TypeError("Requires a _CompositionBase or GlycanCompositionProxy")


        n = PyTuple_GET_SIZE(self.components)
        limit = 100000
        for i in range(n):
            component = <object>PyTuple_GET_ITEM(self.components, i)
            count = PyInt_AsLong(composition._getitem_fast(component))
            if count < limit:
                limit = count
        return limit

    cpdef DeconvolutedPeak peak_of(self, DeconvolutedPeakSet spectrum, double error_tolerance):
        cdef:
            size_t i, j, n
            double mass, best_signal
            DeconvolutedPeak peak, next_peak
            tuple peaks

        peak = None
        best_signal = -1
        for j in range(self.n_masses):
            mass = self._masses[j]
            peaks = spectrum.all_peaks_for(mass, error_tolerance)
            n = PyTuple_GET_SIZE(peaks)
            for i in range(n):
                next_peak = <DeconvolutedPeak>PyTuple_GET_ITEM(peaks, i)
                if next_peak.intensity > best_signal:
                    peak = next_peak
                    best_signal = next_peak.intensity
        return peak


@cython.freelist(1000)
cdef class OxoniumIndexMatch(object):

    def __init__(self, index_matches, glycan_to_index, id_to_index):
        self.index_matches = index_matches
        self.glycan_to_index = glycan_to_index
        self.id_to_index = id_to_index

    @staticmethod
    cdef OxoniumIndexMatch _create(dict index_matches, dict glycan_to_index, dict id_to_index):
        cdef OxoniumIndexMatch self = OxoniumIndexMatch.__new__(OxoniumIndexMatch)
        self.index_matches = index_matches
        self.glycan_to_index = glycan_to_index
        self.id_to_index = id_to_index
        return self

    cpdef list by_glycan(self, glycan):
        cdef:
            PyObject* tmp

        tmp = PyDict_GetItem(self.glycan_to_index, glycan)
        if tmp == NULL:
            return None
        index = <object>tmp
        tmp = PyDict_GetItem(self.index_matches, index)
        if tmp == NULL:
            return None
        return <list>tmp

    cpdef list by_id(self, glycan_id):
        cdef:
            PyObject* tmp

        tmp = PyDict_GetItem(self.id_to_index, glycan_id)
        if tmp == NULL:
            return None
        index = <object>tmp
        tmp = PyDict_GetItem(self.index_matches, index)
        if tmp == NULL:
            return None
        return <list>tmp


cdef class OxoniumIndex(object):
    '''An index for quickly matching all oxonium ions against a spectrum and efficiently mapping them
    back to individual glycan compositions.
    '''

    def __init__(self, fragments=None, fragment_index=None, glycan_to_index=None):
        self.fragments = fragments or []
        self.fragment_index = fragment_index or {}
        self.glycan_to_index = glycan_to_index or {}
        self.index_to_glycan = {v: k for k, v in self.glycan_to_index.items()}
        self.index_to_simplified_index = None

    def _make_glycopeptide_stub(self, glycan_composition):
        p = PeptideSequence("P%s" % glycan_composition)
        return p

    def build_index(self, glycan_composition_records, **kwargs):
        cdef:
            PyObject* tmp
            list acc
            dict fragments, fragment_index, glycan_index

        fragments = {}
        fragment_index = {}
        glycan_index = {}
        for gc_rec in glycan_composition_records:
            glycan_index[gc_rec.composition] = gc_rec.id

            p = self._make_glycopeptide_stub(gc_rec.composition)
            for frag in p.glycan_fragments(**kwargs):
                fragments[frag.name] = frag
                tmp = PyDict_GetItem(fragment_index, frag.name)
                if tmp == NULL:
                    acc = []
                    PyDict_SetItem(fragment_index, frag.name, acc)
                else:
                    acc = <list>tmp
                acc.append(gc_rec.id)

        self.glycan_to_index = glycan_index
        self.fragment_index = fragment_index
        self.fragments = sorted(fragments.values(), key=lambda x: x.mass)
        self.index_to_glycan = {v: k for k, v in self.glycan_to_index.items()}
        self.simplify()

    cpdef OxoniumIndexMatch match(self, DeconvolutedPeakSet spectrum, double error_tolerance):
        cdef:
            dict match_index
            size_t i, n, j, m
            DeconvolutedPeak peak
            SimpleFragment fragment
            list bucket, acc
            PyObject* tmp
        match_index = {}
        n = PyList_GET_SIZE(self.fragments)
        for i in range(n):
            fragment = <SimpleFragment>PyList_GET_ITEM(self.fragments, i)
            peak = spectrum.has_peak(fragment.mass, error_tolerance)
            if peak is not None:
                bucket = <list>PyDict_GetItem(self.fragment_index, fragment.name)
                m = PyList_GET_SIZE(bucket)
                for j in range(m):
                    key = <object>PyList_GET_ITEM(bucket, j)
                    tmp = PyDict_GetItem(match_index, key)
                    if tmp == NULL:
                        acc = []
                        PyDict_SetItem(match_index, key, acc)
                    else:
                        acc = <list>tmp
                    acc.append((fragment, peak.index.neutral_mass))
        return OxoniumIndexMatch._create(match_index, self.glycan_to_index, self.index_to_simplified_index)

    cpdef object simplify(self):
        cdef:
            PyObject* tmp
            list acc
        id_to_frag_group = defaultdict(set)
        for f, members in self.fragment_index.items():
            for member in members:
                id_to_frag_group[member].add(f)

        groups = defaultdict(list)
        for member, group in id_to_frag_group.items():
            groups[frozenset(group)].append(member)

        counter = 0
        new_fragment_index = {}
        new_glycan_index = {}
        secondary_index = {}
        for frag_group, members in groups.items():
            new_id = counter
            counter += 1
            for frag in frag_group:
                tmp = PyDict_GetItem(new_fragment_index, frag)
                if tmp == NULL:
                    acc = []
                    PyDict_SetItem(new_fragment_index, frag, acc)
                else:
                    acc = <list>tmp
                acc.append(new_id)

            for member in members:
                new_glycan_index[self.index_to_glycan[member]] = new_id
                secondary_index[member] = new_id

        self.index_to_simplified_index = secondary_index
        self.glycan_to_index = new_glycan_index
        self.fragment_index = new_fragment_index
        self.index_to_glycan = {}
        for k, v in self.glycan_to_index.items():
            tmp = PyDict_GetItem(self.index_to_glycan, v)
            if tmp == NULL:
                acc = []
                PyDict_SetItem(self.index_to_glycan, v, acc)
            else:
                acc = <list>tmp
            acc.append(k)
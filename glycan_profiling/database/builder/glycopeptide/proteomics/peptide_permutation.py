from collections import defaultdict
import itertools
from multiprocessing import Process, Queue, Event

from lxml.etree import XMLSyntaxError

from ms_deisotope.peak_dependency_network.intervals import Interval, IntervalTreeNode

from glycopeptidepy import enzyme
from .utils import slurp
from .uniprot import uniprot, get_uniprot_accession

from glypy.composition import formula
from glycopeptidepy.structure import sequence, modification, residue
from glycopeptidepy import PeptideSequence

from glycan_profiling.task import TaskBase
from glycan_profiling.serialize import DatabaseBoundOperation
from glycan_profiling.serialize.hypothesis.peptide import Peptide, Protein

RestrictedModificationTable = modification.RestrictedModificationTable
combinations = itertools.combinations
product = itertools.product
chain_iterable = itertools.chain.from_iterable

SequenceLocation = modification.SequenceLocation


def n_glycan_sequon_sites(peptide, protein, use_local_sequence=False):
    sites = set()
    sites |= set(site - peptide.start_position for site in protein.n_glycan_sequon_sites
                 if peptide.start_position <= site < peptide.end_position)
    if use_local_sequence:
        sites |= set(sequence.find_n_glycosylation_sequons(
            peptide.modified_peptide_sequence))
    return list(sites)


def o_glycan_sequon_sites(peptide, protein=None, use_local_sequence=False):
    sites = set()
    sites |= set(site - peptide.start_position for site in protein.o_glycan_sequon_sites
                 if peptide.start_position <= site < peptide.end_position)
    if use_local_sequence:
        sites |= set(sequence.find_o_glycosylation_sequons(
            peptide.modified_peptide_sequence))
    return sites


def gag_sequon_sites(peptide, protein=None, use_local_sequence=False):
    sites = set()
    sites |= set(site - peptide.start_position for site in protein.glycosaminoglycan_sequon_sites
                 if peptide.start_position <= site < peptide.end_position)
    if use_local_sequence:
        sites = sequence.find_glycosaminoglycan_sequons(
            peptide.modified_peptide_sequence)
    return sites


def get_base_peptide(peptide_obj):
    if isinstance(peptide_obj, Peptide):
        return PeptideSequence(peptide_obj.base_peptide_sequence)
    else:
        return PeptideSequence(str(peptide_obj))


class ModificationSiteAssignmentCombinator(object):
    def __init__(self, variable_site_map):
        self.modification_to_site = variable_site_map
        self.site_to_modification = self.transpose_sites()

    def transpose_sites(self):
        """Given a dictionary mapping between modification names and
        an iterable of valid sites, create a dictionary mapping between
        modification names and a list of valid sites plus the constant `None`

        Returns
        -------
        dict
        """
        sites = defaultdict(list)
        for mod, varsites in self.modification_to_site.items():
            for site in varsites:
                sites[site].append(mod)
        for site in list(sites):
            sites[site].append(None)
        return sites

    def _remove_empty_sites(self, site_mod_pairs):
        return [sm for sm in site_mod_pairs if sm[1] is not None]

    def assign(self):
        sites = list(self.site_to_modification.keys())
        choices = list(self.site_to_modification.values())
        for selected in itertools.product(*choices):
            site_mod_pairs = zip(sites, selected)
            yield self._remove_empty_sites(site_mod_pairs)

    def __iter__(self):
        return self.assign()


class PeptidePermuter(object):
    def __init__(self, constant_modifications, variable_modifications, max_variable_modifications=None):
        if max_variable_modifications is None:
            max_variable_modifications = 4
        self.constant_modifications = list(constant_modifications)
        self.variable_modifications = list(variable_modifications)
        self.max_variable_modifications = max_variable_modifications

        (self.n_term_modifications,
         self.c_term_modifications,
         self.variable_modifications) = self.split_terminal_modifications(self.variable_modifications)

    @staticmethod
    def split_terminal_modifications(modifications):
        """Group modification rules into three classes, N-terminal,
        C-terminal, and Internal modifications.

        A modification rule can be assigned to multiple groups if it
        is valid at multiple sites.

        Parameters
        ----------
        modifications : Iterable of ModificationRule
            The modification rules

        Returns
        -------
        n_terminal: list
            list of N-terminal modification rules
        c_terminal: list
            list of C-terminal modification rules
        internal: list
            list of Internal modification rules
        """
        n_terminal = []
        c_terminal = []
        internal = []

        for mod in modifications:
            n_term = mod.n_term_targets
            if n_term and all([t.amino_acid_targets is None for t in n_term]):
                n_term_rule = mod.clone(n_term)
                mod = mod - n_term_rule
                n_terminal.append(n_term_rule)
            c_term = mod.c_term_targets
            if c_term and all([t.amino_acid_targets is None for t in c_term]):
                c_term_rule = mod.clone(c_term)
                mod = mod - c_term_rule
                c_terminal.append(c_term_rule)
            if (mod.targets):
                internal.append(mod)

        return n_terminal, c_terminal, internal

    def prepare_peptide(self, sequence):
        return get_base_peptide(sequence)

    def terminal_modifications(self, sequence):
        n_term_modifications = [
            mod for mod in self.n_term_modifications if mod.find_valid_sites(sequence)]
        c_term_modifications = [
            mod for mod in self.c_term_modifications if mod.find_valid_sites(sequence)]
        # the case with unmodified termini
        n_term_modifications.append(None)
        c_term_modifications.append(None)

        return n_term_modifications, c_term_modifications

    def apply_fixed_modifications(self, sequence):
        has_fixed_n_term = False
        has_fixed_c_term = False

        for mod in self.constant_modifications:
            for site in mod.find_valid_sites(sequence):
                if site == SequenceLocation.n_term:
                    has_fixed_n_term = True
                elif site == SequenceLocation.c_term:
                    has_fixed_c_term = True
                sequence.add_modification(site, mod.name)
        return has_fixed_n_term, has_fixed_c_term

    def modification_sites(self, sequence):
        variable_sites = {
            mod.name: set(
                mod.find_valid_sites(sequence)) for mod in self.variable_modifications}
        modification_sites = ModificationSiteAssignmentCombinator(variable_sites)
        return modification_sites

    def apply_variable_modifications(self, sequence, assignments, n_term, c_term):
        n_variable = 0
        result = sequence.clone()
        if n_term is not None:
            result.n_term = n_term
            n_variable += 1
        if c_term is not None:
            result.c_term = c_term
            n_variable += 1
        for site, mod in assignments:
            if mod is not None:
                result.add_modification(site, mod)
                n_variable += 1
        return result, n_variable

    def permute_peptide(self, sequence):
        try:
            sequence = self.prepare_peptide(sequence)
        except residue.UnknownAminoAcidException:
            return
        (n_term_modifications,
         c_term_modifications) = self.terminal_modifications(sequence)

        (has_fixed_n_term,
         has_fixed_c_term) = self.apply_fixed_modifications(sequence)

        if has_fixed_n_term:
            n_term_modifications = [None]
        if has_fixed_c_term:
            c_term_modifications = [None]

        modification_sites = self.modification_sites(sequence)

        for n_term, c_term in itertools.product(n_term_modifications, c_term_modifications):
            for assignments in modification_sites:
                if len(assignments) > self.max_variable_modifications:
                    continue
                yield self.apply_variable_modifications(
                    sequence, assignments, n_term, c_term)

    def __call__(self, peptide):
        return self.permute_peptide(peptide)

    @classmethod
    def peptide_permutations(cls, sequence, constant_modifications, variable_modifications,
                             max_variable_modifications=4):
        inst = cls(constant_modifications, variable_modifications,
                   max_variable_modifications)
        return inst.permute_peptide(sequence)


peptide_permutations = PeptidePermuter.peptide_permutations


def cleave_sequence(sequence, protease, missed_cleavages=2, min_length=6, max_length=60, semispecific=False):
    for peptide, start, end, missed in protease.cleave(sequence, missed_cleavages=missed_cleavages,
                                                       min_length=min_length, max_length=max_length,
                                                       semispecific=semispecific):
        if "X" in peptide:
            continue
        yield peptide, start, end, missed


class ProteinDigestor(TaskBase):

    def __init__(self, protease, constant_modifications=None, variable_modifications=None,
                 max_missed_cleavages=2, min_length=6, max_length=60, semispecific=False,
                 max_variable_modifications=None):
        if constant_modifications is None:
            constant_modifications = []
        if variable_modifications is None:
            variable_modifications = []
        self.protease = self._prepare_protease(protease)
        self.constant_modifications = constant_modifications
        self.variable_modifications = variable_modifications
        self.peptide_permuter = PeptidePermuter(
            self.constant_modifications,
            self.variable_modifications,
            max_variable_modifications=max_variable_modifications)
        self.max_missed_cleavages = max_missed_cleavages
        self.min_length = min_length
        self.max_length = max_length
        self.semispecific = semispecific
        self.max_variable_modifications = max_variable_modifications

    def _prepare_protease(self, protease):
        if isinstance(protease, enzyme.Protease):
            pass
        elif isinstance(protease, basestring):
            protease = enzyme.Protease(protease)
        elif isinstance(protease, (list, tuple)):
            protease = enzyme.Protease.combine(*protease)
        return protease

    def cleave(self, sequence):
        return cleave_sequence(sequence, self.protease, self.max_missed_cleavages,
                               min_length=self.min_length, max_length=self.max_length,
                               semispecific=self.semispecific)

    def digest(self, protein):
        sequence = protein.protein_sequence
        for peptide, start, end, n_missed_cleavages in self.cleave(sequence):
            if end - start > self.max_length:
                continue
            for inst in self.modify_string(peptide):
                inst.count_missed_cleavages = n_missed_cleavages
                inst.start_position = start
                inst.end_position = end
                yield inst

    def modify_string(self, peptide):
        for modified_peptide, n_variable_modifications in self.peptide_permuter(peptide):
            inst = Peptide(
                base_peptide_sequence=str(peptide),
                modified_peptide_sequence=str(modified_peptide),
                count_missed_cleavages=-1,
                count_variable_modifications=n_variable_modifications,
                sequence_length=len(modified_peptide),
                start_position=-1,
                end_position=-1,
                calculated_mass=modified_peptide.mass,
                formula=formula(modified_peptide.total_composition()))
            yield inst

    def process_protein(self, protein_obj):
        protein_id = protein_obj.id
        hypothesis_id = protein_obj.hypothesis_id

        for peptide in self.digest(protein_obj):
            peptide.protein_id = protein_id
            peptide.hypothesis_id = hypothesis_id
            peptide.peptide_score = 0
            peptide.peptide_score_type = 'null_score'
            n_glycosites = n_glycan_sequon_sites(
                peptide, protein_obj)
            o_glycosites = o_glycan_sequon_sites(peptide, protein_obj)
            gag_glycosites = gag_sequon_sites(peptide, protein_obj)
            peptide.count_glycosylation_sites = len(n_glycosites)
            peptide.n_glycosylation_sites = sorted(n_glycosites)
            peptide.o_glycosylation_sites = sorted(o_glycosites)
            peptide.gagylation_sites = sorted(gag_glycosites)
            yield peptide

    def __call__(self, protein_obj):
        return self.process_protein(protein_obj)

    @classmethod
    def digest_protein(cls, sequence, protease, constant_modifications=None,
                       variable_modifications=None, max_missed_cleavages=2,
                       min_length=6, max_length=60, semispecific=False):
        inst = cls(
            protease, constant_modifications, variable_modifications,
            max_missed_cleavages, min_length, max_length, semispecific)
        if isinstance(sequence, basestring):
            sequence = Protein(protein_sequence=sequence)
        return inst(sequence)


digest = ProteinDigestor.digest_protein


class ProteinDigestingProcess(Process):

    def __init__(self, connection, hypothesis_id, input_queue, digestor, done_event=None,
                 chunk_size=5000, message_handler=None):
        Process.__init__(self)
        self.connection = connection
        self.input_queue = input_queue
        self.hypothesis_id = hypothesis_id
        self.done_event = done_event
        self.digestor = digestor
        self.chunk_size = chunk_size
        self.message_handler = message_handler

    def task(self):
        database = DatabaseBoundOperation(self.connection)
        session = database.session
        has_work = True

        digestor = self.digestor
        acc = []
        if self.message_handler is None:
            self.message_handler = lambda x: None
        while has_work:
            try:
                work_items = self.input_queue.get(timeout=5)
                if work_items is None:
                    has_work = False
                    continue
            except Exception:
                if self.done_event.is_set():
                    has_work = False
                continue
            proteins = slurp(session, Protein, work_items, flatten=False)
            acc = []

            threshold_size = 3000

            for protein in proteins:
                size = len(protein.protein_sequence)
                if size > threshold_size:
                    self.message_handler("...... Started digesting %s (%d)" % (protein.name, size))
                i = 0
                for peptide in digestor.process_protein(protein):
                    acc.append(peptide)
                    i += 1
                    if len(acc) > self.chunk_size:
                        session.bulk_save_objects(acc)
                        session.commit()
                        acc = []
                    if i % 10000 == 0:
                        self.message_handler(
                            "...... Digested %d peptides from %r (%d)" % (
                                i, protein.name, size))
                if size > threshold_size:
                    self.message_handler("...... Finished digesting %s (%d)" % (protein.name, size))
            session.bulk_save_objects(acc)
            session.commit()
            acc = []
        if acc:
            session.bulk_save_objects(acc)
            session.commit()
            acc = []

    def run(self):
        self.task()


class MultipleProcessProteinDigestor(TaskBase):
    def __init__(self, connection, hypothesis_id, protein_ids, digestor, n_processes=4):
        self.connection = connection
        self.hypothesis_id = hypothesis_id
        self.protein_ids = protein_ids
        self.digestor = digestor
        self.n_processes = n_processes

    def run(self):
        logger = self.ipc_logger()
        input_queue = Queue(20 * self.n_processes)
        done_event = Event()
        processes = [
            ProteinDigestingProcess(
                self.connection, self.hypothesis_id, input_queue,
                self.digestor, done_event=done_event,
                message_handler=logger.sender()) for i in range(
                self.n_processes)
        ]
        protein_ids = self.protein_ids
        i = 0
        n = len(protein_ids)
        chunk_size = 2
        interval = 30
        for process in processes:
            input_queue.put(protein_ids[i:(i + chunk_size)])
            i += chunk_size
            process.start()

        last = i
        while i < n:
            input_queue.put(protein_ids[i:(i + chunk_size)])
            i += chunk_size
            if i - last > interval:
                self.log("... Dealt Proteins %d-%d %0.2f%%" % (
                    i - chunk_size, min(i, n), (min(i, n) / float(n)) * 100))
                last = i

        done_event.set()
        for process in processes:
            process.join()
        logger.stop()


class PeptideInterval(Interval):
    def __init__(self, peptide):
        Interval.__init__(self, peptide.start_position, peptide.end_position, [peptide])
        self.data['peptide'] = str(peptide)


class ProteinSplitter(TaskBase):
    def __init__(self, constant_modifications=None, variable_modifications=None, min_length=6):
        if constant_modifications is None:
            constant_modifications = []
        if variable_modifications is None:
            variable_modifications = []

        self.constant_modifications = constant_modifications
        self.variable_modifications = variable_modifications
        self.min_length = min_length
        self.peptide_permuter = PeptidePermuter(
            self.constant_modifications, self.variable_modifications)

    def handle_protein(self, protein_obj):
        try:
            accession = get_uniprot_accession(protein_obj.name)
            if accession:
                try:
                    sites = self.get_split_sites(accession)
                    return self.split_protein(protein_obj, sites)
                except IOError:
                    return []
            else:
                return []
        except XMLSyntaxError:
            return []
        except Exception as e:
            self.error(
                ("An unhandled error occurred while retrieving"
                 " non-proteolytic cleavage sites"), e)
            return []

    def get_split_sites(self, accession):
        record = uniprot.get(accession)
        splittable_features = ("signal peptide", "propeptide", "initiator methionine",
                               "peptide", "transit peptide")
        split_sites = set()
        for feature in record.features:
            if feature.feature_type in splittable_features:
                split_sites.add(feature.start)
                split_sites.add(feature.end)
        try:
            split_sites.remove(0)
        except KeyError:
            pass
        return sorted(split_sites)

    def _make_split_expression(self, sites):
        return [
            (Peptide.start_position < s) & (Peptide.end_position > s) for s in sites]

    def _permuted_peptides(self, sequence):
        return self.peptide_permuter(sequence)

    def split_protein(self, protein_obj, sites=None):
        if sites is None:
            sites = []
        if not sites:
            return
        seen = set()
        sites_seen = set()
        peptides = protein_obj.peptides.all()
        peptide_intervals = IntervalTreeNode.build(map(PeptideInterval, peptides))
        for site in sites:
            overlap_region = peptide_intervals.contains_point(site - 1)
            spanned_intervals = IntervalTreeNode.build(overlap_region)
            # No spanned peptides. May be caused by regions of protein which digest to peptides
            # of unacceptable size.
            if spanned_intervals is None:
                continue
            lo = spanned_intervals.start
            hi = spanned_intervals.end
            # Get the set of all sites spanned by any peptide which spans the current query site
            spanned_sites = [s for s in sites if lo <= s <= hi]
            for i in range(1, len(spanned_sites) + 1):
                for split_sites in itertools.combinations(spanned_sites, i):
                    site_key = frozenset(split_sites)
                    if site_key in sites_seen:
                        continue
                    sites_seen.add(site_key)
                    spanning_peptides_query = spanned_intervals.contains_point(split_sites[0])
                    for site_j in split_sites[1:]:
                        spanning_peptides_query = [
                            sp for sp in spanning_peptides_query if site_j in sp
                        ]
                    spanning_peptides = []
                    for sp in spanning_peptides_query:
                        spanning_peptides.extend(sp)
                    for peptide in spanning_peptides:
                        adjusted_sites = [0] + [s - peptide.start_position for s in split_sites] + [
                            peptide.sequence_length]
                        for j in range(len(adjusted_sites) - 1):
                            begin, end = adjusted_sites[j], adjusted_sites[j + 1]
                            if end - begin < self.min_length:
                                continue
                            start_position = begin + peptide.start_position
                            end_position = end + peptide.start_position
                            if (start_position, end_position) in seen:
                                continue
                            else:
                                seen.add((start_position, end_position))
                            for modified_peptide, n_variable_modifications in self._permuted_peptides(
                                    peptide.base_peptide_sequence[begin:end]):

                                inst = Peptide(
                                    base_peptide_sequence=str(peptide.base_peptide_sequence[begin:end]),
                                    modified_peptide_sequence=str(modified_peptide),
                                    count_missed_cleavages=peptide.count_missed_cleavages,
                                    count_variable_modifications=n_variable_modifications,
                                    sequence_length=len(modified_peptide),
                                    start_position=start_position,
                                    end_position=end_position,
                                    calculated_mass=modified_peptide.mass,
                                    formula=formula(modified_peptide.total_composition()),
                                    protein_id=protein_obj.id)
                                inst.hypothesis_id = protein_obj.hypothesis_id
                                inst.peptide_score = 0
                                inst.peptide_score_type = 'null_score'
                                n_glycosites = n_glycan_sequon_sites(
                                    inst, protein_obj)
                                o_glycosites = o_glycan_sequon_sites(inst, protein_obj)
                                gag_glycosites = gag_sequon_sites(inst, protein_obj)
                                inst.count_glycosylation_sites = len(n_glycosites)
                                inst.n_glycosylation_sites = sorted(n_glycosites)
                                inst.o_glycosylation_sites = sorted(o_glycosites)
                                inst.gagylation_sites = sorted(gag_glycosites)
                                yield inst

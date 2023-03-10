# General
from collections import defaultdict
from pathlib import Path

# Typing and class templates
from dataclasses import dataclass, field
from abc import ABC, abstractmethod

# Cheminformatics
import networkx as nx
from rdkit import Chem

# OpenForceField
from openff.toolkit.topology.molecule import Molecule, Atom
from openff.toolkit.typing.engines.smirnoff import ForceField
from openff.toolkit.typing.engines.smirnoff import parameters as offtk_parameters
from openff.toolkit.utils.toolkits import RDKitToolkitWrapper, OpenEyeToolkitWrapper, AmberToolsToolkitWrapper

TOOLKITS = { # for convenience of reference
    'rdkit' : RDKitToolkitWrapper,
    'openeye' : OpenEyeToolkitWrapper,
    'ambertools' : AmberToolsToolkitWrapper
}


def generate_molecule_charges(mol : Molecule, toolkit_method : str='openeye', partial_charge_method : str='am1bcc') -> Molecule:
    '''Takes a Molecule object and computes partial charges with AM1BCC using toolkit method of choice. Returns charged molecule'''
    tk_reg = TOOLKITS.get(toolkit_method)()
    mol.assign_partial_charges( 
        partial_charge_method=partial_charge_method, 
        toolkit_registry=tk_reg
    )
    charged_mol = mol # rename for clarity
    
    # charged_mol.generate_conformers( # get some conformers to run elf10 charge method. By default, `mol.assign_partial_charges`...
    #     n_conformers=10,             # ...uses 500 conformers, but we can generate and use 10 here for demonstration
    #     rms_cutoff=0.25 * unit.angstrom,
    #     make_carboxylic_acids_cis=True,
    #     toolkit_registry=tk_reg
    # ) # very slow for large polymers! 
    # print(f'final molecular charges: {charged_mol.partial_charges}')     # note: the charged_mol has metadata about which monomers were assigned where as a result of the chemicaly info assignment.

    for atom in charged_mol.atoms:
        assert(atom.metadata['already_matched'] == True)
    
    return charged_mol 


# charge averaging methods
ChargeMap = dict[int, float] # makes typehinting clearer

class ChargeDistributionStrategy(ABC):
    '''Interface for defining how excess charge should be distributed within averaged residues
    to ensure an overall net 0 charge for each monomer fragment'''
    @abstractmethod
    def determine_distribution(self, net_charge : float, base_charges : ChargeMap, struct : nx.Graph) -> ChargeMap:
        pass

class UniformDistributionStrategy(ChargeDistributionStrategy):
    '''Simplest possible strategy, distribute any excess charge evenly among all molecules in residue
    Each charge effectively becomes an average of averages when viewed in the context of the whole polymer'''
    def determine_distribution(self, net_charge : float, base_charges: ChargeMap, struct: nx.Graph) -> ChargeMap:
        charge_offset = net_charge / len(base_charges) # net charge divided evenly amongst atoms (average of averages, effectively)
        return {sub_id : charge_offset for sub_id in base_charges}


@dataclass
class Accumulator:
    '''Compact container for accumulating averages'''
    sum : float = 0.0
    count : int = 0

    @property
    def average(self) -> float:
        return self.sum / self.count

@dataclass
class ChargedResidue:
    '''Dataclass for more conveniently storing averaged charges for a residue group'''
    charges : ChargeMap
    residue_name : str
    SMARTS : str
    mol_fragment : Chem.rdchem.Mol

    CDS : ChargeDistributionStrategy = field(default_factory=UniformDistributionStrategy) # set default strategy here

    def distrib_mono_charges(self) -> None:
        '''Distribute any excess charge amongst residue to ensure neutral, integral net charge'''
        net_charge = sum(chg for chg in self.charges.values())
        distrib = self.CDS.determine_distribution(net_charge, base_charges=self.charges, struct=self.mol_fragment)
        for sub_id, charge in self.charges.items():
            self.charges[sub_id] = charge - distrib[sub_id] # subtract respective charge offsets from each atom's partial charge


def find_repr_residues(cmol : Molecule) -> dict[str, int]:
    '''Determine names and smallest residue numbers of all unique residues in charged molecule
    Used as representatives for generating labelled SMARTS strings '''
    rep_res_nums = defaultdict(set) # numbers of representative groups for each unique residue, used to build SMARTS strings
    for atom in cmol.atoms: 
        rep_res_nums[atom.metadata['residue_name']].add(atom.metadata['residue_number']) # collect unique residue numbers

    for res_name, ids in rep_res_nums.items():
        rep_res_nums[res_name] = min(ids) # choose group with smallest id of each residue to denote representative group

    return rep_res_nums

def get_averaged_charges_orig(cmol : Molecule, monomer_data : dict[str, dict], distrib_mono_charges : bool=False) -> list[ChargedResidue]:
    '''Takes a charged molecule and a dict of monomer structure data and averages charges for each repeating residue. 
    Returns a list of ChargedResidue objects each of which holds:
        - A dict of the averaged charges by atom 
        - The name of the residue associated with the charges
        - A SMARTS string of the residue's structure'''
    rdmol = cmol.to_rdkit() # create rdkit representation of Molecule to allow for SMARTS generation
    rep_res_nums = find_repr_residues(cmol) # determine ids of representatives of each unique residue

    atom_ids_for_SMARTS = defaultdict(list)
    res_charge_accums   = defaultdict(lambda : defaultdict(Accumulator))
    for atom in cmol.atoms: # accumulate counts and charge values across matching substructures
        res_name, res_num     = atom.metadata['residue_name']   , atom.metadata['residue_number']
        substruct_id, atom_id = atom.metadata['substructure_id'], atom.metadata['pdb_atom_id']

        if res_num == rep_res_nums[res_name]: # if atom is member of representative group for any residue...
            atom_ids_for_SMARTS[res_name].append(atom_id)             # ...collect pdb id...
            rdmol.GetAtomWithIdx(atom_id).SetAtomMapNum(substruct_id) # ...and set atom number for labelling in SMARTS string

        curr_accum = res_charge_accums[res_name][substruct_id] # accumulate charge info for averaging
        curr_accum.sum += atom.partial_charge.magnitude # eschew units (easier to handle, added back when writing to XML)
        curr_accum.count += 1

    avg_charges_by_residue = []
    for res_name, charge_map in res_charge_accums.items():
        # SMARTS = rdmolfiles.MolFragmentToSmarts(rdmol, atomsToUse=atom_ids_for_SMARTS[res_name]) # determine SMARTS for the current residue's representative group
        SMARTS = monomer_data['monomers'][res_name] # extract SMARTS string from monomer data
        charge_map = {substruct_id : accum.average for substruct_id, accum in charge_map.items()} 

        if distrib_mono_charges: # distribute any excess average charge among monomer atoms to ensure no net charge per monomer
            chg_offset = sum(avg for avg in charge_map.values()) / len(charge_map)
            charge_map = {sub_id : avg - chg_offset for sub_id, avg in charge_map.items()}
        
        avg_charges_by_residue.append(ChargedResidue(charges=charge_map, residue_name=res_name, SMARTS=SMARTS))

    return avg_charges_by_residue

def get_averaged_charges(cmol : Molecule, monomer_data : dict[str, dict], distrib_mono_charges : bool=True) -> list[ChargedResidue]:
    '''Takes a charged molecule and a dict of monomer SMIRKS strings and averages charges for each repeating residue. 
    Returns a list of ChargedResidue objects, each of which holds:
        - A dict of the averaged charges by atom 
        - The name of the residue associated with the charges
        - A SMARTS string of the residue's structure
        - An nx.Graph representing the structure of the residue'''
    # rdmol = cmol.to_rdkit() # create rdkit representation of Molecule to allow for SMARTS generation
    mol_graph = cmol.to_networkx()
    rep_res_nums = find_repr_residues(cmol) # determine ids of representatives of each unique residue

    atom_id_mapping   = defaultdict(lambda : defaultdict(int))
    res_charge_accums = defaultdict(lambda : defaultdict(Accumulator))
    for atom in cmol.atoms: # accumulate counts and charge values across matching substructures
        res_name, res_num     = atom.metadata['residue_name'   ], atom.metadata['residue_number']
        substruct_id, atom_id = atom.metadata['substructure_id'], atom.metadata['pdb_atom_id'   ]

        if res_num == rep_res_nums[res_name]: # if atom is member of representative group for any residue...
            # rdmol.GetAtomWithIdx(atom_id).SetAtomMapNum(atom_id)  # ...and set atom number for labelling in SMARTS string
            atom_id_mapping[res_name][atom_id] = (substruct_id, atom.symbol) # ...collect pdb id...

        curr_accum = res_charge_accums[res_name][substruct_id] # accumulate charge info for averaging
        curr_accum.sum += atom.partial_charge.magnitude # eschew units (easier to handle, added back when writing to XML)
        curr_accum.count += 1

    avg_charges_by_residue = []
    for res_name, charge_map in res_charge_accums.items():
        # rdSMARTS = rdmolfiles.MolFragmentToSmarts(rdmol, atomsToUse=atom_id_mapping[res_name].keys()) # determine SMARTS for the current residue's representative group
        # mol_frag = rdmolfiles.MolFromSmarts(rdSMARTS) # create fragment from rdkit SMARTS to avoid wild atoms (using rdkit over nx.subgraph for more detailed atomwise info)
        
        SMARTS = monomer_data['monomers'][res_name] # extract SMARTS string from monomer data
        charge_map = {substruct_id : accum.average for substruct_id, accum in charge_map.items()} 
        atom_id_map = atom_id_mapping[res_name]

        mol_frag = mol_graph.subgraph(atom_id_map.keys()) # isolate subgraph of residue to obtain connectivity info for charge redistribution
        for atom_id, (substruct_id, symbol) in atom_id_map.items(): # assign additional useful info not present by default in graph
            mol_frag.nodes[atom_id]['substruct_id'] = substruct_id
            mol_frag.nodes[atom_id]['symbol'] = symbol

        chgd_res = ChargedResidue(
            charges=charge_map,
            residue_name=res_name,
            SMARTS=SMARTS,
            mol_fragment=mol_frag
        )
        if distrib_mono_charges: # only distribute charges if explicitly called for (enabled by default)
            chgd_res.distrib_mono_charges()
        avg_charges_by_residue.append(chgd_res)

    return avg_charges_by_residue, atom_id_mapping

def write_new_library_charges(avgs : list[ChargedResidue], offxml_src : Path, output_path : Path) -> tuple[ForceField, list[offtk_parameters.LibraryChargeHandler]]:
    '''Takes dict of residue-averaged charges to generate and append library charges to an .offxml file of choice, creating a new xml with the specified filename'''
    assert(output_path.suffix == '.offxml') # ensure output path is pointing to correct file type
    forcefield = ForceField(offxml_src)     # simpler to add library charges through forcefield API than to directly write to xml
    lc_handler = forcefield["LibraryCharges"]

    lib_chgs = [] #  all library charges generated from the averaged charges for each residue
    for averaged_res in avgs:
        lc_entry = { # stringify charges into form usable for library charges
            f'charge{cid}' : f'{charge} * elementary_charge' # +1 accounts for 1-index to 0-index when going from smirks atom ids to substructure ids
                for cid, charge in averaged_res.charges.items()
        } 

        lc_entry['smirks'] = averaged_res.SMARTS # add SMIRKS string to library charge entry to allow for correct labelling
        lc_params = offtk_parameters.LibraryChargeHandler.LibraryChargeType(allow_cosmetic_attributes=True, **lc_entry) # must enable cosmetic params for general kwarg passing
        
        lc_handler.add_parameter(parameter=lc_params)
        lib_chgs.append(lc_params)  # record library charges for reference
    forcefield.to_file(output_path) # write modified library charges to new xml (avoid overwrites in case of mistakes)
    
    return forcefield, lib_chgs
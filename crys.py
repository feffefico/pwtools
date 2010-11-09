# crys.py
#
# Crystal and unit-cell related tools. Some MD analysis tools, too.
#

from math import acos, pi, sin, cos, sqrt
from itertools import izip
import textwrap
import tempfile
import time
import os

import numpy as np
from scipy.linalg import inv
from scipy.integrate import cumtrapz

# Cif parser
try:
    import CifFile as pycifrw_CifFile
except ImportError:
    print("%s: Warning: Cannot import CifFile from the PyCifRW package. " 
    "Parsing Cif files will not work." %__file__)

from pwtools.common import assert_cond
import pwtools.common as common
import pwtools.constants as con
from pwtools.decorators import crys_add_doc
from pwtools.pwscf import atpos_str

#-----------------------------------------------------------------------------
# misc math
#-----------------------------------------------------------------------------

# np.linalg.norm handles also complex arguments, but we don't need that here. 
##norm = np.linalg.norm
def norm(a):
    """2-norm for real vectors."""
    assert_cond(len(a.shape) == 1, "input must be 1d array")
    # math.sqrt is faster then np.sqrt for scalar args
    return sqrt(np.dot(a,a))


def angle(x,y):
    """Angle between vectors `x' and `y' in degrees.
    
    args:
    -----
    x,y : 1d numpy arrays
    """
    # Numpy's `acos' is "acrcos", but we take the one from math for scalar
    # args.
    return acos(np.dot(x,y)/norm(x)/norm(y))*180.0/pi


def floor_eps(arg, copy=True):
    """sin(180 * pi/180.) == 1e-17, we want 0.0"""
    eps = np.finfo(np.float).eps
    if np.isscalar(arg):
        return 0.0 if abs(arg) < eps else arg
    else:
        if copy:
            _arg = np.asarray(arg).copy()
        else:            
            _arg = np.asarray(arg)
        _arg[np.abs(_arg) < eps] = 0.0
        return _arg


def deg2rad(x):
    return x * pi/180.0

#-----------------------------------------------------------------------------
# crystallographic constants and basis vectors
#-----------------------------------------------------------------------------

@crys_add_doc
def volume_cell(cell):
    """Volume of the unit cell from CELL_PARAMETERS. Calculates the triple
    product 
        np.dot(np.cross(a,b), c) 
    of the basis vectors a,b,c contained in `cell`. Note that (mathematically)
    the vectors can be either the rows or the cols of `cell`.

    args:
    -----
    %(cell_doc)s

    returns:
    --------
    volume, unit: [a]**3

    example:
    --------
    >>> a = [1,0,0]; b = [2,3,0]; c = [1,2,3.];
    >>> m = np.array([a,b,c])
    >>> volume_cell(m)
    9.0
    >>> volume_cell(m.T)
    9.0
    >>> m = rand(3,3)
    >>> volume_cell(m)
    0.11844733769775126
    >>> volume_cell(m.T)
    0.11844733769775123
    >>> np.linalg.det(m)
    0.11844733769775125
    >>> np.linalg.det(m.T)
    0.11844733769775125

    notes:
    ------
    %(notes_cell_crys_const)s
    """    
    assert_cond(cell.shape == (3,3), "input must be (3,3) array")
    return np.dot(np.cross(cell[0,:], cell[1,:]), cell[2,:])        


@crys_add_doc
def volume_cc(cryst_const):
    """Volume of the unit cell from crystallographic constants [1].
    
    args:
    -----
    %(cryst_const_doc)s
    
    returns:
    --------
    volume, unit: [a]**3
    
    notes:
    ------
    %(notes_cell_crys_const)s

    refs:
    -----
    [1] http://en.wikipedia.org/wiki/Parallelepiped
    """
    a = cryst_const[0]
    b = cryst_const[1]
    c = cryst_const[2]
    alpha = cryst_const[3]*pi/180
    beta = cryst_const[4]*pi/180
    gamma = cryst_const[5]*pi/180
    return a*b*c*sqrt(1+ 2*cos(alpha)*cos(beta)*cos(gamma) -\
          cos(alpha)**2 - cos(beta)**2 - cos(gamma)**2 )


@crys_add_doc
def cell2cc(cell, align='rows'):
    """From CELL_PARAMETERS to crystallographic constants a, b, c, alpha, beta,
    gamma. 
    This mapping is unique in the sense that multiple `cell`s will have
    the same `cryst_const`, i.e. the representation of the cell in
    `cryst_const` is independent from the spacial orientation of the cell
    w.r.t. a cartesian coord sys.
    
    args:
    -----
    %(cell_doc)s
    %(align_doc)s

    returns:
    --------
    %(cryst_const_doc)s, 
        unit: [a]**3

    notes:
    ------
    %(notes_cell_crys_const)s
    """
    cell = np.asarray(cell)
    assert_cond(cell.shape == (3,3), "cell must be (3,3) array")
    if align == 'cols':
        cell = cell.T
    cryst_const = np.empty((6,), dtype=float)
    # a = |a|, b = |b|, c = |c|
    cryst_const[:3] = np.sqrt((cell**2.0).sum(axis=1))
    va = cell[0,:]
    vb = cell[1,:]
    vc = cell[2,:]
    # alpha
    cryst_const[3] = angle(vb,vc)
    # beta
    cryst_const[4] = angle(va,vc)
    # gamma
    cryst_const[5] = angle(va,vb)
    return cryst_const


@crys_add_doc
def cc2cell(cryst_const):
    """From crystallographic constants a, b, c, alpha, beta,
    gamma to CELL_PARAMETERS.
    This mapping not NOT unique in the sense that one set of `cryst_const` can
    have arbitrarily many representations in terms of `cell`s. We stick to a
    common convention. See notes below.
    
    args:
    -----
    %(cryst_const_doc)s
    
    returns:
    --------
    %(cell_doc)s
        Basis vecs are the rows.
        unit: [a]**3
    
    notes:
    ------
    * %(notes_cell_crys_const)s
    
    * Basis vectors fulfilling the crystallographic constants are arbitrary
      w.r.t. their orientation in space. We choose the common convention that
        va : along x axis
        vb : in the x-y plane
      Then, vc is fixed. 
      cell = [[-- va --],
            [-- vb --],
            [-- vc --]]
    """
    a = cryst_const[0]
    b = cryst_const[1]
    c = cryst_const[2]
    alpha = cryst_const[3]*pi/180
    beta = cryst_const[4]*pi/180
    gamma = cryst_const[5]*pi/180
    va = np.array([a,0,0])
    vb = np.array([b*cos(gamma), b*sin(gamma), 0])
    # vc must be calculated:
    # cx: projection onto x axis (va)
    cx = c*cos(beta)
    # Now need cy and cz ...
    #
    # Maxima solution
    #   
    ## vol = volume_cc(cryst_const)
    ## cz = vol / (a*b*sin(gamma))
    ## cy = sqrt(a**2 * b**2 * c**2 * sin(beta)**2 * sin(gamma)**2 - \
    ##     vol**2) / (a*b*sin(gamma))
    ## cy2 = sqrt(c**2 - cx**2 - cz**2)
    #
    # PWscf , WIEN2K's sgroup, results are the same as with Maxima but the
    # formulas are shorter :)
    cy = c*(cos(alpha) - cos(beta)*cos(gamma))/sin(gamma)
    cz = sqrt(c**2 - cy**2 - cx**2)
    vc = np.array([cx, cy, cz])
    return np.array([va, vb, vc])


@crys_add_doc
def recip_cell(cell, align='rows'):
    """Reciprocal lattice vectors.
        {a,b,c}* = 2*pi / V * {b,c,a} x {c, a, b}
    
    The volume is calculated using `cell`, so make sure that all units match.

    args:
    -----
    %(cell_doc)s
    %(align_doc)s

    returns:
    --------
    Shape (3,3) numpy array with reciprocal vectors as rows.

    notes:
    ------
    %(notes_cell_crys_const)s

    The unit of the recip. vecs is 1/[cell] and the unit of the volume is
    [cell]**3.
    """
    cell = np.asarray(cell)
    assert_cond(cell.shape == (3,3), "cell must be (3,3) array")
    if align == 'cols':
        cell = cell.T
    cell_recip = np.empty_like(cell)
    vol = volume_cell(cell)
    a = cell[0,:]
    b = cell[1,:]
    c = cell[2,:]
    cell_recip[0,:] = 2*pi/vol * np.cross(b,c)
    cell_recip[1,:] = 2*pi/vol * np.cross(c,a)
    cell_recip[2,:] = 2*pi/vol * np.cross(a,b)
    return cell_recip

#-----------------------------------------------------------------------------
# super cell building
#-----------------------------------------------------------------------------

def scell_mask(dim1, dim2, dim3):
    """Build a mask for the creation of a dim1 x dim2 x dim3 supercell (for 3d
    coordinates).  Return all possible permutations with repitition of the
    integers n1, n2, n3, and n1, n2, n3 = 0, ..., dim1-1, dim2-1, dim3-1 .

    args:
    -----
    dim1, dim2, dim3 : int

    returns:
    --------
    mask : 2d array, shape (dim1*dim2*dim3, 3)

    example:
    --------
    >>> # 2x2x2 supercell
    >>> scell_mask(2,2,2)
    array([[ 0.,  0.,  0.],
           [ 0.,  0.,  1.],
           [ 0.,  1.,  0.],
           [ 0.,  1.,  1.],
           [ 1.,  0.,  0.],
           [ 1.,  0.,  1.],
           [ 1.,  1.,  0.],
           [ 1.,  1.,  1.]])
    >>> # a "plane" of 4 cells           
    >>> scell_mask(2,2,1)
    array([[ 0.,  0.,  0.],
           [ 0.,  1.,  0.],
           [ 1.,  0.,  0.],
           [ 1.,  1.,  0.]])
    
    notes:
    ------
    If dim1 == dim2 == dim3 == n, then we have a permutation with repetition
    (german: Variation mit Wiederholung):  select r elements out of n with
    rep. In gerneral, n >= r or n < r possible. There are always n**r
    possibilities.
    Here r = 3 always (select x,y,z direction):
    example:
    n=2 : {0,1}   <=> 2x2x2 supercell: 
      all 3-tuples out of {0,1}   -> n**r = 2**3 = 8
    n=3 : {0,1,2} <=> 3x3x3 supercell:
      all 3-tuples out of {0,1,2} -> n**r = 3**3 = 27
    Computationally, we need `r` nested loops (or recursion of depth 3), one
    per dim.  
    """
    b = [] 
    for n1 in range(dim1):
        for n2 in range(dim2):
            for n3 in range(dim3):
                b.append([n1,n2,n3])
    return np.array(b, dtype=float)


@crys_add_doc
def raw_scell(coords, mask, symbols, behave='new'):
    """Build supercell based on `mask`. This function does only translate the
    atomic coords. it does NOT do anything with the crystal axes. See scell()
    for that.

    NOTE: This is depretacted. The functionality (behave='new') is built into
    scell() now.

    args:
    -----
    coords : 2d array, (natoms, 3) with atomic positions in *crystal* (fractional)
        coordinates (i.e. in units of the basis vecs in `cell`, for instance in .cif
        files _atom_site_fract_*), these represent the initial single unit cell
    mask : what scell_mask() returns, (N, 3)
    symbols : list of strings with atom symbols, (natoms,), must match with the
        rows of coords
    behave : str, {'new', 'old'}        

    returns:
    --------
    (symbols, coords)
    symbols : list of strings with atom symbols, (N*natoms,)
    coords : array (N*natoms, 3)
        Atomic crystal coords in the super cell w.r.t the original cell, 
        i.e. the numbers are not in [0,1], but in [0, max(dims)].
    """
    nmask = mask.shape[0]
    natoms = coords.shape[0]
    if behave == 'new':   
        sc_symbols = np.array(symbols).repeat(nmask).tolist()   
        sc_coords = (coords[:,None] + mask[None,:]).reshape(natoms*nmask,3)
    elif behave == 'old':        
        sc_symbols = []
        sc_coords = np.empty((nmask*natoms, 3), dtype=float)
        k = 0
        for iatom in range(coords.shape[0]):
            for j in range(nmask):
                sc_symbols.append(symbols[iatom])  
                sc_coords[k,:] = coords[iatom,:] + mask[j,:]
                k += 1
    else:
        raise
    return sc_symbols, sc_coords


@crys_add_doc
def scell(coords, cell, dims, symbols, align='rows'):
    """Build supercell based on `dims`. It scales the unit cell to the dims of
    the super cell and returns crystal atomic positions w.r.t. this cell.
    
    args:
    -----
    coords : 2d array, (natoms, 3) with atomic positions in *crystal* coordinates
        (i.e. in units of the basis vecs in `cell`), these represent the initial
        single unit cell
    %(cell_doc)s
    dims : tuple (nx, ny, nz) for a N = nx * ny * nz supercell
    symbols : list of strings with atom symbols, (natoms,), must match with the
        rows of coords
    %(align_doc)s

    returns:
    --------
    dict {symbols, coords, cell}
    symbols : list of strings with atom symbols for the supercell, (N*natoms,)
    coords : array (N*natoms, 3)
        Atomic crystal coords in the super cell w.r.t `cell`, i.e.
        the numbers are in [0,1].
    cell : array (3,3), basis vecs of the super cell        
    """
    assert_cond(cell.shape == (3,3), "cell must be (3,3) array")
    if align == 'cols':
        cell = cell.T
    mask = scell_mask(*tuple(dims))
    
    # Place each atom N = dim1*dim2*dim3 times in the
    # supercell, i.e. copy unit cell N times. Actually, N-1, since
    # n1=n2=n3=0 is the unit cell itself.
    #
    # mask[j,:] = [n1, n2, n3], ni = integers (floats actually, but
    #   mod(ni, floor(ni)) == 0.0)
    #
    # original cell:
    # coords[i,:] = r_i = position vect of atom i in the unit cell in *crystal*
    #           coords!!
    # 
    # super cell:
    # r*_i = r_i + [n1, n2, n3]
    #   for all permutations (see scell_mask()) of n1, n2, n3.
    #   ni = 0, ..., dim_i - 1, i = 1,2,3
    #
    # sc_coords : crystal coords w.r.t the *old* cell, i.e. the entries are in
    # [0,(max(dims))], not [0,1], is scaled below
    #
    ##sc_symbols, sc_coords = raw_scell(coords, mask, symbols, behave='new')
    nmask = mask.shape[0]
    natoms = coords.shape[0]
    sc_symbols = np.array(symbols).repeat(nmask).tolist()   
    sc_coords = (coords[:,None] + mask[None,:]).reshape(natoms*nmask,3)
    # scale cell acording to super cell dims
    sc_cell = cell * np.asarray(dims)[:,None]
    # Rescale crystal coords to new bigger cell (coord_trans
    # actually) -> all values in [0,1] again
    sc_coords[:,0] /= dims[0]
    sc_coords[:,1] /= dims[1]
    sc_coords[:,2] /= dims[2]
    return {'symbols': sc_symbols, 'coords': sc_coords, 
            'cell': sc_cell}

#-----------------------------------------------------------------------------
# file parsers / converters
#-----------------------------------------------------------------------------

@crys_add_doc
def wien_sgroup_input(lat_symbol, symbols, atpos_crystal, cryst_const):
    """Generate input for WIEN2K's sgroup tool.

    args:
    -----
    lat_symbol : str, e.g. 'P'
    symbols : list of strings with atom symbols, (atpos_crystal.shape[0],)
    atpos_crystal : array_like (natoms, 3), crystal ("fractional") atomic
        coordinates
    %(cryst_const_doc)s

    notes:
    ------
    From sgroup's README:

    / ------------------------------------------------------------
    / in input file symbol "/" means a comment
    / and trailing characters are ignored by the program

    / empty lines are allowed

    P  /type of lattice; choices are P,F,I,C,A

    /  parameters of cell:
    /  lengths of the basis vectors and
    /  angles (degree unit is used)  alpha=b^c  beta=a^c  gamma=a^b
    /   |a|  |b|   |c|               alpha  beta  gamma

       1.0   1.1   1.2                90.   91.    92.

    /Number of atoms in the cell
    4

    /List of atoms
    0.1 0.2 0.3  / <-- Atom positions in units of the vectors a b c
    Al           / <-- name of this atom

    0.1 0.2 0.4  /....
    Al1

    0.2 0.2 0.3
    Fe

    0.1 0.3 0.3
    Fe

    / ------------------------------------------------------------------
    """
    atpos_crystal = np.asarray(atpos_crystal)
    assert_cond(len(symbols) == atpos_crystal.shape[0], 
        "len(symbols) != atpos_crystal.shape[0]")
    empty = '\n\n'
    txt = "/ lattice type symbol\n%s" %lat_symbol
    txt += empty
    txt += "/ a b c alpha beta gamma\n"
    txt += " ".join(["%.16e"]*6) % tuple(cryst_const)
    txt += empty
    txt += "/ number of atoms\n%i" %len(symbols)
    txt += empty
    txt += "/ atom list (crystal cooords)\n"
    fmt = ' '.join(['%.16e']*3)
    for sym, coord in izip(symbols, atpos_crystal):
        txt += fmt % tuple(coord) + '\n' + sym + '\n'
    return txt


@crys_add_doc
def write_cif(filename, coords, symbols, cryst_const, fac=con.a0_to_A, conv=False):
    """Q'n'D Cif writer. Should be a method of parse.StructureFileParser ....
    stay tuned.
    
    args:
    -----
    filename : str
        name of output .cif file
    coords : array (natoms, 3)
        crystal (fractional) coords
    symbols : list of strings
        atom symbols
    %(cryst_const_doc)s
    fac : conv factor Bohr -> Ang (.cif wants Angstrom)
    conv: bool
        Convert cryst_const[:3] to Ang
    """
    cf = pycifrw_CifFile.CifFile()
    block = pycifrw_CifFile.CifBlock()
    symbols = list(symbols)

    # Bohr -> A
    if conv:
        # nasty trick, make local var with same name, otherwise, 'cryst_const'
        # in global scope (module level) gets changed!
        cryst_const = cryst_const.copy()
        cryst_const[:3] *= fac
    # cell
    #
    # dunno why I have to use str() here, assigning floats does not work
    block['_cell_length_a'] = str(cryst_const[0])
    block['_cell_length_b'] = str(cryst_const[1])
    block['_cell_length_c'] = str(cryst_const[2])
    block['_cell_angle_alpha'] = str(cryst_const[3])
    block['_cell_angle_beta'] = str(cryst_const[4])
    block['_cell_angle_gamma'] = str(cryst_const[5])
    block['_symmetry_space_group_name_H-M'] = 'P 1'
    block['_symmetry_Int_Tables_number'] = 1
    # assigning a list produces a "loop_"
    block['_symmetry_equiv_pos_as_xyz'] = ['x,y,z']
    
    # atoms
    #
    # _atom_site_label: We just use symbols, which is then =
    #   _atom_site_type_symbol, but we *could* use that to number atoms of each
    #   specie, e.g. Si1, Si2, ..., Al1, Al2, ...
    data_names = ['_atom_site_label', 
                  '_atom_site_fract_x',
                  '_atom_site_fract_y',
                  '_atom_site_fract_z',
                  '_atom_site_type_symbol']
    data = [symbols, 
            coords[:,0].tolist(), 
            coords[:,1].tolist(), 
            coords[:,2].tolist(),
            symbols]
    # "loop_" with multiple columns            
    block.AddCifItem([[data_names], [data]])                
    cf['pwtools'] = block
    common.file_write(filename, str(cf))


@crys_add_doc
def write_xyz(filename, coords, cell, symbols, align='rows', name='pwtools_dummy_mol_name'):
    """Write VMD-style [VMD] XYZ file.
    
    B/c we require `coords` to be fractional, we need `cell` to transform to
    cartesian Angstrom.
    
    args:
    -----
    filename : target file name
    coords : 2d (one unit cell) or 3d array (e.g. MD trajectory)
        crystal (fractional) coords,
        2d: (natoms, 3)
        3d: (natoms, 3, nstep)
    %(cell_doc)s 
        In Angstrom units.
    symbols : list of strings (natoms,)
        atom symbols
    %(align_doc)s
    name : str, optional
        Molecule name.

    refs:
    -----
    [VMD] http://www.ks.uiuc.edu/Research/vmd/plugins/molfile/xyzplugin.html
    """
    if align == 'cols':
        cell = cell.T
    is3d = coords.ndim == 3
    atoms_axis = 0
    time_axis = -1
    natoms = coords.shape[atoms_axis]
    if is3d:
        nstep = coords.shape[time_axis]
        sl = [slice(None)]*3
        # (natoms, 3, nstep) -> (natoms, nstep, 3) -> transform -> (natoms, nstep, 3)
        # -> (natoms, 3, nstep)
        coords_cart = np.dot(coords.swapaxes(-1,-2), cell).swapaxes(-1,-2)
    else:
        nstep = 1
        sl = [slice(None)]*2
        coords_cart = np.dot(coords, cell)
    xyz_str = ""
    for istep in range(nstep):
        if is3d:
            sl[time_axis] = istep
        xyz_str += "%i\n%s\n%s\n" %(natoms,
                                  name + '.%i' %(istep + 1),
                                  atpos_str(symbols, coords_cart[sl]),
                                  )
    common.file_write(filename, xyz_str)


@crys_add_doc
def write_axsf(filename, coords, cell, symbols, align='rows'):
    """Write animated XSF file. ATM, only fixed cells, i.e. `cell` cannot be 3d
    array, in pwscf: md, relax, not vc-md, vc-relax. Forces are all set to
    zero.

    Note that `cell` must be in Angstrom, not the usual PWscf style scaled `cell`
    in alat units.
    
    args:
    -----
    filename : target file name
    coords : 2d (one unit cell) or 3d array (e.g. MD trajectory)
        crystal (fractional) coords,
        2d: (natoms, 3)
        3d: (natoms, 3, nstep)
    %(cell_doc)s 
        In Angstrom units.
    symbols : list of strings (natoms,)
        atom symbols
    %(align_doc)s

    refs:
    -----
    [XSF] http://www.xcrysden.org/doc/XSF.html
    """
    # notes:
    # ------
    # The XSF spec [XSF] is a little fuzzy about what PRIMCOORD actually is
    # (fractional or cartesian Angstrom). Only the latter case results in a
    # correctly displayed structure in xcrsyden. So we use that.
    #
    # We could extend this to variable cell by allowing `cell` to be 3d and
    # accept an 3d array for forces, too. Then we had (together with
    # parse.Pw*OutputFile) a replacement for pwo2xsf.sh .
    if align == 'cols':
        cell = cell.T
    atoms_axis = 0
    time_axis = -1
    xyz_axis = 1
    is3d = coords.ndim == 3
    natoms = coords.shape[atoms_axis]
    if is3d:
        nstep = coords.shape[time_axis]
        sl = [slice(None)]*3
        # (natoms, 3, nstep) -> (natoms, nstep, 3) -> transform -> (natoms, nstep, 3)
        # -> (natoms, 3, nstep)
        coords_cart = np.dot(coords.swapaxes(-1,-2), cell).swapaxes(-1,-2)
    else:
        nstep = 1
        sl = [slice(None)]*2
        coords_cart = np.dot(coords, cell)
    coords_cart = np.concatenate((coords_cart, 
                                  np.zeros_like(coords_cart)),
                                  axis=xyz_axis)
    axsf_str = "ANIMSTEPS %i\nCRYSTAL\nPRIMVEC\n%s" %(nstep, common.str_arr(cell))
    for istep in range(nstep):
        if is3d:
            sl[time_axis] = istep
        axsf_str += "\nPRIMCOORD %i\n%i 1\n%s" %(istep+1, natoms, 
                                                 atpos_str(symbols,
                                                           coords_cart[sl]))
    common.file_write(filename, axsf_str)

#-----------------------------------------------------------------------------
# atomic coords processing / evaluation, MD analysis
#-----------------------------------------------------------------------------


def rms(arr, nitems='all'):
    """RMS of all elements in a ndarray.
    
    args:
    -----
    arr : ndarray
    nitems : {'all', float)
        normalization constant, the sum of squares is divided by this number,
        set to unity for no normalization, if 'all' then use nitems = number of
        elements in the array

    returns:
    --------
    rms : scalar
    """
    if nitems == 'all':
        nitems = float(arr.nbytes / arr.itemsize)
    else:
        nitems = float(nitems)
    rms = np.sqrt((arr**2.0).sum() / nitems)
    return rms        


def rms3d(arr, axis=-1, nitems='all'):
    """RMS of 3d array along `axis`. Sum all elements of all axes != axis.
    
    args:
    -----
    arr : 3d array
    axis : int
        The axis along which the RMS of all sub-arrays is to be computed.
    nitems : {'all', float)
        normalization constant, the sum of squares is divided by this number,
        set to unity for no normalization, if 'all' then use nitems = number of
        elements in each sub-array along `axis`
    
    returns:
    --------
    rms : 1d array, (arr.shape[axis],)
    """
    assert -1 <= axis <= 2, "allowed axis values: -1,0,1,2"
    assert arr.ndim == 3, "arr must be 3d array"
    if axis == -1:
        axis = arr.ndim - 1
    if nitems == 'all':
        sl = [slice(None)]*arr.ndim
        sl[axis] = 0 # pick out 1st sub-array along axis
        nitems = float(arr[sl].nbytes / arr.itemsize)
    else:
        nitems = float(nitems)
    if axis == 0:
        rms =  np.sqrt((arr**2.0).sum(1).sum(1) / nitems)
    elif axis == 1:
        rms =  np.sqrt((arr**2.0).sum(0).sum(1) / nitems)
    elif axis == 2:
        rms =  np.sqrt((arr**2.0).sum(0).sum(0) / nitems)
    return rms        
    

def rmsd(coords_cart, ref_idx=0, axis=-1):
    """Root mean square distance of an MD trajectory of a whole atomic
    structure. The normalization constant is the number of atoms
    (coords_cart.shape[0]).
    
    args:
    -----
    coords_cart : 3d array 
        atom coords, time axis `axis`, natoms axis must be 0
    ref_idx : time index of the reference structure (i.e. 0 to compare with the
        start structure).
    axis : int
        time axis in `coords_cart`
    
    returns:
    --------
    rmsd : 1d array (coords_cart.shape[axis],)

    examples:
    ---------
    # The RMSD w.r.t. the start structure.
    >>> corrds=rand(10,3,500)
    >>> rmsd(coords, ref_idx=0, axis=-1)
    # For a relaxation run, the RMSD w.r.t. the final converged structure. The
    # RMSD should converge to zero here.
    >>> rmsd(coords, ref_idx=-1, axis=-1)
    """
    # sl_ref : pull out 2d array of coords of the reference structure
    # sl_newaxis : slice to broadcast (newaxis) this 2d array to 3d for easy
    #     substraction
    assert coords_cart.ndim == 3
    ndim = 3
    R = coords_cart.copy()
    sl_ref = [slice(None)]*ndim
    sl_ref[axis] = ref_idx
    sl_newaxis = [slice(None)]*ndim
    sl_newaxis[axis] = None
    ref = R[sl_ref].copy()
    R -= ref[sl_newaxis]
    N = float(R.shape[0])
    return rms3d(R, axis=axis, nitems=N)


#FIXME implement axis kwarg, get rid of loops
##def max_displacement(coords_cart):
##    R = coords_cart
##    md = np.empty((R.shape[0], R.shape[2]), dtype=float)
##    # iatom
##    for i in range(R.shape[0]):
##        # x,y,z
##        for j in range(R.shape[2]):
##            md[i,j] = R[i,:,j].max() - R[i,:,j].min()
##    return md            


def pbc_wrap(coords, copy=True, mask=[True]*3, xyz_axis=-1):
    """Apply periodic boundary conditions. Wrap atoms with fractional coords >
    1 or < 0 into the cell.
    
    args:
    -----
    coords : array 2d or 3d
        fractional coords, if 3d then one axis is assumed to be a time axis and
        the array is a MD trajectory or such
    copy : bool
        Copy coords before applying pbc.     
    mask : sequence of bools, len = 3 for x,y,z
        Apply pbc only x, y or z. E.g. [True, True, False] would not wrap the z
        coordinate.
    xyz_axis : the axis of `coords` where the indices 0,1,2 pull out the x,y,z
        coords. For a usual 2d array of coords with shape (natoms,3), 
        xyz_axis=1 (= last axis = -1). For a 3d array (natoms, nstep, 3),
        xyz_axis=2 (also -1).
    
    returns:
    --------
    coords with all values in [0,1] except for those where mask[i] = False.

    notes:
    ------
    About the copy arg: If copy=False, then this is an in-place operation and
    the array in the global scope is modified! In fact, then these do the same:
    >>> a = pbc_wrap(a, copy=False)
    >>> pbc_wrap(a, copy=False)
    """
    assert coords.shape[xyz_axis] == 3, "dim of xyz_axis of `coords` must be == 3"
    ndim = coords.ndim
    assert ndim in [2,3], "coords must be 2d or 3d array"
    tmp = coords.copy() if copy else coords
    for i in range(3):
        if mask[i]:
            sl = [slice(None)]*ndim
            sl[xyz_axis] = i
            tmp[sl] %= 1.0
    return tmp        


def coord_trans(coords, old=None, new=None, copy=True, align='cols'):
    """Coordinate transformation.
    
    args:
    -----
    coords : array (d0, d1, ..., M) 
        Array of arbitrary rank with coordinates (length M vectors) in old
        coord sys `old`. The only shape resiriction is that the last dim must
        equal the number of coordinates (coords.shape[-1] == M == 3 for normal
        3-dim x,y,z). 
            1d : trivial, transform that vector (length M)
            2d : The matrix must have shape (N,M), i.e. the vectors to be
                transformed are the *rows*.
            3d : coords must have the shape (..., M)                 
    old, new : 2d arrays
        matrices with the old and new basis vectors as rows or cols
    copy : bool, optional
        True: overwrite `coords`
        False: return new array
    align : string
        {'cols', 'rows'}
        cols : basis vecs are columns of `old` and `new`
        rows : basis vecs are rows    of `old` and `new`

    returns:
    --------
    array of shape = coords.shape, coordinates in system `new`
    
    examples:
    ---------
    # Taken from [1].
    >>> import numpy as np
    >>> import math
    >>> v = np.array([1.0,1.5])
    >>> I = np.identity(2)
    >>> X = math.sqrt(2)/2.0*np.array([[1,-1],[1,1]])
    >>> Y = np.array([[1,1],[0,1]])
    >>> coord_trans(v,I,I)
    array([ 1. ,  1.5])
    >>> v_X = coord_trans(v,I,X)
    >>> v_Y = coord_trans(v,I,Y)
    >>> v_X
    array([ 1.76776695,  0.35355339])
    >>> v_Y
    array([-0.5,  1.5])
    >>> coord_trans(v_Y,Y,I)
    array([ 1. ,  1.5])
    >>> coord_trans(v_X,X,I)
    array([ 1. ,  1.5])
    
    >>> coordsold = np.random.rand(30,200,3)
    >>> old = np.random.rand(3,3)
    >>> new = np.random.rand(3,3)
    >>> coordsnew = coord_trans(coordsold, old=old, new=new)
    >>> coordsold2 = coord_trans(coordsnew, old=new, new=old)
    >>> np.testing.assert_almost_equal(coordsold, coordsold2)
    
    # these do the same: A, B have vecs as rows
    >>> coordsB1=coord_trans(coordsold, old=old, new=new, align='rows') 
    >>> coordsB2=coord_trans(coordsold, old=old.T, new=new.T, align='cols') 
    >>> np.testing.assert_almost_equal(coordsold, coordsold2)
    
    # If you have an array of shape, say (10,3,100), i.e. the last dimension is
    # NOT 3, then use numpy.swapaxes():
    >>> coord_trans(arr.swapaxes(1,2), old=..., new=...).swapaxes(1,2)

    refs:
    [1] http://www.mathe.tu-freiberg.de/~eiermann/Vorlesungen/HM/index_HM2.htm
        Kapitel 6
    """ 
    # Coordinate transformation:
    # --------------------------
    #     
    # From the textbook:
    # X, Y square matrices with basis vecs as *columns*.
    #
    # X ... old, shape: (3,3)
    # Y ... new, shape: (3,3)
    # I ... identity matrix, basis vecs of cartesian system, shape: (3,3)
    # A ... transformation matrix, shape(3,3)
    # v_X ... column vector v in basis X, shape: (3,1)
    # v_Y ... column vector v in basis Y, shape: (3,1)
    # v_I ... column vector v in basis I, shape: (3,1)
    #
    # "." denotes matrix multiplication (i.e. dot() in numpy).
    #     
    #     Y . v_Y = X . v_X = I . v_I = v_I
    #     v_Y = Y^-1 . X . v_X = A . v_X
    #
    #     v_Y^T = (A . v_X)^T = v_Y^T . A^T
    # 
    # Every product X . v_X, Y . v_Y, v_I . I (in general [basis] .
    # v_[basis]) is actually an expansion of v_{X,Y,...} in the basis vectors
    # contained in X,Y,... . If the dot product is computed, we always get v in
    # cartesian coords. 
    # 
    # Now, v_X^T is a row(!) vector (1,M). This form is implemented here (see
    # below for why). With
    #     
    #     A^T = [[--- a0 ---], 
    #            [--- a1 ---], 
    #            [--- a2 ---]] 
    # 
    # we have
    #
    #     v_Y^T = v_X^T . A^T = 
    #
    #       = v_X[0]*a0       + v_X[1]*a1       + v_X[2]*a2
    #       
    #       = v_X[0]*A.T[0,:] + v_X[1]*A.T[1,:] + v_X[2]*A.T[2,:]
    #       
    #       = [v_X[0]*A.T[0,0] + v_X[1]*A.T[1,0] + v_X[2]*A.T[2,0],
    #          v_X[0]*A.T[0,1] + v_X[1]*A.T[1,1] + v_X[2]*A.T[2,1],
    #          v_X[0]*A.T[0,2] + v_X[1]*A.T[1,2] + v_X[2]*A.T[2,2]]
    #       
    #       = dot(A, v_X)         <=> v_Y[i] = sum(j=0..2) A[i,j]*v_X[j]
    #       = dot(v_X, A.T)       <=> v_Y[j] = sum(i=0..2) v_X[i]*A[i,j]
    # 
    # numpy note: In numpy A.T is the transpose. `v` is actually an 1d array
    # for which v.T == v, i.e. the transpose is not defined and so dot(A, v_X)
    # == dot(v_X, A.T).
    #
    # In general, if we don't have one vector `v` but an array R (N,M) of row
    # vectors:
    #     
    #     R = [[--- r0 ---],
    #          [--- r1 ---],
    #          ...
    #          [-- rN-1 --]]
    #
    # it's more practical to use dot(R,A.T) instead of dot(A,R) b/c of numpy
    # array broadcasting.
    #         
    # shape of `R`:
    # -------------
    #     
    # If we want to use fast numpy array broadcasting to transform many `v`
    # vectors at once, we must use the form dot(R,A.T) or, well, transform R to
    # have the vectors as cols: dot(A,R.T)).T .
    # The shape of `R` doesn't matter, as long as the last dimension matches
    # the dimensions of A (e.g. R: shape = (n,m,3), A: (3,3), dot(R,A.T): shape
    # = (n,m,3)).
    #  
    # 1d: R.shape = (3,)
    # R == v = [x,y,z] 
    # -> dot(A, v) == dot(v,A.T) = [x', y', z']
    #
    # 2d: R.shape = (N,3)
    # Array of coords of N atoms, R[i,:] = coord of i-th atom. The dot
    # product is broadcast along the first axis of R (i.e. *each* row of R is
    # dot()'ed with A.T).
    # R = 
    # [[x0,       y0,     z0],
    #  [x1,       y1,     z1],
    #   ...
    #  [x(N-1),   y(N-1), z(N-1)]]
    # -> dot(R,A.T) = 
    # [[x0',     y0',     z0'],
    #  [x1',     y1',     z1'],
    #   ...
    #  [x(N-1)', y(N-1)', z(N-1)']]
    # 
    # 3d: R.shape = (natoms, nstep, 3) 
    # R[i,j,:] is the shape (3,) vec of coords for atom i at time step j.
    # Broadcasting along the first and second axis. 
    # These loops have the same result as newR=dot(R, A.T):
    #     # New coords in each (nstep, 3) matrix R[i,...] containing coords
    #     # of atom i for each time step. Again, each row is dot()'ed.
    #     for i in xrange(R.shape[0]):
    #         newR[i,...] = dot(R[i,...],A.T)
    #     
    #     # same as example with 2d array: R[:,j,:] is a matrix with atom
    #     # coord on each row at time step j
    #     for j in xrange(R.shape[1]):
    #             newR[:,j,:] = dot(R[:,j,:],A.T)
                 
    common.assert_cond(old.ndim == new.ndim == 2, "`old` and `new` must be rank 2 arrays")
    common.assert_cond(old.shape == new.shape, "`old` and `new` must have th same shape")
    msg = ''        
    if align == 'rows':
        old = old.T
        new = new.T
        msg = 'after transpose, '
    common.assert_cond(coords.shape[-1] == old.shape[0], 
                       "%slast dim of `coords` must match first dim"
                       " of `old` and `new`" %msg)
    if copy:
        tmp = coords.copy()
    else:
        tmp = coords
    # must use `tmp[:] = ...`, just `tmp = ...` is a new array
    tmp[:] = np.dot(tmp, np.dot(inv(new), old).T)
    return tmp


def min_image_convention(sij, copy=False):
    """Helper function for rpdf(). Apply minimum image convention to
    differences of fractional coords.
    
    args:
    -----
    sij : ndarray
    copy : bool, optional

    returns:
    --------
    sij in-place modified or copy
    """
    _sij = sij.copy() if copy else sij
    _sij[_sij > 0.5] -= 1.0
    _sij[_sij < -0.5] += 1.0
    return _sij


@crys_add_doc
def rmax_smith(cell, align='cols'):
    """Helper function for rpdf(). Calculate rmax as in [Smith].
    The cell vecs must be the rows of `cell`.

    args:
    -----
    %(cell_doc)s
    %(align_doc)s

    returns:
    --------
    rmax : float

    refs:
    -----
    [Smith] W. Smith, The Minimum Image Convention in Non-Cubic MD Cells,
            http://citeseerx.ist.psu.edu/viewdoc/summary?doi=10.1.1.57.1696
            1989
    """
    if align == 'cols':
        cell = cell.T
    a = cell[:,0]
    b = cell[:,1]
    c = cell[:,2]
    bxc = np.cross(b,c)
    cxa = np.cross(c,a)
    axb = np.cross(a,b)
    wa = abs(np.dot(a, bxc)) / norm(bxc)
    wb = abs(np.dot(b, cxa)) / norm(cxa)
    wc = abs(np.dot(c, axb)) / norm(axb)
    rmax = 0.5*min(wa,wb,wc)
    return rmax


@crys_add_doc
def rpdf(coords, cell, dr, rmax='auto', tslice=slice(None), align='rows', 
         pbc=True, full_output=False):
    """Radial pair distribution (pair correlation) function. This is for one
    atomic structure (2d arrays) or a MD trajectory (3d arrays). Can also handle
    non-orthorhombic unit cells (simulation boxes).
    
    args:
    -----
    coords : one array [2d (natoms, 3) or 3d (natoms, 3,  nstep)] or a
        sequence of 2 such arrays 
        Crystal coords. If it is a sequence, then the RPDF of the 2nd coord set
        (coords[1]) w.r.t. to the first (coords[0]) is calculated, i.e. the
        order matters! This is like selection 1 and 2 in VMD.
    %(cell_doc)s
    dr : float
        Radius spacing. Must have the same unit as `cell`, e.g. Angstrom.
    rmax : {'auto', float}, optional
        Max. radius up to which minimum image nearest neighbors are counted.
        For cubic boxes of side length L, this is L/2 [AT,MD].
        'auto': the method of [Smith] is used to calculate the max. sphere
            raduis for any cell shape
        float: set value yourself
    tslice : slice object, optional
        Slice for the time axis if coords had 3d arrays.
    %(align_doc)s 
        optional
    pbc : bool, optional
        apply minimum image convention
    full_output : bool, optional

    returns:
    --------
    (rad, hist, (number_integral, rmax_auto))
    rad : 1d array, radius (x-axis) with spacing `dr`, each value r[i] is the
        middle of a histogram bin 
    hist : 1d array, (len(rad),)
        the function values g(r)
    if full_output:
        number_integral : 1d array, (len(rad),)
            number_density*hist*4*pi*r**2.0*dr
        rmax_auto : float
            auto-calculated rmax, even if not used (i.e. rmax is set from
            input)
    
    examples:
    ---------
    # 2 selections (O and H atoms, time step 3000 to end)
    >>> pp = parse.PwOutputFile(...)
    >>> pp.parse()
    # lattice constant, assume cubic box
    >>> alat = 5
    >>> cell = np.identity(3)*alat
    # transform to crystal coords (simple for cubic box, can also use 
    # coord_trans()), result is 3d array (natoms, 3, nstep)
    >>> coords = pp.coords / alat
    # make selections, numpy rocks!
    >>> sy = np.array(pp.infile.symbols)
    >>> msk1 = sy=='O'; msk2 = sy=='H'
    # do time slice here or with `tslice` kwd
    >>> clst = [coords[msk1,:,3000:], coords[msk2,:,3000:]]
    >>> rad, hist, num_int, rmax_auto = rpdf(clst, cell, dr, full_output=True)
    >>> plot(rad, hist)
    >>> plot(rad, num_int)
     
    refs:
    -----
    [AT] M. P. Allen, D. J. Tildesley, Computer Simulation of Liquids,
         Clarendon Press, 1989
    [MD] R. Haberlandt, S. Fritzsche, G. Peinel, K. Heinzinger,
         Molekulardynamik - Grundlagen und Anwendungen,
         Friedrich Vieweg & Sohn Verlagsgesellschaft 1995
    [Smith] W. Smith, The Minimum Image Convention in Non-Cubic MD Cells,
            http://citeseerx.ist.psu.edu/viewdoc/summary?doi=10.1.1.57.1696
            1989
    """
    # Theory
    # ------
    # 
    # 1) N equal particles (atoms) in a volume V.
    #
    # Below, "density" always means number density, i.e. 
    # (N atoms in the unit cell)  / (unit cell volume V).
    #
    # g(r) is (a) the average number of atoms in a shell [r,r+dr] around an
    # atom or (b) the average density of atoms in that shell -- relative to an
    # "ideal gas" (random distribution) of density N/V. Also sometimes: The
    # number of atom pairs with distance r relative to the number of pairs in
    # a random distribution.
    #
    # For each atom i=1,N, count the number dn(r) of atoms j around it in the
    # shell [r,r+dr] with r_ij = r_i - r_j.
    #   
    #   dn(r) = sum(i=1,N) sum(j=1,N, j!=i) delta(r - r_ij)
    # 
    # In practice, this is done by calculating all distances r_ij and bin them
    # in a histogram dn(k) with k = r_ij / dr the histogram index.
    # 
    # We sum over N atoms, so we have to divide by N -- that's why g(r) is an
    # average. Also, we normalize to ideal gas values
    #   
    #   g(r) = dn(r) / [N * (N/V) * V(r)]
    #        = dn(r) / [N**2/V * V(r)]
    #   V(r) = 4*pi*r**2*dr = 4/3*pi*[(r+dr)**3 - r**3]
    # 
    # where V(r) the volume of the shell. Formulation (a) from above: N/V*V(r)
    # is the number of atoms in the shell for an ideal gas (density*volume) or
    # (b):  dn(r) / V(r) is the density of atoms in the shell and dn(r) / [V(r)
    # * (N/V)] is that density relative to the ideal gas density N/V. Clear? :)
    # 
    # g(r) -> 1 for r -> inf in liquids, i.e. long distances are not
    # correlated. Their distribution is random. In a crystal, we get an
    # infinite series of delta peaks at the distances of the 1st, 2nd, ...
    # nearest neighbor shell.
    #
    # The number integral is
    #
    #   I(r1,r2) = int(r=r1,r2) N/V*g(r)*4*pi*r**2*dr
    # 
    # which can be used to calculate coordination numbers, i.e. it counts the
    # average number of atoms around an atom in a shell [r1,r2]. 
    #   
    # Integrating to infinity
    #
    #   I(0,inf) = N-1
    #
    # gives the average number of *all* atoms around an atom, *excluding* the
    # central one. This integral will converge to N-1 with or without PBC, but
    # w/o PBC, the nearest neigbor numbers I(r1,r2) will be wrong! Always use
    # PBC (minimum image convention). Have a look at the following table.
    # rmax_auto is the rmax value for the given unit cell by the method of
    # [Smith].
    #
    #                                    nearest neighb.     I(0,rmax) = N-1  
    # 1.) pbc=Tue,   rmax <  rmax_auto   +                   -
    # 2.) pbc=Tue,   rmax >> rmax_auto   +                   +
    # 3.) pbc=False, rmax <  rmax_auto   -                   -
    # 4.) pbc=Nalse, rmax >> rmax_auto   -                   +
    # 
    # Note that case (1) is the use case in [Smith]. Always use this. Also note
    # that case (2) appears to be also useful. However, it can be shown that
    # nearest neigbors are correct only up to rmax_auto! See
    # examples/rpdf/rpdf_aln.py .
    #
    # For a crystal, integrating over a peak [r-dr/2, r+dr/2] gives *exactly*
    # the number of nearest neighbor atoms for that distance r b/c the
    # normalization factor -- the number of atoms in an ideal gas for a narrow
    # shell of width dr -- is 1.
    #
    # 2) 2 selections
    #
    # Lets say you have 10 waters -> 10 x O (atom type A), 20 x H (type B),
    # then let A = 10, B = 20.
    #
    #   dn(r) = sum(i=1,A) sum(j=1,B) delta(r - r_ij) = 
    #           dn_AB(r) + dn_BA(r)
    # 
    # where dn_AB(r) is the number of B's around A's and vice versa. With the
    # densities A/V and B/V, we get
    #
    #   g(r) = g_AB(r) + g_BA(r) = 
    #          dn_AB(r) / [A * (B/V) * V(r)] + 
    #          dn_BA(r) / [B * (A/V) * V(r)]
    # 
    # Note that the density used is always the density of the *sourrounding*
    # atom type. Finally:
    #
    #  g(r) = [dn_AB(r) +  dn_BA(r)] / [A*B/V * V(r)]
    # 
    # Note the similarity to the case of one atom type:
    #
    #   g(r) = dn(r) / [N**2/V * V(r)]
    # 
    # This g(r) is independent of the selection order in `coords` b/c it's a
    # sum. But the number integal is not! It must depend on which atom type
    # surounds the other.
    # 
    #   I_AB(r1,r2) = int(r=r1,r2) (B/V)*g(r)*4*pi*r**2*dr
    #   I_BA(r1,r2) = int(r=r1,r2) (A/V)*g(r)*4*pi*r**2*dr
    #
    #
    # Verification
    # ------------
    # 
    # This function was tested against VMD's "measure gofr" command. VMD can
    # only handle orthorhombic boxes. To test non-orthorhombic boxes, see 
    # examples/rpdf/.
    #
    # Make sure to convert all length to Angstrom of you compare with VMD.
    #
    # Number integral mehod
    # ---------------------
    #
    # To match with VMD results, we use the most basic method, namely the
    # "rectangle rule", i.e. just y_i*dx. This is even cheaper than the
    # trapezoidal rule! To use the latter, we would do:
    #   number_integral_avg = np.zeros(len(bins)-2, dtype=float)
    #   for ...
    #       number_integral = \
    #           cumtrapz(1.0*natoms_lst[1]/volume*hist*4*pi*rad**2.0, rad)
    #   
    # Shape of result arrays:
    #   rect. rule :  len(hist)     = len(bins) - 1
    #   trapz. rule:  len(hist) - 1 = len(bins) - 2
    #
  
    assert cell.shape == (3,3), "`cell` must be (3,3) array"
    # nd array or list of 2 arrays
    if not type(coords) == type([]):
        coords_lst = [coords, coords]
    else:
        coords_lst = coords
    assert len(coords_lst) == 2, "len(coords_lst) != 2"
    assert coords_lst[0].ndim == coords_lst[1].ndim, ("coords do not have "
           "same shape") 
    # 2 or 3
    coords_ndim = coords_lst[0].ndim
    # (natoms,3) or (natoms,3,nstep)
    assert [3,3] == [c.shape[1] for c in coords_lst], ("axis 1 of one or "
           "both coord arrays does not have length 3")
    # assert shape 3d
    if coords_ndim == 2:
        for i in range(len(coords_lst)):
            coords_lst[i] = coords_lst[i][...,None]
        nstep = 1
    elif coords_ndim == 3:
        for i in range(len(coords_lst)):
            coords_lst[i] = coords_lst[i][...,tslice]
        nstep = coords_lst[0].shape[-1]
    else:
        raise StandardError("arrays in coords_lst have wrong shape, expect 2d "
                            "or 3d, got [%s, %s]" \
                            %tuple(map(str, [c.shape for c in coords_lst])))
    if align == 'cols':
        cell = cell.T
    rmax_auto = rmax_smith(cell)
    if rmax == 'auto':
        rmax = rmax_auto
    natoms_lst = [c.shape[0] for c in coords_lst]
    
    # sij : distance "matrix" in crystal coords
    # rij : in cartesian coords, same unit as `cell`, e.g. Angstrom
    # 
    # sij: for coords_lst[0] == coords_lst[1] == coords with shape (natoms,3), 
    #   i.e. only one structure:
    #   sij.shape = (N,N,3) where N = natoms, sij is a "(N,N)-matrix" of
    #   length=3 distance vectors,
    #   equivalent 2d:  
    #   >>> a=arange(5)
    #   >>> sij = a[:,None]-a[None,:]
    #   
    #   For 3d arrays with (N,3,nstep), we get (N,N,3,nstep).
    #
    #   For coords_lst[0] != coords_lst[1], i.e. 2 selections, we get
    #   (N,M,3) or (N,M,3,nstep), respectively.
    # 
    # If we have arbitrary selections, we cannot use np.tri() to select only
    # the upper (or lower) triangle of this "matrix" to skip duplicates (zero
    # distance on the main diagonal) and avoid double counting. We must
    # calculate and bin *all* distances.
    #
    # (natoms0, natoms1, 3, nstep)
    sij = coords_lst[0][:,None,...] - coords_lst[1][None,...]
    if pbc:
        sij = min_image_convention(sij)
    # (natoms0 * natoms1, 3, nstep)
    sij = sij.reshape(natoms_lst[0]*natoms_lst[1], 3, nstep)
    # (natoms0 * natoms1, 3, nstep)
    rij = np.dot(sij.swapaxes(-1,-2), cell).swapaxes(-1,-2)
    # (natoms0 * natoms1, nstep)
    dists_all = np.sqrt((rij**2.0).sum(axis=1))
    
    # Duplicate atoms in both coord sets from 1st time step, this is slow. Note
    # that VMD uses natoms_lst_prod - num_duplicates, but frankly, I don't
    # understand why.
    #
    ##zero_xyz = np.ones((3,)) * np.finfo(float).eps * 2.0
    ##num_duplicates = 0
    ##c0 = coords_lst[0][...,0]
    ##c1 = coords_lst[1][...,0]
    ##for i in range(c0.shape[0]):
    ##    for j in range(c1.shape[0]):
    ##        if (np.abs(c0[i,:] - c1[j,:]) < zero_xyz).all():
    ##            num_duplicates += 1
    
    natoms_lst_prod = float(np.prod(natoms_lst))
    volume = np.linalg.det(cell)
    bins = np.arange(0, rmax+dr, dr)
    rad = bins[:-1]+0.5*dr
    volume_shells = 4.0/3.0*pi*(bins[1:]**3.0 - bins[:-1]**3.0)
    norm_fac = volume / volume_shells / natoms_lst_prod
    
    # Set all dists > rmax to 0.0 and thereby keep shape of `dists_all`. This
    # is b/c we may calculate all nstep hists in a vectorized fashion later
    # (avoid Python loop). The first bin counting the zero distances will be
    # corrected below later anyway. The other way would be to do in each loop:
    #   dists = dists_all[...,idx][dists_all[...,idx] < rmax]
    #
    # Calculate hists for each time step and average them.
    #
    # XXX This Python loop is the bottleneck if we have many timesteps.
    dists_all[dists_all >= rmax] = 0.0
    hist_avg = np.zeros(len(bins)-1, dtype=float)
    number_integral_avg = np.zeros(len(bins)-1, dtype=float)
    for idx in range(dists_all.shape[-1]):
        dists = dists_all[...,idx]
        # rad_hist == bins
        hist, rad_hist = np.histogram(dists, bins=bins)
        # correct first bin
        if bins[0] == 0.0:
            hist[0] = 0.0
            # works only if we do NOT set dists > rmax to 0.0
            ##hist[0] -= num_duplicates 
        hist = hist * norm_fac            
        hist_avg += hist
        # Note that we use "natoms_lst[1] / volume" to get the correct density.
        number_integral = np.cumsum(1.0*natoms_lst[1]/volume*hist*4*pi*rad**2.0 * dr)
        number_integral_avg += number_integral
    hist_avg = hist_avg / (1.0*dists_all.shape[-1])
    number_integral = number_integral_avg / (1.0*dists_all.shape[-1])
    out = (rad, hist_avg)
    if full_output:
        out += (number_integral, rmax_auto) 
    return out

@crys_add_doc
def vmd_measure_gofr(coords, cell, symbols, dr, rmax='auto', selstr1='all', selstr2='all', 
                     fntype='xsf', first=0,
                     last=-1, step=1, usepbc=1, datafn=None,
                     scriptfn=None, logfn=None, xsffn=None, tmpdir='/tmp', 
                     keepfiles=False, full_output=False, align='rows'):
    """Call VMD's "measure gofr" command. This is a simple interface which does
    in fact the same thing as the gofr GUI. This is intended as a complementary
    function to rpdf() and should, of course, produce the "same" results.

    Only cubic boxes are alowed.
    
    args:
    -----
    coords : 3d array (natoms, 3, nstep)
        Crystal coords.
    %(cell_doc)s
        Unit: Angstrom
    symbols : (natoms,) list of strings
        Atom symbols.
    dr : float
        dr in Angstrom
    rmax : {'auto', float}, optional
        Max. radius up to which minimum image nearest neighbors are counted.
        For cubic boxes of side length L, this is L/2 [AT,MD].
        'auto': the method of [Smith] is used to calculate the max. sphere
            raduis for any cell shape
        float: set value yourself
    selstr1, selstr2 : str, optional
        string to select atoms, "all", "name O", ...
    fntype : str, optional
        file type of `fn` for the VMD "mol" command
    first, last, step: int, optional
        Select which MD steps are averaged. Like Python, VMD starts counting at
        0. Last is -1, like in Python. 
    usepbc: int {1,0}, optional
        Whether to use min image convention.
    datafn : str, optional (auto generated)
        temp file where VMD results are written to and loaded
    scriptfn : str, optional (auto generated)
        temp file where VMD tcl input script is written to
    logfn : str, optional (auto generated)
        file where VMD output is logged 
    xsffn : str, optional (auto generated)
        temp file where .axsf file generated from `coords` is written to and
        loaded by VMD
    tmpdir : str, optional
        dir where auto-generated tmp files are written
    keepfiles : bool, optional
        Whether to delete `datafn` and `scriptfn`.
    %(align_doc)s

    returns:
    --------
    (rad, hist, (number_integral, rmax_auto))
    rad : 1d array, radius (x-axis) with spacing `dr`, each value r[i] is the
        middle of a histogram bin 
    hist : 1d array, (len(rad),)
        the function values g(r)
    if full_output:
        number_integral : 1d array, (len(rad),)
            number_density*hist*4*pi*r**2.0*dr
        rmax_auto : float
            auto-calculated rmax, even if not used (i.e. rmax is set from
            input)
    """

    vmd_tcl = textwrap.dedent("""                    
    # VMD interface script. Call "measure gofr" and write RPDF to file.
    # Tested with VMD 1.8.7.
    #
    # Automatically generated by pwtools, XXXTIME
    #
    # Format of the output file (columns):
    #
    # radius    g(r)    int(0,r) g(rr)*drr
    # [Ang]
    
    # Load molecule file with MD trajectory. Typically, foo.axsf with type=xsf
    mol new XXXFN type XXXFNTYPE waitfor all
    
    # "top" is current top molecule (the one labeled with "T" in the GUI). 
    set molid top
    set selstr1 "XXXSELSTR1"
    set selstr2 "XXXSELSTR2"
    set first XXXFIRST
    set last XXXLAST
    set step XXXSTEP
    set delta XXXDR
    set rmax XXXRMAX
    set usepbc XXXUSEPBC
    
    set sel1 [atomselect $molid "$selstr1"]
    set sel2 [atomselect $molid "$selstr2"]
    
    # $result is a list of 5 lists, we only need the first 3
    set result [measure gofr $sel1 $sel2 delta $delta rmax $rmax first $first last $last step $step usepbc $usepbc]
    set rad [lindex $result 0]
    set hist [lindex $result 1]
    set num_int [lindex $result 2]
    
    # write to file
    set fp [open "XXXDATAFN" w]
    foreach r $rad h $hist i $num_int {
        puts $fp "$r $h $i"
    }    
    quit
    """)
    # Speed: The VMD command "measure gofr" is multithreaded and written in C.
    # That's why it is faster then the pure Python rpdf() above when we have to
    # average many timesteps. But the writing of the .axsf file here is
    # actually the bottleneck and makes this function slower.
    assert None not in [dr, rmax], "`dr` or `rmax` is None"
    tmpstr = tempfile.mktemp(prefix='', dir='')
    if datafn is None:
        datafn = os.path.join(tmpdir, "vmd_data_%s" %tmpstr)
    if scriptfn is None:
        scriptfn = os.path.join(tmpdir, "vmd_script_%s" %tmpstr)
    if logfn is None:
        logfn = os.path.join(tmpdir, "vmd_log_%s" %tmpstr)
    if xsffn is None:
        xsffn = os.path.join(tmpdir, "vmd_xsf_%s" %tmpstr)
    if align == 'cols':
        cell = cell.T
    cc = cell2cc(cell)
    if np.abs(cc[3:] - 90.0).max() > 0.1:
        print cell
        raise StandardError("`cell` is not a cubic cell, check angles")
    rmax_auto = rmax_smith(cell)
    if rmax == 'auto':
        rmax = rmax_auto
    write_axsf(xsffn, coords, cell, symbols)
    dct = {}
    dct['fn'] = xsffn
    dct['fntype'] = fntype
    dct['selstr1'] = selstr1
    dct['selstr2'] = selstr2
    dct['first'] = first
    dct['last'] = last
    dct['step'] = step
    dct['dr'] = dr
    dct['rmax'] = rmax
    dct['usepbc'] = usepbc
    dct['datafn'] = datafn
    dct['time'] = time.asctime()
    for key,val in dct.iteritems():
        vmd_tcl = vmd_tcl.replace('XXX'+key.upper(), str(val))
    common.file_write(scriptfn, vmd_tcl)
    common.system("vmd -dispdev none -eofexit -e %s 2>&1 | "
                  " tee %s" %(scriptfn, logfn))
    data = np.loadtxt(datafn)
    if not keepfiles:
        os.remove(datafn)
        os.remove(scriptfn)
        os.remove(xsffn)
        ##os.remove(logfn)
    rad = data[:,0]
    hist_avg = data[:,1]
    number_integral = data[:,2]
    out = (rad, hist_avg)
    if full_output:
        out += (number_integral, rmax_auto) 
    return out

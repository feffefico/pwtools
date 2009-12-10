#!/usr/bin/env python
# -*- coding: utf-8 -*- 
# vim:ts=4:sw=4:et

# 
# Copyright (c) 2008-2009, Steve Schmerler <mefx@gmx.net>.
# 

"""
Parse and post-process molecular dynamics data produced by the Quantum
Espresso package (quantum-espresso.org). 

Currently, pw.x and "calculation='md'" type data is supported as well as
'vc-md'.  Other calulation types write results in different order to the
outfile. Since we exploit the knowledge of the order for performance reasons,
supporting different orders requires a little rewrite/generalisation of the
code in parse_pwout().

XXX Changed implementation using sed/grep/awk -> order independent parsing

Tested with QE 3.2.3, 4.0.x, 4.1. 

Units
------
From http://www.quantum-espresso.org/input-syntax/INPUT_PW.html

    All quantities whose dimensions are not explicitly specified are in
    RYDBERG ATOMIC UNITS

    See constants.py

File handling:
--------------
Some functions accept only filenames or only fileobjects. Some accept both.
Default in all functions is that 
- in case of filenames, a file will be opened
  and closed inside the function. 
- In case of fileobjects, they will NOT be closed
  but returned, i.e. they must be opened and closed outside of a function.
- Most functions are decoratd with decorators.open_and_close by now. 
- All parsing functions which get and also return a fileobject are
  technically not required to return it. The object is modified in place when
  it's .next() method is called inside the function. We use this e.g. in
  next_line().

Filenames:
----------
Many functions which accept a file handle (mostly named "fh", file-like object)
use fh.name (the filename) in error messages etc. Most of these functions are
decorated w/ decorators.open_and_close. We try hard to make sure that each fh
has the 'name' attribute. Some file-like objects don't have fh.name and never
will (see decorators.py for details). In that case, avoid using fh.name or use
fn = com.get_filename(fh) early in the function. There is no other way.

Calculation of the PDOS
-----------------------

PDOS obtained the VACF way (V -> VACF -> FFT(VACF)) and the direct way
(direct_pdos()) differ by a very smal ammount. Probably numerical error due to
fft. Note that in order to compare both methods, you must NOT mirror the VACF
before FFT in order to get the double frequency resolution.

TODO
----
- Turn on Fortran order by default in array constructors (or wherever) in
  numpy to avoid copies by f2py extensions which operate on rank>2 arrays. 

  Changing ndarray.__new__() doesn't help, b/c it's only called in cases `a
  = ndarray()`, i.e. like __init__() -- call class constructor directly.
  empty(), array(), etc. are no members of ndarray. They are normal
  numpy.core.multiarray functions with kwarg "order={'C','F'}".

  => Best would be to subclass numpy.ndarray and redefind array() etc.

- Nose-integrated test suite.

- setup.py, real pythonic install, no fiddling with PYTHONPATH etc,
  you can also install in $HOME dir using `setup.py install
  --prefix=~/some/path/`, but it's probably overkill for this little project

- unify vacf_pdos(), direct_pdos(), thay have almost the same signature and
  functionallity, use 'method' kwarg or so, OR make a base class Pdos, derive 2
  special-case classes ... 

- If pydos.main(opts) is called in a script, find a way to define defaults in
  here so that the user doesn't have to provide *all* options. Currently we
  solve this by creating `opts` as a full argparse.Namepace in the script.
  This seems overkill although it's very fast. But the advantage is that we
  could pass pydos.py cmd line opts to any othert script if we wanted to.
  Python rocks!

- Implement the maximum entropy method.

- We assume that the ATOMIC_POSITIONS unit (crystal, alat, angstrom) in
  pw.in and that of the parsed out ones from pw.out are the same. Check this!
  For example, it is seems to be NOT the same for cp.x!

- Separate parsing and calculation in this tool. Skip the cmd-line stuff
  completely. Make a toolkit with plugin-type functionallity for extracting
  atomic pos etc from "any kind" of simulation data. Read these infos into an
  internal format (our 3D arrays for coords and velocitys seem fine). 
  Then, pass those infos to a class or function that calculates stuff.
  Essentially, replace Unix working style: grep/sed/awk into files (parsing) +
  apply some Fortran code to files (calculation) with flexible short Python
  scripts which call function and pass data arround ONLY IN MEMORY (or as
  binary files).
  
  As for the workflow: We replace writing shell scripts with writing Python
  scripts + out-of-the-box binary file writing by default. 

  Inventing an "internal format" is not bad. In fact, it is even necessary if
  we want to do math here. And if we have to invent one, then let's use numpy
  ndarrays and save them to disk *exactly in that form", i.e. use numpy binary
  arrays. That way we don't have to parse our own slow text file format all 
  the time (as xcrysden does with XSF :)

  If numpy arrays won't do it anymore, use NetCDF or HDF5.
"""

##from debug import Debug
##DBG = Debug()

# timing of the imports
##DBG.t('import')

import re
import os
import textwrap
from cStringIO import StringIO
##from StringIO import StringIO

import numpy as np
norm = np.linalg.norm

# slow import time for these
from scipy.fftpack import fft
from scipy.linalg import inv
from scipy.integrate import simps

# own modules
import constants
import _flib
import common as com
from decorators import open_and_close
import regex
import io
from verbose import verbose

# aliases
pjoin = os.path.join

##DBG.pt('import')

#-----------------------------------------------------------------------------
# globals 
#-----------------------------------------------------------------------------

# Used only in verbose().
VERBOSE = False

# All card names that may follow the namelist section in a pw.x input file.
INPUT_PW_CARDS = [\
    'atomic_species',
    'atomic_positions',
    'k_points',
    'cell_parameters',
    'occupations',
    'climbing_images',
    'constraints',
    'collective_vars']

FLOAT_RE = regex.float_re


#-----------------------------------------------------------------------------
# parsing 
#-----------------------------------------------------------------------------


def next_line(fh):
    """Will raise StopIteration at end of file ."""
    return fh.next().strip()


def next_nonempty_line(fh):
    """Will raise StopIteration at end of file. """
    line = next_line()
    while line == '':
        line = next_line(fh)
    return line        


# cannot use:
#
#   >>> fh, flag = scan_until_pat(fh, ...)
#   # do something with the current line
#   >>> line = fh.readline()
#   <type 'exceptions.ValueError'>: Mixing iteration and read methods would
#   lose data
# 
# Must return line at file position instead.

def scan_until_pat(fh, pat="atomic_positions", err=True, retline=False):
    """
    Go to pattern `pat` in file `fh` and return `fh` at this position. Usually
    this is a header followed by a table in a pw.x input or output file.

    args:
    ----
    fh : file like object
    pat : string, pattern in lower case
    err : bool, raise error at end of file b/c pattern was not found
    retline : bool, return current line
    
    returns:
    --------
    fh : file like object
    flag : int
    {line : current line}
    
    notes:
    ------
    Currently all lines in the file are converted to lowercase before scanning.

    examples:
    ---------
    example file (pw.x input):
        
        [...]
        CELL_PARAMETERS
           4.477898982  -0.031121661   0.004594438
          -2.231322621   3.882493243  -0.004687159
           0.012261087  -0.006915066  12.050227607
        ATOMIC_SPECIES
        Si 28.0855 Si.LDA.fhi.UPF
        O 15.9994 O.LDA.fhi.UPF   
        Al 26.981538 Al.LDA.fhi.UPF
        N 14.0067 N.LDA.fhi.UPF
        ATOMIC_POSITIONS alat                              <---- fh at this pos
        Al       4.482670384  -0.021685570   4.283770714         returned
        Al       2.219608875   1.302084775   8.297440557
        Si      -0.015470487  -0.023393016   1.789590196
        Si       2.194751751   1.364416814   5.817547157
        [...]
    """
    for line in fh:
        if line.strip().lower().startswith(pat):
            if retline:
                return fh, 1, line
            return fh, 1
    if err:
        raise StandardError("end of file '%s', pattern "
            "'%s' not found" %(fh, pat))
    # nothing found = end of file
    if retline:
        return fh, 0, line
    return fh, 0


def scan_until_pat2(fh, rex, err=True, retmatch=False):
    """
    *Slightly* faster than scan_until_pat().

    args:
    ----
    fh : file like object
    rex : Regular Expression Object with compiled pattern for re.match().
    err : bool, raise error at end of file b/c pattern was not found
    retmatch : bool, return current Match Object
    
    returns:
    --------
    fh : file like object
    flag : int
    {match : re Match Object}

    notes:
    ------
    For this function to be fast, you must pass in a compiled re object. 
        rex = re.compile(...)
        fh, flag = scan_until_pat2(fh, rex)
    or 
        fh, flag = scan_until_pat2(fh, re.compile(...))
    BUT, if you call this function often (e.g. in a loop) with the same
    pattern, use the first form, b/c otherwise re.compile(...) is evaluated in
    each loop pass. Then, this would essentially be the same as using
    re.match() directly.
    """
    for line in fh:
        match = rex.match(line)
        if match is not None:
            if retmatch:
                return fh, 1, match
            return fh, 1
    if err:
        raise StandardError("end of file '%s', pattern "
            "not found" %fh)
    # nothing found = end of file, rex.match(line) should be == None
    if retmatch:
        return fh, 0, match
    return fh, 0


# Must parse for ATOMIC_SPECIES and ATOMIC_POSITIONS separately (open, close
# infile) each time b/c the cards can be in arbitrary order in the input file.
# Therefore, we can't take an open fileobject as argumrent, but use the
# filename.
@open_and_close
def pwin_atomic_species(fh):
    """Parses ATOMIC_SPECIES card in a pw.x input file.

    args:
    -----
    fn : fileobj of pw.x input file

    returns:
    --------
        {'atoms': atoms, 'masses': masses, 'pseudos': pseudos}
        
        atoms : list of strings, (number_of_atomic_species,), 
            ['Si', 'O', 'Al', 'N']
        masses : 1d arary, (number_of_atomic_species,)
            array([28.0855, 15.9994, 26.981538, 14.0067])
        pseudos : list of strings, (number_of_atomic_species,)
            ['Si.LDA.fhi.UPF', 'O.LDA.fhi.UPF', 'Al.LDA.fhi.UPF',
            'N.LDA.fhi.UPF']
    notes:
    ------
    scan for:
        [...]
        ATOMIC_SPECIES|atomic_species
        [possible empty lines]
        Si 28.0855 Si.LDA.fhi.UPF
        O 15.9994 O.LDA.fhi.UPF   
        Al 26.981538 Al.LDA.fhi.UPF
        N 14.0067 N.LDA.fhi.UPF
        [...]
    """
    fn = com.get_filename(fh)
    verbose('[pwin_atomic_species] reading ATOMIC_SPECIES from %s' %fn)
    # rex: for the pseudo name, we include possible digits 0-9 
    rex = re.compile(r'\s*([a-zA-Z]+)\s+(' + FLOAT_RE + ')\s+([0-9a-zA-Z\.]*)')
    fh, flag = scan_until_pat(fh, pat='atomic_species')
    line = next_line(fh)
    while line == '':
        line = next_line(fh)
    match = rex.match(line)
    lst = []
    while match is not None:
        # match.groups: tuple ('Si', '28.0855', 'Si.LDA.fhi.UPF')
        lst.append(list(match.groups()))
        line = next_line(fh)
        match = rex.match(line)
    # numpy string array :)
    ar = np.array(lst)
    atoms = ar[:,0].tolist()
    masses = np.asarray(ar[:,1], dtype=float)
    pseudos = ar[:,2].tolist()
    return {'atoms': atoms, 'masses': masses, 'pseudos': pseudos}


@open_and_close
def pwin_cell_parameters(fh):
    """Parses CELL_PARAMETERS card in a pw.x input file. Extract primitive
    lattice vectors.

    notes:
    ------
    From the PWscf help:
        
        --------------------------------------------------------------------
        Card: CELL_PARAMETERS { cubic | hexagonal }
        
        Optional card, needed only if ibrav = 0 is specified, ignored
        otherwise!
        
        Flag "cubic" or "hexagonal" specify if you want to look for symmetries
        derived from the cubic symmetry group (default) or from the hexagonal
        symmetry group (assuming c axis as the z axis, a axis along the x
        axis).
        
        v1, v2, v3  REAL

            Crystal lattice vectors:
            v1(1)  v1(2)  v1(3)    ... 1st lattice vector
            v2(1)  v2(2)  v2(3)    ... 2nd lattice vector
            v3(1)  v3(2)  v3(3)    ... 3rd lattice vector

            In alat units if celldm(1) was specified or in a.u. otherwise.
        --------------------------------------------------------------------
        
        In a.u. = Rydberg atomic units (see constants.py).
    """
    fn = com.get_filename(fh)
    verbose('[pwin_cell_parameters] reading CELL_PARAMETERS from %s' %fn)
##>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
    rex = re.compile(r'\s*((' + FLOAT_RE + '\s*){3})\s*')
    fh, flag = scan_until_pat(fh, pat="cell_parameters")
    line = next_line(fh)
    while line == '':
        line = next_line(fh)
    match = rex.match(line)
    lst = []
    while match is not None:
        # match.groups(): ('1.3 0 3.0', ' 3.0')
        lst.append(match.group(1).strip().split())
        line = next_line(fh)
        match = rex.match(line)
    cp = np.array(lst, dtype=float)
#----------------------------------------------------------
##    cmd = "sed -nre '/CELL_PARAMETERS/,+3p' %s | tail -n3" %fn
##    cp = np.loadtxt(StringIO(com.backtick(cmd)))
#<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<
    com.assert_cond(len(cp.shape) == 2, "`cp` is no 2d array")
    com.assert_cond(cp.shape[0] == cp.shape[1], "dimensions of `cp` don't match")
    return cp


@open_and_close
def pwin_atomic_positions(fh, atspec=None):
    """Parse ATOMIC_POSITIONS card in pw.x input file.
    
    args:
    -----
    fh : fileobj of pw.x input file
    atspec : optional, dict returned by pwin_atomic_species()
    
    returns:
    --------
        {'coords': coords, 'natoms': natoms, 'massvec': massvec, 'symbols':
        symbols, 'unit': unit}
        
        coords : ndarray,  (natoms, 3)
        natoms : int
        massvec : 1d array, (natoms,)
        symbols : list of strings, (natoms,), ['Al', 'A', 'Si', ...]
        unit : string, 'alat', 'crystal', etc.

    notes:
    ------
    scan for:
        [...]
        ATOMIC_POSITIONS|atomic_positions [<unit>]
        [0 or more empty lines]
        Al       4.482670384  -0.021685570   4.283770714
        Al       2.219608875   1.302084775   8.297440557
        ...
        Si       2.134975048   1.275864192  -0.207552657
        [empty or nonempty line that does not match the RE]
        [...]

        <unit> is a string: 'alat', 'crystal' etc.
    """
    fn = com.get_filename(fh)
    verbose("[pwin_atomic_positions] reading ATOMIC_POSITIONS from %s" %fn)
    if atspec is None:
        atspec = pwin_atomic_species(fh)
        # need to seek to start of file here
        fh.seek(0)
    rex = re.compile(r'\s*([a-zA-Z]+)((\s+' + FLOAT_RE + '){3})\s*')
    fh, flag, line = scan_until_pat(fh, pat="atomic_positions", retline=True)
    line = line.strip().lower().split()
    if len(line) > 1:
        unit = re.sub(r'[{\(\)}]', '', line[1])
    else:
        unit = ''
    line = next_line(fh)
    while line == '':
        line = next_line(fh)
    lst = []
    # Must use REs here b/c we don't know natoms. 
    match = rex.match(line)
    while match is not None:
        # match.groups():
        # ('Al', '       4.482670384  -0.021685570   4.283770714', '    4.283770714')
        lst.append([match.group(1)] + match.group(2).strip().split())
        line = next_line(fh)
        match = rex.match(line)
    ar = np.array(lst)
    symbols = ar[:,0].tolist()
    # same as coords = np.asarray(ar[:,1:], dtype=float)
    coords = ar[:,1:].astype(float)
    natoms = coords.shape[0]
    masses = atspec['masses']
    atoms = atspec['atoms']
    massvec = np.array([masses[atoms.index(s)] for s in symbols], dtype=float)
    return {'coords': coords, 'natoms': natoms, 'massvec': massvec, 'symbols':
        symbols, 'unit': unit}


def _is_cardname(line, cardnames=INPUT_PW_CARDS):
    for string in cardnames:
        # matches "occupations", but not "occupations='semaring'"
        if re.match(r'^\s*%s\s*([^=].*$|$)' %string, line.lower()):
            return True
    return False            


@open_and_close
def pwin_namelists(fh):
    """
    Parse "namelist" part of a pw.x input file.

    args:
    -----
    fh : open fileobject

    notes:
    ------
    file to parse:
        [...]
        &namelist1
            FLOATKEY = 1.0d0, stringkey = 'foo' [,]
            boolkey = .true. [,]
            intkey = 10 [,]
            ...
        /
        &namelist2
            floatkey = 2.0d0, stringkey = 'bar' [,]
            boolkey = .false. [,]
            intkey = -10 [,]
            ...
        /
        ...

        [CARD SECTION]
        ATOMIC_SPECIES
        [...]
        
    
    returns: 
    --------
    dict of dicts:
        
        {namelist1:
            {'floatkey': '1.0d0', 'stringkey': "'foo'",
              boolkey = '.true.', intkey = '10'},
         namelist2:
            {'floatkey': '2.0d0', 'stringkey': "'bar'",
              boolkey = '.false.', intkey = '-10'},
        }
        
    All keys are converted to lowercase! All values are strings and must be
    converted.
    >>> d = pwin_namelists(...)
    >>> floatkey  = com.ffloat(d['namelist1']['floatkey'])
    >>> stringkey = d['namelist1']['stringkey']
    >>> intkey    = int(d['namelist1']['intkey'])
    >>> boolkey   = com.tobool(d['namelist1']['boolkey'])
    """
    fn = com.get_filename(fh)
    verbose("[pwin_namelists] parsing %s" %fn)
    dct = {}
    nl_kvps = None
    for line in fh:
        # '   A = b, c=d,' -> 'A=b,c=d'
        line = line.strip().strip(',').replace(' ', '')
        # Start of namelist.
        if line.startswith('&'):
            # namelist key value pairs
            nl_kvps = []
            # '&CONTROL' -> 'control'
            nl = line[1:].lower()
        # End of namelist. Enter infos from last namelist into `dct`.           
        elif line == '/':
            # nl = 'control', enter dict for namelist 'control' in `dct` under
            # name 'control'.
            # [['a', 'b'], ['c', 'd']] -> dct = {'control': {'a': 'b', 'c': 'd'}, ...}
            if nl_kvps is not None: 
                dct[nl] = dict(nl_kvps)
                nl_kvps = None
            else:
                dct[nl] = {}
        elif line == '' or line.startswith('!'):
            continue
        # end of namelist part
        elif _is_cardname(line):
            break
        else:
            # 'A=b,c=d' -> ['A=b', 'c=d'] -> 
            # nl_kvps = [..., ['a', 'b'], ['c', 'd']]
            for p in line.split(','):
                tmp = p.split('=')
                tmp[0] = tmp[0].lower()
                # HACK: remove nested quotes: "'foo'" -> 'foo'
                if tmp[1][0] == "'" and tmp[1][-1] == "'":
                    tmp[1] = eval(tmp[1])
                nl_kvps.append(tmp)
    return dct


def parse_pwout_md(fn, natoms=None, nstep=None, **kwargs):
    verbose("[parse_pwout_md] parsing %s" %fn)
    verbose("[parse_pwout_md] input:")
    verbose("[parse_pwout_md]     natoms: %s" %str(natoms))
    verbose("[parse_pwout_md]     nstep: %s" %str(nstep))
    verbose("[parse_pwout_md]     kwargs.keys(): %s" %str(kwargs.keys()))
    
    #########################################################################
    # Unlike the old parse_pwout_md, we IGNORE the first coords
    # (the one from input), temperature (which is printed is "Starting
    # temperature") in md and vc-md. We grep only for nstep occurrences, NOT
    # nstep+1 !!! 
    #########################################################################
    
    # FIXME It MAY be that with the grep-type approch, we grep the last coords
    # twice for relax runs b/c they are printed twice.
    #
    # TODO 
    # * get also etot
    # * Check if this grepping also works for scf runs, where we have in
    #   principle nstep=1
    # * Test getting CELL_PARAMETERS from cp.x out files
    # * temperature is stored completely different in cp.x outfiles, maybe use
    #   the files it writes to 'outdir', i.e. scratch
    
    # fallbacks, may be slow
    if nstep is None:
        verbose("[parse_pwout_md] using fallback for nstep")
        nstep = int(com.backtick('grep ATOMIC_POSITIONS %s | wc -l' %fn))
    if natoms is None:
        verbose("[parse_pwout_md] using fallback for natoms")
        # tail ... b/c this line appears multiple times if output file
        # is a concatenation of multiple smaller files
        natoms = int(com.backtick(r"egrep 'number[ ]+of[ ]+atoms' %s | \
            sed -re 's/.*=(.*)$/\1/' | tail -n1" %fn))
    verbose("[parse_pwout_md] natoms: %s" %str(natoms))
    verbose("[parse_pwout_md] nstep: %s" %str(nstep))
    
    # pressure
    verbose("[parse_pwout_md] getting pressure")
    ret_str = com.backtick(r"grep P= %s | awk '{print $6}'" %fn)
    pressure = np.loadtxt(StringIO(ret_str))
     
    # temperature
    verbose("[parse_pwout_md] getting temperature")
    cmd = r"egrep 'temperature\s*=' %s " %fn + "| sed -re 's/.*temp.*=\s*(" + FLOAT_RE + \
          r")\s*K/\1/'"
    ret_str = com.backtick(cmd)
    temperature = np.loadtxt(StringIO(ret_str))
    
    # atomic positions
    verbose("[parse_pwout_md] getting atomic positions")
    key = 'ATOMIC_POSITIONS'
    cmd = "sed -nre '/%s/,+%ip' %s | grep -v %s | \
          awk '{printf $2\"  \"$3\"  \"$4\"\\n\"}'" %(key, natoms, fn, key)
    ret_str = com.backtick(cmd)          
    coords = io.readtxt(StringIO(ret_str), axis=1, shape=(natoms, nstep, 3))

    # cell parameters
    verbose("[parse_pwout_md] getting cell parameters")
    key = 'CELL_PARARAMETERS'
    cmd = "sed -nre '/%s/,+3p' %s | grep -v %s" %(key, fn, key)
    cps_str = com.backtick(cmd)
    cps = io.readtxt(StringIO(cps_str), axis=1, shape=(3, nstep, 3))
    
    # XXX HACK: disable skipend stuff. This can be removed anyway since we do
    # not allocate R, T etc in advance, so they only contain the actual data
    # present in the out files.
    return {'R': coords, 'T': temperature, 'P': pressure, 'cps':cps, 
            'skipend': 0}

#-----------------------------------------------------------------------------
# computational
#-----------------------------------------------------------------------------

def normalize(a):
    """Normalize array by it's max value. Works also for complex arrays.

    example:
    --------
    >>> a=np.array([3+4j, 5+4j])
    >>> a
    array([ 3.+4.j,  5.+4.j])
    >>> a.max()
    (5.0+4.0j)
    >>> a/a.max()
    array([ 0.75609756+0.19512195j,  1.00000000+0.j ])
    """
    return a / a.max()


def velocity(R, dt=None, copy=True, rslice=slice(None)):
    """Compute V from R. Essentially, time-next-neighbor-differences are
    calculated.
        
    args:
    -----
    R : 3D array, shape (natoms, nstep+1, 3)
        atomic coords with initial coords (Rold) at R[:,0,:]
    dt: float
        time step
    copy : bool
        If False, then we do in-place modification of R to save memory and
        avoid array copies. Use only if you don't use R after calling this
        function.
    rslice : slice object, defaults to slice(None), i.e. take all
        a slice for the 2nd axis (time axis) of R  
    
    returns:            
    --------
    V : 3D array, shape (natoms, <determined_by_rslice>, 3)

    notes:
    ------
    Even with copy=False, a temporary copy of R in the calculation is
    unavoidable.
    """
    # FIXME We assume that the time axis the axis=1 in R. This not safe should we
    # ever change that.
    if copy:
        tmp = R.copy()[:,rslice,:]
    else:
        # view into R
        tmp = R[:,rslice,:]
    tmp[:,1:,:] =  tmp[:,1:,:] - tmp[:,:-1,:]
    # (natoms, nstep, 3), view only, skip j=0 <=> Rold
    V = tmp[:,1:,:]
    verbose("[velocity] V.shape: %s" %repr(V.shape))
    if dt is not None:
        V /= dt
    return V


def pyvacf(V, m=None, method=3):
    """
    Reference implementation. We do some crazy numpy vectorization here
    for speedups.
    """
    natoms = V.shape[0]
    nstep = V.shape[1]
    c = np.zeros((nstep,), dtype=float)
    # we add extra multiplications by unity if m is None, but since it's only
    # the ref impl. .. who cares. better than having tons of if's in the loops.
    if m is None:
        m = np.ones((natoms,), dtype=float)

    if method == 1:
        # c(t) = <v(t0) v(t0 + t)> / <v(t0)**2> = C(t) / C(0)
        #
        # "displacements" `t'
        for t in xrange(nstep):
            # time origins t0 == j
            for j in xrange(nstep-t):
                for i in xrange(natoms):
                    c[t] += np.dot(V[i,j,:], V[i,j+t,:]) * m[i]
    elif method == 2:    
        # replace 1 inner loop
        for t in xrange(nstep):
            for j in xrange(nstep-t):
                # Multiply with mass-vector m:
                # Use array broadcasting: each col of V[:,j,:] is element-wise
                # multiplied with m (or in other words: multiply each of the 3
                # vectors V[:,j,k] in V[:,j,:] element-wise with m).
                #   V[:,j,:]          -> (natoms, 3)
                #   m[:,np.newaxis]    -> (natoms, 1)
                c[t] += (V[:,j,:] * V[:,j+t,:] * m[:,np.newaxis]).sum()
    elif method == 3:    
        # replace 2 inner loops, the last loop can't be vectorized (at least I
        # don't see how), `t' as array of indices doesn't work in numpy and
        # fortran
        
        # Multiply with mass-vector m:
        # method A: 
        # DON'T USE!! IT WILL OVERWRITE V !!!
        # Multiply whole V (each vector V[:,j,k]) with sqrt(m), m =
        # sqrt(m)*sqrt(m) in the dot product, then. 3x faster than method B. 
        #   m[:,np.newaxis,np.newaxis] -> (natoms, 1, 1)
        #   V                        -> (natoms, nstep, 3)
        ## V *= np.sqrt(m[:,np.newaxis,np.newaxis])
        for t in xrange(nstep):
            # method B: like in method 2, but extended to 3D
            c[t] = (V[:,:(nstep-t),:] * V[:,t:,:]*m[:,np.newaxis,np.newaxis]).sum()
##            c[t] = (V[:,:(nstep-t),:] * V[:,t:,:]).sum()
    else:
        raise ValueError('unknown method: %s' %method)
    # normalize to unity
    c = c / c[0]
    return c



def fvacf(V, m=None, method=2, nthreads=None):
    """
    5+ times faster than pyvacf. Only 5 times b/c pyvacf is already
    partially numpy-optimized.

    notes:
    ------
    $ python -c "import _flib; print _flib.vacf.__doc__"
    vacf - Function signature:
      c = vacf(v,m,c,method,use_m,[nthreads,natoms,nstep])
    Required arguments:
      v : input rank-3 array('d') with bounds (natoms,nstep,3)
      m : input rank-1 array('d') with bounds (natoms)
      c : input rank-1 array('d') with bounds (nstep)
      method : input int
      use_m : input int
    Optional arguments:
      nthreads : input int
      natoms := shape(v,0) input int
      nstep := shape(v,1) input int
    Return objects:
      c : rank-1 array('d') with bounds (nstep)
    """
    natoms = V.shape[0]
    nstep = V.shape[1]
    # `c` as "intent(in, out)" could be "intent(out), allocatable" or so,
    # makes extension more pythonic, don't pass `c` in, let be allocated on
    # Fortran side
    c = np.zeros((nstep,), dtype=float)
    if m is None:
        # dummy
        m = np.empty((natoms,), dtype=float)
        use_m = 0
    else:
        use_m = 1
    # With "order='F'", we convert V to F-order and a copy is made. If we don't
    # do it, the wrapper code does. This copy is unavoidable, unless we
    # allocate the array in F order in the first place.
##    c = _flib.vacf(np.array(V, order='F'), m, c, method, use_m)
    verbose("calling _flib.vacf ...")
    if nthreads is None:
        # possible f2py bug workaround: catch OMP_NUM_THREADS here and set
        # number of threads
        key = 'OMP_NUM_THREADS'
        if os.environ.has_key(key):
            nthreads = int(os.environ[key])
            c = _flib.vacf(V, m, c, method, use_m, nthreads)
        else:            
            c = _flib.vacf(V, m, c, method, use_m)
    else:        
        c = _flib.vacf(V, m, c, method, use_m, nthreads)
    verbose("... ready")
    return c

# alias
vacf = fvacf



def norm_int(y, x, area=1.0):
    """Normalize integral area of y(x) to `area`.
    
    args:
    -----
    x,y : numpy 1d arrays
    area : float

    returns:
    --------
    scaled y

    notes:
    ------
    The argument order y,x might be confusing. x,y would be more natural but we
    stick to the order used in the scipy.integrate routines.
    """
    # First, scale x and y to the same order of magnitude before integration.
    # Not sure if this is necessary.
    fx = 1.0 / np.abs(x).max()
    fy = 1.0 / np.abs(y).max()
    sx = fx*x
    sy = fy*y
##    # Don't scale.
##    fx = fy = 1.0
##    sx=x
##    sy=y
    # Area under unscaled y(x).
    _area = simps(sy, sx) / (fx*fy)
    return y*area/_area



# XXX Freeuency in f, not 2*pi*f as in all the paper formulas! Check this.
def direct_pdos(V, dt=1.0, m=None, full_out=False, natoms=1.0):
    """Compute PDOS without the VACF by direct FFT of the atomic velocities.
    We call this Direct Method. Integral area is normalized 1.0.
    
    args:
    -----
    V : velocity array (natoms, nstep, 3)
    dt : time step in seconds
    m : 1d array (natoms,), atomic mass array, if None then mass=1.0 for all
        atoms is used  
    full_out : bool
    natoms : float, number of atoms

    returns:
    --------
    full_out = False
        (faxis, pdos)
        faxis : 1d array, frequency in Hz
        pdos : 1d array, the PDOS
    full_out = True
        (faxis, pdos, (full_faxis, full_pdos, split_idx))

    refs:
    -----
    [1] Phys Rev B 47(9) 1993
    """
    massvec=m 
    time_axis=1
    # array of V.shape, axis=1 is the fft of the arrays along axis 1 of V
    fftv = np.abs(fft(V, axis=time_axis))**2.0
    if massvec is not None:
        com.assert_cond(len(massvec) == V.shape[0], "len(massvec) != V.shape[0]")
        fftv *= massvec[:,np.newaxis, np.newaxis]
    # average remaining axes        
    full_pdos = fftv.sum(axis=0).sum(axis=1)        
    full_faxis = np.fft.fftfreq(V.shape[time_axis], dt)
    split_idx = len(full_faxis)/2
    faxis = full_faxis[:split_idx]
    pdos = full_pdos[:split_idx]
    
    #FIXME : 3*natoms or 1 ??? Check with other functions.
##    default_out = (faxis, norm_int(pdos, faxis, area=3.0*natoms))
    default_out = (faxis, norm_int(pdos, faxis, area=1.0))
    extra_out = (full_faxis, full_pdos, split_idx)
    if full_out:
        return default_out + (extra_out,)
    else:
        return default_out



def vacf_pdos(V, dt=1.0, m=None, mirr=False, full_out=False, natoms=1.0):
    """Compute PDOS by FFT of the VACF. Integral area is normalized to
    1.0.
    
    args:
    -----
    V : (natoms, nstep, 3)
    dt : time step in seconds
    m : 1d array (natoms,), atomic mass array, if None then mass=1.0 for all
        atoms is used  
    mirr : bool, mirror VACF at t=0 before fft
    full_out : bool
    natoms : float, number of atoms

    returns:
    --------
    full_out = False
        (faxis, pdos)
        faxis : 1d array, frequency in Hz
        pdos : 1d array, the PDOS
    full_out = True
        (faxis, pdos, (full_faxis, fftcc, split_idx, c))
        ffttc : 1d complex array, result of fft(c) or fft(mirror(c))
        c : 1d array, the VACF
    """
    massvec=m 
    c = vacf(V, m=massvec)
    if mirr:
        verbose("[vacf_pdos] mirror VACF at t=0")
        cc = mirror(c)
    else:
        cc = c
       
    fftcc = fft(cc)
    # in Hz
    full_faxis = np.fft.fftfreq(fftcc.shape[0], dt)
    full_pdos = np.abs(fftcc)
    split_idx = len(full_faxis)/2
    faxis = full_faxis[:split_idx]
    pdos = full_pdos[:split_idx]

    # FIXME
    # area must be == 3*atoms in unit cell, NOT supercell
##    default_out = (faxis, norm_int(pdos, faxis, area=3.0*natoms))
    default_out = (faxis, norm_int(pdos, faxis, area=1.0))
    extra_out = (full_faxis, fftcc, split_idx, c)
    if full_out:
        return default_out + (extra_out,)
    else:
        return default_out



def mirror(a):
    # len(aa) = 2*len(a) - 1
    return np.concatenate((a[::-1],a[1:]))



def slicetake(a, sl, axis=None, copy=False):
    """The equivalent of numpy.take(a, ..., axis=<axis>), but accepts slice
    objects instead of an index array. Also by default, it returns a *view* and
    no copy.
    
    args:
    -----
    a : numpy ndarray
    sl : slice object, list or tuple of slice objects
        axis=<int>
            one slice object for *that* axis
        axis=None 
            `sl` is a list of tuple of slice objects, one for each axis. 
            It must index the whole array, i.e. len(sl) == len(a.shape).
    axis : {None, int}
    copy : return a copy instead of a view
    
    returns:
    --------
    A view into `a`.

    examples:
    ---------
    >>> from numpy import s_
    >>> a = np.random.rand(20,20,20)
    >>> b1 = a[:,:,10:]
    >>> # single slice for axis 2 
    >>> b2 = slicetake(a, s_[10:], axis=2)
    >>> # tuple of slice objects 
    >>> b3 = slicetake(a, s_[:,:,10:])
    >>> (b2 == b1).all()
    True
    >>> (b3 == b1).all()
    True

    notes:
    ------
    
    1) Why do we need that:
    
    # no problem
    a[5:10:2]
    
    # the same, more general
    sl = slice(5,10,2)
    a[sl]

    But we want to:
     - Define (type in) a slice object only once.
     - Take the slice of different arrays along different axes.
    Since numpy.take() and a.take() don't handle slice objects, one would have
    to use direct slicing and pay attention to the shape of the array:
          a[sl], b[:,:,sl,:], etc ... not practical.
    
    Example of things that don't work with numpy-only tools:

        R = R[:,sl,:]
        T = T[sl]
        P = P[sl]
    
    This is not generic. We want to use an 'axis' keyword instead. np.r_()
    generates index arrays from slice objects (e.g r_[1:5] == r_[s_[1:5]
    ==r_[slice(1,5,None)]). Since we need index arrays for numpy.take(), maybe
    we can use that?
        
        R = R.take(np.r_[sl], axis=1)
        T = T.take(np.r_[sl], axis=0)
        P = P.take(np.r_[sl], axis=0)
    
    But it does not work alawys:         
    np.r_[slice(...)] does not work for all slice types. E.g. not for
        
        r_[s_[::5]] == r_[slice(None, None, 5)] == array([], dtype=int32)
        r_[::5]                                 == array([], dtype=int32)
        r_[s_[1:]]  == r_[slice(1, None, None)] == array([0])
        r_[1:]
            ValueError: dimensions too large.
    
    The returned index arrays are wrong (or we even get an exception).
    The reason is that s_ translates a fancy index (1:, ::5, 1:10:2,
    ...) to a slice object. This always works. But since take()
    accepts only index arrays, we use r_[s_[<fancy_index>]], where r_
    translates the slice object prodced by s_ to an index array.
    THAT works only if start and stop of the slice are known. r_ has
    no way of knowing the dimensions of the array to be sliced and so
    it can't transform a slice object into a correct index arry in case
    of slice(<number>, None, None) or slice(None, None, <number>).

    2) Slice vs. copy
    
    numpy.take(a, array([0,1,2,3])) or a[array([0,1,2,3])] return a copy of `a` b/c
    that's "fancy indexing". But a[slice(0,4,None)], which is the same as
    indexing (slicing) a[:4], return *views*. 
    """
    # numpy.s_[:] == slice(0, None, None). The same works with  
    # slice(None) == slice(None, None, None).
    if axis is None:
        slices = sl
    else:        
        slices = [slice(None)]*len(a.shape)
        slices[axis] = sl
    # a[...] can take a tuple or list of slice objects
    # a[x:y:z, i:j:k] is the same as
    # a[(slice(x,y,z), slice(i,j,k))] == a[[slice(x,y,z), slice(i,j,k)]]
##    return a[tuple(slices)]
    if copy:
        return a[slices].copy()
    else:        
        return a[slices]



def sliceput(a, b, sl, axis=None):
    """The equivalent of a[<slice or index>]=b, but accepts slices objects
    instead of array indices (e.g. a[:,1:]).
    
    args:
    -----
    a : numpy ndarray
    sl : slice object, list or tuple of slice objects
        axis=<int>
            one slice object for *that* axis
        axis=None 
            `sl` is a list or tuple of slice objects, one for each axis. 
            It must index the whole array, i.e. len(sl) == len(a.shape).
    axis : {None, int}
    
    returns:
    --------
    The modified `a`.
    
    examples:
    ---------
    >>> from numpy import s_
    >>> a=np.arange(12).reshape((2,6))
    >>> a[:,1:3] = 100
    >>> a
    array([[  0, 100, 100,   3,   4,   5],
           [  6, 100, 100,   9,  10,  11]])
    >>> sliceput(a, 200, s_[1:3], axis=1)
    array([[  0, 200, 200,   3,   4,   5],
           [  6, 200, 200,   9,  10,  11]])
    >>> sliceput(a, 300, s_[:,1:3])
    array([[  0, 300, 300,   3,   4,   5],
           [  6, 300, 300,   9,  10,  11]])
    """
    if axis is None:
        # silce(...) or (slice(...), slice(...), ...)
        tmp = sl
    else:
        # [slice(...), slice(...), ...]
        tmp = [slice(None)]*len(a.shape)
        tmp[axis] = sl
##    a[tuple(tmp)] = b
    a[tmp] = b
    return a



def coord_trans(R, old=None, new=None, copy=True, align='cols'):
    """Coordinate transformation.
    
    args:
    -----
    R : array (d0, d1, ..., M), Array of arbitrary rank with coordinates
        (length M vectors) in old coord sys `old`. The only shape resiriction is that
        the last dim must equal the number of coordinates (R.shape[-1] == M ==
        3 for normal 3-dim x,y,z). See "shape of `R`" in the notes section
        below for examples.
    old, new : 2d arrays
        matrices with the old and new basis vectors as rows or cols
    copy : bool, optional
        True: overwrite `R`
        False: return new array
    align : string
        {'cols', 'rows'}
        cols : basis vecs are columns of `old` and `new`
        rows : basis vecs are rows    of `old` and `new`

    returns:
    --------
    array of shape = R.shape, coordinates in system `new`
    
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
    
    >>> Rold = np.random.rand(30,200,3)
    >>> old = np.random.rand(3,3)
    >>> new = np.random.rand(3,3)
    >>> Rnew = coord_trans(Rold, old=old, new=new)
    >>> Rold2 = coord_trans(Rnew, old=new, new=old)
    >>> np.testing.assert_almost_equal(Rold, Rold2)
    
    # these do the same: A, B have vecs as rows
    >>> RB1=coord_trans(Rold, old=old, new=new, align='rows') 
    >>> RB2=coord_trans(Rold, old=old.T, new=new.T, align='cols') 
    >>> np.testing.assert_almost_equal(Rold, Rold2)

    refs:
    [1] http://www.mathe.tu-freiberg.de/~eiermann/Vorlesungen/HM/index_HM2.htm
        Kapitel 6
    
    notes:
    ------
    Coordinate transformation:
        
        Mathematical formulation:
        X, Y square matrices with basis vecs as *columns*.

        X ... old, shape: (3,3)
        Y ... new, shape: (3,3)
        I ... identity matrix, basis vecs of cartesian system, shape: (3,3)
        A ... transformation matrix, shape(3,3)
        v_X ... column vector v in basis X, shape: (3,1)
        v_Y ... column vector v in basis Y, shape: (3,1)
        v_I ... column vector v in basis I, shape: (3,1)

        "." denotes matrix multiplication (i.e. dot() in numpy).
            Y . v_Y = X . v_X = I . v_I = v_I
            v_Y = Y^-1 . X . v_X = A . v_X
        And some general linalg:
            (A . B)^T = B^T . A^T
        With this:
            v_Y^T = (A . v_X)^T = v_X^T . A^T
        In numpy, v^T == v, and so no "vector" needs to be transposed.
        That's because a vector is a 1d array, e.g. v = array([1,2,3]) with
        shape (3,) and rank 1 instead of column or row vector ((3,1) or (1,3))
        and rank 2. Transposing is not defined: v.T == v .  The dot() function
        knows that and performs the correct multiplication accordingly. 
        So, we then have finally
            v_Y = A . v_X
                = v_X . A^T 
        The latter form is implemented here.            

        Example:
        
        Transformation from crystal to cartesian coords.

        old:
        X = coord sys for a hexagonal lattice with primitive lattice
            vectors (basis vectors) a0, a1, a2, each shape (3,)
        new:                
        Y = cartesian, i.e. the components a0[i], a1[i], a2[i] of the 
            basis vectors are cartesian:
                a0 = a0[0]*[1,0,0] + a0[1]*[0,1,0] + a0[2]*[0,0,1]
                a1 = a1[0]*[1,0,0] + a1[1]*[0,1,0] + a1[2]*[0,0,1]
                a2 = a2[0]*[1,0,0] + a2[1]*[0,1,0] + a2[2]*[0,0,1]
        v = shape (3,) vec in the hexagonal lattice ("crystal
            coordinates")
        
        We have Y == I and I^-1 == I, so
            A = (Y^-1 . X) = X
        and
            v_Y = v_I = X . v_X = A . v_X
        Let the a's be the *rows* of the transformation matrix. In general, if
        we don't have one vector `v` but an array R of row vectors, 
        it's more practical to use dot(R,A.T) instead of dot(A,R) b/c of numpy
        array broadcasting. See below.
            
            A^T == A.T = [[--- a0 ---], 
                          [--- a1 ---], 
                          [--- a2 ---]] 
        and let
            
            v == v_X. 
        
        Every product X . v_X is actually an expansion of v_X in the basis
        vectors contained in X.
        We expand v_X in terms of the crystal basis (X):         
            
            v_Y = 
              = v[0]*a0       + v[1]*a1       + v[2]*a2
              
              = v[0]*A.T[0,:] + v[1]*A.T[1,:] + v[2]*A.T[2,:]
              
              = [v[0]*A.T[0,0] + v[1]*A.T[1,0] + v[2]*A.T[2,0],
                 v[0]*A.T[0,1] + v[1]*A.T[1,1] + v[2]*A.T[2,1],
                 v[0]*A.T[0,2] + v[1]*A.T[1,2] + v[2]*A.T[2,2]]
              
              = dot(v, A.T)       <=> v[j] = sum(i=0..2) v[i]*A[i,j]
              = dot(A, v)         <=> v[i] = sum(j=0..2) A[i,j]*v[j]
         
        If this dot product is actually computed, we get v in cartesian coords
        (Y).  
    
    shape of `R`:
        
        If we want to use fast numpy array broadcasting to transform many `v`
        vectors at once, we must use the form dot(R,A.T).
        The shape of `R` doesn't matter, as long as the last dimension matches
        the dimensions of A (e.g. R: (n,m,3), A: (3,3), dot(R,A.T): (n,m,3)).
         
        1d: R.shape = (3,)
        R == v = [x,y,z] 
        -> dot(A, v) == dot(v,A.T) = [x', y', z']

        2d: R.shape = (N,3)
        Array of coords of N atoms, R[i,:] = coord of i-th atom. The dot
        product is broadcast along the first axis of R (i.e. *each* row of R is
        dot()'ed with A.T).
        R = 
        [[x0,       y0,     z0],
         [x1,       y1,     z1],
          ...
         [x(N-1),   y(N-1), z(N-1)]]
        -> dot(R,A.T) = 
        [[x0',     y0',     z0'],
         [x1',     y1',     z1'],
          ...
         [x(N-1)', y(N-1)', z(N-1)']]
        
        3d: R.shape = (natoms, nstep, 3) 
        R[i,j,:] is the shape (3,) vec of coords for atom i at time step j.
        Broadcasting along the first and second axis. 
        These loops have the same result as newR=dot(R, A.T):
            # New coords in each (nstep, 3) matrix R[i,...] containing coords
            # of atom i for each time step. Again, each row is dot()'ed.
            for i in xrange(R.shape[0]):
                newR[i,...] = dot(R[i,...],A.T)
            
            # same as example with 2d array: R[:,j,:] is a matrix with atom
            # coord on each row at time step j
            for j in xrange(R.shape[1]):
                newR[:,j,:] = dot(R[:,j,:],A.T)
    
    """
    com.assert_cond(old.ndim == new.ndim == 2, "`old` and `new` must be rank 2 arrays")
    com.assert_cond(old.shape == new.shape, "`old` and `new` must have th same shape")
    msg = ''        
    if align == 'rows':
        old = old.T
        new = new.T
        msg = 'after transpose, '
    com.assert_cond(R.shape[-1] == old.shape[0], "%slast dim of `R` must match first dim"
        " of `old` and `new`" %msg)
    if copy:
        tmp = R.copy()
    else:
        tmp = R
    # must use `tmp[:] = ...`, just `tmp = ...` is a new array
    tmp[:] = np.dot(tmp, np.dot(inv(new), old).T)
    return tmp
        
#-----------------------------------------------------------------------------
# misc
#-----------------------------------------------------------------------------

def str_arr(arr, fmt='%.15g', delim=' '*4):
    """Convert array `arr` to nice string representation for printing.
    
    args:
    -----
    arr : array_like, 1d or 2d array
    fmt : string, format specifier, all entries of arr are formatted with that
    delim : string, delimiter

    returns:
    --------
    string

    examples:
    ---------
    >>> a=rand(3)
    >>> pydos.str_arr(a, fmt='%.2f')
    '0.26 0.35 0.97'
    >>> a=rand(2,3)
    >>> pydos.str_arr(a, fmt='%.2f')
    '0.13 0.75 0.39\n0.54 0.22 0.66'

    >>> print pydos.str_arr(a, fmt='%.2f')
    0.13 0.75 0.39
    0.54 0.22 0.66
    
    notes:
    ------
    Essentially, we replicate the core part of np.savetxt.
    """
    arr = np.asarray(arr)
    if arr.ndim == 1:
        return delim.join([fmt]*arr.size) % tuple(arr)
    elif arr.ndim == 2:
        _fmt = delim.join([fmt]*arr.shape[1])
        lst = [_fmt % tuple(row) for row in arr]
        return '\n'.join(lst)
    else:
        raise ValueError('rank > 2 arrays not supported')



def atpos_str(symbols, coords, fmt="%.10f"):
    """Convenience function to make a string for the ATOMIC_POSITIONS section
    of a pw.x input file. Usually, this can be used to process the output of
    crys.scell().
    
    args:
    -----
    symbols : list of strings with atom symbols, (natoms,), must match with the
        rows of coords
    coords : array (natoms, 3) with atomic coords

    returns:
    --------
    string

    example:
    --------
    >>> print atpos_str(['Al', 'N'], array([[0,0,0], [0,0,1.]]))
    Al      0.0000000000    0.0000000000    0.0000000000
    N       0.0000000000    0.0000000000    1.0000000000
    """
    coords = np.asarray(coords)
    assert len(symbols) == coords.shape[0], "len(symbols) != coords.shape[0]"
    txt = '\n'.join(symbols[i] + '\t' +  str_arr(row, fmt=fmt) \
        for i,row in enumerate(coords))
    return txt        




def main(opts):
    """Main function.

    args:
    ----
    opts: This will usually be a class argparse.Namespace, i.e. output of
        `opts=parser.parse_args()`, where `parser=argparse.ArgumentParser(...)`.
    
    notes:
    ------
    In general, `opts` can be any object providing attributes like opts.dos,
    opts.vacf_method, etc. For example

        class Opts(object):
            pass
        
        opts = Opts()
        opts.dos = True
        opts.vacf_method = 'direct'
        ...

    The disadvantage is that you must set *all* opts.<attribute>, since `opts`
    is supposed to contain all of them.
    """
    # print options
    verbose("options:")
    # opts.__dict__: a dict with options and values {'opt1': val1, 'opt2':
    # val2, ...}
    for key, val in opts.__dict__.iteritems():
        verbose("    %s: %s" %(key, com.frepr(val)))
    
    # make outdir
    if not os.path.exists(opts.outdir):
        verbose("creating outdir: %s" %opts.outdir)
        os.makedirs(opts.outdir)
    
    # make filenames
    if opts.pwofn.endswith('.out'):
        fn_body = os.path.basename(opts.pwofn.replace('.out', ''))
    else:
        fn_body = os.path.basename(opts.pwofn)
    if opts.file_type == 'bin':
        file_suffix = '.npy'
    elif opts.file_type == 'txt':
        file_suffix = '.txt'
    else:
        raise StandardError("wrong file_type: %s"  %opts.file_type)
    verbose("fn_body: %s" %fn_body)
    #TODO: parse for ekin, etot, etc. But don't fix file names here. Instead,
    # depending of what can be parsed out of a data file (e.g. P, T. Etot,
    # etc), generate file names automagically. -> We need that Parser calss
    # really bad :)
    vfn = pjoin(opts.outdir, fn_body + '.v' + file_suffix)
    rfn = pjoin(opts.outdir, fn_body + '.r' + file_suffix)
    mfn = pjoin(opts.outdir, fn_body + '.m' + file_suffix)
    tfn = pjoin(opts.outdir, fn_body + '.temp' + file_suffix)
    vacffn = pjoin(opts.outdir, fn_body + '.vacf' + file_suffix)
    pfn = pjoin(opts.outdir, fn_body + '.p' + file_suffix)
    pdosfn = pjoin(opts.outdir, fn_body + '.pdos' + file_suffix)
    timefn = pjoin(opts.outdir, fn_body + '.time' + file_suffix)

    # needed in 'parse' and 'dos'
    pwin_nl = pwin_namelists(opts.pwifn)
    
    # time axis in sec
    # 
    timestep = com.ffloat(pwin_nl['control']['dt']) * constants.tryd
    verbose("dt: %s seconds" %timestep)
    # get number of steps in pw.out         
    if opts.nstep is not None:
        nstep = opts.nstep
        parse_inp_nstep = nstep
        verbose("nstep from cmd line: %i" %nstep)
    else:        
        parse_inp_nstep = None
        nstep = int(pwin_nl['control']['nstep'])
        verbose("nstep from input file: %i" %nstep)
    time = np.linspace(0, timestep*nstep, nstep+1)      
    # If we take, say, only every 10's point, we must scale dt*10 to
    # preserve the correct time axis.
    if (opts.slice is not None) and (opts.slice.step is not None):
        verbose("scaling dt with slice step: %s" %opts.slice.step)
        timestep *= float(opts.slice.step)
        verbose("    scaled dt: %s seconds" %timestep)
        # This would be the correct time axis. But since we also always write
        # Rfull etc, we don't slice the time axis.
        ## time = time[opts.slice]
    
    # --- parse and write ----------------------------------------------------
    
    _already_sliced = False
    if opts.parse:
        atspec = pwin_atomic_species(opts.pwifn)
        atpos_in = pwin_atomic_positions(opts.pwifn, atspec)
        
        # This is a bit messy: call every single parse function (atomic_*(),
        # pwin_namelists()) and pass their output as args to parse_pwout_md(). Why
        # not make a big function that does all that under the hood? Because we
        # plan to use a class Parser one day which will have all these
        # individual functions as methods. Individual output args will be
        # shared via data members. Stay tuned.
        if opts.calc_type in ['md', 'vc-md']:
            parse_pwout = parse_pwout_md
        else:
            raise StandardError("illegal calc_type, allowed: md, vc-md")
        pwout = parse_pwout(opts.pwofn,
                            pwin_nl=pwin_nl, 
                            atspec=atspec,
                            atpos_in=atpos_in,
                            nstep=parse_inp_nstep)
        
        massvec = atpos_in['massvec']
        Rfull = pwout['R']
        Tfull = pwout['T']
        Pfull = pwout['P']
         
        # Handle outfile from aborted/killed calcs. 
        #
        # We consider a "full" data array an array consisting of only
        # meaningful data, i.e. not np.zeros() or np.empty(). That's why we
        # cut off the useless end parts of the arrays and "overwrite" the 
        # "full" arrays here.
        if opts.skipend and (pwout['skipend'] > 0):
            # XXX If used in Parser class, use slicetake() or
            # something, i.e. use axis information, not direct indexing.
            Rfull = Rfull[:,:-pwout['skipend'],:]
            Tfull = Tfull[:-pwout['skipend']]
            Pfull = Pfull[:-pwout['skipend']]
            verbose("skipping %s time points at end of R, new shape: %s" 
                  %(pwout['skipend'], str(Rfull.shape)))
            verbose("skipping %s time points at end of T, new shape: %s"
                  %(pwout['skipend'], str(Tfull.shape)))
            verbose("skipping %s time points at end of P, new shape: %s"
                  %(pwout['skipend'], str(Pfull.shape)))
        verbose("mean temperature: %f" %Tfull.mean())
        verbose("mean pressure: %f" %Pfull.mean())
        
        # Slice arrays if -s option was used. This does not affect the "full"
        # arrays. Slices are only views.
        if opts.slice is not None:
            verbose("slicing arrays")
            verbose("    R: old shape: %s" %str(Rfull.shape))
            verbose("    T: old shape: %s" %str(Tfull.shape))
            verbose("    P: old shape: %s" %str(Pfull.shape))
            R = slicetake(Rfull, opts.slice, axis=1)
            T = slicetake(Tfull, opts.slice, axis=0)
            P = slicetake(Pfull, opts.slice, axis=0)
            verbose("    R: new shape: %s" %str(R.shape))
            verbose("    T: new shape: %s" %str(T.shape))
            verbose("    P: new shape: %s" %str(P.shape))
            _already_sliced = True
        else:
            R = Rfull
            T = Tfull
            P = Pfull
        
        # Coord trans of atomic positons for testing only.
        #
        # If 
        #   ibrav=0 
        #   CELL_PARAMETERS is present
        #   ATOMIC_POSITIONS crystal
        # and if requested by the user, we transform the atomic coords crystal
        # -> cartesian.
        #
        # If the pw.x calc went fine, then the VACF calculated with R in
        # cartesian alat|bohr|angstrom or crystal must be the same. The coord
        # trans here is actually not necessary at all and serves only for
        # testing/verification purposes.
        #
        # allowed ATOMIC_POSITIONS units (from the Pwscf help):
        #    alat    : atomic positions are in cartesian coordinates,
        #              in units of the lattice parameter "a" (default)
        #
        #    bohr    : atomic positions are in cartesian coordinate,
        #              in atomic units (i.e. Bohr)
        #
        #    angstrom: atomic positions are in cartesian coordinates,
        #              in Angstrom
        #
        #    crystal : atomic positions are in crystal coordinates, i.e.
        #              in relative coordinates of the primitive lattice vectors
        #              (see below)
        # 
        unit = atpos_in['unit']
        ibrav = int(pwin_nl['system']['ibrav'])
        verbose("unit: '%s'" %unit)
        verbose("ibrav: %s" %ibrav)
        # ATOMIC_POSITIONS angstrom  -> cartesian angstrom
        # ATOMIC_POSITIONS bohr      -> cartesian bohr  [== a.u.] 
        # ATOMIC_POSITIONS           -> cartesian alat
        # ATOMIC_POSITIONS alat      -> cartesian alat
        # ATOMIC_POSITIONS crystal   -> crystal alat | a.u. 
        #
        # if celldm(1) present  -> CELL_PARAMETERS in alat -> crystal alat
        # if not                -> CELL_PARAMETERS in a.u. -> crystal a.u.
        # 
        if unit == 'crystal' and opts.coord_trans:
            if ibrav == 0:
                cp = pwin_cell_parameters(opts.pwifn)
                # CELL_PARAMETERS in alat
                if pwin_nl['system'].has_key('celldm(1)'):
                    alat = com.ffloat(pwin_nl['system']['celldm(1)'])
                    verbose("alat:", alat)
                    verbose("assuming CELL_PARAMETERS in alat")
                    new_unit = 'alat'
                # CELL_PARAMETERS in a.u.
                else:
                    verbose("celldm(1) not present" )
                    verbose("assuming CELL_PARAMETERS Bohr (Rydberg a.u.)")
                    new_unit = 'a.u.'
                verbose("doing coord transformation: %s -> %s" %('crystal',
                    'cartesian' + new_unit))
                R = coord_trans(R, old=cp, new=np.identity(cp.shape[0]),
                                copy=False, align='rows')
            else:
                verbose("ibrav != 0, ignoring possibly available card "
                "CELL_PARAMETERS, no coord transformation") 

        # write R here if it's overwritten in velocity()
        io.writearr(mfn,    massvec,   type=opts.file_type)
        io.writearr(tfn,    Tfull,     type=opts.file_type)
        io.writearr(rfn,    Rfull,     type=opts.file_type, axis=1)
        io.writearr(pfn,    Pfull,     type=opts.file_type)
        io.writearr(timefn, time,      type=opts.file_type)
        
    # --- dos ----------------------------------------------------------------
    
    if opts.dos: 
        
        # Read if we did not parse before.
        if not opts.parse:   
            massvec = io.readarr(mfn, type=opts.file_type)
            Rfull   = io.readarr(rfn, type=opts.file_type)
            Tfull   = io.readarr(tfn, type=opts.file_type)
            Pfull   = io.readarr(pfn, type=opts.file_type)


        # XXX: Code duplication here .......
        if opts.slice is not None and (not _already_sliced):
            verbose("slicing arrays")
            verbose("    R: old shape: %s" %str(Rfull.shape))
            verbose("    T: old shape: %s" %str(Tfull.shape))
            verbose("    P: old shape: %s" %str(Pfull.shape))
            R = slicetake(Rfull, opts.slice, axis=1)
            T = slicetake(Tfull, opts.slice, axis=0)
            P = slicetake(Pfull, opts.slice, axis=0)
            verbose("    R: new shape: %s" %str(R.shape))
            verbose("    T: new shape: %s" %str(T.shape))
            verbose("    P: new shape: %s" %str(P.shape))
        else:
            R = Rfull
            T = Tfull
            P = Pfull
        # ....... to here            

        # If we compute the *normalized* VCAF, then dt is a factor: 
        #     <v_x(0) v_x(t)> = 1/dt^2 <dx(0) dx(t)> 
        # which cancels in the normalization. dt is not needed in the velocity
        # calculation.
        V = velocity(R, copy=False)
        io.writearr(vfn, V, type=opts.file_type, axis=1)

        if not opts.mass:
            massvec = None
        else:
            verbose("mass-weighting VACF")
        
        # XXX Frequency in f, not 2*pi*f as in all the paper formulas! Check this.
        if opts.dos_method == 'vacf':
            faxis, pdos, extra = vacf_pdos(
                V, 
                dt=timestep, 
                m=massvec,
                mirr=opts.mirror, 
                full_out=True, 
                natoms=float(pwin_nl['system']['nat']),
                )
            full_faxis, fftcc, split_idx, vacf_data = extra                
            real_imag_ratio = norm(fftcc.real) / norm(fftcc.imag)
            verbose("fft: real/imag: %s" %real_imag_ratio)
            pdos_out = np.empty((split_idx, 4), dtype=float)
            pdos_comment = textwrap.dedent("""
            # PDOS by FFT of VACF
            # Integral normalized to 1.0: int(abs(fft(vacf)), f) = 1.0 
            # f [Hz]  abs(fft(vacf))  fft(vacf).real  fft(vacf).imag 
            """)
        # XXX Frequency in f, not 2*pi*f as in all the paper formulas! Check this.
        elif opts.dos_method == 'direct':
            if opts.mirror:
                verbose("note: opts.mirror useless for direct method")
            faxis, pdos, extra = direct_pdos(
                V, 
                dt=timestep, 
                m=massvec,
                full_out=True, 
                natoms=float(pwin_nl['system']['nat']),
                )
            full_faxis, full_pdos, split_idx = extra                
            real_imag_ratio = None
            pdos_out = np.empty((split_idx, 2), dtype=float)
            pdos_comment = textwrap.dedent("""
            # Direct PDOS by FFT of atomic velocities
            # Integral normalized to 1.0: int(pdos, f) = 1.0 
            # f [Hz]  pdos
            """)
        else:
            raise StandardError("illegal dos_method: %s" %opts.dos_method)

        df1 = full_faxis[1]-full_faxis[0]
        df2 = 1.0/len(full_faxis)/timestep
        # f axis in in order 1e12, so small `decimal` values are sufficient
        np.testing.assert_almost_equal(df1, df2, decimal=0)
        df = df1
        
        pdos_out[:,0] = faxis
        pdos_out[:,1] = pdos
        if opts.dos_method == 'vacf':
            pdos_out[:,2] = normalize(fftcc[:split_idx].real)
            pdos_out[:,3] = normalize(fftcc[:split_idx].imag)
        info = dict()
        info['nyquist_freq'] = "%e" %(0.5/timestep),
        info['freq_resolution'] = "%e" %df,
        info['dt'] = "%e" %timestep,
        if real_imag_ratio is not None:
            info['real_imag_ratio'] = "%e" %real_imag_ratio,
        pdos_info = {'fft': info}

        io.writearr(pdosfn, pdos_out, info=pdos_info, comment=pdos_comment, type=opts.file_type)
        if opts.dos_method == 'vacf':
            io.writearr(vacffn, vacf_data, type=opts.file_type)
       


def get_parser():
    """Create argparse.ArgumentParser instance with default values."""
 
    import argparse
    
    # argparse's bool options
    # We use argparse.py instead of Python's standard lib optparse, b/c only
    # argparse supports optional arguments. We use this for our bool valued
    # options.
    #
    epilog=textwrap.dedent("""
    bool options
    ------------
    Bool options have optional arguments: In gereral: pydos.py ... [-p [ARG]]
    
    Example with the -p option:

    * NO -p -> the default [False] is used automatically
    * -p    -> the "const" value [True] is used (switch -p "on")
    * with argument:
        -p 1 | true | on | yes -> True
        -p 0 | false | off | no  -> False
    
    For bool opts, all "const" values are True, i.e. if the option is
    used w/o argument, then it is [True].

    examples
    --------
    See the README file.
    """)    
    parser = argparse.ArgumentParser(
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter, 
##        version="%(prog)s " + str(VERSION),
        )
    # --- bool flags with optional args  ------------------
    parser.add_argument('-p', '--parse',
        nargs='?',
        metavar='bool',
        const=True,
        type=com.tobool,
        default=False,
        help="parse pw.x in- and outfile [%(default)s]",
        )
    parser.add_argument('-d', '--dos', 
        nargs='?',
        metavar='bool',
        const=True,
        type=com.tobool,
        default=False,
        help="calculate PDOS [%(default)s]",
        )
    parser.add_argument('-m', '--mirror', 
        nargs='?',
        metavar='bool',
        const=True,
        type=com.tobool,
        default=True,
        help="mirror VACF around t=0 before fft [%(default)s]",
        )
    parser.add_argument('-M', '--mass', 
        nargs='?',
        metavar='bool',
        const=True,
        type=com.tobool,
        default=True,
        help="use mass-weightng in PDOS calculation [%(default)s]",
        )
    parser.add_argument('-c', '--coord-trans', 
        nargs='?',
        metavar='bool',
        const=True,
        type=com.tobool,
        default=False,
        help="""perform coordinate transformation if atomic coords are not  
            in cartesian alat coords [%(default)s]""",
        )
    parser.add_argument('-e', '--skipend', 
        nargs='?',
        metavar='bool',
        const=True,
        type=com.tobool,
        default=True,
        help="""if PWOFN contains < nstep time iterations (unfinished
            calculation), then skip the last points [%(default)s]""",
        )
    # --- opts with required args -------------------------
    parser.add_argument('-x', '--outdir',
        default=pjoin(com.fullpath(os.curdir), 'pdos'),
        type=com.fullpath,
        help="[%(default)s]",
        )
    parser.add_argument('-i', '--pwi',
        dest="pwifn",
        type=com.fullpath,
        help="pw.x input file",
        )
    parser.add_argument('-o', '--pwo',
        dest="pwofn",
        type=com.fullpath,
        help="pw.x output file",
        )
    parser.add_argument('-s', '--slice',
        default=None,
        type=com.toslice,
        help="""indexing for the time axis of R in the velocity
            calculation (remember that Python's indexing is 0-based),
            examples: '3:', '3:7', '3:7:2', 3::2', ':7:2', '::-1',
            see also: http://scipy.org/Tentative_NumPy_Tutorial, 
            section "Indexing, Slicing and Iterating"
            """,  
        )
    parser.add_argument('-f', '--file-type', 
        default='bin',
        help="""{'bin', 'txt'} write data files as binary or ASCII text 
            [%(default)s]""",
        )
    parser.add_argument('-t', '--dos-method', 
        default='vacf',
        help="""{'vacf', 'direct'} method to calculate PDOS  [%(default)s]""",
        )
    parser.add_argument('-C', '--calc-type', 
        default='md',
        help="""{'md', 'vc-md'} calculation method of the md in pw.x [%(default)s]""",
        )
    parser.add_argument('-n', '--nstep', 
        type=int,
        default=None,
        help="""int, if for some reason the number of steps in PWINFN
        (control:nstep) does not match the actual number of iterations in
        PWOFN, use this to set the value [%(default)s]""",
        )
    return parser



def get_default_opts():
    """Return a class argparse.Namespace with all default options."""
    return get_parser().parse_args('')

# module level variable for convenience
default_opts = get_default_opts()


if __name__ == '__main__':
    
    import sys
##    # Turn off text messages from individual functions. They confuse doctest.
##    VERBOSE = False
##    import doctest
##    doctest.testmod(verbose=True)
    
    # Be chatty only ion cmd line mode.
    VERBOSE = True
    
    parser = get_parser()
    opts = parser.parse_args()    
    ret = main(opts)
    sys.exit()

# common.py
#
# File operations, common system utils and other handy tools that could as well
# live in the std lib.
#
# Steve Schmerler 2009 <mefx@gmx.net>
#

import types
import os
import subprocess
import re
import shutil

def assert_cond(cond, string=None):
    """Use this instead of `assert cond, string`. It's been said on
    numpy-discussions that the assert statement shouldn't be used to test user
    input in functions b/c with `python ... -O0` or __debug__ not beeing
    defined, the statement is not tested.
    
    args:
    -----
    cond : bool
        True : None is returned
        False : exception is raised
    string : str
    
    example:
    --------
    assert_cond(1==1, 'lala') -> ok
    assert_cond(1==2, 'lala') -> exception is raised
    """
    if not cond:
        raise AssertionError(string)

#-----------------------------------------------------------------------------
# Some handy file operations.
#-----------------------------------------------------------------------------

def fileo(val, mode='r', force=False):
    """Return open file object with mode `mode`. Handles also gzip'ed files.
    Non-empty files are protected. File objects are just passed through, not
    modified.

    args:
    -----
    val : str or file object
    mode : file mode (everything that Python's open() can handle)
    force : bool, force to overwrite non-empty files
    """
    if isinstance(val, types.StringType):
        if os.path.exists(val): 
            if not os.path.isfile(val):
                raise ValueError("argument '%s' exists but is no file" %val)
            if ('w' in mode) and (not force) and (os.path.getsize(val) > 0):
                raise StandardError("file '%s' not empty, won't ovewrite, use "
                    "force=True" %val)
        if val.endswith('.gz'):
            import gzip
            ret =  gzip.open(val, mode)
            # Files opened with gzip don't have a 'name' attr.
            if not hasattr(ret, 'name'):
                ret.name = fullpath(val)
            return ret                
        else:
            return open(val, mode)
    elif isinstance(val, types.FileType):
        if val.closed:
            raise StandardError("need an open file") 
        elif not val.mode == mode:
            raise ValueError("mode of file '%s' is '%s', must be '%s'" 
                %(val.name, val.mode, mode))
        return val

#-----------------------------------------------------------------------------

def file_read(fn):
    """Open file with name `fn`, return open(fn).read()."""
    fd = open(fn, 'r')
    txt = fd.read()
    fd.close()
    return txt

#-----------------------------------------------------------------------------

def file_write(fn, txt):
    """Write string `txt` to file with name `fn`. No check is made wether the
    file exists and/or is nonempty. Yah shalleth know whath thy is doingth.  
    shell$ echo $string > $file """
    fd = open(fn, 'w')
    fd.write(txt)
    fd.close()

#-----------------------------------------------------------------------------

def file_readlines(fn):
    """Open file with name `fn`, return open(fn).readlines()."""
    fd = open(fn, 'r')
    lst = fd.readlines()
    fd.close()
    return lst

#-----------------------------------------------------------------------------

def fullpath(s):
    """Complete path: absolute path + $HOME expansion."""
    return os.path.abspath(os.path.expanduser(s))

#-----------------------------------------------------------------------------

def fullpathjoin(*args):
    return fullpath(os.path.join(*args))

#-----------------------------------------------------------------------------

def igrep(pat_or_rex, iterable, func='search'):
    """
    Grep thru strings provided by iterable.next() calls. On each match, yield a
    Match object.

    args:
    -----
    pat_or_rex : regex string or compiled re Pattern object, if string then it
        will be compiled
    iterable : sequence of lines (strings to search) or open file object or in 
        general anything that can be iterated over and yields a string (i.e. an
        object that has a next() method)
    func : string, the used re function for matching, e.g. 'match' for re.match
        functionallity

    returns:
    --------
    generator object which yields Match objects

    example:
    --------
    # If a line contains at least three numbers, grep the first three.
    >>> !cat test.txt
    a b 11  2   3   xy
    b    4  5   667
    c    7  8   9   4 5
    lol 2
    foo
    >>> fd=open('test.txt')
    >>> for m in igrep(r'(([ ]+[0-9]+){3}?)', fd, 'search'): print m.group(1).strip() 
    11  2   3
    4  5   667
    7  8   9
    >>> fd.seek(0)
    # Put numbers directly into numpy array.
    >>> ret=igrep(r'(([ ]+[0-9]+){3}?)', fd, 'search') 
    >>> array([m.group(1).split() for m in ret], dtype=float)
    array([[  11.,    2.,    3.],
           [   4.,    5.,  667.],
           [   7.,    8.,    9.]])
    >>> fd.close()
    
    notes:
    ------
    This function could also live in Python's itertools module.

    Similar to the shell grep(1) utility, one can directly access match
    groups. In the previous example, this is the same as 
        $ egrep -o '([ ]+[0-9]+){3}?' test.txt
    or `grep ...  | sed ...` or `grep ... | awk` or `awk ...` in more
    complicated situations. The advantage here is obviously that it's pure
    Python. We don't need any temp files as in
        os.system('grep ... > tmp')
        fd=open('tmp')
        print fd.read()
        fd.close()
    One possilbe other way w/o tempfiles would be to call grep(1) & friends thru
        p = subprocess.Popen('grep ...', stdout=PIPE) 
        print p.stdout.read()
    But that's not really pythonic, eh? :)
    """
    if isinstance(pat_or_rex, types.StringType):
        rex = re.compile(pat_or_rex)
    else:
        rex = pat_or_rex
    # rex.match(), rex.search(), ... anything that has the signature of
    # {re|Pattern object).match() and returns None or a Match object
    rex_func = getattr(rex, func)        
    for line in iterable:
        match = rex_func(line)
        if match is not None:
            yield match

#-----------------------------------------------------------------------------

def grep(*args):
    """Like igrep, but returns a list of Match objects."""
    return [m for m in igrep(*args)]
    
#-----------------------------------------------------------------------------

def raw_template_replace(dct, txt, conv=False, warn_found=False):
    """Replace placeholders dct.keys() with string values dct.values() in a
    text string. 
    
    args:
    -----
    dct : dictionary 
    txt : string
    conv : bool, convert values to strings with str()
    warn_found : bool, warning if a key is found multiple times in a file
    
    returns:
    --------
    new string

    notes:
    ------
    `txt` us usually a read text file (txt=fd.read()).  Although we use
    txt.replace(), this method ~ 4 times faster then looping
    over lines in fd. But: only as long as `txt` fits entirely into memory.
    """
    # This is a pointer. Each txt.replace() returns a copy.
    new_txt = txt
    for key, val in dct.iteritems():
        if key in new_txt:
            if val is None:
                print "template_replace: value for key '%s' is None, skipping" %key
                continue
            if conv:
                val = str(val)
            if not isinstance(val, types.StringType):
                raise StandardError("dict vals must be strings: key: '%s', val: " %key + \
                    str(type(val)))
            if warn_found:                    
                cnt = txt.count(key)
                if cnt > 1:
                    print("template_replace: warning: key '%s' found %i times"
                    %(key, cnt))
            new_txt = new_txt.replace(key, val)                                          
        else:
            print "template_replace: key not found: %s" %key
    return new_txt

#-----------------------------------------------------------------------------

def template_replace(dct, txt):
    return raw_template_replace(dct, txt, conv=True, warn_found=True)

#-----------------------------------------------------------------------------

def file_template_replace(fn, dct, bak=''):
    """
    dct = {'xxx': 'foo', 'yyy': 'bar'}
    fn = bla.txt
    file_template_replace(fn, dct, '.bak')

    shell$ sed -i.bak -r -e 's/xxx/foo/g -e 's/yyy/bar/g' bla.txt"""
    txt = template_replace(dct, file_read(fn))
    if bak != '':
        shutil.copy(fn, fn + bak)                
    file_write(fn, txt)

#-----------------------------------------------------------------------------

def print_dct(dct):
    for key, val in dct.iteritems():
        print "%s: %s" %(key, str(val))


#-----------------------------------------------------------------------------
# Child processes & shell calls
#-----------------------------------------------------------------------------

def system(call, wait=True):
    """Fire up shell commamd line `call`. 
    
    args:
    -----
    call: str (example: 'ls -l')
    wait : bool
        False: Don't wait for `call` to terminate.
            This can be used to spawn multiple processes concurrently. This is
            identical to calling os.system(call) (as opposed to ret=os.system(call).
        True: This is identical to calling ret=os.system(call).
    """
    p = subprocess.Popen(call, shell=True)
    if wait:
        os.waitpid(p.pid, 0)

#-----------------------------------------------------------------------------
# aliases
#-----------------------------------------------------------------------------

fpj = fullpathjoin

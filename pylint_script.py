# Python equivalent of pylint.sh script
import os
import re
import subprocess
import sys
import tempfile
from urllib import request
from pathlib import Path
from datetime import datetime as dt

SCRIPT_DIR= tempfile.gettempdir()
PYLINTRC_DIR = os.path.join(tempfile.gettempdir(), "pylintrc")
PYLINT_ALLOWLIST_DIR = os.path.join(tempfile.gettempdir(), "pylint_allowlist")

# name of this script; we want to exclude it from the linting process 
PYLINT_SCRIPT_NAME = "pylint_script.py" 

# Download latest configs from main TensorFlow repo
request.urlretrieve("https://raw.githubusercontent.com/tensorflow/tensorflow/master/tensorflow/tools/ci_build/pylintrc", PYLINTRC_DIR)
request.urlretrieve("https://raw.githubusercontent.com/tensorflow/tensorflow/master/tensorflow/tools/ci_build/pylint_allowlist", PYLINT_ALLOWLIST_DIR)

# Get the number of CPUs
def num_cpus():
    N_CPUS = os.cpu_count()
    if (N_CPUS is None):
        print("ERROR: Unable to determine the number of CPUs")
        exit()
    return N_CPUS

# Get list of all Python files, regardless of mode
def get_py_files_to_check():
    py_files_list = []
    current_dir = os.getcwd()
    for root, folders, files in os.walk(current_dir):
        for filename in folders + files:
            if filename.endswith(".py") and filename != PYLINT_SCRIPT_NAME:
                py_files_list.append(os.path.join(root, filename))
    return py_files_list

def do_pylint():
    # Something happened. TF no longer has Python code if this branch is taken
    PYTHON_SRC_FILES=get_py_files_to_check()
    if not PYTHON_SRC_FILES:
        print("do_pylint found no Python files to check. Returning.")
        exit()
    # Now that we know we have to do work, check if `pylint` is installed
    # if "pylint" in sys.modules:
    #     print("yes")
    # else:
    #     print("no")

    # Configure pylint using the following file
    PYLINTRC_FILE=PYLINTRC_DIR
    if not os.path.isfile(PYLINTRC_FILE):
        print("ERROR: Cannot find pylint rc file at "+PYLINTRC_FILE)
        exit()
    
    # Run pylint in parallel, after some disk setup
    NUM_SRC_FILES = len(PYTHON_SRC_FILES)
    NUM_CPUS = num_cpus()    
    print("Running pylint on %d files with %d parallel jobs...\n"
        %(NUM_SRC_FILES, NUM_CPUS))

    PYLINT_START_TIME = dt.now()

    # mkstemp returns a tuple: (file_descriptor, file_path); we only care
    # about the file path, but need to close out the file_descriptor later 
    _, OUTPUT_FILE= tempfile.mkstemp(suffix="_pylint_output.log")
    _, ERRORS_FILE= tempfile.mkstemp(suffix="_pylint_errors.log")
    _, PERMIT_FILE= tempfile.mkstemp(suffix="_pylint_permit.log")
    _, FORBID_FILE= tempfile.mkstemp(suffix="_pylint_forbid.log")

    # When running, filter to only contain the error code lines. Removes module
    # header, removes lines of context that show up from some lines.
    # Also, don't redirect stderr as this would hide pylint fatal errors.

    out = subprocess.Popen(['pylint', '--rcfile='+PYLINTRC_FILE, '--output-format=parseable',
        '--jobs='+str(NUM_CPUS)]+PYTHON_SRC_FILES, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    stdout, stderr = out.communicate()
    #print(stdout)

    OUTPUT_list = []
    ERRORS_list = []
    PERMIT_list = []
    FORBID_list = []

    stdout = stdout.split('\n')
    with open(OUTPUT_FILE, 'w') as f:
        for line in stdout:
            if "[C" in line or "[E" in line or "[F" in line or "[W" in line:
                #line += "\n"
                OUTPUT_list.append(line)
                f.write(line)
    
    #print(OUTPUT_list)
    #print(*OUTPUT_list)

    # with open(OUTPUT_FILE, 'r') as f:            
    #     for line in f.readlines():
    #         print(line, end='')
    
    PYLINT_END_TIME=dt.now()
    PYLINT_TIME = (PYLINT_END_TIME - PYLINT_START_TIME).total_seconds()
    print("pylint took "+str(PYLINT_TIME)+" seconds\n")
    
    # Report only what we care about
    # Ref https://pylint.readthedocs.io/en/latest/technical_reference/features.html
    # E: all errors
    # W0311 bad-indentation
    # W0312 mixed-indentation
    # C0330 bad-continuation
    # C0301 line-too-long
    # C0326 bad-whitespace
    # W0611 unused-import
    # W0622 redefined-builtin

    for line in OUTPUT_list:
        if ("[E" in line or "[W0311" in line or "[W0312" in line or "[C0330" in line 
            or "[C0301" in line or "[C0326" in line or "[W0611" in line or "[W0622" in line):
            ERRORS_list.append(line)

    # Split the pylint reported errors into permitted ones and those we want to
    # block submit on until fixed.
    # We use `${ALLOW_LIST_FILE}` to record the errors we temporarily accept. Goal
    # is to make that file only contain errors caused by difference between
    # internal and external versions.
    ALLOW_list = []
    ALLOW_LIST_FILE = Path(PYLINT_ALLOWLIST_DIR)
    ALLOW_LIST_FILE_errors = set()

    if not ALLOW_LIST_FILE.exists():
        print("ERROR: Cannot find pylint allowlist file at "+PYLINT_ALLOWLIST_DIR)
        exit()
    else:
        with open(ALLOW_LIST_FILE, 'r') as f:
            for line in f.readlines():
                line = line.partition("[")[2].partition(".")[0]
                ALLOW_LIST_FILE_errors.add(line)
    
    # Split into Permit and Forbid files... not as easy as with grep 
    for line in ERRORS_list:
        if any(error in line for error in ALLOW_LIST_FILE_errors):
            # error is in allowed list
            PERMIT_list.append(line)
        else:
            # error isn't in allowed list;
            FORBID_list.append(line)

    # Determine counts of errors
    N_PERMIT_ERRORS = len(PERMIT_list)
    N_FORBID_ERRORS = len(FORBID_list)

    # First print all allowed errors
    if (N_PERMIT_ERRORS != 0):
        print("\nFound %d allowlisted pylint errors: " % (N_PERMIT_ERRORS))
        print(*PERMIT_list, sep='\n')

    # Now, print the errors we should fix
    if (N_FORBID_ERRORS != 0):
        print("\nFound %d non-allowlisted pylint errors: " % (N_FORBID_ERRORS))
        print(*FORBID_list, sep='\n')
    
        print("\n\nFAIL: Found %d non-allowlisted errors and %d allowlisted errors" 
            %(N_FORBID_ERRORS, N_PERMIT_ERRORS))
    else:
        print("PASS: Found only %d allowlisted errors" %())

do_pylint()

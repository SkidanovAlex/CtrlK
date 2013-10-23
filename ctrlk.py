import leveldb
import json
import os.path
import vim
import threading
import signal
import time

from clang.cindex import Index, Config, TranslationUnitLoadError, CursorKind

# TODO: handle files that are deleted. today we only add and reparse files

# prefixes for the indexDb entries:
#
#   f%%%<file_name> => <lastModified>
#      file <file_name> was indexed, at that moment its mtime was lastModified
#
#   c%%%<file_name>%%%<symbol> => 1
#      file <file_name> contains symbol <symbol>. used to delete symbols when we reparse file
#
#   spelling%%%<symbol> => <spelling>
#      spelling of a symbol
#
#   s%%%<symbol>%%%<file_name>%%%<line>%%%<col> => <use_type>
#      actual symbols database for 'goto definition' and 'goto declaration'
#
#   n%%%<spelling>%%%<file_name>%%%<line>%%%<col> => <use_type>
#      actual symbols database for Ctrl+K
#
#   F%%%<file_name_without_path>%%%<full_file_path> => 1
#      so that we can show files in Ctrl_K
#
#   h%%%<file_name>%%%<header_name> => <command_line_args>
#      file <file_name> includes file <header_name>. used to both traverse header files and so
#      that we can delete them when we reparse the <file_name>
#      the value is command arguments with which the source file was compiled. same arguments will
#      be used to parse the header file
#
# <symbol> is what get_usr for a cursor returns
# <use_type> is a CursorKind.value. If the entry is also a definition, <use_type> is negative of that number
#


clangLibraryPath = None
indexDbPath = None
compilationDbPath = None
updateProcess = None

parsingState = ""

# the following two functions are taken from clang_complete plugin
def canFindBuiltinHeaders(index, args = []):
  flags = 0
  currentFile = ("test.c", '#include "stddef.h"')
  try:
    tu = index.parse("test.c", args, [currentFile], flags)
  except TranslationUnitLoadError, e:
    return 0
  return len(tu.diagnostics) == 0

# Derive path to clang builtin headers.
#
# This function tries to derive a path to clang's builtin header files. We are
# just guessing, but the guess is very educated. In fact, we should be right
# for all manual installations (the ones where the builtin header path problem
# is very common) as well as a set of very common distributions.
def getBuiltinHeaderPath(library_path):
  index = Index.create()
  knownPaths = [
          library_path + "/../lib/clang", # default value
          library_path + "/../clang", # gentoo
          library_path + "/clang", # opensuse
          library_path + "/", # Google
          "/usr/lib64/clang", # x86_64 (openSUSE, Fedora)
          "/usr/lib/clang"
  ]

  for path in knownPaths:
    try:
      files = os.listdir(path)
      if len(files) >= 1:
        files = sorted(files)
        subDir = files[-1]
      else:
        subDir = '.'
      path = path + "/" + subDir + "/include/"
      arg = "-I" + path
      if canFindBuiltinHeaders(index, [arg]):
        return path
    except Exception as e:
      pass

  return None

""" BerkeleyDb implementation
# abstract all the communication to the database so that replacing the underlying DB is easier
def IndexDbOpen(path, readOnly, create = False):
    conn = db.DB()
    conn.open(path, None, db.DB_BTREE, (db.DB_RDONLY if readOnly else 0) or db.DB_CREATE)
    return conn

def IndexDbRangeIter(conn, startWith = ''):
    startWith = str(startWith)
    cursor = conn.cursor()
    entry = cursor.set_range(startWith)
    # apparently BerkeleyDB doesn't support iterating in a presense of concurrent deletes, so snapshot the list instead of yielding elements right away
    ret = []
    while not entry is None and entry[0].startswith(startWith):
        ret.append((entry[0], entry[1]))
        entry = cursor.next()
    cursor.close()
    for entry in ret:
        yield entry

def IndexDbDelete(conn, key):
    try:
        conn.delete(key)
    except db.DBNotFoundError as e:
        pass

def IndexDbPut(conn, key, value):
    conn.put(str(key), str(value))

def IndexDbGet(conn, key, default=None):
    if not default is None:
        default = str(default)
    return conn.get(str(key), default=default)
"""

""" SQLLite implementation
# abstract all the communication to the database so that replacing the underlying DB is easier
def IndexDbOpen(path, readOnly, create = False):
    conn = sqlite3.connect(path)
    if create:
        c = conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS symbols(key text, value text, PRIMARY KEY(key))")
        conn.commit()
    return conn

def IndexDbRangeIter(conn, startWith = None):
    c = conn.cursor()
    if startWith == None:
        res = c.execute("SELECT * FROM symbols");
    else:
        assert startWith[-1] == '%'
        firstExcl = startWith[:-1] + '^'
        res = c.execute("SELECT * FROM symbols WHERE key BETWEEN ? AND ?", (startWith, firstExcl))
    for row in res:
        yield row

def IndexDbDelete(conn, key):
    c = conn.cursor()
    c.execute("DELETE FROM symbols WHERE key = ?", (key,))
    conn.commit()

def IndexDbPut(conn, key, value):
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO symbols VALUES (?, ?)", (key, value))
    conn.commit()

def IndexDbGet(conn, key, default=None):
    c = conn.cursor()
    c.execute("SELECT * FROM symbols WHERE key = ?", (key,))
    ret = c.fetchone() 
    if ret is None: ret = default
    else: ret = ret[1]
    return ret
"""

# abstract all the communication to the database so that replacing the underlying DB is easier
dbLock = threading.Lock()
dbInstance = None
def IndexDbOpen(path, readOnly, create = False):
    global dbLock
    global dbInstance
    with dbLock:
        if dbInstance != None:
            return dbInstance
        if create:
            if not os.path.exists(path):
                os.makedirs(pat)
        dbInstance = leveldb.LevelDB(path)
        return dbInstance

def IndexDbRangeIter(conn, startWith = None):
    if startWith != None:
        if startWith[-1] == '%':
            firstExcl = startWith[:-1] + '^'
        else: 
            firstExcl = startWith + "~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~"
    else:
        firstExcl = None
    for key, value in conn.RangeIter(startWith, firstExcl, True):
        yield key, value

def IndexDbDelete(conn, key):
    conn.Delete(key)

def IndexDbPut(conn, key, value):
    conn.Put(key, value)

def IndexDbGet(conn, key, default=None):
    try:
        return conn.Get(key)
    except KeyError:
        return default

def ResetIndex():
    global indexDbPath
    if indexDbPath == None: return
    indexDb = OpenIndexDb(indexDbPath, readOnly = False)
    for key, value in IndexDbRangeIter(indexDb):
        IndexDbDelete(indexDb, key)

def DeleteFromIndex(indexDb, pattern, callback = None):
    assert pattern[-1] == '%'
    for key, value in IndexDbRangeIter(indexDb, pattern):
        IndexDbDelete(indexDb, key)
        if callback != None:
            callback(indexDb, key)

def ExtractPart(line, ordinal):
    return line.split('%%%')[ordinal]

def IterateOverFiles(compDb, indexDb):
    for entry in compDb:
        if 'command' in entry and 'file' in entry:
            command = entry['command'].split()
            if '++' in command[0]:
                fileName = os.path.normpath(entry['file'])

                # it could be startswith in the general case, but for my specific purposes I needed to check the middle of the string too -- AS
                if "/usr/include" in fileName:
                    continue
                flags = command[1:]

                lastModified = int(os.path.getmtime(fileName))
                yield (fileName, command, lastModified)

def GetNodeUseType(node):
    ret = node.kind.value
    if node.is_definition():
        ret = -ret
    return ret

def ExtractSymbols(indexDb, fileName, node):
    if node.location.file != None and os.path.normpath(node.location.file.name) != os.path.normpath(fileName):
        return

    symbol = node.get_usr()
    if symbol and node.spelling:
        parsingState = "Parsing %s : %s" % (fileName, node.spelling)
        IndexDbPut(indexDb, "spelling%%%" + symbol, node.spelling)
        IndexDbPut(indexDb, "c%%%" + fileName + "%%%" + symbol, '1')
        IndexDbPut(indexDb, "s%%%" + symbol + "%%%" + fileName + "%%%" + str(node.location.line) + "%%%" + str(node.location.column), str(GetNodeUseType(node)))
        IndexDbPut(indexDb, "n%%%" + node.spelling + "%%%" + fileName + "%%%" + str(node.location.line) + "%%%" + str(node.location.column), str(GetNodeUseType(node)))
    for c in node.get_children():
        ExtractSymbols(indexDb, fileName, c)

def GetSymbolSpelling(indexDb, symbol):
    return IndexDbGet(indexDb, "spelling%%%" + symbol, default = "(not found)")

def RemoveSymbol(indexDb, key):
    symbol = ExtractPart(key, 2)
    fname = ExtractPart(key, 1)

    DeleteFromIndex(indexDb, "s%%%" + symbol + "%%%" + fname + "%%%")
    DeleteFromIndex(indexDb, "n%%%" + GetSymbolSpelling(indexDb, symbol) + "%%%" + fname + "%%%")
    

def ParseFile(index, command, indexDb, fileName, lastModified, additionalInclude, parseHeaders):
    global parsingState

    lastKnown = int(IndexDbGet(indexDb, 'f%%%' + fileName, default = 0))

    if lastKnown < lastModified:
        parsingState = "Parsing %s" % fileName

        IndexDbPut(indexDb, 'F%%%' + os.path.basename(fileName) + "%%%" + fileName, '1')
        try:
            tu = index.parse(None, command + ["-I%s" % x for x in additionalInclude])
        except TranslationUnitLoadError as e:
            # TODO: handle failure
            return

        # set it to zero so that if we crash or stop while parse, it is reparsed when we are restarted
        IndexDbPut(indexDb, 'f%%%' + fileName, str(0))

        for x in tu.diagnostics:
            if x.severity >= 3:
                # TODO: remember errors
                pass

        # parse headers
        DeleteFromIndex(indexDb, "h%%%" + fileName + "%%%")
        includes = set()
        for incl in tu.get_includes():
            if incl.include:
                includes.add(os.path.normpath(incl.include.name))

        for incl in includes:
            IndexDbPut(indexDb, "h%%%" + fileName + "%%%" + incl, ' '.join(command))

        # parse symbols
        DeleteFromIndex(indexDb, "c%%%" + fileName + "%%%", RemoveSymbol)

        ExtractSymbols(indexDb, fileName, tu.cursor)

        IndexDbPut(indexDb, 'f%%%' + fileName, str(lastModified))

def UpdateCppIndex(clangLibraryPath, indexDbPath, compilationDbPath):
    global parsingState

    Config.set_library_path(clangLibraryPath)
    Config.set_compatibility_check(False)

    if indexDbPath == None:
        parsingState = "indexDbPath is not set"
        return
    if compilationDbPath == None:
        parsingState = "compilationDbPath is not set"
        return

    indexDb = IndexDbOpen(indexDbPath, readOnly = False, create = True)

    additionalInclude = getBuiltinHeaderPath(clangLibraryPath)

    if additionalInclude == None:
        parsingState = "Cannot find clang includes"
        return

    while True:
        with file(compilationDbPath) as f:
            compDb = json.loads(f.read())

            index = Index.create()

            # on the first scan we parse both headers and symbols
            for fileName, command, lastModified in IterateOverFiles(compDb, indexDb):
                ParseFile(index, command, indexDb, fileName, lastModified, additionalInclude, parseHeaders = True)

            # add all the header files to the compilation database we use
            for key, value in IndexDbRangeIter(indexDb, "h%%%"):
                compDb.append({'command': value, 'file': ExtractPart(key, 2)})

            # on the second scan we only parse symbols (it should only parse the headers added after the first scan)
            for fileName, command, lastModified in IterateOverFiles(compDb, indexDb):
                ParseFile(index, command, indexDb, fileName, lastModified, additionalInclude, parseHeaders = False)

        parsingState = "Sleeping"
        time.sleep(10)

lastLocations = []
lastRet = []

def JumpTo(filename, line, column):
  if filename != vim.current.buffer.name:
    try:
      vim.command("edit %s" % filename)
    except:
      # For some unknown reason, whenever an exception occurs in
      # vim.command, vim goes crazy and output tons of useless python
      # errors, catch those.
      return
  else:
    vim.command("normal m'")
  vim.current.window.cursor = (line, column - 1)

def NavigateToEntry(entry):
    global lastLocations
    if '[' in entry:
        id = int(entry[entry.find('[') + 1:entry.find(']')])
        loc = lastLocations[id]
        JumpTo(loc[0], int(loc[1]), int(loc[2]))

def GetItemsMatchingPattern(prefix, limit):
    global indexDbPath

    if indexDbPath == None:
        return []

    global lastLocations
    global lastRet

    ret = []
    locations = []

    ordinal = 0
    try:
        indexDb = IndexDbOpen(indexDbPath, readOnly = True)

        for key, value in IndexDbRangeIter(indexDb, 'n%%%' + prefix):
            if limit > 0:
                ret.append(ExtractPart(key, 1) + " - " + GetReferenceKind(int(value)) + " from " + (ExtractPart(key, 2)) + " [" + str(ordinal) + "]")
                locations.append([ExtractPart(key, 2), int(ExtractPart(key, 3)), int(ExtractPart(key, 4))])
                ordinal += 1
                limit -= 1
        for key, value in IndexDbRangeIter(indexDb, 'F%%%' + prefix):
            if limit > 0:
                ret.append(ExtractPart(key, 1) + " [" + str(ordinal) + "]")
                locations.append([ExtractPart(key, 2), 1, 1])
                ordinal += 1
                limit -= 1
        lastRet = ret
        lastLocations = locations

        return ret
    except Exception as e:
        raise
        return lastRet

def InitCtrlK(libraryPath):
    global compilationDbPath
    global indexDbPath
    global clangLibraryPath
    global parsingState
    global updateProcess

    clangLibraryPath = libraryPath

    Config.set_library_path(libraryPath)
    path = os.path.abspath(os.getcwd())
    parts = os.path.split(path)
    for i in range(len(parts)):
        path = os.path.join(*list(parts[:len(parts) - i] + ("compile_commands.json",)))
        if os.path.exists(path):
            compilationDbPath = path
            indexDbPath = os.path.join(*list(parts[:len(parts) - i] + (".ctrlk",)))
            break

    parsingState = "Ready to parse"

    if compilationDbPath == None:
        parsingState = "Cannot find compilation database"
    elif updateProcess == None:
        updateProcess = threading.Thread(target=UpdateCppIndex, args=(clangLibraryPath, indexDbPath, compilationDbPath))
        updateProcess.daemon = True
        updateProcess.start()

def LeaveCtrlK():
    """ for multiprocessing
    global updateProcess
    if updateProcess != None:
        os.kill(updateProcess.pid, signal.SIGKILL)
        """
    for thread in threading.enumerate():
        if thread.isAlive():
            thread._Thread__stop()

def GetCtrlKState():
    global parsingState
    print parsingState

def GetReferenceKind(val):
    isDef = False
    if val < 0:
        val = -val
        isDef = True
    if val in referenceKinds:
        ret = referenceKinds[val]
        if isDef:
            ret = ret.replace("declaration", "DEFINITION")
        return ret
    return "other"

referenceKinds = dict({
 1 : 'type declaration',
 2 : 'type declaration',
 3 : 'type declaration',
 4 : 'type declaration',
 5 : 'type declaration',
 6 : 'member declaration',
 7 : 'enum declaration',
 8 : 'function declaration',
 9 : 'variable declaration',
10 : 'argument declaration',
20 : 'typedef declaration',
21 : 'method declaration',
22 : 'namespace declaration',
24 : 'constructor declaration',
25 : 'destructor declaration',
26 : 'conversion function declaration',
27 : 'template type parameter',
28 : 'non-type template parameter',
29 : 'template template parameter',
30 : 'function template declaration',
31 : 'class template declaration',
32 : 'class template partial specialization',
33 : 'namespace alias',
43 : 'type reference',
44 : 'base specifier',
45 : 'template reference',
46 : 'namespace reference',
47 : 'member reference',
48 : 'label reference',
49 : 'overloaded declaration reference',
100 : 'expression',
101 : 'reference',
102 : 'member reference',
103 : 'function call'
})


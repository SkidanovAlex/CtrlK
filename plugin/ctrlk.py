import leveldb
import json
import os.path
import vim
import threading
import time
import traceback
import ctypes

from clang.cindex import Index, Config, TranslationUnitLoadError, CursorKind, File, SourceLocation, Cursor, TranslationUnit

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
#   ndef%%%<spelling>%%%<symbol>%%%<file_name>%%%<line>%%%<col>%%%<spelling_with_class> => <use_type>
#      Definitions for symbol navigation
#
#   ndecl%%%<spelling>%%%<symbol>%%%<file_name>%%%<line>%%%<col>%%%<spelling_with_class> => <use_type>
#      Declarations for symbol navigation
#
#   ndefsuf%%%<spelling suffix>%%%<symbol>%%%<file_name>%%%<line>%%%<col>%%%<spelling_with_class> => <use_type>
#      Suffixes of definitions for symbol navigation
#
#   ndeclsuf%%%<spelling suffix>%%%<symbol>%%%<file_name>%%%<line>%%%<col>%%%<spelling_with_class> => <use_type>
#      Suffixes of declarations for symbol navigation
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
updateFileProcess = None

parsingState = ""
parsingCurrentState = ""

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
                os.makedirs(path)
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
    indexDb = IndexDbOpen(indexDbPath, readOnly = False)
    for key, value in IndexDbRangeIter(indexDb):
        IndexDbDelete(indexDb, key)

def DeleteFromIndex(indexDb, pattern, callback = None):
    assert pattern[-1] == '%'
    for key, value in IndexDbRangeIter(indexDb, pattern):
        IndexDbDelete(indexDb, key)
        if callback != None:
            callback(indexDb, key)

# ==== Maintaining the index

def ExtractPart(line, ordinal):
    return line.split('%%%')[ordinal]

def IterateOverFiles(compDb, indexDb):
    for entry in compDb:
        if 'command' in entry and 'file' in entry:
            command = entry['command'].split()
            if '++' in command[0] or "cc" in command[0] or "clang" in command[0]:
                fileName = os.path.abspath(entry['file'])

                # it could be startswith in the general case, but for my specific purposes I needed to check the middle of the string too -- AS
                if "/usr/include" in fileName:
                    continue

                if not os.path.exists(fileName):
                    continue

                lastModified = int(os.path.getmtime(fileName))
                yield (fileName, command, lastModified)

def GetNodeUseType(node):
    ret = node.kind.value
    if node.is_definition():
        ret = -ret
    return ret

# pos = 0 means it is not a suffix, it is full string
def DbEntryPrefix(pos, node):
    ret = 'n'

    if node.is_definition():
        ret += "def"
    else:
        ret += "decl"

    if pos != 0:
        ret += "suf"
    return ret

def ExtractSymbols(indexDb, fileName, node, parentSpelling):
    if node.location.file != None and os.path.normpath(node.location.file.name) != os.path.normpath(fileName):
        return

    if node.location.file != None:
        try:
            symbol = node.get_usr()
            spelling = node.spelling
            if symbol and not spelling:
                spelling = node.displayname
            addToN = True
            if node.referenced:
                if not symbol: 
                    symbol = node.referenced.get_usr()
                    addToN = False
                if not spelling: spelling = node.referenced.spelling
                if symbol and not spelling:
                    spelling = node.displayname
            if symbol and spelling:
                if parentSpelling != "": parentSpelling += "::"
                parentSpelling += spelling
                IndexDbPut(indexDb, "spelling%%%" + symbol, spelling)
                IndexDbPut(indexDb, "c%%%" + fileName + "%%%" + symbol, '1')
                IndexDbPut(indexDb, "s%%%" + symbol + "%%%" + fileName + "%%%" + str(node.location.line) + "%%%" + str(node.location.column), str(GetNodeUseType(node)))
                if addToN:
                    for i in range(len(spelling)):
                        suffix = spelling[i:].lower()
                        IndexDbPut(indexDb, DbEntryPrefix(i, node) + "%%%" + suffix + "%%%" + symbol + "%%%" + fileName + "%%%" + str(node.location.line) + "%%%" + str(node.location.column) + "%%%" + node.displayname, str(GetNodeUseType(node)))
        except ValueError:
            pass
    for c in node.get_children():
        ExtractSymbols(indexDb, fileName, c, parentSpelling)

def GetSymbolSpelling(indexDb, symbol):
    return IndexDbGet(indexDb, "spelling%%%" + symbol, default = "(not found)")

def RemoveSymbol(indexDb, key):
    symbol = ExtractPart(key, 2)
    fname = ExtractPart(key, 1)
    spelling = GetSymbolSpelling(indexDb, symbol)

    DeleteFromIndex(indexDb, "s%%%" + symbol + "%%%" + fname + "%%%")
    DeleteFromIndex(indexDb, "ndef%%%" + spelling.lower() + "%%%" + symbol + "%%%" + fname + "%%%")
    DeleteFromIndex(indexDb, "ndecl%%%" + spelling.lower() + "%%%" + symbol + "%%%" + fname + "%%%")
    for i in range(1, len(spelling)):
        suffix = spelling[i:].lower()
        DeleteFromIndex(indexDb, "ndefsuf%%%" + suffix + "%%%" + symbol + "%%%" + fname + "%%%")
        DeleteFromIndex(indexDb, "ndeclsuf%%%" + suffix + "%%%" + symbol + "%%%" + fname + "%%%")
    

def ParseFile(index, command, indexDb, fileName, lastModified, additionalInclude, parseHeaders):
    global parsingState

    lastKnown = int(IndexDbGet(indexDb, 'f%%%' + fileName, default = 0))

    if lastKnown < lastModified:
        parsingState = "Parsing %s" % fileName

        IndexDbPut(indexDb, 'F%%%' + os.path.basename(fileName).lower() + "%%%" + fileName, '1')
        try:
            tu = index.parse(None, command + ["-I%s" % additionalInclude], options = TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD)
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

        ExtractSymbols(indexDb, fileName, tu.cursor, "")

        IndexDbPut(indexDb, 'f%%%' + fileName, str(lastModified))

        parsingState = "Looking for files to parse"

def UpdateCppIndexThread(clangLibraryPath, indexDbPath, compilationDbPath):
    global parsingState

    try:
        Config.set_library_path(clangLibraryPath)
        Config.set_compatibility_check(False)

        if indexDbPath == None:
            parsingState = "indexDbPath is not set"
            return
        if compilationDbPath == None:
            parsingState = "compilationDbPath is not set"
            return

        additionalInclude = getBuiltinHeaderPath(clangLibraryPath)

        if additionalInclude == None:
            parsingState = "Cannot find clang includes"
            return

        while True:
            try:
                indexDb = IndexDbOpen(indexDbPath, readOnly = False, create = True)

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
            except leveldb.LevelDBError:
                parsingState = "Failed to write to LevelDB. Likely there is a concurrent VIM instance updating index."
            time.sleep(10)
    except Exception as e:
        parsingState = "Failed with %s" % (str(e))
    except SystemExit as e:
        pass

# === Symbol lookup

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
    if not lastLocations:
        return
    if '[' in entry:
        id = int(entry[entry.find('[') + 1:entry.find(']')])
        loc = lastLocations[id]
        JumpTo(loc[0], int(loc[1]), int(loc[2]))

def GetItemsMatchingPattern(prefix, limit):
    global indexDbPath

    if indexDbPath == None:
        return []

    if prefix == "" or prefix == None:
        return ["Search for a function, class, variable, or file name."]

    global lastLocations
    global lastRet

    ret = []
    locations = []

    ordinal = 0
    try:
        indexDb = IndexDbOpen(indexDbPath, readOnly = True)

        for key, value in IndexDbRangeIter(indexDb, 'F%%%' + prefix.lower()):
            if limit > 0:
                full_path = ExtractPart(key, 2)
                ret.append(os.path.basename(full_path) + " (" + full_path + ") [" + str(ordinal) + "]")
                locations.append([ExtractPart(key, 2), 1, 1])
                ordinal += 1
                limit -= 1
            else:
                break
        for dbPrefix in ["ndef", "ndefsuf", "ndecl", "ndeclsuf"]:
            for key, value in IndexDbRangeIter(indexDb, dbPrefix + '%%%' + prefix.lower()):
                if limit > 0:
                    ret.append(ExtractPart(key, 6) + " - " + GetReferenceKind(int(value)) + " from " + (ExtractPart(key, 3)) + " [" + str(ordinal) + "]")
                    locations.append([ExtractPart(key, 3), int(ExtractPart(key, 4)), int(ExtractPart(key, 5))])
                    ordinal += 1
                    limit -= 1
                else:
                    break
        lastRet = ret
        lastLocations = locations

        return ret
    except Exception as e:
        return lastRet

# === Goto defintion / declaration

parseLock = threading.Lock()
parseFile = ""
parseContent = ""
parseNeeded = False

parseTus = {}

def ParseCurrentFileThread(clangLibraryPath, indexDbPath):
    global parsingCurrentState

    try:
        additionalInclude = getBuiltinHeaderPath(clangLibraryPath)
        if additionalInclude == None:
            parsingCurrentState = "Cannot find clang includes"
            return

        if indexDbPath == None:
            parsingCurrentState = "indexDbPath is not set"
            return

        indexDb = IndexDbOpen(indexDbPath, readOnly = True, create = True)

        while True:
            time.sleep(0.1)
            ParseCurrentFile(indexDb, additionalInclude)
    except Exception as e:
        parsingCurrentState = "Failed with %s" % (traceback.format_exc(e))
    except SystemExit as e:
        pass

def ParseCurrentFile(indexDb, additionalInclude):
    global parseLock
    global parseFile
    global parseContent
    global parseNeeded
    global parseLastFile
    global parseTus
    global parsingCurrentState

    with parseLock:
        if not parseNeeded:
            return
        fileToParse = parseFile
        contentToParse = parseContent
        parseNeeded = False
        parsingCurrentState = "Parsing %s" % fileToParse

    command = None
    # HACK! FIXME! DON'T LEAVE ME LIKE THIS
    # Hope this file has at least one header file, that header file will conviniently have command line args for this file
    for k, v in IndexDbRangeIter(indexDb, "h%%%" + fileToParse):
        command = v
        break

    if command == None:
        parsingCurrentState = "Can't find command line arguments"
        return

    index = Index.create()
    tu = index.parse(None, command.split() + ["-I%s" % additionalInclude], unsaved_files=[(fileToParse, contentToParse)], options = TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD)

    with parseLock:
        parseTus[fileToParse] = tu
        parsingCurrentState = "Parsed %s" % fileToParse

def RequestParse():
    global parseLock
    global parseFile
    global parseContent
    global parseNeeded
    with parseLock:
        parseFile = vim.current.buffer.name
        if parseFile != None:
            parseContent = "\n".join(vim.current.buffer[:] + ["\n"])
            parseNeeded = True

def CtrlKBufferUnload(s):
    parseTus.pop(s, None)

def GetCurrentTranslationUnit():
    global parseLock
    global parseLastFile
    global parseTus
    with parseLock:
        curName = vim.current.buffer.name
        if curName is not None and curName in parseTus:
            return parseTus[curName]
        return None

def GetCurrentUsrCursor(tu):
    line, col = vim.current.window.cursor
    col = col + 1
    f = File.from_name(tu, vim.current.buffer.name)
    loc = SourceLocation.from_position(tu, f, line, col)
    cursor = Cursor.from_location(tu, loc)

    while cursor is not None and (not cursor.referenced or not cursor.referenced.get_usr()):
        nextCursor = cursor.lexical_parent
        if nextCursor is not None and nextCursor == cursor:
            return None
        cursor = nextCursor
    if cursor is None:
        return None
    return cursor.referenced

def GoToDefinition():
    global indexDbPath
    if indexDbPath == None: return
    indexDb = IndexDbOpen(indexDbPath, readOnly = True)

    tu = GetCurrentTranslationUnit()
    if tu is not None:
        cursor = GetCurrentUsrCursor(tu)
        if cursor is not None:
            usr = cursor.get_usr()
            for k, v in IndexDbRangeIter(indexDb, "s%%%" + usr + "%%%"):
                if int(v) < 0:
                    JumpTo(ExtractPart(k, 2), int(ExtractPart(k, 3)), int(ExtractPart(k, 4)))
                    return

            # For macros is_definition is always false => we always store their type as a positive number =>
            #    condition in the loop above is always false. It is actually a good thing, because in case
            #    of macros if several header files in the project declare the same macro, it will have the
            #    same USR in all of them. When we want to go to definition, we want to go to the one which
            #    is visible from the current location, which is exactly what cursor.location points right now,
            #    so just use it instead of the project database.
            # It is also a good fall back for the case when we cannot find someting in the database (file is
            #    not parsed yet, or failed to parse) -- we will jump to the visible declaration of the symbol
            #
            JumpTo(cursor.location.file, cursor.location.line, cursor.location.column)



def FindReferences():
    global indexDbPath
    if indexDbPath == None: return
    indexDb = IndexDbOpen(indexDbPath, readOnly = True)

    ret = []
    tu = GetCurrentTranslationUnit()
    if tu is not None:
        cursor = GetCurrentUsrCursor(tu)
        if cursor is not None:
            usr = cursor.get_usr()
            for k, v in IndexDbRangeIter(indexDb, "s%%%" + usr + "%%%"):
                fileName = ExtractPart(k, 2)
                line = int(ExtractPart(k, 3))
                col = int(ExtractPart(k, 4))
                ret.append({'filename': fileName, 'lnum': line, 'col': col, 'text': GetReferenceKind(int(v)), 'kind': abs(int(v))})

    return ret

def GetCurrentScopeStrInternal(cursor, pos):
    for ch in cursor.get_children():
        if ch.extent.start.line <= pos and ch.extent.end.line >= pos and str(ch.extent.end.file) == str(cursor.extent.end.file):
            ret = ''
            if ch.spelling is not None:
              ret = ch.spelling
            other = GetCurrentScopeStrInternal(ch, pos)
            if '' != other:
              if ret != '': ret += '::'
              ret += other
            if '' != ret:
              return ret
    return ''

def GetCurrentScopeStr():
    line, col = vim.current.window.cursor

    tu = GetCurrentTranslationUnit()
    if tu is None:
        return ""
  
    return GetCurrentScopeStrInternal(tu.cursor, line)

def InitCtrlK(libraryPath):
    global compilationDbPath
    global indexDbPath
    global clangLibraryPath
    global parsingState
    global parsingCurrentState
    global updateProcess
    global updateFileProcess

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
    parsingCurrentState = "Ready to parse"

    if compilationDbPath == None:
        parsingState = "Cannot find compilation database"
    elif updateProcess == None:
        updateProcess = threading.Thread(target=UpdateCppIndexThread, args=(clangLibraryPath, indexDbPath, compilationDbPath))
        updateProcess.daemon = True
        updateProcess.start()

        updateFileProcess = threading.Thread(target=ParseCurrentFileThread, args=(clangLibraryPath, indexDbPath))
        updateFileProcess.daemon = True
        updateFileProcess.start()

def terminate_thread(thread):
    if not thread.isAlive():
        return

    exc = ctypes.py_object(SystemExit)
    res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_long(thread.ident), exc)
    if res == 0:
        raise ValueError("nonexistent thread id")
    elif res > 1:
        # """if it returns a number greater than one, you're in trouble,
        # and you should call it again with exc=NULL to revert the effect"""
        ctypes.pythonapi.PyThreadState_SetAsyncExc(thread.ident, None)
        raise SystemError("PyThreadState_SetAsyncExc failed")

def LeaveCtrlK():
    global updateProcess
    global updateFileProcess

    if updateProcess:
        terminate_thread(updateProcess)
    if updateFileProcess:
        terminate_thread(updateFileProcess)

def GetCtrlKState():
    global parsingState
    global parsingCurrentState
    print "Index: %s / Current: %s" % (parsingState, parsingCurrentState)

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
103 : 'function call',
501 : 'macro declaraion',
502 : 'macro instantiation'
})


import os.path
import vim
import threading
import time
import traceback
import ctypes
import socket

from clang.cindex import Index, Config, TranslationUnitLoadError, CursorKind, File, SourceLocation, Cursor, TranslationUnit

from ctrlk import project
from ctrlk import search

g_project = None

updateProcess = None
listeningProcess = None
updateFileProcess = None

parsingState = ""
parsingCurrentState = ""
jumpState = "normal"

#def ResetIndex():
#    global indexDbPath
#    if indexDbPath == None: return
#    indexDb = IndexDbOpen(indexDbPath, readOnly = False)
#    for key, value in IndexDbRangeIter(indexDb):
#        IndexDbDelete(indexDb, key)
#
#def DeleteFromIndex(indexDb, pattern, callback = None):
#    assert pattern[-1] == '%'
#    for key, value in IndexDbRangeIter(indexDb, pattern):
#        IndexDbDelete(indexDb, key)
#        if callback != None:
#            callback(indexDb, key)

#def GetSymbolSpelling(indexDb, symbol):
#    return IndexDbGet(indexDb, "spelling%%%" + symbol, default = "(not found)")
#
#def RemoveSymbol(indexDb, key):
#    symbol = search.extract_part(key, 2)
#    fname = search.extract_part(key, 1)
#    spelling = GetSymbolSpelling(indexDb, symbol)
#
#    DeleteFromIndex(indexDb, "s%%%" + symbol + "%%%" + fname + "%%%")
#    DeleteFromIndex(indexDb, "ndef%%%" + spelling.lower() + "%%%" + symbol + "%%%" + fname + "%%%")
#    DeleteFromIndex(indexDb, "ndecl%%%" + spelling.lower() + "%%%" + symbol + "%%%" + fname + "%%%")
#    for i in range(1, len(spelling)):
#        suffix = spelling[i:].lower()
#        DeleteFromIndex(indexDb, "ndefsuf%%%" + suffix + "%%%" + symbol + "%%%" + fname + "%%%")
#        DeleteFromIndex(indexDb, "ndeclsuf%%%" + suffix + "%%%" + symbol + "%%%" + fname + "%%%")
    

#def ParseFile(index, command, indexDb, fileName, lastModified, additionalInclude, parseHeaders):
#    global parsingState
#
#    lastKnown = int(IndexDbGet(indexDb, 'f%%%' + fileName, default = 0))
#
#    if lastKnown < lastModified:
#        parsingState = "Parsing %s" % fileName
#
#        IndexDbPut(indexDb, 'F%%%' + os.path.basename(fileName).lower() + "%%%" + fileName, '1')
#        try:
#            tu = index.parse(None, command + ["-I%s" % additionalInclude], options = TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD)
#        except TranslationUnitLoadError as e:
#            # TODO: handle failure
#            return
#
#        # set it to zero so that if we crash or stop while parse, it is reparsed when we are restarted
#        IndexDbPut(indexDb, 'f%%%' + fileName, str(0))
#
#        for x in tu.diagnostics:
#            if x.severity >= 3:
#                # TODO: remember errors
#                pass
#
#        # parse headers
#        DeleteFromIndex(indexDb, "h%%%" + fileName + "%%%")
#        includes = set()
#        for incl in tu.get_includes():
#            if incl.include:
#                includes.add(os.path.normpath(incl.include.name))
#
#        for incl in includes:
#            IndexDbPut(indexDb, "h%%%" + fileName + "%%%" + incl, ' '.join(command))
#
#        # parse symbols
#        DeleteFromIndex(indexDb, "c%%%" + fileName + "%%%", RemoveSymbol)
#
#        ExtractSymbols(indexDb, fileName, tu.cursor)
#
#        IndexDbPut(indexDb, 'f%%%' + fileName, str(lastModified))
#
#        parsingState = "Looking for files to parse"

def UpdateCppIndexThread():
    global parsingState

    try:
        while True:
            start_time = time.time()
            g_project.scan_and_index()
            g_project.wait_on_work()
            end_time = time.time()
            parsingState = "sleeping (last sweep = %g)" % (end_time-start_time)
            time.sleep(10)
    except Exception as e:
        parsingState = "Failed with %s" % (str(e))
    except SystemExit as e:
        pass

# === Symbol lookup

lastLocations = []
lastRet = []

def GetItemsMatchingPattern(prefix, limit):
    global lastRet, lastLocations
    global jumpState
    lastRet, lastLocations = search.get_items_matching_pattern(g_project.leveldb_connection, prefix, limit)
    return lastRet

def JumpTo(filename, line, column):
  global jumpState  
  if filename != vim.current.buffer.name:
    try:
      vim.command("edit %s" % filename)
    except:
      # For some unknown reason, whenever an exception occurs in
      # vim.command, vim goes crazy and output tons of useless python
      # errors, catch those.
      jumpState = traceback.format_exc()
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


# === Goto defintion / declaration

parseLock = threading.Lock()
parseFile = ""
parseContent = ""
parseNeeded = False

parseTus = {}

def ParseCurrentFileThread():
    global parsingCurrentState

    try:
        while True:
            time.sleep(0.1)
            ParseCurrentFile()
    except Exception as e:
        parsingCurrentState = "Failed with %s" % (traceback.format_exc(e))
    except SystemExit as e:
        pass

def ParseCurrentFile():
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

    _, command, _ = g_project.get_file_args(fileToParse)

    if command == None:
        parsingCurrentState = "Can't find command line arguments"
        return

    index = Index.create()
    tu = index.parse(None, command + ["-I%s" % g_project.builtin_header_path], unsaved_files=[(fileToParse, contentToParse)], options = TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD)

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
    tu = GetCurrentTranslationUnit()
    if tu is not None:
        cursor = GetCurrentUsrCursor(tu)
        if cursor is not None:
            usr = cursor.get_usr()
            for k, v in search.leveldb_range_iter(g_project.leveldb_connection, "s%%%" + usr + "%%%"):
                if int(v) < 0:
                    JumpTo(search.extract_part(k, 2), int(search.extract_part(k, 3)), int(search.extract_part(k, 4)))
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
    ret = []
    tu = GetCurrentTranslationUnit()
    if tu is not None:
        cursor = GetCurrentUsrCursor(tu)
        if cursor is not None:
            usr = cursor.get_usr()
            for k, v in search.leveldb_range_iter(g_project.leveldb_connection, "s%%%" + usr + "%%%"):
                fileName = search.extract_part(k, 2)
                line = int(search.extract_part(k, 3))
                col = int(search.extract_part(k, 4))
                ret.append({'filename': fileName, 'lnum': line, 'col': col, 'text': search.get_reference_kind(int(v)), 'kind': abs(int(v))})

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
    global g_project
    global parsingState
    global parsingCurrentState
    global updateProcess
    global updateFileProcess

    start_time = time.time()
    try:
        g_project = project.Project(libraryPath, os.path.abspath(os.getcwd()))
    except Exception as e:
        parsingState = str(e)
        return
    end_time = time.time()
    
    parsingState = "Ready to parse %g" % (end_time-start_time)
    parsingCurrentState = "Ready to parse"

    if updateProcess == None:
        updateProcess = threading.Thread(target=UpdateCppIndexThread)
        updateProcess.daemon = True
        updateProcess.start()

        updateFileProcess = threading.Thread(target=ParseCurrentFileThread)
        updateFileProcess.daemon = True
        updateFileProcess.start()

def LeaveCtrlK():
    pass

def GetCtrlKState():
    global parsingState
    global parsingCurrentState
    print "Index: %s / Current: %s / Jump: %s" % (parsingState, parsingCurrentState, jumpState)

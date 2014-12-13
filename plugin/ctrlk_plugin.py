import os.path
import vim
import threading
import time
import traceback
import ctypes
import socket
import subprocess
import sys
import json

from clang.cindex import Index, Config, TranslationUnitLoadError, CursorKind, File, SourceLocation, Cursor, TranslationUnit

import requests
from ctrlk import client_api
from ctrlk import search
from ctrlk import ctrlk_server

g_api = None

g_builtin_header_path = None
updateProcess = None
listeningProcess = None
updateFileProcess = None

parsingState = ""
parsingCurrentState = ""
jumpState = "normal"

def UpdateCppIndexThread():
    global parsingState

    try:
        while True:
            if g_api is not None:
                queue_size = g_api.get_queue_size()
                if queue_size <= 1:
                    g_api.parse()
                parsingState = "Parse Queue Size = %d" % (queue_size)
            time.sleep(10)
    except Exception as e:
        parsingState = "Failed with %s" % (str(e))
    except SystemExit as e:
        pass

# === Symbol lookup

lastLocations = []
lastRet = []

def GetItemsMatchingPattern(prefix, limit):
    if not g_api:
        return ["CtrlK is not Running"]

    global lastRet, lastLocations
    global jumpState
    try:
        lastRet, lastLocations = g_api.get_items_matching_pattern(prefix, limit)
    except Exception as e:
        return [str(e)]
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

def FollowDef(filename, line):
    vim.command("call CtrlKOpenFileInFollowWindow('%s', %d)" % (filename, line))

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
parseScopeNames = {}

def ParseCurrentFileThread():
    global parsingCurrentState

    try:
        while True:
            time.sleep(0.1)
            ParseCurrentFile()
    except Exception as e:
        with parseLock:
            parsingCurrentState = "Failed with %s" % (traceback.format_exc(e))
        pass
    except SystemExit as e:
        pass

def PopulateScopeNames(cursor, scopeNames, scopeDepths, depth = 0):
    if cursor is None:
        return
    for ch in cursor.get_children():
        if ch.extent and ch.extent.start and ch.extent.end and cursor.extent and cursor.extent.end:
            if str(ch.extent.end.file) == str(cursor.extent.end.file):
                if ch.spelling is not None:
                    for i in range(ch.extent.start.line, ch.extent.end.line + 1):
                        while len(scopeNames) <= i:
                            scopeNames.append('')
                            scopeDepths.append(-1)

                        if scopeDepths[i] < depth: 
                            scopeDepths[i] = depth
                            if scopeNames[i] != '': scopeNames[i] += '::'
                            scopeNames[i] += ch.spelling

                PopulateScopeNames(ch, scopeNames, scopeDepths, depth + 1)

def GetCursorForFile(tu, fileName):
    cursor = tu.cursor
    if str(cursor.extent.start.file) == str(cursor.extent.end.file) and os.path.abspath(str(cursor.extent.start.file)) == fileName:
        return cursor

    # TODO
    return None

def ParseCurrentFile():
    global parseLock
    global parseFile
    global parseContent
    global parseNeeded
    global parseLastFile
    global parseTus
    global parseScopeNames
    global parsingCurrentState

    with parseLock:
        if not parseNeeded:
            return
        fileToParse = parseFile
        contentToParse = parseContent
        parseNeeded = False
        parsingCurrentState = "Parsing %s" % fileToParse

    command = None
    try:
        if g_api is not None:
            command = g_api.get_file_args(fileToParse)
    except Exception as e:
        with parseLock:
            parsingCurrentState = str(e)
        return

    parsingStatePrefix = ""
    unsaved_files = [(fileToParse, contentToParse)]
    if command == None:
        if fileToParse.endswith(".h") or fileToParse.endswith(".hpp"):
            unsaved_files.append(("temporary_source.cpp", "#include \"%s\"\n#include <stdint.h>\nint main() { return 0; }\n" % fileToParse))
            command = ["g++", "temporary_source.cpp"]
            parsingStatePrefix = "[HEADER] "
        elif fileToParse.endswith(".cpp") or fileToParse.endswith(".cc") or fileToParse.endswith(".c") or fileToParse.endswith(".cxx"):
            command = ["g++", fileToParse]
            parsingStatePrefix = "[SOURCE] "
        else:
            with parseLock:
                parsingCurrentState = "Can't find command line arguments"
            return

    index = Index.create()
    tu = index.parse(None, command + ["-I%s" % g_builtin_header_path], unsaved_files=unsaved_files, options = TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD)

    with parseLock:
        parseTus[fileToParse] = tu
        parsingCurrentState = "%sPartially parsed %s" % (parsingStatePrefix, fileToParse)

    scopeNames = []
    scopeDepths = []
    # create a new tu so that we don't walk it from two different threads
    index = Index.create()
    tu = index.parse(None, command + ["-I%s" % g_builtin_header_path], unsaved_files=unsaved_files, options = TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD)
    PopulateScopeNames(GetCursorForFile(tu, os.path.abspath(fileToParse)), scopeNames, scopeDepths)

    with parseLock:
        parseScopeNames[fileToParse] = scopeNames
        parsingCurrentState = "%sFully parsed %s" % (parsingStatePrefix, fileToParse)

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
    with parseLock:
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

def GoToDefinition(mode):
    global parsingCurrentState

    tu = GetCurrentTranslationUnit()
    if tu is not None:
        cursor = GetCurrentUsrCursor(tu)
        if cursor is not None:
            usr = cursor.get_usr()
            try:
                if g_api is not None:
                    for k, v in g_api.leveldb_search("s%%%" + usr + "%%%"):
                        if int(v) < 0:
                            if mode == 'j':
                                vim.command('rightbelow split')
                            elif mode == 'l':
                                vim.command('rightbelow vsplit')
                            if mode != 'f':
                                JumpTo(search.extract_part(k, 2), int(search.extract_part(k, 3)), int(search.extract_part(k, 4)))
                            else:
                                FollowDef(search.extract_part(k, 2), int(search.extract_part(k, 3)))
                            return
            except Exception as e:
                with parseLock:
                    parsingCurrentState = str(e)

            # For macros is_definition is always false => we always store their type as a positive number =>
            #    condition in the loop above is always false. It is actually a good thing, because in case
            #    of macros if several header files in the project declare the same macro, it will have the
            #    same USR in all of them. When we want to go to definition, we want to go to the one which
            #    is visible from the current location, which is exactly what cursor.location points right now,
            #    so just use it instead of the project database.
            # It is also a good fall back for the case when we cannot find someting in the database (file is
            #    not parsed yet, or failed to parse) -- we will jump to the visible declaration of the symbol
            #
            if mode == 'j':
                vim.command('rightbelow split')
            elif mode == 'l':
                vim.command('rightbelow vsplit')
            if mode != 'f':
                JumpTo(cursor.location.file, cursor.location.line, cursor.location.column)
            else:
                FollowDef(cursor.location.file, cursor.location.line)



def FindReferences():
    global parsingCurrentState
    ret = []
    if not g_api:
        return ret
    tu = GetCurrentTranslationUnit()
    if tu is not None:
        cursor = GetCurrentUsrCursor(tu)
        if cursor is not None:
            usr = cursor.get_usr()
            try:
                for k, v in g_api.leveldb_search("s%%%" + usr + "%%%"):
                    fileName = search.extract_part(k, 2)
                    line = int(search.extract_part(k, 3))
                    col = int(search.extract_part(k, 4))

                    kindText = search.get_reference_kind(int(v))
                    text = "[   ]"
                    if "DEFINITION" in kindText:
                        text = "[DEF]"
                    elif "declaration" in kindText:
                        text = "[Dcl]"
                    elif "reference" in kindText:
                        text = "[ref]"

                    try:
                        with open(fileName) as f:
                            lines = [x for x in f]
                        text += ' %s' % lines[line - 1].strip()
                    except:
                        pass

                    ret.append({'filename': fileName, 'lnum': line, 'col': col, 'text': text, 'kind': abs(int(v))})
            except Exception as e:
                with parseLock:
                    parsingCurrentState = str(e)

    return ret

def GetCurrentScopeStr():
    global parseLock
    global parseLastFile
    global parseTus
    with parseLock:
        curName = vim.current.buffer.name
        line, col = vim.current.window.cursor
        if curName is not None and curName in parseScopeNames and line < len(parseScopeNames[curName]):
            return parseScopeNames[curName][line]
        return "(no scope)"

def try_initialize(libraryPath):
    global g_api
    global parsingState

    try:
        g_api = client_api.CtrlKApi()
        g_api.register(libraryPath, os.path.abspath(os.getcwd()))
        parsingState = "Ready to parse"
        return True
    except Exception as e:
        g_api = None
        parsingState = "CtrlK Failed to Initialize: %s" % (str(e))
        return False

def api_init_thread(libraryPath):
    global g_api
    global g_builtin_header_path
    global parsingState
    global parsingCurrentState
    global updateProcess
    global updateFileProcess

    Config.set_library_path(libraryPath)
    Config.set_compatibility_check(False)

    parsingState = "Initializing"
    parsingCurrentState = "Initializing"

    if not try_initialize(libraryPath):
        server_path = ctrlk_server.get_absolute_path()
        with open('/tmp/ctrlk_server_stdout', 'a') as server_stdout:
            with open('/tmp/ctrlk_server_stderr', 'a') as server_stderr:
                subprocess.Popen(['python', server_path, '--port', str(client_api.DEFAULT_PORT), '--suicide-seconds', '3600'],\
                        stdout=server_stdout, stderr=server_stderr)

        for i in range(30):
            if try_initialize(libraryPath):
                break
            time.sleep(0.1)
        else:
            parsingState = "Failed to initialize"
            pass
    
    if g_api is not None:
        g_builtin_header_path = g_api.get_builtin_header_path()

    parsingCurrentState = "Ready to parse"

    if updateProcess == None:
        updateProcess = threading.Thread(target=UpdateCppIndexThread)
        updateProcess.daemon = True
        updateProcess.start()

        updateFileProcess = threading.Thread(target=ParseCurrentFileThread)
        updateFileProcess.daemon = True
        updateFileProcess.start()

def InitCtrlK(libraryPath):
    t = threading.Thread(target=api_init_thread, args=(libraryPath,))
    t.daemon = True
    t.start()

def LeaveCtrlK():
    pass

def GetCtrlKState():
    global parsingState
    global parsingCurrentState
    with parseLock:
        print "Index: %s / Current: %s / Jump: %s" % (parsingState, parsingCurrentState, jumpState)
